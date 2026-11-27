"""book_digest.py: external per-block book digest for the G2 check (Yuta, Nov 27). NOT engine_py/state.py.

engine_py/state.py (the spec's canonical encoding + FNV-1a chain) is a TODO stub, so there is no engine state hash to
compare. This script uses Replay's existing `hasher(engine, prev_hash, height)` hook (engine_py/replay.py) to record,
after every block, sha256(prev || canonical dump of every book): per asset (sorted), bids best-first then asks
best-first, every level's px and its nodes in queue order as (oid or '-', user or '-', remaining, seq or '-').
Placeholders (node.order None) are included as '-'. It covers the books only (not ledgers, learners or counters).

Runs analysis.validate_chunks in-process (jobs 1) with Replay patched to pass the hasher; writes
<out>/digests.csv: chunk_start,height,digest (in replay order).
usage (from the worktree root): PYTHONPATH=. PYTHONHASHSEED=N python report/determinism/book_digest.py OUT CHUNK...
"""
from __future__ import annotations

import csv
import hashlib
import sys
from pathlib import Path

from analysis import validate_chunks as vc
from analysis import validate_replay as vr
from engine_py.types import Side

ROWS = []
SIZES = []                                         # dump length per block (sanity: the books are non-empty)


def dump(engine) -> bytes:
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


def hasher(engine, prev, height):
    d = dump(engine)
    h = hashlib.sha256((prev or "").encode() + d).hexdigest()
    ROWS.append((height, h))
    SIZES.append(len(d))
    return h


def main():
    out, chunks = Path(sys.argv[1]), sys.argv[2:]
    from analysis.validate_chunks import main as _  # noqa: F401  (import check)
    flags = open(Path.home() / "book-replay-data/handoff/i27/baseline.txt").read().splitlines()[1]
    import os, re
    if os.environ.get("DIGEST_FLAG_SED"):                  # sensitivity check only: 'OLD=>NEW'
        old, new = os.environ["DIGEST_FLAG_SED"].split("=>")
        flags = flags.replace(old, new)
        print("FLAGS (sensitivity):", flags, flush=True)
    flags = flags.split()
    import argparse
    from replay.adapter import run_kwargs_from_options, split_policy
    policy, opts = split_policy(flags)
    kw = run_kwargs_from_options(str(Path.home() / "hl-data/mac3/hl_data"), opts)
    saved = vr.Replay

    def make(*args, **k):
        k["hasher"] = hasher
        return saved(*args, **k)

    vr.Replay = make
    found, skipped = vc.find_chunks(chunks)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "digests.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["chunk_start", "height", "digest"])
        for c in found:
            ROWS.clear()
            vc.run_batch([c], str(Path.home() / "hl-data/mac3/hl_data"), policy, out_dir=str(out / "csv" / Path(c).name),
                         jobs=1, skipped=[], **kw)
            for h, d in ROWS:
                w.writerow([Path(c).name.split("-")[0], h, d])
            fh.flush()
            print(Path(c).name, len(ROWS), "dump bytes min/max", min(SIZES), max(SIZES), flush=True)
            SIZES.clear()


if __name__ == "__main__":
    main()
