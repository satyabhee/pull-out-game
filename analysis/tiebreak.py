"""Ambiguous int-oid cancels: can a tie-break rule pick the right order? Held-out test.

Replay WITHOUT order-id facts; truth (the real target's placement) from the facts loaded separately, as in
analysis/wrong_match. For each cancel the resolver leaves ambiguous, the candidates are the user's resting orders
on that asset whose estimated real oid is within tolerance. Rules scored by how often they pick the true target:
  oldest     the candidate placed first (front of its queue, the stalest quote)
  newest     the candidate placed last
  nearest    the smallest |estimate - real oid|
Only cancels whose true target is among the candidates are graded (else no rule can be right: reported apart).

usage: python -m analysis.tiebreak <dirs...> --hl-data H --no-oid-facts [replay options] [--last N]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from multiprocessing import Pool

from analysis import validate_replay as vr
from analysis.reproducible import windows_of
from data.oids import OidEstimator


class _Obs(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.out = Counter()
        self.ready = False
        _Obs.last = self

    def before_action(self, rp, block, a):
        if not self.ready and rp.resolver is not None:
            self.ready = True
            truth = OidEstimator.from_files(rp.window_dir, None, facts=True, hl_data=rp.hl_data).exact
            res, pos_to_engine, obs = rp.resolver, {}, self

            def resolve(user, asset, hl_oid):
                x = res(user, asset, hl_oid)
                if x is not None or res._last_reason != "ambiguous" or hl_oid not in truth:
                    return x
                if len(pos_to_engine) < len(rp.placed_by):
                    for oid, act in rp.placed_by.items():
                        pos_to_engine[(act.block, act.tx_index, act.item_index)] = oid
                target = pos_to_engine.get(truth[hl_oid])
                book = rp.engine.books[asset]
                cands = []
                for oid in rp.engine.by_user.get(user, {}):
                    if oid in book and oid in res.hl:
                        e, tol = res.hl[oid]
                        if abs(e - hl_oid) <= tol:
                            cands.append((oid, abs(e - hl_oid)))
                obs.out["ambiguous_with_truth"] += 1
                if target not in [c for c, _ in cands]:
                    obs.out["target_not_a_candidate"] += 1
                    return x
                obs.out["graded"] += 1
                obs.out["candidates"] += len(cands)
                picks = {"oldest": min(cands)[0], "newest": max(cands)[0],
                         "nearest": min(cands, key=lambda c: (c[1], c[0]))[0]}
                for rule, p in picks.items():
                    obs.out[rule] += p == target
                return x
            rp.engine.resolve_oid = resolve
        return super().before_action(rp, block, a)


def one(job):
    window, hl_data, policy, kw = job
    vr.run(window, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
    return _Obs.last.out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows", nargs="+")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--last", type=int, default=0)
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    ws = windows_of(a.windows)
    ws = ws[-a.last:] if a.last else ws
    with Pool(a.jobs) as pool:
        outs = pool.map(one, [(str(w), a.hl_data, a.policy, vr.replay_kwargs(a)) for w in ws])
    t = Counter()
    for o in outs:
        t.update(o)
    g = max(t["graded"], 1)
    print(f"{len(ws)} windows: ambiguous cancels with known truth {t['ambiguous_with_truth']}; true target not among "
          f"the candidates {t['target_not_a_candidate']}; graded {t['graded']} (avg {t['candidates'] / g:.1f} candidates; "
          f"a random pick is right about {g / max(t["candidates"], 1):.3f})")
    for rule in ("oldest", "newest", "nearest"):
        print(f"  {rule:8} right {t[rule] / g:.3f}")


if __name__ == "__main__":
    sys.exit(main())
