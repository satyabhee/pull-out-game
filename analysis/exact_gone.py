"""Cancels whose exact target a fact gives (data/oids exact) but which no longer rests in the engine
("exact_gone"): how the engine lost that order, and what the real chain did with it.

How it left the engine (first matching cause):
  never_rested     created but not resting after its own action (filled as a taker, IOC, or STP)
  filled           fully filled later by an engine taker or outside fill
  cancelled        removed earlier by a cancel / modify / scheduleCancel of the user (another resolution hit it)
  evicted_gate     the ALO touch gate evicted it (an admitted ALO crossed it)
  evicted_snapshot reconcile at a snapshot evicted it (known="oldest" drift)
  trade_learn      the trade_learn rules removed it (sweep / queue)
  other            any other path
Real chain (order-id facts for that oid, hist / ws status): its final status and time against the cancel:
  real_cancelled_here  real final status is canceled at the cancel's block time (+-2 s): the cancel worked on chain
  real_gone_before     a final status (filled, canceled, rejected...) BEFORE the cancel: it failed on chain too
  real_unknown         no status fact

usage: python -m analysis.exact_gone <dirs...> --hl-data H [replay options] [--jobs N]
"""
from __future__ import annotations

import argparse
import sys
import traceback
from collections import Counter
from multiprocessing import Pool

from analysis import validate_replay as vr
from analysis.reproducible import windows_of
from data import oid_facts
from engine_py.types import Cancel, Modify

CAUSE = (("_learn", "trade_learn"), ("evict", "evicted_gate"), ("reconcile", "evicted_snapshot"),
         ("_cancel_hl_oid", "cancelled"), ("_cancel_cloid", "cancelled"), ("end_block", "cancelled"),
         ("_fill", "filled"), ("_place", "filled"), ("_outside_fill", "filled"))


class _Obs(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.wrapped = False
        self.gone_how = {}                  # engine oid -> cause
        self.gone_by = {}                   # engine oid -> (action that removed it, its block height)
        self.rows = Counter()
        self.examples = []
        _Obs.last = self

    def _wrap(self, rp):
        self.wrapped = True
        for book in rp.engine.books.values():
            orig = book.remove

            def remove(oid, orig=orig):
                if oid not in self.gone_how:
                    names = [f.name for f in traceback.extract_stack(limit=12)]
                    c = next((c for n, c in CAUSE if n in names), "other")
                    if c == "cancelled":
                        c = "cancelled_by:" + self._cur_path()
                    self.gone_how[oid] = c
                    self.gone_by[oid] = (self._cur, self._cur_height)
                return orig(oid)
            book.remove = remove

    def before_action(self, rp, block, a):
        self._cur, self._cur_height = a, block.height
        self._stats0 = dict(rp.resolver.stats) if rp.resolver is not None else {}
        self._rp = rp
        return super().before_action(rp, block, a)

    def _cur_path(self):
        """What kind of action is removing an order right now, and how the resolver resolved it."""
        a = self._cur
        kind = type(a).__name__
        if isinstance(a, (Cancel, Modify)) and isinstance(a.oid if isinstance(a, Cancel) else a.target, int):
            res = self._rp.resolver
            if res is None:
                return kind + "/no_resolver"
            d = {k: v - self._stats0.get(k, 0) for k, v in res.stats.items() if v != self._stats0.get(k, 0)}
            for k in ("resolved_exact", "resolved_named", "resolved_group", "resolved_nearest", "resolved"):
                if d.get(k):
                    via = {"resolved_exact": "exact", "resolved_named": "named"}.get(k, "estimate")
                    if via == "estimate" and any(x.startswith("resolved_") and x not in ("resolved_nearest",) for x in d):
                        via = "estimate:" + ",".join(sorted(x for x in d if x.startswith("resolved_")))
                    return kind + "/" + via
            return kind + "/?"
        return kind

    def after_action(self, rp, block, a, fills, rejects, created):
        if not self.wrapped:
            self._wrap(rp)
        out = super().after_action(rp, block, a, fills, rejects, created)
        if created is not None and created not in rp.engine.books[a.asset] and created not in self.gone_how:
            self.gone_how[created] = "never_rested"
        return out

    def note_gone(self, rp, block, a, real_status):
        hl = a.oid if isinstance(a, Cancel) else a.target
        x = rp.resolver.by_pos.get(rp.resolver.est.exact[hl])
        how = self.gone_how.get(x, "other")
        prev, ph = self.gone_by.get(x, (None, None))
        if how.startswith("cancelled_by:"):
            same = (isinstance(prev, Cancel) and prev.oid == hl) or (isinstance(prev, Modify) and prev.target == hl)
            pc = getattr(prev, "cloid", None) or getattr(prev, "target", None)
            how += " same_oid" if same else (" cloid" if prev.__class__.__name__ == "CancelByCloid" else " OTHER_oid")
            how += f" {'same_block' if ph == block.height else 'earlier_block'}"
            if prev is not None and prev.user != a.user:
                how += " other_user"
        part = "scored" if block.height >= rp.warmup_end_height else "leadin"
        st = real_status.get(hl)
        if st is None:
            real = "real_unknown"
        else:
            status, ts = st
            if status == "canceled" and abs(ts - block.time_ms) <= 2000:
                real = "real_cancelled_here"
            elif ts < block.time_ms - 2000:
                real = "real_gone_before:" + status
            else:
                real = "real_later:" + status
        self.rows[(part, how, real)] += 1


def one(job):
    window, hl_data, policy, kw = job
    facts = oid_facts.load(window, hl_data)
    real_status = {}
    for o in facts.orders:
        if o["status"] not in (None, "open") and o["status_ts"] is not None:
            cur = real_status.get(o["oid"])
            if cur is None or o["status_ts"] < cur[1]:
                real_status[o["oid"]] = (o["status"], o["status_ts"])

    # count exact_gone per action by diffing the resolver's counter around each apply
    import engine_py.replay as R
    orig_apply = R.Replay._apply

    def _apply(self, block, action):
        n0 = self.resolver.stats.get("exact_gone", 0) if self.resolver is not None else 0
        out = orig_apply(self, block, action)
        if self.resolver is not None and self.resolver.stats.get("exact_gone", 0) > n0:
            _Obs.last.note_gone(self, block, action, real_status)
        return out
    R.Replay._apply = _apply
    try:
        vr.run(window, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
    finally:
        R.Replay._apply = orig_apply
    return _Obs.last.rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows", nargs="+")
    ap.add_argument("--jobs", type=int, default=4)
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    kw = vr.replay_kwargs(a)
    jobs = [(w, a.hl_data, a.policy, kw) for w in windows_of(a.windows)]
    with Pool(a.jobs) as pool:
        outs = pool.map(one, jobs)
    tot = Counter()
    for o in outs:
        tot.update(o)
    for part in ("scored", "leadin"):
        n = sum(v for k, v in tot.items() if k[0] == part)
        print(f"\n{part}: {n} exact_gone cancels")
        how = Counter()
        real = Counter()
        for (p, h, r), v in tot.items():
            if p == part:
                how[h] += v
                real[r.split(":")[0]] += v
        print("  how the engine lost it:")
        for h, v in how.most_common():
            print(f"    {h:18} {v:6d} {v / max(n, 1):.3f}")
        print("  what the real chain did:")
        for r, v in real.most_common():
            print(f"    {r:22} {v:6d} {v / max(n, 1):.3f}")
        print("  cross (how x real), top:")
        for (p, h, r), v in sorted(tot.items(), key=lambda kv: -kv[1])[:14]:
            if p == part:
                print(f"    {h:18} {r:34} {v:6d}")


if __name__ == "__main__":
    sys.exit(main())
