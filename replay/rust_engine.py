"""engine_py.matching.Engine's API over the Rust engine (replay_core.Engine). M5 · Isomer.

`RustEngine` is a drop-in for `engine_py.matching.Engine`: the Python Replay, scoring observer and oid resolver run
on it unchanged (EngineAdapter(backend="rust") swaps it in). The chain rules live in Rust
(replay_core/src/engine.rs, a function-by-function port of engine_py/matching.py). This file only:
  - dispatches each action to the Rust function (same if-chain as Engine.apply);
  - builds engine_py.types Fill / Reject objects from what Rust returns;
  - calls the oid resolver (a Python object) for int-oid cancels, exactly as Engine._cancel_hl_oid does;
  - exposes the internals the replay and resolver use: books (RustBook views of the engine's own books),
    by_user (ordered per-user oid sets), next_oid, block, block_time_ms, _new_order, _rest, _remove, ...
copy.deepcopy(engine) is a Rust copy-on-write copy (books share untouched levels).
"""
from __future__ import annotations

import copy
from collections import Counter

from engine_py.book import BookError
from engine_py.matching import Engine
from engine_py.types import (ASSETS, Cancel, CancelByCloid, Fill, Modify, Order, OutsideFill, Place, Reject,
                             RejectReason, ScheduleCancel, Side)
from replay.rust_book import _SIDE_CODE, _TIF_CODE, RustBook, _to_order, _to_tuple
from replay_core.replay_core import Engine as _CoreEngine

_REASONS = (RejectReason.ALO_WOULD_CROSS, RejectReason.SELF_TRADE, RejectReason.UNKNOWN_ORDER,
            RejectReason.NO_MAKER)
_P_MODES = ("always", "known", "knownpos", "docs", "docsall")


class _UserOrders:
    """by_user[user]: the user's resting engine oids in insertion order (live view)."""
    __slots__ = ("_core", "_user")

    def __init__(self, core, user):
        self._core, self._user = core, user

    def __contains__(self, oid) -> bool:
        return self._core.user_contains(self._user, oid)

    def __iter__(self):
        return iter(self._core.user_oids(self._user) or [])

    def __len__(self) -> int:
        return self._core.user_len(self._user)

    def __bool__(self) -> bool:
        return self._core.user_len(self._user) > 0

    def __setitem__(self, oid, value) -> None:   # by_user.setdefault(user, {})[oid] = None
        self._core.user_add(self._user, oid)

    def keys(self):
        return list(self)


class _ByUser:
    """engine.by_user as the replay and resolver use it: get, setdefault, [], in."""
    __slots__ = ("_core",)

    def __init__(self, core):
        self._core = core

    def get(self, user, default=None):
        return _UserOrders(self._core, user) if self._core.user_len(user) else default

    def setdefault(self, user, default=None):
        return _UserOrders(self._core, user)

    def __getitem__(self, user):
        if not self._core.user_len(user):
            raise KeyError(user)
        return _UserOrders(self._core, user)

    def __contains__(self, user) -> bool:
        return self._core.user_len(user) > 0


class RustEngine:
    _crosses = staticmethod(Engine._crosses)

    def __init__(self, stp: str = "cancel_taker", resolve_oid=None, check_every_action: bool = False,
                 p_priority=None, evict_arrival: bool = False, _core=None):
        if not (p_priority is None or p_priority in _P_MODES
                or (isinstance(p_priority, int) and p_priority >= 0)):
            raise ValueError(f"p_priority must be None, 'always', 'known', 'knownpos', 'docs', 'docsall' or ms >= 0, "
                             f"not {p_priority!r}")
        if stp not in ("cancel_taker", "expire_maker"):
            raise ValueError(f"unknown self-trade policy {stp!r}")
        self.stp = stp
        self.resolve_oid = resolve_oid
        self.check_every_action = check_every_action
        self.p_priority = p_priority
        self.evict_arrival = evict_arrival
        if _core is None:
            mode, window = (None, None) if p_priority is None else \
                (p_priority, None) if isinstance(p_priority, str) else ("window", p_priority)
            _core = _CoreEngine(sorted(ASSETS), stp == "expire_maker", mode, window, evict_arrival)
        self._core = _core
        self.books = {a: RustBook(a, _core.book(a), owner=self) for a in sorted(ASSETS)}
        self.by_user = _ByUser(_core)

    @classmethod
    def like(cls, engine: Engine) -> RustEngine:
        """A new, empty RustEngine with the same settings as a (new, empty) Python Engine."""
        return cls(stp=engine.stp, resolve_oid=engine.resolve_oid, check_every_action=engine.check_every_action,
                   p_priority=engine.p_priority, evict_arrival=engine.evict_arrival)

    # ------------------------------------------------------------ counters the replay reads / writes
    next_oid = property(lambda s: s._core.next_oid, lambda s, v: setattr(s._core, "next_oid", v))
    next_seq = property(lambda s: s._core.next_seq, lambda s, v: setattr(s._core, "next_seq", v))
    next_fill_seq = property(lambda s: s._core.next_fill_seq, lambda s, v: setattr(s._core, "next_fill_seq", v))
    block = property(lambda s: s._core.block, lambda s, v: setattr(s._core, "block", v))
    block_time_ms = property(lambda s: s._core.block_time_ms, lambda s, v: setattr(s._core, "block_time_ms", v))

    @property
    def stats(self) -> Counter:
        return Counter(dict(self._core.stats()))

    @property
    def schedules(self) -> dict:
        return dict(self._core.schedules())

    # ------------------------------------------------------------ public
    def seed(self, asset: int, side: Side, px: int, sz: int) -> int:
        return self._core.seed(asset, _SIDE_CODE[side], px, sz)

    def apply(self, action):
        self._core.block = action.block
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
            self._core.schedule(action.user, action.at_ms)
            out = [], []
        elif isinstance(action, OutsideFill):
            out = self._outside_fill(action)
        else:
            raise TypeError(f"unknown action {type(action).__name__}")

        if self.check_every_action:
            for book in self.books.values():
                book.check_invariants()
        elif hasattr(action, "asset"):
            self._core.assert_not_crossed(action.asset)
        return out

    def reconcile(self, asset: int, bids, asks, known: str | None = None) -> dict[str, int]:
        if known not in (None, "oldest", "newest"):
            raise ValueError(f"unknown eviction order {known!r}")
        return dict(self._core.reconcile(asset, [tuple(x) for x in bids], [tuple(x) for x in asks], known))

    def evict(self, oid: int) -> Order:
        return _to_order(self._core.evict(oid))

    def refill(self, asset: int, bids, asks) -> dict[str, int]:
        return dict(self._core.refill(asset, [tuple(x) for x in bids], [tuple(x) for x in asks]))

    def clear(self, asset: int) -> dict[str, int]:
        return dict(self._core.clear(asset))

    def end_block(self, height: int, time_ms: int) -> None:
        self._core.end_block(time_ms)

    # ------------------------------------------------------------ internals the replay calls
    def _new_order(self, user, asset, side, px, sz, tif, cloid) -> Order:
        return _to_order(self._core.new_order(user, asset, _SIDE_CODE[side], px, sz, _TIF_CODE[tif], cloid))

    def _p_arg(self, p: Place) -> tuple:
        return (p.priority or 0,) if self.p_priority is not None else ()

    def _rest(self, order: Order, remaining: int, p: int | None = None) -> None:
        self._core.rest(_to_tuple(order), remaining, p)

    def _remove(self, oid: int) -> Order:
        return _to_order(self._core.remove(oid))

    def _place(self, p: Place, source):
        parg = (p.priority or 0) if self.p_priority is not None else None
        fills, codes = self._core.place(p.user, p.asset, _SIDE_CODE[p.side], p.px, p.sz, _TIF_CODE[p.tif], p.cloid,
                                        parg)
        return ([Fill(maker_order=m, taker_order=t, maker_user=mu, taker_user=p.user, taker_side=p.side, px=px, sz=sz,
                      block=p.block, seq=seq, taker_hash=p.hash) for m, t, mu, px, sz, seq in fills],
                [Reject(source, _REASONS[c]) for c in codes])

    def _cancel_cloid(self, source, user, asset, cloid) -> list[Reject]:
        c = self._core.cancel_cloid(user, asset, cloid)
        return [] if c is None else [Reject(source, _REASONS[c])]

    def _cancel_hl_oid(self, source, user, asset, hl_oid) -> list[Reject]:
        oid = self.resolve_oid(user, asset, hl_oid) if self.resolve_oid else None
        if oid is None or oid not in self.books[asset]:
            return [Reject(source, RejectReason.UNKNOWN_ORDER)]
        order = self.books[asset].node(oid).order
        if order.user != user:
            raise BookError(f"resolver mapped {user}'s oid {hl_oid} to {order.user}'s order")
        self._remove(oid)
        return []

    def _outside_fill(self, of: OutsideFill):
        fills, c = self._core.outside_fill(of.asset, of.maker_user, _SIDE_CODE[of.taker_side], of.px, of.sz)
        out = [Fill(maker_order=m, taker_order=t, maker_user=mu, taker_user=of.user, taker_side=of.taker_side, px=px,
                    sz=sz, block=of.block, seq=seq, taker_hash=of.hash) for m, t, mu, px, sz, seq in fills]
        return out, ([] if c is None else [Reject(of, _REASONS[c])])

    # ------------------------------------------------------------ pickling (setup cache)
    def __getstate__(self) -> dict:
        """Settings, the oid resolver (it points back at this engine: pickle keeps the cycle) and the Rust state."""
        return {"stp": self.stp, "check_every_action": self.check_every_action, "p_priority": self.p_priority,
                "evict_arrival": self.evict_arrival, "resolve_oid": self.resolve_oid,
                "core": self._core.export_state()}

    def __setstate__(self, st: dict) -> None:
        self.stp, self.check_every_action = st["stp"], st["check_every_action"]
        self.p_priority, self.evict_arrival = st["p_priority"], st["evict_arrival"]
        self._core = _CoreEngine.from_state(st["core"])
        self.books = {a: RustBook(a, self._core.book(a), owner=self) for a in sorted(ASSETS)}
        self.by_user = _ByUser(self._core)
        self.resolve_oid = st["resolve_oid"]

    # ------------------------------------------------------------ copies
    def __deepcopy__(self, memo) -> RustEngine:
        new = RustEngine.__new__(RustEngine)
        memo[id(self)] = new
        new.stp, new.check_every_action = self.stp, self.check_every_action
        new.p_priority, new.evict_arrival = self.p_priority, self.evict_arrival
        new._core = self._core.copy()
        new.books = {a: RustBook(a, new._core.book(a), owner=new) for a in sorted(ASSETS)}
        for a, b in self.books.items():
            memo[id(b)] = new.books[a]
        new.by_user = _ByUser(new._core)
        new.resolve_oid = copy.deepcopy(self.resolve_oid, memo)   # the resolver points back at the engine
        return new
