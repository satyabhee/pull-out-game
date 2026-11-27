"""Per-block state hash (G2): SHA-256 chain over a canonical encoding of the order books.

Adopted by Satya on Nov 27 (docs/determinism_check.md). It replaces the FNV-1a placeholder this file used to name.
- **Scope = the books.** For every resting order: its price, its remaining size, its queue position, and its
  identity (oid, user, seq).
- **Excluded, by decision:** ledgers, learners, oid maps, counters and any other replay-side state. Two replays can
  agree on this hash and still differ there.
- **Encoding:** `encode_books`. For each asset in sorted order: "A<asset>", then "B" with the bid levels best first,
  then "S" with the ask levels best first. Each level is "L<px ticks>", followed by its nodes in queue order (head to
  tail), each "oid,user,remaining lots,seq". An anonymous placeholder (node.order is None) is "-,-,remaining,-".
  Tokens are joined with "|" and encoded as UTF-8.
- **Chain:** `block_hash(engine, prev, height) = sha256(prev_hex.encode() + encode_books(engine)).hexdigest()`. The
  first block uses prev = "". `height` is not hashed: the chain carries order.
- **Use:** pass `hasher=state.block_hash` to engine_py.replay.Replay (or analysis.validate_replay.run). The hook
  runs at every block end and Replay yields the hash as the 4th tuple element. The default is no hasher, which
  yields None, as before.
- **Works for both backends:** it reads `engine.books[asset].levels(side)` and nodes' `.order` / `.remaining`, the
  interface the Python engine and replay.rust_engine.RustEngine (replay.rust_book) both expose.
"""
from __future__ import annotations

import hashlib

from engine_py.types import Side


def encode_books(engine) -> bytes:
    parts = []
    for a in sorted(engine.books):
        book = engine.books[a]
        parts.append(f"A{a}")
        for side in (Side.BUY, Side.SELL):
            parts.append("B" if side is Side.BUY else "S")
            for lv in book.levels(side):
                parts.append(f"L{lv.px}")
                for n in lv:
                    o = n.order
                    parts.append(f"{o.oid if o else '-'},{o.user if o else '-'},{n.remaining},{o.seq if o else '-'}")
    return "|".join(parts).encode()


def block_hash(engine, prev: str | None, height: int) -> str:
    """Replay hasher hook: the next link of the per-block chain (hex)."""
    return hashlib.sha256((prev or "").encode() + encode_books(engine)).hexdigest()
