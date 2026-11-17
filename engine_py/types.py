"""Order, Action, Fill: integer ticks and lots, never floats (specs/01-book.md sections 0-1).

Decimal strings are converted to integers exactly once, here, at the edge. Anything that is not a
whole multiple of the tick or lot is rejected, never rounded.
"""
from __future__ import annotations

import decimal
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, IntEnum

U64_MAX = 2**64 - 1
U128_MAX = 2**128 - 1
_CTX = decimal.Context(prec=80, traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow])


class Side(IntEnum):        # numeric values are the canonical enum bytes (section 7)
    BUY = 0
    SELL = 1


class Tif(IntEnum):
    GTC = 0
    IOC = 1
    ALO = 2


class RejectReason(str, Enum):
    INVALID_UNITS = "INVALID_UNITS"                  # not a whole multiple of tick/lot, or not a number
    SIZE_ZERO = "SIZE_ZERO"
    PRICE_OUT_OF_RANGE = "PRICE_OUT_OF_RANGE"
    SIZE_TOO_LARGE = "SIZE_TOO_LARGE"
    NOTIONAL_TOO_LARGE = "NOTIONAL_TOO_LARGE"
    POST_ONLY_WOULD_TAKE = "POST_ONLY_WOULD_TAKE"


class EngineError(RuntimeError):
    """Invariant violation, counter exhaustion or overflow: the whole block transition is discarded."""


def _exact(value: str, unit: Decimal) -> int | None:
    """Exact base-10 value / unit as an int, or None if it is not a whole, non-negative multiple."""
    try:
        d = _CTX.create_decimal(value)
    except (decimal.InvalidOperation, TypeError, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    q = _CTX.divide(d, unit)
    if q != q.to_integral_value():
        return None
    n = int(q)
    return n if n <= U64_MAX else None


@dataclass(frozen=True)
class MarketConfig:
    """Immutable, versioned market configuration (section 0)."""
    market_id: str
    config_version: int
    tick_size: Decimal
    lot_size: Decimal
    quote_atom_size: Decimal
    min_price_tick: int
    max_price_tick: int
    max_order_lots: int
    max_order_notional: int          # in quote atoms

    def __post_init__(self):
        per = _CTX.divide(_CTX.multiply(self.tick_size, self.lot_size), self.quote_atom_size)
        if per != per.to_integral_value() or per <= 0:
            raise ValueError("tick_size * lot_size must be a whole number of quote atoms")
        object.__setattr__(self, "_atoms_per_tick_lot", int(per))

    def to_ticks(self, price: str) -> int | None:
        return _exact(price, self.tick_size)

    def to_lots(self, size: str) -> int | None:
        return _exact(size, self.lot_size)

    def notional_atoms(self, price_tick: int, size_lots: int) -> int:
        return price_tick * size_lots * self._atoms_per_tick_lot      # Python ints: arbitrary precision

    def canonical_bytes(self) -> bytes:
        parts = [self.market_id, str(self.config_version), str(self.tick_size), str(self.lot_size),
                 str(self.quote_atom_size), str(self.min_price_tick), str(self.max_price_tick),
                 str(self.max_order_lots), str(self.max_order_notional)]
        return "\x1f".join(parts).encode()

    def digest(self) -> bytes:
        return hashlib.sha256(self.canonical_bytes()).digest()


def hyperliquid_perp_config(market_id: str, sz_decimals: int, config_version: int = 1) -> MarketConfig:
    """Hyperliquid perp units: prices have at most (6 - szDecimals) decimals, sizes szDecimals decimals.

    BTC (szDecimals 5): tick 0.1, lot 0.00001.  ETH (szDecimals 4): tick 0.01, lot 0.0001.
    Every valid Hyperliquid price/size is a whole multiple of these. One tick * one lot = one quote atom.
    """
    tick = Decimal(1).scaleb(-(6 - sz_decimals))
    lot = Decimal(1).scaleb(-sz_decimals)
    return MarketConfig(market_id, config_version, tick, lot, _CTX.multiply(tick, lot),
                        min_price_tick=1, max_price_tick=U64_MAX, max_order_lots=U64_MAX,
                        max_order_notional=2**127)


@dataclass(slots=True)
class Order:
    market_id: str
    config_version: int
    order_id: int                    # engine-assigned, per market, never reused
    owner_id: str
    side: Side
    price_tick: int
    size_lots: int
    remaining_lots: int
    tif: Tif
    arrival_sequence: int
    cloid: str | None = None         # external identity, excluded from the state hash
    exchange_oid: int | None = None  # external identity, excluded from the state hash


@dataclass(frozen=True, slots=True)
class Fill:
    market_id: str
    config_version: int
    maker_order_id: int
    taker_order_id: int
    maker_owner_id: str
    taker_owner_id: str
    taker_side: Side
    price_tick: int
    size_lots: int
    fill_sequence: int
    block_height: int
    index_in_block: int


@dataclass(frozen=True, slots=True)
class Place:
    """A new order, already in integer units. tx_index/action_index give its original block position."""
    owner_id: str
    side: Side
    price_tick: int
    size_lots: int
    tif: Tif
    cloid: str | None = None
    exchange_oid: int | None = None
    tx_index: int = 0
    action_index: int = 0


@dataclass(frozen=True, slots=True)
class Cancel:
    """Cancel by the engine's own order_id (targets are resolved before the engine)."""
    owner_id: str
    order_id: int


@dataclass(slots=True)
class SubmitResult:
    accepted: bool
    reject_reason: RejectReason | None = None
    order_id: int | None = None
    fills: list[Fill] | None = None
    rested_lots: int = 0
    cancelled_lots: int = 0          # IOC remainder or self-trade cancel
    self_trade: bool = False
