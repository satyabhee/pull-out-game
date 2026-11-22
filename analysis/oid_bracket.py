"""Can reference points pin down which order an int-oid cancel hits? Held-out measurement, no engine change.

Hyperliquid oids come from one global counter. Count every successful order item in the blocks (any asset):
position k. Then c(k) = oid - k is NON-DECREASING in k (invisible events only ever add ids). Two consequences:
  1. any order whose real oid we know (a reference point) pins c at its position;
  2. an order placed at position k_j, between reference points (k0, c0) and (k1, c1), has
     c0 <= oid - k_j <= c1. A cancel naming oid O by user U can only hit U's placements on that asset
     with  c0(k_j) <= O - k_j <= c1(k_j).  If exactly one placement qualifies, the cancel is resolved
     with no tolerance and no guess.

Reference points: the tracked users' orderUpdates on EVERY coin (not only BTC/ETH), located in the blocks by
cloid (else user, price, size, time). The replay's facts loader keeps BTC/ETH only.

Truth for scoring: cancels by a tracked user whose target oid is one of that user's located orders. Held out:
the bracket for those is built WITHOUT that user's reference points.

usage: python -m analysis.oid_bracket <chunk dirs or parents...> --hl-data H [--limit N]
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from data.oids import OidEstimator, _cancel_items, _order_items
from data.parse import _block_files, read_block


def norm(x) -> str:
    try:
        return format(Decimal(str(x)).normalize(), "f")
    except Exception:
        return str(x)


def scan_blocks(window: Path):
    """Placements (k, user, asset, key_cloid, key_shape) and BTC/ETH int-oid cancels (k_at, user, asset, oid)."""
    places, cancels, k = [], [], 0
    t0 = t1 = None
    for _, path in _block_files(window):
        b = read_block(path)
        t0 = t0 or b.get("blockTime")
        t1 = b.get("blockTime")
        for tx in b["txs"]:
            if tx["error"] is not None:
                continue
            user = tx["user"].lower()
            for a, o in _cancel_items(tx["action"]):
                cancels.append((k, user, a, o))
            for item in _order_items(tx["action"]):
                c = item.get("c")
                places.append((k, user, item.get("a"), c,
                               (user, norm(item.get("p")), norm(item.get("s")), int(tx["time"]))))
                k += 1
    return places, cancels, t0, t1


def stream_orders(hl_data: Path, t0: int, t1: int, margin=60_000):
    """oid -> (user or '', cloid, (user, px, origSz, timestamp)) from orderUpdates within [t0-m, t1+m]."""
    out = {}
    hours = set()
    for t in range(t0 - margin, t1 + margin + 3_600_000, 3_600_000):
        import datetime as dt
        d = dt.datetime.utcfromtimestamp(t / 1000)
        hours.add(hl_data / d.strftime("%Y-%m-%d") / d.strftime("%H") / "user_orders.jsonl.gz")
    for p in sorted(hours):
        if not p.exists():
            continue
        with gzip.open(p, "rt") as f:
            for line in f:
                r = json.loads(line)
                if r["recv_ms"] < t0 - margin or r["recv_ms"] > t1 + margin:
                    continue
                m = r["msg"]
                user = (m.get("user") or "").lower()
                for x in m.get("data", []):
                    o = x.get("order", {})
                    if "oid" not in o:
                        continue
                    ts = int(o.get("timestamp", 0))
                    if ts < t0 - 5_000 or ts > t1 + 5_000:
                        continue                       # placed outside the window: no position in our count
                    out[int(o["oid"])] = (user, o.get("cloid"),
                                          (user, norm(o.get("limitPx")), norm(o.get("origSz")), ts))
    return out


def locate(places, orders):
    by_cloid = defaultdict(list)
    by_shape = defaultdict(list)
    by_shape_nouser = defaultdict(list)
    for k, user, a, c, shape in places:
        if c:
            by_cloid[(c, shape[3])].append((k, user, a))
        by_shape[shape].append((k, user, a))
        by_shape_nouser[shape[1:]].append((k, user, a))
    refs = []                                          # (k, oid, user, asset)
    for oid, (user, cloid, shape) in orders.items():
        hits = by_cloid.get((cloid, shape[3])) if cloid else None
        if not hits:
            hits = by_shape.get(shape) if user else by_shape_nouser.get(shape[1:])
        if hits and len(hits) == 1:
            k, u, a = hits[0]
            refs.append((k, oid, u, a))
    return refs


class Bracket:
    def __init__(self, pts):
        pts = OidEstimator.consistent([(k, o) for k, o in pts])
        self.k = [k for k, _ in pts]
        self.c = [o - k for k, o in pts]

    def bounds(self, kj):
        """(lo, hi) for c at position kj; None side = unbounded."""
        i = bisect_right(self.k, kj)
        lo = self.c[i - 1] if i > 0 else None
        if i > 0 and self.k[i - 1] == kj:
            return lo, lo
        hi = self.c[i] if i < len(self.k) else None
        return lo, hi


def candidates(br: Bracket, cancel, placers):
    k_at, user, a, oid = cancel
    ks = placers.get((user, a), [])
    ks = ks[:bisect_left(ks, k_at)]
    out = []
    for kj in ks:
        lo, hi = br.bounds(kj)
        c = oid - kj
        if (lo is None or c >= lo) and (hi is None or c <= hi):
            out.append(kj)
    return out


def run(window: Path, hl_data: Path):
    places, cancels, t0, t1 = scan_blocks(window)
    orders = stream_orders(hl_data, t0, t1)
    refs = locate(places, orders)
    k_of_oid = {o: k for k, o, _, _ in refs}
    placers = defaultdict(list)
    for k, user, a, _, _ in places:
        placers[(user, a)].append(k)
    btceth = {0, 1}
    full = Bracket([(k, o) for k, o, _, _ in refs])
    only = Bracket([(k, o) for k, o, _, a in refs if a in btceth])
    st = Counter()
    st["items"] = len(places)
    st["refs_all"] = len(full.k)
    st["refs_btceth"] = len(only.k)
    # monotone check of the raw located refs
    pts = sorted((k, o) for k, o, _, _ in refs)
    st["pairs"] = max(0, len(pts) - 1)
    st["pairs_c_decreasing"] = sum(1 for (k0, o0), (k1, o1) in zip(pts, pts[1:]) if o1 - k1 < o0 - k0)
    gaps = [b - a for a, b in zip(full.c, full.c[1:])]
    st["c_steps_zero"] = sum(g == 0 for g in gaps)
    st["c_steps"] = len(gaps)
    by_user_refs = defaultdict(list)
    for k, o, u, a in refs:
        by_user_refs[u].append((k, o))
    held = {}
    for cancel in cancels:
        k_at, user, a, oid = cancel
        truth = k_of_oid.get(oid)
        for name, br in (("all", full), ("btceth", only)):
            n = len(candidates(br, cancel, placers))
            st[f"{name}_n{min(n, 2)}"] += 1
        if truth is not None and truth < k_at:
            if user not in held:
                mine = set(by_user_refs[user])
                held[user] = (Bracket([(k, o) for k, o, u, _ in refs if u != user]),
                              Bracket([(k, o) for k, o, u, a2 in refs if u != user and a2 in btceth]))
            for name, br in zip(("all", "btceth"), held[user]):
                cs = candidates(br, cancel, placers)
                st[f"truth_{name}_total"] += 1
                if len(cs) == 1:
                    st[f"truth_{name}_unique"] += 1
                    st[f"truth_{name}_unique_right"] += cs[0] == truth
                st[f"truth_{name}_contains"] += truth in cs
    return st


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    wins = []
    for d in map(Path, a.dirs):
        wins += [d] if (d / "window.json").exists() else sorted(p for p in d.iterdir() if (p / "window.json").exists())
    if a.limit:
        wins = wins[:a.limit]
    tot = Counter()
    for w in wins:
        st = run(w, Path(a.hl_data))
        tot.update(st)
        print(w.name, dict(st), flush=True)
    print("\nTOTAL", dict(tot))
    f = lambda x, y: f"{x}/{y} = {x / y:.3f}" if y else "n/a"
    print("ref points per 1000 counted items: all coins", round(1000 * tot["refs_all"] / tot["items"], 1),
          "| BTC/ETH only", round(1000 * tot["refs_btceth"] / tot["items"], 1))
    print("c(k) decreasing between consecutive located refs:", f(tot["pairs_c_decreasing"], tot["pairs"]))
    print("c(k) unchanged between consecutive kept refs (exact counting):", f(tot["c_steps_zero"], tot["c_steps"]))
    for name in ("all", "btceth"):
        n = sum(tot[f"{name}_n{i}"] for i in range(3))
        print(f"[{name} refs] every BTC/ETH int-oid cancel: 0 cand {f(tot[name+'_n0'], n)}, "
              f"unique {f(tot[name+'_n1'], n)}, ambiguous {f(tot[name+'_n2'], n)}")
        print(f"[{name} refs] held-out truth: unique {f(tot[f'truth_{name}_unique'], tot[f'truth_{name}_total'])}, "
              f"right when unique {f(tot[f'truth_{name}_unique_right'], tot[f'truth_{name}_unique'])}, "
              f"truth inside bracket {f(tot[f'truth_{name}_contains'], tot[f'truth_{name}_total'])}")


if __name__ == "__main__":
    main()
