"""Rank users for the recorder's order-id tracking by how much exact order ids help the replay resolve their cancels.

Score per user over the most recent chunks: int-oid cancels/modifies that resolved only through an exact fact
(resolved_exact: what tracking buys for users already tracked) plus those left unresolved as ambiguous,
ghost (guard) or no_candidate (what tracking would buy for the others). Duplicate cancels (exact_gone: the
target already went, on chain too), pre-window targets and cancels the estimate resolves count nothing.
Counting resolved_exact keeps a tracked user ranked: their failures vanish once tracked, their need does not.

Writes {"time" (ms), "chunks", "counts" {user: score}} for collect/record_hl.py (CANCELLERS_FILE, TRACK_BY=cancels).

usage: python -m analysis.rank_trackers <windows dir> --hl-data H --out trackers.json [--chunks 6] [--every 600]
       [replay options as validate_replay]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from analysis import validate_replay as vr
from engine_py.types import Cancel, Modify

COUNTED = ("resolved_exact", "ambiguous", "ghost", "no_candidate")


class _Obs(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.score = Counter()
        self.parts = Counter()
        _Obs.last = self

    def before_action(self, rp, block, a):
        self._s0 = dict(rp.resolver.stats) if rp.resolver is not None else {}
        return super().before_action(rp, block, a)

    def after_action(self, rp, block, a, fills, rejects, created):
        out = super().after_action(rp, block, a, fills, rejects, created)
        hl = a.oid if isinstance(a, Cancel) else a.target if isinstance(a, Modify) else None
        if rp.resolver is None or not isinstance(hl, int):
            return out
        st = rp.resolver.stats
        for k in COUNTED:
            if st.get(k, 0) > self._s0.get(k, 0):
                self.score[a.user] += 1
                self.parts[k] += 1
                break
        return out


def recent_chunks(windows_dir, n):
    out = []
    for d in Path(windows_dir).iterdir():
        try:
            m = json.loads((d / "window.json").read_text())
        except (OSError, ValueError):
            continue
        if m.get("kind") == "chunk" and m.get("status") == "complete":
            out.append((m["start_height"], d))
    return [d for _, d in sorted(out)[-n:]]


def rank(windows_dir, hl_data, policy, kw, n):
    score, parts, used = Counter(), Counter(), []
    for d in recent_chunks(windows_dir, n):
        try:
            vr.run(d, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
        except Exception as e:                      # one bad chunk must not stop the ranking
            print(f"{d.name}: {e!r}", file=sys.stderr)
            continue
        score.update(_Obs.last.score)
        parts.update(_Obs.last.parts)
        used.append(d.name)
    return score, parts, used


def write(path, score, used, now_ms=None):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump({"time": int(now_ms if now_ms is not None else time.time() * 1000), "chunks": used,
                   "counts": dict(score.most_common(50))}, f)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows_dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--chunks", type=int, default=6)
    ap.add_argument("--every", type=float, default=0, help="seconds between rankings; 0 = once")
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    kw = vr.replay_kwargs(a)
    while True:
        t = time.time()
        score, parts, used = rank(a.windows_dir, a.hl_data, a.policy, kw, a.chunks)
        if score:
            write(a.out, score, used)
        top = ", ".join(f"{u[:10]} {v}" for u, v in score.most_common(8))
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC {len(used)} chunks "
              f"{dict(parts)} top: {top} ({time.time() - t:.0f} s)", flush=True)
        if not a.every:
            return
        time.sleep(max(a.every - (time.time() - t), 30))


if __name__ == "__main__":
    sys.exit(main())
