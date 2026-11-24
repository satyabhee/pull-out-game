"""I27 diagnostic (measurement only): what happened in our replay to the EXACT maker order the chain filled.

For each contested miss (analysis.retriage misses.csv: chunk, hash), every real fill of the tx whose trade id pins the
maker placement (data/tid_solve.py: unique (taker oid, maker oid) solution) gives the chain's filled order as a
placement position (block, tx index, item). This replays the chunk (flags as for validate_replay; pass the baseline
plus --learn-tid, which builds the tid map; --learn-tid is inert on scored txs on B2, scoreboard "I27a") and reports
the fate of that placement in our book:
  never applied   : no engine action created it (skipped before the engine: gate / taker-reject / hist / oracle),
  never rested    : created nothing resting (reject reasons given, e.g. ALO_WOULD_CROSS),
  removed         : rested, then removed before the tx (mechanism from the call stack, as analysis.lost_trace),
  filled by us    : rested, consumed by our fills of OTHER takers before the tx,
  resting         : still resting when the tx runs (with what is ahead of it at its level: orders / lots, placeholder
                    vs named) -> the miss is a queue / matching question, not a lost order.

usage: python -m analysis.tid_triage MISSES.csv [MISSES.csv ...] --chunks DIR [DIR ...] --hl-data DIR
       [replay flags] --out FILE.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analysis import validate_replay as vr
from analysis.lost_trace import _stack, mechanism
from engine_py.matching import PLACEHOLDER
from engine_py.types import Modify, Place


def make_observer(miss_hashes: set[str], out: list, chunk: str):
    class Obs(vr.ValidateObserver):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._ready = False
            self.targets: dict[tuple, list] = defaultdict(list)    # placement pos -> [(miss hash, trade tid)]
            self.by_hash: dict[str, list] = defaultdict(list)      # miss hash -> [pos]
            self.fate: dict[tuple, dict] = {}                      # pos -> record
            self.eoid_pos: dict[int, tuple] = {}
            self._cur = None
            self.done: set[str] = set()

        def _setup(self, rp):
            for (h, tid), (pos, _t, _m) in rp._tid_maker.items():
                if h in miss_hashes:
                    self.targets[pos].append((h, tid))
                    self.by_hash[h].append(pos)
            eng = rp.engine
            orig = eng._remove

            def _remove(oid, _orig=orig, _rp=rp):
                pos = self.eoid_pos.get(oid)
                if pos is not None:
                    rec = self.fate[pos]
                    if rec.get("removed") is None and rec.get("open", True):
                        st = _stack()
                        kind = self._cur[1] if self._cur else None
                        rec["removed"] = {"by": mechanism(st, kind), "block": _rp.blocks[_rp.block_index].height,
                                          "own": bool(self._cur and self._cur[2] == rec["user"]),
                                          "stack": ">".join(st[-6:])}
                return _orig(oid)
            eng._remove = _remove
            self._ready = True

        def before_action(self, rp, block, action):
            if not self._ready:
                self._setup(rp)
            self._cur = (block.height, type(action).__name__, (getattr(action, "user", "") or "").lower())
            h = action.hash
            if h in self.by_hash and h not in self.done:
                self.done.add(h)
                self._snapshot(rp, h)
            return super().before_action(rp, block, action)

        def _snapshot(self, rp, h):
            for pos in self.by_hash[h]:
                rec = self.fate.get(pos)
                row = {"chunk": chunk, "hash": h, "pos": list(pos), "tids": [t for hh, t in self.targets[pos] if hh == h]}
                if rec is None:
                    row["fate"] = "never applied"
                elif not rec["rested"]:
                    row.update(fate="never rested", rejects=rec["rejects"], kind=rec["kind"], tif=rec["tif"])
                elif rec.get("filled_by"):
                    row.update(fate="filled by us (other taker)", filled_by=rec["filled_by"][:3],
                               removed=rec.get("removed"))
                elif rec.get("removed") is not None:
                    row.update(fate="removed", removed=rec["removed"])
                else:
                    eoid = rec["eoid"]
                    book = rp.engine.books[rec["asset"]]
                    ahead = {"placeholder_orders": 0, "placeholder_lots": 0, "named_orders": 0, "named_lots": 0,
                             "same_user_orders": 0}
                    found = False
                    if True:
                        for nd in (self._level(book, rec) or ()):
                            if nd.order.oid == eoid:
                                found = True
                                break
                            if nd.order.user == PLACEHOLDER:
                                ahead["placeholder_orders"] += 1
                                ahead["placeholder_lots"] += nd.remaining
                            else:
                                pa = rp.placed_by.get(nd.order.oid)
                                ahead.setdefault("owners", []).append(
                                    [(nd.order.user or "")[:10], getattr(pa, "block", None),
                                     type(pa).__name__ if pa is not None else "seeded/known",
                                     (pa.new if isinstance(pa, Modify) else pa).tif.name if pa is not None else None])
                                ahead["named_orders"] += 1
                                ahead["named_lots"] += nd.remaining
                                ahead["same_user_orders"] += (nd.order.user or "").lower() == rec["user"]
                    row.update(fate="resting" if found else "not found at its level", ahead=ahead)
                out.append(row)

        @staticmethod
        def _level(book, rec):
            lvl = book.side(rec["side"]).get(rec["px"])
            return list(lvl) if lvl is not None else None

        def after_action(self, rp, block, action, fills, rejects, created):
            r = super().after_action(rp, block, action, fills, rejects, created)
            for f in fills:
                pos = self.eoid_pos.get(f.maker_order)
                if pos is not None and action.hash not in [hh for hh, _ in self.targets[pos]]:
                    self.fate[pos].setdefault("filled_by", []).append(action.hash[:12])
            place = action.new if isinstance(action, Modify) else action if isinstance(action, Place) else None
            if place is not None:
                pos = (action.block, action.tx_index, action.item_index)
                if pos in self.targets:
                    eng = rp.engine
                    rests = created is not None and any(created in bk for bk in eng.books.values())
                    self.fate[pos] = {"user": action.user.lower(), "asset": action.asset, "px": place.px,
                                      "side": place.side, "kind": type(action).__name__, "tif": place.tif.name,
                                      "rested": rests, "eoid": created, "rejects": sorted({x.reason for x in rejects})}
                    if rests:
                        self.eoid_pos[created] = pos
            self._cur = None
            return r

    return Obs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("misses", nargs="+")
    ap.add_argument("--chunks", nargs="+", required=True, help="chunk-parent dirs (first holding the chunk wins)")
    ap.add_argument("--out", required=True)
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    if not a.learn_tid:
        ap.error("pass --learn-tid (it builds the tid map; inert on scored txs on B2)")
    by_chunk: dict[str, set] = defaultdict(set)
    for f in a.misses:
        for r in csv.DictReader(open(f)):
            by_chunk[r["chunk"]].add(r["hash"])
    kw = vr.replay_kwargs(a)
    rows: list = []
    for ch in sorted(by_chunk):
        d = next((Path(p) / ch for p in a.chunks if (Path(p) / ch).is_dir()), None)
        if d is None:
            print(f"{ch}: not found", file=sys.stderr)
            continue
        vr.run(d, a.hl_data, a.policy, out_dir=None, quiet=True, observer_cls=make_observer(by_chunk[ch], rows, ch), **kw)
        print(f"{ch}: {len(by_chunk[ch])} misses, {sum(1 for r in rows if r['chunk'] == ch)} pinned fills",
              file=sys.stderr, flush=True)
    tally = Counter(r["fate"] for r in rows)
    sub = Counter((r["removed"]["by"] if r.get("removed") else "-") for r in rows if r["fate"] == "removed")
    rej = Counter(",".join(r.get("rejects", [])) or "-" for r in rows if r["fate"] == "never rested")
    miss_pinned = len({(r["chunk"], r["hash"]) for r in rows})
    own = Counter(o[0] for r in rows if r["fate"] == "resting" for o in r["ahead"].get("owners", []))
    how = Counter((o[2], o[3]) for r in rows if r["fate"] == "resting" for o in r["ahead"].get("owners", []))
    res = {"phantom_ahead_owners": dict(own.most_common(15)), "phantom_ahead_how": {f"{a}/{b}": n for (a, b), n in how.most_common()},
           "misses": sum(len(v) for v in by_chunk.values()), "misses_with_pinned_fill": miss_pinned,
           "pinned_fills": len(rows), "fate": dict(tally.most_common()), "removed_by": dict(sub.most_common()),
           "never_rested_rejects": dict(rej.most_common()), "rows": rows}
    Path(a.out).write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
