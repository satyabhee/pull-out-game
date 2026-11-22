"""taker_seed: carry the `learn` block list across chunks.

Reads chunks.csv of a `--taker-reject learn` run (column taker_blocked_end = users blocked when each chunk ended) and
writes {chunk: [users]}: for each chunk, the users blocked at the end of any EARLIER chunk whose snapshot_time is
within --window-min before this chunk's snapshot (never the chunk itself or later ones). Feed it back with
--taker-seed. A seeded user is unblocked by their first real fill, as in plain learn.

usage: python -m analysis.taker_seed <learn variant dir> --out seeds.json [--window-min 30]
"""
import argparse
import csv
import json
from pathlib import Path


def build(rows, window_ms):
    rows = sorted((r for r in rows if r.get("snapshot_time")), key=lambda r: int(r["snapshot_time"]))
    out = {}
    for i, r in enumerate(rows):
        t = int(r["snapshot_time"])
        seed = set()
        for q in rows[:i]:
            if t - window_ms <= int(q["snapshot_time"]) < t:
                seed |= {u for u in q.get("taker_blocked_end", "").split(";") if u}
        out[r["chunk"]] = sorted(seed)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("variant_dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--window-min", type=float, default=30)
    a = ap.parse_args(argv)
    csv.field_size_limit(10**9)
    rows = list(csv.DictReader(open(Path(a.variant_dir) / "chunks.csv")))
    if not any(r.get("taker_blocked_end") for r in rows):
        raise SystemExit("no taker_blocked_end column: rerun the learn variant with this code")
    seeds = build(rows, a.window_min * 60_000)
    json.dump(seeds, open(a.out, "w"), indent=1)
    n = [len(v) for v in seeds.values()]
    print(f"{len(seeds)} chunks, seeded users per chunk: median {sorted(n)[len(n) // 2]}, max {max(n)}")


if __name__ == "__main__":
    main()
