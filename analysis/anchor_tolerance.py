"""Would more anchors make the int-oid resolver commit more often? Held-out test.

The replay runs WITHOUT order-id facts. For each extra-anchor count N, N anchors are sampled (seeded) from the
facts' located placements and added to the chunk's own anchors (anchors only: no exact matches). The resolver is
then graded as in analysis/wrong_match, on cancels whose real target is NOT one of the sampled anchors.
Per N: resolution rate among cancels with a known target, and the wrong-match rate.

usage: python -m analysis.anchor_tolerance <dirs...> --hl-data H --no-oid-facts [replay opts] [--last N]
       [--extra 0,20,50,100,200]
"""
from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

from analysis import validate_replay as vr
from analysis.reproducible import windows_of
from data.oids import OidEstimator, OidResolver
from engine_py.replay import OID_GUARDS

EXTRA = {"n": 0, "base": 50}


class _Obs(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.out = Counter()
        self.ready = False
        _Obs.last = self

    def before_action(self, rp, block, a):
        if not self.ready:
            self.ready = True
            anchors = Path(rp.window_dir) / "anchors.json"
            full = OidEstimator.from_files(rp.window_dir, anchors if anchors.exists() else None, facts=True,
                                           hl_data=rp.hl_data)
            own = OidEstimator.from_files(rp.window_dir, anchors if anchors.exists() else None)
            pool = sorted(set(full.anchors) - set(own.anchors))
            pick = random.Random(7).sample(pool, min(EXTRA["n"], len(pool)))
            est = OidEstimator(own.counts, OidEstimator.consistent(own.anchors + pick), groups=own.groups,
                               block_groups=own.block_groups, placers=own.placers)
            est.first_time = own.first_time
            res = OidResolver(rp.engine, est, base=EXTRA["base"], nearest_ratio=3, **OID_GUARDS[None])
            rp.resolver = res
            truth, sampled = full.exact, {o for _, o in pick}
            pos_to_engine = {}
            obs = self

            def resolve(user, asset, hl_oid):
                if len(pos_to_engine) < len(rp.placed_by):
                    for oid, act in rp.placed_by.items():
                        pos_to_engine[(act.block, act.tx_index, act.item_index)] = oid
                x = res(user, asset, hl_oid)
                pos = truth.get(hl_oid)
                if pos is None or hl_oid in sampled:
                    return x
                target = pos_to_engine.get(pos)
                obs.out["known"] += 1
                obs.out["resolved"] += x is not None
                obs.out["wrong"] += x is not None and x != target
                if x is None:
                    if target is None:
                        why = "target never created in our engine"
                    elif not any(target in b for b in rp.engine.books.values()):
                        why = "target already gone from our book"
                    else:
                        why = "target resting; resolver: " + (res._last_reason or "?")
                    obs.out["why:" + why] += 1
                return x
            rp.engine.resolve_oid = resolve
            self.out["anchors_own"] += len(own.anchors)
            self.out["anchors_added"] += len(pick)
        return super().before_action(rp, block, a)


def one(job):
    window, hl_data, policy, kw, (n, base) = job
    EXTRA["n"], EXTRA["base"] = n, base
    vr.run(window, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
    return (n, base), _Obs.last.out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows", nargs="+")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--last", type=int, default=0)
    ap.add_argument("--extra", default="0:50,100:50,100:10,200:5",
                    help="comma-separated extra_anchors:base_tolerance settings")
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    ws = windows_of(a.windows)
    ws = ws[-a.last:] if a.last else ws
    ns = [tuple(int(y) for y in x.split(":")) for x in a.extra.split(",")]
    jobs = [(str(w), a.hl_data, a.policy, vr.replay_kwargs(a), n) for n in ns for w in ws]
    with Pool(a.jobs) as pool:
        outs = pool.map(one, jobs)
    tot = {n: Counter() for n in ns}
    for n, o in outs:
        tot[n].update(o)
    print(f"{len(ws)} windows; cancels graded = known real target, not itself a sampled anchor")
    for n in ns:
        t = tot[n]
        print(f"  +{n[0]:3d} anchors/chunk, base tolerance {n[1]:3d} (own avg {t['anchors_own'] / len(ws):.0f}, added avg {t['anchors_added'] / len(ws):.0f}): "
              f"graded {t['known']}, resolved {t['resolved'] / max(t['known'], 1):.3f}, "
              f"wrong among resolved {t['wrong'] / max(t['resolved'], 1):.3f}")
        un = t["known"] - t["resolved"]
        for k, v in sorted(((k, v) for k, v in t.items() if k.startswith("why:")), key=lambda kv: -kv[1]):
            print(f"      unresolved, {k[4:]:52} {v / max(un, 1):.3f}")


if __name__ == "__main__":
    sys.exit(main())
