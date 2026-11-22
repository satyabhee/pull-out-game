"""How many oid anchors does a window (or chunk) need? Offline curve on a window with located anchors.

    python -m analysis.anchor_density <window_dir> --hl-data <dir> [--sizes 5 10 20 30 42] \
        [--seeds 11 23 37 41 59 73] [--jobs 2] [--out FILE.json]

For each subset size n and each fixed seed, the window is replayed in listing order with an OidResolver
whose estimator sees only a random subset of the window's located anchors. Reported per size
(mean and range over seeds): resolved / ambiguous / no_candidate / prewindow int-oid cancels, and the
held-out check: cancels aimed at an anchor that was left OUT of the subset (its real oid and position are
truth). A held-out cancel that resolved to a different placement is WRONG; the count must be 0.
Resolver outcomes are also bucketed by the distance (in oid space) from the cancel's target oid to the
nearest anchor in use, which is what an anchor-dense chunk would shrink.

Nothing here changes engine behavior. Integers only; every random choice uses random.Random(seed).
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import random
from collections import Counter
from decimal import Decimal
from pathlib import Path

from data.oids import OidEstimator, OidResolver, _order_items
from data.parse import _block_files, read_block
from engine_py.replay import Observer, Replay, listing
from engine_py.types import ASSETS, Cancel

OUTCOMES = ("resolved", "ambiguous", "no_candidate", "prewindow")
DIST_EDGES = (100, 300, 1000, 3000, 10000)      # oid-space distance buckets: <100, <300, ... , >=10000
DEFAULT_SEEDS = (11, 23, 37, 41, 59, 73)
DEFAULT_SIZES = (5, 10, 20, 30, 42)
Pos = tuple[int, int, int]


# ------------------------------------------------------------------ anchors with positions
def locate_anchors(window_dir, anchors: list[dict]) -> tuple[dict[Pos, int], list[dict]]:
    """(counts, located). counts: position -> id-consuming items before it (as OidEstimator). located: one
    dict per anchor placed uniquely in the window: {user, oid, pos, k}. Same rules as
    OidEstimator.from_window: capture anchors match by (user, asset, side, px, sz, time, cloid); anchors
    with a height/tx/item are taken as given. Anchors matching zero or several placements are dropped."""
    want: dict[tuple, int] = {}
    for a in anchors:
        if "height" in a:
            continue
        ans = a.get("answer", {})
        if ans.get("status") != "order":
            continue
        o = ans["order"]["order"]
        asset = next((s.asset for s in ASSETS.values() if s.name == o["coin"]), None)
        if asset is None:
            continue
        want[(a["user"].lower(), asset, o["side"] == "B", Decimal(o["limitPx"]), Decimal(o["origSz"]),
              int(o["timestamp"]), o.get("cloid"))] = int(o["oid"])
    counts: dict[Pos, int] = {}
    users: dict[Pos, str] = {}
    found: dict[int, list[Pos]] = {}
    k = 0
    for height, path in _block_files(Path(window_dir)):
        b = read_block(path)
        for t, tx in enumerate(b["txs"]):
            if tx["error"] is not None:
                continue
            for i, item in enumerate(_order_items(tx["action"])):
                if item.get("a") in ASSETS:
                    counts[(height, t, i)] = k
                    users[(height, t, i)] = tx["user"].lower()
                    if want:
                        try:
                            key = (tx["user"].lower(), item["a"], bool(item["b"]), Decimal(item["p"]),
                                   Decimal(item["s"]), int(tx["time"]), item.get("c"))
                        except Exception:
                            key = None
                        if key in want:
                            found.setdefault(want[key], []).append((height, t, i))
                k += 1
    located = [{"user": users[p[0]], "oid": oid, "pos": p[0], "k": counts[p[0]]}
               for oid, p in sorted(found.items()) if len(p) == 1]
    for a in anchors:
        pos = (a.get("height"), a.get("tx"), a.get("item"))
        if "height" in a and pos in counts:
            located.append({"user": users[pos], "oid": int(a["oid"]), "pos": pos, "k": counts[pos]})
    located.sort(key=lambda a: (a["k"], a["oid"]))
    return counts, located


def choose_subset(located: list[dict], n: int, seed: int) -> list[dict]:
    """n anchors drawn without replacement, reproducibly (random.Random(seed)); n >= len -> all of them."""
    if n >= len(located):
        return list(located)
    rng = random.Random(seed)
    return sorted(rng.sample(located, n), key=lambda a: (a["k"], a["oid"]))


def dist_bucket(d: int) -> str:
    for e in DIST_EDGES:
        if d < e:
            return f"<{e}"
    return f">={DIST_EDGES[-1]}"


BUCKETS = [f"<{e}" for e in DIST_EDGES] + [f">={DIST_EDGES[-1]}"]


# ------------------------------------------------------------------ one replay
class _Observer(Observer):
    """Counts int-oid cancel outcomes (from resolver stat deltas), by distance bucket, and checks cancels
    aimed at held-out anchors against their true placement."""

    def __init__(self, used_oids: list[int], held: dict[tuple[str, int], Pos]):
        self.used, self.held = used_oids, held
        self.outcome: Counter = Counter()
        self.by_bucket: Counter = Counter()          # (bucket, outcome)
        self.heldout: Counter = Counter()            # total / resolved / correct / WRONG / <unresolved outcome>
        self.wrong: list[dict] = []                  # one entry per WRONG resolution, for triage
        self._before: Counter | None = None

    def before_action(self, rp, block, a):
        self._before = Counter(rp.resolver.stats) if isinstance(a, Cancel) else None

    def after_action(self, rp, block, a, fills, rejects, created):
        if self._before is None or not isinstance(a, Cancel):
            return
        res = rp.resolver
        delta = {k: v - self._before.get(k, 0) for k, v in res.stats.items() if k in OUTCOMES}
        out = next((k for k in OUTCOMES if delta.get(k)), None)
        if out is None:
            return                                   # the engine never asked the resolver
        self.outcome[out] += 1
        d = min((abs(a.oid - o) for o in self.used), default=10**18)
        self.by_bucket[(dist_bucket(d), out)] += 1
        truth = self.held.get((a.user, a.oid))
        if truth is not None:
            self.heldout["total"] += 1
            if out == "resolved":
                self.heldout["resolved"] += 1
                placed = rp.placed_by.get(res.last)
                got = (placed.block, placed.tx_index, placed.item_index) if placed is not None else None
                self.heldout["correct" if got == truth else "WRONG"] += 1
                if got != truth:
                    est, tol = res.hl.get(res.last, (None, None))
                    self.wrong.append({"user": a.user, "block": a.block, "cancel_oid": a.oid, "true_pos": list(truth),
                                       "chose_pos": list(got) if got else None, "chosen_est_oid": est,
                                       "chosen_tolerance": tol, "dist_to_nearest_anchor": d})
            else:
                self.heldout[out] += 1


def run_one(window, hl_data, counts, located, subset, nearest_ratio=3) -> dict:
    """Replay the window once with an estimator that sees only `subset` of the located anchors."""
    used = {a["oid"] for a in subset}
    held = {(a["user"], a["oid"]): a["pos"] for a in located if a["oid"] not in used}
    rp = Replay(window, hl_data, listing, use_resolver=False)
    est = OidEstimator(counts, [(a["k"], a["oid"]) for a in subset])
    rp.resolver = rp.engine.resolve_oid = OidResolver(rp.engine, est, nearest_ratio=nearest_ratio)
    obs = _Observer([a["oid"] for a in subset], held)
    rp.observer = obs
    for _ in rp:
        pass
    return {"n": len(subset), "outcome": dict(obs.outcome),
            "by_bucket": {f"{b}|{o}": c for (b, o), c in sorted(obs.by_bucket.items())},
            "heldout": dict(obs.heldout), "wrong_cases": obs.wrong, "stats": dict(rp.resolver.stats)}


# ------------------------------------------------------------------ pool plumbing (window decoded once per worker)
_CTX: dict = {}


def _init(window, hl_data, anchors, nearest_ratio):
    counts, located = locate_anchors(window, anchors)
    _CTX.update(window=window, hl=hl_data, counts=counts, located=located, nr=nearest_ratio)


def _job(args):
    n, seed = args
    sub = choose_subset(_CTX["located"], n, seed)
    r = run_one(_CTX["window"], _CTX["hl"], _CTX["counts"], _CTX["located"], sub, _CTX["nr"])
    r["seed"] = seed
    return r


# ------------------------------------------------------------------ summary
def summarize(rows: list[dict]) -> list[dict]:
    """One line per anchor count: outcome shares (mean/min/max over seeds) and the held-out totals."""
    out = []
    for n in sorted({r["n"] for r in rows}):
        rs = [r for r in rows if r["n"] == n]
        line = {"n": n, "runs": len(rs)}
        for k in OUTCOMES:
            sh = [10000 * r["outcome"].get(k, 0) // max(1, sum(r["outcome"].values())) for r in rs]   # basis points
            line[k] = {"mean_bp": sum(sh) // len(sh), "min_bp": min(sh), "max_bp": max(sh)}
        line["cancels"] = sum(rs[0]["outcome"].values())
        h = Counter()
        for r in rs:
            h.update(r["heldout"])
        line["heldout"] = dict(h)
        out.append(line)
    return out


def bucket_table(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Counts per distance bucket and outcome, pooled over all runs of every size."""
    t: dict[str, Counter] = {b: Counter() for b in BUCKETS}
    for r in rows:
        for key, c in r["by_bucket"].items():
            b, o = key.split("|")
            t[b][o] += c
    return {b: dict(c) for b, c in t.items()}


def fmt_bp(bp: int) -> str:
    return f"{bp // 100}.{bp % 100:02d}%"


def print_report(summary: list[dict]) -> None:
    print(f"{'anchors':>7} {'runs':>4} {'cancels':>8}  " + "  ".join(f"{k:>22}" for k in OUTCOMES) + "   held-out")
    for s in summary:
        cells = "  ".join(f"{fmt_bp(s[k]['mean_bp']):>7} ({fmt_bp(s[k]['min_bp'])}-{fmt_bp(s[k]['max_bp'])})".rjust(22)
                          for k in OUTCOMES)
        h = s["heldout"]
        held = (f"{h.get('total', 0)} aimed, {h.get('resolved', 0)} resolved, {h.get('correct', 0)} correct, "
                f"WRONG {h.get('WRONG', 0)}")
        print(f"{s['n']:>7} {s['runs']:>4} {s['cancels']:>8}  {cells}   {held}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("window")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--anchors", help="default: <window>/anchors.json")
    ap.add_argument("--sizes", type=int, nargs="+", default=list(DEFAULT_SIZES))
    ap.add_argument("--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS))
    ap.add_argument("--nearest-ratio", type=int, default=3)
    ap.add_argument("--jobs", type=int, default=2, help="worker processes (the machine is shared: keep <= 2)")
    ap.add_argument("--out", help="write the raw rows and summary as JSON")
    a = ap.parse_args(argv)
    anchors = json.loads(Path(a.anchors or Path(a.window) / "anchors.json").read_text())
    _, located = locate_anchors(a.window, anchors)
    print(f"{len(located)} located anchors in {a.window}")
    jobs = []
    for n in a.sizes:
        jobs += [(n, 0)] if n >= len(located) else [(n, s) for s in a.seeds]    # the full set has no randomness
    init = (a.window, a.hl_data, anchors, a.nearest_ratio)
    if a.jobs > 1:
        with mp.get_context("fork").Pool(a.jobs, _init, init) as pool:
            rows = pool.map(_job, jobs, chunksize=1)
    else:
        _init(*init)
        rows = [_job(j) for j in jobs]
    summary = summarize(rows)
    print_report(summary)
    print("\nresolver outcome by distance (oid space) from the cancel's target to the nearest anchor in use, all runs")
    bt = bucket_table(rows)
    for b in BUCKETS:
        tot = sum(bt[b].values())
        print(f"  {b:>8} {tot:>9}  " + "  ".join(
            f"{k} {fmt_bp(10000 * bt[b].get(k, 0) // max(1, tot))}" for k in OUTCOMES))
    if a.out:
        Path(a.out).write_text(json.dumps({"summary": summary, "buckets": bt, "rows": rows}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
