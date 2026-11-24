"""o5_inputs: the fixed inputs mahito's O5 (all-user oracle, RIKA R2) asked for in mail #42, built from files only:
  covered_misses.csv  B2's contested misses (a0_retriage_b2_<set>/misses.csv, distinct (set, chunk, hash); a chunk shared by
                      two sets is kept under the first set listed) with sqd_block_fetched 1/0 (the miss tx's block is on
                      disk from SQD) -> the SQD-covered set is the rows with 1
  tracked_users.csv   per (set, chunk): users the Addendum 3 truth already covers (any row in truth_audit/<set>/status.csv
                      of that chunk), i.e. "tracked" for the O5 split
  all_user_status.csv the SQD all-user truth rows (truth_audit/sqd/<set>/status.csv) with tracked recomputed from
                      tracked_users.csv (1 = user has Addendum 3 truth in that chunk)
usage: python -m analysis.o5_inputs --misses-root R --sqd ~/hl-data/sqd/blocks --truth handoff/truth_audit --out DIR
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

SETS = ("devfast", "dev_margin60", "dev20")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--misses-root", required=True)
    ap.add_argument("--sqd", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    seen_chunks, rows = set(), []
    for s in SETS:
        f = Path(a.misses_root) / f"a0_retriage_b2_{s}" / "misses.csv"
        mine = list(csv.DictReader(open(f)))
        chunks = {r["chunk"] for r in mine}
        for r in mine:
            if r["chunk"] in seen_chunks:
                continue
            got = (Path(a.sqd) / f"{r['block']}.jsonl").exists()
            rows.append({"set": s, "chunk": r["chunk"], "block": r["block"], "hash": r["hash"], "coin": r["coin"],
                         "cause": r["cause"], "sqd_block_fetched": int(got)})
        seen_chunks |= chunks
    uniq = {(r["set"], r["chunk"], r["hash"]): r for r in rows}
    with open(out / "covered_misses.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(uniq.values())
    trk = defaultdict(set)
    for s in SETS:
        for r in csv.DictReader(open(Path(a.truth) / s / "status.csv")):
            trk[(s, r["chunk"])].add(r["user"].lower())
    with open(out / "tracked_users.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["set", "chunk", "user"])
        for (s, ch), us in sorted(trk.items()):
            for u in sorted(us):
                w.writerow([s, ch, u])
    c = Counter()
    with open(out / "all_user_status.csv", "w", newline="") as fh:
        w = None
        for s in SETS:
            p = Path(a.truth) / "sqd" / s / "status.csv"
            if not p.exists():
                continue
            for r in csv.DictReader(open(p)):
                r["tracked"] = int(r["user"].lower() in trk.get((s, r["chunk"]), set()))
                if w is None:
                    w = csv.DictWriter(fh, fieldnames=list(r))
                    w.writeheader()
                w.writerow(r)
                c[("tracked" if r["tracked"] else "untracked", r["status"].split(":")[0])] += 1
    n = len(uniq)
    cov = sum(r["sqd_block_fetched"] for r in uniq.values())
    print(f"B2 contested misses {n} (distinct); SQD-covered (block fetched) {cov}")
    print("by cause (covered / all):", {k: f"{sum(1 for r in uniq.values() if r['cause'] == k and r['sqd_block_fetched'])}/"
                                           f"{sum(1 for r in uniq.values() if r['cause'] == k)}"
                                        for k in sorted({r['cause'] for r in uniq.values()})})
    print("all-user status rows:", dict(sorted(c.items())))


if __name__ == "__main__":
    main()
