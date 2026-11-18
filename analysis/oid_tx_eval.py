"""Independent evaluation of the int-oid resolver (data/oids.py): tx structure, held-out truth, coverage.

    uv run python -m analysis.oid_tx_eval structure WINDOW [--probes HL_DATA]
    uv run python -m analysis.oid_tx_eval heldout WINDOW HL_DATA [--folds 3] [--nearest-ratio 3]
    uv run python -m analysis.oid_tx_eval coverage WINDOW HL_DATA [--nearest-ratio 3]

structure: item-count histograms per tx type (cancel txs never hold more than one oid; this is the
           measured fact behind the cross-tx grouping), plus what the ALO-probe answers say.
heldout:   anchors that locate in the blocks are split into folds; each fold is held out of the
           estimator, the window is replayed, and every resolver call for a held-out oid is
           compared with the engine order created at the anchor's true position:
           correct / wrong / abstained. `wrong` must be 0.
coverage:  resolver outcome counts of a full replay (the number to compare across resolver versions).
Integers only; nothing here touches the network.
"""

from __future__ import annotations

import argparse
import gzip
import json
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from data.oids import _order_items
from data.parse import _block_files, read_block
from engine_py.types import ASSETS, Modify, Place


def tx_structure(window) -> dict[str, Counter]:
    """type -> Counter(items per tx) over successful txs (order / cancel / cancelByCloid / batchModify / modify)."""
    out: dict[str, Counter] = defaultdict(Counter)
    for _, p in _block_files(Path(window)):
        for tx in read_block(p)["txs"]:
            if tx["error"] is not None:
                continue
            a = tx["action"]
            t = a.get("type")
            if t == "order":
                out[t][len(a["orders"])] += 1
            elif t in ("cancel", "cancelByCloid"):
                out[t][len(a["cancels"])] += 1
            elif t == "batchModify":
                out[t][len(a["modifies"])] += 1
            elif t == "modify":
                out[t][1] += 1
    return dict(out)


def probe_rows(hl_data) -> list[dict]:
    rows = []
    for f in sorted(Path(hl_data).glob("*/*/alo_status.jsonl*")):
        op = gzip.open if f.suffix == ".gz" else open
        with op(f, "rt") as fh:
            rows += [json.loads(line)["msg"] for line in fh if line.strip()]
    return rows


def probe_summary(rows: list[dict]) -> dict:
    """Status mix, whether every answered item carries an oid, multi-item txs, and the block span."""
    st = Counter(r["answer"].get("order", {}).get("status", r["answer"].get("status")) for r in rows)
    with_oid = sum(1 for r in rows if r["answer"].get("order", {}).get("order", {}).get("oid"))
    answered = sum(1 for r in rows if r["answer"].get("status") == "order")
    return dict(rows=len(rows), statuses=dict(st), answered=answered, answered_with_oid=with_oid,
                multi_item_txs=sum(1 for r in rows if r["alo_items_in_tx"] > 1),
                blocks=(min((r["block"] for r in rows), default=None), max((r["block"] for r in rows), default=None)))


def locate_anchors(window) -> list[dict]:
    """Anchors (anchors.json answers) that match exactly one placement: [{user, oid, pos, k}]."""
    window = Path(window)
    af = window / "anchors.json"
    if not af.exists():
        return []
    want: dict[tuple, tuple[str, int]] = {}
    for a in json.loads(af.read_text()):
        ans = a.get("answer", {})
        if ans.get("status") != "order":
            continue
        o = ans["order"]["order"]
        asset = next((s.asset for s in ASSETS.values() if s.name == o["coin"]), None)
        if asset is None:
            continue
        want[(a["user"].lower(), asset, o["side"] == "B", Decimal(o["limitPx"]), Decimal(o["origSz"]),
              int(o["timestamp"]), o.get("cloid"))] = (a["user"], int(o["oid"]))
    found: dict[tuple, list] = defaultdict(list)
    k = 0
    for h, p in _block_files(window):
        for t, tx in enumerate(read_block(p)["txs"]):
            if tx["error"] is not None:
                continue
            for i, it in enumerate(_order_items(tx["action"])):
                if it.get("a") in ASSETS:
                    try:
                        key = (tx["user"].lower(), it["a"], bool(it["b"]), Decimal(it["p"]), Decimal(it["s"]),
                               int(tx["time"]), it.get("c"))
                    except Exception:
                        key = None
                    if key in want:
                        found[want[key]].append(((h, t, i), k))
                k += 1
    return [dict(user=u.lower(), oid=o, pos=v[0][0], k=v[0][1]) for (u, o), v in sorted(found.items()) if len(v) == 1]


GUARD: Counter = Counter()           # per-order guard what-ifs over every resolved call (see _guard_flags)
GUARD_LOG: list[dict] = []           # same, for held-out truth calls
WRONG_DETAILS: list[dict] = []        # filled by _replay: the evidence for every wrong held-out resolution


class _Recorder:
    """Replay observer: engine oid created at each position."""

    def __init__(self):
        self.created: dict[tuple, int] = {}
        self.by_user: dict[tuple, list] = defaultdict(list)    # (user, asset) -> [(pos, engine oid | None)]

    def before_action(self, rp, block, a): ...
    def after_reconcile(self, rp, block, s, result, minute): ...
    def after_block(self, rp, block, minute): ...

    def after_action(self, rp, block, a, fills, rejects, created):
        if isinstance(a, (Place, Modify)):
            pos = (a.block, a.tx_index, a.item_index)
            self.by_user[(a.user, a.asset)].append((pos, created))
            if created is not None:
                self.created[pos] = created


def _replay(window, hl_data, anchors_path, truth: dict[tuple, tuple] | None, nearest_ratio: int, replay_kw: dict | None = None, **resolver_kw):
    """One full replay. truth: (user, asset, hl_oid) -> pos. Returns (resolver stats, verdict Counter)."""
    from engine_py.replay import Replay, listing
    rec = _Recorder()
    rp = Replay(window, hl_data, listing, anchors=anchors_path, observer=rec, nearest_ratio=nearest_ratio,
                **(replay_kw or {}))
    verdicts: Counter = Counter()
    if rp.resolver is None:
        return {}, verdicts
    for k, v in resolver_kw.items():
        setattr(rp.resolver, k, v)
    inner = rp.resolver
    calls: list[tuple] = []

    def guard_flags(user, asset, hl_oid, r) -> dict:
        """What-if guards on a per-order resolution r (engine oid), not applied to the replay:
        phantom = the user placed another order within tolerance that is NOT resting now (never admitted,
        filled, cancelled or evicted in our book), so the true target may have left our book;
        ratio_pm = |estimate - hl_oid| / tolerance in per mille (how deep into the tolerance the pick sits)."""
        est = inner.est
        e, tol = inner.hl[r]
        book = rp.engine.books[asset]
        phantom = False
        for pos, eo in rec.by_user.get((user, asset), ()):
            if eo == r or (eo is not None and eo in book):
                continue
            k = est.counts.get(pos)
            if k is None:
                continue
            ep = est.estimate_count(k)
            if ep is not None and abs(ep - hl_oid) <= est.tolerance(k, inner.base, inner.per_mille):
                phantom = True
                break
        return dict(phantom=phantom, ratio_pm=abs(e - hl_oid) * 1000 // max(tol, 1))

    def wrapped(user, asset, hl_oid):
        before = Counter(inner.stats)
        r = inner(user, asset, hl_oid)
        gf = guard_flags(user, asset, hl_oid, r) if r is not None and r in inner.hl else None
        if gf is not None:
            GUARD["resolved"] += 1
            GUARD["phantom"] += gf["phantom"]
            for cut in (250, 500, 750):
                GUARD[f"ratio<={cut}"] += gf["ratio_pm"] <= cut
        pos = (truth or {}).get((user, asset, hl_oid))
        info = None
        if pos is not None:
            t_oid = rec.created.get(pos)
            book = rp.engine.books[asset]
            info = dict(user=user, hl_oid=hl_oid, true_pos=pos, block=rp.engine.block, resolved_to=r,
                        true_engine_oid=t_oid, true_in_book=t_oid in book if t_oid is not None else None,
                        true_est_tol=inner.hl.get(t_oid), picked_est_tol=inner.hl.get(r) if r is not None else None,
                        picked_pos=(lambda a: (a.block, a.tx_index, a.item_index))(rp.placed_by[r])
                        if r in rp.placed_by else None)
        if info is not None and gf is not None:
            info["guard"] = gf
        calls.append((user, asset, hl_oid, r, sorted((inner.stats - before).keys()), info))
        return r

    rp.engine.resolve_oid = wrapped
    for _ in rp:
        pass
    for user, asset, hl_oid, r, how, info in calls:
        pos = (truth or {}).get((user, asset, hl_oid))
        if pos is None:
            continue
        true_oid = rec.created.get(pos)
        v = "correct" if r is not None and r == true_oid else "abstained" if r is None else "WRONG"
        verdicts[v] += 1
        if info is not None and info.get("guard"):
            GUARD_LOG.append(dict(verdict=v, **info["guard"]))
        if v == "WRONG":
            WRONG_DETAILS.append(info)
        verdicts[f"{v} via {'+'.join(how)}"] += 1
    return dict(inner.stats), verdicts


def heldout(window, hl_data, folds=3, nearest_ratio=3, resolver_kw=None) -> dict:
    window = Path(window)
    located = locate_anchors(window)
    anchors = json.loads((window / "anchors.json").read_text())
    total, stats_all = Counter(), []
    for f in range(folds):
        held = {(a["user"], a["oid"]): a for i, a in enumerate(located) if i % folds == f}
        keep = [a for a in anchors if (a["user"], int(a.get("answer", {}).get("order", {}).get("order", {}).get("oid", -1)))
                not in held]
        truth = {(a["user"], ASSET_OF(anchors, a), a["oid"]): a["pos"] for a in held.values()}
        with tempfile.TemporaryDirectory() as td:
            ap = Path(td) / "anchors.json"
            ap.write_text(json.dumps(keep))
            stats, v = _replay(window, hl_data, ap, truth, nearest_ratio, None, **(resolver_kw or {}))
        total += v
        stats_all.append(stats)
    return dict(located=len(located), folds=folds, verdicts=dict(total), wrong_details=list(WRONG_DETAILS), guard=dict(GUARD), guard_truth=list(GUARD_LOG),
                fold_stats=stats_all)


def ASSET_OF(anchors, loc) -> int:
    for a in anchors:
        if a["user"] == loc["user"] and a.get("answer", {}).get("order", {}).get("order", {}).get("oid") == loc["oid"]:
            coin = a["answer"]["order"]["order"]["coin"]
            return next(s.asset for s in ASSETS.values() if s.name == coin)
    raise KeyError(loc)


def coverage(window, hl_data, nearest_ratio=3, resolver_kw=None, **replay_kw) -> dict:
    """replay_kw: Replay options, e.g. reseed=True, keep_known=True (lead-in chunks)."""
    stats, _ = _replay(window, hl_data, None, None, nearest_ratio, replay_kw, **(resolver_kw or {}))
    total = sum(stats.get(k, 0) for k in ("resolved", "no_candidate", "ambiguous", "prewindow"))
    return dict(stats=stats, lookups=total,
                shares={k: f"{100 * stats.get(k, 0) / total:.1f}%" for k in ("resolved", "no_candidate", "ambiguous", "prewindow")}
                if total else {})


# ---------------------------------------------------------------------------------------------- guards
GUARD_CONFIGS = {
    "base": dict(joint_block=False, joint_tx=False, ghost_guard=False, max_tol=None),
    "base_joint": dict(joint_block=True, joint_tx=True, ghost_guard=False, max_tol=None),
    "ghost": dict(joint_block=True, joint_tx=True, ghost_guard=True, max_tol=None),
    "ghost_nojoint": dict(joint_block=False, joint_tx=False, ghost_guard=True, max_tol=None),
    "ratio25": dict(joint_block=True, joint_tx=True, ghost_guard=False, max_tol=None, ratio_per_mille=250),
    "ratio50": dict(joint_block=True, joint_tx=True, ghost_guard=False, max_tol=None, ratio_per_mille=500),
    "cap1000": dict(joint_block=True, joint_tx=True, ghost_guard=False, max_tol=1000),
    "cap2000": dict(joint_block=True, joint_tx=True, ghost_guard=False, max_tol=2000),
    "cap3000": dict(joint_block=True, joint_tx=True, ghost_guard=False, max_tol=3000),
}


def guards(window, hl_data, folds=3, seed=7, configs=None) -> dict:
    """One replay per fold of held-out anchors, driven by the unguarded rule (`base`); every guard config
    answers each held-out int-oid cancel on the same engine state. Verdicts per config: correct / WRONG /
    abstain, against the anchor's true placement. Same idea as agent 3's compare_guards.py."""
    import random
    from data.oids import OidEstimator, OidResolver
    from engine_py.replay import Observer, Replay, listing
    from engine_py.types import Cancel
    window = Path(window)
    names = list(configs or GUARD_CONFIGS)
    located = locate_anchors(window)
    idx = list(range(len(located)))
    random.Random(seed).shuffle(idx)
    agg = {n: Counter() for n in names}
    wrong = []
    for f in range(folds):
        heldi = set(idx[f::folds])
        held = {(a["user"], a["oid"]): a["pos"] for i, a in enumerate(located) if i in heldi}
        sub = [dict(oid=a["oid"], height=a["pos"][0], tx=a["pos"][1], item=a["pos"][2])
               for i, a in enumerate(located) if i not in heldi]
        est = OidEstimator.from_window(window, sub)
        rp = Replay(window, hl_data, listing, use_resolver=False)
        rs = {n: OidResolver(rp.engine, est, nearest_ratio=3, **GUARD_CONFIGS[n]) for n in names}
        drv = rs["base"] if "base" in rs else rs[names[0]]
        leaders: dict = {}
        for n, r in rs.items():                       # resolvers with the same cap share hl/meta
            lead = leaders.setdefault(GUARD_CONFIGS[n].get("max_tol"), r)
            if lead is not r:
                r.hl, r.meta = lead.hl, lead.meta
        state = {"on": False}

        class Multi:
            last = property(lambda s: drv.last)
            hl = property(lambda s: drv.hl)
            stats = property(lambda s: drv.stats)
            first_hl = property(lambda s: drv.first_hl)

            def note(s, action, eo):
                for r in leaders.values():
                    r.note(action, eo)

            def __call__(s, user, asset, o):
                out = {}
                truth = held.get((user, o)) if state["on"] else None
                if truth is not None:
                    for n, r in rs.items():
                        if r is not drv:
                            x = r(user, asset, o)
                            out[n] = r.last if x is not None else None
                res = drv(user, asset, o)
                if truth is not None:
                    out[drv_name] = res
                    for n, x in out.items():
                        c = agg[n]
                        c["calls"] += 1
                        if x is None:
                            c["abstain"] += 1
                            continue
                        act = rp.placed_by.get(x)
                        got = (act.block, act.tx_index, act.item_index) if act else None
                        if got == truth:
                            c["correct"] += 1
                        else:
                            c["WRONG"] += 1
                            wrong.append((n, user[:8], o, truth, got))
                return res

        drv_name = next(n for n, r in rs.items() if r is drv)
        m = Multi()
        rp.resolver = rp.engine.resolve_oid = m

        class Obs(Observer):
            def before_action(self, rp_, block, a):
                state["on"] = isinstance(a, Cancel) or (isinstance(a, Modify) and isinstance(a.target, int))

        rp.observer = Obs()
        for _ in rp:
            pass
    return dict(located=len(located), folds=folds, tally={n: dict(c) for n, c in agg.items()}, wrong=wrong[:20])


# ---------------------------------------------------------------------------------------------- synth
def synth(window, hl_data, seed=1, joint=True, ghost=True, max_tol=None, n_anchors=21, drift_lo=0.005,
          drift_hi=0.07, seg=2000, slack=3, lo=2, rate=80, tx_joint=True) -> dict:
    """Whole-window synthetic truth: every cloid cancel in the real window names an exact placement; rewrite it
    into an int-oid cancel whose oid is SIMULATED, oid(k) = BASE + k + D(k), D = cumulative invisible ids
    (per-`seg`-count rate drawn in [drift_lo, drift_hi], seeded). Anchors = n_anchors evenly spaced placements with
    their simulated oids. Counts right / wrong / abstained lookups (agent 3's synth_oids.py)."""
    import numpy as np
    import engine_py.replay as rpm
    from data import oids
    from engine_py.types import Cancel, CancelByCloid
    window = Path(window)
    BASE = 10**9
    real = oids.OidEstimator.from_files(window, window / "anchors.json")
    counts = real.counts
    N = max(counts.values()) + 1
    rng = np.random.default_rng(seed)
    rates = rng.uniform(drift_lo, drift_hi, N // seg + 1)
    D = np.cumsum(rng.random(N) < rates[np.arange(N) // seg])
    true_oid = lambda k: BASE + k + int(D[k])
    ps = sorted(counts.items(), key=lambda kv: kv[1])
    step = len(ps) // (n_anchors + 1)
    anch = [dict(oid=true_oid(ps[step * (i + 1)][1]), height=ps[step * (i + 1)][0][0], tx=ps[step * (i + 1)][0][1],
                 item=ps[step * (i + 1)][0][2]) for i in range(n_anchors)]
    kw = dict(joint_block=joint, joint_tx=joint and tx_joint, pair_slack=slack, pair_lo=lo, pair_per_mille=rate,
              ghost_guard=ghost, max_tol=max_tol)
    places: dict = {}
    truth: dict = {}
    holder: dict = {}

    def policy(actions):
        out = []
        for x in actions:
            if isinstance(x, Modify):
                if isinstance(x.target, int):
                    x = x.new
                else:
                    places[(x.user, x.new.asset, x.new.cloid)] = (x.new.block, x.new.tx_index, x.new.item_index)
            if isinstance(x, Place):
                if x.cloid:
                    places[(x.user, x.asset, x.cloid)] = (x.block, x.tx_index, x.item_index)
            elif isinstance(x, Cancel):
                continue
            elif isinstance(x, CancelByCloid):
                pos = places.get((x.user, x.asset, x.cloid))
                if pos is not None and pos in counts:
                    o = true_oid(counts[pos])
                    truth[(x.block, x.user, x.asset, o)] = pos
                    holder["est"].block_groups.setdefault((x.block, x.user), []).append((x.asset, o))
                    x = Cancel(user=x.user, block=x.block, tx_index=x.tx_index, item_index=x.item_index, hash=x.hash,
                               priority=x.priority, asset=x.asset, oid=o)
            out.append(x)
        return out

    with tempfile.TemporaryDirectory() as td:
        ap = Path(td) / "anchors.json"
        ap.write_text(json.dumps(anch))
        oids.clear_cache()

        class R(oids.OidResolver):
            def __init__(s, *x, **y):
                super().__init__(*x, **{**y, **kw})

        saved = rpm.OidResolver
        rpm.OidResolver = R
        try:
            rep = rpm.Replay(window, hl_data, policy, anchors=ap, nearest_ratio=3, check_permutation=False)
        finally:
            rpm.OidResolver = saved
        est = rep.resolver.est
        est.block_groups, est.groups = {}, {}
        holder["est"] = est
        res = rep.resolver
        inner = rep.engine.resolve_oid
        out: Counter = Counter()

        def wrapped(user, asset, o):
            b0 = res.stats["resolved_block"] + res.stats["resolved_group"]
            r = inner(user, asset, o)
            via_joint = res.stats["resolved_block"] + res.stats["resolved_group"] > b0
            t = truth.get((rep.engine.block, user, asset, o))
            if t is None:
                out["real_unmapped"] += 1
                return r
            if r is None:
                out["none:" + (res._last_reason or "?")] += 1
                return r
            act = rep.placed_by.get(r)
            got = (act.block, act.tx_index, act.item_index) if act else None
            k = "right" if got == t else "WRONG"
            out[k] += 1
            if via_joint:
                out[k + "_via_joint"] += 1
            return r

        rep.engine.resolve_oid = wrapped
        for _ in rep:
            pass
    return dict(config=kw, seed=seed, outcomes=dict(out), resolver_stats=dict(res.stats))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["structure", "heldout", "coverage", "guards", "synth"])
    ap.add_argument("window")
    ap.add_argument("hl_data", nargs="?")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--nearest-ratio", type=int, default=3)
    ap.add_argument("--probes")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-ghost", action="store_true", help="synth: ghost guard off")
    ap.add_argument("--max-tol", type=int, default=0, help="synth: cap on tolerance (0 = none)")
    ap.add_argument("--baseline", "--no-joint", dest="baseline", action="store_true", help="coverage/heldout: switch the joint layers off (joint_tx/joint_block)")
    ap.add_argument("--chunks", action="store_true", help="coverage: WINDOW is a folder of lead-in chunks (reseed+keep_known)")
    a = ap.parse_args(argv)
    if a.mode == "structure":
        for t, c in sorted(tx_structure(a.window).items()):
            print(t, sorted(c.items())[:12])
        if a.probes:
            print(probe_summary(probe_rows(a.probes)))
    elif a.mode == "guards":
        print(json.dumps(guards(a.window, a.hl_data, a.folds), indent=1))
    elif a.mode == "synth":
        print(json.dumps(synth(a.window, a.hl_data, a.seed, joint=not a.baseline, ghost=not a.no_ghost,
                               max_tol=a.max_tol or None), indent=1))
    elif a.mode == "heldout":
        print(json.dumps(heldout(a.window, a.hl_data, a.folds, a.nearest_ratio,
                                      dict(joint_tx=False, joint_block=False) if a.baseline else None), indent=1))
    else:
        rk = dict(joint_tx=False, joint_block=False) if a.baseline else {}
        if a.chunks:
            tot: Counter = Counter()
            for w in sorted(p for p in Path(a.window).iterdir() if (p / "window.json").exists()):
                tot.update(coverage(w, a.hl_data, a.nearest_ratio, rk, reseed=True, keep_known=True)["stats"])
            n = sum(tot.get(k, 0) for k in ("resolved", "no_candidate", "ambiguous", "prewindow"))
            print(json.dumps(dict(stats=dict(tot), lookups=n), indent=1))
        else:
            print(json.dumps(coverage(a.window, a.hl_data, a.nearest_ratio, rk), indent=1))


if __name__ == "__main__":
    main()
