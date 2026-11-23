"""A8b Stage 1: case table for "queue order at the right price" misses (pre-registration in docs/scoreboard.md).

For every miss whose A0 cause (analysis/retriage.py on B1) is "queue order at the right price", re-runs the chunk on
the Python reference engine with the given replay flags and, just before the tx's first action, records our queue at
the fill price p on the side the taker hits: every order in queue order with its owner, remaining size and how it got
there (placing action kind Place / Modify, block, block time, listing position tx/item, priority p, tif, reduce-only,
cloid). E = our first filled maker (detail "engine"), M = the chain's maker. Per case: E's and M's queue index, how many
of our orders the chain's fill skipped (M's index), and E vs M on every recorded field.

usage: python -m analysis.a8b_cases MISSES.csv CHUNK_PARENT --hl-data DIR [replay flags] --out FILE.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from analysis import validate_replay as vr
from engine_py.matching import PLACEHOLDER
from engine_py.types import Modify, Place, Side

ASSET = {"BTC": 0, "ETH": 1}
QUEUE = "queue order at the right price"


def order_info(replay, oid: int, times: dict) -> dict:
    act = replay.placed_by.get(oid)
    if act is None:
        return {"how": "seed/refill"}
    p = act.new if isinstance(act, Modify) else act
    return {"how": type(act).__name__, "block": act.block, "block_ms": times.get(act.block), "tx": act.tx_index,
            "item": act.item_index, "prio": act.priority, "tif": p.tif.name, "reduce_only": bool(p.reduce_only),
            "cloid": p.cloid, "orig_sz": p.sz, "hash": act.hash}


def make_observer(targets: dict, out: list, chunk: str):
    """targets: tx hash -> case dict (maker, engine maker, px, coin)."""
    class Obs(vr.ValidateObserver):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._seen: set[str] = set()
            self._times = None

        def before_action(self, replay, block, action):
            h = action.hash
            if h in targets and h not in self._seen and isinstance(action, (Place, Modify)):
                self._seen.add(h)
                if self._times is None:
                    self._times = {b.height: b.time_ms for b in replay.blocks}
                c = targets[h]
                p = action.new if isinstance(action, Modify) else action
                side = p.side.opposite
                lvl = replay.engine.books[ASSET[c["coin"]]].side(side).get(c["px"])
                q = []
                for i, nd in enumerate(lvl or ()):
                    u = nd.order.user
                    q.append({"i": i, "oid": nd.order.oid, "user": "placeholder" if u == PLACEHOLDER else u.lower(),
                              "rem": nd.remaining, **order_info(replay, nd.order.oid, self._times)})
                m_idx = [r["i"] for r in q if r["user"].startswith(c["maker"])]
                e_idx = [r["i"] for r in q if c["engine"] and r["user"].startswith(c["engine"])]
                out.append({"chunk": chunk, "hash": h, "block": block.height, "block_ms": block.time_ms,
                            "coin": c["coin"], "px": c["px"], "taker": action.user.lower(),
                            "taker_tif": p.tif.name, "maker": c["maker"], "engine": c["engine"],
                            "m_idx": m_idx, "e_idx": e_idx, "skipped": m_idx[0] if m_idx else None,
                            "missed": c["missed"], "queue": q})
            return super().before_action(replay, block, action)

    return Obs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("misses")
    ap.add_argument("chunks")
    ap.add_argument("--out", required=True)
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    by_chunk: dict[str, dict] = defaultdict(dict)
    for r in csv.DictReader(open(a.misses)):
        if r["cause"] != QUEUE:
            continue
        d = json.loads(r["detail"])
        eng = d.get("engine")
        by_chunk[r["chunk"]][r["hash"]] = {"maker": d["maker"].lower(), "px": int(d["px"]), "coin": r["coin"],
                                           "engine": eng[1].lower() if eng else None, "missed": int(r["missed"])}
    kw = vr.replay_kwargs(a)
    out: list = []
    for chunk in sorted(by_chunk):
        vr.run(Path(a.chunks) / chunk, a.hl_data, a.policy, out_dir=None, quiet=True,
               observer_cls=make_observer(by_chunk[chunk], out, chunk), **kw)
        print(f"{chunk}: {len(by_chunk[chunk])} case(s)", file=sys.stderr)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(a.out, "w"), indent=1, default=str)
    print(f"{len(out)} cases recorded of {sum(len(v) for v in by_chunk.values())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
