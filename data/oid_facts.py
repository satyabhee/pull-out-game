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
  orders: {"user", "asset", "buy", "px" (str), "sz" (str, original size), "rem" (str, size left at status_ts),
           "timestamp", "oid", "cloid", "status", "status_ts" (ms; for open_orders the answer time), "source"}
  fills:  {"user", "asset", "buy" (the user's side), "px" (str), "sz" (str), "oid", "hash", "crossed", "time", "source"}
Only BTC/ETH. Users lower-cased. These are TRUTH about real orders: data/oids.py uses them to locate placements
(anchors) and to identify the order a cancel names; they are never fed to the engine as actions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
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


def _order(o: dict, user: str, source: str, status=None, status_ts=None) -> dict | None:
    asset = _COIN.get(o.get("coin"))
    if asset is None or "oid" not in o:
        return None
    try:
        return {"user": user.lower(), "asset": asset, "buy": o["side"] == "B", "px": str(o["limitPx"]),
                "sz": str(o.get("origSz", o.get("sz"))), "timestamp": int(o["timestamp"]), "oid": int(o["oid"]),
                "cloid": o.get("cloid"), "status": status, "source": source, "rem": str(o.get("sz", o.get("origSz"))),
                "status_ts": int(status_ts) if status_ts is not None else None}
    except (KeyError, TypeError, ValueError):
        return None


def _fill(f: dict, user: str, source: str) -> dict | None:
    asset = _COIN.get(f.get("coin"))
    if asset is None or "oid" not in f:
        return None
    try:
        return {"user": user.lower(), "asset": asset, "buy": f["side"] == "B", "px": str(f["px"]),
                "sz": str(f.get("sz", "0")), "oid": int(f["oid"]), "hash": f.get("hash"), "crossed": bool(f.get("crossed")),
                "time": int(f["time"]), "source": source}
    except (KeyError, TypeError, ValueError):
        return None


def _placements(window_dir: Path) -> tuple[dict, dict]:
    """Every BTC/ETH limit placement (order / modify / batchModify items) of the window's blocks:
    (coin, cloid) -> {users}, (coin, side, px, sz, block time) -> {users}; prices and sizes as Decimal."""
    from decimal import Decimal
    by_cloid, by_shape = {}, {}
    for _, path in _block_files(window_dir):
        b = read_block(path)
        t = b["blockTime"]
        for tx in b.get("txs", []):
            if tx.get("error"):
                continue
            a, u = tx.get("action", {}), tx.get("user", "").lower()
            k = a.get("type")
            items = (a.get("orders", []) if k == "order" else [a.get("order", {})] if k == "modify"
                     else [x.get("order", {}) for x in a.get("modifies", [])] if k == "batchModify" else [])
            for o in items:
                coin = {0: "BTC", 1: "ETH"}.get(o.get("a"))
                if coin is None or "limit" not in o.get("t", {}):
                    continue
                by_shape.setdefault((coin, "B" if o.get("b") else "A", Decimal(o["p"]), Decimal(o["s"]), t),
                                    set()).add(u)
                if o.get("c"):
                    by_cloid.setdefault((coin, o["c"]), set()).add(u)
    return by_cloid, by_shape


def attribute(o: dict, candidates, index) -> str | None:
    """The one user among `candidates` (lower-case) whose placement this order update is, or None."""
    from decimal import Decimal
    by_cloid, by_shape = index
    cands = set(candidates)
    us = set()
    if o.get("cloid"):
        us = by_cloid.get((o.get("coin"), o["cloid"]), set()) & cands
    if not us:
        try:
            key = (o.get("coin"), o.get("side"), Decimal(str(o["limitPx"])), Decimal(str(o.get("origSz", o.get("sz")))),
                   int(o["timestamp"]))
        except (KeyError, ValueError, ArithmeticError):
            return None
        us = by_shape.get(key, set()) & cands
    return next(iter(us)) if len(us) == 1 else None


def _window_range(window_dir: Path):
    files = _block_files(window_dir)
    if not files:
        return None, None
    return read_block(files[0][1])["blockTime"], read_block(files[-1][1])["blockTime"]


SOURCES = ("fills", "hist", "open", "ws_orders", "ws_fills")


def _norm(x) -> str:
    try:
        return format(Decimal(str(x)).normalize(), "f")
    except (ArithmeticError, ValueError):
        return str(x)


def stream_orders_all(window_dir, hl_data, recorder_margin_ms: int = 60_000) -> dict:
    """Reference points on EVERY coin: real oid -> (user or '', cloid, (user, px, origSz, timestamp)) for the
    tracked users' orderUpdates placed inside the window's time range (+- 5 s). Shared-connection updates carry
    no user ('' : located by shape without user, or by cloid)."""
    window_dir = Path(window_dir)
    t0, t1 = _window_range(window_dir)
    out: dict = {}
    if t0 is None or hl_data is None:
        return out
    lo, hi = t0 - recorder_margin_ms, t1 + recorder_margin_ms
    for path in _hour_files(Path(hl_data), "user_orders", lo, hi):
        for r in _records(path, lo, hi, keep_parsed=False):
            m = r.get("msg") or {}
            user = (m.get("user") or "").lower()
            for x in m.get("data") or []:
                o = (x or {}).get("order") or {}
                if "oid" not in o:
                    continue
                try:
                    ts = int(o.get("timestamp", 0))
                except (TypeError, ValueError):
                    continue
                if ts < t0 - 5_000 or ts > t1 + 5_000:
                    continue
                out[int(o["oid"])] = (user, o.get("cloid"),
                                      (user, _norm(o.get("limitPx")), _norm(o.get("origSz", o.get("sz")))), ts)
    return out


def locate_all(places: list, refs: dict) -> list:
    """places: (k, user, cloid, (user, px, sz), time) for every counted item (any asset).
    refs: stream_orders_all output. Returns (k, oid) for refs that match exactly one item."""
    by_cloid: dict = {}
    by_shape: dict = {}
    by_shape_nouser: dict = {}
    for k, user, cloid, shape, t in places:
        if cloid:
            by_cloid.setdefault((cloid, t), []).append(k)
        by_shape.setdefault((shape, t), []).append(k)
        by_shape_nouser.setdefault((shape[1:], t), []).append(k)
    out = []
    for oid, (user, cloid, shape, t) in refs.items():
        hits = by_cloid.get((cloid, t)) if cloid else None
        if not hits:
            hits = by_shape.get((shape, t)) if user else by_shape_nouser.get((shape[1:], t))
        if hits and len(hits) == 1:
            out.append((hits[0], oid))
    return out


def load(window_dir, hl_data=None, recorder_margin_ms: int = 60_000, exclude=()) -> OidFacts:
    """Facts for one window: its info files, plus the recorder's user streams within the window's time range
    (+- recorder_margin_ms). exclude: source names to leave out (SOURCES; ablations)."""
    bad = set(exclude) - set(SOURCES)
    if bad:
        raise ValueError(f"unknown fact sources {sorted(bad)}")
    window_dir = Path(window_dir)
    facts = OidFacts()
    for name, src in WINDOW_FILES.items():
        p = window_dir / name
        if not p.exists() or src in exclude:
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
                    o = _order(x.get("order", x), user, src, x.get("status"), x.get("statusTimestamp"))
                    if o and src == "open":
                        a = rec.get("answered_at")
                        o["status_ts"] = int(a * 1000) if isinstance(a, (int, float)) else None
                    if o:
                        facts.orders.append(o)
    index = None
    if hl_data is not None:
        t0, t1 = _window_range(window_dir)
        if t0 is not None:
            lo, hi = t0 - recorder_margin_ms, t1 + recorder_margin_ms
            for path in ([] if "ws_orders" in exclude else _hour_files(Path(hl_data), "user_orders", lo, hi)):
                for r in _records(path, lo, hi, keep_parsed=False):
                    m = r.get("msg") or {}
                    for x in m.get("data") or []:
                        raw, user = (x or {}).get("order", {}), m.get("user", "")
                        if not user and m.get("shared"):          # shared connection: attribute by placement
                            index = index if index is not None else _placements(window_dir)
                            user = attribute(raw, [u.lower() for u in m["shared"]], index) or ""
                            if not user:
                                continue
                        o = _order(raw, user, "ws", (x or {}).get("status"), (x or {}).get("statusTimestamp"))
                        if o:
                            facts.orders.append(o)
            for path in ([] if "ws_fills" in exclude else _hour_files(Path(hl_data), "user_fills", lo, hi)):
                for r in _records(path, lo, hi):
                    m = r.get("msg") or {}
                    d = m.get("data") or {}
                    for x in d.get("fills", []) if isinstance(d, dict) else []:
                        f = _fill(x, m.get("user", ""), "ws")
                        if f and lo <= f["time"] <= hi:
                            facts.fills.append(f)
    return facts



def resting_at(facts: OidFacts, t0_ms: int, alive_ms: int) -> list[dict]:
    """Orders placed before t0_ms (the window's first block, so none of our placements) that rested at alive_ms
    (the seed snapshot), one per oid in oid order, each {"order": orders-list dict, "ref_ms", "rem"}:
      open_orders answer      still resting at the answer: ref = answer time, rem = size then;
      a final status (hist / ws: canceled, filled, ...) at or after alive_ms: ref = that time, rem = size left then;
      only "open" updates (ws), no final status in the recorder range: ref None (rem = original size).
    A final status before alive_ms drops the order (gone before the snapshot)."""
    by_oid: dict[int, list] = {}
    for o in facts.orders:
        if o["timestamp"] < t0_ms:
            by_oid.setdefault(o["oid"], []).append(o)
    out = []
    for oid in sorted(by_oid):
        recs = by_oid[oid]
        live = [o for o in recs if o["source"] == "open"]
        final = sorted((o for o in recs if o["source"] != "open" and o["status"] not in (None, "open")
                        and o["status_ts"] is not None), key=lambda o: o["status_ts"])
        if final and final[0]["status_ts"] < alive_ms:
            continue
        if live:
            o = live[0]
            out.append({"order": o, "ref_ms": o["status_ts"], "rem": o["rem"]})
        elif final:
            out.append({"order": final[0], "ref_ms": final[0]["status_ts"], "rem": final[0]["rem"]})
        else:
            out.append({"order": recs[0], "ref_ms": None, "rem": recs[0]["sz"]})
    return out
