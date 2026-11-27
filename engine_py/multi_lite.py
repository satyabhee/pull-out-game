"""Multiplicity-lite (`--multi-lite`, scoped L5; rule in docs/scoreboard.md, written before this was built).

Alternatives exist per price level only, as small diffs on the queue the engine holds:
- phantom fork: alternative = the set of candidate orders it drops (frozenset(); () = keep all, B1's choice);
- owner fork:   alternative = the owner of one placeholder piece.
The engine book always holds the primary (highest weight; ties: keep-all / the B1 owner first, then newest).
Fills come from the primary before any evidence (rule 1). Evidence: E1 the next tx with real trades at the level,
E2 an owner's cancel, E3 snapshot order counts. Integers for prices and sizes; weights are exact Fractions.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction

from engine_py.matching import PLACEHOLDER
from engine_py.types import Cancel, CancelByCloid, Modify, Place, RejectReason, Side, Tif

MAX_ALTS = 4
MAX_CANDIDATES = 3
MIN_WEIGHT = Fraction(1, 10_000)
FEED_STREAK = 3
INF = 1 << 62


@dataclass
class Fork:
    kind: str                                   # "phantom" | "owner"
    key: tuple                                  # (asset, side, px)
    alts: dict                                  # alternative -> Fraction weight
    primary: object
    rank: dict = field(default_factory=dict)    # oid -> queue index at fork time (phantom)
    stash: dict = field(default_factory=dict)   # oid -> (order, remaining): dropped by the current primary
    cands: dict = field(default_factory=dict)   # phantom: oid -> user; owner: (users ...)
    piece: int | None = None                    # owner: engine oid of the piece (changes on relabel)
    streak: int = 0
    best_prev: frozenset | None = None
    created: int = 0                            # block height


class MultiLite:
    def __init__(self, rp):
        self.rp = rp
        self.forks: dict[tuple, Fork] = {}
        self.pre: dict[str, dict] = {}          # tx hash -> {level key: [(order, remaining)]} before the tx
        self.st = rp.learn_stats
        self.events: list[dict] = []            # collapses (for the sim truth report)

    # ------------------------------------------------------------------ helpers
    def _level(self, key):
        asset, side, px = key
        return self.rp.engine.books[asset].side(side).get(px)

    def _current(self, key) -> list:
        lv = self._level(key)
        return [] if lv is None else [(nd.order, nd.remaining) for nd in lv]

    def _materialize(self, f: Fork, alt, current=None) -> list:
        """The queue alternative `alt` would hold now: current engine queue + stashed candidates (back at their
        fork-time rank) - alt's drops; owner forks relabel the piece."""
        cur = list(self._current(f.key) if current is None else current)
        if f.kind == "owner":
            return [((alt if o.oid == f.piece else o.user), r) for o, r in cur]
        full = list(cur)
        for oid, (o, r) in sorted(f.stash.items(), key=lambda kv: f.rank.get(kv[0], INF)):
            rk = f.rank.get(oid, INF)
            j = next((i for i, (x, _) in enumerate(full) if f.rank.get(x.oid, INF) > rk), len(full))
            full.insert(j, (o, r))
        return [(o, r) for o, r in full if o.oid not in alt]

    def _sig(self, f: Fork, alt) -> tuple:
        q = self._materialize(f, alt)
        return tuple(((x if isinstance(x, str) else x.user), r) for x, r in q)

    def _renorm(self, f: Fork) -> None:
        sigs: dict = {}
        for alt, w in list(f.alts.items()):                 # merge alternatives with the same queue
            s = self._sig(f, alt)
            if s in sigs:
                keep = sigs[s]
                if self._tie_key(f, alt) < self._tie_key(f, keep):
                    f.alts[alt] = f.alts.pop(keep) + w
                    sigs[s] = alt
                else:
                    f.alts[keep] += f.alts.pop(alt)
                self.st["ml_merged"] += 1
            else:
                sigs[s] = alt
        tot = sum(f.alts.values())
        f.alts = {a: w / tot for a, w in f.alts.items()} if tot else {}
        for a in [a for a, w in f.alts.items() if w < MIN_WEIGHT]:
            del f.alts[a]
            self.st["ml_dropped_small"] += 1
        tot = sum(f.alts.values())
        f.alts = {a: w / tot for a, w in f.alts.items()} if tot else {}

    def _tie_key(self, f: Fork, alt):
        if f.kind == "phantom":                      # keep-all first, then the newest candidate dropped
            return (len(alt), -max((f.rank.get(o, 0) for o in alt), default=0))
        return (f.cands.index(alt) if alt in f.cands else INF,)

    def _choose(self, f: Fork):
        return min(f.alts, key=lambda a: (-f.alts[a], self._tie_key(f, a)))

    def _apply_primary(self, f: Fork, alt) -> None:
        """Write alternative `alt` into the engine's level (nothing scored changes)."""
        if alt == f.primary:
            return
        eng = self.rp.engine
        cur = self._current(f.key)
        if f.kind == "owner":
            for o, r in cur:
                if o.oid == f.piece:
                    self._relabel(f, o, alt)
                    break
        else:
            want = self._materialize(f, alt, cur)
            full = self._materialize(f, frozenset(), cur)
            want_ids = [o.oid for o, _ in want]
            if want_ids != [o.oid for o, _ in cur]:
                for o, _ in cur:
                    eng._remove(o.oid)
                for o, r in want:
                    eng._rest(o, r)
                f.stash = {o.oid: (o, r) for o, r in full if o.oid not in set(want_ids)}
            self.st["ml_rewrites"] += 1
        f.primary = alt

    def _relabel(self, f: Fork, o, user) -> None:
        eng = self.rp.engine
        asset, side, px = f.key
        cur = self._current(f.key)
        for x, _ in cur:
            eng._remove(x.oid)
        new = None
        for x, r in cur:
            if x.oid == o.oid:
                new = eng._new_order(user, asset, side, px, r, Tif.GTC, None)
                eng._rest(new, r)
            else:
                eng._rest(x, r)
        f.piece = new.oid
        self.rp._trade_named.setdefault((user, asset), []).append(new.oid)
        self.st["ml_relabels"] += 1

    def _close(self, f: Fork, why: str, chosen=None) -> None:
        if chosen is not None and chosen in f.alts:
            self._apply_primary(f, chosen)
        self.forks.pop(f.key, None)
        self.st[f"ml_{f.kind}_closed:{why}"] += 1
        self.events.append({"kind": f.kind, "key": f.key, "why": why, "chosen": f.primary, "cands": f.cands,
                            "piece": f.piece, "height": f.created})

    def _after_update(self, f: Fork, why: str) -> None:
        self._renorm(f)
        if not f.alts:
            self.forks.pop(f.key, None)
            self.st[f"ml_{f.kind}_closed:empty"] += 1
            return
        new = self._choose(f)
        if new != f.primary:
            self._apply_primary(f, new)
        if len(f.alts) == 1:
            self._close(f, why, new)

    # ------------------------------------------------------------------ hooks
    def on_snapshot(self, s, height: int) -> None:
        """Phantom-or-real trigger + E3 (after B1's reconcile + refill at a re-seed snapshot)."""
        for side, levels, ns in ((Side.BUY, s.bids, s.bid_n), (Side.SELL, s.asks, s.ask_n)):
            for i, (px, _sz) in enumerate(levels):
                if i >= len(ns):
                    continue
                key, n = (s.asset, side, px), ns[i]
                f = self.forks.get(key)
                if f is not None:                                  # E3
                    if f.created == height:
                        continue
                    d = {a: abs(len(self._materialize(f, a)) - n) for a in f.alts}
                    best = min(d.values())
                    bset = frozenset(a for a, x in d.items() if x == best)
                    for a in f.alts:
                        if a not in bset:
                            f.alts[a] /= 2
                    f.streak = f.streak + 1 if bset == f.best_prev else 1
                    f.best_prev = bset
                    self.st[f"ml_{f.kind}_feed_updates"] += 1
                    if f.streak >= FEED_STREAK:
                        self._renorm(f)
                        self._close(f, "feed", self._choose(f))
                    else:
                        self._after_update(f, "feed")
                    continue
                cur = self._current(key)
                if len(cur) <= n:
                    continue
                known = [(o, r) for o, r in cur if o.user != PLACEHOLDER and o.oid in self.rp.placed]
                if len(known) < 2:
                    self.st["ml_phantom_skip_lt2"] += 1
                    continue
                cands = sorted(known, key=lambda x: -x[0].seq)[:MAX_CANDIDATES]
                alts = {frozenset(): Fraction(1)}
                for o, _ in cands:
                    alts[frozenset([o.oid])] = Fraction(1)
                f = Fork("phantom", key, alts, frozenset(), rank={o.oid: j for j, (o, _) in enumerate(cur)},
                         cands={o.oid: o.user for o, _ in cands}, created=height)
                self.forks[key] = f
                self.st["ml_phantom_forks"] += 1
                self._renorm(f)
                if len(f.alts) <= 1:
                    self.forks.pop(key, None)
                    self.st["ml_phantom_closed:trivial"] += 1

    def owner_fork(self, asset: int, side: Side, w: int, piece_oid: int, makers: list[str], height: int) -> None:
        """Owner-of-piece trigger: the piece was just named after makers[0] (most recent); fork over makers[:3]."""
        key = (asset, side, w)
        if key in self.forks:
            self.st["ml_owner_skip_open"] += 1
            return
        cands = tuple(makers[:MAX_CANDIDATES])
        f = Fork("owner", key, {u: Fraction(1) for u in cands}, cands[0], cands=cands, piece=piece_oid,
                 created=height)
        self.forks[key] = f
        self.st["ml_owner_forks"] += 1
        self._renorm(f)

    def before_action(self, a) -> dict:
        """Snapshot forked levels a taker may hit (E1) and the candidates an owner's cancel may hit (E2)."""
        info = {}
        p = a.new if isinstance(a, Modify) else a
        if isinstance(p, Place) and p.tif is not Tif.ALO and a.hash not in self.pre:
            opp = p.side.opposite
            snap = {k: self._current(k) for k in self.forks if k[0] == p.asset and k[1] is opp}
            if snap:
                self.pre[a.hash] = snap
        if isinstance(a, (Cancel, CancelByCloid, Modify)):
            book = self.rp.engine.books[a.asset]
            info["present"] = [(k, oid) for k, f in self.forks.items() if f.kind == "phantom" and k[0] == a.asset
                               for oid, u in sorted(f.cands.items()) if u == a.user and oid in book]
        return info

    def after_action(self, a, rejects, info: dict) -> None:
        if not isinstance(a, (Cancel, CancelByCloid, Modify)):
            return
        book = self.rp.engine.books[a.asset]
        unknown = any(r.reason is RejectReason.UNKNOWN_ORDER for r in rejects)
        for k, oid in info.get("present", ()):                       # E2: owner cancelled a candidate
            f = self.forks.get(k)
            if f is None or oid in book:
                continue
            f.alts = {alt: w for alt, w in f.alts.items() if oid not in alt}
            f.cands.pop(oid, None)
            self.st["ml_phantom_e2_cancel"] += 1
            self._after_update(f, "cancel")
        if unknown:
            for k, f in list(self.forks.items()):
                if k[0] != a.asset:
                    continue
                if f.kind == "phantom":                                  # cancel of a candidate the primary dropped
                    mine = [oid for oid in f.stash if f.cands.get(oid) == a.user]
                    if len(mine) == 1:
                        f.stash.pop(mine[0])
                        f.alts = {alt: w for alt, w in f.alts.items() if mine[0] not in alt}
                        f.cands.pop(mine[0], None)
                        self.st["ml_phantom_e2_unknown"] += 1
                        self._after_update(f, "cancel")
                elif a.user in f.cands and f.piece is not None:          # owner fork: A4(b2) logic
                    self._apply_primary(f, a.user)
                    if f.piece in book:
                        self.rp.engine._remove(f.piece)
                    self.st["ml_owner_e2_cancel"] += 1
                    self._close(f, "cancel")

    def on_real_trades(self, tx_hash: str, trades: list) -> None:
        """E1, at the tx's block-end correction, before B1's corrections."""
        snaps = self.pre.pop(tx_hash, None)
        if not snaps:
            return
        by_level: dict = {}
        for t in trades:
            by_level.setdefault((t.asset, t.taker_side.opposite, t.px), []).append(t)
        for key, real in by_level.items():
            f = self.forks.get(key)
            if f is None or key not in snaps:
                continue
            need = sum(t.sz for t in real)
            makers = [t.maker for t in real]
            if f.kind == "owner":
                pref = self._prefix([(o, r) for o, r in snaps[key]], need)
                if f.piece not in {o.oid for o, _ in pref}:
                    continue
                hit = [u for u in f.cands if u in makers]
                if len(hit) == 1:
                    self.st["ml_owner_e1"] += 1
                    self._close(f, "trade", hit[0])
                elif hit:
                    f.alts = {u: w for u, w in f.alts.items() if u in hit}
                    self._after_update(f, "trade")
                continue
            scores = {}
            for alt in f.alts:
                q = self._materialize(f, alt, snaps[key])
                pref = self._prefix(q, need)
                users = Counter(o.user for o, _ in pref)
                ph = users.pop(PLACEHOLDER, 0)
                sc = 0
                for m in makers:
                    if users.get(m, 0) > 0:
                        sc += 1
                    elif ph > 0:
                        ph -= 1
                        sc += 1
                scores[alt] = sc
            best = max(scores.values())
            f.alts = {a: w for a, w in f.alts.items() if scores[a] == best}
            self.st["ml_phantom_e1"] += 1
            self._after_update(f, "trade")

    @staticmethod
    def _prefix(q: list, need: int) -> list:
        out, got = [], 0
        for o, r in q:
            if got >= need:
                break
            out.append((o, r))
            got += r
        return out

    def end_block(self) -> None:
        self.pre.clear()
        for k, f in list(self.forks.items()):                       # a fork whose subject is gone closes
            cur = {o.oid for o, _ in self._current(k)}
            if f.kind == "owner" and f.piece not in cur:
                self._close(f, "gone")
            elif f.kind == "phantom" and not (set(f.cands) & (cur | set(f.stash))):
                self._close(f, "gone")
