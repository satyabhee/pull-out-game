"""Policy-invariance check (measurement only): are the replay's real-world inputs identical under two policies?

For each chunk, the same replay options run under two in-block ordering policies (default listing vs class).
Per block it records the EXTERNAL inputs the corrections read (audit P2-P7, overnight_2026-11-21):
  touch  : RealTouch state the block runs against (bid, ask, last update, latest bbo per asset) - P2 gate, P3 ledger
  snaps  : L2 snapshots applied after the block (+ the coarse sf4 book used for the deep reserve) - P4, P5
  outside: outside fills (tid, px, sz, maker, side) and how many actions follow each in the block's apply order - P6
  learn  : the set of (tx hash, real trade tids) trade_learn corrects from in that block - mid-block real trades
and, separately, POLICY-DEPENDENT facts that are expected to differ (not part of the input digest):
  learn_order: the order in which trade_learn corrections run inside the block.
Inputs must be byte-identical per block (sha256 of a canonical repr). Also reports, per policy, whether any
non-outside action is ever applied after an outside fill in the same block (P6 cannot reach same-block takers).

usage: python -m analysis.policy_invariance <chunk>... --hl-data DIR [--policies listing,class] [replay flags]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from collections import defaultdict
from pathlib import Path

from analysis import validate_replay as vr
from analysis.validate_chunks import _join_variant_values
from engine_py import replay as replay_mod
from engine_py.types import OutsideFill


def _h(x) -> str:
    return hashlib.sha256(repr(x).encode()).hexdigest()


class Recorder:
    """Per-block input records for one replay; filled by the observer and the _learn wrapper."""

    def __init__(self):
        self.inputs: dict[int, dict] = defaultdict(dict)
        self.learn_order: dict[int, list] = defaultdict(list)
        self.seq: dict[int, list] = defaultdict(list)       # block -> ["O" outside | "A" other] in apply order
        self.estimator = None


CURRENT: Recorder | None = None


def make_observer():
    class Obs(vr.ValidateObserver):
        def before_action(self, rp, block, action):
            super().before_action(rp, block, action)
            CURRENT.seq[block.height].append("O" if isinstance(action, OutsideFill) else "A")
            if isinstance(action, OutsideFill):
                CURRENT.inputs[block.height].setdefault("outside", []).append(
                    (action.tid, action.px, action.sz, action.maker_user, action.taker_side.name,
                     len(CURRENT.seq[block.height]) - 1))

        def after_block(self, rp, block, minute):
            super().after_block(rp, block, minute)
            t = rp.touch
            if t is not None:
                CURRENT.inputs[block.height]["touch"] = tuple(
                    (a, t.bid.get(a), t.ask.get(a), t.updated.get(a), t.bbo.get(a)) for a in sorted(t.updated))
            n = len(CURRENT.seq[block.height])
            outs = CURRENT.inputs[block.height].get("outside", [])
            # store "actions after it" instead of absolute index: absolute index is policy-independent too,
            # but this is the quantity P6 is about
            CURRENT.inputs[block.height]["outside"] = [o[:5] + (n - 1 - o[5],) for o in outs]

        def after_reconcile(self, rp, block, snapshot, result, minute):
            super().after_reconcile(rp, block, snapshot, result, minute)
            sf = getattr(rp, "_sf", {}).get((snapshot.asset, snapshot.time_ms))
            CURRENT.inputs[block.height].setdefault("snaps", []).append(
                (snapshot.asset, snapshot.time_ms, snapshot.bids, snapshot.asks,
                 None if sf is None else (sf.bids, sf.asks)))
    return Obs


_orig_learn = replay_mod.Replay._learn


def _learn(self, tx_hash):
    trades = self._real_trades.get(tx_hash) or []
    h = self.blocks[self.block_index].height
    CURRENT.inputs[h].setdefault("learn", set()).add((tx_hash, tuple(sorted(t.tid for t in trades))))
    CURRENT.learn_order[h].append(tx_hash)
    return _orig_learn(self, tx_hash)


def _orig_init_wrap(orig):
    def init(self, *a, **k):
        orig(self, *a, **k)
        est = self.resolver.est if self.resolver is not None else None
        CURRENT.estimator = None if est is None else _h((est.anchors, sorted(est.exact.items()),
                                                        sorted(est.oid_px.items())))
    return init


def digest(rec: Recorder) -> dict[int, dict[str, str]]:
    out = {}
    for h, d in rec.inputs.items():
        out[h] = {k: _h(sorted(v) if isinstance(v, set) else v) for k, v in sorted(d.items())}
    return out


def run_policy(chunk, hl_data, policy, kw) -> Recorder:
    global CURRENT
    CURRENT = Recorder()
    vr.run(chunk, hl_data, policy, out_dir=None, quiet=True, observer_cls=make_observer(), **kw)
    return CURRENT


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks", nargs="+")
    ap.add_argument("--policies", default="listing,class")
    vr.add_replay_args(ap)
    if not any(x.dest == "hl_data" for x in ap._actions):
        ap.add_argument("--hl-data", required=True)
    a = ap.parse_args(_join_variant_values(sys.argv[1:] if argv is None else list(argv)))
    kw = vr.replay_kwargs(a)
    pols = a.policies.split(",")
    replay_mod.Replay._learn = _learn
    replay_mod.Replay.__init__ = _orig_init_wrap(replay_mod.Replay.__init__)
    all_ok = True
    for chunk in a.chunks:
        recs = {p: run_policy(chunk, a.hl_data, p, kw) for p in pols}
        ds = {p: digest(r) for p, r in recs.items()}
        base = pols[0]
        name = Path(chunk).name
        for p in pols[1:]:
            heights = sorted(set(ds[base]) | set(ds[p]))
            diff = [(h, k) for h in heights for k in sorted(set(ds[base].get(h, {})) | set(ds[p].get(h, {})))
                    if ds[base].get(h, {}).get(k) != ds[p].get(h, {}).get(k)]
            est_same = recs[base].estimator == recs[p].estimator
            fields = defaultdict(int)
            for h in heights:
                for k in ds[base].get(h, {}):
                    fields[k] += 1
            order_diff = sum(1 for h in heights if recs[base].learn_order.get(h) != recs[p].learn_order.get(h))
            learn_blocks = sum(1 for h in heights if recs[base].learn_order.get(h))
            print(f"{name} {base} vs {p}: blocks {len(heights)}; input fields compared {dict(fields)}; "
                  f"differing (block, field) {len(diff)}; estimator identical {est_same}")
            if diff:
                all_ok = False
                print(f"  FIRST DIFFS: {diff[:10]}")
            print(f"  trade_learn: blocks with corrections {learn_blocks}; blocks where correction ORDER differs "
                  f"{order_diff} (expected: policy-dependent)")
        for p in pols:
            after = sum(1 for h, s in recs[p].seq.items() if "O" in s and "A" in s[s.index("O"):])
            with_out = sum(1 for s in recs[p].seq.values() if "O" in s)
            print(f"  P6 [{p}]: blocks with outside fills {with_out}; blocks where a book action follows an "
                  f"outside fill {after}")
    print("RESULT", "PASS (inputs identical)" if all_ok else "FAIL (inputs differ)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
