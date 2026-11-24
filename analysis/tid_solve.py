"""I27 step 2 (measurement only): how many real fills can be pinned to their exact (taker order, maker order) from the
trade id (data/tid_hash.py: tid = SipHash-1-3(buyer_oid, seller_oid) >> 14).

Per chunk: the oid estimator (anchors + facts, as the replay's resolver) bounds every in-window placement's real oid:
oid in [k + lo, k + hi] (OidEstimator.c_bounds). For each recorded trade whose taker tx is in the window (after
warm-up), the taker oid is searched in the bracket of the taker tx's placement(s) on that asset / side, and the maker
oid among the maker's in-window placements at that asset / side / price placed earlier (each in its own bracket).
A trade is SOLVED when exactly one (taker oid, maker placement) pair reproduces its tid. All fills of one taker tx
share the taker oid: once solved, later fills of the tx only search the maker side.

usage: python -m analysis.tid_solve <chunk-parent dir> --hl-data DIR [--max-width 4000] [--out FILE.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from data.tid_solve import solve_chunk, tid_np  # noqa: F401  (tid_np re-exported for checks)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--max-width", type=int, default=4000)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    stats: Counter = Counter()
    for ch in sorted(p for p in Path(a.chunks).iterdir() if p.is_dir()):
        solve_chunk(ch, Path(a.hl_data), a.max_width, stats)
        print(f"{ch.name}: trades {stats['trades']} solved {stats['solved']}", file=sys.stderr, flush=True)
    print(json.dumps(dict(stats), indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(dict(stats), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
