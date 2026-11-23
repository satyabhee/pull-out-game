"""L1: is the explorer listing class-sorted (runs of the class sequence per block vs a shuffled null)?

Rule and definitions: docs/l1_listing_order.md (written before the first run).
usage: python -m analysis.listing_runs <chunk-parent dir> [--out CSV] [--shuffles 200]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import statistics as st
import sys
from pathlib import Path

TAKER_TIFS = {"Gtc", "Ioc", "FrontendMarket"}


def raw_class(action: dict) -> int:
    k = action.get("type")
    if k == "order":
        items = action.get("orders", [])
    elif k == "modify":
        items = [action.get("order", {})]
    elif k == "batchModify":
        items = [m.get("order", {}) for m in action.get("modifies", [])]
    elif k in ("cancel", "cancelByCloid"):
        return 1
    else:
        return 0
    return 2 if any(o.get("t", {}).get("limit", {}).get("tif") in TAKER_TIFS for o in items) else 0


def runs(seq) -> int:
    return sum(1 for i, c in enumerate(seq) if i == 0 or c != seq[i - 1])


def descents(seq, lo=0, hi=None) -> int:
    """class[i] < class[i-1] for i in (lo, hi]: a boundary just before position i."""
    hi = len(seq) - 1 if hi is None else hi
    return sum(1 for i in range(max(lo, 1), hi + 1) if seq[i] < seq[i - 1])


def block_classes(path: Path) -> list[int]:
    return [raw_class(t["action"]) for t in json.load(gzip.open(path))["blockDetails"]["txs"]]


def null_stats(seq, height: int, shuffles: int, ranges=()):
    rnd = random.Random(height)
    s = list(seq)
    r_runs, r_desc, r_rng = [], [], [[] for _ in ranges]
    for _ in range(shuffles):
        rnd.shuffle(s)
        r_runs.append(runs(s))
        r_desc.append(descents(s))
        for k, (lo, hi) in enumerate(ranges):
            r_rng[k].append(descents(s, lo, hi))
    return st.mean(r_runs), st.mean(r_desc), [st.mean(x) for x in r_rng]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks_dir")
    ap.add_argument("--out", default=None)
    ap.add_argument("--shuffles", type=int, default=200)
    a = ap.parse_args(argv)
    rows = []
    for c in sorted(p for p in Path(a.chunks_dir).iterdir() if p.is_dir()):
        for f in sorted(c.glob("*.json.gz"), key=lambda p: int(p.name.split(".")[0])):
            h = int(f.name.split(".")[0])
            seq = block_classes(f)
            if len(seq) < 10:
                continue
            mr, md, _ = null_stats(seq, h, a.shuffles)
            rows.append(dict(chunk=c.name, block=h, n=len(seq), c0=seq.count(0), c1=seq.count(1), c2=seq.count(2),
                             runs=runs(seq), null_runs=round(mr, 2), rho=round(runs(seq) / mr, 4),
                             descents=descents(seq), null_descents=round(md, 2)))
    if a.out:
        with open(a.out, "w", newline="") as fh:
            w = csv.DictWriter(fh, list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    rho = sorted(r["rho"] for r in rows)
    q = lambda p: rho[min(len(rho) - 1, int(p * len(rho)))]
    print(f"blocks {len(rows)}; rho = runs / shuffled runs: median {q(.5):.3f}, p10 {q(.1):.3f}, p90 {q(.9):.3f}, "
          f"min {rho[0]:.3f}, max {rho[-1]:.3f}")
    print(f"descents per block: median {st.median(r['descents'] for r in rows)}, null median "
          f"{st.median(r['null_descents'] for r in rows)}; txs per block median {st.median(r['n'] for r in rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
