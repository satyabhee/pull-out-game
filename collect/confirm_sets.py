"""confirm_sets: freeze fresh confirm sets after the reserved post-change benchmark (BLITZ: "once the benchmark list is
fixed, every next 60 qualifying chunks become a confirm set, frozen by start height before any replay, used for one round").

Qualifying = exactly mahito's scripts.benchmark_postchange.qualifying (imported from ~/book-replay, same versions and
filters). Ranked by start height. The benchmark (handoff/benchmark_postchange.txt) must exist; confirm set k takes the next
60 qualifying chunks after the LAST benchmark chunk, skipping any chunk already in an existing set or list (dev sets,
confirm sets, final240, confirm68, the benchmark, earlier confirm sets) and any chunk named in a REPLAYED list.
A set is written only when all 60 chunks exist, as handoff/confirm_sets/C<k>.txt (never rewritten) plus a line in
handoff/confirm_sets/README.md (k, first/last start height, frozen-at time, sha256 of the list). Reads window.json only.
usage: python collect/confirm_sets.py (standalone, so analysis.* resolves to mahito's ~/book-replay package, as the benchmark does) [--windows W] [--n 60] [--max-sets 5]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

HOME = Path.home()
sys.path.insert(0, str(HOME / "book-replay"))
from scripts.benchmark_postchange import REPLAYED, qualifying  # noqa: E402

SET_DIRS = ["dev20", "devfast", "dev_margin60", "dev_alo10", "confirm_alo2_60", "confirm_fast5_60", "confirm_margin60",
            "confirm2_margin60", "confirm68", "final240", "held", "held66", "held67"]


def used_chunks(mac3, handoff):
    out = set()
    for s in SET_DIRS:
        p = mac3 / s
        if p.is_dir():
            out |= {d.name for d in p.iterdir() if d.is_dir()}
    for f in list(handoff.glob("*chunks*.txt")) + list((handoff / "confirm_sets").glob("C*.txt")) + \
            [handoff / "benchmark_postchange.txt"]:
        if f.exists():
            out |= {x.strip() for x in f.read_text().split() if "-" in x}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--windows", default=str(HOME / "hl-data/mac3/windows"))
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--max-sets", type=int, default=5)
    a = ap.parse_args(argv)
    handoff = HOME / "book-replay-data/handoff"
    bench = handoff / "benchmark_postchange.txt"
    if not bench.exists():
        print("benchmark list not written yet: nothing frozen", file=sys.stderr)
        return 2
    last = max(int(x.split("-")[0]) for x in bench.read_text().split())
    outdir = handoff / "confirm_sets"
    outdir.mkdir(exist_ok=True)
    used = used_chunks(HOME / "hl-data/mac3", handoff)
    q = [c for c in qualifying(Path(a.windows)) if int(c.name.split("-")[0]) > last
         and c.name not in used and c.name not in REPLAYED]
    k = len(list(outdir.glob("C*.txt")))
    print(f"qualifying after the benchmark (last start {last}), not in any set: {len(q)}; sets already frozen: {k}",
          file=sys.stderr)
    made = 0
    while made < a.max_sets and len(q) >= a.n:
        k += 1
        chunk = q[:a.n]
        q = q[a.n:]
        f = outdir / f"C{k}.txt"
        if f.exists():
            raise SystemExit(f"{f} exists: confirm sets are never rewritten")
        text = "".join(c.name + "\n" for c in chunk)
        f.write_text(text)
        f.chmod(0o444)
        sha = hashlib.sha256(text.encode()).hexdigest()
        with open(outdir / "README.md", "a") as fh:
            fh.write(f"| C{k} | {chunk[0].name.split('-')[0]} | {chunk[-1].name.split('-')[0]} | {len(chunk)} | "
                     f"{time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())} | {sha[:16]} | unused |\n")
        print(f"froze C{k}: {chunk[0].name} .. {chunk[-1].name}", file=sys.stderr)
        made += 1
    if not made:
        print(f"not enough for the next set ({len(q)} of {a.n})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
