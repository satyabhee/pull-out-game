"""L5-lite calibrated-sim check (docs/scoreboard.md, Multiplicity-lite rule): how often each fork's collapse picks
the simulator's truth. Reported only; decides nothing.

Phantom fork: truth = the candidates whose simulated status is a rejection (*Rejected); the collapse is right iff it
dropped exactly a rejected candidate, or kept all when none was rejected. Owner fork: right iff the chosen owner had a
pre-window (seeded) order on that asset / side / price; "any" = some candidate had one.

usage: python -m analysis.ml_sim_truth <sim windows dir> --hl-data DIR --truth DIR [--limit N] --out FILE.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from analysis import validate_chunks as vc
from analysis import validate_replay as vr
from engine_py.types import Side
from replay.adapter import options_for, run_kwargs_from_options, split_policy
from replay.sim.truth import read_truth


class Keep(vr.ValidateObserver):
    rp = None

    def after_block(self, rp, block, minute):
        super().after_block(rp, block, minute)
        Keep.rp = rp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    policy, opts = split_policy(options_for() + ["--multi-lite"])
    kw = run_kwargs_from_options(a.hl_data, opts)
    chunks, _ = vc.find_chunks([a.windows])
    chunks = chunks[:a.limit] if a.limit else chunks
    tot = Counter()
    for ch in chunks:
        truth = read_truth(Path(a.truth) / f"{Path(ch).name}.parquet")
        placed = {(r["height"], r["tx"], r["item"]): r["oid"] for r in truth if r["kind"] == "placed"}
        status = {r["oid"]: r["status"] for r in truth if r["kind"] == "order_status"}
        seeded = {(r["user"], r["asset"], Side.BUY if r["side"] == "B" else Side.SELL, r["px"])
                  for r in truth if r["kind"] == "seeded"}
        vr.run(Path(ch), Path(a.hl_data), policy, quiet=True, observer_cls=Keep, **kw)
        rp = Keep.rp
        for k, v in rp.learn_stats.items():
            if k.startswith("ml_"):
                tot[k] += v
        for e in rp._ml.events:
            asset, side, px = e["key"]
            if e["kind"] == "phantom":
                rej = set()
                for oid in e["cands"]:
                    act = rp.placed_by.get(oid)
                    sim = placed.get((act.block, act.tx_index, act.item_index)) if act is not None else None
                    if sim is not None and str(status.get(sim, "")).endswith("Rejected"):
                        rej.add(oid)
                chosen = set(e["chosen"])
                ok = (chosen <= rej and bool(chosen)) if rej else not chosen
                tot[f"phantom:{e['why']}:n"] += 1
                tot[f"phantom:{e['why']}:right"] += ok
                tot["phantom:any_candidate_rejected"] += bool(rej)
            else:
                ok = (e["chosen"], asset, side, px) in seeded
                anyc = any((u, asset, side, px) in seeded for u in e["cands"])
                tot[f"owner:{e['why']}:n"] += 1
                tot[f"owner:{e['why']}:right"] += ok
                tot[f"owner:{e['why']}:any"] += anyc
        print(f"{Path(ch).name}: {len(rp._ml.events)} collapses", file=sys.stderr, flush=True)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(dict(sorted(tot.items())), open(a.out, "w"), indent=1)
    for k, v in sorted(tot.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
