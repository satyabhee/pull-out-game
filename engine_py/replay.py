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

import json
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

from data.oids import OidEstimator, OidResolver
from data.parse import COIN_TO_ASSET, Stats, _hour_files, _records, iter_books, iter_trades, load_window, nearest_book
from engine_py.matching import PLACEHOLDER, Engine
from engine_py.types import (ASSETS, Action, Block, Cancel, CancelByCloid, Fill, Modify, OutsideFill, Place,
                             Reject, RejectReason, Side, Tif)

COINS = tuple(ASSETS[a].name for a in sorted(ASSETS))       # ("BTC", "ETH")
OUTSIDE_FILL_POSITION = "end of block, after every policy-ordered action, in tid order"

Policy = Callable[[list[Action]], list[Action]]
Hasher = Callable[[Engine, "bytes | None", int], bytes]


def listing(actions: list[Action]) -> list[Action]:
    """The block's listing order, unchanged (a policy; which one is "actual" is decided at G1)."""
    return list(actions)


class PolicyError(ValueError):
    pass


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
        ev += [(t.time_ms, self.TRADE, t.asset, t.tid, t.taker_side, t.px) for t in trades]
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


class Replay:
    def __init__(self, window_dir, hl_data, policy: Policy, *, anchors=None, use_resolver: bool = True,
                 nearest_ratio: int = 0, check_permutation: bool = True, hasher: Hasher | None = None,
                 observer: Observer | None = None, engine: Engine | None = None,
                 alo_gate: str | None = None, gate_max_age_ms: int | None = None,
                 reconcile_known: str | None = None, reseed: bool = False,
                 touch_reconcile: bool = False, touch_n_cap: bool = False,
                 keep_known: bool = False, skip_lead_in: bool = False):
        """anchors: path to an anchors file; None = the window's anchors.json if present.
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
        Lead-in chunks (window.json lead_in_blocks > 0, collect/capture.py LEAD_IN_BLOCKS): blocks before
        warmup_end_height are replayed but never scored, also with reseed. skip_lead_in=True drops them
        (exactly what a chunk without lead-in would replay). keep_known=True (needs reseed): at every
        snapshot, instead of clearing, rule 8 with oldest-first eviction and then Engine.refill: the
        snapshot size we lack goes in as placeholders at the FRONT of each level, and known orders
        carry over (scoring treats them as known, not as pre-re-seed wildcards)."""
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
        blocks, self.parse_stats = load_window(self.window_dir, self.hl_data)   # decoded once per process
        self.lead_in = int(self.window_meta.get("lead_in_blocks", 0) or 0)
        self.skip_lead_in = skip_lead_in
        if skip_lead_in and self.lead_in:
            blocks = [b for b in blocks if b.height >= self.window_meta["warmup_end_height"]]
            if not blocks:
                raise ValueError(f"{self.window_dir}: nothing after the lead-in")
        self.blocks: list[Block] = list(blocks)
        self.t0, self.t1 = self.blocks[0].time_ms, self.blocks[-1].time_ms
        self.warmup_end_height: int = max(self.window_meta.get("warmup_end_height", self.blocks[0].height),
                                          self.blocks[0].height)

        self.placed: dict[int, Place] = {}         # engine oid -> the order placed (a modify's `new`)
        self.placed_by: dict[int, Action] = {}     # engine oid -> the Place or Modify action itself
        self.block_index = -1                      # index of the block being applied
        self.seed_offsets: dict[str, int] = {}     # coin -> seed snapshot time - first block time (ms)
        self._seed()

        self.resolver: OidResolver | None = None
        if anchors is None and (self.window_dir / "anchors.json").exists():
            anchors = self.window_dir / "anchors.json"
        if use_resolver and anchors is not None:
            est = OidEstimator.from_files(self.window_dir, anchors)
            self.resolver = OidResolver(self.engine, est, nearest_ratio=nearest_ratio)
            self.engine.resolve_oid = self.resolver

        self.snapshots = sorted((s for coin in COINS for s in iter_books(self.hl_data, coin, self.t0, self.t1)),
                                key=lambda s: (s.time_ms, s.asset))
        if alo_gate not in (None, "touch"):
            raise ValueError(f"unknown alo_gate {alo_gate!r}")
        self.alo_gate, self.gate_max_age_ms, self.reconcile_known = alo_gate, gate_max_age_ms, reconcile_known
        self.reseed, self.keep_known = reseed, keep_known
        if reseed and not self.lead_in:
            self.warmup_end_height = self.blocks[0].height     # every horizon starts from a snapshot
        self.touch: RealTouch | None = None
        self.touch_reconcile, self.touch_n_cap = touch_reconcile, touch_n_cap
        self.touch_reconciled: Counter = Counter()  # (coin, trimmed|drift|evicted|...|n_capped) -> lots/orders
        if alo_gate == "touch" or touch_reconcile:
            lo = self.t0 - TOUCH_LOOKBACK_MS
            self.touch = RealTouch(
                [s for coin in COINS for s in iter_books(self.hl_data, coin, lo, self.t1)],
                iter_trades(self.hl_data, COINS, lo, self.t1),
                [x for coin in COINS for x in iter_bbo(self.hl_data, coin, lo, self.t1)])
        self.gate_stats: Counter = Counter()
        self.reconciled: Counter = Counter()        # (coin, trimmed|drift|missing|evicted...) -> lots
        self.outside_fill_log: list[tuple[int, int, int]] = []   # (height, tid, lots unmatched)

    # ------------------------------------------------------------ setup
    def _seed(self) -> None:
        for coin in COINS:
            snap = nearest_book(self.hl_data, coin, self.t0)
            for px, sz in snap.bids:
                self.engine.seed(snap.asset, Side.BUY, px, sz)
            for px, sz in snap.asks:
                self.engine.seed(snap.asset, Side.SELL, px, sz)
            self.seed_offsets[coin] = snap.time_ms - self.t0

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
            res = eng.reconcile(asset, [bid[:2]] if bid else [], [ask[:2]] if ask else [], known="oldest")
            name = ASSETS[asset].name
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

    def _apply(self, block: Block, action: Action) -> tuple[list[Fill], list[Reject]]:
        self.observer.before_action(self, block, action)
        before = self.engine.next_oid
        self._block = block
        gated = self._apply_gated(action) if self.touch is not None else None
        fills, rejects = gated if gated is not None else self.engine.apply(action)
        created = before if self.engine.next_oid > before and isinstance(action, (Place, Modify)) else None
        if created is not None:
            self.placed[created] = action.new if isinstance(action, Modify) else action
            self.placed_by[created] = action
            if self.resolver is not None:
                self.resolver.note(action, created)
        if isinstance(action, OutsideFill):
            self.outside_fill_log.append((block.height, action.tid, action.sz - sum(f.sz for f in fills)))
        self.observer.after_action(self, block, action, fills, rejects, created)
        return fills, rejects

    def __iter__(self) -> Iterator[tuple[int, list[Fill], list[Reject], bytes | None]]:
        eng = self.engine
        state_hash: bytes | None = None
        si = 0
        snaps = self.snapshots
        for bi, b in enumerate(self.blocks):
            self.block_index = bi
            if self.touch is not None:
                self.touch.advance(b.time_ms)
            if self.touch_reconcile:
                self._reconcile_touch()
            ordered, outside = self._ordered(b)
            fills: list[Fill] = []
            rejects: list[Reject] = []
            for a in [*ordered, *outside]:
                f, r = self._apply(b, a)
                fills += f
                rejects += r
            eng.end_block(b.height, b.time_ms)
            minute = self.minute(b.time_ms)
            self.observer.after_block(self, b, minute)
            nxt = self.blocks[bi + 1].time_ms if bi + 1 < len(self.blocks) else self.t1 + 1
            while si < len(snaps) and snaps[si].time_ms < nxt:
                s = snaps[si]
                if s.time_ms >= b.time_ms:
                    if self.reseed and self.keep_known:
                        res = eng.reconcile(s.asset, s.bids, s.asks, known="oldest")
                        res.update(eng.refill(s.asset, s.bids, s.asks), refill=1)
                    elif self.reseed:
                        res = eng.reconcile(s.asset, s.bids, s.asks)     # measure drift, then rebuild
                        res.update(eng.clear(s.asset), reseed=1)
                        for px, sz in s.bids:
                            eng.seed(s.asset, Side.BUY, px, sz)
                        for px, sz in s.asks:
                            eng.seed(s.asset, Side.SELL, px, sz)
                    else:
                        res = eng.reconcile(s.asset, s.bids, s.asks, known=self.reconcile_known)
                    for k, v in res.items():
                        self.reconciled[(ASSETS[s.asset].name, k)] += v
                    self.observer.after_reconcile(self, b, s, res, minute)
                si += 1
            if self.hasher is not None:
                state_hash = self.hasher(eng, state_hash, b.height)
            yield b.height, fills, rejects, state_hash


def run(window_dir, policy: Policy, *, hl_data, **kw) -> Iterator[tuple[int, list[Fill], list[Reject], bytes | None]]:
    """AGENT_PLAN 5.2 contract: yields (height, fills, rejects, state_hash) per block."""
    return iter(Replay(window_dir, hl_data, policy, **kw))
