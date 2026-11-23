"""Reserve the held-out post-change benchmark set (overnight protocol, mahito queue item 4; never replay it).

Qualifying chunk (same filter as Yuta's confirm_alo2 set, prereg_alo_margin Amendment 1/2): complete chunk,
capture_version "2026-11-23-alo-margin" (or its superset "2026-11-23-open-margin"), non-empty alo_margin_users, info_clearinghouseState_errors == 0, no ingest
gap (analysis.validate_chunks.find_chunks). Ordered by start height: 1-10 = dev_alo10, 11-70 = confirm_alo2 (Yuta),
71 .. 70+N = THIS benchmark (N >= 120, default 120). Writes the list only when all of it exists; reads only
window.json files (no replay).

usage: python -m scripts.benchmark_postchange [--windows ~/hl-data/mac3/windows] [--n 120]
       [--out ~/book-replay-data/handoff/benchmark_postchange.txt]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from analysis.validate_chunks import find_chunks

VERSION = "2026-11-23-alo-margin"
# open-margin (Yuta, Nov 23 17:52Z) is a strict superset of alo-margin: the replay's inputs are produced exactly as
# before and v3 output is byte-identical with or without the new files (mail to_mahito #16), so it qualifies too
VERSIONS = (VERSION, "2026-11-23-open-margin")
FIRST = 71                      # 1-based rank of the first benchmark chunk among qualifying chunks


def qualifying(windows: Path) -> list[Path]:
    chunks, _ = find_chunks([windows])
    out = []
    for c in chunks:
        w = json.load(open(c / "window.json"))
        if (w.get("capture_version") in VERSIONS and w.get("alo_margin_users")
                and not w.get("info_clearinghouseState_errors", 0)):
            out.append(c)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--windows", default=str(Path.home() / "hl-data/mac3/windows"))
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--out", default=str(Path.home() / "book-replay-data/handoff/benchmark_postchange.txt"))
    a = ap.parse_args(argv)
    if a.n < 120:
        raise SystemExit("--n must be >= 120 (protocol)")
    q = qualifying(Path(a.windows))
    need = FIRST - 1 + a.n
    print(f"qualifying post-change chunks: {len(q)}; need {need} (benchmark = ranks {FIRST}..{need})", file=sys.stderr)
    out = Path(a.out)
    if len(q) < need:
        print("not enough yet: nothing written", file=sys.stderr)
        return 2
    if out.exists():
        raise SystemExit(f"{out} exists: the reserved list is never rewritten")
    out.write_text("".join(c.name + "\n" for c in q[FIRST - 1:need]))
    print(f"wrote {a.n} chunks to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
