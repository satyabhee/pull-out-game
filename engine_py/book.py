"""Price levels + FIFO queue per level + order index (specs/01-book.md sections 3 and 7).

Each side is a SortedDict of price levels; a level is a dict order_id -> Order whose insertion order
is arrival order (arrival sequences only grow, so appending keeps FIFO). The front of a level is
the first key. Lookups by (owner, cloid) and (owner, exchange_oid) serve cancel-target resolution.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator

from sortedcontainers import SortedDict

from .types import Order, Side


class Book:
    def __init__(self) -> None:
        self.bids: SortedDict = SortedDict()          # key = -price  -> best (highest) bid first
        self.asks: SortedDict = SortedDict()          # key = price   -> best (lowest) ask first
        self.orders: dict[int, Order] = {}
        self._by_cloid: dict[tuple[str, str], set[int]] = defaultdict(set)
        self._by_xoid: dict[tuple[str, int], set[int]] = defaultdict(set)

    # ---- best prices -------------------------------------------------------------------------
    def best_bid(self) -> int | None:
        return -self.bids.peekitem(0)[0] if self.bids else None

    def best_ask(self) -> int | None:
        return self.asks.peekitem(0)[0] if self.asks else None

    def crossed(self) -> bool:
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb >= ba

    # ---- levels ------------------------------------------------------------------------------
    def _side(self, side: Side) -> SortedDict:
        return self.bids if side == Side.BUY else self.asks

    @staticmethod
    def _key(side: Side, price: int) -> int:
        return -price if side == Side.BUY else price

    def levels(self, side: Side) -> Iterator[tuple[int, dict[int, Order]]]:
        """(price, level) from best to worst."""
        for k, level in self._side(side).items():
            yield (-k if side == Side.BUY else k), level

    def best_level(self, side: Side) -> tuple[int, dict[int, Order]] | None:
        s = self._side(side)
        if not s:
            return None
        k, level = s.peekitem(0)
        return (-k if side == Side.BUY else k), level

    # ---- mutation ----------------------------------------------------------------------------
    def add(self, o: Order) -> None:
        s = self._side(o.side)
        k = self._key(o.side, o.price_tick)
        level = s.get(k)
        if level is None:
            level = s[k] = {}
        level[o.order_id] = o
        self.orders[o.order_id] = o
        if o.cloid is not None:
            self._by_cloid[(o.owner_id, o.cloid)].add(o.order_id)
        if o.exchange_oid is not None:
            self._by_xoid[(o.owner_id, o.exchange_oid)].add(o.order_id)

    def remove(self, order_id: int) -> Order:
        o = self.orders.pop(order_id)
        s = self._side(o.side)
        k = self._key(o.side, o.price_tick)
        level = s[k]
        del level[order_id]
        if not level:
            del s[k]
        for idx, key in ((self._by_cloid, (o.owner_id, o.cloid)), (self._by_xoid, (o.owner_id, o.exchange_oid))):
            if key[1] is not None:
                ids = idx[key]
                ids.discard(order_id)
                if not ids:
                    del idx[key]
        return o

    # ---- target resolution (read-only; used by the replay layer) ------------------------------
    def find_by_cloid(self, owner_id: str, cloid: str) -> list[int]:
        return sorted(self._by_cloid.get((owner_id, cloid), ()))

    def find_by_exchange_oid(self, owner_id: str, oid: int) -> list[int]:
        return sorted(self._by_xoid.get((owner_id, oid), ()))

    def __len__(self) -> int:
        return len(self.orders)
