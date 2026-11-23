"""Fallback 3b / A3: ioc_reject classifier (rule in docs/scoreboard.md, written before this ran).

Trains on the de-duplicated order histories (windows/*/hist_orders.json): label 1 = the chain filled nothing.
Time split at SPLIT_MS (dev20 / final240 first block): fit on the first 80% of the training period by time,
validation (calibration, cutoff) on the last 20%, test after the split (report only). Features: the user's no-fill
rate in the fit part (Laplace), coin, side, log10 notional. Writes the model as exact per-(user, coin, side)
notional thresholds (Decimal strings), so the replay decides without floats.

usage: python -m analysis.a3_ioc_model ~/hl-data/mac3/windows --out report/validate/a3/model.json
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from decimal import Decimal
from glob import glob
from pathlib import Path

import numpy as np
import statsmodels.api as sm

SPLIT_MS = None            # set from dev20's first block (Nov 19 15:22 UTC); see main()
COINS = ("BTC", "ETH")


def load(windows: Path) -> list[dict]:
    seen: dict[int, dict] = {}
    for f in sorted(glob(str(windows / "*" / "hist_orders.json"))):
        for r in json.load(open(f)):
            ans = r.get("answer")
            if not isinstance(ans, list):
                continue
            for x in ans:
                if not isinstance(x, dict):
                    continue
                o = x["order"]
                if o.get("coin") not in COINS or o.get("tif") not in ("Ioc", "FrontendMarket") or o["oid"] in seen:
                    continue
                seen[o["oid"]] = {"user": r["user"].lower(), "coin": o["coin"], "buy": o["side"] == "B",
                                  "ts": int(o["timestamp"]), "ntl": Decimal(o["limitPx"]) * Decimal(o["origSz"]),
                                  "y": int(x["status"] != "filled")}
    return sorted(seen.values(), key=lambda d: (d["ts"], d["user"]))


def auc(y, p) -> float:
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    n1, n0 = pos.sum(), (~pos).sum()
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else float("nan")


def logit(q: float) -> float:
    return math.log(q / (1 - q))


def design(rows, rates, glob_rate):
    X = np.array([[logit(rates.get(r["user"], glob_rate)), r["coin"] == "ETH", r["buy"],
                   math.log10(float(r["ntl"]))] for r in rows], float)
    return sm.add_constant(X, has_constant="add")


def cost_cutoff(y, p, R: float, fs_cap: float):
    """A3 / I12: skip iff p >= c0 = R/(R+1); if more than fs_cap of the filled IOCs (y == 0) would be skipped, raise
    the cutoff to the lowest p that meets the cap. Not used (None) unless no-fill skipped - R x filled skipped > 0."""
    y = np.asarray(y)
    p = np.asarray(p)
    c0 = R / (R + 1)
    n_filled = int((y == 0).sum())
    cap = math.floor(fs_cap * n_filled)

    def stats(c):
        m = p >= c
        return int(((y == 1) & m).sum()), int(((y == 0) & m).sum())

    c = c0
    tn, fs = stats(c)
    if fs > cap:                                             # candidates: the distinct p values above c0
        for v in sorted(set(p[p > c0].tolist())):
            tn, fs = stats(v)
            if fs <= cap:
                c = v
                break
        else:
            c, (tn, fs) = float("inf"), (0, 0)
    net = tn - R * fs
    info = {"R": R, "c0": c0, "fs_cap": fs_cap, "cap_count": cap, "cutoff": c if math.isfinite(c) else None,
            "val_skipped_nofill": tn, "val_skipped_filled": fs, "val_filled_total": n_filled, "val_net": net,
            "used": bool(math.isfinite(c) and net > 0)}
    return (float(c) if info["used"] else None), info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows")
    ap.add_argument("--split-ms", type=int, default=1795101767239, help="dev20 / final240 first block (Nov 19 15:22:47 UTC)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cost-ratio", type=float, default=None,
                    help="A3 / I12: Bayes cutoff R/(R+1) with a false-skip cap (default: the 3b precision cutoff)")
    ap.add_argument("--fs-cap", type=float, default=0.05, help="max share of validation's filled IOCs skipped")
    a = ap.parse_args(argv)
    rows = [r for r in load(Path(a.windows)) if r["ntl"] > 0]
    train = [r for r in rows if r["ts"] < a.split_ms]
    test = [r for r in rows if r["ts"] >= a.split_ms]
    cut = train[int(len(train) * 0.8)]["ts"]
    fit = [r for r in train if r["ts"] < cut]
    val = [r for r in train if r["ts"] >= cut]
    assert all(r["ts"] < a.split_ms for r in fit + val)                      # leakage test (i)
    n, k = {}, {}
    for r in fit:                                                            # leakage test (iii): fit part only
        n[r["user"]] = n.get(r["user"], 0) + 1
        k[r["user"]] = k.get(r["user"], 0) + r["y"]
    rates = {u: (k[u] + 1) / (n[u] + 2) for u in n}
    glob_rate = (sum(k.values()) + 1) / (sum(n.values()) + 2)
    yf = np.array([r["y"] for r in fit])
    res = sm.Logit(yf, design(fit, rates, glob_rate)).fit(disp=0)
    yv = np.array([r["y"] for r in val])
    pv = res.predict(design(val, rates, glob_rate))
    rng = random.Random(7)                                                   # leakage test (ii)
    yperm = yf.copy()
    idx = list(range(len(yperm)))
    rng.shuffle(idx)
    yperm = yperm[idx]
    pn, pk = {}, {}                                                          # user rates from the PERMUTED labels
    for r, yy in zip(fit, yperm):
        pn[r["user"]] = pn.get(r["user"], 0) + 1
        pk[r["user"]] = pk.get(r["user"], 0) + int(yy)
    prates = {u: (pk[u] + 1) / (pn[u] + 2) for u in pn}
    try:
        perm_auc = auc(yv, sm.Logit(yperm, design(fit, prates, glob_rate)).fit(disp=0)
                       .predict(design(val, prates, glob_rate)))
    except Exception as e:                                                   # e.g. separation: report it
        perm_auc = f"fit failed: {type(e).__name__}"
    bins = []
    for lo in np.linspace(0, 0.9, 10):
        m = (pv >= lo) & (pv < lo + 0.1 + (1e-9 if lo >= 0.9 else 0))
        if m.any():
            bins.append({"bin": f"{lo:.1f}", "n": int(m.sum()), "mean_p": float(pv[m].mean()), "rate": float(yv[m].mean())})
    order = np.argsort(-pv, kind="mergesort")
    cutoff, best_recall = None, 0.0
    hits = np.cumsum(yv[order])
    for i in range(len(order)):
        prec = hits[i] / (i + 1)
        if prec >= 0.99 and (i + 1 == len(order) or pv[order[i + 1]] < pv[order[i]]):
            cutoff, best_recall = float(pv[order[i]]), float(hits[i] / max(1, yv.sum()))
    cost = None
    if a.cost_ratio is not None:                             # A3 / I12 rule (docs/scoreboard.md), replaces the 3b cutoff
        cutoff, cost = cost_cutoff(yv, pv, a.cost_ratio, a.fs_cap)
        best_recall = cost["val_skipped_nofill"] / max(1, int(yv.sum())) if cutoff is not None else 0.0
    out = {"split_ms": a.split_ms, "fit_until_ms": cut, "n": {"fit": len(fit), "val": len(val), "test": len(test)},
           "users_fit": len(rates), "global_rate": glob_rate, "coef": [float(c) for c in res.params],
           "val_auc": auc(yv, pv), "perm_auc": perm_auc, "reliability": bins, "cutoff": cutoff,
           "val_recall_at_cutoff": best_recall, "val_base_rate": float(yv.mean())}
    if cost is not None:
        out["cost_rule"] = cost
    if test:
        yt = np.array([r["y"] for r in test])
        pt = res.predict(design(test, rates, glob_rate))
        out["test_auc"] = auc(yt, pt)
        if cutoff is not None:
            m = pt >= cutoff
            out["test_at_cutoff"] = {"skipped": int(m.sum()), "precision": float(yt[m].mean()) if m.any() else None,
                                     "filled_skipped": int((yt[m] == 0).sum()), "filled_total": int((yt == 0).sum()),
                                     "recall": float(yt[m].sum() / max(1, yt.sum())), "base_rate": float(yt.mean())}
    if cutoff is not None:                                   # exact thresholds: skip iff notional (>= | <=) T
        b0, b1, b2, b3, b4 = out["coef"]
        L = logit(min(max(cutoff, 1e-12), 1 - 1e-12))
        thr = {}
        for u in list(rates) + ["*"]:
            ru = rates.get(u, glob_rate)
            for coin in COINS:
                for buy in (True, False):
                    rest = b0 + b1 * logit(ru) + b2 * (coin == "ETH") + b3 * buy
                    if b4 == 0:
                        t = ("all" if rest >= L else "none", None)
                    else:
                        x = (L - rest) / b4                          # log10 notional at the boundary
                        x = max(min(x, 30.0), -30.0)
                        t = ("ge" if b4 > 0 else "le", str(Decimal(repr(10 ** x)).quantize(Decimal("0.000001"))))
                    thr[f"{u}|{coin}|{'B' if buy else 'A'}"] = t
        out["thresholds"] = thr
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(a.out, "w"), indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "thresholds"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
