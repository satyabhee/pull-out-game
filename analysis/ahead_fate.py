"""ahead_fate: for same-block orders queued ahead of the chain's filled order X that DID rest on chain (SQD "open" with an
oid), find what removed them before X filled: a cancel (by oid or cloid) or a modify of that oid by the owner, in the owner's
SQD actions between X's placement block and the miss (fill) block (~/hl-data/sqd/ranges/<from>_<to>.jsonl; the miss block
itself is also in ~/hl-data/sqd/blocks when fetched). Diagnostic only (RIKA, mahito #38/#41).
Per row: fate = cancelled_before_fill_block | cancelled_in_fill_block (actionIndex before / after the taker's action) |
modified | no removal seen (still resting at the fill block) | range not fetched.
usage: python -m analysis.ahead_fate AHEAD_STATUS.csv --ranges DIR --blocks DIR --txs-root R --chunks-root C --out O.csv
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter
from pathlib import Path

from analysis.sqd_truth import align, outcome


def load_range(path):
    acts = []
    if not path.exists():
        return None
    for line in open(path):
        b = json.loads(line)
        if "header" not in b:
            return None
        for a in b.get("actions", []):
            acts.append((b["header"]["number"], a))
    return acts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ahead_status")
    ap.add_argument("--ranges", required=True)
    ap.add_argument("--blocks", required=True)
    ap.add_argument("--txs-root", required=True)
    ap.add_argument("--chunks-root", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    blk = {}
    for s in ("dev20", "devfast", "dev_margin60"):
        for r in csv.DictReader(open(Path(a.txs_root) / f"bf1_{s}" / "txs.csv")):
            blk[(r["chunk"], r["hash"])] = int(r["block"])
    rows = [r for r in csv.DictReader(open(a.ahead_status)) if r["sqd_status"] == "open"]
    tally, out, rng_cache, taker_ai = Counter(), [], {}, {}
    for r in rows:
        b0, b1 = int(r["block"]), blk[(r["chunk"], r["miss_hash"])]
        key = (b0, b1)
        if key not in rng_cache:
            rng_cache[key] = load_range(Path(a.ranges) / f"{b0}_{b1}.jsonl")
        acts = rng_cache[key]
        if acts is None:
            fate = "range not fetched"
        else:
            oid = int(r["sqd_oid"]) if r["sqd_oid"] else None
            cl = (r.get("cloid") or "").lower()
            u = r["ahead_user"].lower()
            hit = None
            for h, act in acts:
                if (act.get("user") or "").lower() != u:
                    continue
                t = act["action"].get("type")
                items = act["action"].get("cancels", []) if t in ("cancel", "cancelByCloid") else \
                    [{"oid": m.get("oid")} for m in act["action"].get("modifies", [])] if t == "batchModify" else \
                    [{"oid": act["action"].get("oid")}] if t == "modify" else []
                for j, it in enumerate(items):
                    if (oid is not None and it.get("o", it.get("oid")) == oid) or (cl and str(it.get("cloid", "")).lower() == cl):
                        st, _, _ = outcome(act.get("response"), j)
                        hit = (h, act["actionIndex"], t, st)
                        break
                if hit:
                    break
            if hit is None:
                fate = "no removal seen (resting at the fill block)"
            elif hit[0] < b1:
                fate = f"removed BEFORE the fill block ({hit[2]})"
            else:
                tk = (b1, r["miss_hash"])
                if tk not in taker_ai:
                    f = Path(a.blocks) / f"{b1}.jsonl"
                    taker_ai[tk] = None
                    if f.exists():
                        sq = json.loads(f.read_text().splitlines()[0])
                        with gzip.open(Path(a.chunks_root) / r["set"] / r["chunk"] / f"{b1}.json.gz", "rt") as fh:
                            eb = json.load(fh)
                        eb = eb.get("blockDetails", eb)
                        idx = next((i for i, tx in enumerate(eb["txs"]) if tx["hash"] == r["miss_hash"]), None)
                        al = align(eb["txs"], sq.get("actions", []))
                        taker_ai[tk] = al.get(idx)[0] if idx is not None and al.get(idx) else None
                ta = taker_ai[tk]
                fate = f"removed IN the fill block ({hit[2]}), " + (
                    "before the taker" if ta is not None and hit[1] < ta else
                    "after the taker" if ta is not None else "taker position unknown")
            hit = hit or ("", "", "", "")
        out.append({**r, "fill_block": b1, "fate": fate})
        tally[fate] += 1
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    for k, n in tally.most_common():
        print(n, k)


if __name__ == "__main__":
    main()
