"""M0 parity: score chunks with every block driven through EngineAdapter.apply_block, then compare each output
CSV byte-for-byte with a baseline validate_chunks output folder.

usage: python -m replay.parity <chunk-parent dir> --hl-data DIR --out DIR --baseline DIR [--options "..."]
       (--options = replay options; default replay.adapter.BASELINE_OPTIONS). Always --jobs 1 (in-process patch).
Exit 0 iff every CSV the baseline wrote is identical.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

from analysis import validate_chunks as vc
from analysis import validate_replay as vr
from engine_py.replay import Replay
from replay import setup_cache
from replay.adapter import (BASELINE_SWITCHES, SWITCHES, EngineAdapter, options_for, run_kwargs_from_options,
                            split_policy)


BACKEND = "rust"


class AdapterDrivenReplay(Replay):
    """The Replay validate_replay.run builds, but its blocks are advanced through the adapter."""

    def __iter__(self):
        ad = EngineAdapter(BACKEND)
        book = ad.book_from_replay(self)          # keeps the observer validate_replay.run attached
        while book.next_height is not None:
            h = book.next_height
            ad.apply_block(book, h)
            yield h, None, None, self._state_hash


USE_CACHE = True


def make_replay(*args, **kw) -> AdapterDrivenReplay:
    """Stands in for Replay(...) inside validate_replay.run: the backend's engine, the setup cache, the adapter."""
    kw["engine"] = EngineAdapter(BACKEND).prepare_engine(kw["engine"])
    rp = setup_cache.get_replay(args, kw, BACKEND) if USE_CACHE else Replay(*args, **kw)
    rp.__class__ = AdapterDrivenReplay            # same object, iterated through the adapter (no new attributes)
    return rp


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def compare(out: Path, baseline: Path) -> list[str]:
    bad = []
    files = sorted(p.relative_to(baseline) for p in baseline.rglob("*.csv"))
    for rel in files:
        mine = out / rel
        if not mine.exists() or sha(mine) != sha(baseline / rel):
            bad.append(str(rel))
    return bad if files else ["<baseline has no CSVs>"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--options", default=None, help="replay options as one quoted string (default: baseline)")
    ap.add_argument("--backend", default="rust", choices=["py", "rust"])
    ap.add_argument("--no-cache", action="store_true", help="build every chunk's Replay (no setup cache)")
    for name, values in SWITCHES.items():          # switch settings (instead of --options); default = baseline
        ap.add_argument(f"--{name}", default=BASELINE_SWITCHES[name], choices=sorted(values))
    a = ap.parse_args(argv)
    global BACKEND, USE_CACHE
    BACKEND, USE_CACHE = a.backend, not a.no_cache
    EngineAdapter(BACKEND)                         # fail fast if the rust extension is missing
    opts = a.options.split() if a.options else options_for(stp=a.stp, order=a.order, correction=a.correction)
    policy, opts = split_policy(opts)
    kw = run_kwargs_from_options(a.hl_data, opts)
    chunks, skipped = vc.find_chunks([a.chunks])
    saved = vr.Replay
    vr.Replay = make_replay
    t = time.perf_counter()
    try:
        vc.run_batch(chunks, a.hl_data, policy, out_dir=a.out, jobs=1, skipped=skipped, **kw)
    finally:
        vr.Replay = saved
    print(f"adapter run ({BACKEND}): {len(chunks)} chunks in {time.perf_counter() - t:.1f} s; setup cache "
          f"{'off' if not USE_CACHE else setup_cache.stats}", file=sys.stderr)
    bad = compare(Path(a.out), Path(a.baseline))
    print("PARITY OK: every baseline CSV identical" if not bad else f"PARITY FAIL: {bad}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
