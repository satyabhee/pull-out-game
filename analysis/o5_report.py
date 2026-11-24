"""O5 ALL-USER ORACLE report (RIKA R2; ORACLE = look-ahead by design, diagnostic only; plan in handoff/ORACLE.md).

Scores an oracle variant vs the baseline on the SQD-covered contested misses only: the baseline's contested misses
(analysis.retriage misses.csv) whose (chunk, block) is in the frozen covered list. Contested points are on the pooled
contested set of the given sets (chunks shared by two sets counted once), so 1 tx = 100 / n pt.
Per miss class (retriage cause) and tracked / untracked split (a miss is "untracked" if any of its real makers, its
taker, or any named owner ahead of the chain's filled order (analysis.tid_triage rows) is not a tracked user of that
chunk), it reports n, misses fixed fully, pt gained with a chunk-bootstrap 95% CI, and the ceiling:
    ceiling = (pt gained on covered misses / covered misses) x all baseline contested misses of the sets
assumption (printed): covered misses are representative of uncovered ones.

usage: python -m analysis.o5_report --sets SET:REF:VAR:CHUNKS:MISSES ... --covered FIXED.csv --tracked tracked_users.csv
       --tid-triage DIR --out F.json [--name O5]
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analysis.contested import load
from analysis.contested_paired import per_tx, scope_of

FIELD = "matched_notional_full"


def boot_gain(keys, ref, var, n, boot=4000, seed=11):
    """sum over keys of (var - ref) x 100 / n, with a chunk-bootstrap 95% CI (resampling the chunks of `keys`)."""
    by = defaultdict(float)
    for k in keys:
        by[k[0]] += var[k] - ref[k]
    chunks = sorted(by)
    est = 100 * sum(by.values()) / n
    if not chunks:
        return est, 0.0, 0.0
    rnd = random.Random(seed)
    bs = sorted(100 * sum(by[rnd.choice(chunks)] for _ in chunks) / n for _ in range(boot))
    return est, bs[int(0.025 * boot)], bs[int(0.975 * boot) - 1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sets", nargs="+", required=True, metavar="SET:REF:VAR:CHUNKS:MISSES")
    ap.add_argument("--covered", required=True)
    ap.add_argument("--tracked", required=True)
    ap.add_argument("--tid-triage", required=True, help="dir of analysis.tid_triage per-chunk JSONs (baseline misses)")
    ap.add_argument("--name", default="O5")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    cov_blocks = {(r["chunk"], int(r["block"])) for r in csv.DictReader(open(a.covered)) if r["sqd_block_fetched"] == "1"}
    tracked = defaultdict(set)
    for r in csv.DictReader(open(a.tracked)):
        tracked[r["chunk"]].add(r["user"].lower())
    ahead = defaultdict(set)
    for f in Path(a.tid_triage).glob("*.json"):
        for r in json.load(open(f))["rows"]:
            for o in r.get("ahead", {}).get("owners", []):
                ahead[(r["chunk"], r["hash"])].add(o[0].lower())
    ref_all, var_all, cont, owner, misses = {}, {}, [], {}, {}
    for spec in a.sets:
        name, ref_dir, var_dir, chunks, mfile = spec.split(":")
        rr, vv = load(Path(ref_dir)), load(Path(var_dir))
        scope = scope_of(rr, Path(chunks), False)
        for c in sorted({c for c, _ in scope}):
            owner.setdefault(c, name)
        mine = lambda k: owner[k[0]] == name
        ref, var = per_tx(rr, FIELD), per_tx(vv, FIELD)
        ref_all.update({k: x for k, x in ref.items() if mine(k)})
        var_all.update({k: x for k, x in var.items() if mine(k)})
        cont += [k for k, s in scope.items() if s and mine(k)]
        for r in csv.DictReader(open(mfile)):
            k = (r["chunk"], r["hash"])
            if mine(k):
                misses[k] = r
    ck = [k for k in cont if k in ref_all and k in var_all]
    n = len(ck)
    all_miss = [k for k in ck if ref_all[k] < 1.0]
    covered = [k for k in all_miss if k in misses and (k[0], int(misses[k]["block"])) in cov_blocks]

    def untracked(k):
        r = misses[k]
        users = {m.lower()[:10] for m in re.findall(r"0x[0-9a-fA-F]{8,}", r["real"] + " " + r.get("engine", ""))}
        users |= {u[:10] for u in ahead.get(k, ())}
        tr = {u[:10] for u in tracked[k[0]]}
        return any(u not in tr for u in users)

    out = {"variant": a.name, "label": "ORACLE (look-ahead by design, diagnostic only)",
           "pooled_contested_n": n, "baseline_misses": len(all_miss), "covered_misses": len(covered)}
    g, lo, hi = boot_gain(covered, ref_all, var_all, n)
    out["covered: pt gained [95% CI]"] = [round(g, 2), round(lo, 2), round(hi, 2)]
    out["covered: fixed fully"] = sum(1 for k in covered if var_all[k] >= 1.0)
    out["covered: changed up / down"] = [sum(var_all[k] > ref_all[k] for k in covered),
                                         sum(var_all[k] < ref_all[k] for k in covered)]
    per_miss = g / len(covered) if covered else float("nan")
    out["ceiling pt if 100% coverage (gain per covered miss x all misses)"] = round(per_miss * len(all_miss), 2)
    out["ceiling CI"] = [round(lo / len(covered) * len(all_miss), 2), round(hi / len(covered) * len(all_miss), 2)] if covered else None
    out["assumption"] = "covered misses representative of uncovered ones (same per-miss gain)"
    matched_cov_chunks = {k[0] for k in covered}
    dmg = [k for k in ck if ref_all[k] >= 1.0 and k[0] in matched_cov_chunks]
    out["damage on baseline-matched contested txs in covered chunks (pt)"] = round(
        100 * sum(var_all[k] - ref_all[k] for k in dmg) / n, 2)
    classes = defaultdict(list)
    for k in covered:
        classes[(misses[k]["cause"], "untracked" if untracked(k) else "tracked")].append(k)
    out["by class"] = {}
    for (cause, tr), ks in sorted(classes.items()):
        g2, lo2, hi2 = boot_gain(ks, ref_all, var_all, n)
        out["by class"][f"{cause} | {tr}"] = {"n": len(ks), "fixed": sum(1 for k in ks if var_all[k] >= 1.0),
                                              "pt": round(g2, 2), "ci": [round(lo2, 2), round(hi2, 2)]}
    out["tracked split"] = dict(Counter("untracked" if untracked(k) else "tracked" for k in covered))
    print(json.dumps(out, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
