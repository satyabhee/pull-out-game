"""I27c information check (measurement only): is "proven phantom" a per-user property that PAST evidence predicts?

Replays chunks with the given flags plus --learn-tid. Each time the queue correction cuts at a tid-pinned order, the
replay logs per-user proofs (engine_py/replay.py tid_events): the pinned order's owner = proven REAL (its order rested on
chain), a named order ahead of it whose owner the chain did not fill at that price in the tx = proven PHANTOM.
Reports, in time order per chunk: how often a phantom proof's owner already had a phantom proof earlier (recall of a
"flag after k proofs" rule), and, for flagged users, the share of their LATER proofs that are phantom vs real
(precision). Only past events are used for each decision (rule 1).

usage: python -m analysis.tid_phantoms <chunk-parent> --only CHUNK --hl-data DIR [replay flags] --learn-tid --out F.json
       python -m analysis.tid_phantoms --summarize F1.json F2.json ... [--k 1 2 3]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analysis import validate_replay as vr


def run_one(a) -> int:
    holder = {}

    class Keep(vr.ValidateObserver):
        def before_action(self, rp, block, action):
            holder["rp"] = rp
            return super().before_action(rp, block, action)

    d = Path(a.chunks) / a.only
    vr.run(d, a.hl_data, a.policy, out_dir=None, quiet=True, observer_cls=Keep, **vr.replay_kwargs(a))
    ev = holder["rp"].tid_events if "rp" in holder else []
    Path(a.out).write_text(json.dumps({"chunk": a.only, "events": ev}))
    print(f"{a.only}: {len(ev)} events", file=sys.stderr)
    return 0


def summarize(files: list[str], ks: list[int]) -> dict:
    out = {"chunks": len(files)}
    allev = []
    for f in files:
        d = json.load(open(f))
        allev.append((d["chunk"], d["events"]))
    kinds = Counter(e[3] for _, ev in allev for e in ev)
    out["events"] = dict(kinds)
    for k in ks:
        tp = fp = fn = 0                    # decisions at each proof event, using only earlier blocks
        for _, ev in allev:
            hist: dict[str, Counter] = defaultdict(Counter)
            ev = sorted(ev, key=lambda e: e[0])
            i = 0
            while i < len(ev):
                h = ev[i][0]
                j = i
                while j < len(ev) and ev[j][0] == h:
                    j += 1
                for _, _, u, kind in ev[i:j]:
                    flagged = hist[u]["phantom"] >= k
                    if kind == "phantom":
                        tp += flagged
                        fn += not flagged
                    else:
                        fp += flagged
                for _, _, u, kind in ev[i:j]:
                    hist[u][kind] += 1
                i = j
        out[f"k={k}"] = {"flagged_phantom (tp)": tp, "unflagged_phantom (fn)": fn, "flagged_real (fp)": fp,
                         "recall": round(tp / (tp + fn), 3) if tp + fn else None,
                         "precision": round(tp / (tp + fp), 3) if tp + fp else None}
    users = Counter(e[2] for _, ev in allev for e in ev if e[3] == "phantom")
    out["top_phantom_users"] = [[u[:10], n] for u, n in users.most_common(10)]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks", nargs="?")
    ap.add_argument("--only")
    ap.add_argument("--out")
    ap.add_argument("--summarize", nargs="+")
    ap.add_argument("--k", nargs="+", type=int, default=[1, 2, 3])
    vr.add_replay_args(ap, hl_data_required=False)
    a = ap.parse_args(argv)
    if a.summarize:
        print(json.dumps(summarize(a.summarize, a.k), indent=1))
        return 0
    if not a.learn_tid:
        ap.error("pass --learn-tid")
    return run_one(a)


if __name__ == "__main__":
    sys.exit(main())
