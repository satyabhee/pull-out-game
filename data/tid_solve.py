"""I27 core: pin real fills to their exact (taker order, maker placement) from the trade id
(data/tid_hash.py). See analysis/tid_solve.py for the measurement CLI and the method."""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from data.oids import OidEstimator, _order_items
from data.parse import iter_trades
from engine_py.types import ASSETS, Side

_M64 = np.uint64(0xFFFFFFFFFFFFFFFF)


def _rotl(x, b):
    return (x << np.uint64(b)) | (x >> np.uint64(64 - b))


def tid_np(buyer: np.ndarray, seller: np.ndarray) -> np.ndarray:
    """Vectorised data.tid_hash.tid for uint64 arrays."""
    with np.errstate(over="ignore"):
        v0 = np.full(buyer.shape, 0x736F6D6570736575, np.uint64)
        v1 = np.full(buyer.shape, 0x646F72616E646F6D, np.uint64)
        v2 = np.full(buyer.shape, 0x6C7967656E657261, np.uint64)
        v3 = np.full(buyer.shape, 0x7465646279746573, np.uint64)

        def rnd(v0, v1, v2, v3):
            v0 = v0 + v1; v1 = _rotl(v1, 13) ^ v0; v0 = _rotl(v0, 32)
            v2 = v2 + v3; v3 = _rotl(v3, 16) ^ v2
            v0 = v0 + v3; v3 = _rotl(v3, 21) ^ v0
            v2 = v2 + v1; v1 = _rotl(v1, 17) ^ v2; v2 = _rotl(v2, 32)
            return v0, v1, v2, v3

        for m in (buyer.astype(np.uint64), seller.astype(np.uint64)):
            v3 = v3 ^ m
            v0, v1, v2, v3 = rnd(v0, v1, v2, v3)
            v0 = v0 ^ m
        b = np.uint64(16 << 56)
        v3 = v3 ^ b
        v0, v1, v2, v3 = rnd(v0, v1, v2, v3)
        v0 = v0 ^ b
        v2 = v2 ^ np.uint64(0xFF)
        for _ in range(3):
            v0, v1, v2, v3 = rnd(v0, v1, v2, v3)
        return (v0 ^ v1 ^ v2 ^ v3) >> np.uint64(14)


def bracket(est: OidEstimator, k: int | None, max_width: int):
    if k is None:
        return None
    lo, hi = est.c_bounds(k)
    if lo is None and hi is None:
        return None
    if lo is None:
        lo = hi - max_width
    if hi is None:
        hi = lo + max_width
    if hi - lo > max_width:
        return None
    a, b = max(1, k + lo), k + hi                # oids are positive
    return (a, b) if a <= b else None


def solve_chunk(ch: Path, hl: Path, max_width: int, stats: Counter, est: OidEstimator | None = None) -> dict:
    """{(taker tx hash, tid): ((height, tx, item) of the maker placement, taker oid, maker oid)} for uniquely solved
    trades; stats counts outcomes."""
    if est is None:
        anchors = ch / "anchors.json"
        est = OidEstimator.from_files(ch, anchors if anchors.exists() else None, facts=True, hl_data=hl)
    out: dict = {}
    meta = json.loads((ch / "window.json").read_text())
    warm = meta.get("warmup_end_height", 0)
    files = sorted(ch.glob("*.json.gz"), key=lambda p: int(p.name.split(".")[0]))
    taker_items: dict[str, list] = defaultdict(list)          # tx hash -> [(asset, buy, k, height)]
    maker_items: dict[tuple, list] = defaultdict(list)        # (user, asset, buy, px) -> [(height, t, k)]
    tx_pos: dict[str, tuple[int, int]] = {}
    t0 = t1 = None
    for f in files:
        b = json.load(gzip.open(f))["blockDetails"]
        h = b["height"]
        t0 = b["blockTime"] if t0 is None else t0
        t1 = b["blockTime"]
        for t, tx in enumerate(b["txs"]):
            if tx.get("error") is not None:
                continue
            tx_pos[tx["hash"]] = (h, t)
            for i, it in enumerate(_order_items(tx["action"])):
                a = it.get("a")
                if a not in ASSETS:
                    continue
                try:
                    px = ASSETS[a].px_to_ticks(it["p"])
                except Exception:
                    continue
                k = est.counts.get((h, t, i))
                taker_items[tx["hash"]].append((a, bool(it["b"]), k, h))
                maker_items[(tx["user"].lower(), a, bool(it["b"]), px)].append((h, t, k, i))
    by_tx: dict[str, list] = defaultdict(list)
    for tr in iter_trades(hl, ["BTC", "ETH"], t0, t1):
        if tr.hash in tx_pos and tx_pos[tr.hash][0] >= warm:
            by_tx[tr.hash].append(tr)
    for h, trs in by_tx.items():
        trs.sort(key=lambda x: x.seq)
        th, tt = tx_pos[h]
        taker_oid = None
        for tr in trs:
            stats["trades"] += 1
            buy = tr.taker_side is Side.BUY
            cands_t = [bracket(est, k, max_width) for a, b_, k, _ in taker_items[h] if a == tr.asset and b_ == buy]
            cands_t = [c for c in cands_t if c]
            if taker_oid is not None:
                cands_t = [(taker_oid, taker_oid)]
            if not cands_t:
                stats["no_taker_bracket"] += 1
                continue
            mk = [(hh, t, k, i) for hh, t, k, i in maker_items.get((tr.maker.lower(), tr.asset, not buy, tr.px), ())
                  if (hh, t) < (th, tt)]
            mb = [(bracket(est, k, max_width), (hh, t, i)) for hh, t, k, i in mk]
            mb = [(c, p) for c, p in mb if c]
            if not mb:
                stats["no_maker_candidate (pre-window or unbracketed)"] += 1
                continue
            T = np.concatenate([np.arange(lo, hi + 1, dtype=np.uint64) for lo, hi in cands_t])
            sols = []
            for (lo, hi), pos in mb:
                Mo = np.arange(lo, hi + 1, dtype=np.uint64)
                if T.size * Mo.size > 20_000_000:
                    stats["search_too_big"] += 1
                    continue
                tt_, mm = np.repeat(T, Mo.size), np.tile(Mo, T.size)
                got = tid_np(tt_, mm) if buy else tid_np(mm, tt_)
                hit = np.nonzero(got == np.uint64(tr.tid))[0]
                sols += [(int(tt_[j]), int(mm[j]), pos) for j in hit]
            if len(sols) == 1:
                stats["solved"] += 1
                taker_oid = sols[0][0]
                out[(h, tr.tid)] = (sols[0][2], sols[0][0], sols[0][1])
                stats["solved_maker_is_latest_candidate"] += sols[0][2] == max(p for _, p in mb)
                stats["solved_maker_candidates_gt1"] += len(mb) > 1
            elif not sols:
                stats["no_solution"] += 1
            else:
                stats["ambiguous"] += 1
    return out
