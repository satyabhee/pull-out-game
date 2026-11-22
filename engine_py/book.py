"""One market's book: price levels, a FIFO queue per level, and an order index.

From specs/01-book.md:
- levels: one sorted map per side, px -> Level. Best bid = largest key, best ask = smallest.
- Level: doubly linked list with head/tail sentinels (head side = oldest) and a cached total.
- index: engine oid -> node, for O(1) cancel.

Costs: add O(log L); cancel O(1) + O(log L) if the level empties; best price O(1)
(sortedcontainers keeps its ends cheap); matching pops from the head.

Invariants (check_invariants): a price is in levels iff its queue is non-empty; every
resting order is in the index once and in exactly one queue at its own price and side;
each level's total equals the sum of its remaining sizes; the book is never crossed.
"""

from __future__ import annotations

from typing import Callable, Iterator

from sortedcontainers import SortedDict

from engine_py.types import Order, Side


class BookError(RuntimeError):
    """The book was asked to do something impossible. Always a bug: never caught."""


class Node:
    __slots__ = ("order", "remaining", "prev", "next", "level")

    def __init__(self, order: Order | None, remaining: int):
        self.order = order
        self.remaining = remaining
        self.prev: Node | None = None
        self.next: Node | None = None
        self.level: Level | None = None


class Level:
    __slots__ = ("px", "head", "tail", "total", "count")

    def __init__(self, px: int):
        self.px = px
        self.head = Node(None, 0)   # sentinel; head.next is the oldest order
        self.tail = Node(None, 0)   # sentinel; tail.prev is the newest order
        self.head.next, self.tail.prev = self.tail, self.head
        self.total = 0
        self.count = 0

    def append(self, node: Node) -> None:
        last = self.tail.prev
        last.next, node.prev, node.next, self.tail.prev = node, last, self.tail, node
        node.level = self
        self.total += node.remaining
        self.count += 1

    def prepend(self, node: Node) -> None:
        first = self.head.next
        self.head.next, node.prev, node.next, first.prev = node, self.head, first, node
        node.level = self
        self.total += node.remaining
        self.count += 1

    def insert_before(self, node: Node, ref: Node) -> None:
        """Link `node` directly ahead of `ref` (a node already in this level)."""
        prev = ref.prev
        prev.next, node.prev, node.next, ref.prev = node, prev, ref, node
        node.level = self
        self.total += node.remaining
        self.count += 1

    def unlink(self, node: Node) -> None:
        node.prev.next, node.next.prev = node.next, node.prev
        node.prev = node.next = node.level = None
        self.total -= node.remaining
        self.count -= 1

    def first(self) -> Node | None:
        n = self.head.next
        return None if n is self.tail else n

    def __iter__(self) -> Iterator[Node]:
        n = self.head.next
        while n is not self.tail:
            nxt = n.next          # safe if the caller unlinks n
            yield n
            n = nxt

    def __len__(self) -> int:
        return self.count


class Book:
    def __init__(self, asset: int):
        self.asset = asset
        self.bids: SortedDict = SortedDict()
        self.asks: SortedDict = SortedDict()
        self.index: dict[int, Node] = {}

    # ---- queries
    def side(self, side: Side) -> SortedDict:
        return self.bids if side is Side.BUY else self.asks

    def best_bid(self) -> int | None:
        return self.bids.peekitem(-1)[0] if self.bids else None

    def best_ask(self) -> int | None:
        return self.asks.peekitem(0)[0] if self.asks else None

    def best_level(self, side: Side) -> Level | None:
        levels = self.side(side)
        if not levels:
            return None
        return levels.peekitem(-1 if side is Side.BUY else 0)[1]

    def __contains__(self, oid: int) -> bool:
        return oid in self.index

    def node(self, oid: int) -> Node:
        return self.index[oid]

    def levels(self, side: Side) -> Iterator[Level]:
        """Levels best first."""
        levels = self.side(side)
        keys = reversed(levels.keys()) if side is Side.BUY else iter(levels.keys())
        for px in keys:
            yield levels[px]

    def is_crossed(self) -> bool:
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb >= ba

    # ---- mutations
    def add(self, order: Order, remaining: int, front: bool = False,
            jumps: Callable[[Node], bool] | None = None) -> Node:
        """Rest an order at the tail of its level (front=True: at the head, ahead of every order there).
        jumps(node): walking forward from the tail, the new order goes ahead of each node for which it is
        True and stops at the first node for which it is False (priority insertion; None = plain FIFO)."""
        if order.asset != self.asset:
            raise BookError(f"order for asset {order.asset} added to book {self.asset}")
        if order.oid in self.index:
            raise BookError(f"oid {order.oid} already resting")
        if not 0 < remaining <= order.sz:
            raise BookError(f"remaining {remaining} outside (0, {order.sz}]")
        levels = self.side(order.side)
        level = levels.get(order.px)
        if level is None:
            level = levels[order.px] = Level(order.px)
        node = Node(order, remaining)
        if front:
            level.prepend(node)
        elif jumps is None:
            level.append(node)
        else:
            ref = level.tail
            while ref.prev is not level.head and jumps(ref.prev):
                ref = ref.prev
            level.insert_before(node, ref)
        self.index[order.oid] = node
        return node

    def remove(self, oid: int) -> Node:
        """Take an order off the book (cancel, or fully filled)."""
        node = self.index.pop(oid, None)
        if node is None:
            raise BookError(f"oid {oid} is not resting")
        level = node.level
        level.unlink(node)
        if level.count == 0:
            del self.side(node.order.side)[level.px]
        return node

    def reduce(self, oid: int, q: int) -> Node:
        """Fill q lots of a resting order; removes it when it reaches zero."""
        node = self.index.get(oid)
        if node is None:
            raise BookError(f"oid {oid} is not resting")
        if not 0 < q <= node.remaining:
            raise BookError(f"cannot fill {q} of {node.remaining} remaining (oid {oid})")
        if q == node.remaining:
            self.remove(oid)            # unlink takes its remaining off the level total
            node.remaining = 0
        else:
            node.remaining -= q
            node.level.total -= q
        return node

    # ---- checks
    def check_invariants(self) -> None:
        seen = 0
        for side in (Side.BUY, Side.SELL):
            for px, level in self.side(side).items():
                if level.px != px or level.count == 0:
                    raise BookError(f"bad level {px} ({level.count} orders)")
                total = n = 0
                for node in level:
                    o = node.order
                    if o.side is not side or o.px != px or node.level is not level:
                        raise BookError(f"oid {o.oid} queued at the wrong place")
                    if self.index.get(o.oid) is not node:
                        raise BookError(f"oid {o.oid} not indexed to its node")
                    if not 0 < node.remaining <= o.sz:
                        raise BookError(f"oid {o.oid} remaining {node.remaining}")
                    total += node.remaining
                    n += 1
                if total != level.total or n != level.count:
                    raise BookError(f"level {px}: cached {level.total}/{level.count}, real {total}/{n}")
                seen += n
        if seen != len(self.index):
            raise BookError(f"index has {len(self.index)} orders, queues have {seen}")
        if self.is_crossed():
            raise BookError(f"crossed book: bid {self.best_bid()} >= ask {self.best_ask()}")
