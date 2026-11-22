"""Identification test: does the real in-block ordering rule reproduce the chain better than wrong ones?

Inputs: validate_chunks output folders, one per policy, same chunks and options. Scored on the SAME txs for
every policy: scored txs with real fills present in all runs; "contested" from the REAL makers only (one of
them sent a cancel / modify / ALO on that asset in that block: analysis/contested), so the subset does not
depend on the policy. Per policy: per-tx recall (each tx weighs the same), per-tx strict recall, dollar G1.
Then paired differences against a reference policy, per tx, with 95% bootstrap intervals over chunks.
If the reference does not beat the deliberately wrong controls (anti, shuffle) by more than the interval,
the replay cannot identify the ordering rule on that subset.

usage: python -m analysis.identify <chunk-parent dir> name=dir [name=dir ...] [--ref class] [--boot 2000]
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import defaultdict
from pathlib import Path

from analysis.contested import ASSET_OF, block_actors, users_of


def load(d: Path) -> dict:
    p = next(Path(d).glob("*/txs.csv"), None) or Path(d) / "txs.csv"
    out = {}
    for r in csv.DictReader(open(p)):
        if r["warmup"] != "0" or r["status"] not in ("match", "mismatch") or int(r["real_notional"]) <= 0:
            continue
        out[(r["chunk"], r["hash"])] = r
    return out


def contested_keys(rows: dict, chunks_dir: Path) -> set:
    out, cache = set(), {}
    for k, r in sorted(rows.items(), key=lambda kv: (kv[1]["chunk"], int(kv[1]["block"]))):
        key = (r["chunk"], int(r["block"]))
        if key not in cache:
            p = chunks_dir / r["chunk"] / f"{r['block']}.json.gz"
            cache = {key: block_actors(p) if p.exists() else {}}
        if users_of(r["real"]) & cache[key].get(ASSET_OF[r["coin"]], set()):
            out.add(k)
    return out


def per_tx(r):
    real = int(r["real_notional"])
    return int(r["matched_notional"]) / real, int(r["matched_notional_full"]) / real


def summary(runs, keys):
    out = {}
    for name, rows in runs.items():
        rs = [rows[k] for k in keys]
        rec = [per_tx(r) for r in rs]
        real = sum(int(r["real_notional"]) for r in rs)
        out[name] = (sum(a for a, _ in rec) / len(rec), sum(b for _, b in rec) / len(rec),
                     sum(int(r["matched_notional"]) for r in rs) / real)
    return out


def paired(runs, keys, ref, boot, seed=11):
    by_chunk = defaultdict(list)
    for k in keys:
        by_chunk[k[0]].append(k)
    chunks = sorted(by_chunk)
    diffs = {}
    rnd = random.Random(seed)
    samples = [[k for c in (rnd.choice(chunks) for _ in chunks) for k in by_chunk[c]] for _ in range(boot)]
    for name, rows in runs.items():
        if name == ref:
            continue
        d = {k: per_tx(runs[ref][k])[0] - per_tx(rows[k])[0] for k in keys}
        mean = sum(d.values()) / len(d)
        bs = sorted(sum(d[k] for k in s) / len(s) for s in samples if s)
        ci = (bs[int(0.025 * len(bs))], bs[int(0.975 * len(bs)) - 1])
        n_better = sum(v > 1e-12 for v in d.values())
        n_worse = sum(v < -1e-12 for v in d.values())
        diffs[name] = (mean, ci, n_better, n_worse)
    return diffs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks_dir")
    ap.add_argument("runs", nargs="+", help="name=validate_chunks output dir")
    ap.add_argument("--ref", default="class")
    ap.add_argument("--boot", type=int, default=2000)
    a = ap.parse_args(argv)
    runs = {n: load(Path(d)) for n, d in (x.split("=", 1) for x in a.runs)}
    common = set.intersection(*(set(r) for r in runs.values()))
    con = contested_keys({k: runs[a.ref][k] for k in common}, Path(a.chunks_dir))
    for label, keys in (("all txs with real fills", common), ("contested (real makers)", con & common)):
        print(f"\n{label}: {len(keys)} txs")
        s = summary(runs, keys)
        print(f"  {'policy':10} {'per-tx':>7} {'per-tx strict':>13} {'G1 $':>7}")
        for name, (p, ps, g) in sorted(s.items(), key=lambda kv: -kv[1][0]):
            print(f"  {name:10} {p:7.3f} {ps:13.3f} {g:7.3f}")
        print(f"  paired per-tx recall, {a.ref} minus each (95% CI over chunks; txs where {a.ref} is better / worse):")
        for name, (m, (lo, hi), nb, nw) in sorted(paired(runs, keys, a.ref, a.boot).items(), key=lambda kv: -kv[1][0]):
            sig = "  significant" if lo > 0 or hi < 0 else ""
            print(f"    vs {name:9} {m:+.4f} [{lo:+.4f}, {hi:+.4f}]  better {nb:3d} worse {nw:3d}{sig}")


if __name__ == "__main__":
    sys.exit(main())
