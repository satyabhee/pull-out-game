"""Gate 2b (docs/gate2b.md): what cancels-first is worth to cancelling makers, at scale.

run:   python -m analysis.gate2b run --out report/validate/gate2b --jobs 6 --hl-data ~/hl-data/mac3/hl_data <B3 flags>
score: python -m analysis.gate2b score report/validate/gate2b

One B3 replay (ON = listing order) per captured window. An observer finds conflicts: a cancel / modify that removes a
resting order o of its user, plus taker (GTC / IOC) items in the same P5 batch, opposite side, crossing o's price, from
another user. Each conflict is valued with the L21 maker-side mid-to-mid 10 s markout:
    value = saved size x px x max(0, -mo) / 1e4 USD
UPPER: saved = min(r, Q), P = 1. ESTIMATE: saved = clamp(Q - A, 0, r), times P_hat (SQD-timed share of taker-first
pairs). Window-cluster bootstrap. No significance gate.
"""
from __future__ import annotations

import bisect
import gzip
import hashlib
import json
import math
import sys
import time
from decimal import Decimal
from pathlib import Path

HORIZON_MS = 10_000
MID_MAX_WAIT_MS = 60_000          # same wait limit as gate 2
VOL_MS = 10_000
BOOT_REPS, BOOT_SEED = 10_000, 20261127
RANK = {"C": 0, "A": 1, "G": 2, "O": 3}
SQD_SEND_TIMES = (Path.home() / "book-replay-data/handoff/gate2/send_times_yuta95.csv",
                  Path.home() / "book-replay-data/handoff/gate2_ext/send_times_yuta95.csv")


# ------------------------------------------------------------------ P5 batches on raw explorer txs

def _tifs(orders) -> list:
    out = []
    for o in orders:
        t = (o or {}).get("t") or {}
        out.append(t["limit"].get("tif") if "limit" in t else "trigger")
    return out


def raw_class(a: dict) -> str:
    """Class of one explorer tx action, as data-streams analysis/gate2_input.action_class (Yuta's SQD batch column)."""
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


def raw_batches(txs: list) -> dict[str, int]:
    """tx hash (lowercase) -> batch number: a rank drop in listing order starts a new batch (P5)."""
    out, b, prev = {}, 0, None
    for tx in txs:
        r = RANK[raw_class(tx.get("action") or {})]
        if prev is not None and r < prev:
            b += 1
        out.setdefault(str(tx.get("hash", "")).lower(), b)
        prev = r
    return out


def load_raw_txs(window_dir: Path, h: int) -> list | None:
    p = Path(window_dir) / f"{h}.json.gz"
    if not p.exists():
        return None
    with gzip.open(p, "rt") as fh:
        d = json.load(fh)
    return d.get("blockDetails", d).get("txs") or []


# ------------------------------------------------------------------ book helpers (pure, tested)

def queue_ahead(book, oid: int) -> int:
    """Lots ahead of resting order `oid`: all size at better prices on its side plus the size ahead of it at its
    price (FIFO)."""
    from engine_py.types import Side
    node = book.index[oid]
    side, px = node.order.side, node.order.px
    ahead = 0
    for lv in book.levels(side):
        better = lv.px > px if side is Side.BUY else lv.px < px
        if better:
            ahead += lv.total
            continue
        if lv.px != px:
            break
        for n in lv:
            if n is node:
                return ahead
            ahead += n.remaining
    raise ValueError(f"order {oid} not found in its own level")


def crosses(taker_side, taker_px: int, maker_px: int) -> bool:
    from engine_py.types import Side
    return taker_px >= maker_px if taker_side is Side.BUY else taker_px <= maker_px


def taker_items(actions) -> list[dict]:
    """GTC / IOC order items (Place, or the new order of a Modify) of one block: the possible takers."""
    from engine_py.types import Modify, Place, Tif
    out = []
    for a in actions:
        p = a.new if isinstance(a, Modify) else a
        if isinstance(p, Place) and p.tif in (Tif.GTC, Tif.IOC):
            out.append({"hash": a.hash.lower(), "user": a.user.lower(), "asset": p.asset, "side": p.side,
                        "px": p.px, "sz": p.sz})
    return out


def saved_sizes(r: int, q: int, ahead: int) -> tuple[int, int]:
    """(UPPER, ESTIMATE) saved lots: min(r, Q) and clamp(Q - A, 0, r)."""
    return min(r, q), max(0, min(r, q - ahead))


# ------------------------------------------------------------------ mids, markout, volatility

class MidSeries:
    def __init__(self, ts: list[int], mids: list[Decimal]):
        self.ts, self.mids = ts, mids

    def before(self, t: int):
        i = bisect.bisect_left(self.ts, t) - 1
        return self.mids[i] if i >= 0 else None

    def at_or_after(self, t: int):
        i = bisect.bisect_left(self.ts, t)
        if i >= len(self.ts) or self.ts[i] > t + MID_MAX_WAIT_MS:
            return None
        return self.mids[i]

    def vol_bp(self, t: int) -> tuple[float | None, int]:
        """Trailing realised vol over [t - 10 s, t): sqrt(sum of squared log returns of consecutive mids), bp."""
        i0, i1 = bisect.bisect_left(self.ts, t - VOL_MS), bisect.bisect_left(self.ts, t)
        ms = [float(m) for m in self.mids[i0:i1]]
        if len(ms) < 2:
            return None, len(ms)
        return 1e4 * math.sqrt(sum(math.log(b / a) ** 2 for a, b in zip(ms, ms[1:]))), len(ms)


def maker_markout_bp(series: MidSeries, t_ms: int, maker_is_bid: bool):
    """L21: 1e4 x s' x (m(t + 10 s) - m(t-)) / m(t-), s' = +1 for a bid. None without both mids."""
    m0, m1 = series.before(t_ms), series.at_or_after(t_ms + HORIZON_MS)
    if m0 is None or m1 is None:
        return None
    return float(Decimal(10_000) * (1 if maker_is_bid else -1) * (m1 - m0) / m0)


def value_usd(saved_sz: Decimal, px: Decimal, mo_bp: float | None) -> float:
    if mo_bp is None:
        return 0.0
    return float(saved_sz * px) * max(0.0, -mo_bp) / 1e4


def load_mids(hl_data, coin: str, t0: int, t1: int) -> MidSeries:
    from engine_py.replay import iter_bbo
    from data.parse import COIN_TO_ASSET
    from engine_py.types import ASSETS
    spec = ASSETS[COIN_TO_ASSET[coin]]
    ts, mids = [], []
    for t, _a, b, k in iter_bbo(hl_data, coin, t0, t1):
        if b and k:
            ts.append(t)
            mids.append((Decimal(spec.ticks_to_px(b[0])) + Decimal(spec.ticks_to_px(k[0]))) / 2)
    return MidSeries(ts, mids)


# ------------------------------------------------------------------ P_hat

def p_hat(pairs: list[tuple[int, int]]) -> tuple[float | None, int]:
    """(share of pairs with taker send < cancel send, ties 0.5; n). pairs = (taker_ms, cancel_ms)."""
    if not pairs:
        return None, 0
    s = sum(1.0 if t < c else 0.5 if t == c else 0.0 for t, c in pairs)
    return s / len(pairs), len(pairs)


# ------------------------------------------------------------------ observer

def observer_for(window_dir: Path, warm_h: int, sqd_heights: frozenset):
    from analysis.validate_replay import ValidateObserver
    from engine_py.types import Cancel, CancelByCloid, Modify, Side

    class Gate2bObserver(ValidateObserver):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.conflicts, self.sqd_batches = [], {}
            self.stats = {"scored_blocks": 0, "first_scored_ms": None, "last_ms": None, "raw_missing": 0,
                          "hash_not_in_raw": 0, "cancels_with_takers": 0, "cancels_with_takers_removed_none": 0}
            self._h, self._batch, self._takers, self._snap = None, {}, {}, None

        def _new_block(self, block):
            self._h = block.height
            txs = load_raw_txs(window_dir, block.height)
            if txs is None:
                self.stats["raw_missing"] += 1
                txs = []
            self._batch = raw_batches(txs)
            if block.height in sqd_heights:
                self.sqd_batches[str(block.height)] = self._batch
            self._takers = {}
            for t in taker_items(block.actions):
                b = self._batch.get(t["hash"])
                if b is None:
                    self.stats["hash_not_in_raw"] += 1
                    continue
                self._takers.setdefault((b, t["asset"]), []).append(t)
            self.stats["scored_blocks"] += 1
            if self.stats["first_scored_ms"] is None:
                self.stats["first_scored_ms"] = block.time_ms
            self.stats["last_ms"] = block.time_ms

        def before_action(self, rp, block, a):
            super().before_action(rp, block, a)
            if block.height < warm_h:
                return
            if block.height != self._h:
                self._new_block(block)
            self._snap = None
            if not isinstance(a, (Cancel, CancelByCloid, Modify)):
                return
            b = self._batch.get(a.hash.lower())
            tk = [t for t in self._takers.get((b, a.asset), []) if t["user"] != a.user.lower()] if b is not None else []
            if not tk:
                return
            self.stats["cancels_with_takers"] += 1
            book = rp.engine.books[a.asset]
            snap = {}
            for oid in rp.engine.by_user.get(a.user, {}):
                node = book.index.get(oid)
                if node is None or node.order is None:
                    continue
                o = node.order
                cross = [t for t in tk if t["side"] is not o.side and crosses(t["side"], t["px"], o.px)]
                if cross:
                    snap[oid] = (o, node.remaining, queue_ahead(book, oid), cross)
            self._snap = (b, snap)

        def after_action(self, rp, block, a, fills, rejects, created):
            super().after_action(rp, block, a, fills, rejects, created)
            if self._snap is None:
                return
            b, snap = self._snap
            self._snap = None
            from engine_py.types import ASSETS
            book = rp.engine.books[a.asset]
            filled = {fl.maker_order for fl in fills}
            removed = [oid for oid in sorted(snap) if oid not in book.index and oid not in filled]
            if not removed:
                self.stats["cancels_with_takers_removed_none"] += 1
            spec = ASSETS[a.asset]
            for oid in removed:
                o, r, ahead, cross = snap[oid]
                q = sum(t["sz"] for t in cross)
                up, est = saved_sizes(r, q, ahead)
                self.conflicts.append(dict(
                    height=block.height, time_ms=block.time_ms, batch=b, coin=spec.name,
                    maker_user=a.user.lower(), cancel_hash=a.hash.lower(), maker_is_bid=o.side is Side.BUY,
                    px=str(spec.ticks_to_px(o.px)), r=str(spec.lots_to_sz(r)), ahead=str(spec.lots_to_sz(ahead)),
                    q=str(spec.lots_to_sz(q)), saved_up=str(spec.lots_to_sz(up)), saved_est=str(spec.lots_to_sz(est)),
                    takers=sorted({t["hash"] for t in cross})))

    return Gate2bObserver


# ------------------------------------------------------------------ one window

def run_window(job) -> dict:
    from analysis import validate_replay as vr
    from analysis.validate_chunks import _free_chunk_caches
    window, hl_data, kw, sqd_heights = job
    window = Path(window)
    t_start = time.time()
    out = {"window": window.name, "status": "ok"}
    try:
        meta = json.loads((window / "window.json").read_text())
        warm = int(meta.get("warmup_end_height") or meta["start_height"])
        holder = {}
        base = observer_for(window, warm, frozenset(sqd_heights))

        class Cls(base):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                holder["obs"] = self
        vr.run(str(window), hl_data, "listing", out_dir=None, quiet=True, observer_cls=Cls, **kw)
        o = holder["obs"]
        st = o.stats
        rows = o.conflicts
        if st["first_scored_ms"] is not None:
            t0, t1 = st["first_scored_ms"] - VOL_MS, st["last_ms"] + MID_MAX_WAIT_MS
            series = {c: load_mids(hl_data, c, t0, t1) for c in sorted({x["coin"] for x in rows})}
            for x in rows:
                s = series[x["coin"]]
                x["mo_bp"] = maker_markout_bp(s, x["time_ms"], x["maker_is_bid"])
                x["vol_bp"], x["vol_n"] = s.vol_bp(x["time_ms"])
                x["v_up"] = value_usd(Decimal(x["saved_up"]), Decimal(x["px"]), x["mo_bp"])
                x["v_est"] = value_usd(Decimal(x["saved_est"]), Decimal(x["px"]), x["mo_bp"])
        out.update(stats=st, conflicts=rows, sqd_batches=o.sqd_batches)
    except Exception as e:                           # recorded and reported, never dropped
        out.update(status="failed", error=f"{type(e).__name__}: {e}")
    _free_chunk_caches()
    out["secs"] = round(time.time() - t_start, 1)
    return out


def window_order(names: list[str]) -> list[str]:
    """Fixed pseudo-random run order (sha256 of the name), so a partial run is a random subset (gate2b.md 1)."""
    return sorted(names, key=lambda n: hashlib.sha256(n.encode()).hexdigest())


def load_send_times_all() -> dict:
    """(height, hash) -> (send_ms, batch) from both SQD yuta95 files."""
    from engine_py.ordering import load_send_times
    out = {}
    for p in SQD_SEND_TIMES:
        out.update(load_send_times(p))
    return out


def run_main(argv):
    import argparse
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
    from analysis import validate_replay as vr
    from data.catalog import chunk_gap
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--windows", default=str(Path.home() / "hl-data/mac3/windows"))
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--stop-at", default="13:15", help="UTC HH:MM: no new window starts after this (partial run)")
    ap.add_argument("--limit", type=int, default=0)
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    kw = vr.replay_kwargs(a)
    out = Path(a.out)
    (out / "windows").mkdir(parents=True, exist_ok=True)
    root = Path(a.windows)
    all_w = sorted(p.name for p in root.iterdir() if p.is_dir() and "-" in p.name)
    gap = [w for w in all_w if chunk_gap(root / w) is not None]
    todo = window_order([w for w in all_w if w not in set(gap)])
    if a.limit:
        todo = todo[:a.limit]
    sqd = sorted({h for h, _ in load_send_times_all()})
    meta = {"argv": argv, "windows_total": len(all_w), "windows_gap_excluded": gap, "windows_todo": len(todo),
            "sqd_heights": len(sqd), "send_times": [str(p) for p in SQD_SEND_TIMES],
            "send_times_sha256": [hashlib.sha256(p.read_bytes()).hexdigest() for p in SQD_SEND_TIMES]}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    hh, mm = map(int, a.stop_at.split(":"))
    stop = lambda: (time.gmtime().tm_hour, time.gmtime().tm_min) >= (hh, mm)
    pending = [w for w in todo if not (out / "windows" / f"{w}.json").exists()]
    done = len(todo) - len(pending)
    with ProcessPoolExecutor(a.jobs) as ex:
        live, it = set(), iter(pending)
        while True:
            while len(live) < 2 * a.jobs and not stop():
                w = next(it, None)
                if w is None:
                    break
                lo, hi = map(int, w.split("-"))
                hs = [h for h in sqd[bisect.bisect_left(sqd, lo):bisect.bisect_right(sqd, hi)]]
                live.add(ex.submit(run_window, (root / w, a.hl_data, kw, hs)))
            if not live:
                break
            fin, live = wait(live, return_when=FIRST_COMPLETED)
            for f in fin:
                r = f.result()
                (out / "windows" / f"{r['window']}.json").write_text(json.dumps(r, default=str))
                done += 1
                if done % 50 == 0:
                    print(f"{time.strftime('%H:%MZ', time.gmtime())} {done}/{len(todo)}", flush=True)
    print(f"gate2b run: {done}/{len(todo)} windows written -> {out} (stopped early: {done < len(todo)})")


# ------------------------------------------------------------------ score

def _quantile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def batch_agreement(sqd_batches: dict, send: dict) -> tuple[int, int]:
    """(agreeing tx pairs, tx pairs): same-batch relation, explorer P5 cut vs Yuta's SQD batch, per block."""
    agree = tot = 0
    for h, mine in sqd_batches.items():
        rows = [(mine[x], send[(int(h), x)][1]) for x in sorted(mine) if (int(h), x) in send
                and send[(int(h), x)][1] is not None]
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                tot += 1
                agree += (rows[i][0] == rows[j][0]) == (rows[i][1] == rows[j][1])
    return agree, tot


def score_main(argv):
    import numpy as np
    out = Path(argv[0])
    meta = json.loads((out / "meta.json").read_text())
    res = [json.loads(p.read_text()) for p in sorted((out / "windows").glob("*.json"))]
    ok = [r for r in res if r["status"] == "ok"]
    failed = [r for r in res if r["status"] != "ok"]
    send = load_send_times_all()
    # per-conflict SQD pairs, batch validation
    same_sqd = pairs_tot = 0
    for r in ok:
        for x in r["conflicts"]:
            cb = send.get((x["height"], x["cancel_hash"]))
            x["pairs"] = []
            for t in x["takers"]:
                tb = send.get((x["height"], t))
                if cb is not None and tb is not None:
                    x["pairs"].append((tb[0], cb[0]))
                    if cb[1] is not None and tb[1] is not None:
                        pairs_tot += 1
                        same_sqd += cb[1] == tb[1]
    agree, tot_pairs = batch_agreement({h: b for r in ok for h, b in r.get("sqd_batches", {}).items()}, send)
    sqd_conf = [x for r in ok for x in r["conflicts"] if str(x["height"]) in r.get("sqd_batches", {})]
    sqd_pairs_untimed = sum(len(x["takers"]) - len(x["pairs"]) for x in sqd_conf)
    allpairs = [p for x in sqd_conf for p in x["pairs"]]
    P, n_p = p_hat(allpairs)
    # volatility deciles per coin
    conf = [x for r in ok for x in r["conflicts"]]
    cuts = {}
    for c in sorted({x["coin"] for x in conf}):
        v = [x["vol_bp"] for x in conf if x["coin"] == c and x["vol_bp"] is not None]
        cuts[c] = ([_quantile(v, k / 10) for k in range(1, 10)], _quantile(v, 0.99)) if v else ([], math.inf)
    for x in conf:
        if x["vol_bp"] is None:
            x["dec"], x["top1"] = 10, False                      # row "no vol"
        else:
            x["dec"] = bisect.bisect_right(cuts[x["coin"]][0], x["vol_bp"])
            x["top1"] = x["vol_bp"] >= cuts[x["coin"]][1]
    # per-window arrays: [up, est, n_conf, pairs_first, pairs_n, blocks, secs, up_dec0..10, est_dec0..10, up_top1, est_top1]
    K = 11
    rows = []
    for r in ok:
        st = r["stats"]
        secs = ((st["last_ms"] - st["first_scored_ms"]) / 1000) if st["first_scored_ms"] is not None else 0.0
        v = np.zeros(7 + 2 * K + 2)
        for x in r["conflicts"]:
            v[0] += x["v_up"]
            v[1] += x["v_est"]
            v[2] += 1
            for t, c in x["pairs"]:
                v[3] += 1.0 if t < c else 0.5 if t == c else 0.0
                v[4] += 1
            v[7 + x["dec"]] += x["v_up"]
            v[7 + K + x["dec"]] += x["v_est"]
            if x["top1"]:
                v[7 + 2 * K] += x["v_up"]
                v[7 + 2 * K + 1] += x["v_est"]
        v[5], v[6] = st["scored_blocks"], secs
        rows.append(v)
    M = np.array(rows)

    def stats(T):                                    # T: (..., ncol) totals -> dict of statistics
        Ph = T[..., 3] / np.where(T[..., 4] > 0, T[..., 4], np.nan)
        up, est = T[..., 0], Ph * T[..., 1]
        d = {"W_up": up, "W_est": est, "P_hat": Ph,
             "up_per_day": up / T[..., 6] * 86400, "est_per_day": est / T[..., 6] * 86400,
             "up_per_block": up / T[..., 5], "est_per_block": est / T[..., 5],
             "up_share_topdec": T[..., 7 + 9] / up, "est_share_topdec": T[..., 7 + K + 9] / T[..., 1],
             "up_share_top1": T[..., 7 + 2 * K] / up, "est_share_top1": T[..., 7 + 2 * K + 1] / T[..., 1]}
        for k in range(K):
            d[f"up_dec{k}"] = T[..., 7 + k]
            d[f"est_dec{k}"] = Ph * T[..., 7 + K + k]
        return d
    point = stats(M.sum(0))
    rng = np.random.default_rng(BOOT_SEED)
    n = len(M)
    sims = {k: [] for k in point}
    for _ in range(BOOT_REPS // 250):
        idx = rng.integers(0, n, size=(250, n))
        T = M[idx].sum(1)
        for k, v in stats(T).items():
            sims[k].append(v)
    ci = {k: (float(np.nanpercentile(np.concatenate(v), 2.5)), float(np.nanpercentile(np.concatenate(v), 97.5)))
          for k, v in sims.items()}
    f = lambda k, u="": f"{float(point[k]):,.2f}{u}  95% CI [{ci[k][0]:,.2f}, {ci[k][1]:,.2f}]"
    blocks, secs = M[:, 5].sum(), M[:, 6].sum()
    no_mid = sum(1 for x in conf if x["mo_bp"] is None)
    lines = [
        f"gate2b score: {out}",
        f"windows: total {meta['windows_total']}, gap-excluded {len(meta['windows_gap_excluded'])}, to run "
        f"{meta['windows_todo']}, written {len(res)}, ok {len(ok)}, failed {len(failed)}"
        + (f"  PARTIAL: {len(res)} of {meta['windows_todo']} windows" if len(res) < meta["windows_todo"] else "")
        + ("  PARTIAL: >5% failed" if failed and len(failed) > 0.05 * max(1, len(res)) else ""),
        f"scored blocks {int(blocks):,}; scored time {secs / 3600:,.2f} h ({secs / 86400:.3f} days)",
        f"conflicts {len(conf):,} (no mid {no_mid}); cancels with same-batch crossing takers "
        f"{sum(r['stats']['cancels_with_takers'] for r in ok):,}, of which removed no crossing order of the user (the cancel hit another order, or missed) "
        f"{sum(r['stats']['cancels_with_takers_removed_none'] for r in ok):,}; raw blocks missing "
        f"{sum(r['stats']['raw_missing'] for r in ok)}; parsed hashes not in raw {sum(r['stats']['hash_not_in_raw'] for r in ok)}",
        f"batch validation (SQD heights {sum(len(r.get('sqd_batches', {})) for r in ok)}): tx-pair same-batch agreement "
        f"{agree:,}/{tot_pairs:,} = {agree / tot_pairs if tot_pairs else float('nan'):.4f}; conflict pairs also in one SQD batch "
        f"{same_sqd}/{pairs_tot} = {same_sqd / pairs_tot if pairs_tot else float('nan'):.4f}",
        f"P_hat (taker sent first, ties 0.5): {f('P_hat')}  on {n_p} timed pairs from {len(sqd_conf)} SQD-height "
        f"conflicts ({sqd_pairs_untimed} pairs untimed, excluded)",
        "",
        f"UPPER    W_up  {f('W_up', ' USD')}   per day {f('up_per_day', ' USD')}   per block {f('up_per_block', ' USD')}",
        f"ESTIMATE W_est {f('W_est', ' USD')}   per day {f('est_per_day', ' USD')}   per block {f('est_per_block', ' USD')}",
        f"share of W in top vol decile: UPPER {f('up_share_topdec')}; ESTIMATE {f('est_share_topdec')}",
        f"share of W in top 1% vol:     UPPER {f('up_share_top1')}; ESTIMATE {f('est_share_top1')}",
        "",
        "by trailing 10 s vol decile (per coin; 10 = fewer than 2 mids): n | W_up | W_est",
    ]
    for k in range(K):
        nk = sum(1 for x in conf if x["dec"] == k)
        lines.append(f"  dec {k:>2}: n {nk:>7,} | {f(f'up_dec{k}')} | {f(f'est_dec{k}')}")
    lines.append("vol decile cuts (bp): " + "; ".join(f"{c} {[round(v, 2) for v in cuts[c][0]]} top1% >= "
                                                      f"{cuts[c][1]:.2f}" for c in sorted(cuts)))
    if failed:
        lines.append(f"failed windows: {[(r['window'], r['error'][:80]) for r in failed]}")
    (out / "score.txt").write_text("\n".join(lines) + "\n")
    with open(out / "conflicts.jsonl", "w") as fh:
        for r in ok:
            for x in r["conflicts"]:
                fh.write(json.dumps({"window": r["window"], **x}, default=str) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run":
        run_main(sys.argv[2:])
    elif cmd == "score":
        score_main(sys.argv[2:])
    else:
        sys.exit(__doc__)
