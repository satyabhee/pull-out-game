"""outside_feed: where are the phantom-maker fills whose price is not in the aligned 5-level fast feed?

Input: the --cases CSV of analysis/phantom_l2n (one row per phantom maker fill; bucket "level not in feed"), the chunk
folders, the recorder data and (optional) mahito's A0 misses.csv files (cause per contested miss, joined by tx hash).
For each such fill at price p on maker side S in block B (block time bt):
  feed  = the last book_<coin>_fast update with time < bt (same alignment as phantom_l2n)
  where = "in front of the real best" (p better than the feed's best on S: our level sits ahead of the chain's touch),
          "crosses the other side" (p at or through the feed's best on the opposite side),
          "gap inside the 5 levels" (p between the feed's best and 5th level on S, but absent: the chain has no
          order there), "behind the 5th level" (p worse than the feed's 5th level: the feed cannot see it)
  slow  = the last 20-level book_<coin> snapshot (with order counts) before bt, when p is within its levels:
          "slow: level present (n=k)" / "slow: level absent" / "slow: beyond 20 levels" / "slow: none within 30 s"
  placed = when the maker's order at (S, p) was placed (the maker's last placement at p on S in the chunk's blocks up
          to B): "after the feed update" (the feed could not show it yet), "before the feed update", "not in the
          chunk" (placed before the fetched blocks: pre-window order)
usage: python -m analysis.outside_feed CASES.csv CHUNKS_ROOT --hl-data H [--a0 'glob/misses.csv'] [--out rows.csv]
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path

from analysis.phantom_l2n import load_fast
from data.parse import _hour_files, _records
from engine_py.types import ASSETS, Side

SLOW_MAX_AGE_MS = 30_000
COIN = {a.name: i for i, a in ASSETS.items()}


def load_slow(hl_data, asset, t0, t1):
    """[(time, {Side: {px ticks: n}})] of the 20-level book_<coin> snapshots in [t0, t1], time order."""
    a = ASSETS[asset]
    seen = {}
    for path in _hour_files(Path(hl_data), f"book_{a.name}", t0, t1):
        for rec in _records(path, t0, t1):
            d = (rec.get("msg") or {}).get("data") or {}
            if "levels" in d and t0 <= d.get("time", -1) <= t1:
                seen[d["time"]] = d["levels"]
    out = []
    for t in sorted(seen):
        bids, asks = seen[t]
        out.append((t, {Side.BUY: {a.px_to_ticks(l["px"]): l.get("n") for l in bids},
                        Side.SELL: {a.px_to_ticks(l["px"]): l.get("n") for l in asks}}))
    return out


def better(side, x, y):
    """x is a better price than y for a resting order on `side` (higher bid / lower ask)."""
    return x > y if side is Side.BUY else x < y


def where(side, px, levels):
    own = sorted(levels[side], reverse=side is Side.BUY)            # best first
    opp = sorted(levels[side.opposite], reverse=side.opposite is Side.BUY)
    if opp and (px == opp[0] or better(side, px, opp[0])):
        return "crosses the other side"
    if not own:
        return "behind the 5th level"
    if better(side, px, own[0]):
        return "in front of the real best"
    if better(side, own[-1], px):
        return "behind the 5th level"
    return "gap inside the 5 levels"


def block_index(chunk_dir):
    """height -> (block time ms, txs) for every block file in the chunk (loaded lazily by the caller)."""
    out = {}
    for f in Path(chunk_dir).glob("*.json.gz"):
        name = f.name[: -len(".json.gz")]
        if name.isdigit():
            out[int(name)] = f
    return out


def read_block(path):
    with gzip.open(path, "rt") as f:
        b = json.load(f)
    return b.get("blockDetails", b)


def placements(chunk_dir, cache):
    """(user, asset, Side, px ticks) -> [(height, block time)] of every successful placement in the chunk."""
    if chunk_dir in cache:
        return cache[chunk_dir]
    out = defaultdict(list)
    for h, path in sorted(block_index(chunk_dir).items()):
        b = read_block(path)
        bt = b["blockTime"]
        for tx in b["txs"]:
            if tx.get("error") is not None:
                continue
            a = tx.get("action") or {}
            kind = a.get("type")
            items = (a.get("orders", []) if kind == "order" else [a.get("order", {})] if kind == "modify"
                     else [m.get("order", {}) for m in a.get("modifies", [])] if kind == "batchModify" else [])
            for o in items:
                if not isinstance(o, dict) or o.get("a") not in ASSETS or "limit" not in (o.get("t") or {}):
                    continue
                try:
                    px = ASSETS[o["a"]].px_to_ticks(o["p"])
                except Exception:
                    continue
                out[(tx["user"].lower(), o["a"], Side.BUY if o.get("b") else Side.SELL, px)].append((h, bt))
    cache[chunk_dir] = out
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cases")
    ap.add_argument("chunks_root")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--a0", default=None, help="glob of A0 misses.csv files (cause per contested miss)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    rows = [r for r in csv.DictReader(open(args.cases)) if r["bucket"] == "level not in feed"]
    cause = {}
    for p in sorted(glob.glob(args.a0)) if args.a0 else []:
        for r in csv.DictReader(open(p)):
            cause[r["hash"]] = r["cause"]
    feeds, slows, pcache = {}, {}, {}
    out_rows = []
    tally = defaultdict(Counter)
    for r in rows:
        chunk_dir = Path(args.chunks_root) / r["chunk"]
        asset = COIN[r["coin"]]
        side = Side[r["side"]]
        px = ASSETS[asset].px_to_ticks(r["px"])
        h = int(r["block"])
        bt = read_block(block_index(chunk_dir)[h])["blockTime"]
        key = (r["chunk"], asset)
        if key not in feeds:
            t0 = read_block(min(block_index(chunk_dir).items())[1])["blockTime"] - 60_000
            t1 = read_block(max(block_index(chunk_dir).items())[1])["blockTime"]
            feeds[key] = load_fast(args.hl_data, asset, t0, t1)
            slows[key] = load_slow(args.hl_data, asset, t0 - SLOW_MAX_AGE_MS, t1)
        f, s = feeds[key], slows[key]
        i = bisect.bisect_left([t for t, _ in f], bt) - 1
        ft = f[i][0] if i >= 0 else None
        w = where(side, px, f[i][1]) if i >= 0 else "no feed"
        j = bisect.bisect_left([t for t, _ in s], bt) - 1
        if j < 0 or bt - s[j][0] > SLOW_MAX_AGE_MS:
            slow = "slow: none within 30 s"
        else:
            lv = s[j][1][side]
            if px in lv:
                slow = "slow: level present"
            elif lv and better(side, min(lv) if side is Side.BUY else max(lv), px):
                slow = "slow: beyond 20 levels"
            else:
                slow = "slow: level absent"
        pl = [x for x in placements(chunk_dir, pcache).get((r["maker"].lower(), asset, side, px), []) if x[0] <= h]
        if not pl:
            placed = "not in the chunk (pre-window order)"
        elif ft is not None and pl[-1][1] > ft:
            placed = "after the feed update"
        else:
            placed = "before the feed update"
        c = cause.get(r["hash"], "not a contested miss in A0")
        for k, v in (("where", w), ("slow", slow), ("placed", placed), ("A0 cause", c),
                     ("where x placed", f"{w} | {placed}")):
            tally[k][v] += 1
        out_rows.append({**r, "where": w, "slow": slow, "placed": placed, "a0_cause": c,
                         "feed_age_ms": "" if ft is None else bt - ft,
                         "placed_age_ms": "" if not pl else bt - pl[-1][1]})
    print(f"{len(rows)} phantom maker fills at levels not in the aligned 5-level fast feed")
    for k in ("where", "slow", "placed", "where x placed", "A0 cause"):
        print(k)
        for v, n in tally[k].most_common():
            print(f"  {n:5d}  {n / max(1, len(rows)):6.1%}  {v}")
    if args.out:
        with open(args.out, "w", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(out_rows[0]))
            wr.writeheader()
            wr.writerows(out_rows)


if __name__ == "__main__":
    main()
