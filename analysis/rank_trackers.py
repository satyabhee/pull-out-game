"""Rank users for the recorder's order-id tracking by how much exact order ids help the replay resolve their cancels.

Score per user over the most recent chunks: int-oid cancels/modifies that resolved only through an exact fact
(resolved_exact: what tracking buys for users already tracked) plus those left unresolved as ambiguous,
ghost (guard) or no_candidate (what tracking would buy for the others). Duplicate cancels (exact_gone: the
target already went, on chain too), pre-window targets and cancels the estimate resolves count nothing.
Counting resolved_exact keeps a tracked user ranked: their failures vanish once tracked, their need does not.

Contested slots (--contested-slots K, default 0): the first K places go to the makers with the most per-tx miss
on CONTESTED scored txs (a real maker also cancels / modifies / posts ALO on that asset in that block:
analysis/contested) over the latest --contested-chunks chunks; each tx's miss (1 - matched / real notional) is
split over its makers (real, and engine makers other than placeholders). The other places follow the cancel
score. Per-chunk results are cached (--cache), so each round replays only new chunks.

Writes {"time" (ms), "chunks", "counts" {user: rank score}, "cancel_scores", "contested_scores"} for
collect/record_hl.py (CANCELLERS_FILE, TRACK_BY=cancels): the recorder takes the top of "counts".

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
from analysis.contested import ASSET_OF, block_actors
from engine_py.matching import PLACEHOLDER
from engine_py.types import ASSETS, Cancel, Modify

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


def chunk_scores(d, hl_data, policy, kw) -> dict:
    """{"cancel": {user: n}, "parts": {reason: n}, "contested": {user: miss weight}} for one chunk."""
    res, rows = vr.run(d, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
    obs = _Obs.last
    byhash = {r["hash"]: r for r in rows}
    contested, actors = Counter(), {}
    for rec in obs.txs.values():
        r = byhash.get(rec.hash)
        if r is None or r["warmup"] or r["status"] not in ("match", "mismatch") or r["real_notional"] <= 0:
            continue
        if rec.block not in actors:
            p = Path(d) / f"{rec.block}.json.gz"
            actors[rec.block] = block_actors(p) if p.exists() else {}
        acts = actors[rec.block].get(rec.asset, set())
        real = {m for _, _, m in rec.real}
        if not any(m[:10] in acts for m in real):
            continue
        makers = real | {m for _, _, m, _ in rec.engine if m != PLACEHOLDER}
        miss = 1 - r["matched_notional"] / r["real_notional"]
        for m in makers:
            contested[m] += miss / len(makers)
    return {"cancel": dict(obs.score), "parts": dict(obs.parts), "contested": dict(contested)}


def rank(windows_dir, hl_data, policy, kw, n, contested_n=0, cache=None):
    """Cancel score over the latest n chunks, contested score over the latest contested_n."""
    cache = cache if cache is not None else {}
    names = recent_chunks(windows_dir, max(n, contested_n))
    for d in names:
        if d.name not in cache:
            try:
                cache[d.name] = chunk_scores(d, hl_data, policy, kw)
            except Exception as e:                  # one bad chunk must not stop the ranking
                print(f"{d.name}: {e!r}", file=sys.stderr)
    score, parts, contested, used = Counter(), Counter(), Counter(), []
    for i, d in enumerate(reversed(names)):         # newest first
        c = cache.get(d.name)
        if c is None:
            continue
        if i < n:
            score.update(c["cancel"])
            parts.update(c["parts"])
            used.append(d.name)
        if i < contested_n:
            contested.update(c["contested"])
    return score, parts, used, contested


def order(score: Counter, contested: Counter, k: int, total: int = 50) -> list:
    """Users in tracking order: the top k contested makers, then the cancel ranking."""
    out = [u for u, v in contested.most_common() if v > 0][:k]
    out += [u for u, _ in score.most_common() if u not in out]
    return out[:total]


def write(path, score, used, now_ms=None, contested=None, k=0):
    users = order(score, contested or Counter(), k)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump({"time": int(now_ms if now_ms is not None else time.time() * 1000), "chunks": used,
                   "counts": {u: len(users) - i for i, u in enumerate(users)},
                   "cancel_scores": dict(score.most_common(50)),
                   "contested_scores": {u: round(v, 4) for u, v in (contested or Counter()).most_common(50)}}, f)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows_dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--chunks", type=int, default=6)
    ap.add_argument("--every", type=float, default=0, help="seconds between rankings; 0 = once")
    ap.add_argument("--contested-slots", type=int, default=0)
    ap.add_argument("--contested-chunks", type=int, default=40)
    ap.add_argument("--cache", help="json file caching per-chunk scores")
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    if a.every:
        # Each round runs in a fresh child process: replay caches (decoded blocks, recorder files, estimators)
        # are module-level and grew to ~10 GB over a day in one long-lived process. A child returns it all.
        import subprocess
        args = list(sys.argv[1:] if argv is None else argv)
        child = [x for i, x in enumerate(args)
                 if not (x.startswith("--every") or (i > 0 and args[i - 1] == "--every"))]
        while True:
            t = time.time()
            rc = subprocess.run([sys.executable, "-m", "analysis.rank_trackers", *child]).returncode
            if rc:
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC round failed (exit {rc})", flush=True)
            time.sleep(max(a.every - (time.time() - t), 30))
    kw = vr.replay_kwargs(a)
    cache = {}
    if a.cache and os.path.exists(a.cache):
        try:
            cache = json.load(open(a.cache))
        except ValueError:
            cache = {}
    if True:
        t = time.time()
        cn = a.contested_chunks if a.contested_slots else 0
        score, parts, used, contested = rank(a.windows_dir, a.hl_data, a.policy, kw, a.chunks, cn, cache)
        if a.cache:
            keep = {d.name for d in recent_chunks(a.windows_dir, max(a.chunks, cn))}
            cache = {k: v for k, v in cache.items() if k in keep}
            with open(a.cache + ".tmp", "w") as f:
                json.dump(cache, f)
            os.replace(a.cache + ".tmp", a.cache)
        if score:
            write(a.out, score, used, contested=contested, k=a.contested_slots)
        top = ", ".join(f"{u[:10]}" for u in order(score, contested, a.contested_slots)[:6])
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC {len(used)} chunks "
              f"{dict(parts)} tracking: {top} ({time.time() - t:.0f} s)", flush=True)
        return


if __name__ == "__main__":
    sys.exit(main())
