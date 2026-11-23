"""suspect_makers: rank makers whose orders the replay filled where the chain had no such fill ("phantom makers"),
for the capture's post-only margin snapshot (collect/capture.py ALO_MARGIN_USERS, SUSPECTS_FILE).

A phantom maker fill = an engine fill (size@px user) in a scored, mismatched tx whose (px, user) is not among the
real fills of that tx. txs.csv shows 8-hex user prefixes; full addresses come from the chunk's own block txs.

usage: python -m analysis.suspect_makers <txs.csv ...> --windows DIR --out suspects.json [--top 200]
Use replay outputs of dev / already-used sets only (never final240 or a confirm set that is still to be run).
"""
import argparse
import collections
import csv
import glob
import gzip
import json
import os
import re
import time


def phantom_counts(paths):
    """{(chunk, user prefix): phantom maker fills}."""
    csv.field_size_limit(10**9)
    c = collections.Counter()
    for p in paths:
        for r in csv.DictReader(open(p)):
            if r["warmup"] in ("True", "1") or r["status"] != "mismatch":
                continue
            real = set(re.findall(r"@([0-9.]+) (0x[0-9a-f]+)", r["real"]))
            for _, px, u in re.findall(r"([0-9.]+)@([0-9.]+) (0x[0-9a-f]+)", r["engine"]):
                if (px, u) not in real:
                    c[(r["chunk"], u)] += 1
    return c


def resolve(chunk_dir, prefixes):
    """prefix -> full address from the chunk's block txs (first match; prefixes left unresolved are dropped)."""
    want, out = set(prefixes), {}
    for p in sorted(glob.glob(os.path.join(chunk_dir, "*.json.gz"))):
        if not want:
            break
        b = json.load(gzip.open(p, "rt"))
        for tx in b.get("blockDetails", b).get("txs", []):
            u = (tx.get("user") or "").lower()
            if u[:10] in want:
                out[u[:10]] = u
                want.discard(u[:10])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("txs", nargs="+")
    ap.add_argument("--windows", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=200)
    a = ap.parse_args(argv)
    c = phantom_counts(a.txs)
    by_chunk = collections.defaultdict(set)
    for ch, u in c:
        by_chunk[ch].add(u)
    tot, unresolved = collections.Counter(), 0
    for ch, us in sorted(by_chunk.items()):
        full = resolve(os.path.join(a.windows, ch), us)
        for u in us:
            if u in full:
                tot[full[u]] += c[(ch, u)]
            else:
                unresolved += c[(ch, u)]
    users = [u for u, _ in sorted(tot.items(), key=lambda x: (-x[1], x[0]))][:a.top]
    json.dump({"time": int(time.time() * 1000), "sources": a.txs, "users": users,
               "fills": {u: tot[u] for u in users}, "unresolved_fills": unresolved}, open(a.out, "w"), indent=1)
    n = sum(tot.values())
    print(f"phantom maker fills {n + unresolved} (unresolved {unresolved}), makers {len(tot)}; "
          f"top 20 cover {sum(tot[u] for u in users[:20]) / max(1, n):.0%}")


if __name__ == "__main__":
    main()
