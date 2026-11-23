"""A8 hypothesis test on the chain's fills: for consecutive real fills at one price by two makers whose latest ALO
placements at that price (before the tx) are in the SAME block, does the chain fill them in listing order, and how
does that relate to their priority bids (grouping p)?

Raw data only (explorer blocks + recorded trades); no engine. A maker's "latest placement" is its last ALO item at
that asset/side/price before the tx; pairs where either maker placed more than once at that price in that block are
skipped (ambiguous).

usage: python -m analysis.same_block_queue <chunk-parent dir> --hl-data DIR
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from data.parse import iter_trades
from engine_py.types import ASSETS, Side

COIN = {0: "BTC", 1: "ETH"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks_dir")
    ap.add_argument("--hl-data", required=True)
    a = ap.parse_args(argv)
    hl = Path(a.hl_data)
    res = Counter()
    for ch in sorted(p for p in Path(a.chunks_dir).iterdir() if p.is_dir()):
        files = sorted(ch.glob("*.json.gz"), key=lambda p: int(p.name.split(".")[0]))
        last = {}                                   # (user, asset, buy, px ticks) -> (height, tx_index, p, n_in_block)
        tx_at = {}                                  # hash -> (height, tx_index)
        times = {}
        blocks = []
        for f in files:
            b = json.load(gzip.open(f))["blockDetails"]
            blocks.append(b)
            times[b["height"]] = b["blockTime"]
            for k, t in enumerate(b["txs"]):
                tx_at[t["hash"]] = (b["height"], k)
        events = defaultdict(list)                  # height -> placements in that block (applied after the block)
        t0, t1 = times[blocks[0]["height"]], times[blocks[-1]["height"]]
        trades_by_hash = defaultdict(list)
        for tr in iter_trades(hl, ["BTC", "ETH"], t0, t1):
            trades_by_hash[tr.hash].append(tr)
        for b in blocks:
            h = b["height"]
            seen_here = Counter()
            for k, t in enumerate(b["txs"]):
                act = t["action"]
                if not t.get("error") and act.get("type") == "order":
                    for o in act.get("orders", []):
                        if o.get("a") in ASSETS and o.get("t", {}).get("limit", {}).get("tif") == "Alo":
                            spec = ASSETS[o["a"]]
                            key = (t["user"].lower(), o["a"], bool(o["b"]), spec.px_to_ticks(o["p"]))
                            seen_here[key] += 1
                            last[key] = (h, k, (act.get("grouping") or {}).get("p") if isinstance(act.get("grouping"), dict) else None)
                trs = trades_by_hash.get(t["hash"])
                if not trs:
                    continue
                trs = sorted(trs, key=lambda x: x.seq)
                for x, y in zip(trs, trs[1:]):
                    if x.px != y.px or x.maker == y.maker:
                        continue
                    buy_maker = x.taker_side is Side.SELL
                    kx = (x.maker.lower(), x.asset, buy_maker, x.px)
                    ky = (y.maker.lower(), y.asset, buy_maker, y.px)
                    lx, ly = last.get(kx), last.get(ky)
                    if not lx or not ly or lx[0] != ly[0] or lx[0] == h:
                        continue
                    res["pairs"] += 1
                    res["listing order agrees" if lx[1] < ly[1] else "listing order disagrees"] += 1
                    px_, py_ = lx[2], ly[2]
                    if px_ is not None and py_ is not None and px_ != py_:
                        res["first filled has HIGHER p" if px_ > py_ else "first filled has LOWER p"] += 1
                        if lx[1] < ly[1]:
                            res["agrees & higher p listed first" if px_ > py_ else "agrees & lower p listed first"] += 1
                        else:
                            res["disagrees & higher p listed first" if py_ > px_ else "disagrees & lower p listed first"] += 1
        print(f"{ch.name}: pairs so far {res['pairs']}", file=sys.stderr, flush=True)
    for k, v in sorted(res.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
