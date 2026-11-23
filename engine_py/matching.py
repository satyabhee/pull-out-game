"""The engine: applies actions to the BTC/ETH books and emits fills and rejects.

Rules (specs/01-book.md, AGENT_PLAN.md sections 3-4):
- Price-time priority; every fill is at the resting maker's price; the best level is
  re-read every iteration.
- GTC: match, rest the remainder. IOC: match, cancel the remainder (no fill is not an
  error). ALO: rejected whole if it would cross (equal price included), else rests.
- Self-trade: `stp="cancel_taker"` (Satya's spec: stop, keep the maker, cancel the
  taker's remainder) or `stp="expire_maker"` (cancel the maker, keep matching), until the
  data shows which one Hyperliquid uses.
- Cancels resolve by (user, asset, cloid) exactly; if two resting orders share the
  cloid, the target is unresolved (specs/01-book.md: the engine never guesses). Int-oid cancels go through
  `resolve_oid(user, asset, hl_oid) -> engine oid | None`; without one they are rejected
  as UNKNOWN_ORDER.
- Modify = cancel the target, then place `new` as a new order (new id, back of the queue).
  If the target isn't found, the new order is still placed: a modify whose target was
  gone carries an error on chain and never reaches the engine.
- scheduleCancel: at end_block, users whose time has come lose all their orders.
- OutsideFill: forced fill against the named maker's orders at that price (time order),
  then placeholders at that price; any rest is rejected as NO_MAKER.
- A crossed book after any action raises: never repaired.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable

from engine_py.book import Book, BookError
from engine_py.types import (
    ASSETS, Action, Cancel, CancelByCloid, Fill, Modify, Order, OutsideFill, Place,
    Reject, RejectReason, ScheduleCancel, Side, Tif,
)

PLACEHOLDER = "placeholder"   # owner of seeded pre-window liquidity


def split_equal(total: int, k: int) -> list[int]:
    """total lots in k positive integer parts, as equal as possible, larger parts first (deterministic)."""
    if not 1 <= k <= total:
        raise ValueError(f"cannot split {total} lots into {k} orders")
    q, r = divmod(total, k)
    return [q + 1] * r + [q] * (k - r)

Resolver = Callable[[str, int, int], "int | None"]


class CrossedBookError(BookError):
    pass


DOCS_T_MS = 400        # priority-fees doc: ALO orders within T = 400 ms sort by priority


class Engine:
    def __init__(self, stp: str = "cancel_taker", resolve_oid: Resolver | None = None,
                 check_every_action: bool = False, p_priority: str | int | None = None,
                 evict_arrival: bool = False):
        """p_priority (G1, measured by analysis/p_priority.py): at one price a resting order with a higher
        grouping.p is served first. None = pure price-time (default). "always" = queue by (p desc, arrival).
        int W = a new order goes ahead of a lower-p order only if that order arrived at most W ms earlier
        (block time; set block_time_ms before applying a block). Orders without p (placeholders, named
        pre-window orders, actions without grouping) count as p = 0 and, under W, are never jumped.
        "known" (Nov 20 overnight) = p order only among orders placed in the window through this engine (an
        in-window order without grouping.p counts as p = 0 and can be jumped); placeholders and orders named
        from outside evidence (pre-window liquidity whose p we never saw) are never jumped.
        "knownpos" (Nov 21) = "known", except that an order without a priority bid (p = 0) is never jumped: p only
        reorders orders that both carry a bid (dump: in 44 of 82 wrong-maker misses the chain's maker had p = 0).
        "docs" (Nov 22) = Hyperliquid priority-fees doc: a new ALO order goes ahead of each in-window ALO order
        (placed through this engine less than DOCS_T_MS = 400 ms earlier, block time) with a lower p, p = 0
        included; older orders have locked in their place. Placeholders / outside-evidence orders are never jumped.
        "docsall" = "docs" for every resting tif, not only ALO.
        evict_arrival (off by default): reconcile(known="oldest"|"newest") picks known orders by arrival
        (Order.seq) instead of queue position. Under p_priority the queue head is the highest-p order, not
        the oldest one, so queue order would trim the freshest high-p orders first."""
        self.docs_t_ms = DOCS_T_MS
        if isinstance(p_priority, str) and p_priority.startswith(("docs:", "docsall:")):
            # I01 (tournament, Nov 23): "docs:<ms>" / "docsall:<ms>" = the docs rule with window T = <ms>
            p_priority, t = p_priority.split(":")
            self.docs_t_ms = int(t)
            if self.docs_t_ms < 0:
                raise ValueError(f"docs window must be >= 0 ms, not {t}")
        if not (p_priority is None or p_priority in ("always", "known", "knownpos", "docs", "docsall")
                or (isinstance(p_priority, int) and p_priority >= 0)):
            raise ValueError(f"p_priority must be None, 'always', 'known', 'knownpos', 'docs', 'docsall' or ms >= 0, "
                             f"not {p_priority!r}")
        self.evict_arrival = evict_arrival
        self.p_priority = p_priority
        self.p_meta: dict[int, tuple[int, int]] = {}         # engine oid -> (p, block time ms)
        self.block_time_ms = 0
        if stp not in ("cancel_taker", "expire_maker"):
            raise ValueError(f"unknown self-trade policy {stp!r}")
        self.stp = stp
        self.resolve_oid = resolve_oid
        self.check_every_action = check_every_action
        self.books = {a: Book(a) for a in sorted(ASSETS)}
        self.next_oid = 1
        self.next_seq = 1
        self.next_fill_seq = 1
        self.by_cloid: dict[tuple[str, int, str], list[int]] = {}   # resting oids, oldest first
        self.by_user: dict[str, dict[int, None]] = {}        # insertion-ordered set
        self.schedules: dict[str, int] = {}
        self.block = 0
        self.stats: Counter = Counter()

    # ------------------------------------------------------------ public
    def seed(self, asset: int, side: Side, px: int, sz: int) -> int:
        """Pre-window liquidity: one anonymous order, behind anything already seeded."""
        order = self._new_order(PLACEHOLDER, asset, side, px, sz, Tif.GTC, None)
        self.books[asset].add(order, sz)
        self._assert_not_crossed(asset)
        return order.oid

    def apply(self, action: Action) -> tuple[list[Fill], list[Reject]]:
        self.block = action.block
        if isinstance(action, Place):
            out = self._place(action, action)
        elif isinstance(action, Cancel):
            out = [], self._cancel_hl_oid(action, action.user, action.asset, action.oid)
        elif isinstance(action, CancelByCloid):
            out = [], self._cancel_cloid(action, action.user, action.asset, action.cloid)
        elif isinstance(action, Modify):
            if isinstance(action.target, str):
                rejects = self._cancel_cloid(action, action.user, action.asset, action.target)
            else:
                rejects = self._cancel_hl_oid(action, action.user, action.asset, action.target)
            fills, more = self._place(action.new, action)
            out = fills, rejects + more
        elif isinstance(action, ScheduleCancel):
            if action.at_ms is None:
                self.schedules.pop(action.user, None)
            else:
                self.schedules[action.user] = action.at_ms
            out = [], []
        elif isinstance(action, OutsideFill):
            out = self._outside_fill(action)
        else:
            raise TypeError(f"unknown action {type(action).__name__}")

        if self.check_every_action:
            for book in self.books.values():
                book.check_invariants()
        elif hasattr(action, "asset"):
            self._assert_not_crossed(action.asset)
        return out

    def reconcile(self, asset: int, bids, asks, known: str | None = None) -> dict[str, int]:
        """ENGINE_WINDOW_RULES rule 8: trim placeholders toward an L2 snapshot.

        `bids`/`asks` are (px, sz) levels from the snapshot. Inside the snapshot's
        price range, where we hold more than the snapshot shows, placeholders at that
        price are reduced newest first. The gap left is returned as drift, and
        snapshot size we lack is returned as missing.

        known=None (rule 8 as written): known orders are never touched.
        known="oldest" | "newest" (rule 8 extension, approved Nov 17 as G1 fix #2): the
        drift is then removed from known orders at that price in that queue order
        (`evicted` lots, `evicted_orders` orders). Phantom orders (accepted by us,
        silently rejected on chain) are never cancelled by their owners, so they age;
        "oldest" targets them first.
        """
        if known not in (None, "oldest", "newest"):
            raise ValueError(f"unknown eviction order {known!r}")
        book = self.books[asset]
        out = {"trimmed": 0, "drift": 0, "missing": 0, "evicted": 0, "evicted_orders": 0}
        for side, levels in ((Side.BUY, bids), (Side.SELL, asks)):
            if not levels:
                continue
            snap = dict(levels)
            edge = min(snap) if side is Side.BUY else max(snap)
            ours = book.side(side)
            in_range = [px for px in ours.keys() if (px >= edge if side is Side.BUY else px <= edge)]
            for px in in_range:
                level = ours[px]
                gap = level.total - snap.get(px, 0)
                if gap <= 0:
                    continue
                for node in reversed([n for n in level if n.order.user == PLACEHOLDER]):
                    if gap == 0:
                        break
                    q = min(gap, node.remaining)
                    book.reduce(node.order.oid, q)
                    out["trimmed"] += q
                    gap -= q
                out["drift"] += gap
                if known is None or gap == 0:
                    continue
                nodes = [n for n in level if n.order.user != PLACEHOLDER]
                if self.evict_arrival:
                    nodes.sort(key=lambda n: n.order.seq)
                for node in (nodes if known == "oldest" else reversed(nodes)):
                    if gap == 0:
                        break
                    q = min(gap, node.remaining)
                    if q == node.remaining:
                        self._remove(node.order.oid)
                        out["evicted_orders"] += 1
                    else:
                        book.reduce(node.order.oid, q)
                    out["evicted"] += q
                    gap -= q
            for px, sz in snap.items():
                level = ours.get(px)
                have = level.total if level is not None else 0
                if sz > have:
                    out["missing"] += sz - have
        for k, v in out.items():
            self.stats[f"reconcile_{k}"] += v
        return out

    def evict(self, oid: int) -> Order:
        """Remove a resting order on outside evidence (G1 fix #1: an ALO the real touch admits
        proves nothing real rested where it crosses). Counted in stats."""
        order = self._remove(oid)
        self.stats["evicted_by_gate"] += 1
        return order

    def refill(self, asset: int, bids, asks, counts=None) -> dict[str, int]:
        """Lead-in re-seed (keep known orders): wherever a snapshot level holds more than we do, add the
        difference as one placeholder at the FRONT of that level. That liquidity rested before anything
        we saw placed, so it queues ahead. Never trims; run reconcile(known=...) first so nothing better
        than the snapshot's best remains (otherwise the crossed-book check fails loudly)."""
        book = self.books[asset]
        out = {"refilled": 0, "refilled_orders": 0}
        for side, levels, ns in ((Side.BUY, bids, counts[0] if counts else ()),
                                 (Side.SELL, asks, counts[1] if counts else ())):
            ours = book.side(side)
            for i, (px, sz) in enumerate(levels):
                level = ours.get(px)
                gap = sz - (level.total if level is not None else 0)
                if gap > 0:
                    # counts given (A4a, --placeholder-split): the gap is the snapshot's orders we do not hold, so it
                    # becomes (snapshot n - orders we hold there) placeholders of equal size, not one
                    held = len(level) if level is not None else 0
                    k = max(1, min(gap, ns[i] - held)) if i < len(ns) else 1
                    for q in reversed(split_equal(gap, k)):          # added at the front: last piece first
                        order = self._new_order(PLACEHOLDER, asset, side, px, q, Tif.GTC, None)
                        book.add(order, q, front=True)
                        out["refilled_orders"] += 1
                    out["refilled"] += gap
        self._assert_not_crossed(asset)
        for k, v in out.items():
            self.stats[f"refill_{k}"] += v
        return out

    def clear(self, asset: int) -> dict[str, int]:
        """Remove every resting order on one asset (G1 #3, short-horizon re-seed: the book is
        rebuilt from a snapshot). Best price first, queue order inside a level: deterministic."""
        book = self.books[asset]
        oids = [nd.order.oid for side in (Side.BUY, Side.SELL) for lv in book.levels(side) for nd in lv]
        out = {"cleared_known": 0, "cleared_placeholder": 0}
        for oid in oids:
            order = self._remove(oid)
            out["cleared_placeholder" if order.user == PLACEHOLDER else "cleared_known"] += 1
        return out

    def end_block(self, height: int, time_ms: int) -> None:
        """Fire due scheduleCancels (deterministic order), then check every book."""
        due = sorted((at, user) for user, at in self.schedules.items() if at <= time_ms)
        for _, user in due:
            del self.schedules[user]
            for oid in sorted(self.by_user.get(user, {})):
                self._remove(oid)
                self.stats["schedule_cancelled"] += 1
        for asset in self.books:
            self._assert_not_crossed(asset)

    # ------------------------------------------------------------ internals
    def _new_order(self, user, asset, side, px, sz, tif, cloid) -> Order:
        order = Order(oid=self.next_oid, user=user, asset=asset, side=side, px=px, sz=sz,
                      tif=tif, cloid=cloid, seq=self.next_seq, block=self.block)
        self.next_oid += 1
        self.next_seq += 1
        return order

    def _p_arg(self, p: Place) -> tuple:
        """Extra _rest argument only when p_priority is on (subclasses overriding _rest stay compatible)."""
        return (p.priority or 0,) if self.p_priority is not None else ()   # None (no grouping.p) -> 0

    def _rest(self, order: Order, remaining: int, p: int | None = None) -> None:
        jumps = None
        if self.p_priority is not None and p:
            t, window, meta, docs_t = self.block_time_ms, self.p_priority, self.p_meta, self.docs_t_ms

            def jumps(node, p=p, t=t, window=window, meta=meta, docs_t=docs_t):
                m = meta.get(node.order.oid)
                if window == "always":
                    return p > (m[0] if m else 0)
                if window == "known":
                    return m is not None and p > m[0]
                if window == "knownpos":
                    return m is not None and 0 < m[0] < p
                if window in ("docs", "docsall"):
                    return (m is not None and p > m[0] and t - m[1] < docs_t
                            and (window == "docsall" or (order.tif is Tif.ALO and node.order.tif is Tif.ALO)))
                return m is not None and m[0] > 0 and p > m[0] and t - m[1] <= window
        if self.p_priority is not None and order.user != PLACEHOLDER and p is not None and (p or self.p_priority in ("known", "knownpos", "docs", "docsall")):
            self.p_meta[order.oid] = (p, self.block_time_ms)     # p None = rested from outside evidence
        self.books[order.asset].add(order, remaining, jumps=jumps)
        if order.user != PLACEHOLDER:
            self.by_user.setdefault(order.user, {})[order.oid] = None
            if order.cloid is not None:
                key = (order.user, order.asset, order.cloid)
                if key in self.by_cloid:
                    self.stats["cloid_reused_while_resting"] += 1
                self.by_cloid.setdefault(key, []).append(order.oid)

    def _forget(self, order: Order) -> None:
        if order.user == PLACEHOLDER:
            return
        self.p_meta.pop(order.oid, None)
        mine = self.by_user.get(order.user)
        if mine is not None:
            mine.pop(order.oid, None)
            if not mine:
                del self.by_user[order.user]
        if order.cloid is not None:
            key = (order.user, order.asset, order.cloid)
            oids = self.by_cloid.get(key)
            if oids is not None and order.oid in oids:
                oids.remove(order.oid)
                if not oids:
                    del self.by_cloid[key]

    def _remove(self, oid: int) -> Order:
        order = self._book_of(oid).remove(oid).order
        self._forget(order)
        return order

    def _book_of(self, oid: int) -> Book:
        for book in self.books.values():
            if oid in book:
                return book
        raise BookError(f"oid {oid} is not resting")

    @staticmethod
    def _crosses(side: Side, px: int, best_opposite: int | None) -> bool:
        if best_opposite is None:
            return False
        return px >= best_opposite if side is Side.BUY else px <= best_opposite

    def _fill(self, maker, taker_order, taker_user, taker_side, q, block, taker_hash) -> Fill:
        f = Fill(maker_order=maker.oid, taker_order=taker_order, maker_user=maker.user,
                 taker_user=taker_user, taker_side=taker_side, px=maker.px, sz=q,
                 block=block, seq=self.next_fill_seq, taker_hash=taker_hash)
        self.next_fill_seq += 1
        return f

    def _place(self, p: Place, source: Action) -> tuple[list[Fill], list[Reject]]:
        book = self.books[p.asset]
        opposite = p.side.opposite
        best_opp = book.best_ask() if p.side is Side.BUY else book.best_bid()

        if p.tif is Tif.ALO:
            if self._crosses(p.side, p.px, best_opp):
                return [], [Reject(source, RejectReason.ALO_WOULD_CROSS)]
            self._rest(self._new_order(p.user, p.asset, p.side, p.px, p.sz, p.tif, p.cloid), p.sz, *self._p_arg(p))
            return [], []

        taker = self._new_order(p.user, p.asset, p.side, p.px, p.sz, p.tif, p.cloid)
        remaining = p.sz
        fills: list[Fill] = []
        rejects: list[Reject] = []
        while remaining > 0:
            level = book.best_level(opposite)              # re-read every iteration
            if level is None or not self._crosses(p.side, p.px, level.px):
                break
            maker_node = level.first()
            maker = maker_node.order
            if maker.user == p.user:
                if self.stp == "cancel_taker":
                    rejects.append(Reject(source, RejectReason.SELF_TRADE))
                    remaining = 0                          # cancel the taker's remainder
                    break
                self._remove(maker.oid)                    # expire_maker
                self.stats["self_trade_expired_maker"] += 1
                continue
            q = min(remaining, maker_node.remaining)
            fills.append(self._fill(maker, taker.oid, p.user, p.side, q, p.block, p.hash))
            remaining -= q
            if book.reduce(maker.oid, q).remaining == 0:
                self._forget(maker)

        if remaining > 0 and p.tif is Tif.GTC:
            self._rest(taker, remaining, *self._p_arg(p))
        return fills, rejects

    def _cancel_cloid(self, source, user, asset, cloid) -> list[Reject]:
        oids = self.by_cloid.get((user, asset, cloid), [])
        if len(oids) != 1:                     # none, or ambiguous: never guess
            if oids:
                self.stats["cloid_ambiguous"] += 1
            return [Reject(source, RejectReason.UNKNOWN_ORDER)]
        oid = oids[0]
        self._remove(oid)
        return []

    def _cancel_hl_oid(self, source, user, asset, hl_oid) -> list[Reject]:
        oid = self.resolve_oid(user, asset, hl_oid) if self.resolve_oid else None
        if oid is None or oid not in self.books[asset]:
            return [Reject(source, RejectReason.UNKNOWN_ORDER)]
        order = self.books[asset].node(oid).order
        if order.user != user:
            raise BookError(f"resolver mapped {user}'s oid {hl_oid} to {order.user}'s order")
        self._remove(oid)
        return []

    def _outside_fill(self, of: OutsideFill) -> tuple[list[Fill], list[Reject]]:
        book = self.books[of.asset]
        level = book.side(of.taker_side.opposite).get(of.px)
        fills: list[Fill] = []
        remaining = of.sz
        if level is not None:
            named = [n for n in level if n.order.user == of.maker_user]
            anon = [n for n in level if n.order.user == PLACEHOLDER]
            for node in named + anon:
                if remaining == 0:
                    break
                maker = node.order
                q = min(remaining, node.remaining)
                fills.append(self._fill(maker, None, of.user, of.taker_side, q, of.block, of.hash))
                remaining -= q
                if book.reduce(maker.oid, q).remaining == 0:
                    self._forget(maker)
        if remaining > 0:
            self.stats["outside_fill_lots_unmatched"] += remaining
            return fills, [Reject(of, RejectReason.NO_MAKER)]
        return fills, []

    def _assert_not_crossed(self, asset: int) -> None:
        book = self.books[asset]
        if book.is_crossed():
            raise CrossedBookError(
                f"asset {asset} crossed at block {self.block}: bid {book.best_bid()} >= ask {book.best_ask()}")
