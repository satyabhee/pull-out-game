"""Window blocks and recorder files -> typed, integer-only engine inputs.

Implements the decoding rules in AGENT_PLAN.md section 3 / specs/00-data-contract.md.

    iter_window(window_dir, hl_data=None, stats=None) -> Iterator[Block]
    iter_trades(hl_data, coins, t0_ms, t1_ms)         -> Iterator[Trade]
    nearest_book(hl_data, coin, at_ms)                -> BookSnapshot

Run `python -m data.parse <window_dir> [--hl-data DIR]` for a summary.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from engine_py.types import (
    ASSETS, Block, Cancel, CancelByCloid, ConversionError, Modify, OutsideFill,
    Place, ScheduleCancel, Side, TIF_FROM_WIRE, ZERO_HASH,
)

COIN_TO_ASSET = {spec.name: a for a, spec in ASSETS.items()}

# Action types with no effect on the BTC/ETH book: dropped and counted.
BOOK_TYPES = {"order", "cancel", "cancelByCloid", "modify", "batchModify", "scheduleCancel"}


class Stats(Counter):
    """Counts of everything decoded, skipped or rejected (keys are strings)."""


# ------------------------------------------------------------------ blocks

def _block_files(window_dir: Path) -> list[tuple[int, Path]]:
    out = []
    for name in os.listdir(window_dir):
        if name.endswith(".json.gz") and name[: -len(".json.gz")].isdigit():
            out.append((int(name[: -len(".json.gz")]), window_dir / name))
    out.sort()  # numeric, never text order
    return out


def read_block(path: Path) -> dict:
    with gzip.open(path, "rt") as f:
        raw = json.load(f)
    b = raw.get("blockDetails", raw)
    if "txs" not in b or "numTxs" not in b:
        raise ValueError(f"{path}: not a block (error response saved as a file?)")
    if len(b["txs"]) != b["numTxs"]:
        raise ValueError(f"{path}: incomplete block, {len(b['txs'])} of {b['numTxs']} txs")
    return b


def _priority(action: dict) -> int | None:
    g = action.get("grouping")
    if isinstance(g, dict) and "p" in g:
        return int(g["p"])
    return None


def _place(item: dict, meta: dict, stats: Stats) -> Place | None:
    """One order item -> Place, or None (other asset, trigger, invalid)."""
    asset = item["a"]
    if asset not in ASSETS:
        return None
    t = item["t"]
    if "trigger" in t:
        stats["skip_trigger"] += 1
        return None
    tif_wire = t["limit"]["tif"]
    if tif_wire not in TIF_FROM_WIRE:
        raise ValueError(f"unknown tif {tif_wire!r}")
    spec = ASSETS[asset]
    try:
        return Place(**meta, asset=asset,
                     side=Side.BUY if item["b"] else Side.SELL,
                     px=spec.px_to_ticks(item["p"]), sz=spec.sz_to_lots(item["s"]),
                     tif=TIF_FROM_WIRE[tif_wire], cloid=item.get("c"),
                     reduce_only=bool(item["r"]))
    except ConversionError:
        stats["skip_invalid_precision"] += 1
        return None
    except ValueError:  # px or sz of zero (real: size-0 IOCs, a price-0 IOC)
        stats["skip_zero_px_or_sz"] += 1
        return None


def decode_tx(tx: dict, tx_index: int, stats: Stats) -> list:
    """Decode one tx into BTC/ETH actions (possibly none)."""
    action = tx["action"]
    kind = action.get("type")
    if kind not in BOOK_TYPES:
        stats[f"ignored:{kind}"] += 1
        return []
    if tx["error"] is not None:
        stats[f"errored:{kind}"] += 1
        return []
    base = dict(user=tx["user"].lower(), block=tx["block"], tx_index=tx_index,
                hash=tx["hash"], priority=_priority(action))
    out: list = []

    if kind == "order":
        for i, item in enumerate(action["orders"]):
            p = _place(item, {**base, "item_index": i}, stats)
            if p is not None:
                out.append(p)
    elif kind == "cancel":
        for i, c in enumerate(action["cancels"]):
            if c["a"] in ASSETS:
                out.append(Cancel(**base, item_index=i, asset=c["a"], oid=int(c["o"])))
    elif kind == "cancelByCloid":
        for i, c in enumerate(action["cancels"]):
            if c["asset"] in ASSETS:
                out.append(CancelByCloid(**base, item_index=i, asset=c["asset"], cloid=c["cloid"]))
    elif kind in ("modify", "batchModify"):
        mods = [action] if kind == "modify" else action["modifies"]
        for i, m in enumerate(mods):
            meta = {**base, "item_index": i}
            new = _place(m["order"], meta, stats)
            if new is None:
                continue
            target = m["oid"] if isinstance(m["oid"], str) else int(m["oid"])
            out.append(Modify(**meta, asset=new.asset, target=target, new=new))
    elif kind == "scheduleCancel":
        t = action.get("time")
        out.append(ScheduleCancel(**base, item_index=0, at_ms=None if t is None else int(t)))

    for a in out:
        stats[f"action:{type(a).__name__}"] += 1
    return out


def iter_window(window_dir, hl_data=None, stats: Stats | None = None,
                outside_other_txs: bool = False, other_at_tx_index: bool = False) -> Iterator[Block]:
    """Yield the window's blocks in strictly increasing height order.

    With `hl_data`, zero-hash BTC/ETH trades are appended to the block whose
    time equals the trade time, after all real txs, ordered by tid.
    outside_other_txs=True (G1 triage measurement flag, default off): trades whose hash is a tx
    in this block that decodes to no BTC/ETH book action (e.g. SetGlobalAction, the oracle
    update that fires triggers/liquidations) are also outside fills, in the same end-of-block
    tid order. Counted as stats["outside_fill_other_tx"].
    other_at_tx_index=True (measurement, needs outside_other_txs): those fills carry their own tx's
    index as tx_index (item_index = tid order within the tx) instead of len(txs), so the replay can
    apply them at the tx's position (engine_py/replay.Replay other_position="index"). They still
    come after the book actions in Block.actions; zero-hash trades are unchanged.
    """
    if other_at_tx_index and not outside_other_txs:
        raise ValueError("other_at_tx_index needs outside_other_txs")
    window_dir = Path(window_dir)
    stats = stats if stats is not None else Stats()
    files = _block_files(window_dir)
    if not files:
        raise ValueError(f"no blocks in {window_dir}")

    outside: dict[int, list] = defaultdict(list)
    by_hash: dict[str, list] = defaultdict(list)
    if hl_data is not None:
        t0 = read_block(files[0][1])["blockTime"]
        t1 = read_block(files[-1][1])["blockTime"]
        for tr in iter_trades(hl_data, ("BTC", "ETH"), t0, t1):
            if tr.hash == ZERO_HASH:
                outside[tr.time_ms].append(tr)
            elif outside_other_txs:
                by_hash[tr.hash].append(tr)

    prev = None
    for height, path in files:
        b = read_block(path)
        if b["height"] != height:
            raise ValueError(f"{path}: file name says {height}, block says {b['height']}")
        if prev is not None and height <= prev:
            raise AssertionError(f"heights not strictly increasing: {prev} then {height}")
        prev = height

        actions: list = []
        other: list = []
        at_index: list = []
        for i, tx in enumerate(b["txs"]):
            decoded = decode_tx(tx, i, stats)
            actions.extend(decoded)
            if not decoded and tx["hash"] in by_hash:
                stats["outside_fill_other_tx"] += 1
                if other_at_tx_index:
                    for k, tr in enumerate(sorted(by_hash.pop(tx["hash"]), key=lambda t: t.tid)):
                        at_index.append(tr.as_outside_fill(height, i, k))
                else:
                    other += by_hash.pop(tx["hash"])

        trades = sorted(outside.pop(b["blockTime"], []) + other, key=lambda t: t.tid)
        for k, tr in enumerate(trades):
            actions.append(tr.as_outside_fill(height, len(b["txs"]), k))
            stats["action:OutsideFill"] += 1
        actions += at_index
        stats["action:OutsideFill"] += len(at_index)

        stats["blocks"] += 1
        stats["txs"] += len(b["txs"])
        yield Block(height=height, time_ms=b["blockTime"], actions=tuple(actions))

    stats["outside_fill_no_block"] += sum(len(v) for v in outside.values())


# ------------------------------------------------------------------ recorder files

def _hour_files(hl_data: Path, stem: str) -> list[Path]:
    """One file per hour folder: plain .jsonl if present, else .jsonl.gz."""
    out = []
    for day in sorted(p for p in hl_data.iterdir() if p.is_dir()):
        for hour in sorted(p for p in day.iterdir() if p.is_dir()):
            plain, gz = hour / f"{stem}.jsonl", hour / f"{stem}.jsonl.gz"
            if plain.exists():
                out.append(plain)
            elif gz.exists():
                out.append(gz)
    return out


def _lines(path: Path) -> Iterator[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # the open hour's last line can be cut mid-write; anything else is real
                continue


@dataclass(frozen=True)
class Trade:
    """A recorded trade, integer units."""
    asset: int
    taker_side: Side
    px: int
    sz: int
    time_ms: int
    hash: str
    tid: int
    buyer: str
    seller: str

    @property
    def taker(self) -> str:
        return self.buyer if self.taker_side is Side.BUY else self.seller

    @property
    def maker(self) -> str:
        return self.seller if self.taker_side is Side.BUY else self.buyer

    def as_outside_fill(self, block: int, tx_index: int, item_index: int) -> OutsideFill:
        return OutsideFill(user=self.taker, block=block, tx_index=tx_index,
                           item_index=item_index, hash=self.hash, priority=None,
                           asset=self.asset, maker_user=self.maker,
                           taker_side=self.taker_side, px=self.px, sz=self.sz, tid=self.tid)


def iter_trades(hl_data, coins, t0_ms: int, t1_ms: int) -> Iterator[Trade]:
    """Recorded trades with t0 <= time <= t1, deduplicated by tid, in (time, tid) order."""
    hl_data = Path(hl_data)
    seen: dict[int, Trade] = {}
    for coin in coins:
        asset = COIN_TO_ASSET[coin]
        spec = ASSETS[asset]
        for path in _hour_files(hl_data, f"trades_{coin}"):
            for rec in _lines(path):
                for x in rec["msg"]["data"]:
                    if not (t0_ms <= x["time"] <= t1_ms) or x["coin"] != coin:
                        continue
                    seen[x["tid"]] = Trade(
                        asset=asset, taker_side=Side(x["side"]),
                        px=spec.px_to_ticks(x["px"]), sz=spec.sz_to_lots(x["sz"]),
                        time_ms=x["time"], hash=x["hash"], tid=x["tid"],
                        buyer=x["users"][0].lower(), seller=x["users"][1].lower())
    yield from sorted(seen.values(), key=lambda t: (t.time_ms, t.tid))


@dataclass(frozen=True)
class BookSnapshot:
    asset: int
    time_ms: int
    bids: tuple[tuple[int, int], ...]   # (px ticks, sz lots), best first
    asks: tuple[tuple[int, int], ...]


def _snapshot(asset: int, d: dict) -> BookSnapshot:
    spec = ASSETS[asset]
    conv = lambda lv: tuple((spec.px_to_ticks(x["px"]), spec.sz_to_lots(x["sz"])) for x in lv)
    return BookSnapshot(asset=asset, time_ms=d["time"], bids=conv(d["levels"][0]), asks=conv(d["levels"][1]))


def iter_books(hl_data, coin: str, t0_ms: int, t1_ms: int) -> Iterator[BookSnapshot]:
    """L2 snapshots with t0 <= time <= t1, deduplicated by time, in time order."""
    asset = COIN_TO_ASSET[coin]
    seen: dict[int, dict] = {}
    for path in _hour_files(Path(hl_data), f"book_{coin}"):
        for rec in _lines(path):
            d = rec["msg"]["data"]
            if t0_ms <= d["time"] <= t1_ms:
                seen[d["time"]] = d
    for t in sorted(seen):
        yield _snapshot(asset, seen[t])


def nearest_book(hl_data, coin: str, at_ms: int) -> BookSnapshot:
    """The L2 snapshot whose time is nearest `at_ms` (ties: the earlier one)."""
    asset = COIN_TO_ASSET[coin]
    spec = ASSETS[asset]
    best = None
    for path in _hour_files(Path(hl_data), f"book_{coin}"):
        for rec in _lines(path):
            d = rec["msg"]["data"]
            key = (abs(d["time"] - at_ms), d["time"])
            if best is None or key < best[0]:
                best = (key, d)
    if best is None:
        raise ValueError(f"no book_{coin} snapshots under {hl_data}")
    return _snapshot(asset, best[1])


# ------------------------------------------------------------------ CLI

def summarize(window_dir, hl_data=None) -> Stats:
    stats = Stats()
    cloid_targets = oid_targets = 0
    for block in iter_window(window_dir, hl_data, stats):
        for a in block.actions:
            if isinstance(a, Modify):
                if isinstance(a.target, str):
                    cloid_targets += 1
                else:
                    oid_targets += 1
    stats["modify_target_cloid"] += cloid_targets
    stats["modify_target_oid"] += oid_targets
    return stats


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("window_dir")
    ap.add_argument("--hl-data", help="recorder folder, to add zero-hash trades")
    args = ap.parse_args(argv)
    s = summarize(args.window_dir, args.hl_data)

    print(f"blocks {s['blocks']}   txs {s['txs']}")
    print("\nBTC/ETH actions")
    for k in sorted(k for k in s if k.startswith("action:")):
        print(f"  {k[7:]:<16}{s[k]:>10}")
    cl, oid = s["action:CancelByCloid"], s["action:Cancel"]
    if cl + oid:
        print(f"\ncancels by cloid {cl} ({100 * cl / (cl + oid):.1f}%), by oid {oid}")
    print(f"modify targets: cloid {s['modify_target_cloid']}, oid {s['modify_target_oid']}")
    print(f"\nskipped: trigger orders {s['skip_trigger']}, invalid precision {s['skip_invalid_precision']}, "
          f"zero price or size {s['skip_zero_px_or_sz']}")
    print(f"zero-hash trades (outside fills) {s['action:OutsideFill']}, with no matching block time {s['outside_fill_no_block']}")
    errored = sorted((k, v) for k, v in s.items() if k.startswith("errored:"))
    print("errored txs (all assets): " + (", ".join(f"{k[8:]} {v}" for k, v in errored) or "none"))
    ignored = sorted(((k, v) for k, v in s.items() if k.startswith("ignored:")), key=lambda kv: -kv[1])
    print("ignored action types (all assets): " + ", ".join(f"{k[8:]} {v}" for k, v in ignored[:8]))


if __name__ == "__main__":
    main()
