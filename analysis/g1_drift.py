"""G1 book-drift triage (AGENT_PLAN 6.1): measure each lead as its own category before any fix.

    python -m analysis.g1_drift <window_dir> --hl-data <dir> [--anchors FILE] [--cache FILE] [--no-resolver]

Replays the window in listing order, seeded from the nearest L2 snapshot, with rule 8
reconciliation at every later snapshot (as g0_block_check --reconcile), and reports:
  (a) reduce-only orders: share of the known orders left resting, and of the drift lots
  (b) ALO would-cross rejects by what they cross: only placeholders, or known orders
      (and of those, reduce-only), plus the first-blocks rate
  (c) int-oid cancels through data/oids.py: resolved / pre-window / no candidate / ambiguous,
      leave-one-out anchor error, and correctness against orderStatus answers (--cache)
  (e) cloids reused while the first order still rests
plus resting-order counts, user concentration and the linked-taker match classes.
Nothing here changes engine behavior: it is identical under every ordering policy.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from analysis.g0_block_check import COINS, classify
from data.oids import OidEstimator, OidResolver
from data.parse import iter_books, iter_trades, iter_window, nearest_book
from engine_py.matching import PLACEHOLDER, Engine
from engine_py.types import (ASSETS, Cancel, CancelByCloid, Modify, OutsideFill, Place, RejectReason,
                             Side, Tif, ZERO_HASH)


def pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "   n/a"


def crossing_liquidity(eng, p: Place, placed: dict) -> Counter:
    """Lots on the opposite side that an ALO at p.px would cross, by owner kind."""
    out = Counter()
    for lv in eng.books[p.asset].levels(p.side.opposite):
        if not Engine._crosses(p.side, p.px, lv.px):
            break
        for nd in lv:
            if nd.order.user == PLACEHOLDER:
                out["placeholder"] += nd.remaining
            elif placed.get(nd.order.oid) is not None and placed[nd.order.oid].reduce_only:
                out["known_reduce_only"] += nd.remaining
            else:
                out["known"] += nd.remaining
    return out


def run(window, hl_data, anchors_path=None, cache_path=None, use_resolver=True, early_blocks=3,
        holdout=False, nearest_ratio=0, drop_reduce_only=False):
    blocks = list(iter_window(window, hl_data))
    t0, t1 = blocks[0].time_ms, blocks[-1].time_ms
    eng = Engine()
    for coin in COINS:
        snap = nearest_book(hl_data, coin, t0)
        for px, sz in snap.bids:
            eng.seed(snap.asset, Side.BUY, px, sz)
        for px, sz in snap.asks:
            eng.seed(snap.asset, Side.SELL, px, sz)
    snaps = sorted((s for coin in COINS for s in iter_books(hl_data, coin, t0, t1)), key=lambda s: (s.time_ms, s.asset))

    res = None
    if use_resolver and anchors_path:
        est = OidEstimator.from_files(window, anchors_path)
        if holdout:                                      # estimate from even anchors, judge on the odd ones
            held = {o for _, o in est.anchors[1::2]}
            est = OidEstimator(est.counts, est.anchors[::2])
        res = OidResolver(eng, est, nearest_ratio=nearest_ratio)
        eng.resolve_oid = res
        loo = sorted(abs(e) for e in est.leave_one_out())
        print(f"oid anchors: {len(est.anchors)}; leave-one-out |error| in ids: "
              + (f"median {loo[len(loo) // 2]}, p90 {loo[9 * len(loo) // 10]}, max {loo[-1]}" if loo else "n/a"))

    truth = {}                                           # (user, hl oid) -> orderStatus order
    if cache_path:
        for k, v in json.loads(Path(cache_path).read_text()).items():
            if v.get("status") == "order":
                u, o = k.split(":")
                truth[(u.lower(), int(o))] = v["order"]["order"]

    real = defaultdict(list)
    for tr in iter_trades(hl_data, COINS, t0, t1):
        if tr.hash != ZERO_HASH:
            real[tr.hash].append((tr.px, tr.sz))

    placed: dict[int, Place] = {}                        # engine oid -> the Place that created it
    alo = Counter()
    cancels = Counter()
    check = Counter()
    engine_fills = defaultdict(list)
    taker_hashes = set()
    drift = Counter()
    si = 0
    held = held if res is not None and holdout else None
    for bi, b in enumerate(blocks):
        for a in b.actions:
            p = a.new if isinstance(a, Modify) else a
            if drop_reduce_only and isinstance(p, Place) and p.reduce_only and p.tif is not Tif.IOC:
                check["dropped_reduce_only"] += 1        # measurement only: what if these never rested
                if not isinstance(a, Modify):
                    continue
                meta = dict(user=a.user, block=a.block, tx_index=a.tx_index, item_index=a.item_index,
                            hash=a.hash, priority=a.priority, asset=a.asset)
                a = (CancelByCloid(**meta, cloid=a.target) if isinstance(a.target, str)
                     else Cancel(**meta, oid=a.target))   # keep the modify's cancel half
                p = a
            pre = None
            if isinstance(p, Place) and p.tif is Tif.ALO:
                pre = crossing_liquidity(eng, p, placed)
            before = eng.next_oid
            fills, rejects = eng.apply(a)
            if eng.next_oid > before and isinstance(p, Place):
                placed[before] = p
                if res is not None:
                    res.note(a, before)
            reasons = {r.reason for r in rejects}
            if isinstance(p, Place) and p.tif is Tif.ALO:
                alo["total"] += 1
                early = bi < early_blocks
                alo["early_total"] += early
                if RejectReason.ALO_WOULD_CROSS in reasons:
                    alo["rejected"] += 1
                    alo["early_rejected"] += early
                    kind = ("crosses known reduce-only" if pre["known_reduce_only"] else
                            "crosses known orders" if pre["known"] else "crosses placeholders only")
                    alo[kind] += 1
            if isinstance(a, Cancel):
                cancels["oid_total"] += 1
                hit = RejectReason.UNKNOWN_ORDER not in reasons
                cancels["oid_resolved_and_removed"] += hit
                t = truth.get((a.user, a.oid))
                if t is not None and res is not None:
                    where = ("in-window" if t["timestamp"] >= t0 else "pre-window")
                    if held is not None and where == "in-window" and a.oid not in held:
                        t = None                         # an anchor the estimator was given: too easy
                if t is not None and res is not None:
                    check["with_truth"] += 1
                    check[f"truth {where}: {res_outcome(res)}"] += 1
                    if hit:
                        check["resolved"] += 1
                        o = placed.get(res.last)
                        spec = ASSETS[a.asset]
                        ok = (o is not None and o.px == spec.px_to_ticks(t["limitPx"])
                              and o.sz == spec.sz_to_lots(t["origSz"]) and o.side.value == t["side"])
                        check["resolved_correct" if ok else "resolved_WRONG"] += 1
            elif isinstance(a, CancelByCloid):
                cancels["cloid_total"] += 1
                if RejectReason.UNKNOWN_ORDER in reasons:
                    cancels["cloid_missed"] += 1
                    mine = [o for o in eng.by_user.get(a.user, {}) if o in eng.books[a.asset]]
                    cancels["cloid_missed_user_has_no_resting_order"] += not mine
            if not isinstance(a, OutsideFill):
                if isinstance(p, Place) and p.hash in real:
                    taker_hashes.add(p.hash)
                for f in fills:
                    engine_fills[f.taker_hash].append((f.px, f.sz))
        eng.end_block(b.height, b.time_ms)
        nxt = blocks[bi + 1].time_ms if bi + 1 < len(blocks) else t1 + 1
        while si < len(snaps) and snaps[si].time_ms < nxt:
            s = snaps[si]
            if s.time_ms >= b.time_ms:
                r = eng.reconcile(s.asset, s.bids, s.asks)
                name = ASSETS[s.asset].name
                for k, v in r.items():
                    drift[(name, k)] += v
                attribute_drift(eng, s, placed, drift)
            si += 1

    # ---------------------------------------------------------------- report
    print(f"\nwindow {Path(window).name}: {len(blocks)} blocks, {len(snaps)} later snapshots, "
          f"resolver {'on' if res else 'off'}")

    print("\n(b) ALO would-cross rejects")
    n = alo["total"]
    print(f"  rejected {alo['rejected']} of {n} ALOs ({pct(alo['rejected'], n).strip()}); "
          f"first {early_blocks} blocks: {alo['early_rejected']} of {alo['early_total']}")
    for k in ("crosses placeholders only", "crosses known orders", "crosses known reduce-only"):
        print(f"    {k:<28}{alo[k]:>7}  {pct(alo[k], alo['rejected'])} of rejects")

    print("\n(c) cancels")
    print(f"  cancelByCloid: missed {cancels['cloid_missed']} of {cancels['cloid_total']}; "
          f"of the misses, user had no resting order on that asset: {cancels['cloid_missed_user_has_no_resting_order']}")
    print(f"  int-oid cancel: removed an order {cancels['oid_resolved_and_removed']} of {cancels['oid_total']}")
    if res is not None:
        st = res.stats
        tot = sum(st.values())
        for k in ("resolved", "prewindow", "no_candidate", "ambiguous"):
            print(f"    {k:<14}{st[k]:>7}  {pct(st[k], tot)}")
        print(f"  against orderStatus answers{' (held-out anchors only)' if holdout else ''}: "
              f"{check['with_truth']} cancels with a known target; "
              f"resolved {check['resolved']}, correct {check['resolved_correct']}, WRONG {check['resolved_WRONG']}")
        for k in sorted(k for k in check if k.startswith("truth ")):
            print(f"    {k[6:]:<34}{check[k]:>5}")
    if drop_reduce_only:
        print(f"  (measurement run: {check['dropped_reduce_only']} resting reduce-only placements dropped)")

    print("\n(a) known orders resting at the end of the window")
    for asset, spec in ASSETS.items():
        rest = [(oid, nd.remaining) for lv_side in (Side.BUY, Side.SELL)
                for lv in eng.books[asset].levels(lv_side) for nd in lv
                for oid in [nd.order.oid] if nd.order.user != PLACEHOLDER]
        cnt, lots = len(rest), sum(q for _, q in rest)
        ro = [(o, q) for o, q in rest if placed.get(o) is not None and placed[o].reduce_only]
        users = Counter(eng.books[asset].node(o).order.user for o, _ in rest)
        top5 = sum(c for _, c in users.most_common(5))
        print(f"  {spec.name}: {cnt} orders, {spec.lots_to_sz(lots)} {spec.name}; reduce-only {len(ro)} orders "
              f"({pct(len(ro), cnt).strip()}), {spec.lots_to_sz(sum(q for _, q in ro))} {spec.name} "
              f"({pct(sum(q for _, q in ro), lots).strip()} of lots); top 5 users hold {pct(top5, cnt).strip()} of orders")

    print("\n(a) rule 8 drift attribution (summed over snapshots)")
    for name in COINS:
        spec = next(s for s in ASSETS.values() if s.name == name)
        d, dro = drift[(name, "drift")], drift[(name, "drift_reduce_only")]
        print(f"  {name}: trimmed {spec.lots_to_sz(drift[(name, 'trimmed')])}, drift {spec.lots_to_sz(d)} "
              f"(reduce-only share {pct(dro, d).strip()}), missing {spec.lots_to_sz(drift[(name, 'missing')])}")

    print("\n(e) cloids")
    print(f"  reused while the first order still rests: {eng.stats['cloid_reused_while_resting']}; "
          f"cancels that hit an ambiguous cloid: {eng.stats['cloid_ambiguous']}")

    cats = Counter(classify(real[h], engine_fills.get(h, [])) for h in taker_hashes)
    print(f"\nlinked taker txs: {sum(cats.values())}: " + ", ".join(f"{k} {v}" for k, v in cats.most_common()))
    return dict(alo=alo, cancels=cancels, check=check, drift=drift, cats=cats,
                resolver=res.stats if res else Counter())


def res_outcome(res) -> str:
    """Which counter the resolver's last call bumped."""
    return "resolved" if res.last is not None else res._last_reason


def attribute_drift(eng, s, placed, drift):
    """After reconcile: per in-range level still above the snapshot, split the gap by the
    reduce-only share of that level's known lots (integer, rounded down)."""
    name = ASSETS[s.asset].name
    for side, levels in ((Side.BUY, s.bids), (Side.SELL, s.asks)):
        if not levels:
            continue
        snap = dict(levels)
        edge = min(snap) if side is Side.BUY else max(snap)
        for px, lv in eng.books[s.asset].side(side).items():
            if (px < edge) if side is Side.BUY else (px > edge):
                continue
            gap = lv.total - snap.get(px, 0)
            if gap <= 0:
                continue
            known = ro = 0
            for nd in lv:
                if nd.order.user == PLACEHOLDER:
                    continue
                known += nd.remaining
                pl = placed.get(nd.order.oid)
                if pl is not None and pl.reduce_only:
                    ro += nd.remaining
            if known:
                drift[(name, "drift_reduce_only")] += gap * ro // known


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("window")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--anchors", help="anchors.json (capture) or g0_anchors.json")
    ap.add_argument("--cache", help="orderStatus answers {user:oid: answer} to check resolved cancels")
    ap.add_argument("--no-resolver", action="store_true")
    ap.add_argument("--holdout", action="store_true", help="estimate oids from half the anchors, check on the rest")
    ap.add_argument("--nearest-ratio", type=int, default=0, help="accept a clear nearest candidate (see OidResolver)")
    ap.add_argument("--drop-reduce-only", action="store_true",
                    help="MEASUREMENT: never rest reduce-only orders, to size lead (a)")
    a = ap.parse_args(argv)
    anchors = a.anchors or (str(Path(a.window) / "anchors.json") if (Path(a.window) / "anchors.json").exists() else None)
    run(a.window, a.hl_data, anchors, a.cache, not a.no_resolver, holdout=a.holdout,
        nearest_ratio=a.nearest_ratio, drop_reduce_only=a.drop_reduce_only)


if __name__ == "__main__":
    main()
