"""ORACLE ceiling report (BLITZ Addendum 3; ORACLE = look-ahead by design, diagnostic only).

For one oracle variant vs B1 over the allowed sets: pooled maker-strict contested / uncontested (analysis.pooled_paired
logic: chunks shared by two sets counted once), all-scored, and per covered miss: how many of the B1 contested misses the
oracle's truth touches (Yuta's coverage_b1.csv, column O1 / O2 / O3 / O4 / any) were fixed (per-tx strict 0 -> 1 or
improved), the points gained per covered miss, and the ceiling if coverage were 100%:
    ceiling pt = (pooled contested pt gained on covered misses / covered misses) x (all pooled contested misses)
assumption (printed): the covered misses are representative of the uncovered ones.

usage: python -m analysis.oracle_report --variant NAME --cov-col O1 --sets dev20:REF:VAR:CHUNKS ... --coverage FILE
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from analysis.contested import load
from analysis.contested_paired import paired, per_tx, scope_of

FIELD = "matched_notional_full"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--variant", required=True)
    ap.add_argument("--cov-col", required=True)
    ap.add_argument("--sets", nargs="+", required=True, metavar="SET:REF:VAR:CHUNKS")
    ap.add_argument("--coverage", required=True)
    ap.add_argument("--boot", type=int, default=4000)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    covered = set()
    for r in csv.DictReader(open(a.coverage)):
        if r["kind"] == "miss" and r[a.cov_col] == "1":
            covered.add((r["chunk"], r["hash"]))
    ref_all, var_all, cont, unc, allk, owner = {}, {}, [], [], [], {}
    for spec in a.sets:
        name, ref_dir, var_dir, chunks = spec.split(":")
        rr, vv = load(Path(ref_dir)), load(Path(var_dir))
        scope = scope_of(rr, Path(chunks), False)
        ref, var = per_tx(rr, FIELD), per_tx(vv, FIELD)
        for c in sorted({c for c, _ in scope}):
            owner.setdefault(c, name)
        mine = lambda k: owner[k[0]] == name
        ref_all.update({k: x for k, x in ref.items() if mine(k)})
        var_all.update({k: x for k, x in var.items() if mine(k)})
        cont += [k for k, s in scope.items() if s and mine(k)]
        unc += [k for k, s in scope.items() if not s and mine(k)]
        allk += [k for k in scope if mine(k)]
    pc = paired(ref_all, var_all, cont, a.boot)
    pu = paired(ref_all, var_all, unc, a.boot)
    pa = paired(ref_all, var_all, allk, a.boot)
    ck = [k for k in cont if k in ref_all and k in var_all]
    misses = [k for k in ck if ref_all[k] < 1.0]
    cov = [k for k in misses if k in covered]
    n = len(ck)
    gain_cov = sum(var_all[k] - ref_all[k] for k in cov)
    gain_unc = sum(var_all[k] - ref_all[k] for k in misses if k not in covered)
    hurt = sum(var_all[k] - ref_all[k] for k in ck if ref_all[k] >= 1.0)
    fixed = sum(1 for k in cov if var_all[k] >= 1.0)
    per_cov = gain_cov / len(cov) if cov else float("nan")
    out = {
        "variant": a.variant, "label": "ORACLE (look-ahead by design, diagnostic only)",
        "contested": {k: round(100 * pc[k], 2) if k in ("diff", "lo", "hi") else pc[k] for k in pc},
        "uncontested": {k: round(100 * pu[k], 2) if k in ("diff", "lo", "hi") else pu[k] for k in pu},
        "all_scored": {k: round(100 * pa[k], 2) if k in ("diff", "lo", "hi") else pa[k] for k in pa},
        "contested_txs": n, "contested_misses_B1": len(misses), "covered_misses": len(cov),
        "covered_fixed_fully": fixed,
        "pt_gained_on_covered": round(100 * gain_cov / n, 2),
        "pt_gained_on_uncovered_misses": round(100 * gain_unc / n, 2),
        "pt_changed_on_B1_matches": round(100 * hurt / n, 2),
        "pt_per_covered_miss": round(100 * per_cov / n, 3),
        "ceiling_pt_if_100pct": round(100 * per_cov * len(misses) / n, 1),
        "assumption": "covered misses representative of uncovered ones (same per-miss gain)",
    }
    print(json.dumps(out, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
