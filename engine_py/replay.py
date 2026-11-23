"""Window replay (AGENT_PLAN section 4, specs/ENGINE_WINDOW_RULES.md rules 1-3, 5, 8).

    for height, fills, rejects, state_hash in replay.run(window_dir, policy, hl_data=...): ...

Per window:
  1. Placeholders: per asset, the L2 snapshot nearest the first block time, one anonymous
     order per level, seeded before any window action (so ahead of every window order).
  2. Int-oid cancels go through data/oids.OidResolver when the window has anchors.json.
  3. Per block: policy(book actions) -> engine.apply each -> outside fills (end of block,
     tid order; the policy never sees or moves them) -> engine.end_block (scheduleCancel
     expiries) -> rule 8 reconcile for every snapshot with block.time <= s.time < next block.
  4. Yield (height, fills, rejects, state_hash).

Warm-up (rule 1) is a scoring matter: the replay runs every block and exposes
`warmup_end_height`; analysis/validate_replay.py skips what comes before it.

The state hash needs engine_py/state.py, whose hash function is Satya's call (section 5.3);
until it lands `state_hash` is None unless a `hasher(engine, prev_hash, height)` is passed.

Analyses that need more than the yielded tuple subclass `Observer`; every hook is a no-op.
"""

from __future__ import annotations

import bisect
from decimal import Decimal
import json
import heapq
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

from data.oids import OidEstimator, OidResolver
from data.parse import COIN_TO_ASSET, Stats, _hour_files, _records, iter_books, iter_trades, load_window, nearest_book
from engine_py.matching import split_equal, PLACEHOLDER, Engine
from engine_py.types import (ASSETS, Action, Block, Cancel, CancelByCloid, Fill, Modify, OutsideFill, Place,
                             Reject, RejectReason, Side, Tif, ZERO_HASH)

COINS = tuple(ASSETS[a].name for a in sorted(ASSETS))       # ("BTC", "ETH")
OUTSIDE_FILL_POSITION = "end of block, after every policy-ordered action, in tid order"

Policy = Callable[[list[Action]], list[Action]]
Hasher = Callable[[Engine, "bytes | None", int], bytes]


def listing(actions: list[Action]) -> list[Action]:
    """The block's listing order, unchanged (a policy; which one is "actual" is decided at G1)."""
    return list(actions)


class PolicyError(ValueError):
    pass


OID_GUARDS = {None: {}, "ghost": {},
              "ratio25": dict(ghost_guard=False, ratio_per_mille=250),
              "ratio50": dict(ghost_guard=False, ratio_per_mille=500),
              "cap3000": dict(ghost_guard=False, max_tol=3000),
              "none": dict(ghost_guard=False, joint_tx=False, joint_block=False)}
TOUCH_LOOKBACK_MS = 60_000          # touch events this long before the first block start the estimate


class RealTouch:
    """Estimate of Hyperliquid's real best bid/ask per asset, BEFORE each block, from logged
    inputs only (so it is identical under every ordering policy):
      - L2 snapshots and bbo records (if recorded) set the touch exactly at their time;
      - trades move it: a taker buy at p means the ask was at p (bid pushed below p if needed),
        a taker sell at p means the bid was at p.
    Events at a block's own time are not used for that block (they happen during it).
    At every snapshot, the estimate just before it is scored against it (`errors`)."""

    SNAP, BBO, TRADE = 0, 1, 2      # rank at equal time

    def __init__(self, snapshots=(), trades=(), bbos=()):
        ev = [(s.time_ms, self.SNAP, s.asset, 0, s.bids[0][0] if s.bids else None,
               s.asks[0][0] if s.asks else None) for s in snapshots]
        ev += [(t, self.BBO, a, 0, b, k) for t, a, b, k in bbos]      # b, k = (px, sz, n) | None
        ev += [(t.time_ms, self.TRADE, t.asset, t.seq, t.taker_side, t.px) for t in trades]
        ev.sort(key=lambda e: e[:4])
        self.events = ev
        self.i = 0
        self.bid: dict[int, int | None] = {}
        self.ask: dict[int, int | None] = {}
        self.updated: dict[int, int] = {}
        self.errors: dict[int, list[tuple[int, int]]] = {a: [] for a in ASSETS}   # (bid err, ask err) ticks
        self.bbo: dict[int, tuple] = {}     # asset -> (time, bid level | None, ask level | None), latest

    def advance(self, before_ms: int) -> None:
        while self.i < len(self.events) and self.events[self.i][0] < before_ms:
            t, kind, a, _, x, y = self.events[self.i]
            self.i += 1
            if kind == self.TRADE:
                side, px = x, y
                if side is Side.BUY:
                    self.ask[a] = px
                    if self.bid.get(a) is not None and self.bid[a] >= px:
                        self.bid[a] = px - 1
                else:
                    self.bid[a] = px
                    if self.ask.get(a) is not None and self.ask[a] <= px:
                        self.ask[a] = px + 1
            elif kind == self.BBO:
                self.bbo[a] = (t, x, y)
                self.bid[a] = x[0] if x else None
                self.ask[a] = y[0] if y else None
            else:
                if kind == self.SNAP and a in self.updated and x is not None and y is not None \
                        and self.bid.get(a) is not None and self.ask.get(a) is not None:
                    self.errors[a].append((self.bid[a] - x, self.ask[a] - y))
                self.bid[a], self.ask[a] = x, y
            self.updated[a] = t

    def get(self, asset: int, now_ms: int) -> tuple[int | None, int | None, int | None]:
        """(bid, ask, age ms) or (None, None, None) before any event."""
        if asset not in self.updated:
            return None, None, None
        return self.bid.get(asset), self.ask.get(asset), now_ms - self.updated[asset]


def iter_bbo(hl_data, coin: str, t0_ms: int, t1_ms: int):
    """Recorded bbo feed (hl_data/<date>/<hour>/bbo_<COIN>.jsonl[.gz]), if any:
    (time, asset, bid, ask) with bid/ask = (px ticks, sz lots, n orders) or None."""
    asset = COIN_TO_ASSET[coin]
    spec = ASSETS[asset]
    lv = lambda x: (spec.px_to_ticks(x["px"]), spec.sz_to_lots(x["sz"]), int(x["n"])) if x else None
    for path in _hour_files(Path(hl_data), f"bbo_{coin}", t0_ms, t1_ms):
        for rec in _records(path, t0_ms, t1_ms):
            d = rec["msg"]["data"]
            if t0_ms <= d["time"] <= t1_ms:
                b, k = d["bbo"]
                yield d["time"], asset, lv(b), lv(k)


class Observer:
    """Hooks for analyses. `replay` is the running Replay (engine, resolver, placed, ...)."""

    def before_action(self, replay: "Replay", block: Block, action: Action) -> None: ...

    def after_action(self, replay: "Replay", block: Block, action: Action, fills: list[Fill],
                     rejects: list[Reject], created: int | None) -> None: ...

    def after_block(self, replay: "Replay", block: Block, minute: int) -> None: ...

    def after_reconcile(self, replay: "Replay", block: Block, snapshot, result: dict[str, int],
                        minute: int) -> None: ...

    def before_reseed(self, replay: "Replay", block: Block, snapshot) -> None:
        """reseed=True only: called before the asset's book is reconciled and rebuilt from `snapshot`."""


def unique_subset(sizes: list[int], k: int, target: int, budget: int = 300_000):
    """The one set of exactly k indices whose sizes sum to target (sizes > 0), as a sorted tuple; else "none",
    "ambiguous", or "budget" (more than `budget` partial states). Exact DP over (count, sum) with ways capped at 2,
    sums above the target pruned; searches the complement (len - k items summing to total - target) when smaller."""
    m = len(sizes)
    if not 0 <= k <= m:
        return "none"
    flip = m - k < k
    kk, tt = (m - k, sum(sizes) - target) if flip else (k, target)
    layers = [{(0, 0): 1}]
    states = 1
    for x in sizes:
        prev = layers[-1]
        cur = dict(prev)
        for (c, sm), w in prev.items():
            if c < kk and sm + x <= tt:
                key = (c + 1, sm + x)
                cur[key] = min(2, cur.get(key, 0) + w)
        states += len(cur)
        if states > budget:
            return "budget"
        layers.append(cur)
    w = layers[-1].get((kk, tt), 0)
    if w == 0:
        return "none"
    if w > 1:
        return "ambiguous"
    picked, c, sm = [], kk, tt                    # walk back: item i is in iff the state is unreachable without it
    for i in range(m - 1, -1, -1):
        if layers[i].get((c, sm), 0):
            continue
        picked.append(i)
        c, sm = c - 1, sm - sizes[i]
    chosen = set(picked)
    out = sorted(set(range(m)) - chosen) if flip else sorted(chosen)
    return tuple(out)


# historicalOrders statuses for placements the chain refused for reasons the blocks don't show (not
# iocCancelRejected / badAloPxRejected: the engine decides those itself)
HIDDEN_REJECTS = {"perpMarginRejected", "minTradeNtlRejected", "tooManyOpenOrdersRejected", "reduceOnlyRejected",
                  "insufficientSpotBalanceRejected", "oracleRejected", "perpMaxPositionRejected",
                  "positionIncreaseAtOpenInterestCapRejected", "positionFlipAtOpenInterestCapRejected",
                  "tooAggressiveAtOpenInterestCapRejected", "openInterestIncreaseRejected"}
HIST_MATCH_MS = 2000


def load_hist_rejects(window_dir) -> dict:
    """taker_reject hist: the chunk's hist_orders.json (capture HIST_IOC_USERS) -> {key: [timestamp ms]} of
    BTC/ETH placements with a HIDDEN_REJECTS final status. key = ("c", user, cloid) when the order has a cloid,
    else (user, asset, side, px ticks, size lots)."""
    path = Path(window_dir) / "hist_orders.json"
    if not path.exists():
        return {}
    by_name = {a.name: (aid, a) for aid, a in ASSETS.items()}
    out: dict = {}
    for r in json.load(open(path)):
        ans = r.get("answer")
        if not isinstance(ans, list):
            continue
        user = r["user"].lower()
        for rec in ans:
            o = rec.get("order") or {}
            if rec.get("status") not in HIDDEN_REJECTS or o.get("coin") not in by_name:
                continue
            aid, a = by_name[o["coin"]]
            ts = int(o["timestamp"])
            if o.get("cloid"):
                out.setdefault(("c", user, o["cloid"].lower()), []).append(ts)
            side = Side.BUY if o["side"] == "B" else Side.SELL
            try:
                key = (user, aid, side, a.px_to_ticks(o["limitPx"]), a.sz_to_lots(o.get("origSz") or o["sz"]))
            except (ValueError, ArithmeticError):
                continue
            out.setdefault(key, []).append(ts)
    return out


def hist_rejected(rej: dict, p: Place, t_ms: int) -> bool:
    """True when this placement matches a hidden rejection within HIST_MATCH_MS of the block time."""
    keys = [(p.user.lower(), p.asset, p.side, p.px, p.sz)]
    if p.cloid:
        keys.insert(0, ("c", p.user.lower(), p.cloid.lower()))
    return any(abs(ts - t_ms) <= HIST_MATCH_MS for k in keys for ts in rej.get(k, ()))


MAX_LEVERAGE = {"BTC": 40, "ETH": 25}          # Hyperliquid max leverage (Nov 2026): the most lenient margin need


def load_margin(window_dir, spot: bool = False) -> dict:
    """taker_reject margin: user -> (free margin USD, {coin: signed position size}) from the chunk's plan-time
    clearinghouseState.json (collect/capture.py MARGIN_USERS). free = accountValue - totalMarginUsed (perp only);
    spot=True adds the spot USDC available after maintenance (unified accounts). Missing file: {}."""
    path = Path(window_dir) / "clearinghouseState.json"
    if not path.exists():
        return {}
    out = {}
    for r in json.load(open(path)):
        ans = r.get("answer")
        if not isinstance(ans, dict) or "marginSummary" not in ans:
            continue
        ms = ans["marginSummary"]
        free = float(ms["accountValue"]) - float(ms["totalMarginUsed"])
        pos = {p["position"]["coin"]: float(p["position"]["szi"]) for p in ans.get("assetPositions", [])}
        out[r["user"].lower()] = (free, pos)
    if spot:
        sp = Path(window_dir) / "spotClearinghouseState.json"
        for r in (json.load(open(sp)) if sp.exists() else []):
            ans, u = r.get("answer"), r["user"].lower()
            if u in out and isinstance(ans, dict):
                avail = dict((int(k), float(v)) for k, v in ans.get("tokenToAvailableAfterMaintenance") or [])
                if 0 in avail:
                    out[u] = (out[u][0] + avail[0], out[u][1])
    return out


class Replay:
    def __init__(self, window_dir, hl_data, policy: Policy, *, anchors=None, use_resolver: bool = True,
                 nearest_ratio: int = 0, check_permutation: bool = True, hasher: Hasher | None = None,
                 observer: Observer | None = None, engine: Engine | None = None,
                 alo_gate: str | None = None, gate_max_age_ms: int | None = None,
                 reconcile_known: str | None = None, reseed: bool = False,
                 touch_reconcile: bool = False, touch_n_cap: bool = False, touch_refill: str | None = None,
                 keep_known: bool = False, skip_lead_in: bool = False, lead_in_keep: int | None = None,
                 touch_evict: str = "oldest", ledger_k: int = 3,
                 snapshot_shift: int = 0, outside_position: str = "end", outside_other_txs: bool = False,
                 other_position: str = "end",
                 placeholder_ttl_ms: int | None = None, placeholder_queue: str = "ahead",
                 placeholder_cap: str | None = None, reseed_keep_known: bool = False, placeholder_split: bool = False,
                 named_cancel: bool = False,
                 trade_learn: str | None = None, trade_learn_at: str = "tx", oid_guard: str | None = None,
                 refill_position: str = "front",
                 oid_facts: bool = False, name_prewindow: bool = False, named_anon: str = "front",
                 deep_book: bool = False, deep_lookback_ms: int = 600_000, learn_keep_known: bool = False,
                 learn_evidence: str = "loose",
                 facts_exclude: tuple = (), use_bbo: bool = True, anchor_drop_kinds: tuple = (),
                 oid_bracket: bool = False, taker_reject: str | None = None,
                 taker_seed=None):
        """oid_bracket: reference points from the tracked users' orders on every coin + the monotone c(k) bracket
        rule in the resolver (data/oids.py).
        anchors: path to an anchors file; None = the window's anchors.json if present.
        check_permutation=False only for measurement runs whose policy drops or rewrites actions.
        alo_gate="touch" (G1 fix #1): ALOs are admitted or rejected against RealTouch, not our
        book; an admitted ALO that crosses our book evicts what it crosses (it cannot have been
        there). The gate abstains (engine rule) when the touch is older than gate_max_age_ms.
        reconcile_known="oldest"|"newest" (G1 fix #2): rule 8 also removes drift from known orders.
        reseed=True (G1 #3, short horizons): at every snapshot, after measuring rule 8 trimmed/drift/
        missing, the asset's book is cleared and re-seeded from the snapshot as placeholders, so
        errors cannot carry past one snapshot interval. Scoring starts at the first block.
        touch_reconcile=True (needs recorded bbo): before every block, rule 8 against the latest bbo
        as a one-level snapshot, evicting known orders oldest first: nothing rests better than the
        real touch, and the touch level holds at most its real size. touch_n_cap=True also evicts
        known orders (oldest first) while our touch level has more orders than bbo's n.
        touch_refill="front"|"back" (implies touch_reconcile; Nov 20): after that trim, wherever the bbo touch
        level holds more than we do, the difference goes in as one placeholder, ahead of (front) or behind
        (back) the level's orders. The bbo is stamped with block time and describes the book after that
        block, so the latest one before a block is the real touch going into it.
        Lead-in chunks (window.json lead_in_blocks > 0, collect/capture.py LEAD_IN_BLOCKS): blocks before
        warmup_end_height are replayed but never scored, also with reseed. skip_lead_in=True drops them
        (exactly what a chunk without lead-in would replay). keep_known=True (needs reseed): at every
        snapshot, instead of clearing, rule 8 with oldest-first eviction and then Engine.refill: the
        snapshot size we lack goes in as placeholders at the FRONT of each level, and known orders
        carry over (scoring treats them as known, not as pre-re-seed wildcards).
        Measurement flags (G1 triage; defaults = the rules above):
        snapshot_shift=k applies each snapshot k blocks later (k < 0: earlier) than rule 8's
        block; snapshots pushed outside the window are dropped (counted in `shift_dropped`).
        outside_position="start" applies outside fills before the block's policy-ordered actions.
        outside_other_txs=True: trades linked to non-book txs (SetGlobalAction) become outside fills
        (data/parse.iter_window).
        other_position (needs outside_other_txs; default "end" = with the zero-hash fills, as above):
        where the non-zero-hash outside fills go, the zero-hash ones staying at outside_position:
        "start" = before every other action of the block; "index" = at their own tx's position, i.e.
        before the first policy-ordered action whose tx_index is larger (listing order: exactly
        where the tx sits in the block).
        Placeholder measurement flags (H11 deep dive; defaults = the rules above, outputs unchanged):
        placeholder_ttl_ms=N: before each block, placeholders seeded more than N ms before the block
        time are removed (anonymous liquidity is trusted for N ms only).
        placeholder_queue="behind": whenever a known order rests at a level holding placeholders, the
        placeholders move behind it (known orders get time priority over snapshot liquidity).
        placeholder_cap="next" (LOOK-AHEAD, measurement only): each re-seeded level holds at most the
        NEXT snapshot's size at that price plus the real lots traded there (maker side) until it;
        prices outside the next snapshot's range keep the snapshot size.
        reseed_keep_known=True: alias of keep_known=True (the deep dive's own re-implementation was
        dropped at integration; the refill placeholders are registered in ph_meta like seeded ones).
        `ph_meta` maps every placeholder oid to (seed snapshot time ms, rank of its price in that
        snapshot side (0 = best), seeded lots, asset) for analyses.
        trade_learn_at="tx" (default) | "block-end" (Nov 21, for counterfactuals): "tx" applies each tx's corrections
        right after its last action, i.e. mid-block at a position that depends on the ordering policy; "block-end"
        applies all of the block's corrections after its last action (before end_block), in the block's listing
        order (tx_index), so they never change who fills inside the block and their position is the same under
        every policy.
        trade_learn (measurement, default None = off): after each tx's last action, the tx's REAL
        trades (recorder, linked by hash) correct the book for LATER actions only; the tx itself was
        already applied and scored. Uses no information from the future.
          "sweep": a real taker that traded down to price w on a maker side consumed everything better
                   than w on that side (price-time priority), so any order (known or placeholder) the
                   engine still holds better than w is stale and is removed.
          "name":  at w, the last real maker M (highest tid at w) may still rest there; if the engine's
                   front order at w is a placeholder and M has no order at w, that placeholder becomes
                   M's order (same size, still at the front). Guess: M's order may have been used up.
          "queue": at each price p where the chain filled maker M and the engine holds an order of M at p,
                   everything the engine holds AHEAD of M's first order at p is stale (the chain reached M,
                   so all of it was consumed first) and is removed.
          "name-clip": as name, but only the size M just filled is named; the rest stays anonymous.
          Rules combine with commas, applied sweep, queue, name: e.g. "sweep,queue,name-clip".
          Aliases: "both" = "sweep,name", "both-clip" = "sweep,name-clip". Counters in `learn_stats`.
        refill_position (keep_known): "front" (default) = Engine.refill's placement, missing snapshot size
        ahead of every known order at the level; "back" = moved behind them (known orders queue first).
        oid_facts: the int-oid resolver also uses real order-id facts (data/oid_facts.py: fills.json,
        hist_orders.json, open_orders.json in the window, user_orders/user_fills recorder streams): located facts
        become anchors and exact oid -> placement matches; other facts give the target's side and price.
        name_prewindow=True (with oid_facts): pre-window orders the window's open_orders.json shows resting (placed
        before the first block) take their share of the seeded placeholder at their price as KNOWN orders of
        their real owner, in real-oid order, so the owner's later int-oid / cloid cancels and modifies find them
        and their fills carry the real maker. Size at the first block = size at the answer + that order's fills
        since (fact fills; recorded trades of the maker at that price when the maker has no fill facts), capped by
        the placeholder. named_anon: the anonymous rest of the level queues "front" (ahead) or "back" of them.
        A price the snapshot range covers but does not show is skipped (named_conflict); deeper prices add the
        order outright (named_deep). `named` maps real oid -> engine oid; `name_stats` counts.
        deep_book=True: liquidity past the full snapshot's last level, from the recorder's coarse book (book_*_sf4,
        same times; prices grouped into buckets). At every full snapshot each bucket's size beyond that last level
        (less the full levels inside it and the known orders resting there) goes into a RESERVE, split over the
        bucket's prices by what was observable up to then: sizes those prices showed in full snapshots of the last
        deep_lookback_ms, real trades there, known orders there; evenly over the bucket's prices when nothing
        was seen. The reserve enters the book only for an order whose own limit reaches past that last level
        (a non-ALO buy above the last ask / sell below the last bid), as placeholders behind any known order at
        each price; what that order leaves is taken out again right after it. Nothing else ever sees it.
        `deep_stats` counts.
        facts_exclude: order-id fact sources to leave out (data/oid_facts.SOURCES; ablations).
        use_bbo=False: the touch (ALO gate, touch reconcile) ignores the recorded bbo feed (snapshots + trades only).
        learn_keep_known=True: trade_learn's sweep and queue rules remove placeholders only, never a known order
        (one we saw placed): such an order may still rest on chain (its owner's later cancel names it).
        oid_guard (int-oid resolver preset; None/"ghost" = data/oids defaults): "ratio25" / "ratio50"
        (ratio guard, ghost off), "cap3000" (max_tol 3000, ghost off), "none" (old rule: ghost and
        joint layers off).
        Zero-hash trades (outside fills) are not used: they are applied as fills already."""
        keep_known = keep_known or reseed_keep_known
        if other_position not in ("end", "start", "index"):
            raise ValueError(f"unknown other_position {other_position!r}")
        if other_position != "end" and not outside_other_txs:
            raise ValueError("other_position needs outside_other_txs")
        self.other_position = other_position
        if keep_known and not reseed:
            raise ValueError("keep_known needs reseed=True")
        self.window_dir = Path(window_dir)
        self.hl_data = Path(hl_data)
        self.policy = policy
        self.check_permutation = check_permutation
        self.hasher = hasher
        self.observer = observer or Observer()
        self.engine = engine or Engine()
        meta_path = self.window_dir / "window.json"
        self.window_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        blocks, self.parse_stats = load_window(self.window_dir, self.hl_data, outside_other_txs,
                                               other_at_tx_index=other_position == "index")   # decoded once per process
        self.lead_in = int(self.window_meta.get("lead_in_blocks", 0) or 0)
        self.skip_lead_in = skip_lead_in
        if skip_lead_in and self.lead_in:
            blocks = [b for b in blocks if b.height >= self.window_meta["warmup_end_height"]]
            if not blocks:
                raise ValueError(f"{self.window_dir}: nothing after the lead-in")
        elif lead_in_keep is not None and self.lead_in:     # measurement: keep only the last N lead-in blocks
            w = self.window_meta["warmup_end_height"]
            blocks = [b for b in blocks if b.height >= w - lead_in_keep]
        self.blocks: list[Block] = list(blocks)
        self.t0, self.t1 = self.blocks[0].time_ms, self.blocks[-1].time_ms
        self.warmup_end_height: int = max(self.window_meta.get("warmup_end_height", self.blocks[0].height),
                                          self.blocks[0].height)

        self.placed: dict[int, Place] = {}         # engine oid -> the order placed (a modify's `new`)
        self.placed_by: dict[int, Action] = {}     # engine oid -> the Place or Modify action itself
        self.block_index = -1                      # index of the block being applied
        self.seed_offsets: dict[str, int] = {}     # coin -> seed snapshot time - first block time (ms)
        self.ph_meta: dict[int, tuple[int, int, int, int]] = {}   # placeholder oid -> (time, rank, lots, asset)
        self._ph_heap: list[tuple[int, int]] = []                # (seed time, oid) min-heap (ttl)
        if placeholder_queue not in ("ahead", "behind"):
            raise ValueError(f"unknown placeholder_queue {placeholder_queue!r}")
        if placeholder_cap not in (None, "next"):
            raise ValueError(f"unknown placeholder_cap {placeholder_cap!r}")
        if reseed_keep_known and not reseed:
            raise ValueError("reseed_keep_known needs reseed=True")
        self.placeholder_ttl_ms, self.placeholder_queue = placeholder_ttl_ms, placeholder_queue
        self.placeholder_cap, self.reseed_keep_known = placeholder_cap, reseed_keep_known   # alias, see keep_known
        self.placeholder_split = placeholder_split  # A4a: a snapshot level's uncovered size = its uncovered orders
        # A4b2: an unresolvable cancel of user U on asset a removes U's trade-named piece on a if exactly one rests
        self.named_cancel = named_cancel
        self._trade_named: dict[tuple[str, int], list[int]] = {}   # (user, asset) -> engine oids named by trade_learn
        self.ph_stats: Counter = Counter()          # placeholder flag effects (lots / counts)
        self.trade_learn = trade_learn
        self._rules = self.parse_trade_learn(trade_learn)
        if trade_learn_at not in ("tx", "block-end"):
            raise ValueError(f"unknown trade_learn_at {trade_learn_at!r}")
        self.trade_learn_at = trade_learn_at
        self._name_clip = "name-clip" in self._rules
        self.learn_keep_known = learn_keep_known
        # trade_learn evidence (Satya Q7, docs/deviations.md D33):
        #   "loose": the original rules (queue cuts at every real maker's first order; sweep removes every order
        #            priced better than the tx's real worst fill, as the book stands when the correction runs);
        #   "tx":    only what the tx itself shows: queue cuts only for makers our engine filled LESS than the chain
        #            did at that price in that tx; sweep removes only orders that were resting before the tx ran.
        if learn_evidence not in ("loose", "tx"):
            raise ValueError(f"unknown learn_evidence {learn_evidence!r}")
        self.learn_evidence = learn_evidence
        self._seq_at_tx: dict[str, int] = {}              # this block: engine next_seq when each tx started
        self._fills_by_hash: dict[str, list] = {}         # this block's engine fills by taker tx hash
        if oid_guard not in OID_GUARDS:
            raise ValueError(f"unknown oid_guard {oid_guard!r}")
        self.oid_guard = oid_guard
        self.oid_facts = oid_facts
        if refill_position not in ("front", "back"):
            raise ValueError(f"unknown refill_position {refill_position!r}")
        self.refill_position = refill_position
        self.learn_stats: Counter = Counter()
        self.taker_reject = frozenset(x for x in (taker_reject or "").split(",") if x)
        bad = self.taker_reject - {"min10", "learn", "margin", "margin_spot", "hist"}
        if bad:
            raise ValueError(f"unknown taker_reject rules {sorted(bad)}")
        self.taker_stats: Counter = Counter()
        self._taker_blocked: set[str] = set(taker_seed or ())    # learn: users whose last engine-filled IOC had no real fill (seeded from earlier chunks)
        self._blk_ioc: dict[str, tuple[str, bool]] = {}   # this block: hash -> (user, engine filled or skipped)
        self._margin = load_margin(self.window_dir, spot="margin_spot" in self.taker_reject) \
            if self.taker_reject & {"margin", "margin_spot"} else {}
        self._hist_rej = load_hist_rejects(self.window_dir) if "hist" in self.taker_reject else {}
        self._real_trades: dict[str, list] = {}
        if self._rules or "learn" in self.taker_reject:
            for tr in iter_trades(self.hl_data, COINS, self.t0, self.t1):
                if tr.hash != ZERO_HASH:
                    self._real_trades.setdefault(tr.hash, []).append(tr)
        self.facts_exclude, self.use_bbo = tuple(facts_exclude), use_bbo
        self.anchor_drop_kinds = tuple(anchor_drop_kinds)    # e.g. ("target",): ignore capture's cancel-target lookups
        self.deep_book, self.deep_lookback_ms = deep_book, deep_lookback_ms
        self.deep_stats: Counter = Counter()
        self._deep: dict[tuple[int, Side], dict[int, int]] = {}      # (asset, side) -> {px: lots} reserve
        self._deep_edge: dict[tuple[int, Side], int] = {}            # last full level price per side
        if deep_book:
            lo = self.t0 - deep_lookback_ms - 60_000
            self._sf = {(s.asset, s.time_ms): s for coin in COINS
                        for s in iter_books(self.hl_data, coin, lo, self.t1 + 60_000, suffix="_sf4")}
            self._full_hist = sorted((s for coin in COINS for s in iter_books(self.hl_data, coin, lo, self.t1 + 60_000)),
                                     key=lambda s: s.time_ms)
            self._deep_trades = [tr for tr in iter_trades(self.hl_data, COINS, lo, self.t1 + 60_000)
                                 if tr.hash != ZERO_HASH]
        self._seed()
        if name_prewindow and not oid_facts:
            raise ValueError("name_prewindow needs oid_facts")
        if named_anon not in ("front", "back"):
            raise ValueError(f"unknown named_anon {named_anon!r}")
        self.named: dict[int, int] = {}
        self.named_levels: set[tuple[str, int, Side, int]] = set()   # (user, asset, side, px) of named orders
        self.name_stats: Counter = Counter()
        self.named_anon = named_anon
        if name_prewindow:
            self._name_prewindow()

        self.resolver: OidResolver | None = None
        if anchors is None and (self.window_dir / "anchors.json").exists():
            anchors = self.window_dir / "anchors.json"
        if use_resolver and oid_facts:
            est = OidEstimator.from_files(self.window_dir, anchors, facts=True, hl_data=self.hl_data,
                                          exclude=self.facts_exclude, drop_kinds=self.anchor_drop_kinds,
                                          all_coins=oid_bracket)
            self.resolver = OidResolver(self.engine, est, nearest_ratio=nearest_ratio, bracket=oid_bracket,
                                        **OID_GUARDS[oid_guard])
            self.resolver.named = self.named
            self.engine.resolve_oid = self.resolver
        elif use_resolver and anchors is not None:
            est = OidEstimator.from_files(self.window_dir, anchors, drop_kinds=self.anchor_drop_kinds)
            self.resolver = OidResolver(self.engine, est, nearest_ratio=nearest_ratio, **OID_GUARDS[oid_guard])
            self.engine.resolve_oid = self.resolver

        self.snapshots = sorted((s for coin in COINS for s in iter_books(self.hl_data, coin, self.t0, self.t1)),
                                key=lambda s: (s.time_ms, s.asset))
        if alo_gate not in (None, "touch"):
            raise ValueError(f"unknown alo_gate {alo_gate!r}")
        self.alo_gate, self.gate_max_age_ms, self.reconcile_known = alo_gate, gate_max_age_ms, reconcile_known
        self.reseed, self.keep_known = reseed, keep_known
        if outside_position not in ("end", "start"):
            raise ValueError(f"unknown outside_position {outside_position!r}")
        self.snapshot_shift, self.outside_position = snapshot_shift, outside_position
        self.outside_other_txs = outside_other_txs
        self.shift_dropped = 0
        if reseed and not self.lead_in:
            self.warmup_end_height = self.blocks[0].height     # every horizon starts from a snapshot
        self.touch: RealTouch | None = None
        if touch_refill not in (None, "front", "back"):
            raise ValueError(f"unknown touch_refill {touch_refill!r}")
        self.touch_refill = touch_refill
        if touch_evict not in ("oldest", "newest", "ledger"):
            raise ValueError(f"unknown touch_evict {touch_evict!r}")
        self.touch_evict = touch_evict
        self.ledger_k = ledger_k          # touch_evict="ledger": most orders removed from one touch level (0 = none)
        self.touch_reconcile, self.touch_n_cap = touch_reconcile or touch_refill is not None, touch_n_cap
        touch_reconcile = self.touch_reconcile
        self.touch_reconciled: Counter = Counter()  # (coin, trimmed|drift|evicted|...|n_capped) -> lots/orders
        self._next_snap: dict[tuple[int, int], object] = {}
        self._trades_by: dict[tuple, list] = {}
        if placeholder_cap == "next":
            last: dict[int, object] = {}
            for s in self.snapshots:
                if s.asset in last:
                    self._next_snap[(last[s.asset].time_ms, s.asset)] = s
                last[s.asset] = s
            for tr in iter_trades(self.hl_data, COINS, self.t0 - TOUCH_LOOKBACK_MS, self.t1 + TOUCH_LOOKBACK_MS):
                self._trades_by.setdefault((tr.asset, tr.taker_side.opposite, tr.px), []).append((tr.time_ms, tr.sz))
        if alo_gate == "touch" or touch_reconcile:
            lo = self.t0 - TOUCH_LOOKBACK_MS
            self.touch = RealTouch(
                [s for coin in COINS for s in iter_books(self.hl_data, coin, lo, self.t1)],
                iter_trades(self.hl_data, COINS, lo, self.t1),
                [x for coin in COINS for x in iter_bbo(self.hl_data, coin, lo, self.t1)] if self.use_bbo else [])
        self.gate_stats: Counter = Counter()
        self.reconciled: Counter = Counter()        # (coin, trimmed|drift|missing|evicted...) -> lots
        self.outside_fill_log: list[tuple[int, int, int]] = []   # (height, tid, lots unmatched)

    # ------------------------------------------------------------ setup
    def _seed(self) -> None:
        for coin in COINS:
            snap = nearest_book(self.hl_data, coin, self.t0)
            self._seed_levels(snap.asset, snap.bids, snap.asks, snap.time_ms,
                              counts=(snap.bid_n, snap.ask_n) if self.placeholder_split else None)
            self.seed_offsets[coin] = snap.time_ms - self.t0
            if self.deep_book:
                self._build_deep(snap)

    def _name_prewindow(self) -> None:
        """name_prewindow (see __init__): split the seeded placeholders into the known pre-window orders."""
        from data import oid_facts
        facts = oid_facts.load(self.window_dir, self.hl_data, exclude=self.facts_exclude)
        alive = self.t0 + max(self.seed_offsets.values(), default=0)
        items = oid_facts.resting_at(facts, self.t0, max(alive, self.t0))
        if not items:
            return
        fill_users = {f["user"] for f in facts.fills}
        fills_of: dict[int, list] = {}
        for f in facts.fills:
            fills_of.setdefault(f["oid"], []).append((f["time"], ASSETS[f["asset"]].sz_to_lots(f["sz"])))
        t_end = max([x["ref_ms"] or self.t1 for x in items] + [self.t1])
        traded: Counter = Counter()                       # (maker, asset, maker side, px) -> lots since t0
        for tr in iter_trades(self.hl_data, COINS, self.t0, t_end):
            if tr.hash != ZERO_HASH:
                traded[(tr.maker, tr.asset, tr.taker_side.opposite, tr.px)] += tr.sz
        eng = self.engine
        by_level: dict[tuple[int, Side, int], list] = {}
        for x in items:
            o, ref = x["order"], x["ref_ms"]
            spec = ASSETS[o["asset"]]
            side, px = (Side.BUY if o["buy"] else Side.SELL), spec.px_to_ticks(o["px"])
            q = spec.sz_to_lots(x["rem"])
            fs = fills_of.get(o["oid"], ())
            if ref is None:                              # original size less what filled before the window
                q -= sum(sz for t, sz in fs if t < self.t0)
                self.name_stats["sized_from_orig"] += 1
            elif o["user"] in fill_users:                # size then + what filled since the window began
                q += sum(sz for t, sz in fs if self.t0 <= t <= ref)
            else:                                        # no fill facts: the maker's trades at that price
                k = (o["user"], o["asset"], side, px)
                add = min(traded[k], max(spec.sz_to_lots(o["sz"]) - q, 0))
                traded[k] -= add
                q += add
            self.name_stats["from_" + o["source"]] += 1
            if q > 0:
                by_level.setdefault((o["asset"], side, px), []).append((o, q))
        edge = {(a, sd): (min(lv) if sd is Side.BUY else max(lv)) if lv else None     # the seeded snapshot's range
                for a in eng.books for sd in (Side.BUY, Side.SELL) for lv in (eng.books[a].side(sd),)}
        for (asset, side, px), named in sorted(by_level.items(), key=lambda kv: (kv[0][0], kv[0][1].value, kv[0][2])):
            book = eng.books[asset]
            level = book.side(side).get(px)
            phs = [nd for nd in level if nd.order.user == PLACEHOLDER] if level is not None else []
            if not phs:
                e = edge[(asset, side)]
                deeper = e is not None and (px < e if side is Side.BUY else px > e)
                if level is not None or not deeper:
                    self.name_stats["named_conflict"] += len(named)
                    continue
                for o, q in named:
                    self._rest_named(o, asset, side, px, q)
                    self.name_stats["named_deep"] += 1
                continue
            ph = phs[0]
            avail = ph.remaining
            meta = self.ph_meta.get(ph.order.oid)
            eng._remove(ph.order.oid)
            take = []
            for o, q in named:
                q2 = min(q, avail)
                if q2 <= 0:
                    self.name_stats["named_no_room"] += 1
                    continue
                self.name_stats["named_clipped_lots"] += q - q2
                avail -= q2
                take.append((o, q2))
            anon = lambda: avail > 0 and self._seed_one(asset, side, px, avail, *(meta[:2] if meta else (self.t0, 0)))
            if self.named_anon == "front":
                anon()
            for o, q in take:
                self._rest_named(o, asset, side, px, q)
                self.name_stats["named"] += 1
                self.name_stats["named_lots"] += q
            if self.named_anon == "back":
                anon()

    def _rest_named(self, o: dict, asset: int, side: Side, px: int, q: int) -> None:
        eng = self.engine
        order = eng._new_order(o["user"], asset, side, px, q, Tif.GTC, o.get("cloid"))
        eng._rest(order, q)
        self.named[o["oid"]] = order.oid
        self.named_levels.add((o["user"], asset, side, px))

    # ------------------------------------------------------------ deep book (deep_book=True)
    def _build_deep(self, snap) -> None:
        """Reserve of liquidity past snap's last full level, from the coarse book at the same time (see __init__)."""
        asset = snap.asset
        for side in (Side.BUY, Side.SELL):
            self._deep[(asset, side)] = {}
            self._deep_edge.pop((asset, side), None)
        sf = self._sf.get((asset, snap.time_ms))
        if sf is None:
            self.deep_stats["no_coarse_book"] += 1
            return
        book = self.engine.books[asset]
        for side, full, coarse in ((Side.BUY, snap.bids, sf.bids), (Side.SELL, snap.asks, sf.asks)):
            if not full or len(coarse) < 2:
                continue
            edge = full[-1][0]
            self._deep_edge[(asset, side)] = edge
            pxs = [px for px, _ in coarse]
            w = min(abs(a - b) for a, b in zip(pxs, pxs[1:]))
            fullmap = dict(full)
            res: dict[int, int] = {}
            ev = self._deep_evidence(asset, side, snap.time_ms)
            for bpx, bsz in coarse:
                lo, hi = (bpx, bpx + w - 1) if side is Side.BUY else (bpx - w + 1, bpx)   # bucket's tick range
                beyond = [p for p in range(lo, hi + 1) if (p < edge if side is Side.BUY else p > edge)]
                if not beyond:
                    continue
                levels = book.side(side)
                known = sum(levels[p].total for p in beyond if p in levels)
                q = bsz - sum(sz for p, sz in fullmap.items() if lo <= p <= hi) - known
                if q <= 0:
                    continue
                wts = {p: ev.get(p, 0) + (levels[p].total if p in levels else 0) for p in beyond}
                if sum(wts.values()) > 0:
                    self.deep_stats["buckets_from_evidence"] += 1
                else:
                    wts = {p: 1 for p in beyond}
                    self.deep_stats["buckets_even"] += 1
                tot, given = sum(wts.values()), 0
                shares = sorted(((p, q * wt // tot) for p, wt in wts.items() if wt), key=lambda x: -wts[x[0]])
                for p, x in shares:
                    given += x
                shares[0] = (shares[0][0], shares[0][1] + q - given)         # rounding to the best-supported price
                for p, x in shares:
                    if x > 0:
                        res[p] = res.get(p, 0) + x
                self.deep_stats["reserve_lots"] += q
            self._deep[(asset, side)] = res

    def _deep_evidence(self, asset: int, side: Side, at_ms: int) -> dict[int, int]:
        """{px: weight} observable by at_ms: last size each price showed in a full snapshot of the lookback, plus
        real maker-side trade size there."""
        lo = at_ms - self.deep_lookback_ms
        ev: dict[int, int] = {}
        for s in self._full_hist:
            if s.asset != asset or not lo <= s.time_ms <= at_ms:
                continue
            for px, sz in (s.bids if side is Side.BUY else s.asks):
                ev[px] = sz
        for tr in self._deep_trades:
            if tr.asset == asset and lo <= tr.time_ms <= at_ms and tr.taker_side.opposite is side:
                ev[tr.px] = ev.get(tr.px, 0) + tr.sz
        return ev

    def _deep_in(self, action) -> list | None:
        """Put the reserve into the book for an order whose limit reaches past the last full level."""
        p = action.new if isinstance(action, Modify) else action
        if not isinstance(p, Place) or p.tif is Tif.ALO:
            return None
        side = p.side.opposite
        edge = self._deep_edge.get((p.asset, side))
        res = self._deep.get((p.asset, side))
        if edge is None or not res or not (p.px > edge if p.side is Side.BUY else p.px < edge):
            return None
        eng = self.engine
        book = eng.books[p.asset]
        own = book.side(p.side)
        best_own = (max(own) if p.side is Side.BUY else min(own)) if own else None
        out = []
        for px in sorted(res, reverse=side is Side.BUY):
            if not (px <= p.px if p.side is Side.BUY else px >= p.px):
                continue                                 # past this order's limit: it cannot reach
            if best_own is not None and (px <= best_own if side is Side.SELL else px >= best_own):
                continue                                 # would cross our own book
            oid = eng.seed(p.asset, side, px, res[px])
            self.ph_meta[oid] = (self._block.time_ms if self._block is not None else self.t0, -1, res[px], p.asset)
            out.append((oid, p.asset, side, px, res[px]))
        if out:
            self.deep_stats["orders_reaching_past"] += 1
        return out

    def _deep_out(self, placed: list) -> None:
        """Take back what the order left; the reserve keeps only that."""
        eng = self.engine
        for oid, asset, side, px, q in placed:
            book = eng.books[asset]
            left = book.node(oid).remaining if oid in book else 0
            if oid in book:
                book.remove(oid)
            used = q - left
            res = self._deep[(asset, side)]
            if left:
                res[px] = left
            else:
                res.pop(px, None)
            if used:
                self.deep_stats["consumed_lots"] += used
                self.deep_stats["consumed_levels"] += 1

    def _seed_one(self, asset: int, side: Side, px: int, sz: int, time_ms: int, rank: int) -> int:
        oid = self.engine.seed(asset, side, px, sz)
        self.ph_meta[oid] = (time_ms, rank, sz, asset)
        if self.placeholder_ttl_ms is not None:
            heapq.heappush(self._ph_heap, (time_ms, oid))
        return oid

    def _seed_levels(self, asset: int, bids, asks, time_ms: int, sizes=None, counts=None) -> None:
        """One placeholder per snapshot level (tail of the level). sizes: {(side, px): lots} overrides.
        counts (A4a, --placeholder-split): (bid_n, ask_n); each level becomes n equal placeholders instead of one."""
        for side, levels, ns in ((Side.BUY, bids, counts[0] if counts else ()),
                                 (Side.SELL, asks, counts[1] if counts else ())):
            for rank, (px, sz) in enumerate(levels):
                q = sz if sizes is None else sizes.get((side, px), sz)
                if q > 0:
                    k = max(1, min(q, ns[rank])) if rank < len(ns) else 1
                    for piece in split_equal(q, k):
                        self._seed_one(asset, side, px, piece, time_ms, rank)

    def _expire_placeholders(self, now_ms: int) -> None:
        eng = self.engine
        while self._ph_heap and self._ph_heap[0][0] < now_ms - self.placeholder_ttl_ms:
            _, oid = heapq.heappop(self._ph_heap)
            asset = self.ph_meta[oid][3]
            if oid in eng.books[asset]:
                q = eng.books[asset].node(oid).remaining
                eng._remove(oid)
                self.ph_stats[f"ttl_expired_lots:{ASSETS[asset].name}"] += q
                self.ph_stats["ttl_expired_orders"] += 1

    def _placeholders_behind(self, oid: int, asset: int) -> None:
        """placeholder_queue="behind": move the level's placeholders behind the order just rested."""
        eng = self.engine
        book = eng.books[asset]
        if oid not in book:
            return
        o = book.node(oid).order
        level = book.side(o.side)[o.px]
        phs = [(nd.order.oid, nd.remaining) for nd in level if nd.order.user == PLACEHOLDER]
        for ph, q in phs:
            t, rank, lots, a = self.ph_meta[ph]
            eng._remove(ph)
            new = self._seed_one(asset, o.side, o.px, q, t, rank)
            self.ph_meta[new] = (t, rank, lots, a)
            self.ph_stats["moved_behind"] += 1

    def _cap_sizes(self, s) -> dict:
        """placeholder_cap="next" (look-ahead): {(side, px): lots} for snapshot s."""
        nxt = self._next_snap.get((s.time_ms, s.asset))
        if nxt is None:
            return {}
        out = {}
        for side, levels, nlevels in ((Side.BUY, s.bids, nxt.bids), (Side.SELL, s.asks, nxt.asks)):
            if not nlevels:
                continue
            nmap = dict(nlevels)
            edge = min(nmap) if side is Side.BUY else max(nmap)
            for px, sz in levels:
                if (px >= edge) if side is Side.BUY else (px <= edge):
                    traded = self._traded(s.asset, side, px, s.time_ms, nxt.time_ms)
                    q = min(sz, nmap.get(px, 0) + traded)
                    out[(side, px)] = q
                    self.ph_stats[f"cap_removed_lots:{ASSETS[s.asset].name}"] += sz - q
        return out

    def _traded(self, asset, maker_side, px, t0, t1) -> int:
        trs = self._trades_by.get((asset, maker_side, px), ())
        i = bisect.bisect_left(trs, (t0, -1))
        j = bisect.bisect_left(trs, (t1, -1))
        return sum(q for _, q in trs[i:j])

    def _refill_to_back(self, asset: int, first_oid: int) -> None:
        """refill_position="back": move the placeholders Engine.refill just created behind the level."""
        book = self.engine.books[asset]
        for oid in range(first_oid, self.engine.next_oid):
            if oid in book:
                nd = book.node(oid)
                order, q = nd.order, nd.remaining
                book.remove(oid)
                book.add(order, q)

    def _note_refill(self, s, first_oid: int) -> None:
        """keep_known: register the placeholders Engine.refill just created (oids first_oid..) in
        ph_meta (and the ttl heap), as _seed_one does for seeded ones."""
        ranks = {(side, px): r for side, levels in ((Side.BUY, s.bids), (Side.SELL, s.asks))
                 for r, (px, _) in enumerate(levels)}
        book = self.engine.books[s.asset]
        for oid in range(first_oid, self.engine.next_oid):
            if oid not in book:
                continue
            nd = book.node(oid)
            self.ph_meta[oid] = (s.time_ms, ranks[(nd.order.side, nd.order.px)], nd.remaining, s.asset)
            if self.placeholder_ttl_ms is not None:
                heapq.heappush(self._ph_heap, (s.time_ms, oid))

    @staticmethod
    def parse_trade_learn(spec: str | None) -> frozenset:
        if spec is None:
            return frozenset()
        spec = {"both": "sweep,name", "both-clip": "sweep,name-clip"}.get(spec, spec)
        rules = frozenset(x.strip() for x in spec.split(","))
        if not rules or not rules <= {"sweep", "queue", "name", "name-clip"} or {"name", "name-clip"} <= rules:
            raise ValueError(f"unknown trade_learn {spec!r}")
        return rules

    def _learn_queue(self, asset: int, side: Side, trs: list, coin: str) -> None:
        """queue rule: at each price with real fills, drop what rests ahead of the first order (in our
        queue) of any maker the chain filled there."""
        eng = self.engine
        levels = eng.books[asset].side(side)
        for px in sorted({t.px for t in trs}):
            level = levels.get(px)
            if level is None:
                continue
            makers = {t.maker for t in trs if t.px == px}
            if self.learn_evidence == "tx":
                real: Counter = Counter()
                ours: Counter = Counter()
                for t in trs:
                    if t.px == px:
                        real[t.maker] += t.sz
                for fl in self._fills_by_hash.get(trs[0].hash, ()):
                    if fl.px == px:
                        ours[fl.maker_user] += fl.sz
                makers = {m for m in makers if ours[m] < real[m]}
                if not makers:
                    continue                       # our own fills explain every real fill here
            nodes = list(level)
            first_of: dict[str, int] = {}
            for i, nd in enumerate(nodes):
                first_of.setdefault(nd.order.user, i)
            held = [first_of[m] for m in makers if m in first_of]
            if not held:
                continue                           # no filled maker has an order we hold here
            cut = max(held)
            for nd in nodes[:cut]:
                if nd.order.user in makers:
                    continue                       # a filled maker's own older order: leave it
                if self.learn_keep_known and nd.order.user != PLACEHOLDER:
                    self.learn_stats[f"queue_kept_known:{coin}"] += 1
                    continue
                kind = "placeholder" if nd.order.user == PLACEHOLDER else "known"
                self.learn_stats[f"queue_{kind}_orders:{coin}"] += 1
                self.learn_stats[f"queue_{kind}_lots:{coin}"] += nd.remaining
                eng._remove(nd.order.oid)

    def _taker_skip(self, p: Place) -> str | None:
        """taker_reject: why this IOC is treated as rejected by the chain (None = apply it).
        min10: notional (limit px x size) under 10 USD, Hyperliquid's minimum order value.
        learn: the user's last IOC that filled in the engine had no real trade (and no real fill since).
        margin / margin_spot: the order grows the user's position and their plan-time free margin (see load_margin)
        is under the grown notional / max leverage; reduce-only orders and users without a snapshot pass."""
        if "min10" in self.taker_reject:
            a = ASSETS[p.asset]
            if Decimal(a.ticks_to_px(p.px)) * Decimal(a.lots_to_sz(p.sz)) < 10:
                return "skip_min10"
        if "learn" in self.taker_reject and p.user in self._taker_blocked:
            return "skip_learn"
        if self._margin and not p.reduce_only:
            st = self._margin.get(p.user.lower())
            if st is None:
                self.taker_stats["margin_unknown"] += 1
            else:
                a = ASSETS[p.asset]
                free, pos = st
                szi = pos.get(a.name, 0.0)
                signed = float(a.lots_to_sz(p.sz)) * (1 if p.side is Side.BUY else -1)
                grow = abs(signed) if szi == 0 or (szi > 0) == (signed > 0) else max(0.0, abs(signed) - abs(szi))
                if grow > 0 and free < grow * float(a.ticks_to_px(p.px)) / MAX_LEVERAGE[a.name]:
                    return "skip_margin"
        return None

    def _taker_learn(self) -> None:
        """taker_reject learn, once per block from that block's real trades (policy-independent): a real fill
        for a user's tx unblocks the user; an engine-filled (or skipped) IOC with no real trade blocks them."""
        for h, (user, _) in sorted(self._blk_ioc.items()):
            if self._real_trades.get(h):
                if user in self._taker_blocked:
                    self.taker_stats["unblock"] += 1
                self._taker_blocked.discard(user)
            elif user not in self._taker_blocked:
                self._taker_blocked.add(user)
                self.taker_stats["block"] += 1
        self._blk_ioc.clear()

    def _learn(self, tx_hash: str) -> None:
        """trade_learn: correct the book from this tx's real trades (see __init__)."""
        trades = self._real_trades.get(tx_hash)
        if not trades:
            return
        eng = self.engine
        groups: dict[tuple[int, Side], list] = {}
        for tr in trades:
            groups.setdefault((tr.asset, tr.taker_side.opposite), []).append(tr)
        for (asset, side), trs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1].value)):
            coin = ASSETS[asset].name
            levels = eng.books[asset].side(side)
            w = min(t.px for t in trs) if side is Side.BUY else max(t.px for t in trs)
            if "sweep" in self._rules:
                better = [px for px in levels.keys() if (px > w if side is Side.BUY else px < w)]
                before = self._seq_at_tx.get(tx_hash) if self.learn_evidence == "tx" else None
                for px in better:
                    for nd in list(levels[px]):
                        if before is not None and nd.order.seq >= before:
                            self.learn_stats[f"swept_skipped_later:{coin}"] += 1
                            continue                    # placed after this tx: the tx says nothing about it
                        if self.learn_keep_known and nd.order.user != PLACEHOLDER:
                            self.learn_stats[f"swept_kept_known:{coin}"] += 1
                            continue
                        kind = "placeholder" if nd.order.user == PLACEHOLDER else "known"
                        self.learn_stats[f"swept_{kind}_orders:{coin}"] += 1
                        self.learn_stats[f"swept_{kind}_lots:{coin}"] += nd.remaining
                        eng._remove(nd.order.oid)
            if "queue" in self._rules:
                self._learn_queue(asset, side, trs, coin)
            if "name" in self._rules or "name-clip" in self._rules:
                level = levels.get(w)
                if level is None:
                    continue
                maker = max((t for t in trs if t.px == w), key=lambda t: t.seq).maker
                nodes = list(level)
                if any(nd.order.user == maker for nd in nodes) or nodes[0].order.user != PLACEHOLDER:
                    continue
                ph, q = nodes[0].order, nodes[0].remaining
                if self._name_clip:                 # name only M's just-filled size; the rest stays anonymous
                    q = min(q, sum(t.sz for t in trs if t.px == w and t.maker == maker))
                if q == nodes[0].remaining:
                    eng._remove(ph.oid)
                else:
                    eng.books[asset].reduce(ph.oid, q)
                order = eng._new_order(maker, asset, side, w, q, Tif.GTC, None)
                eng.books[asset].add(order, q, front=True)
                eng.by_user.setdefault(maker, {})[order.oid] = None
                self.learn_stats[f"named_orders:{coin}"] += 1
                self.learn_stats[f"named_lots:{coin}"] += q
                self._trade_named.setdefault((maker, asset), []).append(order.oid)

    def _cancel_named(self, action) -> None:
        """named_cancel (A4b2): U's cancel found no order; if U has exactly one trade-named piece resting on that
        asset, the cancel was most likely for it (a named piece has no oid / cloid for the cancel to match)."""
        key = (action.user, action.asset)
        book = self.engine.books[action.asset]
        alive = [oid for oid in self._trade_named.get(key, ()) if oid in book]
        self._trade_named[key] = alive
        coin = ASSETS[action.asset].name
        if len(alive) != 1:
            self.learn_stats[f"named_cancel_{'none' if not alive else 'ambiguous'}:{coin}"] += 1
            return
        self.engine._remove(alive[0])
        self._trade_named[key] = []
        self.learn_stats[f"named_cancel_removed:{coin}"] += 1

    def minute(self, time_ms: int) -> int:
        """Minute of window time, 0-based from the first block."""
        return (time_ms - self.t0) // 60_000

    # ------------------------------------------------------------ running
    def _ordered(self, block: Block) -> tuple[list[Action], list[OutsideFill]]:
        book = [a for a in block.actions if not isinstance(a, OutsideFill)]
        outside = [a for a in block.actions if isinstance(a, OutsideFill)]
        out = self.policy(list(book))
        if self.check_permutation and sorted(map(id, out)) != sorted(map(id, book)):
            raise PolicyError(f"block {block.height}: policy did not return a permutation of the block's actions")
        return out, outside

    def _sequence(self, ordered: list[Action], outside: list[OutsideFill]) -> list[Action]:
        """The block's apply order: policy-ordered actions plus outside fills (see other_position)."""
        if self.other_position == "end":
            return [*outside, *ordered] if self.outside_position == "start" else [*ordered, *outside]
        other = [a for a in outside if a.hash != ZERO_HASH]
        zero = [a for a in outside if a.hash == ZERO_HASH]
        if self.other_position == "start":
            body = [*zero, *ordered] if self.outside_position == "start" else [*ordered, *zero]
            return [*other, *body]
        other.sort(key=lambda a: (a.tx_index, a.item_index))
        seq: list[Action] = []
        i = 0
        for a in ordered:
            while i < len(other) and other[i].tx_index < a.tx_index:
                seq.append(other[i])
                i += 1
            seq.append(a)
        seq += other[i:]
        return [*zero, *seq] if self.outside_position == "start" else [*seq, *zero]

    def _gate(self, block: Block, p: Place) -> bool | None:
        """True admit / False reject / None abstain, for an ALO, against the real touch."""
        bid, ask, age = self.touch.get(p.asset, block.time_ms)
        if age is None or (self.gate_max_age_ms is not None and age > self.gate_max_age_ms):
            self.gate_stats["abstain"] += 1
            return None
        crosses = (ask is not None and p.px >= ask) if p.side is Side.BUY else (bid is not None and p.px <= bid)
        return not crosses

    def _apply_gated(self, action: Action) -> tuple[list[Fill], list[Reject]] | None:
        """Apply an ALO (or a modify placing one) under the gate; None = gate not involved."""
        p = action.new if isinstance(action, Modify) else action
        if not (isinstance(p, Place) and p.tif is Tif.ALO):
            return None
        admit = self._gate(self._block, p)
        if admit is None:
            return None
        eng = self.engine
        book = eng.books[p.asset]
        best = book.best_ask() if p.side is Side.BUY else book.best_bid()
        own_would_cross = Engine._crosses(p.side, p.px, best)
        if not admit:
            self.gate_stats["reject"] += 1
            self.gate_stats["reject_engine_would_admit"] += not own_would_cross
            out_f, out_r = [], [Reject(action, RejectReason.ALO_WOULD_CROSS)]
            if isinstance(action, Modify):              # the cancel half still happens
                meta = dict(user=action.user, block=action.block, tx_index=action.tx_index,
                            item_index=action.item_index, hash=action.hash, priority=action.priority,
                            asset=action.asset)
                cancel = (CancelByCloid(**meta, cloid=action.target) if isinstance(action.target, str)
                          else Cancel(**meta, oid=action.target))
                f, r = eng.apply(cancel)
                out_f, out_r = f, [Reject(action, x.reason) for x in r] + out_r
            return out_f, out_r
        self.gate_stats["admit"] += 1
        if own_would_cross:
            self.gate_stats["admit_engine_would_reject"] += 1
            victims = []
            for lv in book.levels(p.side.opposite):
                if not Engine._crosses(p.side, p.px, lv.px):
                    break
                victims += [(nd.order.oid, nd.remaining) for nd in lv]
            for oid, q in victims:
                eng.evict(oid)
                self.gate_stats["evicted_orders"] += 1
                self.gate_stats[f"evicted_lots:{ASSETS[p.asset].name}"] += q
        return eng.apply(action)

    def _reconcile_touch(self) -> None:
        eng = self.engine
        for asset in sorted(ASSETS):
            latest = self.touch.bbo.get(asset)
            if latest is None:
                self.touch_reconciled[(ASSETS[asset].name, "abstain_no_bbo")] += 1
                continue
            _, bid, ask = latest
            bids, asks = [bid[:2]] if bid else [], [ask[:2]] if ask else []
            name = ASSETS[asset].name
            if self.touch_evict == "ledger":
                bids, asks = self._ledger_touch(asset, Side.BUY, bid, name), self._ledger_touch(asset, Side.SELL, ask, name)
            res = eng.reconcile(asset, bids, asks, known="oldest" if self.touch_evict == "ledger" else self.touch_evict)
            if self.touch_refill is not None:
                first = eng.next_oid
                res.update(eng.refill(asset, bids, asks))
                blk = getattr(self, "_block", None)
                t = blk.time_ms if blk is not None else self.t0
                for oid in range(first, eng.next_oid):
                    if oid in eng.books[asset]:
                        self.ph_meta[oid] = (t, 0, eng.books[asset].node(oid).remaining, asset)
                        if self.placeholder_ttl_ms is not None:
                            heapq.heappush(self._ph_heap, (t, oid))
                if self.touch_refill == "back":
                    self._refill_to_back(asset, first)
            for k, v in res.items():
                self.touch_reconciled[(name, k)] += v
            if self.touch_n_cap:
                for side, lvl in ((Side.BUY, bid), (Side.SELL, ask)):
                    if lvl is None:
                        continue
                    level = eng.books[asset].side(side).get(lvl[0])
                    if level is None:
                        continue
                    nodes = list(level)
                    extra = len(nodes) - lvl[2]
                    for nd in nodes:
                        if extra <= 0:
                            break
                        if nd.order.user != PLACEHOLDER:
                            eng.evict(nd.order.oid)
                            self.touch_reconciled[(name, "n_capped")] += 1
                            extra -= 1

    LEDGER_BUDGET = 300_000   # combinations tried per touch level before abstaining
    # The ledger's subset search; replay/adapter.py (backend="rust") swaps in replay_core.unique_subset, which
    # returns exactly the same answers (tests/test_replay_core.py).
    subset_search = staticmethod(unique_subset)
    LEDGER_MAX_ORDERS = 60

    def _ledger_touch(self, asset: int, side: Side, lvl, name: str) -> list:
        """touch_evict="ledger": the bbo gives the touch level's exact size and order count (px, sz, n). If our level
        there holds only known orders, k = (ours - n) > 0 of them must be gone; when exactly one set of k orders sums
        to the size excess, remove that set (and only then). Returns the level to hand to reconcile: the real one for
        better prices (everything better than the touch goes), but never trimming the touch level itself further."""
        if lvl is None:
            return []
        px, sz, n = lvl
        level = self.engine.books[asset].side(side).get(px)
        if level is None:
            return [(px, sz)]
        nodes = list(level)
        st = self.touch_reconciled
        if any(nd.order.user == PLACEHOLDER for nd in nodes):
            st[(name, "ledger_skip_placeholder")] += 1
            return [(px, max(sz, level.total))]
        k, excess = len(nodes) - n, level.total - sz
        if k <= 0 or excess <= 0:
            st[(name, "ledger_nothing" if k == 0 and excess == 0 else "ledger_short")] += 1
            return [(px, max(sz, level.total))]
        if k > self.ledger_k or len(nodes) > self.LEDGER_MAX_ORDERS:
            st[(name, "ledger_too_many")] += 1
            return [(px, max(sz, level.total))]
        got = self.subset_search([nd.remaining for nd in nodes], k, excess, self.LEDGER_BUDGET)
        if not isinstance(got, tuple):
            st[(name, f"ledger_{got}")] += 1
            return [(px, max(sz, level.total))]
        sols = [got]
        for i in sols[0]:
            self.engine.evict(nodes[i].order.oid)
        st[(name, "ledger_evicted")] += k
        return [(px, sz)]

    def _apply(self, block: Block, action: Action) -> tuple[list[Fill], list[Reject]]:
        self.observer.before_action(self, block, action)
        if self._hist_rej and isinstance(action, Place) and hist_rejected(self._hist_rej, action, block.time_ms):
            self.taker_stats[f"skip_hist_{action.tif.name.lower()}"] += 1
            if action.tif is Tif.IOC:
                self._blk_ioc[action.hash] = (action.user, True)
            return [], []
        if self.taker_reject and isinstance(action, Place) and action.tif is Tif.IOC:
            skip = self._taker_skip(action)
            if skip:
                self.taker_stats[skip] += 1
                self._blk_ioc[action.hash] = (action.user, True)
                return [], []
        before = self.engine.next_oid
        self._block = block
        self.engine.block_time_ms = block.time_ms
        deep = self._deep_in(action) if self.deep_book else None
        gated = self._apply_gated(action) if self.touch is not None else None
        fills, rejects = gated if gated is not None else self.engine.apply(action)
        if deep:
            self._deep_out(deep)
        if (self.named_cancel and isinstance(action, (Cancel, CancelByCloid))
                and any(r.reason is RejectReason.UNKNOWN_ORDER for r in rejects)):
            self._cancel_named(action)
        created = before if self.engine.next_oid > before and isinstance(action, (Place, Modify)) else None
        if created is not None:
            self.placed[created] = action.new if isinstance(action, Modify) else action
            self.placed_by[created] = action
            if self.resolver is not None:
                self.resolver.note(action, created)
            if self.placeholder_queue == "behind":
                self._placeholders_behind(created, action.asset)
        if self.taker_reject and isinstance(action, Place) and action.tif is Tif.IOC and fills:
            self._blk_ioc[action.hash] = (action.user, True)
        if isinstance(action, OutsideFill):
            self.outside_fill_log.append((block.height, action.tid, action.sz - sum(f.sz for f in fills)))
        self.observer.after_action(self, block, action, fills, rejects, created)
        return fills, rejects

    def __iter__(self) -> Iterator[tuple[int, list[Fill], list[Reject], bytes | None]]:
        self._begin_blocks()
        while self._next_bi < len(self.blocks):
            yield self.step_block()

    def _begin_blocks(self) -> None:
        """Set up block-by-block replay (replay/adapter.py drives step_block directly)."""
        self._state_hash: bytes | None = None
        self._next_bi = 0
        times = [b.time_ms for b in self.blocks]
        at_block: dict[int, list] = {}               # block index -> snapshots applied after it
        for s in self.snapshots:                    # rule 8: the last block with time <= s.time
            bi = bisect.bisect_right(times, s.time_ms) - 1
            if bi < 0:
                continue                            # before the first block: the seed covers it
            bi += self.snapshot_shift
            if not 0 <= bi < len(self.blocks):
                self.shift_dropped += 1
                continue
            at_block.setdefault(bi, []).append(s)
        self._at_block = at_block

    def step_block(self) -> tuple[int, list[Fill], list[Reject], bytes | None]:
        """Replay the next block: (height, fills, rejects, state_hash). Same body as the former __iter__ loop."""
        eng, at_block, bi = self.engine, self._at_block, self._next_bi
        b = self.blocks[bi]
        self.block_index = bi
        if self.touch is not None:
            self.touch.advance(b.time_ms)
        if self.touch_reconcile:
            self._reconcile_touch()
        if self.placeholder_ttl_ms is not None:
            self._expire_placeholders(b.time_ms)
        ordered, outside = self._ordered(b)
        fills: list[Fill] = []
        rejects: list[Reject] = []
        seq = self._sequence(ordered, outside)
        self._fills_by_hash = {}
        self._seq_at_tx = {}
        for i, a in enumerate(seq):
            self._seq_at_tx.setdefault(a.hash, eng.next_seq)
            f, r = self._apply(b, a)
            fills += f
            rejects += r
            for fl in f:
                self._fills_by_hash.setdefault(fl.taker_hash, []).append(fl)
            if (self._rules and self.trade_learn_at == "tx" and not isinstance(a, OutsideFill)
                    and (i + 1 == len(seq) or seq[i + 1].hash != a.hash)):
                self._learn(a.hash)
        if self._rules and self.trade_learn_at == "block-end":
            done: set[str] = set()
            for a in sorted((x for x in ordered), key=lambda x: (x.tx_index, x.item_index)):
                if a.hash not in done:
                    done.add(a.hash)
                    self._learn(a.hash)
        if "learn" in self.taker_reject:
            self._taker_learn()
        else:
            self._blk_ioc.clear()
        eng.end_block(b.height, b.time_ms)
        minute = self.minute(b.time_ms)
        self.observer.after_block(self, b, minute)
        for s in at_block.get(bi, ()):
            if self.reseed:
                self.observer.before_reseed(self, b, s)
            if self.reseed and self.keep_known:
                res = eng.reconcile(s.asset, s.bids, s.asks, known="oldest")
                first = eng.next_oid
                res.update(eng.refill(s.asset, s.bids, s.asks, counts=(s.bid_n, s.ask_n))
                           if self.placeholder_split else eng.refill(s.asset, s.bids, s.asks), refill=1)
                self._note_refill(s, first)
                if self.refill_position == "back":
                    self._refill_to_back(s.asset, first)
            elif self.reseed:
                res = eng.reconcile(s.asset, s.bids, s.asks)     # measure drift, then rebuild
                res.update(eng.clear(s.asset), reseed=1)
                self._seed_levels(s.asset, s.bids, s.asks, s.time_ms,
                                  self._cap_sizes(s) if self.placeholder_cap == "next" else None)
            else:
                res = eng.reconcile(s.asset, s.bids, s.asks, known=self.reconcile_known)
            for k, v in res.items():
                self.reconciled[(ASSETS[s.asset].name, k)] += v
            if self.deep_book:
                self._build_deep(s)
            self.observer.after_reconcile(self, b, s, res, minute)
        if self.hasher is not None:
            self._state_hash = self.hasher(eng, self._state_hash, b.height)
        self._next_bi = bi + 1
        return b.height, fills, rejects, self._state_hash


def run(window_dir, policy: Policy, *, hl_data, **kw) -> Iterator[tuple[int, list[Fill], list[Reject], bytes | None]]:
    """AGENT_PLAN 5.2 contract: yields (height, fills, rejects, state_hash) per block."""
    return iter(Replay(window_dir, hl_data, policy, **kw))
