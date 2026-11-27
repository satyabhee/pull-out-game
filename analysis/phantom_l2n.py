"""phantom_l2n: phantom maker or queue priority? Compare our order count with the chain's at the fill's level.

Population: same as analysis/phantom_provenance (scored mismatched txs; engine maker fills whose maker is not a
placeholder and not among the tx's real makers).
For each such fill at price p on the maker side S, at block B:
  ours = number of orders (nodes, placeholders included) at (S, p) in our book at the START of block B (just before
         the block's first action, so intra-block actions are excluded on both sides of the comparison)
  n    = the "n" (order count) of level p in the last recorder l2Book fast update (book_<coin>_fast, 5 levels per
         side) with time < B's block time; "level not in feed" when p is not among that update's 5 levels on S,
         "no feed" when there is no update within FEED_MAX_AGE_MS
Buckets: ours > n (phantom likely), ours == n (order likely real: queue priority), ours < n.
Also reported for the fills whose real oid is known exactly with a status fact (method check), and the feed age.

usage: python -m analysis.phantom_l2n <dirs...> --hl-data H [replay options] [--jobs 1] [--cases out.csv]
--cases: one row per fill (chunk, hash, block, coin, side, px, maker, ours, n, bucket, truth).
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

from analysis import validate_replay as vr
from analysis.phantom_provenance import _Obs as _ProvObs
from analysis.reproducible import windows_of
from data import oid_facts
from data.parse import _hour_files, _records
from engine_py.matching import PLACEHOLDER
from engine_py.types import ASSETS, Modify, Side

FEED_MAX_AGE_MS = 5_000
DEPTH = 10                                         # our levels recorded per side at each block start


class _Obs(_ProvObs):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._height = None
        self.start_counts = {}                     # (height, asset, side) -> {px ticks: (orders, placeholders)}
        self.block_time = {}                       # height -> block time ms

    def before_action(self, rp, block, a):
        if block.height != self._height:
            self._height = block.height
            self.block_time[block.height] = block.time_ms
            for asset, book in rp.engine.books.items():
                for side in (Side.BUY, Side.SELL):
                    d = {}
                    for i, lv in enumerate(book.levels(side)):
                        if i == DEPTH:
                            break
                        nodes = list(lv)
                        d[lv.px] = (len(nodes), sum(1 for nd in nodes if nd.order.user == PLACEHOLDER))
                    self.start_counts[(block.height, asset, side)] = d
        super().before_action(rp, block, a)


def load_fast(hl_data, asset, t0, t1):
    """[(time, {Side: {px ticks: n}})] of book_<coin>_fast updates in [t0, t1], time order."""
    a = ASSETS[asset]
    seen = {}
    for path in _hour_files(Path(hl_data), f"book_{a.name}_fast", t0, t1):
        for rec in _records(path, t0, t1):
            d = rec["msg"]["data"]
            if t0 <= d["time"] <= t1:
                seen[d["time"]] = d["levels"]
    out = []
    for t in sorted(seen):
        bids, asks = seen[t]
        out.append((t, {Side.BUY: {a.px_to_ticks(l["px"]): l["n"] for l in bids},
                        Side.SELL: {a.px_to_ticks(l["px"]): l["n"] for l in asks}}))
    return out


def one(job):
    window, hl_data, policy, kw = job
    res, rows = vr.run(window, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
    obs, rp = _Obs.last, _Obs.last._rp
    byhash = {r["hash"]: r for r in rows}
    out = Counter()
    cases = []
    exact_inv = {pos: hl for hl, pos in rp.resolver.est.exact.items()} if rp.resolver is not None else {}
    final = {}
    for o in oid_facts.load(window, hl_data).orders:
        k = (o["user"], int(o["oid"]))
        if o.get("status") and o.get("status_ts") is not None and (k not in final or o["status_ts"] >= final[k][0]):
            final[k] = (o["status_ts"], o["status"])
    feeds = {a: load_fast(hl_data, a, rp.t0 - 60_000, rp.t1) for a in ASSETS}
    ftimes = {a: [t for t, _ in f] for a, f in feeds.items()}
    for rec in obs.txs.values():
        r = byhash.get(rec.hash)
        if r is None or r["warmup"] or r["status"] != "mismatch":
            continue
        real_makers = {m for _, _, m in rec.real}
        side = rec.side.opposite
        bt = obs.block_time.get(rec.block)
        for px, sz, maker, oid in rec.engine:
            if maker == PLACEHOLDER or maker in real_makers:
                continue
            ours, ph = obs.start_counts.get((rec.block, rec.asset, side), {}).get(px, (0, 0))
            i = bisect.bisect_left(ftimes[rec.asset], bt) - 1 if bt is not None else -1
            if i < 0 or bt - ftimes[rec.asset][i] > FEED_MAX_AGE_MS:
                bucket, age = "no feed", None
            else:
                age = bt - ftimes[rec.asset][i]
                n = feeds[rec.asset][i][1][side].get(px)
                if n is None:
                    bucket = "level not in feed"
                elif ours == 0:
                    bucket = "ours 0 at block start (order arrived in-block)"
                else:
                    bucket = "ours > n" if ours > n else "ours == n" if ours == n else "ours < n"
                    if ph:
                        bucket += " (level has a placeholder)"
            act = rp.placed_by.get(oid)
            p = act.new if isinstance(act, Modify) else act
            hl = exact_inv.get((p.block, p.tx_index, p.item_index)) if p is not None else None
            st = final.get((maker.lower(), hl)) if hl is not None else None
            t1 = obs.tx_time.get(rec.hash) or 0
            if st is None:
                truth = None
            elif st[1] == "open" or st[0] > t1:
                truth = "truth: live at fill"
            else:
                truth = f"truth: {st[1]} before fill"
            out[("all", bucket)] += 1
            cases.append({"chunk": Path(window).name, "hash": rec.hash, "block": rec.block, "coin": ASSETS[rec.asset].name,
                          "side": side.name, "px": ASSETS[rec.asset].ticks_to_px(px), "maker": maker, "ours": ours,
                          "n": "" if bucket in ("no feed", "level not in feed") else feeds[rec.asset][i][1][side].get(px),
                          "bucket": bucket, "truth": truth or ""})
            if truth:
                out[("truth", f"{truth} | {bucket}")] += 1
            if age is not None:
                out[("age", "<200ms" if age < 200 else "200-500ms" if age < 500 else "500ms-1s" if age < 1000
                     else ">=1s")] += 1
            out[("total", "")] += 1
    return out, cases


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows", nargs="+")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--cases", default=None, help="write one row per fill to this CSV")
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    kw = vr.replay_kwargs(a)
    jobs = [(w, a.hl_data, a.policy, kw) for w in windows_of(a.windows)]
    tot, rows = Counter(), []
    with Pool(a.jobs, maxtasksperchild=1) as pool:      # a fresh worker per chunk: memory does not build up
        for c, cs in pool.imap_unordered(one, jobs):
            tot.update(c)
            rows += cs
    if a.cases:
        import csv
        with open(a.cases, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["chunk", "hash", "block", "coin", "side", "px", "maker", "ours", "n",
                                              "bucket", "truth"])
            w.writeheader()
            w.writerows(sorted(rows, key=lambda r: (r["chunk"], r["block"], r["hash"])))
    n = tot[("total", "")]
    print(f"{len(jobs)} chunks; phantom maker fills: {n}")
    for group in ("all", "truth", "age"):
        print(group + (" (feed age at the block)" if group == "age" else ""))
        for (g, k), v in sorted(tot.items(), key=lambda x: -x[1]):
            if g == group:
                print(f"  {v:6d} {v / max(1, n):6.1%}  {k}")


if __name__ == "__main__":
    main()
