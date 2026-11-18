"""Estimate Hyperliquid order ids for in-window placements and resolve int-oid cancels.

AGENT_PLAN 6.1 lead (c), ENGINE_WINDOW_RULES rule 5. Hyperliquid oids come from one global
counter. Counting rule (G0 fit, rule B in checks/g0_check.py): every SUCCESSFUL order item
consumes one id: each item of an `order` action (any asset, triggers included), each `modify`,
each `batchModify` item. About 1-5% extra ids go to events not visible in blocks, so counts
alone drift; anchors (placements whose real oid is known from orderStatus) pin the count to
real ids, and between anchors the oid is interpolated linearly.

    est = OidEstimator.from_window(window_dir, anchors)
    res = OidResolver(engine, est)
    engine.resolve_oid = res          # Engine calls res(user, asset, hl_oid)
    ... after each engine.apply(action): res.note(action, created_engine_oid)

The resolver never guesses: it returns an engine oid only when exactly one of the user's
resting orders on that asset has an estimated oid within the tolerance. Integers only.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from data.parse import _block_files, read_block
from engine_py.types import ASSETS, Modify, Place

Pos = tuple[int, int, int]           # (height, tx_index, item_index), as in the decoded actions


def _order_items(action: dict) -> list[dict]:
    kind = action.get("type")
    if kind == "order":
        return list(action.get("orders", []))
    if kind == "modify":
        return [action["order"]]
    if kind == "batchModify":
        return [m["order"] for m in action.get("modifies", [])]
    return []


class OidEstimator:
    """Map a placement position to an estimated Hyperliquid oid."""

    def __init__(self, counts: dict[Pos, int], anchors: list[tuple[int, int]]):
        """counts: position -> number of id-consuming items before it in the window.
        anchors: (count, real oid) pairs, any order."""
        self.counts = counts
        self.anchors = sorted(set(anchors))
        for (k0, o0), (k1, o1) in zip(self.anchors, self.anchors[1:]):
            if k1 == k0 or o1 <= o0:
                raise ValueError(f"inconsistent anchors: count {k0}->{k1}, oid {o0}->{o1}")

    # -------------------------------------------------------------- building
    @classmethod
    def from_window(cls, window_dir, anchors) -> "OidEstimator":
        """anchors: a list in either format:
        - {"oid", "height", "tx", "item"}                      (checks/g0_check.py, g0_anchors.json)
        - {"user", "oid", "answer": orderStatus response}      (collect/capture.py, anchors.json)
        The second kind is located in the blocks by (user, asset, side, px, sz, time[, cloid]);
        an answer that matches zero or several placements is dropped."""
        counts: dict[Pos, int] = {}
        want: dict[tuple, int] = {}
        for a in anchors:
            if "height" in a:
                continue
            ans = a.get("answer", {})
            if ans.get("status") != "order":
                continue
            o = ans["order"]["order"]
            asset = next((s.asset for s in ASSETS.values() if s.name == o["coin"]), None)
            if asset is None:
                continue
            key = (a["user"].lower(), asset, o["side"] == "B", Decimal(o["limitPx"]), Decimal(o["origSz"]),
                   int(o["timestamp"]), o.get("cloid"))
            want[key] = int(o["oid"])
        located: dict[int, list[Pos]] = {}
        k = 0
        for height, path in _block_files(Path(window_dir)):
            b = read_block(path)
            for t, tx in enumerate(b["txs"]):
                if tx["error"] is not None:
                    continue
                for i, item in enumerate(_order_items(tx["action"])):
                    if item.get("a") in ASSETS:
                        counts[(height, t, i)] = k
                        if want:
                            try:
                                key = (tx["user"].lower(), item["a"], bool(item["b"]), Decimal(item["p"]),
                                       Decimal(item["s"]), int(tx["time"]), item.get("c"))
                            except Exception:
                                key = None
                            if key in want:
                                located.setdefault(want[key], []).append((height, t, i))
                    k += 1
        pts = [(counts[(a["height"], a["tx"], a["item"])], int(a["oid"]))
               for a in anchors if "height" in a and (a["height"], a["tx"], a["item"]) in counts]
        pts += [(counts[p[0]], oid) for oid, p in located.items() if len(p) == 1]
        return cls(counts, pts)

    @classmethod
    def from_files(cls, window_dir, anchor_file) -> "OidEstimator":
        return cls.from_window(window_dir, json.loads(Path(anchor_file).read_text()))

    # -------------------------------------------------------------- estimating
    def estimate_count(self, k: int) -> int | None:
        """Estimated oid for the item with count k; None without anchors."""
        a = self.anchors
        if not a:
            return None
        if len(a) == 1:
            k0, o0 = a[0]
            return o0 + (k - k0)                        # slope 1: no information on extra ids
        lo, hi = 0, len(a) - 1                          # pick the bracketing (or nearest end) pair
        if k <= a[0][0]:
            (k0, o0), (k1, o1) = a[0], a[1]
        elif k >= a[-1][0]:
            (k0, o0), (k1, o1) = a[-2], a[-1]
        else:
            while hi - lo > 1:
                mid = (lo + hi) // 2
                lo, hi = (mid, hi) if a[mid][0] <= k else (lo, mid)
            (k0, o0), (k1, o1) = a[lo], a[hi]
        return o0 + (k - k0) * (o1 - o0) // (k1 - k0)

    def estimate(self, pos: Pos) -> int | None:
        k = self.counts.get(pos)
        return None if k is None else self.estimate_count(k)

    def distance_to_anchor(self, k: int) -> int:
        return min((abs(k - ka) for ka, _ in self.anchors), default=10**18)

    def tolerance(self, k: int, base: int = 50, per_mille: int = 50) -> int:
        """Allowed |estimate - real| for count k: grows with the distance to the nearest anchor
        (5% of it by default, the top of the G0 extra-id range)."""
        return base + self.distance_to_anchor(k) * per_mille // 1000

    def leave_one_out(self) -> list[int]:
        """Signed estimation error (estimate - real) for each anchor, from the others alone."""
        errs = []
        for i, (k, o) in enumerate(self.anchors):
            rest = OidEstimator(self.counts, self.anchors[:i] + self.anchors[i + 1:])
            e = rest.estimate_count(k)
            if e is not None:
                errs.append(e - o)
        return errs


class OidResolver:
    """resolve_oid(user, asset, hl_oid) -> engine oid | None, for Engine(resolve_oid=...)."""

    def __init__(self, engine, est: OidEstimator, base: int = 50, per_mille: int = 50, nearest_ratio: int = 0):
        """nearest_ratio > 0: when several orders are within tolerance, accept the nearest one only if
        every other is at least nearest_ratio times farther (and >= base ids farther); 0 = never."""
        self.engine, self.est = engine, est
        self.base, self.per_mille, self.nearest_ratio = base, per_mille, nearest_ratio
        self.hl: dict[int, tuple[int, int]] = {}       # engine oid -> (estimated hl oid, tolerance)
        self.first_hl: int | None = None               # estimated oid of the window's first placement
        if est.counts and est.anchors:
            self.first_hl = est.estimate_count(min(est.counts.values()))
        self.stats: Counter = Counter()
        self.last: int | None = None                   # engine oid picked by the last call (diagnostics)

    def note(self, action, engine_oid: int | None) -> None:
        """Record the estimated hl oid of an order the engine just created for `action`."""
        if engine_oid is None or not isinstance(action, (Place, Modify)):
            return
        pos = (action.block, action.tx_index, action.item_index)
        k = self.est.counts.get(pos)
        if k is None:
            return
        e = self.est.estimate_count(k)
        if e is not None:
            self.hl[engine_oid] = (e, self.est.tolerance(k, self.base, self.per_mille))

    def __call__(self, user: str, asset: int, hl_oid: int) -> int | None:
        self.last, self._last_reason = None, None
        if self.first_hl is not None and hl_oid < self.first_hl - self.est.tolerance(0, self.base, self.per_mille):
            self.stats["prewindow"] += 1
            self._last_reason = "prewindow"                # placed before the window: a placeholder at best
            return None
        book = self.engine.books[asset]
        hits = []
        for oid in self.engine.by_user.get(user, {}):
            if oid not in book or oid not in self.hl:
                continue
            e, tol = self.hl[oid]
            if abs(e - hl_oid) <= tol:
                hits.append((abs(e - hl_oid), oid))
        if not hits:
            self.stats["no_candidate"] += 1
            self._last_reason = "no_candidate"
            return None
        hits.sort()
        if len(hits) > 1:
            (d1, _), (d2, _) = hits[0], hits[1]
            if not (self.nearest_ratio and d2 >= max(self.nearest_ratio * d1, d1 + self.base)):
                self.stats["ambiguous"] += 1            # never guess between two of the user's orders
                self._last_reason = "ambiguous"
                return None
            self.stats["resolved_nearest"] += 1
        self.stats["resolved"] += 1
        self.last = hits[0][1]
        return self.last
