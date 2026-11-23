"""Why does class_sort reproduce real dev20 fills worse than proposer order? (Satya, Nov 23; D34 follow-up)

Method: replay each chunk on the baseline (proposer order, the trunk). At every scored block (a non-warm-up tx with
real fills in the baseline's txs.csv), fork the replay and re-run ONLY that block under:
  P  proposer (listing) order,
  C  class sort over the whole block (validate_replay.class_policy),
  K  class sort per consensus batch with batch cuts K (puzzles.py P5: within a batch class order, batches in
     listing order; a cut at k splits before tx_index k).
Every fork starts from the same book, so the differences are the block's ordering only (history excluded).

A tx "matches" when its fills equal the printed trades per (price, maker) (validate_replay.is_match, strict).
Per block, M_X = the scored txs ordering X matches.
  class contradicts printed:  M_P \\ M_C non-empty.
  a cut set fixes it:         the fewest cuts K (search capped: <= 3 cuts, <= 400 evaluations per block) with
                              M_K >= M_P | M_C (it reproduces every tx that either whole-block order reproduces).
Candidate cut positions (P5 logic, reduced): a cut changes the result only by putting a scored taker T back in
listing order relative to a later cancel / modify / ALO tx by one of T's makers (real, or engine under P or C) on
T's asset; cut positions that separate the same such pairs are equivalent, one is kept.

Item 2 (classification audit): for each tx in M_P \\ M_C, the txs class sort moved ahead of it (later-listed, lower
class, same asset, by one of its makers) are each tried alone: listing order with only X moved to just before T.
X is a culprit when that alone breaks T's match. Every involved tx is listed with its raw kind and its class.

usage: python -m analysis.order_audit <chunk-parent dir> --hl-data DIR --ref <baseline validate dir> --out DIR
"""
from __future__ import annotations

import argparse
import csv
import gzip
import itertools
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analysis import validate_replay as vr
from engine_py.replay import Observer
from engine_py.types import ASSETS, Cancel, CancelByCloid, Modify, OutsideFill, Place, Tif
from replay.adapter import BookHandle, EngineAdapter, options_for

MAX_CUTS, MAX_EVALS, MAX_MOVED = 3, 400, 40


def fork(rp):
    """Copy of the replay for one block. The window inputs and the read-only oid-estimator tables are shared;
    placed / placed_by are append-only maps of frozen actions, so a shallow copy is an independent copy."""
    import copy
    memo = {id(rp.blocks): rp.blocks, id(rp.snapshots): rp.snapshots, id(rp.observer): Observer(),
            id(rp.placed): dict(rp.placed), id(rp.placed_by): dict(rp.placed_by)}
    for name in ("_deep_trades", "cached_scoring_inputs"):
        v = getattr(rp, name, None)
        if v is not None:
            memo[id(v)] = v
    est = getattr(rp.resolver, "est", None) if getattr(rp, "resolver", None) is not None else None
    if est is not None:
        for v in vars(est).values():
            if isinstance(v, (dict, list, tuple)):
                memo[id(v)] = v
    return copy.deepcopy(rp, memo)


def run_block(rp, policy) -> dict[str, list]:
    f = fork(rp)
    f.policy = policy
    _, fills, _, _ = f.step_block()
    out = defaultdict(list)
    for x in fills:
        out[x.taker_hash].append((x.px, x.sz, x.maker_user))
    return out


def cut_policy(cuts):
    cuts = sorted(cuts)

    def pol(actions):
        cls = vr._tx_classes(actions)
        seg = lambda i: sum(i >= c for c in cuts)
        return sorted(actions, key=lambda a: (seg(a.tx_index), cls[a.tx_index], a.tx_index, a.item_index))
    return pol


def move_policy(x_idx, t_idx):
    """Listing order with tx x moved to just before tx t."""
    def pol(actions):
        return sorted(actions, key=lambda a: ((t_idx, -1) if a.tx_index == x_idx else (a.tx_index, 0), a.item_index))
    return pol


def matched(fills, real, hashes) -> frozenset:
    return frozenset(h for h in hashes if vr.is_match(real[h], fills.get(h, [])))


def asset_of_tx(actions) -> set[int]:
    return {(a.new if isinstance(a, Modify) else a).asset for a in actions}


def touchers(by_tx, asset, users) -> list[int]:
    """tx_index of txs on `asset` by one of `users` that cancel, modify or place an ALO (contested actors)."""
    out = []
    for i, acts in by_tx.items():
        if acts[0].user not in users:
            continue
        for a in acts:
            p = a.new if isinstance(a, Modify) else a
            if getattr(p, "asset", None) != asset:
                continue
            if isinstance(a, (Cancel, CancelByCloid, Modify)) or (isinstance(a, Place) and a.tif is Tif.ALO):
                out.append(i)
                break
    return out


def candidate_cuts(pairs) -> list[int]:
    """pairs: (taker idx t, later actor idx j). A cut k separates (t, j) iff t < k <= j. Keep one k per distinct
    set of separated pairs."""
    ks = sorted({t + 1 for t, _ in pairs} | {j for _, j in pairs})
    seen, out = set(), []
    for k in ks:
        sig = frozenset(p for p in pairs if p[0] < k <= p[1])
        if sig and sig not in seen:
            seen.add(sig)
            out.append(k)
    return out


def raw_info(raw_tx) -> dict:
    a = raw_tx["action"]
    k = a.get("type")
    info = {"kind": k, "tifs": [], "assets": [], "trigger": 0, "other_asset_taker": 0}
    if k == "order":
        items = a.get("orders", [])
    elif k == "modify":
        items = [a.get("order", {})]
    elif k == "batchModify":
        items = [m.get("order", {}) for m in a.get("modifies", [])]
    else:
        items = []
    for o in items:
        t = o.get("t", {})
        tif = t.get("limit", {}).get("tif") if "limit" in t else "trigger"
        info["tifs"].append(tif)
        info["assets"].append(o.get("a"))
        info["trigger"] += tif == "trigger"
        if o.get("a") not in ASSETS and tif in ("Gtc", "Ioc", "FrontendMarket"):
            info["other_asset_taker"] += 1
    if k in ("cancel", "cancelByCloid"):
        info["assets"] = [c.get("a", c.get("asset")) for c in a.get("cancels", [])]
    info["grouping"] = a.get("grouping")
    info["error"] = raw_tx.get("error")
    return info


def flags_of(info, klass) -> list[str]:
    """Things about a tx's class that the docs' rule may treat differently from validate_replay.tx_class."""
    f = []
    if info["kind"] in ("modify", "batchModify"):
        f.append(f"{info['kind']} (implicit cancel) -> class {klass}")
    if info["trigger"]:
        f.append("has trigger items (skipped by the decoder)")
    if info["other_asset_taker"]:
        f.append("GTC/IOC on another asset (ignored by tx_class)")
    if info["kind"] == "order" and len(set(info["tifs"])) > 1:
        f.append("mixed tifs " + "/".join(sorted(set(map(str, info["tifs"])))))
    if info["error"]:
        f.append("action error")
    return f


def audit_chunk(ad, chunk: Path, hl: Path, scored_by_block, out_blocks, out_flips):
    bk = ad.new_book(chunk, hl, options_for())
    rp = bk.replay
    real, _ = vr.scoring_inputs(rp, hl, False)
    real = {h: [(t.px, t.sz, t.maker) for t in trs] for h, trs in real.items()}
    cname = chunk.name
    while bk.next_height is not None:
        h = bk.next_height
        hashes = scored_by_block.get((cname, h))
        if hashes:
            audit_block(rp, chunk, h, sorted(hashes), real, out_blocks, out_flips)
        ad.apply_block(bk)


def audit_block(rp, chunk, h, hashes, real, out_blocks, out_flips):
    b = rp.blocks[rp._next_bi]
    by_tx = defaultdict(list)
    for a in b.actions:
        if not isinstance(a, OutsideFill):
            by_tx[a.tx_index].append(a)
    cls = vr._tx_classes([a for acts in by_tx.values() for a in acts])
    idx_of = {acts[0].hash: i for i, acts in by_tx.items()}
    raw = json.load(gzip.open(chunk / f"{h}.json.gz"))["blockDetails"]["txs"]
    fp = run_block(rp, vr.listing)
    fc = run_block(rp, vr.class_policy)
    mp, mc = matched(fp, real, hashes), matched(fc, real, hashes)
    row = dict(chunk=chunk.name, block=h, n_txs=len(raw), n_scored=len(hashes), matched_P=len(mp),
               matched_C=len(mc), p_only=len(mp - mc), c_only=len(mc - mp), cuts="", n_cuts="", batch_sizes="",
               evals=0, fix="")
    if mp - mc:
        pairs = set()
        for hsh in hashes:
            t = idx_of.get(hsh)
            if t is None:
                continue
            asset = next(iter(asset_of_tx(by_tx[t])))
            makers = {m for *_, m in real[hsh]} | {m for *_, m in fp.get(hsh, [])} | {m for *_, m in fc.get(hsh, [])}
            pairs |= {(t, j) for j in touchers(by_tx, asset, makers) if j > t and cls[j] < cls[t]}
        cands = candidate_cuts(pairs)
        target = mp | mc
        found, evals, best_p = None, 0, None
        for k in range(1, MAX_CUTS + 1):
            for cuts in itertools.combinations(cands, k):
                if evals >= MAX_EVALS:
                    break
                evals += 1
                mk = matched(run_block(rp, cut_policy(cuts)), real, hashes)
                if best_p is None and mk >= mp:
                    best_p = cuts
                if mk >= target:
                    found = cuts
                    break
            if found or evals >= MAX_EVALS:
                break
        bounds = [0, *sorted(found), len(raw)] if found else []
        row.update(cuts=" ".join(map(str, found)) if found else "", n_cuts=len(found) if found else "",
                   batch_sizes=" ".join(str(y - x) for x, y in zip(bounds, bounds[1:])), evals=evals,
                   n_candidates=len(cands),
                   fix="fixed" if found else ("cap" if evals >= MAX_EVALS else "none"),
                   fix_P_only=" ".join(map(str, best_p)) if best_p else "")
        for hsh in sorted(mp - mc):
            audit_flip(rp, chunk, h, hsh, by_tx, cls, idx_of, raw, real, fp, fc, out_flips)
    out_blocks.append(row)


def audit_flip(rp, chunk, h, hsh, by_tx, cls, idx_of, raw, real, fp, fc, out_flips):
    t = idx_of[hsh]
    asset = next(iter(asset_of_tx(by_tx[t])))
    real_m = {m for *_, m in real[hsh]}
    makers = real_m | {m for *_, m in fp.get(hsh, [])} | {m for *_, m in fc.get(hsh, [])}
    moved = [j for j in touchers(by_tx, asset, makers) if j > t and cls[j] < cls[t]]
    tinfo = raw_info(raw[t])
    base = dict(chunk=chunk.name, block=h, taker_hash=hsh, taker_idx=t, taker_kind=tinfo["kind"],
                taker_tifs="/".join(map(str, tinfo["tifs"])), taker_class=cls[t],
                taker_flags="; ".join(flags_of(tinfo, cls[t])), n_moved=len(moved))
    if not moved:
        out_flips.append(dict(base, role="no moved maker tx (other reordering)"))
        return
    for j in moved[:MAX_MOVED]:
        info = raw_info(raw[j])
        breaks = not vr.is_match(real[hsh], run_block(rp, move_policy(j, t)).get(hsh, []))
        out_flips.append(dict(base, role="culprit" if breaks else "moved, harmless", x_idx=j, x_kind=info["kind"],
                              x_tifs="/".join(map(str, info["tifs"])), x_class=cls[j],
                              x_is_real_maker=int(by_tx[j][0].user in real_m),
                              x_flags="; ".join(flags_of(info, cls[j]))))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("chunks_dir")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--ref", required=True, help="baseline validate_chunks dir (txs.csv: which txs are scored)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--backend", default="rust", choices=("rust", "py"))
    ap.add_argument("--limit", type=int, default=None, help="first N chunks (smoke runs)")
    a = ap.parse_args(argv)
    scored = defaultdict(set)
    with open(Path(a.ref) / "txs.csv") as f:
        for r in csv.DictReader(f):
            if r["warmup"] == "0" and r["status"] in ("match", "mismatch") and r["real"]:
                scored[(r["chunk"], int(r["block"]))].add(r["hash"])
    chunks = sorted(p for p in Path(a.chunks_dir).iterdir() if p.is_dir())[:a.limit]
    ad = EngineAdapter(a.backend)
    blocks, flips = [], []
    for c in chunks:
        audit_chunk(ad, c, Path(a.hl_data), scored, blocks, flips)
        print(f"{c.name}: {sum(1 for r in blocks if r['chunk'] == c.name)} scored blocks", file=sys.stderr, flush=True)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("blocks.csv", blocks), ("flips.csv", flips)):
        keys = list(dict.fromkeys(k for r in rows for k in r))
        with open(out / name, "w", newline="") as f:
            w = csv.DictWriter(f, keys)
            w.writeheader()
            w.writerows(rows)
    summarize(blocks, flips)


def summarize(blocks, flips):
    n = len(blocks)
    contra = [r for r in blocks if r["p_only"]]
    rev = [r for r in blocks if r["c_only"]]
    print(f"scored blocks {n}; class contradicts printed (P matches a tx C does not): {len(contra)}; "
          f"class wins somewhere: {len(rev)}")
    print("fix:", Counter(r["fix"] for r in contra), " n_cuts:", Counter(r["n_cuts"] for r in contra if r["fix"] == "fixed"))
    sizes = sorted(int(s) for r in contra if r["fix"] == "fixed" for s in r["batch_sizes"].split())
    if sizes:
        print(f"batch sizes (txs) in fixed blocks: median {sizes[len(sizes) // 2]}, n {len(sizes)}, "
              f"min {sizes[0]}, max {sizes[-1]}")
    print("flip roles:", Counter(r["role"] for r in flips))
    cul = [r for r in flips if r["role"] == "culprit"]
    print("culprit kinds:", Counter((r["x_kind"], r["x_tifs"], r["x_class"]) for r in cul).most_common(10))
    print("taker kinds:", Counter((r["taker_kind"], r["taker_tifs"], r["taker_class"]) for r in flips).most_common(6))


if __name__ == "__main__":
    main()
