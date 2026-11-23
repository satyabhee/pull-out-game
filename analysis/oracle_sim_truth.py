"""Write the ORACLE status CSV (engine_py/oracle.py format) from sim truth logs, to validate the oracle switches on
sim chunks (where the truth is complete). usage: python -m analysis.oracle_sim_truth <sim root> --out status.csv"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from replay.sim.truth import read_truth


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    cols = ["set", "chunk", "user", "tx_hash", "item", "oid", "status", "status_time_ms"]
    n = 0
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        for tp in sorted((Path(a.root) / "truth").glob("*.parquet")):
            t = read_truth(tp)
            time_of = {r["height"]: r["time"] for r in t if r["kind"] == "block"}
            placed = {r["oid"]: r for r in t if r["kind"] == "placed"}
            for r in t:
                if r["kind"] != "order_status" or r["oid"] not in placed:
                    continue
                p = placed[r["oid"]]
                w.writerow({"set": "sim", "chunk": tp.stem, "user": p["user"], "tx_hash": p["hash"], "item": p["item"],
                            "oid": r["oid"], "status": r["status"], "status_time_ms": time_of.get(r["height"], "")})
                n += 1
    print(f"{n} status rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
