"""Hyperliquid trade id (tid) from the two order ids of the fill (BLITZ I27; found Nov 24 by testing on userFills pairs
where both sides' oids are known): tid = SipHash-1-3 with zero keys (Rust's DefaultHasher) over the tuple
(buyer_oid, seller_oid) as two little-endian u64s, shifted right by 14 (the top 50 bits).
Verified: analysis in docs/scoreboard.md "I27" (every pair with both oids known matches)."""
from __future__ import annotations

import struct

_M = (1 << 64) - 1


def _rotl(x: int, b: int) -> int:
    return ((x << b) | (x >> (64 - b))) & _M


def siphash13(data: bytes, k0: int = 0, k1: int = 0) -> int:
    v0, v1 = k0 ^ 0x736F6D6570736575, k1 ^ 0x646F72616E646F6D
    v2, v3 = k0 ^ 0x6C7967656E657261, k1 ^ 0x7465646279746573

    def rnd(v0, v1, v2, v3):
        v0 = (v0 + v1) & _M; v1 = _rotl(v1, 13) ^ v0; v0 = _rotl(v0, 32)
        v2 = (v2 + v3) & _M; v3 = _rotl(v3, 16) ^ v2
        v0 = (v0 + v3) & _M; v3 = _rotl(v3, 21) ^ v0
        v2 = (v2 + v1) & _M; v1 = _rotl(v1, 17) ^ v2; v2 = _rotl(v2, 32)
        return v0, v1, v2, v3

    n = len(data) // 8 * 8
    for i in range(0, n, 8):
        m = struct.unpack_from("<Q", data, i)[0]
        v3 ^= m
        v0, v1, v2, v3 = rnd(v0, v1, v2, v3)
        v0 ^= m
    b = (len(data) & 0xFF) << 56
    for i, ch in enumerate(data[n:]):
        b |= ch << (8 * i)
    v3 ^= b
    v0, v1, v2, v3 = rnd(v0, v1, v2, v3)
    v0 ^= b
    v2 ^= 0xFF
    for _ in range(3):
        v0, v1, v2, v3 = rnd(v0, v1, v2, v3)
    return (v0 ^ v1 ^ v2 ^ v3) & _M


def tid(buyer_oid: int, seller_oid: int) -> int:
    return siphash13(struct.pack("<QQ", buyer_oid, seller_oid)) >> 14
