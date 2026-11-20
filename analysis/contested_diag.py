"""Why contested txs miss, by failure mode of the consumed prefix (the only state a taker's fills depend on).

For each scored CONTESTED tx (a real maker also cancels / modifies / posts ALO on that asset in that block),
our queue at every price the real taker reached is captured just before the tx runs, then compared with the
real fills slice by slice. At the first price where the makers differ (Y = the real maker we did not fill,
X = the maker we filled instead):
  F2 level missing     we held nothing at that price
  F2 gate rejected     Y has no order there; Y's ALO at that price was rejected by our gate
  F2 removed early     Y placed there in our data, but our order is gone (how: cancel / fill / evict / learn)
  F2 pre-window        Y never placed there in our data and no placeholder covers it
  F1 stale (cancel)    X ahead of Y; X has an int-oid cancel/modify we could not resolve after placing it
  F1 X ahead, other    X ahead of Y with no unresolved cancel (rejected on chain, cloid cancel we do not hold,
                       or filled on chain by a taker we did not fill)
  F3 order             X ahead of Y but placed AFTER Y in our data (queue position wrong)
  F4 size              same makers in order, different sizes
Also, at the start of each contested tx's block, our best level on the maker side against the recorder's bbo
(price, size, ORDER COUNT n) from the previous block: how often our touch already disagrees with the chain's.

usage: python -m analysis.contested_diag <chunk-parent dir> --hl-data H [replay options] [--jobs 2]
"""
from __future__ import annotations

import argparse
import bisect
import sys
import traceback
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path

from analysis import validate_replay as vr
from analysis.contested import block_actors
from analysis.exact_gone import CAUSE
from analysis.reproducible import windows_of
from data.parse import iter_trades
from engine_py.matching import PLACEHOLDER
from engine_py.replay import COINS, iter_bbo
from engine_py.types import Modify, Place, Side, Tif, ZERO_HASH

DEPTH = 10


def trade_hashes(hl_data, t0, t1) -> set:
    return {tr.hash for tr in iter_trades(hl_data, COINS, t0, t1) if tr.hash != ZERO_HASH}


class _Obs(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.snap = {}                 # hash -> {px: [(user, remaining, oid, placeholder)]}
        self.touch_start = {}          # (height, asset, maker side) -> (px, sz, n) at block start
        self.removed = {}              # engine oid -> cause
        self._h = None
        self._rpref = None
        self._btime = {}
        self.wrapped = False
        self.want = set()
        _Obs.last = self

    def _wrap(self, rp):
        self.wrapped = True
        for book in rp.engine.books.values():
            orig = book.remove

            def remove(oid, orig=orig):
                if oid not in self.removed:
                    names = [f.name for f in traceback.extract_stack(limit=12)]
                    self.removed[oid] = next((c for n, c in CAUSE if n in names), "other")
                return orig(oid)
            book.remove = remove

    def before_action(self, rp, block, a):
        if not self.wrapped:
            self._wrap(rp)
            self._rpref = rp
        self._btime[block.height] = block.time_ms
        if block.height != self._h:
            self._h = block.height
            for asset, book in rp.engine.books.items():
                for side in (Side.BUY, Side.SELL):
                    lv = book.best_level(side)
                    self.touch_start[(block.height, asset, side)] = (
                        None if lv is None else (lv.px, lv.total, len(lv)))
        p = a.new if isinstance(a, Modify) else a
        if isinstance(p, Place) and p.tif is not Tif.ALO and a.hash in self.want and a.hash not in self.snap:
            levels = rp.engine.books[p.asset].side(p.side.opposite)
            keys = list(levels.keys())
            keys = keys[-DEPTH:][::-1] if p.side.opposite is Side.BUY else keys[:DEPTH]
            self.snap[a.hash] = {px: [(nd.order.user, nd.remaining, nd.order.oid, nd.order.user == PLACEHOLDER)
                                      for nd in levels[px]] for px in keys}
        return super().before_action(rp, block, a)


def classify(obs, rp, rec, queue):
    """First divergence between the real fills and our queue at the prices the real taker reached."""
    side = rec.side.opposite
    real = defaultdict(list)
    for px, sz, m in rec.real:
        real[px].append((m, sz))
    eng = defaultdict(list)
    for px, sz, m, oid in rec.engine:
        eng[px].append((m, sz, oid))
    for px in sorted(real, reverse=side is Side.BUY):
        q = queue.get(px)
        rm = [m for m, _ in real[px]]
        em = [m for m, _, _ in eng.get(px, [])]
        if [x if x != PLACEHOLDER else None for x in em] == rm or (
                len(em) == len(rm) and all(e == PLACEHOLDER or e == r for e, r in zip(em, rm))):
            if sorted(s for _, s in real[px]) != sorted(s for _, s, _ in eng.get(px, [])):
                return "F4 size"
            continue
        if not q:
            return "F2 level missing"
        users = [u for u, _, _, _ in q]
        y = next((m for m in rm if m not in em), None)
        x = next((e for e in em if e != PLACEHOLDER and e not in rm), None)
        if y is None:
            if x is not None:
                obs.detail = (x, px)
            return "F3 order" if x is None else "F1 X ahead, other"
        if y not in users:
            if obs.first_quote.get((y, rec.asset, side, px), rec.seq) < rec.seq:
                alo = [acc for s, _, acc in obs.alo_ids.get((y, rec.asset, side, px), ()) if s < rec.seq]
                if alo and not any(alo):
                    return "F2 gate rejected"
                mine = [oid for oid, s in obs.placed_seq.items() if s < rec.seq
                        and (rp.placed.get(oid) is not None and rp.placed[oid].px == px and
                             rp.placed[oid].side is side and rp.placed[oid].asset == rec.asset)
                        and rp.placed_by[oid].user == y]
                causes = Counter(obs.removed.get(o, "never") for o in mine)
                return "F2 removed early: " + (causes.most_common(1)[0][0] if causes else "not created")
            if any(ph for _, _, _, ph in q):
                return "F2 pre-window, placeholder short"
            return "F2 pre-window"
        if x is None:
            return "F3 order"
        ix = users.index(x) if x in users else -1
        iy = users.index(y)
        if ix >= 0 and ix < iy:
            xo = q[ix][2]
            xs = obs.placed_seq.get(xo, 0)
            ys = obs.placed_seq.get(q[iy][2], 0)
            if any(xs < s < rec.seq for s, _ in obs.unresolved.get((x, rec.asset), ())):
                return "F1 stale (cancel)"
            if xs > ys > 0:
                return "F3 order"
            obs.detail = (x, px)
            return "F1 X ahead, other"
        return "F3 order"
    return "match"


def one(job):
    window, hl_data, policy, kw = job
    window = Path(window)
    import json
    m = json.loads((window / "window.json").read_text())
    t0, t1 = m.get("first_block_time", 0), m.get("last_block_time", 0) + 60_000
    want, actors = trade_hashes(hl_data, t0 - 60_000, t1), {}

    def factory(*a, **k):
        o = _Obs(*a, **k)
        o.want = want
        return o
    res, rows = vr.run(window, hl_data, policy, quiet=True, observer_cls=factory, **kw)
    obs = _Obs.last
    out = {"modes": Counter(), "modes_w": Counter(), "touch": Counter(), "n": 0, "cascade": Counter(),
           "cascade_w": Counter()}
    real_trades, maker_trades = defaultdict(list), defaultdict(list)
    for tr in iter_trades(hl_data, COINS, t0 - 120_000, t1):
        if tr.hash != ZERO_HASH:
            real_trades[tr.hash].append(tr)
        maker_trades[(tr.maker, tr.asset, tr.taker_side.opposite, tr.px)].append(tr)
    byhash = {r["hash"]: r for r in rows}
    bbo = {}
    for coin in COINS:
        for t, asset, b, k in iter_bbo(hl_data, coin, t0 - 60_000, t1):
            bbo.setdefault(asset, []).append((t, b, k))
    for h, rec in obs.txs.items():
        r = byhash.get(h)
        if r is None or r["warmup"] or r["status"] not in ("match", "mismatch") or r["real_notional"] <= 0:
            continue
        if rec.block not in actors:
            p = window / f"{rec.block}.json.gz"
            actors[rec.block] = block_actors(p) if p.exists() else {}
        acts = actors[rec.block].get(rec.asset, set())
        if not any(mk[:10] in acts for _, _, mk in rec.real):
            continue
        miss = 1 - r["matched_notional"] / r["real_notional"]
        out["n"] += 1
        obs.detail = None
        mode = classify(obs, obs._rpref, rec, obs.snap.get(h, {})) if h in obs.snap else "no snapshot"
        if mode == "F1 X ahead, other" and obs.detail is not None:
            x, xpx = obs.detail
            bt = obs._btime.get(rec.block)
            first_seq = min((t.seq for t in real_trades.get(h, [])), default=None)
            hits = [t for t in maker_trades.get((x, rec.asset, rec.side.opposite, xpx), ())
                    if bt - 60_000 <= t.time_ms <= bt and t.hash != h]
            same = [t for t in hits if t.time_ms == bt and (first_seq is None or t.seq < first_seq)]
            where = ("same block, earlier tx" if same else
                     "earlier block" if any(t.time_ms < bt for t in hits) else "no real fill of X there")
            out["cascade"][where] += 1
            out["cascade_w"][where] += miss
        out["modes"][mode] += 1
        out["modes_w"][mode] += miss
        side = rec.side.opposite
        ts = obs.touch_start.get((rec.block, rec.asset, side))
        bt = obs._btime.get(rec.block)
        lst = bbo.get(rec.asset, [])
        i = bisect.bisect_left([x[0] for x in lst], bt) - 1 if bt is not None else -1
        if i >= 0 and ts is not None:
            _, b, k = lst[i]
            real = b if side is Side.BUY else k
            if real is None:
                continue
            px_ok = ts[0] == real[0]
            out["touch"]["blocks"] += 1
            out["touch"]["px_match"] += px_ok
            if px_ok:
                out["touch"]["n_match"] += ts[2] == real[2]
                out["touch"]["n_more_than_chain"] += ts[2] > real[2]
                out["touch"]["n_fewer_than_chain"] += ts[2] < real[2]
                out["touch"]["sz_match"] += ts[1] == real[1]
                out["touch"]["miss_when_n_match"] += miss if ts[2] == real[2] else 0
                out["touch"]["miss_when_n_differs"] += miss if ts[2] != real[2] else 0
                out["touch"]["n_eq_cnt_tx"] += ts[2] == real[2]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows", nargs="+")
    ap.add_argument("--jobs", type=int, default=2)
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    kw = vr.replay_kwargs(a)
    jobs = [(str(w), a.hl_data, a.policy, kw) for w in windows_of(a.windows)]
    with Pool(a.jobs) as pool:
        outs = pool.map(one, jobs)
    tot = {"modes": Counter(), "modes_w": Counter(), "touch": Counter(), "n": 0, "cascade": Counter(),
           "cascade_w": Counter()}
    for o in outs:
        for k in ("modes", "modes_w", "touch", "cascade", "cascade_w"):
            tot[k].update(o[k])
        tot["n"] += o["n"]
    W = sum(tot["modes_w"].values())
    print(f"{tot['n']} contested txs; per-tx miss total {W:.1f} (mean {W / max(tot['n'], 1):.3f})")
    print(f"  {'first divergence':34} {'txs':>4} {'share of miss':>13}")
    for mode in sorted(tot["modes"], key=lambda m: -tot["modes_w"][m]):
        print(f"  {mode:34} {tot['modes'][mode]:4d} {tot['modes_w'][mode] / max(W, 1e-9):13.3f}")
    if tot["cascade"]:
        cw = sum(tot["cascade_w"].values())
        print("\n'F1 X ahead, other': where the chain had consumed X's order at that price before this tx (60 s back):")
        for k, n in tot["cascade"].most_common():
            print(f"  {k:28} {n:4d} txs  {tot['cascade_w'][k] / max(cw, 1e-9):.3f} of that miss")
    t = tot["touch"]
    if t["blocks"]:
        pm = max(t["px_match"], 1)
        print(f"\nour best maker-side level at block start vs the chain's bbo (previous block), {t['blocks']} txs:")
        print(f"  same price {t['px_match'] / t['blocks']:.3f}; when same price: same order count {t['n_match'] / pm:.3f}, "
              f"we hold MORE orders {t['n_more_than_chain'] / pm:.3f}, FEWER {t['n_fewer_than_chain'] / pm:.3f}, "
              f"same size {t['sz_match'] / pm:.3f}")
        nm, nd = t["n_eq_cnt_tx"], pm - t["n_eq_cnt_tx"]
        print(f"  mean per-tx miss when the order count matches {t['miss_when_n_match'] / max(nm, 1):.3f} ({nm} txs), "
              f"when it differs {t['miss_when_n_differs'] / max(nd, 1):.3f} ({nd} txs)")


if __name__ == "__main__":
    sys.exit(main())
