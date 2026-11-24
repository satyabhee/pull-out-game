"""Replayed vs real fills: match rate overall / per asset / per minute, and the mismatch table.

    python -m analysis.validate_replay <window_dir> --hl-data <dir> [--policy listing] [--out DIR]
                                       [--anchors FILE] [--no-resolver]
                                       [--alo-gate touch [--gate-max-age-ms MS]] [--evict-known oldest|newest]

Unit of scoring: one taker tx (hash). Real fills = recorded trades with that hash; engine
fills = the engine's taker fills for that hash's actions (outside fills never count).

Scored population (section 4 / ENGINE_WINDOW_RULES rule 4), decided from the chain side only
so it cannot depend on engine behavior:
  - the tx is in the window and its block is at or after warmup_end_height (per-minute rows
    also show the warm-up minutes, flagged);
  - it has real fills or engine fills (txs with neither are not trades);
  - every real maker placed an order in the window on that side and price before the tx
    ("maker order placed after window start"; HL maker oids are not public, so this is the
    observable proxy). Txs failing this are reported as excluded: real maker pre-window.

Match: same fills per (price, maker address), size summed: exact. `match_px_sz` relaxes to
per-price size only (the G0 criterion).

Mismatch category, first that applies (every flag is also written per tx):
  outside-fill timing    an outside fill hit this asset in the same block, or one found no maker
  wrong ALO decision     an ALO in play (an engine maker order, or a real maker's ALO at that
                         price) whose engine decision contradicts alo_status.jsonl, joined by
                         (block, hash, item)
  unknown pre-window order  the engine filled against placeholder liquidity
  unresolved cancel      an engine maker order that an unresolved int-oid cancel/modify by its owner
                         (sent after it was placed) may have targeted: the cancel's oid is within
                         the resolver's tolerance of the order's estimated oid. Missed cloid cancels
                         name a cloid we don't hold, so they can't be pinned to an order.
  ordering               same size at every price as the chain, different makers (queue position)
  margin rejection (unobservable)  an engine maker order whose owner is not among the real makers:
                         the order likely never rested on chain (silent reject)
  real bug (unexplained) none of the above; the first 20 come with the book before the tx

Informational flag (not a category): "info: engine rejected a real maker's ALO at that price",
set without needing alo_status truth, to size the unverified share of lead (b).

Outputs (CSV, in --out): summary.csv, per_minute.csv, mismatches.csv, txs.csv, alo_truth.csv,
first_mismatches.csv.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import gc
import gzip
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from data.parse import iter_trades, recorder_hours
from engine_py.matching import PLACEHOLDER, Engine
from engine_py.replay import COINS, OUTSIDE_FILL_POSITION, Observer, Replay, listing
from engine_py.types import (ASSETS, Cancel, CancelByCloid, Modify, OutsideFill, Place, RejectReason, Side,
                             Tif, ZERO_HASH)



# --- G1 triage (H1), THROWAWAY measurement policies: not the ordering track's policies (engine_py/ordering.py).
# Hyperliquid docs: within a block, actions that send no GTC/IOC order run first, then cancels, then
# actions that send at least one GTC/IOC; within a class, proposer order. Consensus batches are not
# visible in explorer blocks, so the whole block is treated as one batch.

def tx_class(actions_of_tx) -> int:
    """0: no GTC/IOC order (incl. ALO-only, scheduleCancel); 1: cancel; 2: sends a GTC/IOC order.
    Classified on the tx's decoded BTC/ETH items only."""
    if any((a.new if isinstance(a, Modify) else a).tif is not Tif.ALO
           for a in actions_of_tx if isinstance(a, (Place, Modify))):
        return 2
    if any(isinstance(a, (Cancel, CancelByCloid)) for a in actions_of_tx):
        return 1
    return 0


def _tx_classes(actions) -> dict:
    by_tx = defaultdict(list)
    for a in actions:
        by_tx[a.tx_index].append(a)
    return {k: tx_class(v) for k, v in by_tx.items()}


def class_policy(actions):
    """(class, tx_index, item_index): cancels before every GTC/IOC taker in the block."""
    cls = _tx_classes(actions)
    return sorted(actions, key=lambda a: (cls[a.tx_index], a.tx_index, a.item_index))


def priority_policy(actions):
    """class_policy, then GTC/IOC txs by grouping.p descending (priority fee; docs: IOCs >= 8 bps
    in a 70 ms bucket sort by fee) instead of proposer order. No p sorts as 0."""
    cls = _tx_classes(actions)
    return sorted(actions, key=lambda a: (cls[a.tx_index], -(a.priority or 0) if cls[a.tx_index] == 2 else 0,
                                          a.tx_index, a.item_index))


def anti_policy(actions):
    """Identification control (deliberately wrong): class_policy reversed, GTC/IOC takers before cancels before
    the rest; proposer order inside a class."""
    cls = _tx_classes(actions)
    return sorted(actions, key=lambda a: (-cls[a.tx_index], a.tx_index, a.item_index))


def shuffle_policy(actions):
    """Identification control: txs in a random order (seeded by block height, so runs repeat), items kept in
    order inside a tx."""
    import random
    if not actions:
        return []
    txs = sorted({a.tx_index for a in actions})
    random.Random(actions[0].block).shuffle(txs)
    rank = {t: i for i, t in enumerate(txs)}
    return sorted(actions, key=lambda a: (rank[a.tx_index], a.item_index))


POLICIES = {"listing": listing, "class": class_policy, "priority": priority_policy, "anti": anti_policy,
            "shuffle": shuffle_policy}

# CLI default (validate_replay / validate_chunks): correct the book from each applied tx's real trades
# (engine_py/replay.Replay trade_learn; measured in handoff/integrate_result.md). run() and Replay stay off.
DEFAULT_TRADE_LEARN = "sweep,queue"   # name-clip dropped Nov 19: 74 chunks, 17 txs better, 0 worse (+2.8 pts per-tx, all txs)
# CLI default for keep-known refills: missing snapshot size behind known orders (chunks: recall_full 23.0% -> 26.8%)
DEFAULT_REFILL_POSITION = "back"

CATEGORIES = ("outside-fill timing", "wrong ALO decision", "unknown pre-window order", "unresolved cancel",
              "ordering", "margin rejection (unobservable)", "real bug (unexplained)")
INFO_ALO_REJECTED = "info: engine rejected a real maker's ALO at that price"   # a flag, not a category
FIRST_N = 20
BOOK_DEPTH = 5
G1_TARGET_PCT = 80                   # Satya, Nov 18: per-fill strict recall >= 80%
WILDCARD = "*"                       # horizon mode: a maker whose order predates the last re-seed
HORIZON_BUCKETS_MS = (2000, 4000, 6000, 8000, 10000, 12000)


# ------------------------------------------------------------------ ALO ground truth

def alo_truth_class(answer: dict) -> str:
    """orderStatus answer -> 'rejected_alo' | 'rejected_other' | 'accepted' | 'unknown'."""
    if not isinstance(answer, dict) or answer.get("status") != "order":
        return "unknown"
    st = answer.get("order", {}).get("status", "")
    if st == "badAloPxRejected":
        return "rejected_alo"
    if st.endswith("Rejected"):
        return "rejected_other"
    return "accepted" if st else "unknown"


def load_alo_status(hl_data, heights: range | None = None, t0_ms: int | None = None,
                    t1_ms: int | None = None) -> dict[tuple[int, str, int], dict]:
    """(block, hash, item) -> {"truth": class, "status": raw status} from every alo_status file
    (with t0/t1: only the hour folders around that time range, see data.parse.recorder_hours)."""
    out = {}
    paths = sorted(p for hour in recorder_hours(hl_data, t0_ms, t1_ms) for p in hour.glob("alo_status.jsonl*"))
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt") as f:
            for line in f:
                try:
                    m = json.loads(line)["msg"]
                except (json.JSONDecodeError, KeyError):
                    continue                        # the open hour's last line can be cut mid-write
                if heights is not None and m.get("block") not in heights:
                    continue
                ans = m.get("answer", {})
                out[(m["block"], m["hash"], m["item"])] = {
                    "truth": alo_truth_class(ans),
                    "status": ans.get("order", {}).get("status", ans.get("status")) if isinstance(ans, dict) else None}
    return out


def stream_order_truth(rp, hl_data) -> dict[tuple[int, str, int], dict]:
    """Truth for placements of users the recorder tracked (orderUpdates: user_orders.jsonl), in load_alo_status's
    form. Each Place / Modify of the window joins its order's statuses by (user, asset, cloid), else by (user,
    asset, side, px, original size) with the order timestamp within 1 s of the block. badAloPxRejected ->
    rejected_alo; any other *Rejected -> rejected_other; open / filled / canceled / ... -> accepted.
    MEASUREMENT ONLY: it feeds the wrong-ALO flags and the confusion table, never the engine."""
    from data import oid_facts
    facts = oid_facts.load(rp.window_dir, hl_data)
    by_oid: dict[int, list] = defaultdict(list)
    for o in facts.orders:
        if o["source"] == "ws" and o["status"]:
            by_oid[o["oid"]].append(o)
    if not by_oid:
        return {}
    by_cloid, by_shape = {}, defaultdict(list)
    for oid, recs in by_oid.items():
        o = recs[0]
        sts = [r["status"] for r in recs]
        rej = next((x for x in sts if x.endswith("Rejected")), None)
        truth = ("rejected_alo" if rej == "badAloPxRejected" else "rejected_other") if rej else "accepted"
        v = {"truth": truth, "status": rej or sts[0], "source": "user_orders"}
        spec = ASSETS[o["asset"]]
        if o["cloid"]:
            by_cloid[(o["user"], o["asset"], o["cloid"])] = v
        by_shape[(o["user"], o["asset"], o["buy"], spec.px_to_ticks(o["px"]), spec.sz_to_lots(o["sz"]))].append(
            (o["timestamp"], v))
    out = {}
    for b in rp.blocks:
        for a in b.actions:
            if not isinstance(a, (Place, Modify)):
                continue
            p = a.new if isinstance(a, Modify) else a
            v = by_cloid.get((a.user, p.asset, p.cloid)) if p.cloid else None
            if v is None:
                cands = [x for t, x in by_shape.get((a.user, p.asset, p.side is Side.BUY, p.px, p.sz), ())
                         if abs(t - b.time_ms) <= 1000]
                v = cands[0] if len(cands) == 1 else None
            if v is not None:
                out[(a.block, a.hash, a.item_index)] = v
    return out


# ------------------------------------------------------------------ per-tx record + rules

@dataclass
class TxRecord:
    hash: str
    asset: int
    block: int
    minute: int
    seq: int                                   # replay position of the tx's first action
    side: Side                                 # taker side (of its first order item)
    real: list = field(default_factory=list)   # [(px, sz, maker)]
    engine: list = field(default_factory=list) # [(px, sz, maker, engine maker oid)]
    flags: set = field(default_factory=set)
    book_before: str = ""
    reseed_seq: int = 0                        # horizon mode: seq of the asset's last re-seed before the tx
    horizon_ms: int | None = None              # horizon mode: time since that re-seed


def notional(fills) -> int:
    """Sum of px * sz. Ticks x lots is micro-USD for both BTC (0.1 x 1e-5) and ETH (0.01 x 1e-4),
    so BTC and ETH add up. Integers only."""
    return sum(f[0] * f[1] for f in fills)


def matched_notional(real, engine, key) -> int:
    """Notional both sides agree on, per `key` group (min of the two sizes)."""
    r, e = dict(agg(real, key)), dict(agg(engine, key))
    return sum(min(q, e.get(k, 0)) * k_px(k) for k, q in r.items())


def k_px(k):
    return k[0] if isinstance(k, tuple) else k


def agg(fills, key) -> list:
    c = Counter()
    for f in fills:
        c[key(f)] += f[1]
    return sorted(c.items())


def is_match(real, engine) -> bool:
    return agg(real, lambda f: (f[0], f[2])) == agg(engine, lambda f: (f[0], f[2]))


def is_match_px_sz(real, engine) -> bool:
    return agg(real, lambda f: f[0]) == agg(engine, lambda f: f[0])


def categorize(flags: set) -> str:
    """The first category (in CATEGORIES precedence) among the tx's flags."""
    for c in CATEGORIES[:-1]:
        if c in flags:
            return c
    return CATEGORIES[-1]


class ValidateObserver(Observer):
    def __init__(self, real_by_hash: dict, alo_truth: dict):
        # H11 scoring rule (g1-dd-placeholder, NOT approved; default off): with keep_known, every refill
        # counts as a re-seed and known engine orders placed before it score as WILDCARD
        self.wildcard_pre_reseed_known = False
        self.real_by_hash = real_by_hash
        self.alo_truth = alo_truth
        self.seq = 0
        self.txs: dict[str, TxRecord] = {}
        self.first_quote: dict[tuple, int] = {}        # (user, asset, maker side, px) -> first seq
        self.quote_seqs: dict[tuple, list] = defaultdict(list)   # same key -> every placement seq (ascending)
        self.reseed_seq: dict[int, int] = {}            # asset -> seq of its last re-seed
        self.reseed_time: dict[int, int] = {}           # asset -> snapshot time of its last re-seed
        self.alo_ids: dict[tuple, list] = defaultdict(list)   # (user, asset, side, px) -> [(seq, key, accepted)]
        self.placed_seq: dict[int, int] = {}            # engine oid -> seq
        self.unresolved: dict[tuple, list] = defaultdict(list)   # (user, asset) -> [(seq, hl oid)] int-oid misses
        self.outside_blocks: dict[tuple, int] = Counter()   # (block, asset) -> outside fills
        self.no_maker_blocks: set = set()
        self.alo_confusion: Counter = Counter()         # (engine decision, truth) -> n
        self.minute_stats: dict[tuple, Counter] = defaultdict(Counter)   # (minute, coin) -> counters
        self.resting: dict[tuple, int] = {}             # (minute, coin) -> known orders resting at its end

    # ---------------------------------------------------------- hooks
    def before_action(self, rp, block, a):
        self.seq += 1
        p = a.new if isinstance(a, Modify) else a
        if isinstance(p, Place):
            self.first_quote.setdefault((a.user, p.asset, p.side, p.px), self.seq)
            self.quote_seqs[(a.user, p.asset, p.side, p.px)].append(self.seq)
            if p.tif is not Tif.ALO:
                self._open_tx(rp, block, a, p)

    def _open_tx(self, rp, block, a, p):
        if a.hash in self.txs:
            return
        rec = TxRecord(hash=a.hash, asset=p.asset, block=block.height, minute=rp.minute(block.time_ms),
                       seq=self.seq, side=p.side, reseed_seq=self.reseed_seq.get(p.asset, 0))
        if rp.reseed:
            rec.horizon_ms = block.time_ms - self.reseed_time.get(p.asset, rp.t0)
        rec.book_before = book_text(rp.engine.books[p.asset], p.side.opposite)
        self.txs[a.hash] = rec

    def after_action(self, rp, block, a, fills, rejects, created):
        coin = ASSETS[a.asset].name if hasattr(a, "asset") else None
        ms = self.minute_stats[(rp.minute(block.time_ms), coin)] if coin else Counter()
        reasons = {r.reason for r in rejects}
        p = a.new if isinstance(a, Modify) else a
        if created is not None:
            self.placed_seq[created] = self.seq
        if isinstance(p, Place) and p.tif is Tif.ALO:
            accepted = RejectReason.ALO_WOULD_CROSS not in reasons
            ms["alo_total"] += 1
            ms["alo_rejected"] += not accepted
            key = (a.block, a.hash, a.item_index)
            self.alo_ids[(a.user, p.asset, p.side, p.px)].append((self.seq, key, accepted))
            t = self.alo_truth.get(key)
            if t is not None:
                self.alo_confusion[("accepted" if accepted else "rejected", t["truth"])] += 1
        if isinstance(a, (Cancel, CancelByCloid)):
            kind = "oid" if isinstance(a, Cancel) else "cloid"
            ms[f"{kind}_cancels"] += 1
            ms[f"{kind}_unknown"] += RejectReason.UNKNOWN_ORDER in reasons
        if RejectReason.UNKNOWN_ORDER in reasons:
            hl_oid = a.oid if isinstance(a, Cancel) else a.target if isinstance(a, Modify) else None
            if isinstance(hl_oid, int):                 # a missed cloid names no order we hold: not linkable
                self.unresolved[(a.user, a.asset)].append((self.seq, hl_oid))
        if isinstance(a, OutsideFill):
            self.outside_blocks[(block.height, a.asset)] += 1
            if RejectReason.NO_MAKER in reasons:
                self.no_maker_blocks.add((block.height, a.asset))
            return
        if fills:
            if a.hash not in self.txs:
                self._open_tx(rp, block, a, p)          # unreachable for ALOs; GTC/IOC opened before
            rec = self.txs[a.hash]
            rec.engine += [(f.px, f.sz, f.maker_user, f.maker_order) for f in fills]

    def after_block(self, rp, block, minute):
        """Known orders resting at the end of each minute's last block (O(book), so once a minute)."""
        nxt = rp.block_index + 1
        if nxt < len(rp.blocks) and rp.minute(rp.blocks[nxt].time_ms) == minute:
            return
        for asset in sorted(ASSETS):
            book = rp.engine.books[asset]
            n = sum(1 for side in (Side.BUY, Side.SELL) for lv in book.levels(side) for nd in lv
                    if nd.order.user != PLACEHOLDER)
            self.resting[(minute, ASSETS[asset].name)] = n

    def after_reconcile(self, rp, block, s, result, minute):
        ms = self.minute_stats[(minute, ASSETS[s.asset].name)]
        ms["snapshots"] += 1
        for k, v in result.items():
            ms[k] += v
        if result.get("reseed"):
            self.reseed_seq[s.asset] = self.seq
            self.reseed_time[s.asset] = s.time_ms
        elif result.get("refill"):                     # keep-known re-seed: known orders stay known
            self.reseed_time[s.asset] = s.time_ms
            if self.wildcard_pre_reseed_known:         # H11 rule: a refill counts as a re-seed for scoring
                self.reseed_seq[s.asset] = self.seq

    # ---------------------------------------------------------- scoring
    def flags_for(self, rec: TxRecord, rp) -> set:
        flags = set()
        if self.outside_blocks.get((rec.block, rec.asset)) or (rec.block, rec.asset) in self.no_maker_blocks:
            flags.add("outside-fill timing")
        real_makers = {m for _, _, m in rec.real}
        for px, sz, maker, oid in rec.engine:
            if maker == PLACEHOLDER:
                flags.add("unknown pre-window order")
                continue
            key_action = rp.placed_by.get(oid)
            if key_action is not None:
                t = self.alo_truth.get((key_action.block, key_action.hash, key_action.item_index))
                if t is not None and t["truth"] in ("rejected_alo", "rejected_other"):
                    flags.add("wrong ALO decision")     # engine rested an ALO the chain rejected
            if self._missed_cancel_could_target(rp, oid, maker, rec):
                flags.add("unresolved cancel")
            if maker not in real_makers:
                flags.add("margin rejection (unobservable)")
        maker_side = rec.side.opposite
        for px, sz, maker in rec.real:
            for seq, key, accepted in self.alo_ids.get((maker, rec.asset, maker_side, px), ()):
                if seq >= rec.seq or accepted:
                    continue
                flags.add(INFO_ALO_REJECTED)            # informational: no truth needed
                t = self.alo_truth.get(key)
                if t is not None and t["truth"] == "accepted":
                    flags.add("wrong ALO decision")     # engine rejected an ALO the chain rested
        if is_match_px_sz(rec.real, rec.engine) and not is_match(rec.real, rec.engine):
            flags.add("ordering")
        return flags

    def _missed_cancel_could_target(self, rp, oid, maker, rec) -> bool:
        """An int-oid cancel/modify by the maker, after the order was placed and before this tx, that
        the engine could not resolve, and whose oid is within the resolver's tolerance of this order's
        estimated oid (so it may have been meant for it)."""
        est = rp.resolver.hl.get(oid) if rp.resolver is not None else None
        if est is None:
            return False
        e, tol = est
        placed = self.placed_seq.get(oid, 0)
        return any(placed < s < rec.seq and abs(e - hl) <= tol for s, hl in self.unresolved.get((maker, rec.asset), ()))

    def quoted_since(self, user, asset, side, px, after_seq, before_seq) -> bool:
        seqs = self.quote_seqs.get((user, asset, side, px), [])
        i = bisect.bisect_right(seqs, after_seq)
        return i < len(seqs) and seqs[i] < before_seq

    def effective(self, rec: TxRecord) -> tuple[list, list]:
        """Horizon mode: real makers that placed nothing at that side/price since the last re-seed,
        and engine placeholder fills, both become WILDCARD (price/size scoring for pre-re-seed
        liquidity, strict maker scoring for orders placed after it)."""
        side = rec.side.opposite
        real = [(px, sz, m if self.quoted_since(m, rec.asset, side, px, rec.reseed_seq, rec.seq) else WILDCARD)
                for px, sz, m in rec.real]
        # a known engine order placed before the last re-seed (only with reseed_keep_known: the plain
        # re-seed clears them) is pre-re-seed liquidity too, scored like a placeholder
        if self.wildcard_pre_reseed_known:
            engine = [(px, sz, WILDCARD if m == PLACEHOLDER or self.placed_seq.get(oid, rec.reseed_seq + 1) <= rec.reseed_seq
                       else m, oid) for px, sz, m, oid in rec.engine]
        else:
            engine = [(px, sz, WILDCARD if m == PLACEHOLDER else m, oid) for px, sz, m, oid in rec.engine]
        return real, engine

    def real_makers_in_window(self, rec: TxRecord) -> bool:
        """Every real maker placed an order (accepted by the engine or not) on that side and price
        in the window before this tx."""
        return all(self.first_quote.get((m, rec.asset, rec.side.opposite, px), rec.seq) < rec.seq
                   for px, _, m in rec.real)


def book_text(book, side, depth=BOOK_DEPTH) -> str:
    spec = ASSETS[book.asset]
    parts = []
    for lv in book.levels(side):
        ph = sum(nd.remaining for nd in lv if nd.order.user == PLACEHOLDER)
        known = len([nd for nd in lv if nd.order.user != PLACEHOLDER])
        parts.append(f"{spec.ticks_to_px(lv.px)} {spec.lots_to_sz(lv.total)} "
                     f"(placeholder {spec.lots_to_sz(ph)}, {known} known)")
        if len(parts) == depth:
            break
    return f"{side.name} side: " + "; ".join(parts)


# ------------------------------------------------------------------ run

def scoring_inputs(rp, hl_data, oid_facts) -> tuple[dict, dict]:
    """(real trades by tx hash, ALO truth) for a window: the recorded inputs run() scores against. Loading only;
    reads the recorder files and the window's decoded blocks, never the replay's changing state."""
    heights = range(rp.blocks[0].height, rp.blocks[-1].height + 1)
    real_by_hash: dict[str, list] = defaultdict(list)
    for tr in iter_trades(hl_data, COINS, rp.t0, rp.t1):
        if tr.hash != ZERO_HASH:
            real_by_hash[tr.hash].append(tr)
    alo_truth = load_alo_status(hl_data, heights, rp.t0, rp.t1)
    if oid_facts:
        for k, v in stream_order_truth(rp, hl_data).items():
            alo_truth.setdefault(k, v)                  # a probe answer (orderStatus) wins over the stream
    return real_by_hash, alo_truth


def run(window, hl_data, policy_name="listing", out_dir=None, anchors=None, use_resolver=True, quiet=False,
        alo_gate=None, gate_max_age_ms=None, gate_spare_after=False, touch_trades_first=False, oracle_file=None, oracle=(), oracle_prequeue=None, learn_tid=False, phantom_ledger=None, phantom_mode=None, px_sigfig=None, gate_insweep=False, evict_known=None, nearest_ratio=0, reseed=False,
        touch_reconcile=False, touch_n_cap=False, touch_refill=None, keep_known=False, skip_lead_in=False,
        lead_in_keep=None, touch_evict="oldest", ledger_k=3,
        snapshot_shift=0, outside_position="end", stp="cancel_taker", observer_cls=None, engine=None,
        outside_other_txs=False, other_position="end", placeholder_ttl_ms=None,
        placeholder_queue="ahead", placeholder_cap=None, reseed_keep_known=False, wildcard_pre_reseed_known=False,
        placeholder_split=False, named_cancel=False, multi_lite=False, name_strict=None, ioc_model=None,
        trade_learn=None, trade_learn_at="tx", oid_guard=None, refill_position="front", oid_facts=False,
        name_prewindow=False, named_anon="front", deep_book=False,
        learn_keep_known=False, learn_evidence="loose", facts_exclude=(), use_bbo=True, anchor_drop_kinds=(), oid_bracket=False, p_priority=None,
        evict_arrival=False, taker_reject=None, taker_seed=None):
    """observer_cls / engine: measurement hooks (analysis/g1_triage.py); default = ValidateObserver, Engine."""
    # A replay holds millions of long-lived objects (oid facts, books, placed actions); CPython's full collections
    # re-scan all of them and were ~65% of a chunk's runtime. Young generations still collect cycles; outputs are
    # identical (checked on a chunk, Nov 20). Process-wide, so it also covers validate_chunks workers.
    gc.set_threshold(50_000, 20, 1_000)
    policy = POLICIES[policy_name]
    rp = Replay(window, hl_data, policy, anchors=anchors, use_resolver=use_resolver, alo_gate=alo_gate,
                gate_max_age_ms=gate_max_age_ms, gate_spare_after=gate_spare_after, touch_trades_first=touch_trades_first, oracle_file=oracle_file, oracle=tuple(oracle), oracle_prequeue=oracle_prequeue, learn_tid=learn_tid, phantom_ledger=phantom_ledger, phantom_mode=phantom_mode, px_sigfig=px_sigfig, gate_insweep=gate_insweep, reconcile_known=evict_known, nearest_ratio=nearest_ratio,
                reseed=reseed, touch_reconcile=touch_reconcile, touch_n_cap=touch_n_cap, touch_refill=touch_refill,
                lead_in_keep=lead_in_keep, touch_evict=touch_evict, ledger_k=ledger_k,
                keep_known=keep_known, skip_lead_in=skip_lead_in,
                snapshot_shift=snapshot_shift, outside_position=outside_position, outside_other_txs=outside_other_txs,
                other_position=other_position, placeholder_ttl_ms=placeholder_ttl_ms,
                placeholder_queue=placeholder_queue, placeholder_cap=placeholder_cap, placeholder_split=placeholder_split, named_cancel=named_cancel, multi_lite=multi_lite, name_strict=name_strict, ioc_model=ioc_model,
                reseed_keep_known=reseed_keep_known, trade_learn=trade_learn, trade_learn_at=trade_learn_at, oid_guard=oid_guard, refill_position=refill_position, oid_facts=oid_facts,
                name_prewindow=name_prewindow and oid_facts, named_anon=named_anon, deep_book=deep_book, learn_keep_known=learn_keep_known, learn_evidence=learn_evidence, facts_exclude=facts_exclude,
                use_bbo=use_bbo, anchor_drop_kinds=anchor_drop_kinds, oid_bracket=oid_bracket, taker_reject=taker_reject, taker_seed=_seed_for(taker_seed, window), engine=engine if engine is not None else Engine(stp=stp, p_priority=p_priority, evict_arrival=evict_arrival))
    # Recorded inputs of the scoring (loading only). A Replay from the setup cache carries them already
    # (replay/setup_cache.py computes them with scoring_inputs at build time).
    cached = getattr(rp, "cached_scoring_inputs", None)
    real_by_hash, alo_truth = cached if cached is not None else scoring_inputs(rp, hl_data, oid_facts)
    obs = (observer_cls or ValidateObserver)(real_by_hash, alo_truth)
    obs.wildcard_pre_reseed_known = wildcard_pre_reseed_known
    rp.observer = obs
    for _ in rp:
        pass

    window_hashes = {a.hash for b in rp.blocks for a in b.actions if not isinstance(a, OutsideFill)}
    for h, trs in real_by_hash.items():
        rec = obs.txs.get(h)
        if rec is None:
            continue                                    # ALO-only tx or not in window
        rec.real = [(t.px, t.sz, t.maker) for t in trs]
    unlinked = sum(1 for h in real_by_hash if h not in window_hashes)

    rows = []
    for rec in sorted(obs.txs.values(), key=lambda r: r.seq):
        if not rec.real and not rec.engine:
            continue
        warm = rec.block < rp.warmup_end_height
        real, engine = obs.effective(rec) if rp.reseed else (rec.real, rec.engine)
        if not rp.reseed and rec.real and not obs.real_makers_in_window(rec):
            status, cat, flags = "excluded: real maker pre-window", "", set()
        else:
            m = is_match(real, engine)
            flags = set() if m else obs.flags_for(rec, rp)
            status, cat = ("match", "") if m else ("mismatch", categorize(flags))
        rows.append(dict(hash=rec.hash, block=rec.block, minute=rec.minute, coin=ASSETS[rec.asset].name,
                         warmup=int(warm), status=status, category=cat,
                         horizon_ms="" if rec.horizon_ms is None else rec.horizon_ms,
                         match_px_sz=int(is_match_px_sz(real, engine)),
                         real_notional=notional(real), engine_notional=notional(engine),
                         matched_notional=matched_notional(real, engine, lambda f: (f[0], f[2])),
                         matched_notional_px=matched_notional(real, engine, lambda f: f[0]),
                         real_only=int(bool(rec.real) and not rec.engine),
                         engine_only=int(bool(rec.engine) and not rec.real),
                         flags="|".join(sorted(flags)),
                         real=fmt_fills(rec.asset, rec.real), engine=fmt_fills(rec.asset, [f[:3] for f in rec.engine]),
                         book_before=rec.book_before,
                         match_full=int(is_match(rec.real, rec.engine)),
                         matched_notional_full=matched_notional(rec.real, rec.engine, lambda f: (f[0], f[2]))))

    result = summarize(rp, obs, rows, unlinked, policy_name)
    if out_dir is not None:
        write_csvs(Path(out_dir), result, rows, obs)
    if not quiet:
        print_report(result)
    return result, rows


def fmt_fills(asset, fills) -> str:
    spec = ASSETS[asset]
    return "; ".join(f"{spec.lots_to_sz(sz)}@{spec.ticks_to_px(px)} {m[:10]}"
                     for (px, m), sz in agg(fills, lambda f: (f[0], f[2])))


def rate(n, d):
    return f"{n / d:.4f}" if d else ""


def scored_rows(rows: list) -> list:
    """The scored population: post-warm-up txs with status match or mismatch."""
    return [r for r in rows if not r["warmup"] and r["status"] in ("match", "mismatch")]


def scope_metrics(scored: list) -> dict:
    """'<scope>:<metric>' for scope all/BTC/ETH, plus g1_target, from scored tx rows only
    (so pooled rows from many windows/chunks use the same definitions)."""
    res = {}
    for scope, rs in [("all", scored)] + [(c, [r for r in scored if r["coin"] == c]) for c in COINS]:
        n = len(rs)
        m = sum(r["status"] == "match" for r in rs)
        res[f"{scope}:scored"] = n
        res[f"{scope}:matched"] = m
        res[f"{scope}:match_rate"] = rate(m, n)
        res[f"{scope}:match_rate_px_sz"] = rate(sum(r["match_px_sz"] for r in rs), n)
        realrs = [r for r in rs if not r["engine_only"]]
        res[f"{scope}:match_rate_real_linked_only"] = rate(sum(r["status"] == "match" for r in realrs), len(realrs))
        res[f"{scope}:engine_only_txs"] = sum(r["engine_only"] for r in rs)
        rn, en = sum(r["real_notional"] for r in rs), sum(r["engine_notional"] for r in rs)
        mn = sum(r["matched_notional"] for r in rs)
        res[f"{scope}:fill_f1"] = rate(2 * mn, rn + en)          # 2PR/(P+R) = 2*matched/(real+engine)
        if scope == "all":
            res["g1_target"] = ("" if not rn else "pass" if 100 * mn >= G1_TARGET_PCT * rn else "fail")
            res["g1_target_rule"] = f"all:fill_recall >= {G1_TARGET_PCT / 100:.2f} (report with fill_precision)"
        res[f"{scope}:fill_recall"] = rate(sum(r["matched_notional"] for r in rs), rn)
        res[f"{scope}:fill_precision"] = rate(sum(r["matched_notional"] for r in rs), en)
        res[f"{scope}:match_rate_full"] = rate(sum(r["match_full"] for r in rs), n)
        res[f"{scope}:fill_recall_full"] = rate(sum(r["matched_notional_full"] for r in rs), rn)
        res[f"{scope}:fill_precision_full"] = rate(sum(r["matched_notional_full"] for r in rs), en)
        res[f"{scope}:fill_recall_px_sz"] = rate(sum(r["matched_notional_px"] for r in rs), rn)
        res[f"{scope}:fill_precision_px_sz"] = rate(sum(r["matched_notional_px"] for r in rs), en)
    return res


def mismatch_tables(scored: list) -> tuple[dict, dict]:
    """(first-category counts by (category, coin), flagged counts by category) over mismatched rows."""
    mism = [r for r in scored if r["status"] == "mismatch"]
    table = {(cat, coin): sum(1 for r in mism if r["category"] == cat and r["coin"] == coin)
             for cat in CATEGORIES for coin in COINS}
    flags = {cat: sum(1 for r in mism if cat in r["flags"].split("|")) for cat in CATEGORIES[:-1]}
    return table, flags


def horizon_table(scored: list) -> list[dict]:
    """Match and fill recall by time since the last re-seed (rows with a horizon_ms only)."""
    hz = []
    edges = (0, *HORIZON_BUCKETS_MS, None)
    for lo, hi in zip(edges, edges[1:]):
        rs = [r for r in scored if r["horizon_ms"] != "" and r["horizon_ms"] >= lo
              and (hi is None or r["horizon_ms"] < hi)]
        rn = sum(r["real_notional"] for r in rs)
        hz.append(dict(horizon=f"{lo // 1000}-{hi // 1000}s" if hi else f"{lo // 1000}s+", scored=len(rs),
                       matched=sum(r["status"] == "match" for r in rs),
                       match_rate=rate(sum(r["status"] == "match" for r in rs), len(rs)),
                       fill_recall=rate(sum(r["matched_notional"] for r in rs), rn),
                       fill_recall_px_sz=rate(sum(r["matched_notional_px"] for r in rs), rn)))
    return hz


def summarize(rp, obs, rows, unlinked, policy_name) -> dict:
    scored = scored_rows(rows)
    res = {"window": rp.window_dir.name, "policy": policy_name, "outside_fill_position": OUTSIDE_FILL_POSITION,
           "blocks": len(rp.blocks), "warmup_end_height": rp.warmup_end_height,
           "resolver": "on" if rp.resolver is not None else "off",
           "alo_gate": rp.alo_gate or "off", "gate_max_age_ms": rp.gate_max_age_ms if rp.gate_max_age_ms is not None else "",
           "evict_known": rp.reconcile_known or "off",
           "nearest_ratio": rp.resolver.nearest_ratio if rp.resolver is not None else "",
           "reseed": ("every snapshot, keep known" if rp.keep_known else "every snapshot") if rp.reseed else "off",
           "lead_in": "skipped" if rp.skip_lead_in and rp.lead_in else f"{rp.lead_in} blocks" if rp.lead_in else "none",
           "touch_reconcile": ("bbo every block" + (" + n cap" if rp.touch_n_cap else "")) if rp.touch_reconcile else "off",
           "real_linked_txs_not_in_window": unlinked}
    triage = {"snapshot_shift": rp.snapshot_shift, "shift_dropped": rp.shift_dropped,
              "outside_position": rp.outside_position, "stp": rp.engine.stp,
              "outside_fill_other_tx": rp.parse_stats["outside_fill_other_tx"]}
    if rp.other_position != "end":
        triage["other_position"] = rp.other_position
    if (rp.snapshot_shift, rp.outside_position, rp.engine.stp, rp.outside_other_txs) != (0, "end", "cancel_taker", False):
        res.update(triage)                          # G1 triage flags: only when not the default
    ph = {"placeholder_ttl_ms": rp.placeholder_ttl_ms, "placeholder_queue": rp.placeholder_queue,
          "placeholder_cap": rp.placeholder_cap, "reseed_keep_known": rp.reseed_keep_known}
    if tuple(ph.values()) != (None, "ahead", None, False):
        res.update(ph)                              # H11 placeholder flags: only when not the default
        res.update({f"ph:{k}": v for k, v in sorted(rp.ph_stats.items())})
    if obs.wildcard_pre_reseed_known:
        res["wildcard_pre_reseed_known"] = True
    if rp.refill_position != "front":
        res["refill_position"] = rp.refill_position
    if rp.oid_guard not in (None, "ghost"):
        res["oid_guard"] = rp.oid_guard
    if rp.oid_facts:                                # order-id facts: only when on
        res["oid_facts"] = True
        est = rp.resolver.est
        res["facts:anchors"] = len(est.anchors)
        res["facts:exact"] = len(est.exact)
        res["facts:oid_px"] = sum(v is not None for v in est.oid_px.values())
    if rp.deep_book:
        res["deep_book"] = True
        for k, v in sorted(rp.deep_stats.items()):
            res["deep:" + k] = v
    if rp.named or rp.name_stats:
        for k, v in sorted(rp.name_stats.items()):
            res["name:" + k] = v
        for k in ("resolved_named", "named_gone"):
            res["name:" + k] = rp.resolver.stats.get(k, 0) if rp.resolver is not None else 0
        if rp.named_anon != "front":
            res["named_anon"] = rp.named_anon
        res["facts:dropped_anchors"] = est.dropped_anchors
    if rp.taker_reject:
        res.update({f"taker:{k}": v for k, v in sorted(rp.taker_stats.items())})
        res["taker_blocked_end"] = ";".join(sorted(rp._taker_blocked))
    if rp.trade_learn is not None:                  # trade-learning flag: only when set
        res["trade_learn"] = rp.trade_learn
        res.update({f"learn:{k}": v for k, v in sorted(rp.learn_stats.items())})
        if getattr(rp, "px_sigfig", None):
            res.update({f"sigfig:{k}": v for k, v in sorted(rp.sigfig_stats.items())})
        if getattr(rp, "phantom_mode", None):
            res.update({f"phantom:{k}": v for k, v in sorted(rp.phantom_stats.items())})
    res.update(scope_metrics(scored))
    post = [r for r in rows if not r["warmup"]]
    res["excluded_real_maker_prewindow"] = sum(r["status"].startswith("excluded") for r in post)
    res["warmup_txs"] = sum(r["warmup"] for r in rows)
    res["mismatch_table"], res["flag_table"] = mismatch_tables(scored)
    res["alo_truth_rows"] = len(obs.alo_truth)
    res["alo_confusion"] = dict(obs.alo_confusion)
    res["resolver_stats"] = dict(rp.resolver.stats) if rp.resolver is not None else {}
    res["gate_stats"] = dict(rp.gate_stats) | ({f"ORACLE_{k}": v for k, v in rp.oracle.stats.items()} if getattr(rp, "oracle", None) is not None else {})
    res["touch_reconciled"] = {f"{c}:{k}": v for (c, k), v in sorted(rp.touch_reconciled.items())}
    res["touch_errors"] = {}
    if rp.touch is not None:
        for asset, errs in rp.touch.errors.items():
            name = ASSETS[asset].name
            if errs:
                both = sorted(max(abs(b), abs(k)) for b, k in errs)
                res["touch_errors"][name] = {"n": len(errs), "exact": sum(1 for x in both if x == 0),
                                             "median_ticks": both[len(both) // 2], "p90_ticks": both[9 * len(both) // 10]}
    res["reconciled"] = {f"{c}:{k}": ASSETS[COINS.index(c)].lots_to_sz(v) for (c, k), v in sorted(rp.reconciled.items())}
    pm = defaultdict(lambda: Counter())
    for r in rows:
        if r["status"] in ("match", "mismatch"):
            c = pm[(r["minute"], r["coin"])]
            c["scored"] += 1
            c["matched"] += r["status"] == "match"
            c["matched_px_sz"] += r["match_px_sz"]
            c["warmup_txs"] += r["warmup"]
    keys = sorted(set(pm) | set(obs.minute_stats) | set(obs.resting))
    per_min = []
    for minute, coin in keys:
        if coin is None:
            continue
        c, ms = pm[(minute, coin)], obs.minute_stats[(minute, coin)]
        spec = ASSETS[COINS.index(coin)]
        per_min.append(dict(
            minute=minute, coin=coin, scored=c["scored"], matched=c["matched"], match_rate=rate(c["matched"], c["scored"]),
            match_rate_px_sz=rate(c["matched_px_sz"], c["scored"]), warmup_txs=c["warmup_txs"],
            snapshots=ms["snapshots"], trimmed=spec.lots_to_sz(ms["trimmed"]), drift=spec.lots_to_sz(ms["drift"]),
            missing=spec.lots_to_sz(ms["missing"]), evicted=spec.lots_to_sz(ms["evicted"]),
            evicted_orders=ms["evicted_orders"], alo_total=ms["alo_total"], alo_rejected=ms["alo_rejected"],
            alo_reject_rate=rate(ms["alo_rejected"], ms["alo_total"]),
            cloid_cancels=ms["cloid_cancels"], cloid_unknown=ms["cloid_unknown"],
            oid_cancels=ms["oid_cancels"], oid_unknown=ms["oid_unknown"],
            resting_known_orders=obs.resting.get((minute, coin), "")))
    res["per_minute"] = per_min
    res["horizons"] = horizon_table(scored) if rp.reseed else []
    return res


def write_csvs(out: Path, res: dict, rows: list, obs) -> None:
    out.mkdir(parents=True, exist_ok=True)

    def write(name, header, data):
        with open(out / name, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(data)

    scalars = [(k, v) for k, v in res.items() if not isinstance(v, (dict, list))]
    scalars += [(f"resolver:{k}", v) for k, v in sorted(res["resolver_stats"].items())]
    scalars += [(f"reconciled:{k}", v) for k, v in res["reconciled"].items()]
    scalars += [(f"gate:{k}", v) for k, v in sorted(res["gate_stats"].items())]
    scalars += [(f"touch_reconcile:{k}", v) for k, v in res["touch_reconciled"].items()]
    scalars += [(f"touch:{c}:{k}", v) for c, d in sorted(res["touch_errors"].items()) for k, v in d.items()]
    write("summary.csv", ["metric", "value"], scalars)
    pm = res["per_minute"]
    write("per_minute.csv", list(pm[0]) if pm else ["minute"], [list(r.values()) for r in pm])
    total = sum(res["mismatch_table"].values())
    write("mismatches.csv", ["category", *COINS, "total", "share", "flagged (any precedence)"],
          [[cat, *(res["mismatch_table"][(cat, c)] for c in COINS),
            sum(res["mismatch_table"][(cat, c)] for c in COINS),
            rate(sum(res["mismatch_table"][(cat, c)] for c in COINS), total),
            res["flag_table"].get(cat, "")] for cat in CATEGORIES])
    write("txs.csv", list(rows[0]) if rows else ["hash"], [list(r.values()) for r in rows])
    if res["horizons"]:
        write("horizon.csv", list(res["horizons"][0]), [list(r.values()) for r in res["horizons"]])
    write("alo_truth.csv", ["engine_decision", "truth", "n"],
          [[e, t, n] for (e, t), n in sorted(res["alo_confusion"].items())])
    first = [r for r in rows if r["status"] == "mismatch" and not r["warmup"]][:FIRST_N]
    write("first_mismatches.csv", ["hash", "block", "coin", "category", "flags", "real", "engine", "book_before"],
          [[r["hash"], r["block"], r["coin"], r["category"], r["flags"], r["real"], r["engine"], r["book_before"]]
           for r in first])


def print_report(res: dict) -> None:
    print(f"window {res['window']}  policy {res['policy']}  resolver {res['resolver']}  "
          f"warm-up ends at {res['warmup_end_height']}")
    print(f"outside fills: {res['outside_fill_position']}")
    print(f"ALO gate: {res['alo_gate']} (max age {res['gate_max_age_ms'] or 'none'} ms)  known-order eviction: "
          f"{res['evict_known']}  resolver nearest_ratio: {res['nearest_ratio'] or 0}  re-seed: {res['reseed']}  "
          f"touch reconcile: {res['touch_reconcile']}")
    if res["touch_reconciled"]:
        print("  touch reconcile: " + ", ".join(f"{k} {v}" for k, v in res["touch_reconciled"].items() if v))
    if res["gate_stats"]:
        print("  gate: " + ", ".join(f"{k} {v}" for k, v in sorted(res["gate_stats"].items())))
    for c, d in sorted(res["touch_errors"].items()):
        print(f"  touch estimate vs snapshots, {c}: exact {d['exact']}/{d['n']}, |error| median {d['median_ticks']} "
              f"p90 {d['p90_ticks']} ticks")
    print(f"G1 target ({res['g1_target_rule']}): {res['g1_target'] or 'n/a'}")
    for scope in ("all", *COINS):
        print(f"  {scope:<4} PER-FILL STRICT recall {res[f'{scope}:fill_recall'] or 'n/a'}  "
              f"precision {res[f'{scope}:fill_precision'] or 'n/a'}  F1 {res[f'{scope}:fill_f1'] or 'n/a'}")
    for scope in ("all", *COINS):
        print(f"  {scope:<4} scored {res[f'{scope}:scored']:>5}  matched {res[f'{scope}:matched']:>5}  "
              f"match rate {res[f'{scope}:match_rate'] or 'n/a'}  (px/size only {res[f'{scope}:match_rate_px_sz'] or 'n/a'}; "
              f"real-linked only {res[f'{scope}:match_rate_real_linked_only'] or 'n/a'}; "
              f"engine-only txs {res[f'{scope}:engine_only_txs']})")
        print(f"       per fill (notional): recall {res[f'{scope}:fill_recall'] or 'n/a'} "
              f"precision {res[f'{scope}:fill_precision'] or 'n/a'}  (px/size: recall "
              f"{res[f'{scope}:fill_recall_px_sz'] or 'n/a'} precision {res[f'{scope}:fill_precision_px_sz'] or 'n/a'})")
    print(f"  excluded after warm-up (real maker pre-window): {res['excluded_real_maker_prewindow']}; "
          f"warm-up txs: {res['warmup_txs']}")
    print("\nmismatch table (first category by precedence; 'flagged' counts every tx with that flag)")
    total = sum(res["mismatch_table"].values())
    for cat in CATEGORIES:
        n = sum(res["mismatch_table"][(cat, c)] for c in COINS)
        print(f"  {cat:<34}" + "".join(f"{c} {res['mismatch_table'][(cat, c)]:>5}  " for c in COINS)
              + f"total {n:>5} ({rate(n, total) or 'n/a'})  flagged {res['flag_table'].get(cat, '-')}")
    print(f"\nALO ground truth rows in window: {res['alo_truth_rows']}; engine vs truth: {res['alo_confusion'] or 'none'}")
    if res["horizons"]:
        print("\nby time since re-seed:")
        for h in res["horizons"]:
            print(f"  {h['horizon']:>6}  match {h['matched']:>4}/{h['scored']:<5} ({h['match_rate'] or 'n/a'})  "
                  f"fill recall {h['fill_recall'] or 'n/a'} (px/size {h['fill_recall_px_sz'] or 'n/a'})")
    print("\nper minute:")
    for r in res["per_minute"]:
        print(f"  min {r['minute']:>2} {r['coin']}  match {r['matched']:>4}/{r['scored']:<4} "
              f"({r['match_rate'] or '  n/a '})  warmup {r['warmup_txs']:>4}  ALO rej {r['alo_reject_rate'] or 'n/a'}  "
              f"drift {r['drift']}  evicted {r['evicted']}  trimmed {r['trimmed']}  missing {r['missing']}  "
              f"unknown cancels cloid {r['cloid_unknown']}/{r['cloid_cancels']} oid {r['oid_unknown']}/{r['oid_cancels']}  "
              f"resting {r['resting_known_orders']}")


def add_replay_args(ap: argparse.ArgumentParser, hl_data_required: bool = True) -> None:
    """The replay options shared by validate_replay and validate_chunks."""
    ap.add_argument("--hl-data", required=hl_data_required)
    ap.add_argument("--policy", default="listing", choices=sorted(POLICIES))
    ap.add_argument("--no-resolver", action="store_true")
    ap.add_argument("--alo-gate", choices=["touch"], help="G1 fix #1: admit ALOs against the real touch")
    ap.add_argument("--gate-max-age-ms", type=int, help="gate abstains when the touch is older than this")
    ap.add_argument("--gate-spare-after", action="store_true",
                    help="I21: an admitted ALO that crosses an order placed after the touch evidence evicts nothing")
    ap.add_argument("--touch-trades-first", action="store_true",
                    help="I25: at equal time, apply a block's trades to the touch BEFORE its post-block bbo / snapshot")
    ap.add_argument("--learn-tid", action="store_true",
                    help="I27: the queue correction cuts at the maker order the trade id pins (data/tid_hash.py)")
    ap.add_argument("--gate-insweep", action="store_true",
                    help="I34 (default off): the ALO gate abstains after an earlier in-block trade swept its touch price")
    ap.add_argument("--px-sigfig", choices=("item", "action"),
                    help="I33 (default off): reject a non-integer price with > 5 significant figures (HL rule); action = also the user's adjacent order txs")
    ap.add_argument("--phantom-ledger", help="I27c: tid phantom ledger JSON (user -> [[height, kind]]); proofs before the chunk only")
    ap.add_argument("--phantom-mode", choices=("sink", "reject"),
                    help="I27c (default off): flagged users' orders are jumped by later named orders (sink) or their ALOs not rested (reject)")
    ap.add_argument("--oracle-file", help="ORACLE (look-ahead by design, diagnostic only): chain-truth status CSV")
    ap.add_argument("--oracle-status", action="store_true", help="ORACLE O1 (look-ahead by design): chain order status")
    ap.add_argument("--oracle-cancel", action="store_true", help="ORACLE O3 (look-ahead by design): true cancel target")
    ap.add_argument("--oracle-ioc", action="store_true", help="ORACLE O4 (look-ahead by design): IOC acceptance")
    ap.add_argument("--oracle-prequeue", help="ORACLE O2 (look-ahead by design): true pre-window queues (prequeue.csv)")
    ap.add_argument("--evict-known", choices=["oldest", "newest"], help="G1 fix #2: rule 8 evicts known orders")
    ap.add_argument("--p-priority", type=_p_priority_arg, default=None, metavar="always|known|MS",
                    help="queue by grouping.p at one price: 'always', or only against orders <= MS ms older")
    ap.add_argument("--evict-arrival", action="store_true",
                    help="keep-known/evict-known trims known orders by arrival (seq), not queue position")
    ap.add_argument("--oid-bracket", action="store_true",
                    help="reference points from tracked users' orders on every coin + monotone oid bracket rule")
    ap.add_argument("--nearest-ratio", type=int, default=0,
                    help="resolver: accept the nearest of several candidates if all others are this many times farther")
    ap.add_argument("--reseed", action="store_true", help="G1 #3: rebuild each book from every snapshot")
    ap.add_argument("--touch-reconcile", action="store_true", help="rule 8 against the recorded bbo before every block")
    ap.add_argument("--touch-n-cap", action="store_true", help="with --touch-reconcile: cap touch order count at bbo n")
    ap.add_argument("--touch-refill", choices=["front", "back"], default=None,
                    help="touch reconcile + add missing bbo touch size as a placeholder (ahead of / behind the level)")
    ap.add_argument("--keep-known", action="store_true",
                    help="with --reseed: keep known orders at each snapshot, refill missing size at the front")
    ap.add_argument("--skip-lead-in", action="store_true", help="lead-in chunks: replay only the scored part")
    ap.add_argument("--lead-in-keep", type=int, default=None, help="measurement: replay only the last N lead-in blocks")
    ap.add_argument("--touch-evict", choices=["oldest", "newest", "ledger"], default="oldest",
                    help="touch reconcile: which known orders go first when our touch level exceeds bbo")
    ap.add_argument("--ledger-k", type=int, default=3, help="--touch-evict ledger: most orders removed per touch level")
    ap.add_argument("--snapshot-shift", type=int, default=0, help="triage H4: apply snapshots k blocks later (<0 earlier)")
    ap.add_argument("--outside-position", choices=["end", "start"], default="end", help="triage H7")
    ap.add_argument("--outside-other-txs", action="store_true",
                    help="triage H9: trades linked to non-book txs (SetGlobalAction) become outside fills")
    ap.add_argument("--other-position", choices=["end", "start", "index"], default="end",
                    help="with --outside-other-txs: where those fills go (index = at their own tx's position)")
    ap.add_argument("--stp", choices=["cancel_taker", "expire_maker"], default="cancel_taker", help="triage H8")
    ap.add_argument("--placeholder-ttl-ms", type=int, help="H11: placeholders expire this long after their snapshot")
    ap.add_argument("--placeholder-queue", choices=["ahead", "behind"], default="ahead",
                    help="H11: behind = known orders get priority over placeholders at a level")
    ap.add_argument("--placeholder-cap", choices=["next"],
                    help="H11, LOOK-AHEAD (measurement only): cap re-seeded levels by the next snapshot + real trades")
    ap.add_argument("--placeholder-split", action="store_true",
                    help="A4a: each snapshot level's uncovered size becomes its uncovered ORDER COUNT of equal placeholders")
    ap.add_argument("--ioc-model", default=None, metavar="FILE",
                    help="fallback 3b: model.json of analysis.a3_ioc_model (with --taker-reject ...,ioc_model)")
    ap.add_argument("--name-strict", choices=["N1", "N2", "N3"], default=None,
                    help="fallback 3a: trade-learn name only at this evidence level (docs/scoreboard.md)")
    ap.add_argument("--multi-lite", action="store_true",
                    help="L5-lite: per-level alternatives (phantom-or-real, owner-of-piece); implies --placeholder-split + name")
    ap.add_argument("--named-cancel", action="store_true",
                    help="A4b2: an unresolvable cancel of U removes U's one trade-named piece on that asset (needs trade-learn name)")
    ap.add_argument("--reseed-keep-known", action="store_true",
                    help="H11: alias of --keep-known")
    ap.add_argument("--refill-position", choices=["front", "back"], default=DEFAULT_REFILL_POSITION,
                    help="keep-known: missing snapshot size goes ahead of known orders (front) or behind them")
    ap.add_argument("--oid-guard", choices=["ghost", "ratio25", "ratio50", "cap3000", "none"],
                    help="int-oid resolver preset (default ghost = data/oids defaults; none = old rule)")
    ap.add_argument("--taker-reject", metavar="RULES", default=None,
                    help="treat IOC takers as rejected by the chain (comma list): min10 = notional under 10 USD; "
                         "learn = the user's last engine-filled IOC had no real trade (until a real fill); "
                         "margin = grows the position past the plan-time free margin (clearinghouseState.json); "
                         "margin_spot = margin plus spot USDC available")
    ap.add_argument("--taker-seed", metavar="JSON", default=None,
                    help="with --taker-reject learn: start each chunk with these users blocked "
                         "({chunk: [users]}, built from EARLIER chunks by analysis/taker_seed.py)")
    ap.add_argument("--trade-learn-at", choices=["tx", "block-end"], default="tx",
                    help="when trade_learn corrections run: after each tx (default) or once at the end of the block, in "
                         "listing order (same position under every ordering policy; use for counterfactuals)")
    ap.add_argument("--trade-learn", metavar="RULES", default=DEFAULT_TRADE_LEARN,
                    help="correct the book from each tx's real trades after it is applied (later txs only): "
                         "sweep = remove what the real taker must have consumed; name = front placeholder at the "
                         "last real price becomes the last real maker's order (-clip: only the size that maker just filled); "
                         "queue = remove what rests ahead of a filled maker's order. Comma-separated, e.g. "
                         "sweep,queue,name-clip; both = sweep,name; both-clip = sweep,name-clip. "
                         f"Default {DEFAULT_TRADE_LEARN}; 'off' = none")
    ap.add_argument("--no-oid-facts", action="store_true",
                    help="resolver ignores real order-id facts (fills.json, hist_orders.json, open_orders.json, "
                         "recorder user streams); default: uses them as anchors and exact oid matches")
    ap.add_argument("--name-prewindow", action="store_true",
                    help="with facts: pre-window orders the facts show resting at the seed snapshot become their real "
                         "owner's orders (real oid, queue by oid). Off by default: neutral on 46 Mac chunks (G1 -0.3, "
                         "full recall +0.3 points)")
    ap.add_argument("--deep-book", action=argparse.BooleanOptionalAction, default=True,
                    help="liquidity past the full snapshot's 20 levels from the coarse book (book_*_sf4), split over "
                         "prices by what was observable, used only by orders whose limit reaches past the last level. "
                         "Default ON (approved by Satya Nov 19 as an exception to 'never add liquidity from "
                         "snapshots'; 74 Mac chunks: G1 59.0% -> 66.6%, one tx changed); --no-deep-book turns it off")
    ap.add_argument("--facts-exclude", default="",
                    help="comma-separated order-id fact sources to leave out (fills, hist, open, ws_orders, ws_fills)")
    ap.add_argument("--no-bbo", action="store_true", help="the touch ignores the recorded bbo feed")
    ap.add_argument("--no-target-lookups", action="store_true",
                    help="ignore capture's orderStatus answers for cancel targets (anchors.json kind 'target')")
    ap.add_argument("--learn-keep-known", action="store_true",
                    help="trade_learn's sweep and queue rules remove placeholders only, never an order we saw placed")
    ap.add_argument("--learn-evidence", choices=("loose", "tx"), default="loose",
                    help="trade_learn: original rules (loose) or only what each tx shows (tx: queue cuts only for "
                         "makers our engine under-filled; sweep spares orders placed after the tx; Q7)")
    ap.add_argument("--named-anon", choices=("front", "back"), default="front",
                    help="where the anonymous rest of a level with named pre-window orders queues (default front)")
    ap.add_argument("--wildcard-pre-reseed-known", action="store_true",
                    help="H11 scoring (NOT approved, default off): with --keep-known, known engine orders placed "
                         "before the last re-seed score as WILDCARD")


def _p_priority_arg(v: str):
    if v in ("always", "known", "knownpos", "docs", "docsall"):
        return v
    if v.startswith(("docs:", "docsall:")):              # I01: docs rule with another window, e.g. docs:300
        int(v.split(":")[1])
        return v
    return int(v)


def replay_kwargs(a) -> dict:
    """Keyword arguments for run() from add_replay_args options (all but hl_data, policy, anchors)."""
    return dict(use_resolver=not a.no_resolver, alo_gate=a.alo_gate, gate_max_age_ms=a.gate_max_age_ms,
                gate_spare_after=a.gate_spare_after, touch_trades_first=a.touch_trades_first, oracle_file=a.oracle_file, oracle_prequeue=a.oracle_prequeue, learn_tid=a.learn_tid,
                phantom_ledger=a.phantom_ledger, phantom_mode=a.phantom_mode, px_sigfig=a.px_sigfig, gate_insweep=a.gate_insweep,
                oracle=tuple(k for k in ("status", "cancel", "ioc") if getattr(a, f"oracle_{k}")),
                evict_known=a.evict_known, nearest_ratio=a.nearest_ratio, reseed=a.reseed,
                touch_reconcile=a.touch_reconcile, touch_n_cap=a.touch_n_cap, touch_refill=a.touch_refill,
                keep_known=a.keep_known, skip_lead_in=a.skip_lead_in,
                lead_in_keep=a.lead_in_keep, touch_evict=a.touch_evict, ledger_k=a.ledger_k,
                snapshot_shift=a.snapshot_shift, outside_position=a.outside_position, stp=a.stp,
                outside_other_txs=a.outside_other_txs, other_position=a.other_position,
                placeholder_ttl_ms=a.placeholder_ttl_ms, placeholder_queue=a.placeholder_queue,
                placeholder_cap=a.placeholder_cap, reseed_keep_known=a.reseed_keep_known,
                placeholder_split=a.placeholder_split, named_cancel=a.named_cancel, multi_lite=a.multi_lite, name_strict=a.name_strict, ioc_model=a.ioc_model,
                wildcard_pre_reseed_known=a.wildcard_pre_reseed_known,
                trade_learn=None if a.trade_learn == "off" else a.trade_learn, trade_learn_at=a.trade_learn_at,
                oid_guard=a.oid_guard, refill_position=a.refill_position,
                oid_facts=not a.no_resolver and not a.no_oid_facts,
                name_prewindow=a.name_prewindow, named_anon=a.named_anon,
                deep_book=a.deep_book, learn_keep_known=a.learn_keep_known, learn_evidence=a.learn_evidence,
                facts_exclude=tuple(x for x in a.facts_exclude.split(",") if x), use_bbo=not a.no_bbo,
                anchor_drop_kinds=("target",) if a.no_target_lookups else (), oid_bracket=a.oid_bracket,
                p_priority=a.p_priority, evict_arrival=a.evict_arrival, taker_reject=a.taker_reject,
                taker_seed=a.taker_seed)


def _seed_for(path, window):
    """--taker-seed: {chunk folder name: [users]} (analysis/taker_seed.py); this chunk's list, [] if absent."""
    if not path:
        return None
    return json.load(open(path)).get(Path(window).name, [])


def options_tag(a) -> str:
    """Output-folder suffix naming the replay options, e.g. '-gate-nr3-reseed'."""
    return "".join([f"-gate{a.gate_max_age_ms if a.gate_max_age_ms is not None else ''}" if a.alo_gate else "",
                    f"-evict{a.evict_known}" if a.evict_known else "",
                    f"-nr{a.nearest_ratio}" if a.nearest_ratio else "", "-reseed" if a.reseed else "",
                    "-touch" if a.touch_reconcile else "", "-ncap" if a.touch_n_cap else "",
                    f"-tref{a.touch_refill}" if a.touch_refill else "",
                    f"-tev{a.touch_evict}" if a.touch_evict != "oldest" else "",
                    f"-k{a.ledger_k}" if a.touch_evict == "ledger" and a.ledger_k != 3 else "",
                    f"-lead{a.lead_in_keep}" if a.lead_in_keep is not None else "",
                    "-keep" if a.keep_known else "", "-nolead" if a.skip_lead_in else "",
                    f"-shift{a.snapshot_shift}" if a.snapshot_shift else "",
                    "-ofstart" if a.outside_position == "start" else "", "-stpexpire" if a.stp == "expire_maker" else "",
                    "-ofother" if a.outside_other_txs else "",
                    f"-oth{a.other_position}" if a.other_position != "end" else "",
                    f"-phttl{a.placeholder_ttl_ms}" if a.placeholder_ttl_ms is not None else "",
                    "-phbehind" if a.placeholder_queue == "behind" else "",
                    "-phcapnext" if a.placeholder_cap else "", "-keepknown" if a.reseed_keep_known else "",
                    "-phsplit" if a.placeholder_split else "", "-namedcancel" if a.named_cancel else "", "-multilite" if a.multi_lite else "", f"-name{a.name_strict}" if a.name_strict else "",
                    "-wcknown" if a.wildcard_pre_reseed_known else "",
                    "-tloff" if a.trade_learn == "off" else
                    f"-tl{a.trade_learn.replace(',', '+')}" if a.trade_learn != DEFAULT_TRADE_LEARN else "",
                    "-tlblockend" if a.trade_learn_at == "block-end" and a.trade_learn != "off" else "",
                    f"-og{a.oid_guard}" if a.oid_guard not in (None, "ghost") else "",
                    f"-refill{a.refill_position}" if a.refill_position != DEFAULT_REFILL_POSITION else "",
                    "-nofacts" if a.no_oid_facts else "",
                    "-name" if a.name_prewindow and not a.no_oid_facts else "",
                    "-anonback" if a.named_anon == "back" else "",
                    "-nodeep" if not a.deep_book else "",
                    "-learnkeep" if a.learn_keep_known else "",
                    "-levtx" if a.learn_evidence == "tx" else "",
                    f"-fx{a.facts_exclude.replace(',', '+')}" if a.facts_exclude else "",
                    "-nobbo" if a.no_bbo else "",
                    "-notargets" if a.no_target_lookups else "", "-bracket" if a.oid_bracket else "",
                    f"-p{a.p_priority}" if a.p_priority is not None else "",
                    "-evarr" if a.evict_arrival else "",
                    f"-trej{a.taker_reject.replace(',', '+')}" if a.taker_reject else "",
                    "-tseed" if a.taker_seed else ""])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("window")
    ap.add_argument("--out", help="directory for the CSVs (default: report/validate/<window>-<policy>)")
    ap.add_argument("--anchors", help="anchors file (default: the window's anchors.json)")
    add_replay_args(ap)
    a = ap.parse_args(argv)
    out = a.out or f"report/validate/{Path(a.window).name}-{a.policy}{options_tag(a)}"
    run(a.window, a.hl_data, a.policy, out, a.anchors, **replay_kwargs(a))
    print(f"\nCSVs in {out}")


if __name__ == "__main__":
    main()
