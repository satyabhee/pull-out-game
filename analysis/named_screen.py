"""One run = validate_chunks output (same CSVs, same scoring) + naming-precision counters (docs/scoreboard.md: owner
forks and fallback 3a need naming precision >= 70%).

Counters: engine fills on named / owner-forked pieces (rp._trade_named), and how many of them carry the real maker
at that price in that tx (naming precision = right / all); plus every learn_stats key with "named", "name_" or
"ml_" in it, summed over chunks. Written to <out>/naming.json and printed.

usage: python -m analysis.named_screen <chunk-parent dir> --hl-data DIR <replay options> --out DIR
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from analysis import validate_chunks as vc
from analysis import validate_replay as vr

TOTAL: Counter = Counter()


class NamedObserver(vr.ValidateObserver):
    current = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        flush()
        NamedObserver.current = self
        self.rp = None

    def after_block(self, rp, block, minute):
        super().after_block(rp, block, minute)
        self.rp = rp

    def after_action(self, rp, block, a, fills, rejects, created):
        super().after_action(rp, block, a, fills, rejects, created)
        if not fills:
            return
        named = {oid: u for (u, _), oids in rp._trade_named.items() for oid in oids}
        real = self.real_by_hash.get(getattr(a, "hash", None)) or []
        for f in fills:
            u = named.get(f.maker_order)
            if u is None:
                continue
            TOTAL["named_filled"] += 1
            TOTAL["named_filled_right" if any(t.maker == u and t.px == f.px for t in real) else "named_filled_wrong"] += 1


def flush() -> None:
    obs = NamedObserver.current
    if obs is not None and obs.rp is not None:
        for k, v in obs.rp.learn_stats.items():
            if "named" in k or "name_" in k or k.startswith("ml_"):
                TOTAL[k] += v
        obs.rp = None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks")
    vr.add_replay_args(ap)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    chunks, skipped = vc.find_chunks([a.chunks])
    vc.run_batch(chunks, a.hl_data, a.policy, out_dir=a.out, jobs=1, skipped=skipped, observer_cls=NamedObserver,
                 **vr.replay_kwargs(a))
    flush()
    n = TOTAL["named_filled"]
    out = dict(sorted(TOTAL.items()), naming_precision=(TOTAL["named_filled_right"] / n) if n else None)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    json.dump(out, open(Path(a.out) / "naming.json", "w"), indent=1)
    for k, v in out.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
