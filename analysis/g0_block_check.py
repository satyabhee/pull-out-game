"""G0 check: replay a window and compare the engine's taker fills with recorded trades.

    python -m analysis.g0_block_check <window_dir> --hl-data <dir> [--height H] [--reconcile] [--all]

Runs engine_py/replay.Replay in listing order (the "actual" policy is decided at G1),
without the int-oid resolver (the G0 setup); rule 8 reconciliation only with
--reconcile. Compares, per taker tx, the engine's fills with the real ones on price and size.

Prints a detailed table for one block (default: the first BTC block with a linked
trade). With --all, also scores every linked taker tx in the window.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from data.parse import iter_trades, nearest_book
from engine_py.matching import PLACEHOLDER
from engine_py.replay import COINS, Observer, Replay, listing
from engine_py.types import ASSETS, Modify, OutsideFill, Place, ZERO_HASH


def by_px(fills):
    """[(px, sz)] aggregated per price, best first for the taker is irrelevant: sorted."""
    agg = Counter()
    for px, sz in fills:
        agg[px] += sz
    return sorted(agg.items())


def classify(real, engine) -> str:
    r, e = by_px(real), by_px(engine)
    if r == e:
        return "exact"
    if not e:
        return "engine no fill"
    rs, es = sum(s for _, s in r), sum(s for _, s in e)
    if rs == es:
        return "same size, other prices"
    return "engine filled less" if es < rs else "engine filled more"


def top(book, side, n=4):
    out = []
    for lv in book.levels(side):
        ph = sum(nd.remaining for nd in lv if nd.order.user == PLACEHOLDER)
        out.append((lv.px, lv.total, ph))
        if len(out) == n:
            break
    return out


class _G0Observer(Observer):
    """Collect engine taker fills by hash and the opposite book before the chosen block's takers."""

    def __init__(self, real, height):
        self.real, self.height = real, height
        self.engine_fills = defaultdict(list)          # taker hash -> [(px, sz, maker)]
        self.before = {}                               # taker hash -> opposite top levels before it ran

    def before_action(self, rp, block, a):
        if block.height == self.height and isinstance(a, (Place, Modify)) and a.hash in self.real \
                and a.hash not in self.before:
            p = a if isinstance(a, Place) else a.new
            self.before[a.hash] = top(rp.engine.books[p.asset], p.side.opposite)

    def after_action(self, rp, block, a, fills, rejects, created):
        if not isinstance(a, OutsideFill):
            for f in fills:
                self.engine_fills[f.taker_hash].append((f.px, f.sz, f.maker_user))


def run(window, hl_data, height=None, reconcile=False, score_all=False):
    rp = Replay(window, hl_data, listing, use_resolver=False)
    if not reconcile:
        rp.snapshots = []
    blocks, t0, t1 = rp.blocks, rp.t0, rp.t1
    for coin in COINS:
        snap = nearest_book(hl_data, coin, t0)
        print(f"seeded {coin} from snapshot {rp.seed_offsets[coin]:+d} ms vs first block: "
              f"{len(snap.bids)} bid / {len(snap.asks)} ask levels")
    snaps = rp.snapshots

    real = defaultdict(list)          # taker hash -> [(px, sz, maker)]
    real_meta = {}
    for tr in iter_trades(hl_data, COINS, t0, t1):
        if tr.hash != ZERO_HASH:
            real[tr.hash].append((tr.px, tr.sz, tr.maker))
            real_meta[tr.hash] = (tr.asset, tr.taker, tr.taker_side)

    hash_block = {}
    for b in blocks:
        for a in b.actions:
            if isinstance(a, (Place, Modify)) and a.hash in real:
                hash_block.setdefault(a.hash, b.height)
    if height is None:
        btc = sorted(h for k, h in hash_block.items() if real_meta[k][0] == 0)
        if not btc:
            raise SystemExit("no linked BTC trade in this window")
        height = btc[0]

    obs = _G0Observer(real, height)
    rp.observer = obs
    for _ in rp:
        pass
    engine_fills, before = obs.engine_fills, obs.before
    recon = Counter({(coin, k): v for (coin, k), v in rp.reconciled.items()})

    # ---- the one block
    print(f"\nblock {height}  (policy: listing order, reconcile={'on' if reconcile else 'off'})")
    hashes = [h for h, hb in hash_block.items() if hb == height]
    cats = Counter()
    for h in hashes:
        asset, taker, side = real_meta[h]
        spec = ASSETS[asset]
        r = [(px, sz) for px, sz, _ in real[h]]
        e = [(px, sz) for px, sz, _ in engine_fills.get(h, [])]
        cat = classify(r, e)
        cats[cat] += 1
        fmt = lambda xs: ", ".join(f"{spec.lots_to_sz(s)}@{spec.ticks_to_px(p)}" for p, s in by_px(xs)) or "none"
        print(f"\n  {spec.name} taker {taker[:10]} {side.name}  tx {h[:12]}  ->  {cat.upper()}")
        print(f"    real   : {fmt(r)}")
        print(f"    engine : {fmt(e)}")
        rm = sorted({m[:10] for _, _, m in real[h]})
        em = sorted({m[:10] for _, _, m in engine_fills.get(h, [])})
        print(f"    makers : real {rm}  engine {em}")
        lv = before.get(h, [])
        print("    book before (px, size, placeholder size): " +
              "; ".join(f"{spec.ticks_to_px(p)} {spec.lots_to_sz(t)} ({spec.lots_to_sz(ph)})" for p, t, ph in lv))
    print(f"\n  block {height}: " + ", ".join(f"{k} {v}" for k, v in cats.most_common()))

    if score_all:
        overall = Counter()
        per_min = defaultdict(Counter)
        for h, hb in hash_block.items():
            cat = classify([(p, s) for p, s, _ in real[h]], [(p, s) for p, s, _ in engine_fills.get(h, [])])
            overall[cat] += 1
            minute = (next(b.time_ms for b in blocks if b.height == hb) - t0) // 60_000
            per_min[minute][cat == "exact"] += 1
        n = sum(overall.values())
        print(f"\nall linked taker txs in window: {n}")
        for k, v in overall.most_common():
            print(f"  {k:<26}{v:>5}  {100 * v / n:5.1f}%")
        for m in sorted(per_min):
            c = per_min[m]
            print(f"  minute {m}: exact {c[True]}/{c[True] + c[False]}")
    if reconcile:
        print(f"\nreconciliation over the window ({len(snaps)} snapshots):")
        for coin in COINS:
            spec = ASSETS[COINS.index(coin)]
            print(f"  {coin}: " + ", ".join(f"{k} {spec.lots_to_sz(recon[(coin, k)])}" for k in ("trimmed", "drift", "missing")))
    return cats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("window")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--height", type=int)
    ap.add_argument("--reconcile", action="store_true")
    ap.add_argument("--all", action="store_true", help="score every linked taker tx in the window")
    a = ap.parse_args(argv)
    run(a.window, a.hl_data, a.height, a.reconcile, a.all)


if __name__ == "__main__":
    main()
