"""Fidelity where block ordering matters: scored txs split into contested and uncontested.

A scored tx is CONTESTED when, in its own block, one of its makers (a real maker, or an engine maker other
than a placeholder) also sent a cancel (by oid or cloid), a modify, or an ALO order on the same asset. Those
are the txs whose outcome depends on the order of actions inside the block (cancel-priority protects
exactly these makers). "broad": anyone sent a cancel / modify / ALO on that asset in that block.

For each scope: txs, share of real notional, G1 recall (wildcard, notional-weighted), full-strict recall
(maker must match) and precision, per-tx recall (each tx weighs the same), and 95% bootstrap intervals
over chunks for G1 recall and per-tx recall.

Input: a validate_chunks variant folder (txs.csv) and the chunk folders (block files).
usage: python -m analysis.contested <variant dir with txs.csv> <chunk-parent dir> [--boot 2000]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

from engine_py.types import ASSETS

ASSET_OF = {spec.name: a for a, spec in ASSETS.items()}


def users_of(field: str) -> set[str]:
    """Maker prefixes ('0x' + 8 hex) from a txs.csv real/engine column ('sz@px maker; ...')."""
    out = set()
    for part in field.split(";"):
        bits = part.strip().split()
        if len(bits) == 2 and bits[1].startswith("0x"):
            out.add(bits[1].lower()[:10])
    return out


def block_actors(path: Path) -> dict[int, set[str]]:
    """asset -> user prefixes that sent a cancel / cancelByCloid / modify / batchModify / ALO order there."""
    b = json.load(gzip.open(path))["blockDetails"]
    out = defaultdict(set)
    for t in b["txs"]:
        if t.get("error"):
            continue
        a, u = t["action"], t["user"].lower()[:10]
        k = a.get("type")
        assets = []
        if k == "cancel":
            assets = [c.get("a") for c in a.get("cancels", [])]
        elif k == "cancelByCloid":
            assets = [c.get("asset") for c in a.get("cancels", [])]
        elif k == "modify":
            assets = [a.get("order", {}).get("a")]
        elif k == "batchModify":
            assets = [m.get("order", {}).get("a") for m in a.get("modifies", [])]
        elif k == "order":
            assets = [o.get("a") for o in a.get("orders", []) if o.get("t", {}).get("limit", {}).get("tif") == "Alo"]
        for x in assets:
            if x in ASSETS:
                out[x].add(u)
    return out


def classify(rows, chunks_dir: Path):
    cache = {}
    for r in rows:
        key = (r["chunk"], int(r["block"]))
        if key not in cache:
            p = chunks_dir / r["chunk"] / f"{r['block']}.json.gz"
            cache = {key: block_actors(p) if p.exists() else None}       # rows come grouped by chunk/block
        actors = cache[key]
        if actors is None:
            r["scope"] = None
            continue
        acts = actors.get(ASSET_OF[r["coin"]], set())
        makers = users_of(r["real"]) | users_of(r["engine"])
        r["scope"] = "contested" if makers & acts else "uncontested"
    return rows


def stats(rows):
    real = sum(r["real_notional"] for r in rows)
    eng = sum(r["engine_notional"] for r in rows)
    m = sum(r["matched_notional"] for r in rows)
    mf = sum(r["matched_notional_full"] for r in rows)
    per = [r["matched_notional"] / r["real_notional"] for r in rows if r["real_notional"] > 0]
    perf = [r["matched_notional_full"] / r["real_notional"] for r in rows if r["real_notional"] > 0]
    return {"txs": len(rows), "real": real, "g1": m / real if real else float("nan"),
            "full": mf / real if real else float("nan"), "prec_full": mf / eng if eng else float("nan"),
            "per_tx": sum(per) / len(per) if per else float("nan"),
            "per_tx_full": sum(perf) / len(perf) if perf else float("nan")}


def bootstrap(rows, n, seed=7):
    by = defaultdict(list)
    for r in rows:
        by[r["chunk"]].append(r)
    keys = sorted(by)
    rnd = random.Random(seed)
    g, p = [], []
    for _ in range(n):
        sample = [r for k in (rnd.choice(keys) for _ in keys) for r in by[k]]
        s = stats(sample)
        g.append(s["g1"])
        p.append(s["per_tx"])
    q = lambda xs: (sorted(xs)[int(0.025 * len(xs))], sorted(xs)[int(0.975 * len(xs)) - 1])
    return q(g), q(p)


def load(variant_dir: Path):
    rows = []
    for r in csv.DictReader(open(variant_dir / "txs.csv")):
        if r["warmup"] != "0" or r["status"] not in ("match", "mismatch"):
            continue
        for k in ("real_notional", "engine_notional", "matched_notional", "matched_notional_full"):
            r[k] = int(r[k])
        rows.append(r)
    return rows


def report(rows, boot):
    total = sum(r["real_notional"] for r in rows)
    out = [f"{'scope':24} {'txs':>5} {'real$':>6} {'G1':>6} {'G1 95% CI':>15} {'per-tx':>6} {'per-tx CI':>15} "
           f"{'full':>6} {'per-tx full':>11} {'prec full':>9}"]
    scopes = [("all", rows), ("contested (maker)", [r for r in rows if r["scope"] == "contested"]),
              ("uncontested", [r for r in rows if r["scope"] == "uncontested"]),
              ("contested, no biggest tx", None)]
    con = scopes[1][1]
    if con:
        big = max(con, key=lambda r: r["real_notional"])
        scopes[3] = ("contested, no biggest tx", [r for r in con if r is not big])
    for name, rs in scopes:
        if not rs:
            continue
        s = stats(rs)
        (g0, g1), (p0, p1) = bootstrap(rs, boot)
        out.append(f"{name:24} {s['txs']:5d} {s['real'] / total:6.3f} {s['g1']:6.3f} [{g0:.3f}, {g1:.3f}] "
                   f"{s['per_tx']:6.3f} [{p0:.3f}, {p1:.3f}] {s['full']:6.3f} {s['per_tx_full']:11.3f} "
                   f"{s['prec_full']:9.3f}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("variant_dir")
    ap.add_argument("chunks_dir")
    ap.add_argument("--boot", type=int, default=2000)
    a = ap.parse_args(argv)
    rows = [r for r in classify(load(Path(a.variant_dir)), Path(a.chunks_dir)) if r["scope"]]
    print(report(rows, a.boot))


if __name__ == "__main__":
    sys.exit(main())
