"""G1 fill trace: where was the real maker's order when the engine got a fill wrong?

    python -m analysis.g1_fill_trace <window_dir> --hl-data <dir> [replay flags as in validate_replay]

For every scored, mismatched taker tx (validate_replay's rules), each real fill is classified by the
state of the real maker's order at that side and price in our book just before the tx:
  resting, nothing ahead / resting, other size ahead (queue position) / ALO rejected (by our
  rule or gate) / removed by: gate-evict, rule8-evict, touch-reconcile, re-seed clear,
  scheduleCancel, cancel/modify / filled earlier by the engine ("rested") / never quoted here.
Engine-only fills (no real trade) are split by the maker order's kind and age.
Measurement only: no engine behavior changes.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from analysis import validate_replay as vr
from data.parse import iter_trades
from engine_py.matching import PLACEHOLDER, Engine
from engine_py.replay import COINS, Replay, listing
from engine_py.types import Modify, Place, RejectReason, Tif, ZERO_HASH

AGE_BUCKETS = ((0, "same block"), (12, "1-12 blocks (<1 s)"), (170, "13-170 blocks (1-12 s)"))


class TracingEngine(Engine):
    """Engine that labels every order removal with the replay step that caused it."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.why = "cancel/modify"
        self.removals: list[tuple] = []        # (user, asset, side, px, reason)

    def _labelled(self, why, fn, *a, **k):
        prev, self.why = self.why, why
        try:
            return fn(*a, **k)
        finally:
            self.why = prev

    def _remove(self, oid):
        o = super()._remove(oid)
        self.removals.append((o.user, o.asset, o.side, o.px, self.why))
        return o

    def reconcile(self, *a, **k):
        return self._labelled(getattr(self, "reconcile_label", "rule8-evict"), super().reconcile, *a, **k)

    def evict(self, oid):
        return self._labelled("gate-evict", super().evict, oid)

    def clear(self, asset):
        return self._labelled("re-seed clear", super().clear, asset)

    def end_block(self, *a):
        return self._labelled("scheduleCancel", super().end_block, *a)


class TraceObserver(vr.ValidateObserver):
    def __init__(self, real_by_hash, alo_truth):
        super().__init__(real_by_hash, alo_truth)
        self.fate: dict[tuple, list] = defaultdict(list)      # (user, asset, side, px) -> [(seq, event)]
        self.state_at_tx: dict[str, list] = {}
        self._removals_seen = 0

    def _sync_removals(self, rp):
        eng = rp.engine
        for user, asset, side, px, why in eng.removals[self._removals_seen:]:
            self.fate[(user, asset, side, px)].append((self.seq, "removed: " + why))
        self._removals_seen = len(eng.removals)

    def before_action(self, rp, block, a):
        self._sync_removals(rp)
        if a.hash in self.real_by_hash and a.hash not in self.state_at_tx and isinstance(a, (Place, Modify)):
            self.state_at_tx[a.hash] = [self._maker_state(rp, t) for t in self.real_by_hash[a.hash]]
        super().before_action(rp, block, a)

    def _maker_state(self, rp, t) -> str:
        side = t.taker_side.opposite
        level = rp.engine.books[t.asset].side(side).get(t.px)
        nodes = list(level) if level is not None else []
        mine = [i for i, nd in enumerate(nodes) if nd.order.user == t.maker]
        if mine:
            return "resting, nothing ahead" if mine[0] == 0 else "resting, other size ahead (queue)"
        events = self.fate.get((t.maker, t.asset, side, t.px))
        return events[-1][1] if events else "never quoted here"

    def after_action(self, rp, block, a, fills, rejects, created):
        super().after_action(rp, block, a, fills, rejects, created)
        p = a.new if isinstance(a, Modify) else a
        if isinstance(p, Place) and p.tif is not Tif.IOC:
            key = (a.user, p.asset, p.side, p.px)
            if created is not None and created in rp.engine.books[p.asset]:
                self.fate[key].append((self.seq, "rested (filled or removed since)"))
            elif any(r.reason is RejectReason.ALO_WOULD_CROSS for r in rejects):
                self.fate[key].append((self.seq, "ALO rejected"))
        self._sync_removals(rp)


def run(window, hl_data, quiet=False, **replay_kw):
    eng = TracingEngine()
    rp = Replay(window, hl_data, listing, engine=eng, **replay_kw)
    if rp.touch_reconcile:
        eng.reconcile_label = "rule8-evict"          # snapshot path; the touch path relabels below
        orig = rp._reconcile_touch

        def labelled():
            eng.reconcile_label = "touch-reconcile"
            try:
                orig()
            finally:
                eng.reconcile_label = "rule8-evict"
        rp._reconcile_touch = labelled
    real = defaultdict(list)
    for t in iter_trades(hl_data, COINS, rp.t0, rp.t1):
        if t.hash != ZERO_HASH:
            real[t.hash].append(t)
    obs = TraceObserver(real, {})
    rp.observer = obs
    for _ in rp:
        pass
    for h, trs in real.items():
        if h in obs.txs:
            obs.txs[h].real = [(t.px, t.sz, t.maker) for t in trs]

    real_states, eng_only = Counter(), Counter()
    for rec in obs.txs.values():
        if rec.block < rp.warmup_end_height or (not rec.real and not rec.engine):
            continue
        r, e = obs.effective(rec) if rp.reseed else (rec.real, rec.engine)
        if not rp.reseed and rec.real and not obs.real_makers_in_window(rec):
            continue
        if vr.is_match(r, e):
            continue
        if rec.real:
            real_states.update(obs.state_at_tx.get(rec.hash, []))
        else:
            for _, _, maker, oid in rec.engine:
                pl = rp.placed_by.get(oid)
                age = None if pl is None else rec.block - pl.block
                bucket = next((name for hi, name in AGE_BUCKETS if age is not None and age <= hi),
                              ">12 s" if age is not None else "placeholder" if maker == PLACEHOLDER else "?")
                eng_only[bucket] += 1
    if not quiet:
        tot = sum(real_states.values())
        print(f"real fills in mismatched txs: {tot}; real maker's order just before the tx:")
        for k, v in real_states.most_common():
            print(f"  {k:<40}{v:>6}  {100 * v / tot:5.1f}%")
        tot = sum(eng_only.values())
        print(f"engine-only fills: {tot}; by maker order age:")
        for k, v in eng_only.most_common():
            print(f"  {k:<40}{v:>6}  {100 * v / tot:5.1f}%")
    return real_states, eng_only


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("window")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--anchors")
    ap.add_argument("--alo-gate", choices=["touch"])
    ap.add_argument("--evict-known", choices=["oldest", "newest"])
    ap.add_argument("--nearest-ratio", type=int, default=0)
    ap.add_argument("--reseed", action="store_true")
    ap.add_argument("--touch-reconcile", action="store_true")
    ap.add_argument("--touch-n-cap", action="store_true")
    a = ap.parse_args(argv)
    run(a.window, a.hl_data, anchors=a.anchors, alo_gate=a.alo_gate, reconcile_known=a.evict_known,
        nearest_ratio=a.nearest_ratio, reseed=a.reseed, touch_reconcile=a.touch_reconcile,
        touch_n_cap=a.touch_n_cap)


if __name__ == "__main__":
    main()
