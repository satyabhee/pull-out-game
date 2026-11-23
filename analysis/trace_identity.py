"""trace_identity: base vs variant txs.csv on a set where the variant "can't act" (data-gated switch rule).

Byte-identical -> IDENTICAL. Otherwise every differing row is listed with the columns that differ; rows where only
book_before differs (the book text shows an order the switch removed, every fill / status / score column equal)
are "traced: book text only". Any row with another differing column, or a tx present in only one file, is
"UNTRACED" and must be explained by hand before the rule counts as met.

usage: python -m analysis.trace_identity <base txs.csv> <variant txs.csv>
"""
import csv
import filecmp
import sys
from collections import Counter


def main(argv=None):
    a, b = (argv or sys.argv[1:])[:2]
    if filecmp.cmp(a, b, shallow=False):
        print("IDENTICAL")
        return 0
    csv.field_size_limit(10**9)
    ra = {r["hash"]: r for r in csv.DictReader(open(a))}
    rb = {r["hash"]: r for r in csv.DictReader(open(b))}
    kinds, untraced = Counter(), []
    for h in sorted(set(ra) | set(rb)):
        x, y = ra.get(h), rb.get(h)
        if x is None or y is None:
            kinds["UNTRACED: tx in one file only"] += 1
            untraced.append((h, "one file only"))
            continue
        cols = [k for k in x if x[k] != y.get(k)]
        if not cols:
            continue
        if cols == ["book_before"]:
            kinds["traced: book text only" + (" (warm-up)" if x["warmup"] in ("1", "True") else "")] += 1
        else:
            kinds["UNTRACED: " + ",".join(cols)] += 1
            untraced.append((h, ",".join(cols)))
    print(f"rows {len(ra)} / {len(rb)}; differing: {sum(kinds.values())}")
    for k, v in kinds.most_common():
        print(f"  {v:5d}  {k}")
    for h, c in untraced[:20]:
        print(f"  untraced {h[:12]} {c}")
    return 1 if untraced else 0


if __name__ == "__main__":
    sys.exit(main())
