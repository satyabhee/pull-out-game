"""gate2_input: per-action send-order input for gate 2 from SQD replica-cmds blocks (full action lists + nonces).

For every full SQD block on disk (~/hl-data/sqd/blocks/<height>.jsonl, pulled with the all-actions filter) writes one row
per action: block, block time, batch, class, listing position, signer, nonce, nonce - block time, calibrated send time,
flags. Also writes the block inventory (dev set membership, explorer alignment) and the nonce checks (a)-(c).

Definitions (docs/gate2_data.md has the reasoning and the numbers):
- listing position = SQD actionIndex. RIKA R0 showed explorer listing order = (actionIndex, item) order (2 blocks); this
  script re-checks it on every block whose explorer file we have (analysis.sqd_truth.align).
- class (empirical order inside a batch): C cancel / cancelByCloid; A order, modify or batchModify whose items are ALL
  post-only (Alo); G order / modify / batchModify with at least one non-Alo item (Gtc, Ioc, trigger); O everything else
  (noop, scheduleCancel, updateLeverage, evmRawTx, ...). Rank C0 < A1 < G2 < O3.
- batch: listing order is non-decreasing in class rank inside a consensus batch; a rank DROP starts a new batch
  (batch 0, 1, ...). Batches are not labelled on chain; this is an inference, and rank drops caused by an out-of-class
  action split a batch spuriously (counted in the report as "short batches").
- lag_ms = nonce - block_time (nonce is the signer's ms clock when it signed). fresh: -10 s <= lag <= +2 s.
- calibrated send time: send_cal = nonce - offset_u, offset_u = signer u's clock offset bound. Every action reaches the
  proposer before the block time, so true offset_u >= max(lag) over u's actions; offset_u = the 95th percentile of u's
  fresh lags (robust max; the signer's fastest sends are taken as ~0 latency). Signers with < MIN_N fresh actions get
  the pooled median offset (calib = pooled). Non-fresh nonces get no send time (calib = stale / future): their nonce is
  not a send clock (pre-signed, delayed or counter nonces).
  Calibration error estimate: split-half (offset from even vs odd blocks) per signer with >= 2 * MIN_N fresh actions.
usage: python -m analysis.gate2_input --sqd ~/hl-data/sqd/blocks --sets-root ~/hl-data/mac3 --out handoff/gate2_input
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

from scipy.stats import kendalltau

from analysis.sqd_truth import align

RANK = {"C": 0, "A": 1, "G": 2, "O": 3}
FRESH_LO, FRESH_HI = -10_000, 2_000
MIN_N = 5
# sets that may never choose anything (CLAUDE.local.md, BLITZ rails); blocks there are flagged, not used
DEV = ("dev20", "devfast", "dev_margin60", "dev_alo10", "dev_cap20")
HOLDOUT = ("final240", "confirm68", "held", "held66", "held67", "post", "new3", "new34", "newcov", "tgt")


def _tifs(orders):
    out = []
    for o in orders:
        t = (o or {}).get("t") or {}
        out.append(t["limit"].get("tif") if "limit" in t else "trigger")
    return out


def action_class(a: dict) -> str:
    t = a.get("type")
    if t in ("cancel", "cancelByCloid"):
        return "C"
    if t in ("order", "modify", "batchModify"):
        orders = (a.get("orders", []) if t == "order" else [a.get("order")] if t == "modify"
                  else [m.get("order") for m in a.get("modifies", [])])
        tifs = _tifs(orders)
        if not tifs:
            return "O"
        return "A" if all(x == "Alo" for x in tifs) else "G"
    return "O"


def assets(a: dict) -> list:
    t = a.get("type")
    if t == "order":
        return sorted({o.get("a") for o in a.get("orders", [])})
    if t in ("cancel", "cancelByCloid"):
        return sorted({c.get("a", c.get("asset")) for c in a.get("cancels", [])})
    if t == "modify":
        return [(a.get("order") or {}).get("a")]
    if t == "batchModify":
        return sorted({(m.get("order") or {}).get("a") for m in a.get("modifies", [])})
    return []


def n_items(a: dict) -> int:
    t = a.get("type")
    return len(a.get("orders", []) if t == "order" else a.get("cancels", []) if t in ("cancel", "cancelByCloid")
               else a.get("modifies", []) if t == "batchModify" else [1])


def set_index(root: Path) -> list:
    """[(set, lo, hi, chunk_dir)] for every chunk dir <lo>-<hi> under root/<set>/."""
    out = []
    for s in sorted(p for p in root.iterdir() if p.is_dir()):
        for c in s.iterdir():
            if c.is_dir() and "-" in c.name:
                lo, _, hi = c.name.partition("-")
                if lo.isdigit() and hi.isdigit():
                    out.append((s.name, int(lo), int(hi), c))
    return out


def explorer_txs(chunk_dir: Path, h: int):
    p = chunk_dir / f"{h}.json.gz"
    if not p.exists():
        return None
    with gzip.open(p, "rt") as fh:
        d = json.load(fh)
    bd = d.get("blockDetails", d)
    return bd.get("txs")


def load_block(p: Path):
    d = json.loads(p.read_text().splitlines()[0])
    return d["header"]["number"], d["header"]["timestamp"], d["actions"]


def batches(classes: list) -> list:
    b, out = 0, []
    for i, c in enumerate(classes):
        if i and RANK[c] < RANK[classes[i - 1]]:
            b += 1
        out.append(b)
    return out


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqd", default=str(Path("~/hl-data/sqd/blocks").expanduser()))
    ap.add_argument("--sets-root", default=str(Path("~/hl-data/mac3").expanduser()))
    ap.add_argument("--out", default="handoff/gate2_input")
    a = ap.parse_args()
    out = Path(a.out)
    (out / "blocks").mkdir(parents=True, exist_ok=True)
    idx = set_index(Path(a.sets_root))

    blocks = []
    for p in sorted(Path(a.sqd).glob("*.jsonl"), key=lambda x: int(x.stem) if x.stem.isdigit() else 0):
        if not p.stem.isdigit():
            continue
        h, T, acts = load_block(p)
        blocks.append((h, T, acts))

    # pass 1: rows without calibration
    rows, inv = [], []
    for h, T, acts in blocks:
        cl = [action_class(x["action"]) for x in acts]
        bt = batches(cl)
        sets = sorted({s for s, lo, hi, _ in idx if lo <= h <= hi})
        chunk = next((c for s, lo, hi, c in idx if lo <= h <= hi and (c / f"{h}.json.gz").exists()), None)
        txs = explorer_txs(chunk, h) if chunk else None
        al_n = al_ok = al_mono = None
        if txs is not None:
            m = align(txs, acts)
            got = [v for v in m.values() if v]
            al_n, al_ok = len(txs), len(got)
            keys = [(v[0], v[1]) for v in got]
            al_mono = sum(1 for i in range(1, len(keys)) if keys[i] < keys[i - 1])
        pos_cb = Counter()
        for i, x in enumerate(acts):
            n = x.get("nonce")
            act = x["action"]
            g = act.get("grouping")
            prio = g.get("p", 0) if isinstance(g, dict) else 0
            tier = prio if cl[i] == "G" else ""
            k = (bt[i], cl[i], tier)
            rows.append({"block": h, "block_time_ms": T, "batch": bt[i], "class": cl[i], "priority_p": prio, "tier": tier,
                         "listing_pos": x["actionIndex"],
                         "pos_in_batch_class_tier": pos_cb[k], "signer": (x.get("user") or "").lower(), "nonce": n,
                         "lag_ms": (n - T) if isinstance(n, int) else None, "type": act.get("type"),
                         "n_items": n_items(act), "assets": ";".join(str(s) for s in assets(act)),
                         "btc_eth": int(any(s in (0, 1) for s in assets(act))), "status": x.get("status")})
            pos_cb[k] += 1
        nb = bt[-1] + 1 if bt else 0
        bsz = Counter(bt)
        inv.append({"block": h, "block_time_ms": T, "n_actions": len(acts),
                    "nonce_missing": sum(1 for x in acts if not isinstance(x.get("nonce"), int)),
                    "n_batches": nb, "short_batches_lt10": sum(1 for v in bsz.values() if v < 10),
                    "sets": ";".join(sets), "dev_sets": ";".join(s for s in sets if s in DEV),
                    "holdout_only": int(not any(s in DEV for s in sets) and any(s in HOLDOUT for s in sets)),
                    "explorer_txs": al_n, "aligned": al_ok, "align_inversions": al_mono,
                    "btc_eth_actions": sum(1 for r in rows[-len(acts):] if r["btc_eth"])})

    # nonce reuse: same (signer, nonce) twice anywhere in the data
    seen = Counter((r["signer"], r["nonce"]) for r in rows)
    # pass 2: per-signer offsets (95th pct of fresh lags), split-half error
    fresh = defaultdict(list)
    for r in rows:
        if r["lag_ms"] is not None and FRESH_LO <= r["lag_ms"] <= FRESH_HI:
            fresh[r["signer"]].append((r["block"], r["lag_ms"]))
    off = {u: q([l for _, l in v], 0.95) for u, v in fresh.items() if len(v) >= MIN_N}
    pooled = st.median(off.values())
    split = []
    for u, v in fresh.items():
        ev = [l for b, l in v if b % 2 == 0]
        od = [l for b, l in v if b % 2 == 1]
        if len(ev) >= MIN_N and len(od) >= MIN_N:
            split.append(q(ev, 0.95) - q(od, 0.95))
    for r in rows:
        lag = r["lag_ms"]
        flags = []
        if r["nonce"] is None:
            flags.append("no_nonce")
        elif r["nonce"] < 10 ** 12:
            flags.append("non_ms_nonce")
        if seen[(r["signer"], r["nonce"])] > 1:
            flags.append("reused")
        if lag is not None and lag > FRESH_HI:
            flags.append("future")
        if lag is not None and lag < FRESH_LO:
            flags.append("stale")
        u = r["signer"]
        if lag is None or not (FRESH_LO <= lag <= FRESH_HI):
            r["offset_ms"], r["send_cal_ms"], r["calib"] = "", "", ("future" if lag and lag > 0 else "stale")
        else:
            o = off.get(u, pooled)
            r["offset_ms"], r["calib"] = o, ("signer" if u in off else "pooled")
            r["send_cal_ms"] = r["nonce"] - o
        r["send_minus_block_ms"] = (r["send_cal_ms"] - r["block_time_ms"]) if r["send_cal_ms"] != "" else ""
        r["flags"] = ";".join(flags)

    # (a) nonce order vs listing order within (block, batch, class); raw nonce and calibrated send time
    groups = defaultdict(list)
    for r in rows:
        groups[(r["block"], r["batch"], r["class"], r["tier"])].append(r)
    agree = []
    for (h, b, c, tr), g in groups.items():
        g2 = [r for r in g if r["send_cal_ms"] != ""]
        res = {"block": h, "batch": b, "class": c, "tier": tr, "n": len(g), "n_fresh": len(g2)}
        for name, sub, key in (("nonce", g, "nonce"), ("fresh_nonce", g2, "nonce"), ("send_cal", g2, "send_cal_ms")):
            xs = [r["listing_pos"] for r in sub if r[key] is not None]
            ys = [r[key] for r in sub if r[key] is not None]
            if len(xs) >= 3 and len(set(ys)) > 1:
                t, pv = kendalltau(xs, ys)
                res[f"tau_{name}"] = round(float(t), 4)
                res[f"p_{name}"] = float(pv)
            else:
                res[f"tau_{name}"] = res[f"p_{name}"] = ""
        res["adj_inversions_send_cal"] = res["adj_pairs"] = ""
        if len(g2) >= 2:
            s = sorted(g2, key=lambda r: r["listing_pos"])
            inv_adj = sum(1 for i in range(1, len(s)) if s[i]["send_cal_ms"] < s[i - 1]["send_cal_ms"])
            res["adj_inversions_send_cal"] = inv_adj
            res["adj_pairs"] = len(s) - 1
        agree.append(res)

    # (b) per-signer skew
    by_u = defaultdict(list)
    for r in rows:
        if r["lag_ms"] is not None:
            by_u[r["signer"]].append(r)
    skew = []
    for u, rs in by_u.items():
        lags = [r["lag_ms"] for r in rs]
        fl = [r["lag_ms"] for r in rs if FRESH_LO <= r["lag_ms"] <= FRESH_HI]
        skew.append({"signer": u, "n": len(rs), "blocks": len({r["block"] for r in rs}), "n_fresh": len(fl),
                     "lag_med_ms": st.median(lags), "lag_min_ms": min(lags), "lag_max_ms": max(lags),
                     "fresh_p05_ms": q(fl, .05) if fl else "", "fresh_med_ms": st.median(fl) if fl else "",
                     "fresh_p95_ms": q(fl, .95) if fl else "", "offset_ms": off.get(u, ""),
                     "n_future": sum(1 for x in lags if x > FRESH_HI), "n_stale": sum(1 for x in lags if x < FRESH_LO),
                     "n_reused": sum(1 for r in rs if seen[(u, r["nonce"])] > 1),
                     "n_non_ms": sum(1 for r in rs if r["nonce"] < 10 ** 12)})
    skew.sort(key=lambda d: -d["n"])

    def wcsv(path, rs):
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rs[0].keys()))
            w.writeheader()
            w.writerows(rs)

    for h in sorted({r["block"] for r in rows}):
        wcsv(out / "blocks" / f"{h}.csv", [r for r in rows if r["block"] == h])
    wcsv(out / "inventory.csv", inv)
    wcsv(out / "nonce_order.csv", agree)
    wcsv(out / "signer_skew.csv", skew)
    summ = {"blocks": len(inv), "actions": len(rows), "signers": len(by_u), "signers_calibrated": len(off),
            "pooled_offset_ms": pooled,
            "split_half_offset_diff_ms": {"n": len(split), "median_abs": st.median([abs(x) for x in split]) if split else None,
                                          "p90_abs": q([abs(x) for x in split], .9) if split else None},
            "calib": dict(Counter(r["calib"] for r in rows)),
            "flags": dict(Counter(f for r in rows for f in r["flags"].split(";") if f))}
    tau = {}
    for c in "CAGO":
        for k, nk in (("tau_nonce", "n"), ("tau_fresh_nonce", "n_fresh"), ("tau_send_cal", "n_fresh")):
            xs = [(r[k], r[nk]) for r in agree if r["class"] == c and r[k] != ""]
            if xs:
                tau[f"{c}:{k}"] = {"groups": len(xs), "median": round(st.median(t for t, _ in xs), 3),
                                   "weighted": round(sum(t * n for t, n in xs) / sum(n for _, n in xs), 3),
                                   "pos": int(sum(t > 0 for t, _ in xs)), "neg": int(sum(t < 0 for t, _ in xs))}
        ia = [(r["adj_inversions_send_cal"], r["adj_pairs"]) for r in agree if r["class"] == c and r["adj_pairs"] != ""]
        tau[f"{c}:adj_inversion_rate_send_cal"] = round(sum(a for a, _ in ia) / max(1, sum(b for _, b in ia)), 3)
    summ["nonce_vs_listing"] = tau
    (out / "summary.json").write_text(json.dumps(summ, indent=1))
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()


def resolution(rows_by_group, key, bins=(0, 1, 5, 20, 50, 100, 250, 1000, 10 ** 9)):
    """Pairwise concordance of listing order with `key` order, by |delta key| bin (ties in key skipped)."""
    import bisect
    agg = defaultdict(lambda: [0, 0])
    for g in rows_by_group:
        s = [(r["listing_pos"], r[key]) for r in g if r[key] != ""]
        for i in range(len(s)):
            for j in range(i + 1, len(s)):
                d = s[j][1] - s[i][1]
                if d == 0:
                    continue
                b = bins[bisect.bisect_right(bins, abs(d)) - 1]
                agg[b][0] += (d > 0) == (s[j][0] > s[i][0])
                agg[b][1] += 1
    return {b: (c, n, round(c / n, 3)) for b, (c, n) in sorted(agg.items())}
