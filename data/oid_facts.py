"""Order-id facts from the info API and the recorder's user streams, for data/oids.py.

Sources (all optional; a window without them gives empty facts and the resolver behaves as before):
  window folder (collect/capture.py, chunks):
    fills.json        userFillsByTime answers: every fill of a user with the order's oid and the tx hash
    hist_orders.json  historicalOrders answers: a user's recent orders (oid, px, size, timestamp, final status)
    open_orders.json  frontendOpenOrders answers at the chunk's snapshot: resting orders with oid and timestamp
  recorder (hl_data/<day>/<hour>/, collect/record_hl.py, only users tracked at that time):
    user_orders.jsonl orderUpdates (oid, px, size, timestamp, status) of the tracked top makers
    user_fills.jsonl  userFills of the tracked top makers

Two normalized lists:
  orders: {"user", "asset", "buy", "px" (str), "sz" (str, original size), "timestamp", "oid", "cloid", "status", "source"}
  fills:  {"user", "asset", "buy" (the user's side), "px" (str), "oid", "hash", "crossed", "time", "source"}
Only BTC/ETH. Users lower-cased. These are TRUTH about real orders: data/oids.py uses them to locate placements
(anchors) and to identify the order a cancel names; they are never fed to the engine as actions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from data.parse import _block_files, _hour_files, _records, read_block
from engine_py.types import ASSETS

_COIN = {s.name: s.asset for s in ASSETS.values()}
WINDOW_FILES = {"fills.json": "fills", "hist_orders.json": "hist", "open_orders.json": "open"}


@dataclass
class OidFacts:
    orders: list = field(default_factory=list)
    fills: list = field(default_factory=list)

    def __bool__(self):
        return bool(self.orders or self.fills)

    def files_sig(self):
        return len(self.orders), len(self.fills)


def _order(o: dict, user: str, source: str, status=None) -> dict | None:
    asset = _COIN.get(o.get("coin"))
    if asset is None or "oid" not in o:
        return None
    try:
        return {"user": user.lower(), "asset": asset, "buy": o["side"] == "B", "px": str(o["limitPx"]),
                "sz": str(o.get("origSz", o.get("sz"))), "timestamp": int(o["timestamp"]), "oid": int(o["oid"]),
                "cloid": o.get("cloid"), "status": status, "source": source}
    except (KeyError, TypeError, ValueError):
        return None


def _fill(f: dict, user: str, source: str) -> dict | None:
    asset = _COIN.get(f.get("coin"))
    if asset is None or "oid" not in f:
        return None
    try:
        return {"user": user.lower(), "asset": asset, "buy": f["side"] == "B", "px": str(f["px"]),
                "oid": int(f["oid"]), "hash": f.get("hash"), "crossed": bool(f.get("crossed")),
                "time": int(f["time"]), "source": source}
    except (KeyError, TypeError, ValueError):
        return None


def _window_range(window_dir: Path):
    files = _block_files(window_dir)
    if not files:
        return None, None
    return read_block(files[0][1])["blockTime"], read_block(files[-1][1])["blockTime"]


def load(window_dir, hl_data=None, recorder_margin_ms: int = 60_000) -> OidFacts:
    """Facts for one window: its info files, plus the recorder's user streams within the window's time range
    (+- recorder_margin_ms)."""
    window_dir = Path(window_dir)
    facts = OidFacts()
    for name, src in WINDOW_FILES.items():
        p = window_dir / name
        if not p.exists():
            continue
        for rec in json.loads(p.read_text()):
            ans, user = rec.get("answer"), rec.get("user", "")
            if not isinstance(ans, list):
                continue
            for x in ans:
                if not isinstance(x, dict):
                    continue
                if src == "fills":
                    f = _fill(x, user, src)
                    if f:
                        facts.fills.append(f)
                else:
                    o = _order(x.get("order", x), user, src, x.get("status"))
                    if o:
                        facts.orders.append(o)
    if hl_data is not None:
        t0, t1 = _window_range(window_dir)
        if t0 is not None:
            lo, hi = t0 - recorder_margin_ms, t1 + recorder_margin_ms
            for path in _hour_files(Path(hl_data), "user_orders", lo, hi):
                for r in _records(path, lo, hi):
                    m = r.get("msg") or {}
                    for x in m.get("data") or []:
                        o = _order((x or {}).get("order", {}), m.get("user", ""), "ws", (x or {}).get("status"))
                        if o:
                            facts.orders.append(o)
            for path in _hour_files(Path(hl_data), "user_fills", lo, hi):
                for r in _records(path, lo, hi):
                    m = r.get("msg") or {}
                    d = m.get("data") or {}
                    for x in d.get("fills", []) if isinstance(d, dict) else []:
                        f = _fill(x, m.get("user", ""), "ws")
                        if f and lo <= f["time"] <= hi:
                            facts.fills.append(f)
    return facts
