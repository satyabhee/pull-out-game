"""Why int-oid cancels stay unresolved, and how much missed real notional they may explain (scored part only).

For every int-oid cancel/modify the engine could not apply, the resolver's reason (data/oids.OidResolver):
  no_candidate   none of the user's resting orders is within tolerance of the real oid
  ambiguous      several are, none clearly nearest
  ghost / ratio  a guard refused the nearest (another in-window placement of the user is as plausible)
  prewindow      the real oid is older than the window (a placeholder at best)
  exact_gone     a fact gives the exact target, which no longer rests in our book (filled, cancelled, or never
                 rested in the engine: e.g. an ALO the engine rejected)
  exact_unplaced a fact names it, but the placement was not found among our actions
  named_gone     a named pre-window order, no longer resting
Each scored tx flagged "unresolved cancel" has its missed real notional (real - matched) split over the
unresolved cancels that may have targeted its engine makers' orders (validate_replay's test), by reason.
A cancel whose exact target a fact gives cannot have targeted another order: such flags are counted apart
("flag from an exact cancel": the category is then a false positive).

usage: python -m analysis.cancel_misses <window or chunk-parent dirs...> --hl-data H [replay options] [--jobs N]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

from analysis import validate_replay as vr
from analysis.reproducible import windows_of


class _Obs(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reason = {}                    # (user, asset, seq, hl) -> reason
        self._rp = None
        _Obs.last = self

    def after_action(self, rp, block, a, fills, rejects, created):
        self._rp = rp
        n = len(self.unresolved.get((a.user, getattr(a, "asset", None)), ()))
        out = super().after_action(rp, block, a, fills, rejects, created)
        lst = self.unresolved.get((a.user, getattr(a, "asset", None)), ())
        if len(lst) > n:
            s, hl = lst[-1]
            res = rp.resolver
            why = (res._last_reason or "unknown") if res is not None else "no_resolver"
            self.reason[(a.user, a.asset, s, hl)] = why
            self.scored_flag[(a.user, a.asset, s, hl)] = block.height >= rp.warmup_end_height
        return out

    scored_flag = None


def one(job):
    window, hl_data, policy, kw = job
    _Obs.scored_flag = {}
    res, rows = vr.run(window, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
    obs = _Obs.last
    out = {"cancels": Counter(), "miss": Counter(), "txs": Counter(), "real": 0, "miss_total": 0, "flag_miss": 0}
    for k, why in obs.reason.items():
        out["cancels"][("scored" if _Obs.scored_flag.get(k) else "leadin", why)] += 1
    byhash = {r["hash"]: r for r in rows}
    resolver = obs._rp.resolver if obs._rp is not None else None
    exact = resolver.est.exact if resolver is not None else {}
    for rec in obs.txs.values():
        r = byhash.get(rec.hash)
        if r is None or r["warmup"] or r["status"] not in ("match", "mismatch"):
            continue
        out["real"] += r["real_notional"]
        miss = r["real_notional"] - r["matched_notional"]
        out["miss_total"] += miss
        if "unresolved cancel" not in r["flags"] or miss <= 0:
            continue
        out["flag_miss"] += miss
        hits = []
        for px, sz, maker, oid in rec.engine:
            est = resolver.hl.get(oid) if resolver is not None else None
            if est is None:
                continue
            e, tol = est
            placed = obs.placed_seq.get(oid, 0)
            for s, hl in obs.unresolved.get((maker, rec.asset), ()):
                if placed < s < rec.seq and abs(e - hl) <= tol:
                    why = obs.reason.get((maker, rec.asset, s, hl), "unknown")
                    hits.append("flag from an exact cancel" if hl in exact else why)
        if not hits:
            continue
        c = Counter(hits)
        for why, n in c.items():
            out["miss"][why] += miss * n / len(hits)
        out["txs"][c.most_common(1)[0][0]] += 1
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows", nargs="+")
    ap.add_argument("--jobs", type=int, default=4)
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    kw = vr.replay_kwargs(a)
    jobs = [(w, a.hl_data, a.policy, kw) for w in windows_of(a.windows)]
    with Pool(a.jobs) as pool:
        outs = pool.map(one, jobs)
    tot = {"cancels": Counter(), "miss": Counter(), "txs": Counter(), "real": 0, "miss_total": 0, "flag_miss": 0}
    for o in outs:
        for k in ("cancels", "miss", "txs"):
            tot[k].update(o[k])
        for k in ("real", "miss_total", "flag_miss"):
            tot[k] += o[k]
    R = max(tot["real"], 1)
    print(f"{len(jobs)} windows. missed real $: {tot['miss_total'] / R:.3f} of real; on txs flagged unresolved cancel "
          f"{tot['flag_miss'] / R:.3f}")
    print("\nunresolved int-oid cancels by reason:")
    for part in ("leadin", "scored"):
        n = sum(v for (p, _), v in tot["cancels"].items() if p == part)
        print(f"  {part}: {n}")
        for (p, why), v in sorted(tot["cancels"].items(), key=lambda kv: -kv[1]):
            if p == part:
                print(f"    {why:28} {v:6d}  {v / max(n, 1):.3f}")
    print("\nmissed real $ on flagged txs, split by the reason of the cancels behind the flag (share of all real $):")
    for why, v in tot["miss"].most_common():
        print(f"  {why:28} {v / R:.4f}   txs led by it: {tot['txs'][why]}")


if __name__ == "__main__":
    sys.exit(main())
