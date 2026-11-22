"""M1 observation mask: write a simulated chunk exactly as the real replay sees one (MAHITO_PLAN M1 mask.py).

Output, in the real on-disk formats so data/parse.py and engine_py/replay.py read it unchanged:
- <root>/windows/<first>-<last>/<height>.json.gz  explorer blocks: consensus batches MERGED into one list in proposer
  order (cfg.merge_batches=False keeps them as runs; they look the same: explorer blocks never mark boundaries);
  placement oids, rejections and silent cancels are invisible; action-level errors only;
- window.json (kind chunk, heights, lead-in, warm-up end, block times, sim seed);
- anchors.json: `anchors_per_chunk` placements with their oid ({"oid", "height", "tx", "item"});
- clearinghouseState.json: `account_snapshots` accounts, `account_snapshot_after_ms` after the start;
- <root>/hl_data/<day>/<hour>/: trades_<COIN> (public fills with maker addresses), bbo_<COIN> (every block),
  book_<COIN> (full l2Book, 20 levels with n, every ~12.4 s, plus one just before the first block),
  book_<COIN>_sf4 (all levels, same times), book_<COIN>_fast (5 levels every ~0.54 s), blocks (headers).
Everything else (owners of resting orders, queue order, oids, rejections, batches, burned oids) stays in truth.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path

from engine_py.types import ASSETS

COIN = {a: s.name for a, s in ASSETS.items()}
PROPOSER = "0x" + "5a" * 20


def tx_hash(seed: int, height: int, i: int) -> str:
    return "0x" + hashlib.sha256(f"tx:{seed}:{height}:{i}".encode()).hexdigest()


def block_hash(seed: int, height: int) -> str:
    return "0x" + hashlib.sha256(f"blk:{seed}:{height}".encode()).hexdigest()


def action_json(tx) -> dict:
    if tx.kind == "order":
        out = {"type": "order",
               "orders": [dict({"a": it.asset, "b": it.side == "B", "p": ASSETS[it.asset].ticks_to_px(it.px),
                                "s": ASSETS[it.asset].lots_to_sz(it.sz), "r": it.reduce_only,
                                "t": {"limit": {"tif": it.tif}}}, **({"c": it.cloid} if it.cloid else {}))
                          for it in tx.items],
               "grouping": {"p": tx.p} if tx.p else "na"}
        return out
    if tx.kind == "cancel":
        return {"type": "cancel", "cancels": [{"a": it.asset, "o": it.oid} for it in tx.items]}
    return {"type": "cancelByCloid", "cancels": [{"asset": it.asset, "cloid": it.cloid} for it in tx.items]}


def level_json(asset: int, lv) -> dict:
    px, sz, n = lv
    return {"px": ASSETS[asset].ticks_to_px(px), "sz": ASSETS[asset].lots_to_sz(sz), "n": n}


class Recorder:
    """Collects recorder lines per (hour, stem) and writes them sorted by receive time."""

    def __init__(self, root: Path, lag_ms: int):
        self.root, self.lag = Path(root), lag_ms
        self.lines: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)

    def add(self, stem: str, t_ms: int, msg: dict) -> None:
        recv = t_ms + self.lag
        hour = time.strftime("%Y-%m-%d/%H", time.gmtime(recv / 1000))
        self.lines[(hour, stem)].append((recv, json.dumps({"recv_ms": recv, "msg": msg}, separators=(", ", ": "))))

    def write(self) -> list[Path]:
        out = []
        for (hour, stem), rows in self.lines.items():
            d = self.root / "hl_data" / hour
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{stem}.jsonl"
            with open(p, "a") as f:
                for _, line in sorted(rows, key=lambda r: r[0]):
                    f.write(line + "\n")
            out.append(p)
        return out

    # ---- streams
    def book(self, chain, t_ms: int, kind: str = "full") -> None:
        for a in ASSETS:
            depth = None if kind == "sf4" else (5 if kind == "fast" else 20)
            levels = [[level_json(a, lv) for lv in chain.levels(a, side, depth)] for side in ("B", "A")]
            stem = f"book_{COIN[a]}" + {"full": "", "sf4": "_sf4", "fast": "_fast"}[kind]
            self.add(stem, t_ms, {"channel": "l2Book", "data": {"coin": COIN[a], "time": t_ms, "levels": levels}})

    def bbo(self, chain, t_ms: int) -> None:
        for a in ASSETS:
            b = [chain.levels(a, side, 1) for side in ("B", "A")]
            self.add(f"bbo_{COIN[a]}", t_ms, {"channel": "bbo", "data": {
                "coin": COIN[a], "time": t_ms, "bbo": [level_json(a, x[0]) if x else None for x in b]}})

    def trades(self, trades, t_ms: int) -> None:
        by = defaultdict(list)
        for tr in trades:                                   # one message per (coin, tx), fills in order
            by[(tr.asset, tr.hash)].append(tr)
        for (a, _), trs in by.items():
            self.add(f"trades_{COIN[a]}", t_ms, {"channel": "trades", "data": [
                {"coin": COIN[a], "side": tr.taker_side, "px": ASSETS[a].ticks_to_px(tr.px),
                 "sz": ASSETS[a].lots_to_sz(tr.sz), "time": tr.time_ms, "hash": tr.hash, "tid": tr.tid,
                 "users": [tr.buyer, tr.seller]} for tr in trs]})


def write_block(window: Path, height: int, time_ms: int, seed: int, listing: list) -> None:
    txs = [{"time": time_ms, "user": tx.user, "action": action_json(tx), "block": height, "hash": tx.hash,
            "error": tx.error} for tx in listing]
    body = {"blockDetails": {"height": height, "blockTime": time_ms, "hash": block_hash(seed, height),
                             "proposer": PROPOSER, "numTxs": len(txs), "txs": txs}}
    data = json.dumps(body, separators=(",", ":")).encode()
    with open(window / f"{height}.json.gz", "wb") as raw:              # mtime=0: same seed -> same bytes
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as f:
            f.write(data)


def write_accounts(window: Path, accounts: dict, users: list[str], chain) -> None:
    rows = []
    for u in users:
        a = accounts[u]
        pos = [{"position": {"coin": COIN[asset], "szi": ASSETS[asset].lots_to_sz(sz)}}
               for asset, sz in sorted(a.position.items()) if sz]
        rows.append({"user": u, "kind": "clearinghouseState",
                     "answer": {"marginSummary": {"accountValue": str(a.free_margin), "totalMarginUsed": "0"},
                                "assetPositions": pos}})
    (window / "clearinghouseState.json").write_text(json.dumps(rows, indent=1))
