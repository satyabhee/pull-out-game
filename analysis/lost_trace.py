"""I15: trace lost-order cases by removal mechanism (measurement only; NS-1 follow-up).

A lost-order case (Yuta's A0 on dev_margin60, handoff/a0/lost_order_cases_dev_margin60.csv) is a scored contested tx
whose real maker M had a window order at the fill price p that is NOT resting in our book when the tx runs, and that
we did not fill. This re-runs each case's chunk on the Python reference engine with the given replay flags and logs,
for every order of M on that coin at p: where it was placed, and what removed it, by the replay / engine functions
on the call stack at the removal (cancel by cloid / by oid / modify / scheduleCancel / ALO-gate eviction / touch
ledger eviction / snapshot reconcile / trade correction / ...). Placements of M at p that never rested are logged
with their reject reason.

usage: python -m analysis.lost_trace CASES.csv CHUNK_PARENT --hl-data DIR [replay flags as for validate_replay]
       --out FILE.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analysis import validate_replay as vr
from engine_py.types import Cancel, CancelByCloid, Modify, Place

ASSET = {"BTC": 0, "ETH": 1}
OURS = ("engine_py/replay.py", "engine_py/matching.py", "engine_py/book.py", "engine_py/multi_lite.py")


def _stack(skip: int = 2) -> list[str]:
    """Function names on the call stack inside the engine / replay files, outermost first."""
    f, out = sys._getframe(skip), []
    while f is not None:
        if f.f_code.co_filename.endswith(OURS):
            out.append(f.f_code.co_name)
        f = f.f_back
    return out[::-1]


def mechanism(stack: list[str], action_kind: str | None) -> str:
    """One label per removal, from the innermost recognisable step."""
    s = set(stack)
    if "_cancel_cloid" in s:
        return f"cancel by cloid ({action_kind})"
    if "_cancel_hl_oid" in s:
        return f"cancel by oid, resolver ({action_kind})"
    if "end_block" in s:
        return "scheduleCancel"
    if "_learn" in s:
        return "trade correction (learn)"
    if "_ledger_touch" in s:
        return "touch eviction (ledger)"
    if "_apply_gated" in s and "evict" in s:
        return "ALO gate eviction (admitted ALO crosses it)"
    if "_reconcile_touch" in s:
        return "touch reconcile"
    if "reconcile" in s and "step_block" in s:
        return "snapshot reconcile (evict known)"
    if "clear" in s:
        return "snapshot clear"
    if "evict" in s:
        return f"gate eviction during {action_kind or '?'}"
    if "_apply" in s or "apply" in s:
        return f"other inside {action_kind or '?'}: {'>'.join(stack[-3:])}"
    return "other: " + ">".join(stack[-4:])


def make_observer(cases_by_chunk: dict, log: dict):
    class Obs(vr.ValidateObserver):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._wrapped = False
            self._cur = None                                   # (block, action kind, user, hash) while applying
            self.targets = cases_by_chunk                       # list of (maker prefix, asset, px, case block)
            self.tracked: dict[int, dict] = {}                 # engine oid -> record
            self._snap_ms = None

        def before_reseed(self, replay, block, snapshot):
            self._snap_ms = snapshot.time_ms
            return super().before_reseed(replay, block, snapshot)

        def _match(self, user, asset, px):
            u = (user or "").lower()
            return any(u.startswith(m) and asset == a and px == p for m, a, p, _ in self.targets)

        def _wrap(self, rp):
            eng = rp.engine
            orig = eng._remove

            def _remove(oid, _orig=orig, _rp=rp):
                rec = self.tracked.get(oid)
                if rec is not None and rec.get("removed") is None:
                    st = _stack()
                    blk, kind, user, h = self._cur or (getattr(_rp.blocks[_rp.block_index], "height", None), None, None, None)
                    tb = getattr(_rp, "touch", None)
                    bbo = tb.bbo.get(rec["asset"]) if tb is not None else None
                    rec["removed"] = {"snap_ms": self._snap_ms if "reconcile" in st and "step_block" in st else None,
                                      "touch_ms": bbo[0] if bbo else None,
                                      "block_ms": _rp.blocks[_rp.block_index].time_ms, "block": blk, "by": mechanism(st, kind), "action": kind,
                                      "action_user": (user or "")[:10], "own": (user or "").lower() == rec["user"],
                                      "hash": h, "stack": ">".join(st[-6:])}
                return _orig(oid)
            eng._remove = _remove
            self._wrapped = True

        def before_action(self, replay, block, action):
            if not self._wrapped:
                self._wrap(replay)
            self._cur = (block.height, type(action).__name__, getattr(action, "user", None), action.hash)
            return super().before_action(replay, block, action)

        def after_action(self, replay, block, action, fills, rejects, created):
            r = super().after_action(replay, block, action, fills, rejects, created)
            for f in fills:                                    # our fills of tracked orders (should not happen)
                rec = self.tracked.get(f.maker_order)
                if rec is not None:
                    rec.setdefault("filled_by_us", []).append([block.height, f.sz])
            place = action.new if isinstance(action, Modify) else action if isinstance(action, Place) else None
            if place is not None and self._match(action.user, action.asset, place.px):
                eng = replay.engine
                rests = created is not None and any(created in bk for bk in eng.books.values())
                rec = {"user": action.user.lower(), "block": block.height, "block_ms": block.time_ms, "asset": action.asset, "kind": type(action).__name__,
                       "tif": place.tif.name, "sz": place.sz, "cloid": place.cloid, "hash": action.hash,
                       "rested": rests, "rejects": sorted({x.reason for x in rejects})}
                if rests:
                    self.tracked[created] = rec
                else:
                    log["never_rested"].append({"chunk": self.chunk, **rec})
            self._cur = None
            return r

    return Obs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cases")
    ap.add_argument("chunks")
    ap.add_argument("--out", required=True)
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    cases = list(csv.DictReader(open(a.cases)))
    by_chunk: dict[str, list] = defaultdict(list)
    for c in cases:
        d = json.loads(c["detail"])
        by_chunk[c["chunk"]].append((d["maker"].lower(), ASSET[c["coin"]], int(d["px"]), int(c["block"])))
    kw = vr.replay_kwargs(a)
    out = {"cases": [], "never_rested": []}
    for chunk in sorted(by_chunk):
        Obs = make_observer(by_chunk[chunk], out)
        Obs.chunk = chunk
        holder = {}

        class Keep(Obs):
            def __init__(self, *x, **k):
                super().__init__(*x, **k)
                holder["obs"] = self
        vr.run(Path(a.chunks) / chunk, a.hl_data, a.policy, out_dir=None, quiet=True, observer_cls=Keep, **kw)
        obs = holder["obs"]
        for m, asset, px, blk in by_chunk[chunk]:
            mine = [dict(oid=o, **r) for o, r in obs.tracked.items()
                    if r["user"].startswith(m) and r["block"] <= blk and
                    (r.get("removed") is None or r["removed"]["block"] is None or r["removed"]["block"] <= blk)]
            # the owner's own cancel by cloid is exact (the chain removed that order too): it cannot explain the
            # loss; the case is labelled by the other removals of M's orders at p
            suspects = [o for o in mine if not (o["removed"] and o["removed"]["own"]
                                               and o["removed"]["by"].startswith("cancel by cloid"))]
            label = (" + ".join(sorted({o["removed"]["by"] if o["removed"] else "still resting" for o in suspects}))
                     if suspects else ("only own cloid cancels" if mine else "no rested window order"))
            out["cases"].append({"chunk": chunk, "maker": m, "asset": asset, "px": px, "block": blk,
                                 "label": label, "orders": mine})
        print(f"{chunk}: {len(by_chunk[chunk])} case(s) traced", file=sys.stderr)
    tally = Counter()
    for c in out["cases"]:
        tally[c["label"]] += 1
    out["tally"] = dict(tally.most_common())
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(a.out, "w"), indent=1, default=str)
    print(json.dumps(out["tally"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
