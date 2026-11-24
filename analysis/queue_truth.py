"""Chain queue order from trade ids (measurement only; tests the --p-priority docs rule against the chain).

When one taker tx fills two tid-pinned maker orders A then B at the same price (recorder seq = real fill order), the
chain's queue had A ahead of B at that moment. Each such consecutive pair is compared with what the docs rule
(engine_py/matching.py "docs": a new ALO goes ahead of each in-window ALO placed < 400 ms earlier, block time, with a
lower grouping p) predicts from the two placements (block time, listing index, p, tif):
  fifo       : A placed first (earlier block, or same block and earlier listing), docs predicts no jump  -> consistent
  jump_ok    : B placed first, A placed later with higher p within 400 ms (docs predicts the jump)       -> consistent
  jump_other : B placed first, A placed later but docs does NOT predict it (lower / equal p, or older than 400 ms)
  nojump     : A placed first, B later with higher p within 400 ms, both ALO (docs predicts B ahead) but chain had A
Split by same block vs different blocks.

usage: python -m analysis.queue_truth <chunk-parent> --only CHUNK --hl-data DIR --out F.json
       python -m analysis.queue_truth --summarize F1.json ...
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from data.parse import iter_trades
from data.tid_solve import solve_chunk

DOCS_T_MS = 400


def _items(action: dict):
    t = action.get("type")
    if t == "order":
        return action.get("orders", []), action.get("grouping")
    if t == "batchModify":
        return [m["order"] for m in action.get("modifies", [])], action.get("grouping")
    if t == "modify":
        return [action["order"]], action.get("grouping")
    return [], None


def run_one(chunks: Path, chunk: str, hl: Path) -> dict:
    ch = chunks / chunk
    stats: Counter = Counter()
    sol = solve_chunk(ch, hl, 4000, stats)
    blocks: dict[int, dict] = {}

    def place(pos):
        h, t, i = pos
        if h not in blocks:
            b = json.load(gzip.open(ch / f"{h}.json.gz"))["blockDetails"]
            blocks[h] = b
        b = blocks[h]
        tx = b["txs"][t]
        its, g = _items(tx["action"])
        it = its[i]
        p = (g.get("p") if isinstance(g, dict) else 0) or 0
        tif = it.get("t", {}).get("limit", {}).get("tif")
        return {"h": h, "t": t, "i": i, "time": b["blockTime"], "p": p, "tif": tif}

    meta = json.loads((ch / "window.json").read_text())
    files = sorted(int(p.name.split(".")[0]) for p in ch.glob("*.json.gz"))
    t0 = json.load(gzip.open(ch / f"{files[0]}.json.gz"))["blockDetails"]["blockTime"]
    t1 = json.load(gzip.open(ch / f"{files[-1]}.json.gz"))["blockDetails"]["blockTime"]
    by_tx: dict[str, list] = defaultdict(list)
    for tr in iter_trades(hl, ["BTC", "ETH"], t0, t1):
        if (tr.hash, tr.tid) in sol:
            by_tx[tr.hash].append(tr)
    pairs = []
    for h, trs in by_tx.items():
        trs.sort(key=lambda x: x.seq)
        for a, b in zip(trs, trs[1:]):
            if a.px != b.px:
                continue
            pa, pb = sol[(a.hash, a.tid)][0], sol[(b.hash, b.tid)][0]
            if pa == pb:
                continue                                   # same order filled twice in one tx
            A, B = place(pa), place(pb)
            a_first = (A["h"], A["t"], A["i"]) < (B["h"], B["t"], B["i"])
            same = A["h"] == B["h"]
            alo = A["tif"] == "Alo" and B["tif"] == "Alo"
            if a_first:
                gap = B["time"] - A["time"]
                cat = "nojump" if (alo and B["p"] > A["p"] and gap < DOCS_T_MS) else "fifo"
            else:
                gap = A["time"] - B["time"]
                cat = "jump_ok" if (alo and A["p"] > B["p"] and gap < DOCS_T_MS) else "jump_other"
            pairs.append({"cat": cat, "same_block": same, "gap_ms": gap, "pA": A["p"], "pB": B["p"],
                          "tifA": A["tif"], "tifB": B["tif"], "A": [A["h"], A["t"], A["i"]], "B": [B["h"], B["t"], B["i"]],
                          "taker": h})
    return {"chunk": chunk, "warmup_end_height": meta.get("warmup_end_height"), "solve": dict(stats), "pairs": pairs}


def summarize(files):
    c = Counter()
    for f in files:
        for p in json.load(open(f))["pairs"]:
            c[(p["cat"], "same block" if p["same_block"] else "cross block")] += 1
    return {f"{a} | {b}": n for (a, b), n in sorted(c.items())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks", nargs="?")
    ap.add_argument("--only")
    ap.add_argument("--hl-data")
    ap.add_argument("--out")
    ap.add_argument("--summarize", nargs="+")
    a = ap.parse_args(argv)
    if a.summarize:
        print(json.dumps(summarize(a.summarize), indent=1))
        return 0
    res = run_one(Path(a.chunks), a.only, Path(a.hl_data))
    Path(a.out).write_text(json.dumps(res))
    print(f"{a.only}: {len(res['pairs'])} pairs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
