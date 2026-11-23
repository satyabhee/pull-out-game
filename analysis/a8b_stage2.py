"""A8b Stage 2 (hypotheses H1-H3 in docs/scoreboard.md, written before this ran): raw chain data, no engine.

For every pair of consecutive real fills at one price inside one taker tx by two different makers, take each maker's
latest ALO placement at that asset / side / price before the taker's block (placements in the taker's own block and
makers with more than one placement at that key in the placement's block are skipped). The chain filled X before Y.
  H1 lower p first:   applies when p differs (missing p = 0); predicts the lower-p order first.
  H2 smaller first:   applies when the original sizes differ; predicts the smaller one first.
  H3 FIFO:            applies always (placements are distinct); predicts the earlier (block, tx, item) first.
Supported iff correct in >= 80% of the pairs it applies to, with >= 30 pairs.

usage: python -m analysis.a8b_stage2 <chunk-parent dir> --hl-data DIR [--out FILE.json]
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from data.parse import iter_trades
from engine_py.types import ASSETS, Side


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks_dir")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    hl = Path(a.hl_data)
    res = Counter()
    per_chunk = defaultdict(Counter)
    for ch in sorted(p for p in Path(a.chunks_dir).iterdir() if p.is_dir()):
        files = sorted(ch.glob("*.json.gz"), key=lambda p: int(p.name.split(".")[0]))
        blocks = [json.load(gzip.open(f))["blockDetails"] for f in files]
        if not blocks:
            continue
        t0, t1 = blocks[0]["blockTime"], blocks[-1]["blockTime"]
        trades_by_hash = defaultdict(list)
        for tr in iter_trades(hl, ["BTC", "ETH"], t0, t1):
            trades_by_hash[tr.hash].append(tr)
        last = {}        # (user, asset, buy, px) -> (height, tx, item, p, size, ambiguous)
        for b in blocks:
            h = b["height"]
            here = Counter()
            pending = {}
            for k, t in enumerate(b["txs"]):
                act = t["action"]
                trs = trades_by_hash.get(t["hash"])
                if trs:                                   # judge against placements from EARLIER blocks only
                    trs = sorted(trs, key=lambda x: x.seq)
                    for x, y in zip(trs, trs[1:]):
                        if x.px != y.px or x.maker == y.maker:
                            continue
                        buy_maker = x.taker_side is Side.SELL
                        lx = last.get((x.maker.lower(), x.asset, buy_maker, x.px))
                        ly = last.get((y.maker.lower(), y.asset, buy_maker, y.px))
                        if not lx or not ly or lx[5] or ly[5]:
                            res["pairs skipped (unknown / ambiguous placement)"] += 1
                            continue
                        res["pairs"] += 1
                        per_chunk[ch.name]["pairs"] += 1
                        if lx[3] != ly[3]:
                            res["H1 applies"] += 1
                            res["H1 right"] += lx[3] < ly[3]
                        if lx[4] != ly[4]:
                            res["H2 applies"] += 1
                            res["H2 right"] += lx[4] < ly[4]
                        res["H3 applies"] += 1
                        res["H3 right"] += lx[:3] < ly[:3]
                        per_chunk[ch.name]["H3 right"] += lx[:3] < ly[:3]
                if not t.get("error") and act.get("type") == "order":
                    g = act.get("grouping")
                    p = (g.get("p") if isinstance(g, dict) else None) or 0
                    for j, o in enumerate(act.get("orders", [])):
                        if o.get("a") in ASSETS and o.get("t", {}).get("limit", {}).get("tif") == "Alo":
                            spec = ASSETS[o["a"]]
                            key = (t["user"].lower(), o["a"], bool(o["b"]), spec.px_to_ticks(o["p"]))
                            here[key] += 1
                            pending[key] = (h, k, j, p, Decimal(o["s"]))
            for key, v in pending.items():                # placements take effect for LATER blocks
                last[key] = v + (here[key] > 1,)
        print(f"{ch.name}: pairs so far {res['pairs']}", file=sys.stderr, flush=True)
    out = dict(res)
    for hname in ("H1", "H2", "H3"):
        n, r = res[f"{hname} applies"], res[f"{hname} right"]
        out[f"{hname} share"] = r / n if n else None
        out[f"{hname} supported"] = bool(n >= 30 and r / n >= 0.80)
    print(json.dumps(out, indent=1))
    if a.out:
        json.dump({"summary": out, "per_chunk": per_chunk}, open(a.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
