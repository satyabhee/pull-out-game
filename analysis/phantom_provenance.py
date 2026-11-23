"""phantom_provenance: where do the engine's phantom makers come from? (scored, mismatched txs only)

A phantom maker fill = an engine fill whose maker (not a placeholder) is not among the tx's real makers. Each is
classified by the order's origin and by what its owner did between placement and the fill:
  origin     ALO / GTC placed in the window (replay placed_by), or "pre-window known" (seeded known order)
  unres      the owner had an int-oid cancel/modify the engine could not apply, after placement (any oid)
  requote    the owner placed another order on the same asset and side after it (a market maker that requoted
             usually cancelled the old order: a cancel we applied to another order, or never saw)
  margin     the owner has a plan-time margin snapshot (clearinghouseState.json) / is short by the ALO rule
Also: age of the order at the fill (ms, block time).

usage: python -m analysis.phantom_provenance <dirs...> --hl-data H [replay options] [--jobs N]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

from analysis import validate_replay as vr
from analysis.reproducible import windows_of
from data import oid_facts
from engine_py.matching import PLACEHOLDER
from engine_py.types import Modify, Place


class _Obs(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.side_places = defaultdict(list)       # (user, asset, side) -> [seq]
        self.place_time = {}                        # engine oid -> block time ms
        self.tx_time = {}                           # tx hash -> block time ms
        self._rp = None
        _Obs.last = self

    def before_action(self, rp, block, a):
        super().before_action(rp, block, a)
        p = a.new if isinstance(a, Modify) else a
        if isinstance(p, Place):
            self.side_places[(a.user, p.asset, p.side)].append(self.seq)
            self.tx_time.setdefault(a.hash, block.time_ms)

    def after_action(self, rp, block, a, fills, rejects, created):
        self._rp = rp
        if created is not None:
            self.place_time[created] = block.time_ms
        return super().after_action(rp, block, a, fills, rejects, created)


def _bucket(ms):
    if ms is None:
        return "?"
    for lim, name in ((1_000, "<1s"), (10_000, "1-10s"), (60_000, "10-60s"), (600_000, "1-10min")):
        if ms < lim:
            return name
    return ">10min"


def one(job):
    window, hl_data, policy, kw = job
    res, rows = vr.run(window, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
    obs, rp = _Obs.last, _Obs.last._rp
    byhash = {r["hash"]: r for r in rows}
    out = Counter()
    exact_inv = {pos: hl for hl, pos in rp.resolver.est.exact.items()} if rp.resolver is not None else {}
    final = {}                                      # (user, real oid) -> (status_ts, status), latest status
    for o in oid_facts.load(window, hl_data).orders:
        k = (o["user"], int(o["oid"]))
        if o.get("status") and o.get("status_ts") is not None and (k not in final or o["status_ts"] >= final[k][0]):
            final[k] = (o["status_ts"], o["status"])
    for rec in obs.txs.values():
        r = byhash.get(rec.hash)
        if r is None or r["warmup"] or r["status"] != "mismatch":
            continue
        real_makers = {m for _, _, m in rec.real}
        for px, sz, maker, oid in rec.engine:
            if maker == PLACEHOLDER or maker in real_makers:
                continue
            act = rp.placed_by.get(oid)
            p = act.new if isinstance(act, Modify) else act
            origin = "pre-window known" if p is None else p.tif.name
            placed = obs.placed_seq.get(oid, 0)
            unres = any(placed < s < rec.seq for s, _ in obs.unresolved.get((maker, rec.asset), ()))
            side = rec.side.opposite
            requote = any(placed < s < rec.seq for s in obs.side_places.get((maker, rec.asset, side), ()))
            snap = maker.lower() in rp._margin if rp._margin else False
            short = bool(snap and p is not None and rp._margin_short(p, "diag"))
            t0, t1 = obs.place_time.get(oid), obs.tx_time.get(rec.hash)
            age = None if t0 is None or t1 is None else t1 - t0
            out[("origin", origin)] += 1
            out[("owner", f"unres={unres} requote={requote}")] += 1
            out[("origin x owner", f"{origin} unres={unres} requote={requote}")] += 1
            out[("margin", "short" if short else "snapshot, not short" if snap else "no snapshot")] += 1
            out[("age", f"{origin} {_bucket(age)}")] += 1
            out[("total", "")] += 1
            hl = exact_inv.get((p.block, p.tx_index, p.item_index)) if p is not None else None
            st = final.get((maker.lower(), hl)) if hl is not None else None
            if hl is None:
                truth = "no exact real oid"
            elif st is None:
                truth = "real oid known, no status fact"
            elif st[1] == "open" or st[0] > (t1 or 0):
                truth = f"live at fill ({st[1]} later)"
            else:
                truth = f"{st[1]} before fill"
            out[("truth", truth)] += 1
            out[("truth x requote", f"{truth} requote={requote}")] += 1
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows", nargs="+")
    ap.add_argument("--jobs", type=int, default=1)
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    kw = vr.replay_kwargs(a)
    jobs = [(w, a.hl_data, a.policy, kw) for w in windows_of(a.windows)]
    tot = Counter()
    with Pool(a.jobs) as pool:
        for c in pool.imap_unordered(one, jobs):
            tot.update(c)
    n = tot[("total", "")]
    print(f"{len(jobs)} chunks; phantom maker fills (scored mismatches): {n}")
    for group in ("origin", "owner", "origin x owner", "margin", "age", "truth", "truth x requote"):
        print(group)
        for (g, k), v in sorted(tot.items(), key=lambda x: -x[1]):
            if g == group:
                print(f"  {v:6d} {v / max(1, n):6.1%}  {k}")


if __name__ == "__main__":
    main()
