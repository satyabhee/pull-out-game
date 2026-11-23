"""Paired contested test: per-tx recall of replay variants vs a reference, same chunks, same txs.

Pre-registered (Nov 20): the tx set is FIXED by the reference variant (scored, not warm-up, real fills, contested
per analysis/contested.py under the reference's engine makers), so every variant is scored on identical txs.
Primary metric: per-tx recall (matched_notional / real_notional, as in the 25.5% headline); secondary: the
maker-strict version (matched_notional_full). Effect = mean over txs of (variant - reference); 95% CI and
two-sided p from a chunk-cluster bootstrap (txs cluster by chunk); Holm across the variants compared.
Sensitivity: the same on all scored txs, and with "contested" defined by REAL makers only.

usage: python -m analysis.contested_paired <validate_chunks out dir> <chunk-parent dir> --ref <variant folder>
       [--boot 4000]
"""
from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from pathlib import Path

from analysis.contested import ASSET_OF, block_actors, load, users_of


def scope_of(rows, chunks_dir: Path, real_only: bool):
    """(chunk, hash) -> contested? from the reference rows."""
    out, cache = {}, {}
    for r in rows:
        key = (r["chunk"], int(r["block"]))
        if key not in cache:
            p = chunks_dir / r["chunk"] / f"{r['block']}.json.gz"
            cache = {key: block_actors(p) if p.exists() else None}
        acts = cache[key]
        if acts is None:
            continue
        makers = users_of(r["real"]) | (set() if real_only else users_of(r["engine"]))
        out[(r["chunk"], r["hash"])] = bool(makers & acts.get(ASSET_OF[r["coin"]], set()))
    return out


def per_tx(rows, field):
    return {(r["chunk"], r["hash"]): r[field] / r["real_notional"] for r in rows if r["real_notional"] > 0}


def paired(ref, var, keys, boot, seed=11):
    keys = [k for k in keys if k in ref and k in var]
    by = defaultdict(list)
    for k in keys:
        by[k[0]].append(var[k] - ref[k])
    chunks = sorted(by)
    mean = lambda ds: sum(ds) / len(ds) if ds else float("nan")
    est = mean([d for c in chunks for d in by[c]])
    rnd = random.Random(seed)
    bs = []
    for _ in range(boot):
        bs.append(mean([d for c in (rnd.choice(chunks) for _ in chunks) for d in by[c]]))
    bs.sort()
    lo, hi = bs[int(0.025 * boot)], bs[int(0.975 * boot) - 1]
    # two-sided percentile-bootstrap p: twice the share of bootstrap means on the far side of 0
    p = min(1.0, 2 * min(sum(b <= 0 for b in bs), sum(b >= 0 for b in bs)) / boot) if est == est else float("nan")
    return {"n": len(keys), "chunks": len(chunks), "ref": mean([ref[k] for k in keys]),
            "var": mean([var[k] for k in keys]), "diff": est, "lo": lo, "hi": hi, "p": max(p, 1 / boot)}


def contested_strict(variant_dir, chunks_dir) -> tuple[float, int]:
    """(mean per-tx maker-strict over contested txs, n): the `ref` number main() prints for "per-tx maker-strict |
    contested" when this variant is the reference."""
    rows = load(Path(variant_dir))
    scope = scope_of(rows, Path(chunks_dir), False)
    vals = per_tx(rows, "matched_notional_full")
    keys = [k for k, c in scope.items() if c and k in vals]
    return (sum(vals[k] for k in keys) / len(keys) if keys else float("nan")), len(keys)


def holm(ps):
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adj, running = [0.0] * len(ps), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(ps) - rank) * ps[i]))
        adj[i] = running
    return adj


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out_dir")
    ap.add_argument("chunks_dir")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--boot", type=int, default=4000)
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    variants = sorted(d.name for d in out.iterdir() if (d / "txs.csv").exists())
    if a.ref not in variants:
        sys.exit(f"--ref {a.ref} not in {variants}")
    rows = {v: load(out / v) for v in variants}
    others = [v for v in variants if v != a.ref]
    scopes = {"contested": scope_of(rows[a.ref], Path(a.chunks_dir), False),
              "contested (real makers)": scope_of(rows[a.ref], Path(a.chunks_dir), True)}
    for field, label in (("matched_notional", "per-tx recall"), ("matched_notional_full", "per-tx maker-strict")):
        ref = per_tx(rows[a.ref], field)
        for sname, keys in (("contested", [k for k, c in scopes["contested"].items() if c]),
                            ("contested (real makers)", [k for k, c in scopes["contested (real makers)"].items() if c]),
                            ("uncontested", [k for k, c in scopes["contested"].items() if not c]),
                            ("all scored", list(ref))):
            res = [paired(ref, per_tx(rows[v], field), keys, a.boot) for v in others]
            adj = holm([r["p"] for r in res])
            print(f"\n{label} | {sname} | reference {a.ref}")
            for v, r, h in zip(others, res, adj):
                print(f"  {v:60} n={r['n']:4d} chunks={r['chunks']:3d} ref {r['ref']:.3f} -> {r['var']:.3f}  "
                      f"diff {r['diff']:+.4f} [{r['lo']:+.4f}, {r['hi']:+.4f}]  p {r['p']:.4f}  Holm {h:.4f}")


if __name__ == "__main__":
    sys.exit(main())
