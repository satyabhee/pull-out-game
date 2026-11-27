"""Q8: sampler calibration rounds against dev20 (Satya: each target within +-25% of real, at most 3 rounds).

Each round = a set of SimConfig overrides. A round generates `--seeds` simulated chunks into its own root, computes
the target statistics with replay/sim/calibrate.py (the same code reads real and simulated files), and prints a
table: real, simulated, ratio, within +-25%?

usage: python -m replay.sim.calibration_rounds <work dir> --round r1 [--seeds 0-29]
Rounds are defined in ROUNDS below (the record of what was tried, in order).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from replay.sim.calibrate import feed_and_trades, real_rejections, sim_rejections
from replay.sim.config import SimConfig
from replay.sim.generate import generate_chunk, link_set, parse_seeds

TARGETS = ("trades per block (mean of chunks)", "orders at the touch (median n)",
           "orders per level, 20-level book (median of means)", "touch |dn| median", "touch |dn| p90",
           "touch |dsz|/sz median", "touch |dsz|/sz p90", "post-only margin rejection rate", "tooManyOpenOrders rate")
BAND = 0.25
# Added Nov 23 (Satya): reported next to the 9, not part of the r0-r3 rounds. The difficulty target needs a replay of
# the suite (replay/sim/truth_study.py computes both; docs/m1_truth_ordering.md has the values).
EXTRA_TARGETS = ("baseline contested strict", "maker concentration (top-5 share of fills)")

# Every round lists all the knobs it sets (relative to the untuned M1 values in R0), so the record reproduces even
# though the SimConfig defaults are now r3.
R0 = dict(bot_ioc_prob=0.20, retail_prob=0.10, n_mm=6, mm_levels=4, deep_orders_per_level=0, n_broke_quoters=0,
          broke_quotes_per_block=0.0, n_stackers=0, stacker_prob=0.0, n_flickerers=0, flick_max=0, flick_size_max=6)
ROUNDS: dict[str, dict] = {"r0": dict(R0)}                      # untuned M1 defaults (the "before")
ROUNDS["r1"] = dict(R0, bot_ioc_prob=0.04, retail_prob=0.02,    # takers x0.2: 2.9 -> ~0.56 trades per block
                    n_mm=12, mm_levels=6,                       # more makers near the touch
                    deep_orders_per_level=10,                   # ~12 orders per level over 20 levels
                    n_broke_quoters=2, broke_quotes_per_block=1.5,   # ~1.7% of post-only orders margin-rejected
                    n_stackers=1, stacker_prob=0.13)            # ~0.14% of orders hit the open-order cap
# r2: r1 + bursty touch liquidity (r1 missed every touch-dynamics target: |dn| median 0, p90 3 vs real 2 / 10)
ROUNDS["r2"] = dict(ROUNDS["r1"], n_flickerers=4, flick_max=6)
# r3 (last allowed): wider bursts and sizes (|dn| p90 6 vs 10, |dsz|/sz p90 0.74 vs 1.06), and higher stacker /
# broke-quoter rates to offset the extra orders the flickerers add (cap rate 0.67x, margin 0.82x)
ROUNDS["r3"] = dict(ROUNDS["r2"], n_flickerers=3, flick_max=9, flick_size_max=10, stacker_prob=0.2,
                    broke_quotes_per_block=1.9)


def real_targets() -> dict:
    root = Path.home() / "hl-data" / "mac3"
    return {**feed_and_trades(root, "dev20"),
            **real_rejections(root / "derived" / "order_histories.jsonl.gz")}


def run_round(work: Path, name: str, seeds: list[int]) -> dict:
    root = work / name
    if root.exists():
        shutil.rmtree(root)
    names, secs = [], []
    for s in seeds:
        info = generate_chunk(SimConfig(seed=s, **ROUNDS[name]), root)
        names.append(info["chunk"])
        secs.append(info["seconds"])
    link_set(root, "cal", names)
    sim = {**feed_and_trades(root, "cal"), **sim_rejections(root, "cal")}
    sim["seconds per chunk (max)"] = max(secs)
    return sim


def table(real: dict, sim: dict) -> str:
    rows = ["| statistic | real (dev20) | simulated | sim / real | within +-25% |", "|---|---|---|---|---|"]
    for k in TARGETS:
        r, s = real[k], sim[k]
        ratio = s / r if r else float("nan")
        ok = abs(ratio - 1) <= BAND
        rows.append(f"| {k} | {r:.4g} | {s:.4g} | {ratio:.2f} | {'yes' if ok else '**no**'} |")
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("work")
    ap.add_argument("--round", required=True, choices=sorted(ROUNDS))
    ap.add_argument("--seeds", default="0-29")
    ap.add_argument("--real-json", default=None, help="cached real targets (computed and saved when missing)")
    a = ap.parse_args(argv)
    work = Path(a.work).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    cache = Path(a.real_json) if a.real_json else work / "real_targets.json"
    real = json.loads(cache.read_text()) if cache.exists() else real_targets()
    cache.write_text(json.dumps(real, indent=1))
    sim = run_round(work, a.round, parse_seeds(a.seeds))
    (work / f"{a.round}.json").write_text(json.dumps({"overrides": ROUNDS[a.round], "sim": sim}, indent=1))
    print(f"round {a.round}: overrides {ROUNDS[a.round]}")
    print(table(real, sim))
    print(f"generation: max {sim['seconds per chunk (max)']:.2f} s per chunk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
