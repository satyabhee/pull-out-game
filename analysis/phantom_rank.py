"""Which known orders at the touch are phantoms? Victim rankings scored against tracked users' ground truth.

At every block start where our best level on a side is at the chain's bbo price (end of the previous block) and
holds MORE orders than the bbo count n (no placeholder at that level), k = ours - n orders must go. Each ranking
picks k victims among our known orders there:
  oldest     front of the queue first (what reconcile / touch-n-cap do today)
  newest     back of the queue first
  evidence   owners with an int-oid cancel we could not resolve after the order was placed first (the cancel's
             real oid within the resolver's tolerance of this order's estimate before any other), then newest
Truth, for orders of users with order-id facts whose real oid is known exactly (data/oids exact): PHANTOM if a
final status (canceled / filled / *Rejected) came at or before the previous block's time, REAL otherwise.
Reported: precision (victims with truth that are phantoms) per ranking, and the phantom base rate.
Run it with --no-oid-facts for a held-out test: the replay then cannot resolve the tracked users' cancels either,
so their orders go stale like everyone's, while their real statuses still judge the rankings.

usage: python -m analysis.phantom_rank <dirs...> --hl-data H [replay options] [--jobs 1]
"""
from __future__ import annotations

import argparse
import bisect
import sys
from collections import Counter
from multiprocessing import Pool

from analysis import validate_replay as vr
from analysis.reproducible import windows_of
from data import oid_facts
from engine_py.matching import PLACEHOLDER
from engine_py.replay import COINS, iter_bbo
from engine_py.types import Side


STATE_CAP = 200_000


def exact_removal(sizes: list[int], k: int, d: int):
    """A set of exactly k indices whose sizes sum to exactly d, preferring the oldest (lowest indices: smallest index
    sum). Returns (indices, ways) with ways = number of distinct index sets found (capped at 2), or (None, 0) when
    none exists, or (None, -1) when the search is too big."""
    dp = [dict() for _ in range(k + 1)]          # count -> {sum: (index_sum, ways, mask)}
    dp[0][0] = (0, 1, 0)
    for i, sz in enumerate(sizes):
        for c in range(min(i, k - 1), -1, -1):
            for tot, (score, ways, mask) in list(dp[c].items()):
                t2 = tot + sz
                if t2 > d:
                    continue
                cand = (score + i, ways, mask | (1 << i))
                cur = dp[c + 1].get(t2)
                if cur is None:
                    dp[c + 1][t2] = cand
                else:
                    w = min(2, cur[1] + ways)
                    dp[c + 1][t2] = (cand[0], w, cand[2]) if cand[0] < cur[0] else (cur[0], w, cur[2])
            if len(dp[c + 1]) > STATE_CAP:
                return None, -1
    hit = dp[k].get(d)
    if hit is None:
        return None, 0
    return [i for i in range(len(sizes)) if hit[2] >> i & 1], hit[1]


class _Obs(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._h = None
        self.out = Counter()
        self.ctx = None              # (bbo per asset, real oid status)
        _Obs.last = self

    def before_action(self, rp, block, a):
        if block.height != self._h:
            self._h = block.height
            if self.ctx is None:
                self.ctx = self._load(rp)
            self._check(rp, block)
        return super().before_action(rp, block, a)

    def _load(self, rp):
        bbo = {}
        for coin in COINS:
            for t, asset, b, k in iter_bbo(rp.hl_data, coin, rp.t0 - 60_000, rp.t1):
                bbo.setdefault(asset, []).append((t, b, k))
        fin = {}
        for o in oid_facts.load(rp.window_dir, rp.hl_data).orders:
            if o["status"] not in (None, "open") and o["status_ts"] is not None:
                fin[o["oid"]] = min(fin.get(o["oid"], o["status_ts"]), o["status_ts"])
        # exact real oid per placement from the facts, independent of whether the replay used them (held-out mode:
        # the replay runs with --no-oid-facts, so these users' cancels go unresolved like anyone's)
        from data.oids import OidEstimator
        est = OidEstimator.from_files(rp.window_dir, None, facts=True, hl_data=rp.hl_data)
        inv = {pos: hl for hl, pos in est.exact.items()}
        return bbo, fin, inv

    def _check(self, rp, block):
        bbo, fin, inv = self.ctx
        res = rp.resolver
        for asset, lst in bbo.items():
            times = [x[0] for x in lst]
            i = bisect.bisect_left(times, block.time_ms) - 1
            if i < 0:
                continue
            t_prev, b, k = lst[i]
            for side, lv in ((Side.BUY, b), (Side.SELL, k)):
                if lv is None:
                    continue
                level = rp.engine.books[asset].side(side).get(lv[0])
                if level is None:
                    continue
                nodes = list(level)
                if any(nd.order.user == PLACEHOLDER for nd in nodes) or len(nodes) <= lv[2]:
                    continue
                kx = len(nodes) - lv[2]
                self.out["levels_over"] += 1
                self.out["excess_orders"] += kx

                def truth(nd):
                    pa = rp.placed_by.get(nd.order.oid)
                    if pa is None:
                        return None
                    hl = inv.get((pa.block, pa.tx_index, pa.item_index))
                    if hl is None:
                        return None
                    f = fin.get(hl)
                    return "phantom" if f is not None and f <= t_prev else "real"

                def ev(nd):
                    oid = nd.order.oid
                    placed = self.placed_seq.get(oid, 0)
                    est = res.hl.get(oid) if res is not None else None
                    near = far = False
                    for s, hl in self.unresolved.get((nd.order.user, asset), ()):
                        if s > placed:
                            far = True
                            if est is not None and abs(est[0] - hl) <= est[1]:
                                near = True
                    return (2 if near else 1 if far else 0, self.placed_seq.get(oid, 0))

                ranks = {"oldest": nodes[:kx], "newest": nodes[::-1][:kx],
                         "evidence": sorted(nodes, key=ev, reverse=True)[:kx],
                         "evidence_then_oldest": sorted(nodes, key=lambda nd: (-ev(nd)[0], ev(nd)[1]))[:kx],
                         "near_then_oldest": sorted(nodes, key=lambda nd: (-(ev(nd)[0] == 2), ev(nd)[1]))[:kx]}
                evs = [ev(nd)[0] for nd in nodes]
                self.out["orders_at_over_levels"] += len(nodes)
                self.out["flagged_near"] += sum(e == 2 for e in evs)
                self.out["flagged_any"] += sum(e >= 1 for e in evs)
                ages = sorted(block.time_ms - rp.placed_by[nd.order.oid].block_time_ms
                              for nd in nodes if hasattr(rp.placed_by.get(nd.order.oid), "block_time_ms"))
                users = Counter(nd.order.user for nd in nodes)
                self.out["max_orders_one_user"] += users.most_common(1)[0][1]
                self.out["distinct_users"] += len(users)
                self.out["chain_n"] += lv[2]
                idx, ways = exact_removal([nd.remaining for nd in nodes], kx, sum(nd.remaining for nd in nodes) - lv[1])
                self.out["exact:levels"] += 1
                self.out["exact:" + ("too_big" if ways < 0 else "none" if ways == 0 else "unique" if ways == 1
                                     else "several")] += 1
                if idx is not None:
                    for name, picks in (("exact", [nodes[i] for i in idx]), ("oldest_same_levels", nodes[:kx])):
                        for nd in picks:
                            t = truth(nd)
                            if t is not None:
                                self.out[f"{name}:picked_with_truth"] += 1
                                self.out[f"{name}:picked_phantom"] += t == "phantom"
                for nd in nodes:
                    t = truth(nd)
                    if t is not None:
                        self.out["truth_orders"] += 1
                        self.out["truth_" + t] += 1
                for name, picks in ranks.items():
                    for nd in picks:
                        t = truth(nd)
                        if t is not None:
                            self.out[f"{name}:picked_with_truth"] += 1
                            self.out[f"{name}:picked_phantom"] += t == "phantom"


def one(job):
    window, hl_data, policy, kw = job
    vr.run(window, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
    return _Obs.last.out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows", nargs="+")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--last", type=int, default=0, help="only the last N windows")
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    kw = vr.replay_kwargs(a)
    ws = windows_of(a.windows)
    ws = ws[-a.last:] if a.last else ws
    with Pool(a.jobs) as pool:
        outs = pool.map(one, [(str(w), a.hl_data, a.policy, kw) for w in ws])
    t = Counter()
    for o in outs:
        t.update(o)
    print(f"{len(ws)} windows; block starts where our touch holds more orders than the chain: {t['levels_over']} "
          f"({t['excess_orders']} excess orders)")
    L = max(t["levels_over"], 1)
    print(f"per over-full level: ours {t['orders_at_over_levels'] / L:.1f} orders vs chain {t['chain_n'] / L:.1f}; "
          f"{t['distinct_users'] / L:.1f} distinct owners, the biggest owner holds {t['max_orders_one_user'] / L:.1f}; "
          f"orders whose owner has an unresolved cancel after placing: {t['flagged_any'] / L:.1f} "
          f"(near the cancel's oid: {t['flagged_near'] / L:.1f})")
    print(f"orders there with ground truth: {t['truth_orders']}; phantom base rate "
          f"{t['truth_phantom'] / max(t['truth_orders'], 1):.3f}")
    L2 = max(t["exact:levels"], 1)
    print(f"exact-sum search on {t['exact:levels']} over-full levels: unique {t['exact:unique'] / L2:.3f}, several "
          f"{t['exact:several'] / L2:.3f}, none {t['exact:none'] / L2:.3f}, too big {t['exact:too_big'] / L2:.3f}")
    for name in ("oldest", "newest", "evidence", "evidence_then_oldest", "near_then_oldest", "exact",
                 "oldest_same_levels"):
        n = t[f"{name}:picked_with_truth"]
        print(f"  {name:9} victims with truth {n:5d}; phantom precision {t[f'{name}:picked_phantom'] / max(n, 1):.3f}")


if __name__ == "__main__":
    sys.exit(main())
