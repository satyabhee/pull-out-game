"""Shared interface for the engine: units, actions, orders, fills, rejects.

Frozen once approved (AGENT_PLAN.md section 5.2). Change it only with Satya's OK.

Units
-----
Every price is an int number of ticks and every size an int number of lots.
Decimal strings are converted once, at the edge, with exact rational arithmetic;
anything that is not a whole multiple of the unit is rejected, never rounded.
Only `str` input is accepted, so a binary approximation can never sneak in.

Per asset (Hyperliquid perps, from `{"type":"meta"}`):
    lot  = 10^-szDecimals
    tick = 10^-(6 - szDecimals)
Checked 2026-11-17: BTC szDecimals=5 (tick 0.1, lot 0.00001),
ETH szDecimals=4 (tick 0.01, lot 0.0001). Hyperliquid's extra 5-significant-figure
price rule is an admission rule, not a unit, and is not enforced here.

Function contracts (implemented in other modules)
-------------------------------------------------
    parse.iter_window(dir)            -> Iterator[Block]
    ordering.<policy>(actions)        -> list[Action]       (pure, total order)
    Engine.apply(action)              -> (list[Fill], list[Reject])
    Engine.end_block(height, time_ms) -> None
    state.encode(engine)              -> bytes
    state.chain(prev_hash, height, encoded) -> hash
    replay.run(window_dir, policy)    -> Iterator[(height, fills, rejects, state_hash)]
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from fractions import Fraction
from typing import Union

PERP_MAX_DECIMALS = 6
ZERO_HASH = "0x" + "0" * 64


class ConversionError(ValueError):
    """A decimal string could not be converted exactly to integer units."""


# ---------------------------------------------------------------- units

def to_units(value: str, unit: str) -> int:
    """Convert a decimal string to an exact whole number of `unit`s.

    to_units("101.10", "0.01") == 10110; to_units("101.105", "0.01") raises.
    """
    if not isinstance(value, str) or not isinstance(unit, str):
        raise TypeError("to_units takes decimal strings only")
    try:
        v, u = Decimal(value.strip()), Decimal(unit.strip())
    except InvalidOperation as e:
        raise ConversionError(f"not a decimal: {value!r} / {unit!r}") from e
    if not (v.is_finite() and u.is_finite()) or u <= 0:
        raise ConversionError(f"bad value or unit: {value!r} / {unit!r}")
    q = Fraction(v) / Fraction(u)
    if q.denominator != 1:
        raise ConversionError(f"{value} is not a whole multiple of {unit}")
    return q.numerator


def from_units(n: int, unit: str) -> str:
    """Inverse of to_units, for display and output files (exact)."""
    _check_int("n", n)
    return format((Decimal(n) * Decimal(unit)).normalize(), "f")


@dataclass(frozen=True)
class AssetSpec:
    asset: int
    name: str
    sz_decimals: int

    @property
    def tick(self) -> str:
        return format(Decimal(1).scaleb(-(PERP_MAX_DECIMALS - self.sz_decimals)), "f")

    @property
    def lot(self) -> str:
        return format(Decimal(1).scaleb(-self.sz_decimals), "f")

    def px_to_ticks(self, px: str) -> int:
        return to_units(px, self.tick)

    def sz_to_lots(self, sz: str) -> int:
        return to_units(sz, self.lot)

    def ticks_to_px(self, ticks: int) -> str:
        return from_units(ticks, self.tick)

    def lots_to_sz(self, lots: int) -> str:
        return from_units(lots, self.lot)


# The only assets the book replays. Source: meta universe index, 2026-11-17.
ASSETS: dict[int, AssetSpec] = {
    0: AssetSpec(asset=0, name="BTC", sz_decimals=5),
    1: AssetSpec(asset=1, name="ETH", sz_decimals=4),
}


# ---------------------------------------------------------------- enums

class Side(Enum):
    BUY = "B"   # wire: order b=true, trade side "B"
    SELL = "A"  # wire: order b=false, trade side "A"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class Tif(Enum):
    GTC = "Gtc"
    IOC = "Ioc"
    ALO = "Alo"


# FrontendMarket is a UI market order: IOC at its (wide) limit price.
TIF_FROM_WIRE: dict[str, Tif] = {
    "Gtc": Tif.GTC,
    "Ioc": Tif.IOC,
    "Alo": Tif.ALO,
    "FrontendMarket": Tif.IOC,
}


class RejectReason(Enum):
    ALO_WOULD_CROSS = "ALO_WOULD_CROSS"        # post-only would take on arrival
    UNKNOWN_ORDER = "UNKNOWN_ORDER"            # cancel/modify target not resting
    SELF_TRADE = "SELF_TRADE"                  # taker met its own resting order
    NO_MAKER = "NO_MAKER"                      # outside fill: named maker not found


# ---------------------------------------------------------------- checks

def _check_int(name: str, v: object, *, positive: bool = False) -> None:
    if type(v) is not int:  # rejects bool and every non-int numeric type
        raise TypeError(f"{name} must be int, got {type(v).__name__}")
    if positive and v <= 0:
        raise ValueError(f"{name} must be > 0, got {v}")


# ---------------------------------------------------------------- actions
# Every action carries where it came from, so every ordering policy can end
# its sort key in (block, tx_index, item_index), which is unique.

@dataclass(frozen=True, kw_only=True)
class _Meta:
    user: str                 # lowercase 0x address of the tx sender
    block: int                # block height
    tx_index: int             # position of the tx in the block's listing
    item_index: int           # position of the item inside the action
    hash: str                 # tx hash (ZERO_HASH for OutsideFill)
    priority: int | None      # grouping {"p": N}, else None

    def __post_init__(self) -> None:
        _check_int("block", self.block)
        _check_int("tx_index", self.tx_index)
        _check_int("item_index", self.item_index)
        if self.priority is not None:
            _check_int("priority", self.priority)


@dataclass(frozen=True, kw_only=True)
class _BookMeta(_Meta):
    asset: int

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.asset not in ASSETS:
            raise ValueError(f"asset {self.asset} is not replayed")


@dataclass(frozen=True, kw_only=True)
class Place(_BookMeta):
    side: Side
    px: int                   # ticks
    sz: int                   # lots
    tif: Tif
    cloid: str | None
    reduce_only: bool

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_int("px", self.px, positive=True)
        _check_int("sz", self.sz, positive=True)


@dataclass(frozen=True, kw_only=True)
class Cancel(_BookMeta):
    oid: int                  # Hyperliquid oid (a lookup key, not engine identity)

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_int("oid", self.oid)


@dataclass(frozen=True, kw_only=True)
class CancelByCloid(_BookMeta):
    cloid: str


@dataclass(frozen=True, kw_only=True)
class Modify(_BookMeta):
    target: int | str         # Hyperliquid oid or cloid
    new: Place                # placed as a NEW order: new id, back of the queue


@dataclass(frozen=True, kw_only=True)
class ScheduleCancel(_Meta):
    at_ms: int | None         # cancel all the user's orders at this time; None clears

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.at_ms is not None:
            _check_int("at_ms", self.at_ms)


@dataclass(frozen=True, kw_only=True)
class OutsideFill(_BookMeta):
    """A recorded trade with no user tx (zero hash). `user` is the taker.

    The parser places these after the block's real txs (tx_index = number of
    txs in the block, item_index = rank by tid), i.e. at the end of the block.
    """
    maker_user: str
    taker_side: Side
    px: int
    sz: int
    tid: int

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_int("px", self.px, positive=True)
        _check_int("sz", self.sz, positive=True)
        _check_int("tid", self.tid)


Action = Union[Place, Cancel, CancelByCloid, Modify, ScheduleCancel, OutsideFill]


@dataclass(frozen=True)
class Block:
    height: int
    time_ms: int
    actions: tuple[Action, ...]


# ---------------------------------------------------------------- results

@dataclass(frozen=True, kw_only=True)
class Order:
    """A resting order as admitted. Remaining size is tracked by the book."""
    oid: int                  # engine-assigned, unique, never reused
    user: str
    asset: int
    side: Side
    px: int                   # ticks
    sz: int                   # original lots
    tif: Tif
    cloid: str | None
    seq: int                  # engine arrival sequence (time priority)
    block: int

    def __post_init__(self) -> None:
        _check_int("oid", self.oid)
        _check_int("px", self.px, positive=True)
        _check_int("sz", self.sz, positive=True)
        _check_int("seq", self.seq)


@dataclass(frozen=True, kw_only=True)
class Fill:
    maker_order: int          # engine oid
    taker_order: int | None   # engine oid; None for an OutsideFill
    maker_user: str
    taker_user: str
    taker_side: Side
    px: int                   # maker's price, ticks
    sz: int                   # lots
    block: int
    seq: int                  # per-engine fill sequence
    taker_hash: str

    def __post_init__(self) -> None:
        _check_int("px", self.px, positive=True)
        _check_int("sz", self.sz, positive=True)
        _check_int("seq", self.seq)


@dataclass(frozen=True)
class Reject:
    action: Action
    reason: RejectReason
