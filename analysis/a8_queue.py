"""A8: sub-causes of A0's queue-order misses (rule and definitions: docs/scoreboard.md, written before this ran).

For each queue miss, replays its chunk on the baseline and records, just before the tx, the orders at the real
price p and at our first fill's price p' with their origin (window Place / Modify with block, listing position,
time and priority p; or not placed in the window: placeholder / named-from-trades / kept pre-window), then
classifies by the first matching sub-cause.

usage: python -m analysis.a8_queue <chunk-parent dir> --hl-data DIR --misses report/validate/a0_retriage_dev20/misses.csv
       --out DIR
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analysis import validate_replay as vr
from engine_py.matching import PLACEHOLDER
from engine_py.types import Modify, Place, Tif
from replay.adapter import options_for, run_kwargs_from_options, split_policy

SUBS = ("partial fill / size", "price", "named order placement", "modify semantics", "priority window",
        "placement order within a block", "older order the chain did not fill", "other")


class QueueObserver(vr.ValidateObserver):
    last = None
    targets: set = set()

    def __init__(self, real_by_hash, alo_truth):
        super().__init__(real_by_hash, alo_truth)
        QueueObserver.last = self
        self.snap = {}

    def origin(self, rp, node):
        o = node.order
        act = rp.placed_by.get(o.oid)
        base = {"oid": o.oid, "user": o.user, "remaining": node.remaining, "seq": o.seq, "order_block": o.block}
        if act is None:
            return dict(base, origin="placeholder" if o.user == PLACEHOLDER else "not placed in window")
        q = act.new if isinstance(act, Modify) else act
        return dict(base, origin="modify" if isinstance(act, Modify) else "place", block=act.block,
                    tx_index=act.tx_index, item=act.item_index, time=self.block_time.get(act.block),
                    p=act.priority, tif=q.tif.name, hash=act.hash)

    def before_action(self, rp, block, a):
        super().before_action(rp, block, a)
        if not hasattr(self, "block_time"):
            self.block_time = {b.height: b.time_ms for b in rp.blocks}
        p = a.new if isinstance(a, Modify) else a
        if a.hash not in self.targets or a.hash in self.snap or not isinstance(p, Place) or p.tif is Tif.ALO:
            return
        opp = p.side.opposite
        side = rp.engine.books[p.asset].side(opp)
        items = list(side.items())
        best = items[-12:][::-1] if opp.name == "BUY" else items[:12]     # best 12 levels (bids: highest first)
        self.snap[a.hash] = {"block": block.height, "time": block.time_ms,
                             "levels": {px: [self.origin(rp, n) for n in lv] for px, lv in best}}


def classify(miss, snap, rec):
    d = json.loads(miss["detail"])
    m_pre, p = d["maker"], d["px"]
    e_px, e_pre = d["engine"]
    real = rec.real
    m_full = next(mk for px, _, mk in real if px == p and mk.startswith(m_pre))
    eng = next((e for e in rec.engine if e[0] == e_px and e[2] and e[2].startswith(e_pre)), None)
    lv = snap["levels"]
    at_p = lv.get(p, [])
    e_ord = next((o for o in lv.get(e_px, []) if eng is not None and o["oid"] == eng[3]), None)
    m_ords = [o for o in at_p if o["user"] == m_full]
    info = {"M": m_pre, "E": e_pre, "p": p, "p_engine": e_px, "E_order": e_ord, "M_orders": m_ords,
            "queue_at_p": [(o["user"][:10], o["origin"], o["remaining"]) for o in at_p][:12]}
    if e_pre and m_full.startswith(e_pre):
        return SUBS[0], info
    if e_px != p:
        return SUBS[1], info
    if e_ord is None or not m_ords:
        return SUBS[7], dict(info, why="E's or M's order not found at p before the tx")
    m0 = m_ords[0]
    if e_ord["origin"] == "not placed in window":
        return SUBS[2], info
    if "modify" in (e_ord["origin"], m0["origin"]):
        return SUBS[3], info
    if (e_ord.get("tif") == "ALO" and m0.get("tif") == "ALO" and e_ord.get("time") is not None
            and m0.get("time") is not None and abs(e_ord["time"] - m0["time"]) < 400
            and (e_ord["seq"] < m0["seq"]) != ((e_ord["block"], e_ord["tx_index"]) < (m0["block"], m0["tx_index"]))):
        return SUBS[4], info
    if e_ord.get("block") == m0.get("block"):
        return SUBS[5], info
    if (e_ord.get("block") or 0) < (m0.get("block") or 0):
        return SUBS[6], info
    return SUBS[7], info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks_dir")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--misses", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    misses = [r for r in csv.DictReader(open(a.misses)) if r["cause"] == "queue order at the right price"]
    by_chunk = defaultdict(list)
    for r in misses:
        by_chunk[r["chunk"]].append(r)
    hl = Path(a.hl_data)
    policy, opts = split_policy(options_for())
    kw = run_kwargs_from_options(hl, opts)
    out = []
    for chunk, ms in sorted(by_chunk.items()):
        QueueObserver.targets = {m["hash"] for m in ms}
        vr.run(Path(a.chunks_dir) / chunk, hl, policy, quiet=True, observer_cls=QueueObserver, **kw)
        obs = QueueObserver.last
        for m in ms:
            sub, info = classify(m, obs.snap[m["hash"]], obs.txs[m["hash"]])
            out.append(dict(chunk=chunk, block=m["block"], hash=m["hash"], coin=m["coin"], missed=int(m["missed"]),
                            sub=sub, info=json.dumps(info, default=str)))
        print(f"{chunk}: {len(ms)} queue misses", file=sys.stderr, flush=True)
    od = Path(a.out)
    od.mkdir(parents=True, exist_ok=True)
    with open(od / "queue_misses.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, list(out[0]))
        w.writeheader()
        w.writerows(out)
    tot = sum(x["missed"] for x in out)
    for s in SUBS:
        xs = [x for x in out if x["sub"] == s]
        print(f"{s:40} {len(xs):3d}  {sum(x['missed'] for x in xs) / tot:6.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
