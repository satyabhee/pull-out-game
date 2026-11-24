"""oracle_coverage: share of pooled contested misses (and engine-only txs) each oracle's truth file can touch.

Definition (written before any oracle score, handoff/ORACLE.md "Plan"):
A contested miss (per-tx maker-strict < 1 in the baseline run, contested scope as analysis.contested_paired) is
TOUCHED by
  O1 (order status)  if o1 has an in-window placement at or before the tx's block by one of the tx's real or engine
                     makers (8-hex prefix as in txs.csv) on the tx's coin, maker side and that maker's fill price;
  O2 (pre-window)    if o2 has an order alive at the chunk's first block by such a maker at such a level;
  O3 (cancel target) as O1 but from o3 (placements whose chain oid is known);
  O4 (IOC status)    if o4 has the taker's own IOC (same block, same tx index);
  any                if any of the above.
Engine-only txs (real notional 0, engine > 0) are counted for O4 (their taker IOC) and O1/O2 (engine makers).
usage: python -m analysis.oracle_coverage TRUTH_ROOT SET=BASE_RUN_DIR [...] --chunks-root C
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path

from analysis.contested import load
from analysis.contested_paired import per_tx, scope_of
from analysis.oracle_truth import norm


def fills(s):
    """[(maker8 or 'placeholder', px str)] from a txs.csv real/engine cell like '0.5@1725.5 0xe92cdf7b; ...'."""
    out = []
    for p in (s or "").split("; "):
        p = p.strip()
        if not p or "@" not in p:
            continue
        szpx, _, who = p.partition(" ")
        out.append((who.strip(), norm(szpx.split("@")[1])))
    return out


def taker_tx(chunk_dir, height, hsh, cache):
    key = (str(chunk_dir), height)
    if key not in cache:
        with gzip.open(Path(chunk_dir) / f"{height}.json.gz", "rt") as fh:
            b = json.load(fh)
        b = b.get("blockDetails", b)
        cache.clear()
        cache[key] = {tx["hash"]: (t, tx) for t, tx in enumerate(b["txs"])}
    return cache[key].get(hsh)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("truth_root")
    ap.add_argument("pairs", nargs="+", help="SET=BASE_RUN_DIR")
    ap.add_argument("--chunks-root", required=True)
    ap.add_argument("--out", default=None, help="per-miss CSV")
    a = ap.parse_args(argv)
    tot = Counter()
    per_set = {}
    rows_out = []
    seen_chunks = set()                      # a chunk in two sets is counted once (first set listed wins)
    for pair in a.pairs:
        name, base = pair.split("=")
        T = Path(a.truth_root) / name
        lvl1, lvl3, lvl2, ioc = defaultdict(list), defaultdict(list), set(), set()
        for r in map(json.loads, open(T / "o1_order_status.jsonl")):
            lvl1[(r["chunk"], r["user"][:10], r["coin"], r["side"], r["px"])].append(r["height"])
        for r in map(json.loads, open(T / "o1_order_status.jsonl")):
            pass
        o3 = {(r["chunk"], r["height"], r["tx"], r["item"]) for r in map(json.loads, open(T / "o3_oids.jsonl"))}
        for r in map(json.loads, open(T / "o1_order_status.jsonl")):
            if (r["chunk"], r["height"], r["tx"], r["item"]) in o3:
                lvl3[(r["chunk"], r["user"][:10], r["coin"], r["side"], r["px"])].append(r["height"])
        for r in map(json.loads, open(T / "o2_prewindow.jsonl")):
            lvl2.add((r["chunk"], r["user"][:10], r["coin"], r["side"], r["px"]))
        for r in map(json.loads, open(T / "o4_ioc_status.jsonl")):
            ioc.add((r["chunk"], r["height"], r["tx"]))
        rows = load(Path(base))
        chunks = Path(a.chunks_root) / name
        sc = scope_of(rows, chunks, False)
        v = per_tx(rows, "matched_notional_full")
        raw = {(r["chunk"], r["hash"]): r for r in csv.DictReader(open(Path(base) / "txs.csv"))}
        c = Counter()
        cache = {}
        dup = {ch for ch, _ in raw} & seen_chunks
        for (ch, h), r in raw.items():
            if r["warmup"] != "0" or r["status"] not in ("match", "mismatch") or ch in dup:
                continue
            contested_miss = sc.get((ch, h)) and (ch, h) in v and v[(ch, h)] < 0.999
            engine_only = float(r["real_notional"] or 0) == 0 and float(r["engine_notional"] or 0) > 0
            if not (contested_miss or engine_only):
                continue
            kind = "miss" if contested_miss else "eonly"
            height = int(r["block"])
            tt = taker_tx(chunks / ch, height, h, cache)
            side = None
            t_idx = None
            if tt:
                t_idx, tx = tt
                o = ((tx.get("action") or {}).get("orders") or [{}])[0]
                side = "A" if o.get("b") else "B"          # maker side = opposite of the taker
            mk = [(w, px) for w, px in fills(r["real"]) + fills(r["engine"]) if w.startswith("0x")]
            hit = {
                "O1": any(any(hh <= height for hh in lvl1.get((ch, w, r["coin"], side, px), [])) for w, px in mk),
                "O2": any((ch, w, r["coin"], side, px) in lvl2 for w, px in mk),
                "O3": any(any(hh <= height for hh in lvl3.get((ch, w, r["coin"], side, px), [])) for w, px in mk),
                "O4": t_idx is not None and (ch, height, t_idx) in ioc,
            }
            hit["any"] = any(hit.values())
            c[f"{kind}_n"] += 1
            for k, x in hit.items():
                c[f"{kind}_{k}"] += x
            if contested_miss:
                c["miss_units"] += 1 - v[(ch, h)]
                for k, x in hit.items():
                    c[f"miss_units_{k}"] += (1 - v[(ch, h)]) * x
            rows_out.append({"set": name, "chunk": ch, "hash": h, "kind": kind, "category": r["category"],
                             "lost": round(1 - v.get((ch, h), 0), 4) if contested_miss else "",
                             **{k: int(x) for k, x in hit.items()}})
        c["contested_n"] = sum(1 for (ch, _), x in sc.items() if x and ch not in dup)
        c["dup_chunks"] = len(dup)
        seen_chunks |= {ch for ch, _ in raw}
        per_set[name] = c
        tot.update(c)
    for name, c in list(per_set.items()) + [("POOLED", tot)]:
        n, e = c["miss_n"], c["eonly_n"]
        line = " ".join(f"{k} {c[f'miss_{k}']}/{n} ({c[f'miss_{k}'] / max(1, n):.0%}, units "
                        f"{100 * c[f'miss_units_{k}'] / max(1, c['contested_n']):.1f} pt)" for k in ("O1", "O2", "O3", "O4", "any"))
        print(f"{name:16} contested {c['contested_n']} misses {n} (lost {100 * c['miss_units'] / max(1, c['contested_n']):.1f} pt): {line}")
        print(f"{'':16} engine-only {e}: " + " ".join(f"{k} {c[f'eonly_{k}']} ({c[f'eonly_{k}'] / max(1, e):.0%})"
                                                     for k in ("O1", "O2", "O4", "any")))
    if a.out:
        with open(a.out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows_out[0]))
            w.writeheader()
            w.writerows(rows_out)


if __name__ == "__main__":
    main()
