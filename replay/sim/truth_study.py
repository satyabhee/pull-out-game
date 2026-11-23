"""Which simulator truth ordering puts simulated difficulty closest to real dev20? (Satya, Nov 23)

Decision rule, written before any run (docs/m1_truth_ordering.md): pick the candidate whose baseline contested
strict is closest to real dev20; a candidate that breaks one of the 9 calibration statistics that is in band for
the current default (class_sort, r = 0.1) loses to one that keeps them, unless no candidate keeps it; ties within
0.01 go to the candidate with more statistics in band.

usage: python -m replay.sim.truth_study --candidate NAME ROOT VALIDATE_DIR [--candidate ...] --out FILE
  ROOT: a generate.py root holding the `sim200` set; VALIDATE_DIR: validate_chunks output of the baseline on it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from analysis.contested_paired import contested_strict
from replay.sim.calibrate import feed_and_trades, sim_rejections
from replay.sim.calibration_rounds import BAND, EXTRA_TARGETS, TARGETS, real_targets

HARD, CONC = EXTRA_TARGETS
REAL_VALIDATE = Path("report/validate/m1_ref_blockend20")
REAL_CHUNKS = Path.home() / "hl-data" / "mac3" / "dev20"
DEFAULT = "class_r10"


def in_band(sim: float, real: float) -> bool:
    return bool(real) and abs(sim / real - 1) <= BAND


def stats(root: Path, validate_dir: Path) -> dict:
    s = {**feed_and_trades(root, "sim200"), **sim_rejections(root, "sim200")}
    s[HARD], s["contested n"] = contested_strict(validate_dir, root / "sim200")
    return s


def choose(real: dict, cands: dict[str, dict]) -> tuple[str, list[str]]:
    """The pre-registered rule; returns (winner, reasons)."""
    notes = []
    kept = {k for k in TARGETS if in_band(cands[DEFAULT][k], real[k])} if DEFAULT in cands else set()
    keeps = {n: all(in_band(c[k], real[k]) for k in kept) for n, c in cands.items()}
    pool = [n for n in cands if keeps[n]] or list(cands)
    if len(pool) < len(cands):
        notes.append(f"dropped for breaking a statistic in band under {DEFAULT}: "
                     f"{sorted(set(cands) - set(pool))}")
    gap = {n: abs(cands[n][HARD] - real[HARD]) for n in pool}
    best = min(gap.values())
    tied = [n for n in pool if gap[n] - best <= 0.01]
    n_in = {n: sum(in_band(cands[n][k], real[k]) for k in TARGETS) for n in tied}
    win = max(tied, key=lambda n: (n_in[n], -gap[n]))
    if len(tied) > 1:
        notes.append(f"tie within 0.01 among {tied}; statistics in band {n_in}")
    return win, notes


def table(real: dict, cands: dict[str, dict]) -> str:
    names = list(cands)
    rows = ["| statistic | real (dev20) | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    for k in (HARD, *TARGETS, CONC):
        cells = []
        for n in names:
            v = cands[n][k]
            mark = "" if k in (HARD, CONC) else (" ok" if in_band(v, real[k]) else " **out**")
            cells.append(f"{v:.4g} ({v / real[k]:.2f}x){mark}")
        rows.append(f"| {k} | {real[k]:.4g} | " + " | ".join(cells) + " |")
    rows.append("| in band (of 9) | | " + " | ".join(
        str(sum(in_band(cands[n][k], real[k]) for k in TARGETS)) for n in names) + " |")
    rows.append("| contested txs (n) | " + str(real["contested n"]) + " | " + " | ".join(
        str(cands[n]["contested n"]) for n in names) + " |")
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--candidate", nargs=3, action="append", required=True, metavar=("NAME", "ROOT", "VALIDATE"))
    ap.add_argument("--out", required=True, help="JSON with every number")
    a = ap.parse_args(argv)
    real = real_targets()
    real[HARD], real["contested n"] = contested_strict(REAL_VALIDATE, REAL_CHUNKS)
    cands = {n: stats(Path(r).expanduser(), Path(v)) for n, r, v in a.candidate}
    win, notes = choose(real, cands)
    Path(a.out).write_text(json.dumps({"real": real, "candidates": cands, "winner": win, "notes": notes}, indent=1))
    print(table(real, cands))
    print(f"\nwinner (pre-registered rule): {win}")
    for n in notes:
        print(" -", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
