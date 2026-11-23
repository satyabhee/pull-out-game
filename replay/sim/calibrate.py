"""M1 calibration: simulated vs real statistics, with the gap for each (MAHITO_PLAN M1 calibrate.py).

Every statistic is computed with the SAME code on both sides (the simulator writes the real formats):
- touch changes: at the best bid/ask, between consecutive changed bbo records at the same price, |change in order
  count| and |change in size| / size (median and 90th percentile);
- trades per block (printed fills / blocks, per chunk, then the mean over chunks);
- post-only margin rejections: perpMarginRejected / post-only orders (real: BTC+ETH in the de-duplicated order
  histories; sim: the truth log);
- tooManyOpenOrdersRejected / all orders (same sources);
- engine-only trades: share of scored txs where only the replay engine traded (validate_chunks txs.csv).

usage: python -m replay.sim.calibrate --real ~/hl-data/mac3 dev20 --sim ~/hl-data/sim/m1 sim200 \
         --real-txs report/validate/m0_blockend20/txs.csv --sim-txs <validate out>/txs.csv --out docs/m1_calibration.md
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics as st
import sys
from collections import Counter
from pathlib import Path

from engine_py.replay import iter_bbo
from data.parse import _hour_files, _records, iter_trades
from replay.sim.truth import read_truth


def _q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else float("nan")


def chunks_of(root: Path, set_name: str) -> list[Path]:
    return sorted(p for p in (root / set_name).iterdir() if (p / "window.json").exists())


def book_depth_n(root: Path, coin: str, t0: int, t1: int) -> list[float]:
    """Mean orders per level over the 20 best levels of each side, for every full l2Book snapshot in [t0, t1]
    (read raw: data.parse.iter_books drops the order counts)."""
    out = []
    for path in _hour_files(root / "hl_data", f"book_{coin}", t0, t1):
        for rec in _records(path, t0, t1):
            d = rec["msg"]["data"]
            if t0 <= d["time"] <= t1:
                ns = [lv["n"] for side in d["levels"] for lv in side[:20]]
                if ns:
                    out.append(sum(ns) / len(ns))
    return out


def feed_and_trades(root: Path, set_name: str) -> dict:
    dn, dsz, per_block, touch_n, depth_n, conc = [], [], [], [], [], []
    for c in chunks_of(root, set_name):
        m = json.loads((c / "window.json").read_text())
        t0, t1 = m["first_block_time"], m["last_block_time"]
        nblocks = sum(1 for f in c.iterdir() if f.name.endswith(".json.gz"))
        for coin in ("BTC", "ETH"):
            prev = None
            depth_n += book_depth_n(root, coin, t0 - 30_000, t1)       # the snapshot before the chunk counts too
            for _, _, bid, ask in iter_bbo(root / "hl_data", coin, t0, t1):
                cur = (bid, ask)
                touch_n += [x[2] for x in cur if x]
                if prev is not None and cur != prev:
                    for a, b in zip(prev, cur):
                        if a and b and a[0] == b[0] and (a[1], a[2]) != (b[1], b[2]):
                            dn.append(abs(b[2] - a[2]))
                            dsz.append(abs(b[1] - a[1]) / max(a[1], 1))
                prev = cur
        makers = Counter(tr.maker for tr in iter_trades(root / "hl_data", ["BTC", "ETH"], t0, t1))
        n_tr = sum(makers.values())
        per_block.append(n_tr / max(nblocks, 1))
        if n_tr:
            conc.append(sum(n for _, n in makers.most_common(5)) / n_tr)
    return {"orders at the touch (median n)": _q(touch_n, .5), "orders per level, 20-level book (median of means)":
            _q(depth_n, .5), "touch |dn| median": _q(dn, .5), "touch |dn| p90": _q(dn, .9),
            "touch |dsz|/sz median": _q(dsz, .5), "touch |dsz|/sz p90": _q(dsz, .9),
            "trades per block (mean of chunks)": st.mean(per_block) if per_block else float("nan"),
            "maker concentration (top-5 share of fills)": st.mean(conc) if conc else float("nan"),
            "chunks": len(per_block)}


def real_rejections(histories: Path) -> dict:
    alo = alo_margin = n = too_many = 0
    with gzip.open(histories, "rt") as f:
        for line in f:
            r = json.loads(line)
            o = r.get("order") or {}
            if o.get("coin") not in ("BTC", "ETH"):
                continue
            n += 1
            too_many += r.get("status") == "tooManyOpenOrdersRejected"
            if o.get("tif") == "Alo":
                alo += 1
                alo_margin += r.get("status") == "perpMarginRejected"
    return {"post-only margin rejection rate": alo_margin / alo, "tooManyOpenOrders rate": too_many / n}


def sim_rejections(root: Path, set_name: str) -> dict:
    alo = alo_margin = n = too_many = 0
    for c in chunks_of(root, set_name):
        truth = read_truth(root / "truth" / f"{c.name}.parquet")
        tif = {r["oid"]: r["tif"] for r in truth if r["kind"] == "placed"}
        n += len(tif)
        alo += sum(1 for t in tif.values() if t == "Alo")
        for r in truth:
            if r["kind"] == "order_status":
                too_many += r["status"] == "tooManyOpenOrdersRejected"
                alo_margin += r["status"] == "perpMarginRejected" and tif.get(r["oid"]) == "Alo"
    return {"post-only margin rejection rate": alo_margin / alo, "tooManyOpenOrders rate": too_many / n}


def engine_only(txs_csv: Path) -> dict:
    rows = [r for r in csv.DictReader(open(txs_csv)) if r["warmup"] == "0" and r["status"] in ("match", "mismatch")]
    return {"engine-only trade share (scored txs)": sum(r["engine_only"] == "1" for r in rows) / max(len(rows), 1)}


def table(real: dict, sim: dict) -> str:
    lines = ["| statistic | real (dev20) | simulated | gap (sim - real) |", "|---|---|---|---|"]
    for k in real:
        r, s = real[k], sim.get(k, float("nan"))
        fmt = (lambda v: f"{v:.4f}") if isinstance(r, float) else str
        gap = s - r if isinstance(r, (int, float)) and isinstance(s, (int, float)) else ""
        lines.append(f"| {k} | {fmt(r)} | {fmt(s)} | {gap if gap == '' else f'{gap:+.4f}'} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--real", nargs=2, required=True, metavar=("ROOT", "SET"))
    ap.add_argument("--sim", nargs=2, required=True, metavar=("ROOT", "SET"))
    ap.add_argument("--real-histories", default=str(Path.home() / "hl-data/mac3/derived/order_histories.jsonl.gz"))
    ap.add_argument("--real-txs", default=None)
    ap.add_argument("--sim-txs", default=None, help="omit to skip the engine-only share (needs a replay run)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    rroot, rset = Path(a.real[0]).expanduser(), a.real[1]
    sroot, sset = Path(a.sim[0]).expanduser(), a.sim[1]
    real = {**feed_and_trades(rroot, rset), **real_rejections(Path(a.real_histories).expanduser())}
    sim = {**feed_and_trades(sroot, sset), **sim_rejections(sroot, sset)}
    if a.real_txs and a.sim_txs:
        real.update(engine_only(Path(a.real_txs)))
        sim.update(engine_only(Path(a.sim_txs)))
    out = table(real, sim)
    print(out)
    if a.out:
        Path(a.out).write_text(f"# M1 calibration: simulated vs real\n\nGenerated by `python -m replay.sim.calibrate "
                               f"{' '.join(argv if argv is not None else sys.argv[1:])}`.\n\n{out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
