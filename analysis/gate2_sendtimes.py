"""gate2_sendtimes: mahito's gate-2 inputs (docs/gate2.md section 2) from handoff/gate2_input (analysis.gate2_input).

Writes OUT/blocks.csv (chunk_dir,height: one chunk dir per block, preferring dev_margin60 > devfast > dev20) and
OUT/send_times.csv in the format of tests/test_ordering_m1n.py:
height,tx_hash,item_index,user,nonce,send_ms,calibrated (+ info columns action_index, batch, class, tier).
One row per explorer tx (= one item of an SQD action; split items share the hash), aligned with analysis.sqd_truth.align.
- send_ms = calibrated send time (nonce - per-account offset), calibrated = 1 (signer offset) or 2 (pooled offset).
- --raw: send_ms = raw nonce, calibrated = 0 (sensitivity file; for takers inside a fee tier the raw nonce agrees
  better with listing order, docs/gate2_data.md).
- Actions with a stale or future nonce (not a clock) get NO row, so the replay keeps their stored slot.
usage: python -m analysis.gate2_sendtimes --in handoff/gate2_input --out handoff/gate2 [--raw]
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from analysis.gate2_input import explorer_txs, load_block, set_index
from analysis.sqd_truth import align

PREF = ("dev_margin60", "devfast", "dev20")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="handoff/gate2_input")
    ap.add_argument("--sqd", default=str(Path("~/hl-data/sqd/blocks").expanduser()))
    ap.add_argument("--sets-root", default=str(Path("~/hl-data/mac3").expanduser()))
    ap.add_argument("--out", default="handoff/gate2")
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()
    inp, out = Path(a.inp), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    idx = [x for x in set_index(Path(a.sets_root)) if x[0] in PREF]
    heights = [int(r["block"]) for r in csv.DictReader(open(inp / "inventory.csv"))]
    blocks, st_rows, skipped = [], [], {"no_chunk": 0, "unaligned_tx": 0, "stale_or_future_tx": 0}
    for h in heights:
        cands = sorted(((PREF.index(s), str(c)) for s, lo, hi, c in idx
                        if lo <= h <= hi and (c / f"{h}.json.gz").exists()))
        if not cands:
            skipped["no_chunk"] += 1
            continue
        chunk = Path(cands[0][1])
        blocks.append({"chunk_dir": str(chunk), "height": h})
        rows = {int(r["listing_pos"]): r for r in csv.DictReader(open(inp / "blocks" / f"{h}.csv"))}
        _, _, acts = load_block(Path(a.sqd) / f"{h}.jsonl")
        txs = explorer_txs(chunk, h)
        for t, v in align(txs, acts).items():
            if v is None:
                skipped["unaligned_tx"] += 1
                continue
            ai, j = v[0], v[1]
            r = rows[ai]
            if r["send_cal_ms"] == "":
                skipped["stale_or_future_tx"] += 1
                continue
            send = r["nonce"] if a.raw else r["send_cal_ms"]
            cal = 0 if a.raw else (1 if r["calib"] == "signer" else 2)
            st_rows.append({"height": h, "tx_hash": (txs[t].get("hash") or "").lower(), "item_index": j,
                            "user": r["signer"], "nonce": r["nonce"], "send_ms": send, "calibrated": cal,
                            "action_index": ai, "batch": r["batch"], "class": r["class"], "tier": r["tier"]})
    name = "send_times_raw.csv" if a.raw else "send_times.csv"
    with open(out / "blocks.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["chunk_dir", "height"])
        w.writeheader()
        w.writerows(blocks)
    with open(out / name, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(st_rows[0].keys()))
        w.writeheader()
        w.writerows(st_rows)
    print(json.dumps({"blocks": len(blocks), "chunks": len({b["chunk_dir"] for b in blocks}),
                      "send_time_rows": len(st_rows), "skipped": skipped, "file": str(out / name)}))


if __name__ == "__main__":
    main()
