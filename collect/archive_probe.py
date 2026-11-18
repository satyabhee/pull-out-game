"""archive_probe.py: measure where the explorer's "archived block" rule starts, inside a small request budget.

For each age bucket (minutes before the newest recorder header) it requests a few blocks once, then again
immediately, and logs every request's response time and outcome. A slow first request with a fast repeat
means a cache miss that a repeat avoids; ages where first requests turn slow show where "archived" starts.
It stops at the first cap error and records which request hit it.

    HL_EXPLORER_LOCK=<capture dir>/explorer.lock \\
    python3 archive_probe.py --hl-data <recorder dir> --out probe.jsonl [--buckets 2,5,10,20,40,80] \\
        [--per-bucket 3] [--control 3] [--max-requests 45] [--seed N]

Budget: (control + buckets x per-bucket) x 2 requests, refused above --max-requests (default 45, under the
100/day the cap message names). No retries: every request sent is one log row. It shares the capture's
explorer lock, so it cannot run while capture.py fetches on the same IP. Run it late in the UTC day (the
lockout, if hit, clears at 00:00 UTC).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from collections import defaultdict

from fetch_blocks import BPS, ArchivedCap, Fetcher, NotYet, recorder_head

JITTER_BLOCKS = 300                   # random offset (~20 s) so probed blocks are not ones others just asked for


def plan(head, buckets_min, per_bucket, control, seed, max_requests=45) -> list[dict]:
    """[{bucket_min, height}] young first; bucket 0 = control blocks ~JITTER old at most. Deterministic in seed."""
    n = (control + len(buckets_min) * per_bucket) * 2
    if n > max_requests:
        raise ValueError(f"{n} requests planned, over the budget of {max_requests}")
    rng = random.Random(seed)
    head_h = head[0]
    out, used = [], set()
    for bucket, k in [(0, control)] + [(b, per_bucket) for b in sorted(buckets_min)]:
        base = head_h - round(bucket * 60 * BPS)
        for _ in range(k):
            while True:
                h = base - rng.randint(0, JITTER_BLOCKS) if bucket == 0 else base + rng.randint(-JITTER_BLOCKS, JITTER_BLOCKS)
                if h not in used and h <= head_h:
                    break
            used.add(h)
            out.append({"bucket_min": bucket, "height": h})
    return out


def run(fetcher, planned, out_path, clock=None) -> dict:
    """First request + immediate repeat per planned block; one JSON line per request; stop at the cap."""
    clock = clock or (lambda: time.perf_counter() * 1000)
    n = 0
    with open(out_path, "a") as f:
        for x in planned:
            for attempt in ("first", "repeat"):
                n += 1
                age = fetcher.age_min(x["height"])
                row = {"t": int(time.time() * 1000), "bucket_min": x["bucket_min"], "height": x["height"],
                       "attempt": attempt, "request_no": n, "age_s": None if age is None else round(age * 60, 1)}
                t0 = clock()
                try:
                    fetcher.get(x["height"], tries=1)
                    row["outcome"] = "ok"
                except ArchivedCap as e:
                    row.update(outcome="archived_cap", error=str(e)[:200], ms=round(clock() - t0, 1))
                    f.write(json.dumps(row) + "\n")
                    return {"requests": n, "cap_hit": True,
                            "cap_at": {"bucket_min": x["bucket_min"], "attempt": attempt, "request_no": n}}
                except NotYet as e:
                    row.update(outcome="not_yet", error=str(e)[:200])
                except Exception as e:                      # logged, not hidden; the probe goes on
                    row.update(outcome="error", error=f"{type(e).__name__}: {e}"[:200])
                row["ms"] = round(clock() - t0, 1)
                f.write(json.dumps(row) + "\n")
    return {"requests": n, "cap_hit": False}


def report(rows) -> str:
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.get("outcome") == "ok":
            by[r["bucket_min"]][r["attempt"]].append(r["ms"])
    lines = []
    for b in sorted(by):
        f, rp = by[b]["first"], by[b]["repeat"]
        med = lambda v: f"{statistics.median(v):.0f} ms" if v else "n/a"
        lines.append(f"{'control' if b == 0 else f'{b} min':>8}: first median {med(f)} (n={len(f)}), "
                     f"repeat median {med(rp)} (n={len(rp)})")
    bad = [r for r in rows if r.get("outcome") != "ok"]
    if bad:
        lines.append("not ok: " + ", ".join(f"#{r['request_no']} {r['outcome']} ({r['bucket_min']} min)" for r in bad))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hl-data", required=True, help="recorder folder (for the newest block header)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--buckets", default="2,5,10,20,40,80")
    ap.add_argument("--per-bucket", type=int, default=3)
    ap.add_argument("--control", type=int, default=3)
    ap.add_argument("--max-requests", type=int, default=45)
    ap.add_argument("--seed", type=int, default=20261118)
    a = ap.parse_args(argv)
    head = recorder_head(a.hl_data)
    if head is None:
        raise SystemExit("no recorder block header under --hl-data: start record_hl.py first")
    buckets = [float(b) if "." in b else int(b) for b in a.buckets.split(",")]
    planned = plan(head, buckets, a.per_bucket, a.control, a.seed, a.max_requests)
    fetcher = Fetcher(head=lambda: recorder_head(a.hl_data), max_age_min=max(buckets) + 30)
    print(f"head {head[0]}; {len(planned)} blocks x 2 = {2 * len(planned)} requests; buckets {buckets} min",
          flush=True)
    summary = run(fetcher, planned, a.out)
    print(json.dumps(summary))
    print(report([json.loads(x) for x in open(a.out)]))
    return 0 if not summary["cap_hit"] else 3


if __name__ == "__main__":
    sys.exit(main())
