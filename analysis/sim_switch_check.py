"""Full-visibility sim check (BLITZ lane item 3; plan M1 property): with nothing hidden, every switch combination
must reach maker-strict 1.0 on every scored tx. Any shortfall is a bug (or a switch that acts on evidence the chain
does not use).

Generates full-visibility sim chunks (replay.sim, seeds given) once, then replays each with every variant (a full flag
string as for validate_replay, e.g. "--policy listing --alo-gate touch ...") and prints, per variant: scored txs,
strict-matched txs, strict notional share, and the gate counters. Exit code 1 if any variant falls short.

usage: python -m analysis.sim_switch_check --root DIR --seeds 0-9 --variant NAME="FLAGS" [--variant ...]
       [--n-blocks 120] [--lead-in 40] [--out FILE.json]
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections import Counter
from pathlib import Path

from analysis import validate_replay as vr
from analysis.validate_replay import scored_rows
from replay.sim.config import SimConfig
from replay.sim.generate import chunk_name, generate_chunk


def seeds_arg(s: str) -> list[int]:
    out = []
    for part in s.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def variant_kwargs(flags: str) -> tuple[str, dict]:
    ap = argparse.ArgumentParser()
    vr.add_replay_args(ap, hl_data_required=False)
    a = ap.parse_args(shlex.split(flags))
    return a.policy, vr.replay_kwargs(a)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--seeds", type=seeds_arg, default=seeds_arg("0-9"))
    ap.add_argument("--variant", action="append", required=True, metavar='NAME="FLAGS"')
    ap.add_argument("--n-blocks", type=int, default=120)
    ap.add_argument("--lead-in", type=int, default=40)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    root = Path(a.root)
    windows = []
    for seed in a.seeds:
        cfg = SimConfig.full_visibility(seed=seed, n_blocks=a.n_blocks, lead_in=a.lead_in)
        w = root / "windows" / chunk_name(cfg)
        if not w.exists():
            generate_chunk(cfg, root)
        windows.append((seed, w))
    report, bad = {}, False
    for spec in a.variant:
        name, _, flags = spec.partition("=")
        policy, kw = variant_kwargs(flags)
        tot, per_seed = Counter(), {}
        for seed, w in windows:
            res, rows = vr.run(w, root / "hl_data", policy, quiet=True, **kw)
            sc = scored_rows(rows)
            c = Counter(scored=len(sc), strict=sum(r["match_full"] for r in sc),
                        real=sum(r["real_notional"] for r in sc), matched=sum(r["matched_notional_full"] for r in sc))
            c.update({f"gate:{k}": v for k, v in res.get("gate_stats", {}).items()})
            tot.update(c)
            per_seed[seed] = dict(c)
            if c["strict"] < c["scored"]:
                misses = [r["hash"][:12] for r in sc if not r["match_full"]]
                per_seed[seed]["misses"] = misses
        share = tot["matched"] / tot["real"] if tot["real"] else float("nan")
        ok = tot["strict"] == tot["scored"] and tot["scored"] > 0
        bad |= not ok
        gates = ", ".join(f"{k[5:]} {v}" for k, v in sorted(tot.items()) if k.startswith("gate:"))
        print(f"{name:12} {'OK ' if ok else 'SHORT'} scored {tot['scored']} strict {tot['strict']} "
              f"notional share {share:.4f}  [{gates}]")
        for seed, c in per_seed.items():
            if c.get("misses"):
                print(f"    seed {seed}: {c['scored'] - c['strict']} miss(es) {c['misses'][:5]}")
        report[name] = {"flags": flags, "ok": ok, "total": dict(tot), "per_seed": per_seed}
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=1, default=str))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
