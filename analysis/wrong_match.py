"""How often does the int-oid resolver tie a cancel to the WRONG order? Held-out test.

The replay runs WITHOUT order-id facts (--no-oid-facts), so every cancel goes through the estimate-based
resolver. Truth comes from the facts, loaded separately: for a cancel naming real oid H, the facts give H's exact
placement (block, tx, item), hence the engine order created for it. Each resolved cancel is then:
  correct     the resolver picked that order
  WRONG       it picked another order (that order is wrongly removed; the real target stays as a phantom)
  no truth    H has no exact placement in the facts (user not covered)
Unresolved cancels with truth are counted too (they leave the target as a phantom).

usage: python -m analysis.wrong_match <dirs...> --hl-data H --no-oid-facts [replay options] [--last N] [--jobs 1]
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
        self.wrapped = False
        _Obs.last = self

    def before_action(self, rp, block, a):
        if not self.wrapped:
            self.wrapped = True
            truth = OidEstimator.from_files(rp.window_dir, None, facts=True, hl_data=rp.hl_data).exact
            pos_to_engine = {}
            orig = rp.engine.resolve_oid
            obs = self

            def resolve(user, asset, hl_oid):
                if not pos_to_engine or len(pos_to_engine) < len(rp.placed_by):
                    for oid, act in rp.placed_by.items():
                        pos_to_engine[(act.block, act.tx_index, act.item_index)] = oid
                x = orig(user, asset, hl_oid) if orig is not None else None
                part = "scored" if obs._h >= rp.warmup_end_height else "leadin"
                pos = truth.get(hl_oid)
                target = pos_to_engine.get(pos) if pos is not None else None
                if pos is None:
                    obs.out[(part, "resolved" if x is not None else "unresolved", "no truth")] += 1
                elif x is None:
                    obs.out[(part, "unresolved", "target known")] += 1
                elif x == target:
                    obs.out[(part, "resolved", "correct")] += 1
                else:
                    obs.out[(part, "resolved", "WRONG")] += 1
                return x
            rp.engine.resolve_oid = resolve
        self._h = block.height
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
    for part in ("scored", "leadin"):
        c = {k[1:]: v for k, v in t.items() if k[0] == part}
        ok, bad = c.get(("resolved", "correct"), 0), c.get(("resolved", "WRONG"), 0)
        print(f"{part}: resolved with truth {ok + bad}: correct {ok}, WRONG {bad} "
              f"({bad / max(ok + bad, 1):.3f}); unresolved with a known target {c.get(('unresolved', 'target known'), 0)}; "
              f"resolved without truth {c.get(('resolved', 'no truth'), 0)}; unresolved without truth "
              f"{c.get(('unresolved', 'no truth'), 0)}")


if __name__ == "__main__":
    sys.exit(main())
