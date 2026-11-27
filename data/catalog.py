"""Data catalog: recorder/capture gaps and the rule that keeps them out of every chunk set.

The gaps live in data/ingest_gaps.json (measured from the recorder files). A chunk "touches a gap" when
[first_block_time - before_ms, last_block_time + after_ms] overlaps one: its replay would read recorder data
(snapshots, bbo, trades) that is missing. Such chunks are never used: analysis/validate_chunks.find_chunks skips them
with the reason "ingest gap", and `python -m data.catalog <root>` checks every chunk set under a data root.

usage: python -m data.catalog ~/hl-data/mac3            (exit 1 if any chunk set contains a gap chunk)
"""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

GAPS_FILE = Path(__file__).with_name("ingest_gaps.json")
OPEN_END = 2**63 - 1          # end_ms null: a gap that is still open (recorder not restarted yet)


@lru_cache(maxsize=1)
def _load() -> tuple[tuple[tuple[int, int], ...], int, int]:
    d = json.loads(GAPS_FILE.read_text())
    gaps = tuple(sorted((g["start_ms"], OPEN_END if g["end_ms"] is None else g["end_ms"]) for g in d["gaps"]))
    return gaps, d["margin"]["before_ms"], d["margin"]["after_ms"]


def gap_touched(first_ms: int, last_ms: int) -> tuple[int, int] | None:
    """The first gap the span [first - before, last + after] overlaps, or None."""
    gaps, before, after = _load()
    lo, hi = first_ms - before, last_ms + after
    for g0, g1 in gaps:
        if lo <= g1 and hi >= g0:
            return g0, g1
    return None


def chunk_gap(window_dir) -> tuple[int, int] | None:
    """gap_touched for a chunk folder (window.json first/last block time); None if it has no times."""
    p = Path(window_dir) / "window.json"
    if not p.exists():
        return None
    meta = json.loads(p.read_text())
    t0, t1 = meta.get("first_block_time"), meta.get("last_block_time")
    if t0 is None or t1 is None:
        return None
    return gap_touched(int(t0), int(t1))


def check_sets(root) -> dict[str, list[str]]:
    """{set name: [chunks touching a gap]} for every chunk-set folder under root (folders of chunk links)."""
    root = Path(root)
    out = {}
    for s in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in ("windows", "hl_data")):
        bad = [c.name for c in sorted(s.iterdir()) if c.is_dir() and chunk_gap(c) is not None]
        out[s.name] = bad
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]).expanduser() if argv else Path.home() / "hl-data" / "mac3"
    res = check_sets(root)
    for name, bad in res.items():
        print(f"{name}: {'OK' if not bad else f'{len(bad)} chunks touch an ingest gap: {bad}'}")
    windows = root / "windows"
    if windows.is_dir():
        touched = [c.name for c in sorted(windows.iterdir()) if c.is_dir() and chunk_gap(c) is not None]
        print(f"windows touching a gap (never to be used): {len(touched)} {touched}")
    return 1 if any(res.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
