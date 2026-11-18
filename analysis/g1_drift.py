"""G1 book-drift triage (AGENT_PLAN 6.1): measure each lead as its own category before any fix.

    python -m analysis.g1_drift <window_dir> --hl-data <dir> [--anchors FILE] [--cache FILE] [--no-resolver]

Replays the window with engine_py/replay.Replay in listing order (placeholders, outside
fills at end of block, rule 8 reconciliation at every later snapshot), and reports:
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

from analysis.g0_block_check import classify
from data.oids import OidEstimator, OidResolver
from data.parse import iter_trades
from engine_py.matching import PLACEHOLDER, Engine
from engine_py.replay import COINS, Observer, Replay, listing
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


def drop_reduce_only_policy(counter: Counter):
    """MEASUREMENT policy (listing order): resting reduce-only placements never happen;
    a modify keeps only its cancel half. Not a permutation: run with check_permutation=False."""
    def policy(actions):
        out = []
        for a in actions:
            p = a.new if isinstance(a, Modify) else a
            if isinstance(p, Place) and p.reduce_only and p.tif is not Tif.IOC:
                counter["dropped_reduce_only"] += 1
                if not isinstance(a, Modify):
                    continue
                meta = dict(user=a.user, block=a.block, tx_index=a.tx_index, item_index=a.item_index,
                            hash=a.hash, priority=a.priority, asset=a.asset)
                a = (CancelByCloid(**meta, cloid=a.target) if isinstance(a.target, str)
                     else Cancel(**meta, oid=a.target))
            out.append(a)
        return out
    return policy


class _DriftObserver(Observer):
    def __init__(self, real, truth, t0, holdout_held, early_blocks):
        self.real, self.truth, self.t0, self.held, self.early_blocks = real, truth, t0, holdout_held, early_blocks
        self.alo, self.cancels, self.check, self.drift = Counter(), Counter(), Counter(), Counter()
        self.engine_fills = defaultdict(list)
        self.taker_hashes = set()
        self._pre = None

    def before_action(self, rp, block, a):
        p = a.new if isinstance(a, Modify) else a
        self._pre = crossing_liquidity(rp.engine, p, rp.placed) if isinstance(p, Place) and p.tif is Tif.ALO else None

    def after_action(self, rp, block, a, fills, rejects, created):
        alo, cancels, check, res = self.alo, self.cancels, self.check, rp.resolver
        p = a.new if isinstance(a, Modify) else a
        reasons = {r.reason for r in rejects}
        if isinstance(p, Place) and p.tif is Tif.ALO:
            alo["total"] += 1
            early = rp.block_index < self.early_blocks
            alo["early_total"] += early
            if RejectReason.ALO_WOULD_CROSS in reasons:
                pre = self._pre
                alo["rejected"] += 1
                alo["early_rejected"] += early
                kind = ("crosses known reduce-only" if pre["known_reduce_only"] else
                        "crosses known orders" if pre["known"] else "crosses placeholders only")
                alo[kind] += 1
        if isinstance(a, Cancel):
            cancels["oid_total"] += 1
            hit = RejectReason.UNKNOWN_ORDER not in reasons
            cancels["oid_resolved_and_removed"] += hit
            t = self.truth.get((a.user, a.oid))
            if t is not None and res is not None:
                where = ("in-window" if t["timestamp"] >= self.t0 else "pre-window")
                if self.held is not None and where == "in-window" and a.oid not in self.held:
                    t = None                         # an anchor the estimator was given: too easy
            if t is not None and res is not None:
                check["with_truth"] += 1
                check[f"truth {where}: {res_outcome(res)}"] += 1
                if hit:
                    check["resolved"] += 1
                    o = rp.placed.get(res.last)
                    spec = ASSETS[a.asset]
                    ok = (o is not None and o.px == spec.px_to_ticks(t["limitPx"])
                          and o.sz == spec.sz_to_lots(t["origSz"]) and o.side.value == t["side"])
                    check["resolved_correct" if ok else "resolved_WRONG"] += 1
        elif isinstance(a, CancelByCloid):
            cancels["cloid_total"] += 1
            if RejectReason.UNKNOWN_ORDER in reasons:
                cancels["cloid_missed"] += 1
                eng = rp.engine
                mine = [o for o in eng.by_user.get(a.user, {}) if o in eng.books[a.asset]]
                cancels["cloid_missed_user_has_no_resting_order"] += not mine
        if not isinstance(a, OutsideFill):
            if isinstance(p, Place) and p.hash in self.real:
                self.taker_hashes.add(p.hash)
            for f in fills:
                self.engine_fills[f.taker_hash].append((f.px, f.sz))

    def after_reconcile(self, rp, block, s, result, minute):
        name = ASSETS[s.asset].name
        for k, v in result.items():
            self.drift[(name, k)] += v
        attribute_drift(rp.engine, s, rp.placed, self.drift)


def run(window, hl_data, anchors_path=None, cache_path=None, use_resolver=True, early_blocks=3,
        holdout=False, nearest_ratio=0, drop_reduce_only=False):
    dropped = Counter()
    policy = drop_reduce_only_policy(dropped) if drop_reduce_only else listing
    rp = Replay(window, hl_data, policy, anchors=anchors_path, use_resolver=use_resolver and bool(anchors_path),
                nearest_ratio=nearest_ratio, check_permutation=not drop_reduce_only)
    blocks, t0, t1, eng, snaps = rp.blocks, rp.t0, rp.t1, rp.engine, rp.snapshots

    res, held = rp.resolver, None
    if res is not None:
        est = res.est
        if holdout:                                      # estimate from even anchors, judge on the odd ones
            held = {o for _, o in est.anchors[1::2]}
            est = OidEstimator(est.counts, est.anchors[::2])
            res = OidResolver(eng, est, nearest_ratio=nearest_ratio)
            rp.resolver = eng.resolve_oid = res
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

    obs = _DriftObserver(real, truth, t0, held, early_blocks)
    rp.observer = obs
    for _ in rp:
        pass
    placed, alo, cancels, check, drift = rp.placed, obs.alo, obs.cancels, obs.check, obs.drift
    engine_fills, taker_hashes = obs.engine_fills, obs.taker_hashes
    check["dropped_reduce_only"] = dropped["dropped_reduce_only"]

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
