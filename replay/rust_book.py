"""engine_py.book.Book's API over the Rust copy-on-write book (replay_core.Book). M5 · Isomer.

`RustBook` is a drop-in for `engine_py.book.Book`: the Python Engine and Replay run on it unchanged
(EngineAdapter(backend="rust") swaps it in). Storage, ordering and invariants live in Rust (replay_core/src/book.rs);
this file only translates:
  - orders cross as tuples (oid, user, asset, side, px, sz, tif, cloid, seq, block) and come back as
    engine_py.types.Order values (equal by value to the originals);
  - a level is a LIVE view (total / len / first read the current book), iteration takes a snapshot of the queue
    first (callers only ever remove the node they are on, the one change Python's linked-list iterator survives);
  - `jumps` (the engine's priority rule) is evaluated here, walking from the tail exactly as Book.add does, and
    Rust receives the resulting queue position;
  - copy.deepcopy -> Rust copy-on-write copy (O(1); later changes copy only the levels they touch).
Failures raise engine_py.book.BookError (from Rust), as the Python book does.
"""
from __future__ import annotations

from typing import Callable, Iterator

from engine_py.types import Order, Side, Tif
from replay_core.replay_core import Book as _CoreBook

_SIDES = (Side.BUY, Side.SELL)
_SIDE_CODE = {Side.BUY: 0, Side.SELL: 1}
_TIFS = (Tif.GTC, Tif.IOC, Tif.ALO)
_TIF_CODE = {Tif.GTC: 0, Tif.IOC: 1, Tif.ALO: 2}


def _to_tuple(o: Order) -> tuple:
    return (o.oid, o.user, o.asset, _SIDE_CODE[o.side], o.px, o.sz, _TIF_CODE[o.tif], o.cloid, o.seq, o.block)


def _to_order(t: tuple) -> Order:
    oid, user, asset, side, px, sz, tif, cloid, seq, block = t
    return Order(oid=oid, user=user, asset=asset, side=_SIDES[side], px=px, sz=sz, tif=_TIFS[tif], cloid=cloid,
                 seq=seq, block=block)


class NodeView:
    """A queue entry as callers see it: the order and its remaining lots (at the time it was read)."""
    __slots__ = ("order", "remaining")

    def __init__(self, order: Order, remaining: int):
        self.order = order
        self.remaining = remaining


class LevelView:
    """One price level, read live from the Rust book."""
    __slots__ = ("_core", "_side", "px")

    def __init__(self, core: _CoreBook, side: int, px: int):
        self._core, self._side, self.px = core, side, px

    def _info(self) -> tuple[int, int]:
        return self._core.level_info(self._side, self.px) or (0, 0)

    @property
    def total(self) -> int:
        return self._info()[0]

    @property
    def count(self) -> int:
        return self._info()[1]

    def __len__(self) -> int:
        return self._info()[1]

    def first(self) -> NodeView | None:
        if not self._info()[1]:
            return None
        t, rem = self._core.level_node(self._side, self.px, 0)
        return NodeView(_to_order(t), rem)

    def __iter__(self) -> Iterator[NodeView]:
        for t, rem in self._core.level_nodes(self._side, self.px):
            yield NodeView(_to_order(t), rem)


class SideView:
    """The SortedDict-like px -> level map of one side (only the operations the engine and replay use)."""
    __slots__ = ("_core", "_side")

    def __init__(self, core: _CoreBook, side: int):
        self._core, self._side = core, side

    def keys(self) -> list[int]:
        return self._core.prices(self._side)

    def __iter__(self) -> Iterator[int]:
        return iter(self.keys())

    def __len__(self) -> int:
        return self._core.n_levels(self._side)

    def __bool__(self) -> bool:
        return self._core.n_levels(self._side) > 0

    def __contains__(self, px) -> bool:
        return self._core.level_info(self._side, px) is not None

    def get(self, px, default=None):
        return LevelView(self._core, self._side, px) if px in self else default

    def __getitem__(self, px) -> LevelView:
        if px not in self:
            raise KeyError(px)
        return LevelView(self._core, self._side, px)

    def items(self) -> list[tuple[int, LevelView]]:
        return [(px, LevelView(self._core, self._side, px)) for px in self.keys()]

    def values(self) -> list[LevelView]:
        return [LevelView(self._core, self._side, px) for px in self.keys()]


class RustBook:
    """A standalone Rust book (core = replay_core.Book), or a view of one book of a Rust engine
    (core = replay_core.BookRef, owner = the replay.rust_engine.RustEngine that holds it)."""

    def __init__(self, asset: int, _core=None, owner=None):
        self.asset = asset
        self._core = _core if _core is not None else _CoreBook(asset)
        self._owner = owner

    # ---- queries
    def side(self, side: Side) -> SideView:
        return SideView(self._core, _SIDE_CODE[side])

    def best_bid(self) -> int | None:
        return self._core.best_bid()

    def best_ask(self) -> int | None:
        return self._core.best_ask()

    def best_level(self, side: Side) -> LevelView | None:
        px = self._core.best_bid() if side is Side.BUY else self._core.best_ask()
        return None if px is None else LevelView(self._core, _SIDE_CODE[side], px)

    def __contains__(self, oid: int) -> bool:
        return self._core.contains(oid)

    def node(self, oid: int) -> NodeView:
        if not self._core.contains(oid):
            raise KeyError(oid)                     # Book.node is index[oid]
        t, rem = self._core.node(oid)
        return NodeView(_to_order(t), rem)

    def levels(self, side: Side) -> Iterator[LevelView]:
        """Levels best first."""
        code = _SIDE_CODE[side]
        prices = self._core.prices(code)
        for px in (reversed(prices) if side is Side.BUY else prices):
            yield LevelView(self._core, code, px)

    def is_crossed(self) -> bool:
        return self._core.is_crossed()

    # ---- mutations
    def add(self, order: Order, remaining: int, front: bool = False,
            jumps: Callable[[NodeView], bool] | None = None) -> NodeView:
        """As Book.add: tail by default, head with front=True; with jumps, walk forward from the tail past every
        node for which jumps(node) is True and stop at the first False."""
        at = None
        if front:
            at = 0
        elif jumps is not None:
            code = _SIDE_CODE[order.side]
            info = self._core.level_info(code, order.px)
            i = info[1] if info else 0
            while i > 0:
                t, rem = self._core.level_node(code, order.px, i - 1)
                if not jumps(NodeView(_to_order(t), rem)):
                    break
                i -= 1
            at = i
        self._core.add(_to_tuple(order), remaining, at)
        return NodeView(order, remaining)

    def remove(self, oid: int) -> NodeView:
        t, rem = self._core.remove(oid)
        return NodeView(_to_order(t), rem)

    def reduce(self, oid: int, q: int) -> NodeView:
        t, left = self._core.reduce(oid, q)
        return NodeView(_to_order(t), left)

    # ---- checks and copies
    def check_invariants(self) -> None:
        self._core.check_invariants()

    def __deepcopy__(self, memo) -> RustBook:
        if self._owner is not None:                 # an engine's book: copy the engine, return its book
            import copy
            return copy.deepcopy(self._owner, memo).books[self.asset]
        return RustBook(self.asset, self._core.copy())

    def shares_level_with(self, other: RustBook, side: Side, px: int) -> bool:
        return self._core.shares_level_with(other._core, _SIDE_CODE[side], px)
