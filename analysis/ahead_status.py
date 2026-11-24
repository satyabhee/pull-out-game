"""ahead_status: chain status of the orders our engine queued AHEAD of the chain's exact filled order (mahito's
handoff/i27/same_block_ahead.csv, mail #38/#41), from SQD all-user truth (analysis/sqd_truth.align) and the existing
truth files (handoff/truth_audit/<set>/status.csv). Diagnostic only.
Output: one row per input row + sqd_status, sqd_text, truth_status, tracked; and a tally by status.
usage: python -m analysis.ahead_status AHEAD.csv --sqd ~/hl-data/sqd/blocks --chunks-root C --truth T --out O.csv
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter
from pathlib import Path

from analysis.sqd_truth import align


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ahead")
    ap.add_argument("--sqd", required=True)
    ap.add_argument("--chunks-root", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    rows = list(csv.DictReader(open(a.ahead)))
    truth = {}
    for s in {r["set"] for r in rows}:
        p = Path(a.truth) / s / "status.csv"
        if p.exists():
            truth.update({(s, x["chunk"], x["tx_hash"], x["item"]): x["status"] for x in csv.DictReader(open(p))})
    cache = {}
    tally = Counter()
    out = []
    for r in rows:
        key = (r["set"], r["chunk"], int(r["block"]))
        if key not in cache:
            f = Path(a.sqd) / f"{key[2]}.jsonl"
            if f.exists() and '"header"' in f.read_text()[:200]:
                sq = json.loads(f.read_text().splitlines()[0])
                with gzip.open(Path(a.chunks_root) / key[0] / key[1] / f"{key[2]}.json.gz", "rt") as fh:
                    eb = json.load(fh)
                eb = eb.get("blockDetails", eb)
                cache[key] = align(eb["txs"], sq.get("actions", []))
            else:
                cache[key] = None
        al = cache[key]
        res = al.get(int(r["ahead_tx_index"])) if al is not None else None
        sst, stxt = (res[2], res[4]) if res else ("no sqd block" if al is None else "unaligned", "")
        tst = truth.get((r["set"], r["chunk"], r["ahead_hash"], r["ahead_item"]), "")
        out.append({**r, "sqd_status": sst, "sqd_text": stxt[:100], "truth_status": tst})
        tally[(r["listed"], sst)] += 1
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    for k, n in sorted(tally.items(), key=lambda x: -x[1]):
        print(n, k)


if __name__ == "__main__":
    main()
