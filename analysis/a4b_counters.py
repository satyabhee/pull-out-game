"""A4(b) counters (docs/scoreboard.md, A4(b) rule): pieces named by trade-learn `name`, engine fills against named
pieces and whether the tx's real trades have the named user as a maker at that price, and pieces removed by
--named-cancel. Replays each chunk once with the given options (no scoring output).

usage: python -m analysis.a4b_counters <chunk-parent dir> --hl-data DIR <replay options...>
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from analysis import validate_chunks as vc
from analysis import validate_replay as vr


class NamedObserver(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        NamedObserver.last = self
        self.c = Counter()

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
            self.c["named_filled"] += 1
            ok = any(t.maker == u and t.px == f.px for t in real)
            self.c["named_filled_real_maker" if ok else "named_filled_other"] += 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks")
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    kw = vr.replay_kwargs(a)
    chunks, _ = vc.find_chunks([a.chunks])
    tot = Counter()
    for ch in chunks:
        res, _ = vr.run(Path(ch), Path(a.hl_data), a.policy,
                     quiet=True, observer_cls=NamedObserver, **kw)
        c = Counter(NamedObserver.last.c)
        for k, v in res.items():
            if k.startswith("learn:") and "named" in k:
                c[k] += v
        tot.update(c)
        print(f"{Path(ch).name}: {dict(c)}", file=sys.stderr, flush=True)
    for k, v in sorted(tot.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
