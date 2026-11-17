"""Canonical encoding + SHA-256 state hash (specs/01-book.md section 7).

Big-endian fixed-width integers; identifiers as 4-byte length + UTF-8 bytes; enums as one byte;
order ids 16 bytes; prices, sizes, sequences and counts 8 bytes. Domain prefix 01-book-state-v1.
The market configuration is included by its SHA-256 digest.
Counters are encoded as their last assigned value (0 = none); the next value is that + 1. This keeps
an exhausted-but-valid counter representable.
cloid and exchange_oid are external identities and are NOT encoded.
"""
from __future__ import annotations

import hashlib

from .matching import MarketEngine
from .types import Order, Side

PREFIX = b"01-book-state-v1"


def _u8(n: int) -> bytes:
    return n.to_bytes(8, "big")


def _u16(n: int) -> bytes:
    return n.to_bytes(16, "big")


def _ident(s: str) -> bytes:
    b = s.encode()
    return len(b).to_bytes(4, "big") + b


def encode_order(o: Order) -> bytes:
    return b"".join([_ident(o.market_id), _u8(o.config_version), _u16(o.order_id), _ident(o.owner_id),
                     bytes([o.side]), _u8(o.price_tick), _u8(o.size_lots), _u8(o.remaining_lots),
                     bytes([o.tif]), _u8(o.arrival_sequence)])


def encode_market(e: MarketEngine) -> bytes:
    out = [PREFIX, _ident(e.config.market_id), e.config.digest(),
           _u16(e.last_order_id), _u8(e.last_arrival_sequence), _u8(e.last_fill_sequence)]
    for side in (Side.BUY, Side.SELL):                # bids high->low, asks low->high
        levels = list(e.book.levels(side))
        out.append(_u8(len(levels)))
        for price, level in levels:
            out += [_u8(price), _u8(len(level))]
            out += [encode_order(o) for o in level.values()]   # arrival order
    return b"".join(out)


def market_hash(e: MarketEngine) -> str:
    return hashlib.sha256(encode_market(e)).hexdigest()


def state_hash(engines: dict[str, MarketEngine]) -> str:
    """One hash over all markets, in market_id order."""
    h = hashlib.sha256(PREFIX)
    for mid in sorted(engines):
        h.update(hashlib.sha256(encode_market(engines[mid])).digest())
    return h.hexdigest()
