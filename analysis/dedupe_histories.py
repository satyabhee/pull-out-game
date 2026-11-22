"""Decision 6 (before M3): de-duplicate the order histories by oid.

Input: every `windows/<chunk>/hist_orders.json` (capture's historicalOrders answers: a user's recent orders, each with
its status at the time of asking). The same order appears in many answers, often first "open" and later "canceled"
or "filled". Output: one record per oid, the one with the LATEST status (max statusTimestamp, then the answer asked
last), plus the user it belongs to. Conflicts are counted, never resolved silently: an oid whose order fields
(coin, side, limitPx, origSz, tif, cloid, timestamp) or user differ between answers.

Two passes so memory stays small: pass 1 keeps oid -> (best key, file index, record index); pass 2 writes them.
Writes <root>/derived/order_histories.jsonl.gz (data: never committed) and prints / returns the counts.

usage: python -m analysis.dedupe_histories [~/hl-data/mac3]
"""
from __future__ import annotations

import gzip
import json
import sys
from collections import Counter
from pathlib import Path

ID_FIELDS = ("coin", "side", "limitPx", "origSz", "tif", "cloid", "timestamp")


def _answers(path: Path):
    for r in json.loads(path.read_text()):
        ans = r.get("answer")
        if isinstance(ans, list):
            yield r.get("user", "").lower(), r.get("answered_at") or r.get("asked_at") or 0, ans


def dedupe(root: Path) -> dict:
    files = sorted((root / "windows").glob("*/hist_orders.json"))
    best: dict[int, tuple] = {}          # oid -> ((statusTimestamp, answered_at), file i, answer j, rec k)
    ident: dict[int, int] = {}           # oid -> hash of (user, identity fields), to count conflicts
    records = 0
    conflicts = set()
    for i, f in enumerate(files):
        for j, (user, answered, ans) in enumerate(_answers(f)):
            for k, rec in enumerate(ans):
                o = rec.get("order") or {}
                oid = o.get("oid")
                if oid is None:
                    continue
                records += 1
                h = hash((user, *(str(o.get(x)) for x in ID_FIELDS)))
                if ident.setdefault(oid, h) != h:
                    conflicts.add(oid)
                key = ((rec.get("statusTimestamp") or 0, answered), i, j, k)
                if oid not in best or key > best[oid]:
                    best[oid] = key
    # pass 2: write the chosen record for every oid
    want: dict[tuple[int, int, int], int] = {(i, j, k): oid for oid, (_, i, j, k) in best.items()}
    out_dir = root / "derived"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "order_histories.jsonl.gz"
    statuses, coins, users = Counter(), Counter(), Counter()
    with gzip.open(out, "wt") as w:
        for i, f in enumerate(files):
            for j, (user, _, ans) in enumerate(_answers(f)):
                for k, rec in enumerate(ans):
                    if (i, j, k) in want:
                        w.write(json.dumps({"user": user, **rec}, separators=(",", ":")) + "\n")
                        statuses[rec.get("status")] += 1
                        coins[(rec.get("order") or {}).get("coin")] += 1
                        users[user] += 1
    return {"files": len(files), "records": records, "unique_oids": len(best), "conflicting_oids": len(conflicts),
            "users": len(users), "statuses": dict(statuses.most_common()), "coins_top": dict(coins.most_common(5)),
            "out": str(out)}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]).expanduser() if argv else Path.home() / "hl-data" / "mac3"
    res = dedupe(root)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
