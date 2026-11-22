"""Trace contested wrong-maker failures where the engine's maker cancelled in the same block (measurement only).

For every target tx (contested, strict recall 0, right price, a different KNOWN maker than the chain), re-run its
chunk and record, for each cancel the engine maker sent on that asset in that block before the taker:
  - what it named (cloid / hl oid) and what the engine did with it (removed which order, or nothing and why);
  - the order that took the fill instead (its cloid, when it was placed, by which action).
Each case is classified by the step of the cancel -> order matching where it parts from the filled order.

usage: python -m analysis.cancel_trace <validate_chunks variant dir> <ref variant dir> <chunk parent> --hl-data DIR
       [replay flags as for validate_replay] [--out FILE.json]
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analysis import validate_replay as vr
from analysis.contested import ASSET_OF, load
from analysis.contested_paired import scope_of
from engine_py.matching import PLACEHOLDER
from engine_py.types import Cancel, CancelByCloid, Modify, Place


def fills_of(field: str):
    out = []
    for part in field.split(";"):
        b = part.strip().split()
        if len(b) == 2 and "@" in b[0]:
            sz, px = b[0].split("@")
            out.append((float(px), float(sz), b[1][:10]))
    return out


def targets(var_dir: Path, ref_dir: Path, chunks: Path) -> list[dict]:
    sc = scope_of(load(ref_dir), chunks, False)
    rows = {(r["chunk"], r["hash"]): r for r in load(var_dir)}
    out = []
    for k, c in sc.items():
        r = rows.get(k)
        if not c or r is None or int(r["real_notional"]) <= 0 or int(r["matched_notional_full"]) > 0:
            continue
        real, eng = fills_of(r["real"]), fills_of(r["engine"])
        if not eng or {e[0] for e in eng} != {x[0] for x in real} or all(e[2].startswith("placeholde") for e in eng):
            continue
        out.append({"chunk": r["chunk"], "hash": r["hash"], "block": int(r["block"]), "coin": r["coin"],
                    "real": r["real"], "engine": r["engine"]})
    return out


class TraceObserver(vr.ValidateObserver):
    targets: dict = {}          # hash -> target dict (set per chunk)
    blocks: set = set()

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.placements = defaultdict(list)      # (user, asset, cloid) -> [(block, engine oid | None, tif)]
        self.removed_by: dict[int, tuple] = {}   # engine oid -> (block, what removed it)
        self.events = defaultdict(list)          # block -> [event dict] (cancels, fills) in apply order
        self.user_orders = defaultdict(list)     # (user, asset) -> [placement dict], every Place / Modify in the window
        self._patched = False
        self._current = None

    def _patch(self, rp):
        eng = rp.engine
        real_remove = eng._remove
        obs = self

        def _remove(oid):
            a = obs._current
            what = ("cancel_cloid" if isinstance(a, CancelByCloid) else "cancel_oid" if isinstance(a, Cancel)
                    else "modify" if isinstance(a, Modify) else type(a).__name__ if a is not None else "between_actions")
            obs.removed_by[oid] = (rp._block.height if getattr(rp, "_block", None) else None, what)
            return real_remove(oid)
        real_forget = eng._forget

        def _forget(order):
            if order.oid not in obs.removed_by:          # left the book by a full fill (reduce to 0), not a removal
                a = obs._current
                obs.removed_by[order.oid] = (getattr(getattr(rp, "_block", None), "height", None),
                                             "filled" if a is not None else "end_block (schedule cancel / expiry)")
            return real_forget(order)
        eng._forget = _forget
        eng._remove = _remove
        real_evict = eng.evict

        def evict(oid):
            out = real_evict(oid)
            obs.removed_by[oid] = (getattr(getattr(rp, "_block", None), "height", None),
                                   "evicted (gate / touch ledger / reconcile)")
            return out
        eng.evict = evict
        if hasattr(rp, "_ledger_touch"):
            real_ledger = rp._ledger_touch

            def ledger(asset, side, lvl, name):
                h = rp.blocks[rp.block_index].height
                before = dict(rp.touch_reconciled)
                lv = rp.engine.books[asset].side(side).get(lvl[0]) if lvl else None
                ours = (len(lv), lv.total, sum(nd.order.user == PLACEHOLDER for nd in lv)) if lv is not None else (0, 0, 0)
                out = real_ledger(asset, side, lvl, name)
                if h in obs.blocks:
                    got = [k[1] for k, v in rp.touch_reconciled.items() if k[0] == name and v != before.get(k, 0)
                           and k[1].startswith("ledger_")]
                    obs.events[h].append({"kind": "ledger", "hash": None, "asset": asset, "side": side.name,
                                          "bbo": list(lvl) if lvl else None, "ours_n_lots_ph": ours, "result": got})
                return out
            rp._ledger_touch = ledger
        self._patched = True

    def before_action(self, rp, block, a):
        if not self._patched:
            self._patch(rp)
        self._current = a
        self._removed_before = set(self.removed_by)
        super().before_action(rp, block, a)
        if block.height not in self.blocks:
            return
        pl = a.new if isinstance(a, Modify) else a
        if a.hash in self.targets and isinstance(pl, Place) and not any(e["kind"] == "queue" and e["hash"] == a.hash
                                                                      for e in self.events[block.height]):
            eng, q = rp.engine, []
            for lv in eng.books[pl.asset].levels(pl.side.opposite):
                if not ((lv.px <= pl.px) if pl.side.name == "BUY" else (lv.px >= pl.px)):
                    break
                for nd in lv:
                    o = nd.order
                    by = rp.placed_by.get(o.oid)
                    m = eng.p_meta.get(o.oid)
                    q.append({"px": lv.px, "user": o.user, "oid": o.oid, "lots": nd.remaining,
                              "placed_block": getattr(by, "block", None), "placed_tx": getattr(by, "tx_index", None),
                              "p": None if m is None else m[0], "placeholder": o.user == PLACEHOLDER})
                if len(q) > 40:
                    break
            self.events[block.height].append({"kind": "queue", "hash": a.hash, "taker_p": pl.priority,
                                              "taker_tx": a.tx_index, "queue": q})
        if isinstance(a, CancelByCloid) or (isinstance(a, Modify) and isinstance(a.target, str)):
            cl = a.cloid if isinstance(a, CancelByCloid) else a.target
            held = list(rp.engine.by_cloid.get((a.user, a.asset, cl), []))
            self.events[block.height].append({"kind": "cancel_cloid", "user": a.user, "asset": a.asset,
                                              "hash": a.hash, "cloid": cl, "held": held,
                                              "seen": list(self.placements.get((a.user, a.asset, cl), [])),
                                              "seen_removed_by": [self.removed_by.get(x[1]) for x in
                                                                  self.placements.get((a.user, a.asset, cl), []) if x[1] is not None]})
        elif isinstance(a, Cancel) or (isinstance(a, Modify) and isinstance(a.target, int)):
            oid = a.oid if isinstance(a, Cancel) else a.target
            self.events[block.height].append({"kind": "cancel_oid", "user": a.user, "asset": a.asset,
                                              "hash": a.hash, "hl_oid": oid})

    def after_action(self, rp, block, a, fills, rejects, created):
        super().after_action(rp, block, a, fills, rejects, created)
        self._current = None
        p = a.new if isinstance(a, Modify) else a
        if isinstance(p, Place):
            self.user_orders[(a.user, p.asset)].append({
                "block": block.height, "tx": a.tx_index, "seq": self.seq, "px": p.px, "side": p.side.name,
                "tif": p.tif.name, "oid": created, "rejects": sorted({r.reason.name for r in rejects}),
                "modify": isinstance(a, Modify)})
        if isinstance(p, Place) and p.cloid is not None:
            self.placements[(a.user, p.asset, p.cloid)].append((block.height, created, p.tif.name))
        if block.height not in self.blocks:
            return
        ev = self.events[block.height]
        if ev and ev[-1]["hash"] == a.hash and ev[-1]["kind"].startswith("cancel") and "outcome" not in ev[-1]:
            e = ev[-1]
            e["removed"] = sorted(set(self.removed_by) - self._removed_before)
            e["outcome"] = "removed" if not rejects else "nothing"
            if e["kind"] == "cancel_oid" and rp.resolver is not None:
                e["resolver"] = rp.resolver._last_reason or ("resolved" if rp.resolver.last is not None else None)
                e["resolved_to"] = rp.resolver.last
        if a.hash in self.targets and fills:
            for f in fills:
                if f.maker_user == PLACEHOLDER:
                    continue
                pl = rp.placed.get(f.maker_order)
                by = rp.placed_by.get(f.maker_order)
                ev.append({"kind": "fill", "hash": a.hash, "maker": f.maker_user, "engine_oid": f.maker_order,
                           "cloid": getattr(pl, "cloid", None), "px": f.px,
                           "placed_block": getattr(by, "block", None), "placed_hash": getattr(by, "hash", None),
                           "placed_tif": pl.tif.name if pl is not None else None})

    def _already(self, height, e):
        seen = set()
        for x in self.events[height]:
            if x is e:
                break
            seen.update(x.get("removed", []))
        return seen


def classify(case: dict) -> tuple[str, str]:
    """(scenario, step it fails at) for one traced target."""
    fills = [e for e in case["events"] if e["kind"] == "fill" and e["hash"] == case["hash"]]
    if not fills:
        return "no engine fill traced", "-"
    f = fills[0]
    mine = [e for e in case["events"] if e["kind"].startswith("cancel") and e["user"] == f["maker"]
            and e["asset"] == case["asset"] and e["pos"] < f["pos"]]
    if not mine:
        return "maker's cancel came after the taker", "1 order"
    for e in mine:
        if e["kind"] == "cancel_cloid" and f["cloid"] is not None and e["cloid"] == f["cloid"]:
            return "cancel named the filled order's cloid but it survived", "3 remove"
    if f["cloid"] is None and all(e["kind"] == "cancel_cloid" for e in mine):
        if any(e["held"] for e in mine):
            return "cloid cancel removed another order; the filled order has no cloid", "2 look up"
        return "cloid cancel matched nothing; the filled order has no cloid", "2 look up"
    cl = [e for e in mine if e["kind"] == "cancel_cloid"]
    if cl and all(not e["held"] for e in cl):
        seen = [s for e in cl for s in e["seen"]]
        if not seen:
            return "cloid never placed in the window (order predates it)", "2 look up"
        if all(s[1] is None for s in seen):
            return "cloid's placement was rejected by our engine", "2 look up"
        return "cloid's order already gone from our book", "2 look up"
    if cl and any(e["held"] for e in cl):
        return "cloid cancel removed a different order of the maker", "4 which order"
    oc = [e for e in mine if e["kind"] == "cancel_oid"]
    reasons = Counter(e.get("resolver") for e in oc)
    if any(e.get("resolved_to") not in (None, f["engine_oid"]) for e in oc):
        return "oid cancel resolved to a different order", "3 resolve oid"
    r = reasons.most_common(1)[0][0] if reasons else None
    return f"oid cancel unresolved ({r})", "3 resolve oid"


def _trace_chunk(job) -> list[dict]:
    chunk, hl_data, policy, kw, ts = job
    TraceObserver.targets = {t["hash"]: t for t in ts}
    TraceObserver.blocks = {t["block"] for t in ts}
    holder = {}

    class Obs(TraceObserver):
        def __init__(self, *x, **y):
            super().__init__(*x, **y)
            holder["obs"] = self
    vr.run(Path(chunk), hl_data, policy, out_dir=None, quiet=True, observer_cls=Obs, **kw)
    obs, out = holder["obs"], []
    for t in ts:
        evs = obs.events.get(t["block"], [])
        for i, e in enumerate(evs):
            e["pos"] = i
        case = {**t, "events": [{k: v for k, v in e.items() if not k.startswith("_")} for e in evs]}
        case["scenario"], case["step"] = classify(case)
        case["r_fate"] = r_fate(obs, t)
        out.append(case)
    del holder, obs
    from analysis.validate_chunks import _free_chunk_caches
    _free_chunk_caches()                      # same per-chunk cleanup as validate_chunks (Gate 1): flat RSS
    return out


def r_fate(obs, t) -> list[dict]:
    """For each real maker of the target: their in-window placements at that price and side, and what became of the
    last one before the taker (rejected and why / what removed it / still resting)."""
    from decimal import Decimal
    from engine_py.types import ASSETS
    spec = ASSETS[t["asset"]]
    qs = [e for e in obs.events.get(t["block"], []) if e["kind"] == "queue" and e["hash"] == t["hash"]]
    out = []
    for part in t["real"].split(";"):
        b = part.strip().split()
        if len(b) != 2:
            continue
        px = spec.px_to_ticks(b[0].split("@")[1])
        user = next((u for (u, a) in obs.user_orders if a == t["asset"] and u.lower().startswith(b[1].lower())), None)
        in_q = bool(qs) and any(o["user"].lower().startswith(b[1].lower()) and o["px"] == px for o in qs[0]["queue"])
        if user is None:
            out.append({"maker": b[1], "px": px, "fate": "no placement by this maker in the window", "in_queue": in_q})
            continue
        side = None
        pl = [x for x in obs.user_orders[(user, t["asset"])] if x["px"] == px and x["block"] <= t["block"]]
        if not pl:
            out.append({"maker": b[1], "px": px, "fate": "never placed at this price in the window", "in_queue": in_q})
            continue
        last = pl[-1]
        if last["oid"] is None:
            fate = "rejected by replay: " + ",".join(last["rejects"] or ["?"])
        elif last["oid"] in obs.removed_by:
            fate = "removed: " + obs.removed_by[last["oid"]][1] + (" (same block)" if obs.removed_by[last["oid"]][0] == t["block"] else "")
        else:
            fate = "resting"
        out.append({"maker": b[1], "px": px, "fate": fate, "in_queue": in_q, "n_placements": len(pl),
                    "last_block": last["block"], "last_tif": last["tif"], "same_block": last["block"] == t["block"]})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("var_dir")
    ap.add_argument("ref_dir")
    ap.add_argument("chunks")
    ap.add_argument("--out", default=None)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--all-wrong-maker", action="store_true", help="trace every wrong-maker target, not only those with a cancel")
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    tg = targets(Path(a.var_dir), Path(a.ref_dir), Path(a.chunks))
    # keep the ones whose engine maker sent a cancel on that asset in that block
    keep = []
    for t in tg:
        b = json.load(gzip.open(Path(a.chunks) / t["chunk"] / f"{t['block']}.json.gz"))["blockDetails"]
        em = {e[2] for e in fills_of(t["engine"]) if not e[2].startswith("placeholde")}
        asset = ASSET_OF[t["coin"]]
        if any(x["user"].lower()[:10] in em and not x.get("error") and x["action"].get("type") in ("cancel", "cancelByCloid")
               for x in b["txs"]):
            t["asset"] = asset
            keep.append(t)
    if a.all_wrong_maker:
        for t in tg:
            t["asset"] = ASSET_OF[t["coin"]]
        keep = tg
    print(f"{len(tg)} wrong-maker contested txs, {len(keep)} with the engine maker cancelling in the block", file=sys.stderr)
    kw = vr.replay_kwargs(a)
    by_chunk = defaultdict(list)
    for t in keep:
        by_chunk[t["chunk"]].append(t)
    jobs = [(str(Path(a.chunks) / ch), a.hl_data, a.policy, kw, ts) for ch, ts in sorted(by_chunk.items())]
    if a.jobs > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            cases = [c for out in ex.map(_trace_chunk, jobs) for c in out]
    else:
        cases = [c for j in jobs for c in _trace_chunk(j)]
    c = Counter((x["step"], x["scenario"]) for x in cases)
    for (step, sc), n in sorted(c.items()):
        print(f"{n:4d}  [{step}] {sc}")
    if a.out:
        Path(a.out).write_text(json.dumps(cases, indent=1, default=str))


if __name__ == "__main__":
    sys.exit(main())
