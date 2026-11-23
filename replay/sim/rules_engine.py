"""M1 · Transfigure: the simulated chain. An INDEPENDENT implementation of the chain rules (MAHITO_PLAN §1,
docs/problem_page.md), written from the rules and not from engine_py, so the two can catch each other's bugs.

Chain rules implemented:
- Per consensus batch, txs run in three classes: actions with no GTC/IOC order, then cancels, then actions with a
  GTC/IOC order; proposer order inside a class. A block is a list of batches.
- Every placed order item takes the next oid from one global counter (rejected items too: historicalOrders lists
  rejected orders with oids). Invisible events can burn oids (`burn_oids`).
- Order checks, in this order: $10 minimum notional (`minTradeNtlRejected`), reduce-only that cannot reduce
  (`reduceOnlyRejected`), open-order cap for orders that can rest (`tooManyOpenOrdersRejected`; Hyperliquid's base
  cap is 1,000), margin (`perpMarginRejected`), then post-only that would cross (`badAloPxRejected`).
- Post-only priority: at one price, orders placed in the last 400 ms are sorted by priority bid p (higher first,
  ties by arrival); an order older than 400 ms keeps its place ahead of later, higher bids.
- Takers (IOC, GTC) match best price first, oldest first within a price, at the maker's price. Self-trade: the
  RESTING order is canceled (`selfTradeCanceled`) and matching continues. IOC remainder is dropped (no fill at all:
  `iocCancelRejected`); GTC remainder rests at the tail.
- Reduce-only: after a tx, a resting reduce-only order whose side can no longer reduce the position (flat or the
  wrong sign) is canceled with no action (`reduceOnlyCanceled`). Position shrinking below its size but not to zero:
  left as is (open question, MAHITO_PLAN; config `reduce_only_shrink` is the switch to try the other answer).
- Cancels (by oid or cloid) remove the sender's own resting order; an action whose every item misses errors.

Simplifications (see docs/deviations.md, M1): free margin is fixed per account for the chunk (no mark-to-market),
and the margin check uses the order's full size at its limit price (the part that grows the position).
Units: px in ticks, sizes in lots (ints); margin in Decimal USD.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from engine_py.types import ASSETS

WINDOW_MS = 400
MIN_NOTIONAL = Decimal(10)
MAX_LEVERAGE = {0: 40, 1: 25}            # BTC, ETH


@dataclass
class Account:
    free_margin: Decimal = Decimal(10**9)
    position: dict = field(default_factory=dict)       # asset -> signed lots
    leverage: dict = field(default_factory=dict)       # asset -> leverage (default: max)


@dataclass
class OrderItem:
    asset: int
    side: str            # "B" | "A"
    px: int
    sz: int
    tif: str             # "Alo" | "Gtc" | "Ioc"
    cloid: str | None = None
    reduce_only: bool = False


@dataclass
class CancelItem:
    asset: int
    oid: int | None = None
    cloid: str | None = None


@dataclass
class Tx:
    index: int           # proposer order inside the block
    user: str
    kind: str            # "order" | "cancel" | "cancelByCloid"
    items: list
    p: int = 0           # grouping priority bid
    hash: str = ""
    error: str | None = None   # set by the chain: an action whose every cancel item missed

    @property
    def klass(self) -> int:
        if self.kind == "order" and any(it.tif != "Alo" for it in self.items):
            return 2
        return 1 if self.kind in ("cancel", "cancelByCloid") else 0


@dataclass
class Order:
    oid: int
    user: str
    asset: int
    side: str
    px: int
    sz: int              # remaining lots
    orig_sz: int
    tif: str
    p: int
    t_ms: int
    cloid: str | None
    reduce_only: bool
    status: str = "open"


@dataclass
class Trade:
    """One printed fill, in execution order."""
    asset: int
    taker_side: str
    px: int
    sz: int
    time_ms: int
    hash: str
    tid: int
    buyer: str
    seller: str
    maker_oid: int
    taker_oid: int | None


class Chain:
    def __init__(self, accounts: dict[str, Account] | None = None, reduce_only_shrink: str = "leave",
                 first_oid: int = 1, first_tid: int = 1, min_notional: Decimal = MIN_NOTIONAL,
                 open_order_cap: int | None = None, order: str = "class_sort"):
        if order not in ("class_sort", "proposer"):
            raise ValueError(f"order={order!r}: class_sort | proposer")
        self.order = order                      # truth ordering: class sort per batch (docs) | listing order
        self.accounts = accounts or {}
        self.reduce_only_shrink = reduce_only_shrink
        self.min_notional = Decimal(min_notional)
        self.open_order_cap = open_order_cap                        # resting orders per user (None: no cap)
        self.open_count: dict[str, int] = {}
        self.open_cloid: dict[tuple, int] = {}                      # (user, asset, cloid) -> resting oid
        self.burn_before_item = None                                # callable -> oids to burn before an item
        self.books = {a: {"B": {}, "A": {}} for a in ASSETS}       # asset -> side -> px -> [Order] (front first)
        self.orders: dict[int, Order] = {}                          # every order ever placed, by oid
        self.next_oid, self.next_tid = first_oid, first_tid
        self.truth: list[dict] = []                                 # hidden facts, in execution order

    # ------------------------------------------------------------ views
    def levels(self, asset: int, side: str, depth: int | None = None) -> list[tuple[int, int, int]]:
        """(px, size, count) best first."""
        lv = self.books[asset][side]
        pxs = sorted((p for p, q in lv.items() if q), reverse=(side == "B"))[:depth]
        return [(p, sum(o.sz for o in lv[p]), len(lv[p])) for p in pxs]

    def best(self, asset: int, side: str) -> int | None:
        lv = self.levels(asset, side, 1)
        return lv[0][0] if lv else None

    def queue(self, asset: int, side: str, px: int) -> list[Order]:
        return list(self.books[asset][side].get(px, []))

    # ------------------------------------------------------------ helpers
    def _account(self, user: str) -> Account:
        return self.accounts.setdefault(user, Account())

    def _new_oid(self) -> int:
        oid = self.next_oid
        self.next_oid += 1
        return oid

    def burn_oids(self, n: int, height: int) -> None:
        """Invisible events that use oids (1-5% of ids on the real chain)."""
        self.truth.append({"kind": "burned_oids", "first": self.next_oid, "n": n, "height": height})
        self.next_oid += n

    @staticmethod
    def _notional(asset: int, px: int, sz: int) -> Decimal:
        spec = ASSETS[asset]
        return Decimal(spec.ticks_to_px(px)) * Decimal(spec.lots_to_sz(sz))

    def _grows(self, user: str, asset: int, side: str, sz: int) -> int:
        """Lots of this order that open or grow the position."""
        pos = self._account(user).position.get(asset, 0)
        signed = sz if side == "B" else -sz
        if pos == 0 or (pos > 0) == (signed > 0):
            return sz
        return max(0, abs(signed) - abs(pos))

    def _can_reduce(self, user: str, asset: int, side: str) -> bool:
        pos = self._account(user).position.get(asset, 0)
        return (pos < 0) if side == "B" else (pos > 0)

    def _reject(self, o: Order, reason: str, height: int) -> None:
        o.status = reason
        self.truth.append({"kind": "order_status", "oid": o.oid, "status": reason, "height": height})

    # ------------------------------------------------------------ resting
    def _rest(self, o: Order, height: int) -> None:
        q = self.books[o.asset][o.side].setdefault(o.px, [])
        i = len(q)
        if o.tif == "Alo":
            # sort the young (< 400 ms) post-only tail by p; anything older keeps its place
            while (i > 0 and q[i - 1].tif == "Alo" and o.t_ms - q[i - 1].t_ms < WINDOW_MS
                   and o.p > q[i - 1].p):
                i -= 1
        q.insert(i, o)
        self.open_count[o.user] = self.open_count.get(o.user, 0) + 1
        if o.cloid is not None:
            self.open_cloid[(o.user, o.asset, o.cloid)] = o.oid
        self.truth.append({"kind": "rested", "oid": o.oid, "queue_pos": i, "level_len": len(q), "height": height})

    def _unrest(self, o: Order) -> None:
        q = self.books[o.asset][o.side][o.px]
        q.remove(o)
        self.open_count[o.user] -= 1
        if not q:
            del self.books[o.asset][o.side][o.px]
        if o.cloid is not None and self.open_cloid.get((o.user, o.asset, o.cloid)) == o.oid:
            del self.open_cloid[(o.user, o.asset, o.cloid)]

    def seed(self, user: str, asset: int, side: str, px: int, sz: int, t_ms: int, p: int = 0, tif: str = "Alo",
             cloid: str | None = None, reduce_only: bool = False) -> int:
        """A pre-window resting order (behind anything already at that price)."""
        o = Order(self._new_oid(), user, asset, side, px, sz, sz, tif, p, t_ms, cloid, reduce_only)
        self.orders[o.oid] = o
        self.books[asset][side].setdefault(px, []).append(o)
        self.open_count[user] = self.open_count.get(user, 0) + 1
        if cloid is not None:
            self.open_cloid[(user, asset, cloid)] = o.oid
        self.truth.append({"kind": "seeded", "oid": o.oid, "user": user, "asset": asset, "side": side, "px": px,
                           "sz": sz, "t_ms": t_ms, "queue_pos": len(self.books[asset][side][px]) - 1})
        return o.oid

    # ------------------------------------------------------------ blocks
    def run_block(self, height: int, time_ms: int, batches: list[list[Tx]]) -> list[Trade]:
        trades: list[Trade] = []
        for b, batch in enumerate(batches):
            for tx in self.exec_order(batch):
                self.truth.append({"kind": "exec", "height": height, "batch": b, "tx": tx.index, "hash": tx.hash})
                trades += self._apply(tx, height, time_ms)
        return trades

    def exec_order(self, batch: list[Tx]) -> list[Tx]:
        """One batch's execution order: class sort (docs) or listing order."""
        key = (lambda tx: (tx.klass, tx.index)) if self.order == "class_sort" else (lambda tx: tx.index)
        return sorted(batch, key=key)

    def _apply(self, tx: Tx, height: int, time_ms: int) -> list[Trade]:
        trades: list[Trade] = []
        if tx.kind == "order":
            for k, it in enumerate(tx.items):
                trades += self._place(tx, k, it, height, time_ms)
            self._reduce_only_sweep(height, {(t.buyer, t.asset) for t in trades} | {(t.seller, t.asset) for t in trades})
        else:
            hits = 0
            for it in tx.items:
                o = self._find(tx.user, it)
                if o is not None:
                    self._unrest(o)
                    o.status = "canceled"
                    self.truth.append({"kind": "order_status", "oid": o.oid, "status": "canceled",
                                       "height": height, "by": tx.hash})
                    hits += 1
            tx.error = None if hits else "Order was never placed, already canceled, or filled."
        return trades

    def _find(self, user: str, it: CancelItem) -> Order | None:
        oid = self.open_cloid.get((user, it.asset, it.cloid)) if it.cloid is not None else it.oid
        o = self.orders.get(oid) if oid is not None else None
        if o is not None and o.status == "open" and o.user == user and o.asset == it.asset:
            return o
        return None

    def _place(self, tx: Tx, k: int, it: OrderItem, height: int, time_ms: int) -> list[Trade]:
        if self.burn_before_item is not None:            # invisible events between placements (sampler hook)
            n = self.burn_before_item()
            if n:
                self.burn_oids(n, height)
        o = Order(self._new_oid(), tx.user, it.asset, it.side, it.px, it.sz, it.sz, it.tif, tx.p, time_ms,
                  it.cloid, it.reduce_only)
        self.orders[o.oid] = o
        self.truth.append({"kind": "placed", "oid": o.oid, "user": o.user, "asset": o.asset, "side": o.side,
                           "px": o.px, "sz": o.sz, "tif": o.tif, "p": o.p, "cloid": o.cloid,
                           "reduce_only": o.reduce_only, "height": height, "tx": tx.index, "item": k,
                           "hash": tx.hash})
        acct = self._account(o.user)
        if self._notional(o.asset, o.px, o.sz) < self.min_notional:
            self._reject(o, "minTradeNtlRejected", height)
            return []
        if o.reduce_only and not self._can_reduce(o.user, o.asset, o.side):
            self._reject(o, "reduceOnlyRejected", height)
            return []
        if (self.open_order_cap is not None and o.tif != "Ioc"
                and self.open_count.get(o.user, 0) >= self.open_order_cap):
            self._reject(o, "tooManyOpenOrdersRejected", height)
            return []
        grow = 0 if o.reduce_only else self._grows(o.user, o.asset, o.side, o.sz)
        lev = acct.leverage.get(o.asset, MAX_LEVERAGE[o.asset])
        if grow and self._notional(o.asset, o.px, grow) / lev > acct.free_margin:
            self._reject(o, "perpMarginRejected", height)
            return []
        opp = "A" if o.side == "B" else "B"
        crosses = lambda best: best is not None and (o.px >= best if o.side == "B" else o.px <= best)
        if o.tif == "Alo":
            if crosses(self.best(o.asset, opp)):
                self._reject(o, "badAloPxRejected", height)
                return []
            self._rest(o, height)
            return []
        trades = []
        while o.sz > 0:
            best = self.best(o.asset, opp)
            if not crosses(best):
                break
            maker = self.books[o.asset][opp][best][0]
            if maker.user == o.user:                                   # self-trade: cancel the RESTING order
                self._unrest(maker)
                maker.status = "selfTradeCanceled"
                self.truth.append({"kind": "order_status", "oid": maker.oid, "status": "selfTradeCanceled",
                                   "height": height})
                continue
            q = min(o.sz, maker.sz)
            buyer, seller = (o.user, maker.user) if o.side == "B" else (maker.user, o.user)
            trades.append(Trade(o.asset, o.side, best, q, time_ms, tx.hash, self.next_tid, buyer, seller,
                                maker.oid, o.oid))
            self.next_tid += 1
            self.truth.append({"kind": "fill", "maker_oid": maker.oid, "taker_oid": o.oid, "maker": maker.user,
                               "taker": o.user, "px": best, "sz": q, "height": height, "hash": tx.hash})
            for user, signed in ((buyer, q), (seller, -q)):
                pos = self._account(user).position
                pos[o.asset] = pos.get(o.asset, 0) + signed
            o.sz -= q
            maker.sz -= q
            if maker.sz == 0:
                self._unrest(maker)
                maker.status = "filled"
        if o.sz > 0 and o.tif == "Gtc":
            self._rest(o, height)
        elif o.sz == o.orig_sz and o.tif == "Ioc":
            self._reject(o, "iocCancelRejected", height)
        else:
            o.status = "filled" if o.sz == 0 else "canceled"
        return trades

    def _reduce_only_sweep(self, height: int, changed: set) -> None:
        """Cancel resting reduce-only orders that can no longer reduce their owner's position."""
        for user, asset in sorted(changed):
            for side in ("B", "A"):
                for px in list(self.books[asset][side]):
                    for o in list(self.books[asset][side].get(px, [])):
                        if o.user != user or not o.reduce_only:
                            continue
                        pos = self._account(user).position.get(asset, 0)
                        if not self._can_reduce(user, asset, side) or \
                                (self.reduce_only_shrink == "cancel" and o.sz > abs(pos)):
                            self._unrest(o)
                            o.status = "reduceOnlyCanceled"
                            self.truth.append({"kind": "order_status", "oid": o.oid, "status": "reduceOnlyCanceled",
                                               "height": height, "silent": True})
