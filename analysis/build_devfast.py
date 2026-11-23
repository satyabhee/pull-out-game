"""A1: build the dev-fast set (MAHITO_PLAN §00), by the selection rule in docs/scoreboard.md (written first).

Eligible: chunk, status complete, lead-in 150, first block >= Nov 21 23:11 UTC, no listed ingest gap (find_chunks),
in no existing set, last block <= newest recorder data - 10 min, and (amendment written before any scoring, because
data/ingest_gaps.json was measured on Nov 22 only) the 5-level fast feed of both coins has no silence > 10 s by
feed time over [first block - 15 min, last block + 5 min]. Pick 20 evenly spaced by start height.

usage: python -m analysis.build_devfast ~/hl-data/mac3 [--n 20] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from analysis.validate_chunks import find_chunks
from data.parse import iter_fast

CUTOFF_MS = 1795305600000 - 49 * 60_000          # 2026-11-21 23:11:00 UTC (Nov 22 00:00 = 1795305600000)
SETS = ("dev20", "confirm68", "final240", "held", "held66", "held67", "new3", "new34", "newcov", "post", "tgt")
MAX_SILENCE_MS = 10_000


def newest_recorder_ms(hl: Path) -> int:
    days = sorted(p for p in hl.iterdir() if p.is_dir())
    hours = sorted(p for p in days[-1].iterdir() if p.is_dir())
    return int(max(f.stat().st_mtime for f in hours[-1].iterdir()) * 1000)


def fast_silence(hl: Path, t0: int, t1: int) -> int:
    worst = 0
    for coin in ("BTC", "ETH"):
        times = [b.time_ms for b in iter_fast(hl, coin, t0, t1)]
        edges = [t0, *times, t1]
        worst = max(worst, max(b - a for a, b in zip(edges, edges[1:])))
    return worst


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root).expanduser()
    hl = root / "hl_data"
    used = {p.resolve() for s in SETS if (root / s).exists() for p in (root / s).iterdir()}
    newest = newest_recorder_ms(hl)
    chunks, _ = find_chunks([root / "windows"])
    elig, why = [], {}
    for d in chunks:
        m = json.loads((d / "window.json").read_text())
        f, l = m.get("first_block_time"), m.get("last_block_time")
        reason = ("lead-in" if m.get("lead_in_blocks") != 150 else "before cutoff" if f is None or f < CUTOFF_MS
                  else "in a set" if d.resolve() in used else "too recent" if l > newest - 10 * 60_000 else None)
        if reason is None:
            s = fast_silence(hl, f - 15 * 60_000, l + 5 * 60_000)
            reason = f"fast feed silent {s / 1000:.1f} s" if s > MAX_SILENCE_MS else None
        if reason:
            why[reason] = why.get(reason, 0) + 1
        else:
            elig.append(d)
    n = len(elig)
    pick = [elig[round(i * (n - 1) / (a.n - 1))] for i in range(a.n)] if n >= a.n else elig
    print(f"eligible {n}; excluded {why}; picked {len(pick)}: {pick[0].name} .. {pick[-1].name}")
    if not a.dry_run:
        out = root / "devfast"
        out.mkdir(exist_ok=True)
        for d in pick:
            link = out / d.name
            if not link.exists():
                os.symlink(d.resolve(), link)
        (Path("docs") / "devfast_chunks.txt").write_text("\n".join(d.name for d in pick) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
