"""Gate 2b post-hoc diagnostics (descriptive, not pre-registered): concentration of W and SQD-height coverage.

    uv run python -m analysis.gate2b_diag report/validate/gate2b
"""
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analysis.gate2b import SQD_SEND_TIMES


def top_shares(vals, ks):
    s = sorted(vals, reverse=True)
    t = sum(s)
    return [round(sum(s[:k]) / t, 3) if t else float("nan") for k in ks]


def main(argv):
    out = Path(argv[0])
    vu, ve = [], []
    wu, we, mk = defaultdict(float), defaultdict(float), defaultdict(float)
    coin = defaultdict(lambda: [0, 0.0, 0.0])
    with open(out / "conflicts.jsonl") as f:
        for line in f:
            x = json.loads(line)
            vu.append(x["v_up"]); ve.append(x["v_est"])
            wu[x["window"]] += x["v_up"]; we[x["window"]] += x["v_est"]; mk[x["maker_user"]] += x["v_est"]
            c = coin[x["coin"]]; c[0] += 1; c[1] += x["v_up"]; c[2] += x["v_est"]
    print(f"conflicts {len(vu):,}; sum v_up {sum(vu):,.2f}; sum v_est before P {sum(ve):,.2f}")
    print(f"conflicts with value > 0: UPPER {sum(v > 0 for v in vu):,}; ESTIMATE {sum(v > 0 for v in ve):,}")
    print(f"share of top 1/10/100/1000 conflicts: UPPER {top_shares(vu, [1, 10, 100, 1000])}; "
          f"ESTIMATE {top_shares(ve, [1, 10, 100, 1000])}")
    print(f"share of top 1/10/50 windows: UPPER {top_shares(list(wu.values()), [1, 10, 50])}; "
          f"ESTIMATE {top_shares(list(we.values()), [1, 10, 50])}")
    print(f"windows with conflicts {len(wu):,}; with ESTIMATE value > 0 {sum(v > 0 for v in we.values()):,}")
    print(f"maker users {len(mk):,}; share of ESTIMATE in top 1/5/10 makers {top_shares(list(mk.values()), [1, 5, 10])}")
    for k in sorted(coin):
        n, u, e = coin[k]
        print(f"{k}: conflicts {n:,}; v_up {u:,.2f}; v_est before P {e:,.2f}")
    # SQD heights: scored in a window, or in a window's warm-up
    got, status = set(), {}
    for p in sorted((out / "windows").glob("*.json")):
        r = json.loads(p.read_text())
        got |= set(r.get("sqd_batches", {}))
        status[p.stem] = r["status"]
    hs = set()
    for path in SQD_SEND_TIMES:
        with open(path) as fh:
            hs |= {row["height"] for row in csv.DictReader(fh)}
    meta = json.loads((out / "meta.json").read_text())
    wroot = Path(meta.get("windows_root", Path.home() / "hl-data/mac3/windows"))
    why = Counter()
    for h in sorted(hs - got):
        hh = int(h)
        w = [s for s in status if int(s.split("-")[0]) <= hh <= int(s.split("-")[1])]
        if not w:
            why["in no run window"] += 1
            continue
        wj = wroot / w[0] / "window.json"
        we_h = json.loads(wj.read_text()).get("warmup_end_height") if wj.exists() else None
        why["in warm-up" if we_h is not None and hh < we_h else f"other ({status[w[0]]})"] += 1
    print(f"SQD heights {len(hs)}; scored {len(hs & got)}; not scored: {dict(why)}")


if __name__ == "__main__":
    main(sys.argv[1:])
