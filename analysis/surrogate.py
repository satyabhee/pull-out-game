"""Fast, high-n checks of what a data source does, without waiting for contested transactions.

Per chunk, per replay variant (same chunks, paired):
  cancels      int-oid cancels / modifies in the SCORED blocks, and how many the engine could not find
               (rejected UNKNOWN_ORDER; duplicates of an already-removed order count here too, in every variant)
  touch        at every scored block's start, per asset and side, our best level vs the chain's bbo at the end of
               the previous block: same price, and if so our order count == the bbo count (n); our count - n
  recall       per-tx recall (maker-matched) over scored txs with real fills
Reported: each metric for the reference and every variant, the paired difference (reference - variant) with a
95% chunk-cluster bootstrap interval.

usage: python -m analysis.surrogate <dirs...> --hl-data H [base replay options] --variant '' --variant '--x' ...
"""
from __future__ import annotations

import argparse
import bisect
import random
import shlex
import sys
from collections import Counter
from multiprocessing import Pool

from analysis import validate_replay as vr
from analysis.reproducible import windows_of
from engine_py.replay import COINS, iter_bbo
from engine_py.types import Cancel, Modify, RejectReason, Side


class _Obs(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.out = Counter()
        self._h = None
        self.bbo = None
        _Obs.last = self

    def before_action(self, rp, block, a):
        if self.bbo is None:
            self.bbo = {}
            for coin in COINS:
                for t, asset, b, k in iter_bbo(rp.hl_data, coin, rp.t0 - 60_000, rp.t1):
                    self.bbo.setdefault(asset, []).append((t, b, k))
        if block.height != self._h:
            self._h = block.height
            if block.height >= rp.warmup_end_height:
                for asset, lst in self.bbo.items():
                    i = bisect.bisect_left([x[0] for x in lst], block.time_ms) - 1
                    if i < 0:
                        continue
                    _, b, k = lst[i]
                    for side, lv in ((Side.BUY, b), (Side.SELL, k)):
                        if lv is None:
                            continue
                        ours = rp.engine.books[asset].best_level(side)
                        self.out["touch_obs"] += 1
                        if ours is not None and ours.px == lv[0]:
                            self.out["touch_same_px"] += 1
                            self.out["touch_same_n"] += len(ours) == lv[2]
                            self.out["touch_excess"] += len(ours) - lv[2]
        return super().before_action(rp, block, a)

    def after_action(self, rp, block, a, fills, rejects, created):
        hl = a.oid if isinstance(a, Cancel) else a.target if isinstance(a, Modify) else None
        if isinstance(hl, int) and block.height >= rp.warmup_end_height:
            self.out["cancels"] += 1
            self.out["cancels_unfound"] += any(r.reason is RejectReason.UNKNOWN_ORDER for r in rejects)
        return super().after_action(rp, block, a, fills, rejects, created)


def one(job):
    window, hl_data, policy, kws = job
    res = {}
    for name, kw in kws:
        _, rows = vr.run(window, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
        o = _Obs.last.out
        rec = [r["matched_notional"] / r["real_notional"] for r in rows
               if not r["warmup"] and r["status"] in ("match", "mismatch") and r["real_notional"] > 0]
        o["txs"], o["recall_sum"] = len(rec), sum(rec)
        res[name] = dict(o)
    return res


METRICS = {
    "scored cancels found": lambda c: (c["cancels"] - c["cancels_unfound"], c["cancels"]),
    "touch: same order count as chain": lambda c: (c["touch_same_n"], c["touch_same_px"]),
    "touch: our extra orders (mean)": lambda c: (c["touch_excess"], c["touch_same_px"]),
    "per-tx recall, all txs": lambda c: (c["recall_sum"], c["txs"]),
}


def ratio(chunks, name, f):
    num = sum(f(c[name])[0] for c in chunks)
    den = sum(f(c[name])[1] for c in chunks)
    return num / den if den else float("nan")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows", nargs="+")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--variant", action="append", default=[])
    ap.add_argument("--boot", type=int, default=2000)
    vr.add_replay_args(ap)
    argv = list(sys.argv[1:] if argv is None else argv)
    a = ap.parse_args(argv)
    base, skip = [], set(a.windows)
    it = iter(argv)
    for x in it:                                   # the replay options shared by every variant
        if x in ("--variant", "--jobs", "--boot"):
            next(it, None)
        elif x.startswith(("--variant=", "--jobs=", "--boot=")) or x in skip:
            continue
        else:
            base.append(x)
    opt = argparse.ArgumentParser()
    vr.add_replay_args(opt)
    kws = [(v or "(reference)", vr.replay_kwargs(opt.parse_args(base + shlex.split(v)))) for v in (a.variant or [""])]
    ws = windows_of(a.windows)
    with Pool(a.jobs) as pool:
        outs = pool.map(one, [(str(w), a.hl_data, a.policy, kws) for w in ws])
    ref = kws[0][0]
    rnd = random.Random(2026)
    samples = [[rnd.choice(outs) for _ in outs] for _ in range(a.boot)]
    print(f"{len(ws)} chunks")
    for m, f in METRICS.items():
        print(f"\n{m}:")
        r0 = ratio(outs, ref, f)
        print(f"  {ref:40} {r0:.4f}")
        for name, _ in kws[1:]:
            v = ratio(outs, name, f)
            bs = sorted(ratio(s, ref, f) - ratio(s, name, f) for s in samples)
            lo, hi = bs[int(.025 * len(bs))], bs[int(.975 * len(bs)) - 1]
            print(f"  {name:40} {v:.4f}   reference - this = {r0 - v:+.4f} [{lo:+.4f}, {hi:+.4f}]")


if __name__ == "__main__":
    sys.exit(main())
