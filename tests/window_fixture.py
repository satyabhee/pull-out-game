"""Build tiny synthetic windows (blocks + recorder files) for replay/validate tests."""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

T0 = 1794960199775                     # first block time (ms), as in window 1225181431
H0 = 1000
ZERO = "0x" + "0" * 64
A, B, C, M = ("0x" + c * 40 for c in "abcd")


def order(asset=0, buy=True, px="60000", sz="0.001", tif="Alo", cloid=None, r=False):
    o = {"a": asset, "b": buy, "p": px, "s": sz, "r": r, "t": {"limit": {"tif": tif}}}
    if cloid:
        o["c"] = cloid
    return o


def tx(user, action, h, height, error=None):
    return {"time": 0, "user": user, "action": action, "block": height, "hash": h, "error": error}


def place_tx(user, h, height, *orders):
    return tx(user, {"type": "order", "grouping": "na", "orders": list(orders)}, h, height)


def cancel_tx(user, h, height, asset, oid):
    return tx(user, {"type": "cancel", "cancels": [{"a": asset, "o": oid}]}, h, height)


def schedule_tx(user, h, height, at_ms):
    return tx(user, {"type": "scheduleCancel", "time": at_ms}, h, height)


class Window:
    """window = Window(tmp_path); window.block([...txs]); window.book(...); window.trade(...); window.write()"""

    def __init__(self, root: Path, warmup_blocks: int = 0, name: str = "w", h0: int = H0, t0: int = T0,
                 kind: str | None = None, status: str = "complete", meta: dict | None = None):
        """Several windows (e.g. chunks) can share one root: give each its own name, h0 and t0;
        their recorder lines are appended to the shared hl_data hour files."""
        self.dir = root / "windows" / name
        self.h0, self.t0, self.kind, self.status, self.extra_meta = h0, t0, kind, status, meta or {}
        self.hl = root / "hl_data"
        self.blocks: list[tuple[int, int, list]] = []
        self.books: list[tuple[str, int, list, list]] = []
        self.trades: list[dict] = []
        self.anchors: list[dict] | None = None
        self.bbos: list[dict] = []
        self.warmup_blocks = warmup_blocks

    def block(self, txs, dt_ms=None) -> int:
        h = self.h0 + len(self.blocks)
        t = self.t0 + (dt_ms if dt_ms is not None else 100 * len(self.blocks))
        for x in txs:
            x["block"] = h
            x["time"] = t
        self.blocks.append((h, t, txs))
        return h

    def time_of(self, h):
        return next(t for hh, t, _ in self.blocks if hh == h)

    def book(self, coin, time_ms, bids, asks):
        """bids/asks: [(px str, sz str)], best first."""
        self.books.append((coin, time_ms, bids, asks))

    def bbo(self, coin, time_ms, bid, ask):
        """bid/ask: (px str, sz str, n) or None, as the bbo ws feed sends them."""
        lv = lambda x: {"px": x[0], "sz": x[1], "n": x[2]} if x else None
        self.bbos.append({"channel": "bbo", "data": {"coin": coin, "time": time_ms, "bbo": [lv(bid), lv(ask)]}})

    def trade(self, coin, side, px, sz, time_ms, h, tid, buyer, seller):
        self.trades.append({"coin": coin, "side": side, "px": px, "sz": sz, "time": time_ms,
                            "hash": h, "tid": tid, "users": [buyer, seller]})

    def write(self, alo_status: list[dict] | None = None):
        self.dir.mkdir(parents=True)
        for h, t, txs in self.blocks:
            body = {"type": "blockDetails", "blockDetails": {"height": h, "blockTime": t, "hash": "0xb",
                                                             "proposer": "0xp", "numTxs": len(txs), "txs": txs}}
            with gzip.open(self.dir / f"{h}.json.gz", "wt") as f:
                json.dump(body, f)
        meta = {"start_height": self.h0, "end_height": self.h0 + len(self.blocks) - 1, "status": self.status,
                "warmup_end_height": self.h0 + self.warmup_blocks}
        if self.kind is not None:
            meta["kind"] = self.kind
        meta.update(self.extra_meta)
        (self.dir / "window.json").write_text(json.dumps(meta))
        if self.anchors is not None:
            (self.dir / "anchors.json").write_text(json.dumps(self.anchors))
        hour = self.hl / dt.datetime.fromtimestamp(self.t0 / 1000, dt.UTC).strftime("%Y-%m-%d/%H")
        hour.mkdir(parents=True, exist_ok=True)
        for coin in ("BTC", "ETH"):
            if not any(c == coin for c, *_ in self.books):
                self.books.append((coin, self.t0, [], []))      # every coin needs a seed snapshot
            with open(hour / f"book_{coin}.jsonl", "a") as f:
                for c, t, bids, asks in self.books:
                    if c == coin:
                        lv = lambda xs: [{"px": p, "sz": s, "n": 1} for p, s in xs]
                        f.write(json.dumps({"msg": {"data": {"coin": c, "time": t,
                                                             "levels": [lv(bids), lv(asks)]}}}) + "\n")
            with open(hour / f"trades_{coin}.jsonl", "a") as f:
                for x in self.trades:
                    if x["coin"] == coin:
                        f.write(json.dumps({"msg": {"data": [x]}}) + "\n")
        for coin in ("BTC", "ETH"):
            rows = [m for m in self.bbos if m["data"]["coin"] == coin]
            if rows:
                with open(hour / f"bbo_{coin}.jsonl", "a") as f:
                    f.write("".join(json.dumps({"msg": m}) + "\n" for m in rows))
        if alo_status is not None:
            with open(hour / "alo_status.jsonl", "a") as f:
                for x in alo_status:
                    f.write(json.dumps({"recv_ms": 0, "msg": x}) + "\n")
        return self.dir, self.hl
