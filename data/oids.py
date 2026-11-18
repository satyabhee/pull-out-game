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

Joint resolution (data/oids.py only, no engine change). Two layers before the per-order rule:
- in-tx (joint_tx): items of one tx get consecutive oids, so counted positions inside a placement tx are
  exact relative oids. A cancel/batchModify tx with >= 2 int oids is matched to the user's resting
  orders per placement tx: hypothesis (T, c), c = oid - counted position, support >= 2. (On real data
  cancel txs never hold 2+ oids, so this fires ~never; kept because the spec asked for it.)
- cross-tx (joint_block): all int-oid cancel/modify-target items of one user in one block. Relative
  spacing of the cancelled oids must match the relative counted positions of the orders they hit, up
  to invisible-event drift: for items a, b with candidate orders xa, xb (each within the per-order
  tolerance)  -lo <= (o_b - o_a) - (k_b - k_a) <= slack + rate * |k_b - k_a|  (a<b by k).
  An item is resolved only if every consistent complete assignment of the group gives it the same
  order (bounded search; budget exceeded = give up). Anything else falls back to the per-order rule.
Groups are read from the window's blocks; the call is located by (engine.block, user, asset, oid).
Counters: resolved_group, resolved_block (both also counted in `resolved`), block_conflict.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections import Counter, OrderedDict
from decimal import Decimal
from pathlib import Path

from data.parse import _block_files, read_block
from engine_py.types import ASSETS, Modify, Place

_ESTIMATOR_CACHE_MAX = 8
_estimator_cache: OrderedDict = OrderedDict()


def clear_cache() -> None:
    _estimator_cache.clear()


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


def _cancel_items(action: dict) -> list[tuple[int, int]]:
    """(asset, int oid) of every BTC/ETH int-oid cancel item of a cancel, modify or batchModify tx."""
    kind = action.get("type")
    out = []
    if kind == "cancel":
        out = [(c.get("a"), c.get("o")) for c in action.get("cancels", [])]
    elif kind == "modify":
        out = [(action.get("order", {}).get("a"), action.get("oid"))]
    elif kind == "batchModify":
        out = [(m.get("order", {}).get("a"), m.get("oid")) for m in action.get("modifies", [])]
    return [(a, o) for a, o in out if a in ASSETS and isinstance(o, int) and not isinstance(o, bool)]


class OidEstimator:
    """Map a placement position to an estimated Hyperliquid oid."""

    def __init__(self, counts: dict[Pos, int], anchors: list[tuple[int, int]],
                 groups: dict | None = None, block_groups: dict | None = None,
                 placers: dict | None = None):
        """counts: position -> number of id-consuming items before it in the window.
        anchors: (count, real oid) pairs, any order.
        groups: (height, user) -> list of per-tx cancel groups (>= 2 items each) of (asset, oid).
        block_groups: (height, user) -> every int-oid cancel item of that user in the block, in order."""
        self.counts = counts
        self.groups = groups or {}
        self.block_groups = block_groups or {}
        self.placers = placers or {}      # (user, asset) -> counted positions of EVERY in-window placement, ascending
        self.anchors = sorted(set(anchors))
        for (k0, o0), (k1, o1) in zip(self.anchors, self.anchors[1:]):
            if k1 == k0 or o1 <= o0:
                raise ValueError(f"inconsistent anchors: count {k0}->{k1}, oid {o0}->{o1}")

    # -------------------------------------------------------------- building
    @classmethod
    def from_window(cls, window_dir, anchors, count_errored: bool = False) -> "OidEstimator":
        """anchors: a list in either format:
        - {"oid", "height", "tx", "item"}                      (checks/g0_check.py, g0_anchors.json)
        - {"user", "oid", "answer": orderStatus response}      (collect/capture.py, anchors.json)
        The second kind is located in the blocks by (user, asset, side, px, sz, time[, cloid]);
        an answer that matches zero or several placements is dropped.
        count_errored: also count the items of txs whose action errored (experiment; default off)."""
        counts: dict[Pos, int] = {}
        groups: dict = {}
        block_groups: dict = {}
        placers: dict = {}
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
                if tx["error"] is not None and not count_errored:
                    continue
                g = _cancel_items(tx["action"])
                if g:
                    ukey = (height, tx["user"].lower())
                    block_groups.setdefault(ukey, []).extend(g)
                    if len(g) >= 2:
                        groups.setdefault(ukey, []).append(g)
                for i, item in enumerate(_order_items(tx["action"])):
                    if item.get("a") in ASSETS:
                        counts[(height, t, i)] = k
                        placers.setdefault((tx["user"].lower(), item["a"]), []).append(k)
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
        return cls(counts, pts, groups, block_groups, placers)

    @classmethod
    def from_files(cls, window_dir, anchor_file) -> "OidEstimator":
        """from_window on the anchor file, built once per process per (window, anchors) and rebuilt when
        a block file or the anchor file changes. The estimator is read-only, so it is shared."""
        window_dir, anchor_file = Path(window_dir), Path(anchor_file)
        st = anchor_file.stat()
        sig = (tuple(sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for _, p in _block_files(window_dir))),
               st.st_size, st.st_mtime_ns)
        key = (window_dir.resolve(), anchor_file.resolve())
        hit = _estimator_cache.get(key)
        if hit is None or hit[0] != sig:
            hit = (sig, cls.from_window(window_dir, json.loads(anchor_file.read_text())))
            _estimator_cache[key] = hit
            while len(_estimator_cache) > _ESTIMATOR_CACHE_MAX:
                _estimator_cache.popitem(last=False)
        _estimator_cache.move_to_end(key)
        return hit[1]

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

    def __init__(self, engine, est: OidEstimator, base: int = 50, per_mille: int = 50, nearest_ratio: int = 0,
                 joint_tx: bool = True, joint_block: bool = True, group_gap: int = 0,
                 pair_slack: int = 3, pair_lo: int = 2, pair_per_mille: int = 80, node_budget: int = 20000,
                 guard_mult: int = 2, ghost_guard: bool = True, max_tol: int | None = 2000,
                 ratio_per_mille: int | None = None):
        """nearest_ratio > 0: when several orders are within tolerance, accept the nearest one only if
        every other is at least nearest_ratio times farther (and >= base ids farther); 0 = never.
        joint_tx / joint_block: the two joint layers (module doc). group_gap: ids that may be missing
        between two items of one placement tx. pair_*: cross-tx spacing tolerance (module doc)."""
        self.joint_tx, self.joint_block, self.group_gap = joint_tx, joint_block, group_gap
        self.pair_slack, self.pair_lo, self.pair_per_mille = pair_slack, pair_lo, pair_per_mille
        self.node_budget, self.guard_mult = node_budget, guard_mult
        self.ratio_per_mille = ratio_per_mille          # refuse when |est - oid| > ratio * tolerance (None = off)
        self.ghost_guard, self.max_tol = ghost_guard, max_tol     # max_tol: orders whose tolerance is larger are never candidates
        a = est.anchors
        spans = [est.distance_to_anchor(min(est.counts.values())), est.distance_to_anchor(max(est.counts.values()))] if est.counts and a else [0]
        spans += [k1 - k0 for (k0, _), (k1, _) in zip(a, a[1:])]
        self._maxtol = base + max(spans) * per_mille // 1000 + 1      # largest tolerance any count can have
        self.meta: dict[int, tuple[int, tuple[int, int]]] = {}   # engine oid -> (count k, (block, tx_index))
        self._jc_block: int | None = None
        self._jc: dict = {}
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
            tol = self.est.tolerance(k, self.base, self.per_mille)
            if self.max_tol is not None and tol > self.max_tol:
                self.stats["far_from_anchor"] += 1        # estimate too loose to resolve against: not a candidate
                return
            self.hl[engine_oid] = (e, tol)
            self.meta[engine_oid] = (k, (action.block, action.tx_index))

    def __call__(self, user: str, asset: int, hl_oid: int) -> int | None:
        self.last, self._last_reason = None, None
        if self.first_hl is not None and hl_oid < self.first_hl - self.est.tolerance(0, self.base, self.per_mille):
            self.stats["prewindow"] += 1
            self._last_reason = "prewindow"                # placed before the window: a placeholder at best
            return None
        if self.joint_tx or self.joint_block:
            oid = self._joint(user, asset, hl_oid)
            if oid is not None:
                self.stats["resolved"] += 1
                self.last = oid
                return oid
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
        if self._refused(user, asset, hl_oid, hits[0][1]):
            return None
        if len(hits) > 1:
            self.stats["resolved_nearest"] += 1
        self.stats["resolved"] += 1
        self.last = hits[0][1]
        return self.last

    def _refused(self, user: str, asset: int, hl_oid: int, x: int) -> bool:
        """Final per-order guards on a resolution to engine order x: ratio, then ghost."""
        if self.ratio_per_mille is not None:
            e, tol = self.hl[x]
            if abs(e - hl_oid) * 1000 > self.ratio_per_mille * tol:
                self.stats["ratio"] += 1
                self._last_reason = "ratio"
                return True
        if self.ghost_guard and self._ghosts(user, asset, hl_oid) > 1:
            self.stats["ghost"] += 1                    # abstain: another placement of the user could be the target
            self._last_reason = "ghost"
            return True
        return False

    def _ghosts(self, user: str, asset: int, hl_oid: int) -> int:
        """How many in-window placements of this user on this asset, resting or not, lie within their own
        tolerance of hl_oid. Always 0 without placer data. The real target is one of them (unless it was
        placed before the window), so if two or more qualify, a unique resting candidate may be a decoy
        while the true target already left our book."""
        ks = self.est.placers.get((user, asset))
        if not ks or not self.est.anchors:
            return 0
        est, maxtol = self.est, self._maxtol
        lo = bisect_left(ks, hl_oid - maxtol, key=est.estimate_count)
        hi = bisect_right(ks, hl_oid + maxtol, key=est.estimate_count)
        n = 0
        for k in ks[lo:hi]:
            if abs(est.estimate_count(k) - hl_oid) <= est.tolerance(k, self.base, self.per_mille):
                n += 1
        return n

    # ------------------------------------------------------------------ joint
    def _joint(self, user: str, asset: int, hl_oid: int) -> int | None:
        block = self.engine.block
        if block != self._jc_block:
            self._jc_block, self._jc = block, {}
        item = (asset, hl_oid)
        for layer, enabled, source in (("tx", self.joint_tx, self.est.groups),
                                       ("block", self.joint_block, self.est.block_groups)):
            if not enabled:
                continue
            groups = source.get((block, user))
            if not groups:
                continue
            if layer == "tx":
                gi = next((i for i, g in enumerate(groups) if item in g), None)
                if gi is None:
                    continue
                key, grp = ("tx", user, gi), groups[gi]
            else:
                if item not in groups:
                    continue
                key, grp = ("block", user), groups
            res = self._jc.get(key)
            if res is None:
                res = self._jc[key] = (self._analyse_tx(user, grp) if layer == "tx"
                                       else self._analyse_block(user, grp))
            oid = res.get(item)
            if oid is not None and oid in self.engine.books[asset]:
                if self._refused(user, asset, hl_oid, oid):
                    return None
                self.stats["resolved_group" if layer == "tx" else "resolved_block"] += 1
                return oid
        return None

    def _mine(self, user: str):
        """The user's resting, estimated orders as [(est oid, tol, k, tx key, engine oid)] sorted by est."""
        out = [(self.hl[x][0], self.hl[x][1], self.meta[x][0], self.meta[x][1], x)
               for x in self.engine.by_user.get(user, {}) if x in self.hl and x in self.meta]
        out.sort()
        return out

    def _candidates(self, mine, keys, asset: int, o: int, maxtol: int):
        book = self.engine.books[asset]
        lo, hi = bisect_left(keys, o - maxtol), bisect_right(keys, o + maxtol)
        return [(k, tx, x) for e, tol, k, tx, x in mine[lo:hi] if abs(e - o) <= tol and x in book]

    def _analyse_tx(self, user: str, group) -> dict:
        items = sorted(set(group))
        if len(items) < 2:
            return {}
        mine = self._mine(user)
        keys = [m[0] for m in mine]
        maxtol = max((m[1] for m in mine), default=0)
        pairs = []                                       # (tx key, c, item index, engine oid)
        for j, (a, o) in enumerate(items):
            for k, tx, x in self._candidates(mine, keys, a, o, maxtol):
                pairs.append((tx, o - k, j, x))
        pairs.sort()
        hyps, i = [], 0
        while i < len(pairs):
            tx, c0 = pairs[i][0], pairs[i][1]
            n = i
            while n < len(pairs) and pairs[n][0] == tx and pairs[n][1] - c0 <= self.group_gap:
                n += 1
            hyps.append([(j, x) for _, _, j, x in pairs[i:n]])
            i = n
        cover: dict[int, list] = {}
        for h in hyps:
            js = {j for j, _ in h}
            if len(js) >= 2:
                for j in js:
                    cover.setdefault(j, []).append(h)
        out = {}
        for j, hs in cover.items():
            xs = {x for jj, x in hs[0] if jj == j}
            if len(hs) == 1 and len(xs) == 1:
                out[items[j]] = next(iter(xs))
            else:
                self.stats["block_conflict"] += 1
        return out

    def _pair_ok(self, oa: int, ka: int, ob: int, kb: int, mult: int = 1) -> bool:
        """Offsets of two cancelled oids agree with the counted positions up to invisible-id drift."""
        if kb < ka:
            oa, ka, ob, kb = ob, kb, oa, ka
        d = (ob - oa) - (kb - ka)                       # ids spent on events we cannot see (>= 0 ideally)
        return -self.pair_lo * mult <= d <= (self.pair_slack + (kb - ka) * self.pair_per_mille // 1000) * mult

    def _analyse_block(self, user: str, group) -> dict:
        """Items pinned to one order in every consistent complete assignment of the user's block cancels,
        under the pair tolerance AND under guard_mult times that tolerance, with the same answer. The wide
        pass is the margin guard: if the true assignment violated the slack (invisible-id noise) a decoy
        could be the only fit; with a wider tolerance the true one comes back and the item turns ambiguous."""
        items = sorted(set(group))
        if len(items) < 2:
            return {}
        mine = self._mine(user)
        keys = [m[0] for m in mine]
        maxtol = max((m[1] for m in mine), default=0)
        cands = [self._candidates(mine, keys, a, o, maxtol) for a, o in items]
        live = [j for j in range(len(items)) if cands[j]]
        if len(live) < 2:
            return {}
        live.sort(key=lambda j: (len(cands[j]), j))     # fewest candidates first: prunes early
        narrow = self._search(items, cands, live, 1)
        wide = self._search(items, cands, live, max(1, self.guard_mult)) if narrow is not None else None
        if narrow is None or wide is None or not narrow:
            self.stats["block_conflict"] += 1
            return {}
        out = {j_item: x for j_item, x in narrow.items() if wide.get(j_item) == x}
        if len(out) < len(live):
            self.stats["block_conflict"] += 1
        return out

    def _search(self, items, cands, live, mult):
        """{item: engine oid} for items with the same order in every consistent complete assignment,
        {} when none is consistent, None when the search budget ran out."""
        sols: list[list[int]] = []
        nodes = 0
        assign: list[tuple[int, int, int]] = []         # (j, k, x) in DFS order
        used: set[int] = set()
        def dfs(d: int) -> bool:
            nonlocal nodes
            if d == len(live):
                sols.append([x for _, _, x in sorted(assign)])
                return len(sols) <= 64
            j = live[d]
            o = items[j][1]
            for k, tx, x in cands[j]:
                nodes += 1
                if nodes > self.node_budget:
                    return False
                if x in used or any(not self._pair_ok(o, k, items[jj][1], kk, mult) for jj, kk, _ in assign):
                    continue
                assign.append((j, k, x)); used.add(x)
                ok = dfs(d + 1)
                assign.pop(); used.discard(x)
                if not ok:
                    return False
            return True
        if not dfs(0):
            return None
        out = {}
        for pos, j in enumerate(sorted(live)):
            xs = {s[pos] for s in sols}
            if len(xs) == 1:
                out[items[j]] = next(iter(xs))
        return out
