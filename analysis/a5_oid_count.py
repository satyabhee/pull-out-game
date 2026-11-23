"""A5 Part 1: does every order item take the next Hyperliquid oid? (rule and variants: docs/scoreboard.md, A5,
written before this ran).

For each chunk: every order item of every tx on every asset (errored txs included) gets a listing index j. Truth
points (real oid known) come from capture's orderStatus answers (anchors.json; their recorded position when given),
the oid facts (data/oid_facts.load) and the recorder's user_orders on every coin (stream_orders_all); each is located
at exactly one item by (user, cloid, time) or (user, px, size, time), else dropped; then the longest chain increasing
in both j and oid is kept (OidEstimator.consistent). For consecutive points a < b:
    r = (oid_b - oid_a) - (items in (a, b] that the variant counts)
V0 = items of non-errored order / modify / batchModify txs (any asset, triggers included) = rule B; V1 = V0 + errored
txs' items; V2 = V0 without trigger items.

usage: python -m analysis.a5_oid_count <chunk-parent dir> [<chunk-parent dir> ...] --hl-data DIR --out FILE.json
"""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path

from analysis import validate_chunks as vc
from data import oid_facts
from data.oid_facts import _norm, stream_orders_all
from data.oids import OidEstimator, _order_items
from data.parse import _block_files, read_block

VARIANTS = ("V0", "V1", "V2")
SCOPES = ("same block", "1 boundary", "2-5 boundaries", ">5 boundaries")


def scope(d: int) -> str:
    return SCOPES[0] if d == 0 else SCOPES[1] if d == 1 else SCOPES[2] if d <= 5 else SCOPES[3]


def items_of(chunk: Path) -> tuple[list[dict], list[dict]]:
    """(items in listing order, txs with their first item index and type)."""
    items, txs = [], []
    for height, path in _block_files(chunk):
        b = read_block(path)
        for t, tx in enumerate(b["txs"]):
            act = tx["action"]
            txs.append({"height": height, "t": t, "type": act.get("type"), "j0": len(items),
                        "err": tx.get("error") is not None})
            for i, it in enumerate(_order_items(act)):
                items.append({"height": height, "t": t, "i": i, "asset": it.get("a"),
                              "trigger": "trigger" in (it.get("t") or {}), "err": tx.get("error") is not None,
                              "user": tx["user"].lower(), "cloid": it.get("c"), "px": _norm(it.get("p")),
                              "sz": _norm(it.get("s")), "time": int(tx["time"]), "kind": act.get("type")})
    return items, txs


def truth_points(chunk: Path, hl_data: Path, items: list[dict]) -> tuple[list[tuple[int, int]], Counter]:
    st = Counter()
    pos_index = {(x["height"], x["t"], x["i"]): j for j, x in enumerate(items)}
    by_cloid, by_shape, by_shape_nouser = defaultdict(list), defaultdict(list), defaultdict(list)
    for j, x in enumerate(items):
        if x["cloid"]:
            by_cloid[(x["cloid"].lower(), x["time"])].append(j)
        by_shape[(x["user"], x["px"], x["sz"], x["time"])].append(j)
        by_shape_nouser[(x["px"], x["sz"], x["time"])].append(j)
    cand: dict[int, set] = defaultdict(set)          # real oid -> located item indices (from every source)

    def locate(oid, user, cloid, px, sz, ts, src):
        hits = by_cloid.get((cloid.lower(), ts)) if cloid else None
        if hits and user:
            hits = [j for j in hits if items[j]["user"] == user]
        if not hits:
            hits = by_shape.get((user, px, sz, ts)) if user else by_shape_nouser.get((px, sz, ts))
        if hits and len(hits) == 1:
            cand[oid].add(hits[0])
            st[f"located:{src}"] += 1
        else:
            st[f"unlocated:{src}"] += 1

    path = chunk / "anchors.json"
    for a in (json.load(open(path)) if path.exists() else []):
        ans = a.get("answer") or {}
        if ans.get("status") != "order":
            continue
        o = ans["order"]["order"]
        oid = int(o["oid"])
        if a.get("placed_height") is not None and (a["placed_height"], a.get("tx"), a.get("item")) in pos_index:
            cand[oid].add(pos_index[(a["placed_height"], a["tx"], a["item"])])
            st["located:anchors(position)"] += 1
            continue
        locate(oid, a["user"].lower(), o.get("cloid"), _norm(o["limitPx"]), _norm(o.get("origSz", o.get("sz"))),
               int(o["timestamp"]), "anchors")
    for o in oid_facts.load(chunk, hl_data).orders:
        locate(o["oid"], o["user"], o["cloid"], _norm(o["px"]), _norm(o["sz"]), o["timestamp"], "facts")
    for oid, (user, cloid, shape, ts) in stream_orders_all(chunk, hl_data).items():
        locate(oid, user, cloid, shape[1], shape[2], ts, "user_orders")
    pts = []
    for oid, js in cand.items():
        if len(js) == 1:
            pts.append((next(iter(js)), oid))
        else:
            st["dropped:sources disagree"] += 1
    good = OidEstimator.consistent(sorted(set(pts)))
    st["points"] = len(good)
    st["dropped:not monotone"] = len(set(pts)) - len(good)
    return good, st


def weights(x: dict, v: str) -> int:
    if v == "V0":
        return int(not x["err"])
    if v == "V1":
        return 1
    return int(not x["err"] and not x["trigger"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sets", nargs="+")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    hl = Path(a.hl_data)
    tab = {v: {s: Counter() for s in SCOPES} for v in VARIANTS}
    between = {s: Counter() for s in ("r=0", "r>0", "r<0")}
    rdist = Counter()
    stats = Counter()
    examples = []
    for root in a.sets:
        chunks, _ = vc.find_chunks([root])
        for ch in chunks:
            ch = Path(ch)
            items, txs = items_of(ch)
            pts, st = truth_points(ch, hl, items)
            stats.update(st)
            stats["items"] += len(items)
            stats["items:errored"] += sum(x["err"] for x in items)
            stats["items:trigger"] += sum(x["trigger"] for x in items)
            pre = {v: [0] for v in VARIANTS}
            for x in items:
                for v in VARIANTS:
                    pre[v].append(pre[v][-1] + weights(x, v))
            tx_keys = [(t["height"], t["t"]) for t in txs]       # txs are in listing order
            for (ja, oa), (jb, ob) in zip(pts, pts[1:]):
                d = items[jb]["height"] - items[ja]["height"]
                sc = scope(d)
                for v in VARIANTS:
                    r = (ob - oa) - (pre[v][jb + 1] - pre[v][ja + 1])
                    tab[v][sc]["pairs"] += 1
                    tab[v][sc]["r=0" if r == 0 else "r>0" if r > 0 else "r<0"] += 1
                    if v == "V0":
                        rdist[(sc, max(-3, min(r, 10)))] += 1
                        key = "r=0" if r == 0 else "r>0" if r > 0 else "r<0"
                        ha, hb = items[ja]["height"], items[jb]["height"]
                        ta, tb = (ha, items[ja]["t"]), (hb, items[jb]["t"])
                        between[key]["pairs"] += 1
                        between[key]["boundaries"] += d
                        for t in txs[bisect_right(tx_keys, ta):bisect_right(tx_keys, tb)]:
                            between[key][f"tx:{t['type']}{'(err)' if t['err'] else ''}"] += 1
                        if r != 0 and len(examples) < 40:
                            examples.append({"chunk": ch.name, "a": items[ja] | {"oid": oa}, "b": items[jb] | {"oid": ob},
                                             "r": r, "boundaries": d})
            print(f"{ch.name}: {st['points']} points", file=sys.stderr, flush=True)
    out = {"stats": dict(stats), "table": {v: {s: dict(c) for s, c in t.items()} for v, t in tab.items()},
           "r_V0": {f"{s}|{r}": n for (s, r), n in sorted(rdist.items())},
           "between_V0": {k: dict(c.most_common(25)) for k, c in between.items()}, "examples": examples}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(a.out, "w"), indent=1, default=str)
    for v in VARIANTS:
        for s in SCOPES:
            c = tab[v][s]
            n = c["pairs"]
            print(f"{v} {s:15} pairs {n:6d}  r=0 {c['r=0'] / n if n else 0:6.1%}  r>0 {c['r>0']:5d}  r<0 {c['r<0']:5d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
