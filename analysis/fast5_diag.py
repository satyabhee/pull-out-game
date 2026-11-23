"""fast5_diag: why do phantom-maker fills survive --deep-ledger fast5 / fast5touch?

Replays each chunk with the given options (one must set --deep-ledger) and the engine's fast5 decision log on; for
every phantom-maker fill (scored mismatched tx, engine maker not a placeholder and not among the real makers) reports
the LAST fast5 decision at that asset / maker side / price at or before the fill's block, and how many blocks before.
"never compared" = no aligned feed update ever put that level up for comparison (outside the feed's 5 levels, better
than the feed's best, touch with plain fast5, or no aligned update).

usage: python -m analysis.fast5_diag <dirs...> --hl-data H [replay options incl. --deep-ledger] [--jobs 1]
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
from multiprocessing import Pool

from analysis import validate_replay as vr
from analysis.reproducible import windows_of
from engine_py.matching import PLACEHOLDER


class _Obs(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._rp = None
        _Obs.last = self

    def before_action(self, rp, block, a):
        if rp.fast5_log is None:
            rp.fast5_log = []
        self._rp = rp
        super().before_action(rp, block, a)


def one(job):
    window, hl_data, policy, kw = job
    res, rows = vr.run(window, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
    obs = _Obs.last
    rp = obs._rp
    log = defaultdict(list)                                  # (asset, side, px) -> [(height, outcome, k)]
    for h, asset, side, px, outcome, k, excess, _ in (rp.fast5_log or []):
        log[(asset, side, px)].append((h, outcome.split(" ")[0], k))
    byhash = {r["hash"]: r for r in rows}
    out = Counter()
    for rec in obs.txs.values():
        r = byhash.get(rec.hash)
        if r is None or r["warmup"] or r["status"] != "mismatch":
            continue
        real_makers = {m for _, _, m in rec.real}
        side = rec.side.opposite
        for px, sz, maker, oid in rec.engine:
            if maker == PLACEHOLDER or maker in real_makers:
                continue
            ent = log.get((rec.asset, side, px), [])
            i = bisect.bisect_right(ent, (rec.block, "~")) - 1
            if i < 0:
                out["never compared"] += 1
                continue
            h, outcome, k = ent[i]
            ago = rec.block - h
            out[f"{outcome} ({'same block' if ago == 0 else '1-5 blocks' if ago <= 5 else '>5 blocks'} before)"] += 1
    out["_total"] = sum(v for k, v in out.items() if k != "_total")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows", nargs="+")
    ap.add_argument("--jobs", type=int, default=1)
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    kw = vr.replay_kwargs(a)
    if not kw.get("deep_ledger"):
        raise SystemExit("set --deep-ledger")
    jobs = [(w, a.hl_data, a.policy, kw) for w in windows_of(a.windows)]
    tot = Counter()
    with Pool(a.jobs) as pool:
        for c in pool.imap_unordered(one, jobs):
            tot.update(c)
    n = tot.pop("_total", 0)
    print(f"{len(jobs)} chunks; phantom-maker fills: {n}; last fast5 decision at their level before the fill:")
    for k, v in tot.most_common():
        print(f"  {v:6d} {v / max(1, n):6.1%}  {k}")


if __name__ == "__main__":
    main()
