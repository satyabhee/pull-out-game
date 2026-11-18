"""G1 deep dive H9b: trades carried by non-book txs (SetGlobalAction = validator oracle update, which fires
triggers / liquidations), zero-hash outside fills, and dropped trigger orders. Measurement only.

    python -m analysis.g1_globalaction <window> --hl-data <dir> [--out DIR] [--reuse]

Steps (each writes into --out, default handoff/globalaction):
  characterise  no replay. events.csv: one row per (tx, coin) of every real BTC/ETH trade whose hash is a
                window tx that decodes to no book action: block position, fills, notional, side, takers,
                prices vs the estimated touch before the block (RealTouch: snapshots + earlier trades), the
                SetGlobalAction price move since the previous one, and whether the takers had trigger
                orders in the window that this move crossed. zero.json: the same questions for zero-hash
                trades, plus non-market trigger orders whose owner later made at their limit price.
  runs          validate_replay runs, base config (--alo-gate touch --reseed --nearest-ratio 3) +:
                  base         (nothing)
                  other-end    --outside-other-txs                         (end of block, tid order)
                  other-start  --outside-other-txs --other-position start
                  other-index  --outside-other-txs --other-position index  (at the tx's own position)
                  zero-start   --outside-position start                    (zero-hash fills at block start)
                One at a time (~3 min each). An instrumented observer (CSV outputs unchanged) also records,
                per outside fill, how much found the named maker / a placeholder / nothing, and checks every
                dropped trigger-modify: does its target rest in our book at that moment?
  report        local.csv: missed / spurious notional in blocks h..h+N after each event, per run, vs the
                window's average per-block rate; scoring.csv: denominators if these trades were scored.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from analysis import validate_replay as vr
from data.parse import _block_files, iter_books, iter_trades, read_block
from engine_py.matching import PLACEHOLDER
from engine_py.replay import COINS, TOUCH_LOOKBACK_MS, RealTouch, iter_bbo
from engine_py.types import ASSETS, OutsideFill, ZERO_HASH

BASE = dict(alo_gate="touch", reseed=True, nearest_ratio=3)
BASE_FLAGS = "--alo-gate touch --reseed --nearest-ratio 3"
RUNS = {                               # name -> (validate_replay.run kwargs on top of BASE, CLI flags)
    "base": ({}, ""),
    "other-end": (dict(outside_other_txs=True), "--outside-other-txs"),
    "other-start": (dict(outside_other_txs=True, other_position="start"),
                    "--outside-other-txs --other-position start"),
    "other-index": (dict(outside_other_txs=True, other_position="index"),
                    "--outside-other-txs --other-position index"),
    "zero-start": (dict(outside_position="start"), "--outside-position start"),
}
HORIZONS = (0, 1, 5, 10, 25, 50)       # blocks after the event (0 = the event's own block)
BIG_ZERO_USD = 10_000                  # a "big" zero-hash sweep: >= this much notional in one block and coin
MICRO = 1_000_000                      # ticks x lots = micro-USD (validate_replay.notional)
BOOK_KINDS = {"order", "cancel", "cancelByCloid", "modify", "batchModify", "scheduleCancel"}


def usd(micro: int) -> int:
    return micro // MICRO


# ------------------------------------------------------------------ small helpers (tested)

def trigger_px(t: dict) -> Decimal:
    """triggerPx of an order item's `t` as an exact Decimal (comparisons only; never hashed)."""
    return Decimal(t["trigger"]["triggerPx"])


def crossed(prev, new, level) -> bool:
    """A price path prev -> new crosses `level` (inclusive)."""
    return min(prev, new) <= level <= max(prev, new)


def bps(prev: Decimal, new: Decimal) -> str:
    return str(((new - prev) / prev * 10_000).quantize(Decimal("0.01")))


def index_rows(rows) -> dict:
    """(block, coin) -> [missed, spurious] notional from validate_replay rows."""
    idx: dict = defaultdict(lambda: [0, 0])
    for r in rows:
        real, eng, m = int(r["real_notional"]), int(r["engine_notional"]), int(r["matched_notional"])
        cell = idx[(int(r["block"]), r["coin"])]
        cell[0] += real - m
        cell[1] += eng - m
    return idx


def local_cells(events, n) -> set:
    """(block, coin) cells in blocks h..h+n of each (h, coin|None) event, each counted once."""
    cells = set()
    for h, coin in events:
        for b in range(h, h + n + 1):
            for c in ([coin] if coin else COINS):
                cells.add((b, c))
    return cells


def local_error(idx, events, n) -> tuple[int, int]:
    cells = local_cells(events, n)
    return (sum(idx[c][0] for c in cells if c in idx), sum(idx[c][1] for c in cells if c in idx))


# ------------------------------------------------------------------ characterise (no replay)

def _scan(window):
    """Raw pass over the window: blocks (height, time, n txs), SetGlobalAction prices, trigger items,
    dropped trigger-modifies, txs that decode to no book action."""
    blocks, sga, triggers, dropped, nonbook = [], [], defaultdict(list), [], {}
    for height, path in _block_files(Path(window)):
        b = read_block(path)
        blocks.append((height, b["blockTime"], b["numTxs"]))
        for i, tx in enumerate(b["txs"]):
            act, kind = tx["action"], tx["action"].get("type")
            if kind == "SetGlobalAction":
                sga.append((height, i, {a: tuple(Decimal(x) for x in act["pxs"][a]) for a in ASSETS
                                         if a < len(act["pxs"]) and None not in act["pxs"][a]}))
            if kind not in BOOK_KINDS:
                nonbook[tx["hash"]] = (height, i, b["numTxs"], b["blockTime"], kind)
                continue
            if tx["error"] is not None:
                continue
            user = tx["user"].lower()
            items = (act["orders"] if kind == "order" else [act] if kind == "modify"
                     else act["modifies"] if kind == "batchModify" else [])
            for k, it in enumerate(items):
                o = it if kind == "order" else it["order"]
                if o["a"] not in ASSETS or "trigger" not in o["t"]:
                    continue
                trg = o["t"]["trigger"]
                triggers[(user, o["a"])].append(dict(
                    height=height, tx_index=i, buy=o["b"], trigger=trigger_px(o["t"]),
                    market=bool(trg.get("isMarket")), tpsl=trg.get("tpsl"), limit=Decimal(o["p"]),
                    cloid=o.get("c"), modify=kind != "order", time=b["blockTime"]))
                if kind != "order":
                    tgt = it["oid"]
                    dropped.append(dict(height=height, tx_index=i, item=k, user=user, asset=o["a"],
                                        target=tgt if isinstance(tgt, str) else int(tgt)))
    return blocks, sga, triggers, dropped, nonbook


def characterise(window, hl_data):
    blocks, sga, triggers, dropped, nonbook = _scan(window)
    t0, t1 = blocks[0][1], blocks[-1][1]
    trades = list(iter_trades(hl_data, COINS, t0 - TOUCH_LOOKBACK_MS, t1))
    in_win = [t for t in trades if t.time_ms >= t0]
    by_hash = defaultdict(list)
    zero = []
    for t in in_win:
        if t.hash == ZERO_HASH:
            zero.append(t)
        elif t.hash in nonbook:
            by_hash[t.hash].append(t)

    # SetGlobalAction price path per asset: (height, tx_index) -> prev and this price pair
    sga_prev: dict = {}
    moves: dict = defaultdict(list)                     # asset -> [|move| bps of p0] for ranking
    last: dict = {}
    for h, i, pxs in sga:
        for a, pair in pxs.items():
            if a in last:
                sga_prev[(h, i, a)] = (last[a], pair)
                moves[a].append(abs((pair[0] - last[a][0]) / last[a][0] * 10_000))
            last[a] = pair

    touch = RealTouch([s for c in COINS for s in iter_books(hl_data, c, t0 - TOUCH_LOOKBACK_MS, t1)], trades,
                      [x for c in COINS for x in iter_bbo(hl_data, c, t0 - TOUCH_LOOKBACK_MS, t1)])
    events = []
    for hsh in sorted(by_hash, key=lambda x: nonbook[x][:2]):
        h, i, n, bt, kind = nonbook[hsh]
        touch.advance(bt)
        for a in sorted({t.asset for t in by_hash[hsh]}):
            trs = [t for t in by_hash[hsh] if t.asset == a]
            spec = ASSETS[a]
            sides = {t.taker_side.name for t in trs}
            takers = Counter(t.taker for t in trs)
            bid, ask, age = touch.get(a, bt)
            buys = [t.px for t in trs if t.taker_side.name == "BUY"]
            sells = [t.px for t in trs if t.taker_side.name == "SELL"]
            beyond = max([p - ask for p in buys if ask is not None] + [bid - p for p in sells if bid is not None]
                         or [0])
            ev = dict(height=h, tx_index=i, num_txs=n, position="first" if i == 0 else "last" if i == n - 1
                      else "middle", kind=kind, coin=spec.name, fills=len(trs),
                      notional_usd=usd(sum(t.px * t.sz for t in trs)), notional=sum(t.px * t.sz for t in trs),
                      taker_side="|".join(sorted(sides)), takers=len(takers),
                      top_taker=takers.most_common(1)[0][0], top_taker_share=f"{takers.most_common(1)[0][1] / len(trs):.2f}",
                      makers=len({t.maker for t in trs}), distinct_px=len({t.px for t in trs}),
                      px_min=spec.ticks_to_px(min(t.px for t in trs)), px_max=spec.ticks_to_px(max(t.px for t in trs)),
                      touch_bid_before="" if bid is None else spec.ticks_to_px(bid),
                      touch_ask_before="" if ask is None else spec.ticks_to_px(ask), touch_age_ms=age,
                      worst_ticks_beyond_touch=beyond, oracle_px="", oracle_move_bps="", move_rank="",
                      takers_with_trigger_in_window=0, takers_trigger_crossed_by_oracle=0)
            pp = sga_prev.get((h, i, a))
            if kind == "SetGlobalAction" and pp is not None:
                prev, new = pp
                ev["oracle_px"] = f"{new[0]}|{new[1]}"
                ev["oracle_move_bps"] = bps(prev[0], new[0])
                m = abs((new[0] - prev[0]) / prev[0] * 10_000)
                ev["move_rank"] = f"{1 + sum(x > m for x in moves[a])}/{len(moves[a])}"
            for u in takers:
                side_buy = trs[[t.taker for t in trs].index(u)].taker_side.name == "BUY"
                prior = [x for x in triggers.get((u, a), []) if (x["height"], x["tx_index"]) < (h, i)]
                if prior:
                    ev["takers_with_trigger_in_window"] += 1
                if pp is not None and any(x["buy"] == side_buy and any(crossed(pp[0][j], pp[1][j], x["trigger"])
                                                                       for j in (0, 1)) for x in prior):
                    ev["takers_trigger_crossed_by_oracle"] += 1
            events.append(ev)

    # zero-hash trades
    time_to_block = {t: (h, n) for h, t, n in blocks}
    sga_by_height = defaultdict(list)
    for h, i, _ in sga:
        sga_by_height[h].append(i)
    ev_blocks = {(e["height"], e["coin"]) for e in events}
    ev_takers = {e["top_taker"] for e in events}
    z = Counter()
    per_cell = defaultdict(list)
    for t in zero:
        hn = time_to_block.get(t.time_ms)
        if hn is None:
            z["no_block_trades"] += 1
            continue
        h, n = hn
        coin = ASSETS[t.asset].name
        per_cell[(h, coin)].append(t)
        z["zero_hash_trades"] += 1
        z["zero_hash_notional_usd_micro"] += t.px * t.sz
        if h in sga_by_height:
            z["in_sga_block_trades"] += 1
            z["in_sga_block_notional_usd_micro"] += t.px * t.sz
        if (h, coin) in ev_blocks:
            z["same_block_as_sga_trade_event_trades"] += 1
        if t.taker in ev_takers:
            z["taker_is_an_sga_event_taker_trades"] += 1
        if any(x["time"] <= t.time_ms for x in triggers.get((t.taker, t.asset), [])):
            z["taker_had_trigger_before_trades"] += 1
            z["taker_had_trigger_before_notional_usd_micro"] += t.px * t.sz
    sweeps = []
    for (h, coin), trs in sorted(per_cell.items()):
        nt = sum(t.px * t.sz for t in trs)
        if nt >= BIG_ZERO_USD * MICRO:
            sweeps.append(dict(height=h, coin=coin, fills=len(trs), notional_usd=usd(nt),
                               distinct_px=len({t.px for t in trs}), takers=len({t.taker for t in trs}),
                               sga_in_block="|".join(map(str, sga_by_height.get(h, []))),
                               num_txs=time_to_block[next(t.time_ms for t in trs)][1]))
    z["n_blocks_with_zero_hash"] = len({h for h, _ in per_cell})
    z["n_blocks_with_sga"] = len(sga_by_height)
    z["blocks"] = len(blocks)

    # non-market triggers whose owner later made at the trigger's limit price (a triggered limit that rested)
    tl = Counter()
    for (u, a), xs in triggers.items():
        for x in xs:
            tl["triggers"] += 1
            tl["trigger_modifies"] += x["modify"]
            tl["market"] += x["market"]
            if x["market"]:
                continue
            for t in in_win:
                if (t.asset == a and t.maker == u and t.time_ms > x["time"] and (t.taker_side.name == "SELL") == x["buy"]
                        and Decimal(ASSETS[a].ticks_to_px(t.px)) == x["limit"]):
                    tl["limit_trigger_owner_made_at_limit_trades"] += 1
                    tl["limit_trigger_owner_made_at_limit_notional_usd_micro"] += t.px * t.sz
    z.update({f"trigger:{k}": v for k, v in tl.items()})
    z["dropped_trigger_modifies"] = len(dropped)
    return events, dict(z, sweeps=sweeps)


# ------------------------------------------------------------------ instrumented runs

def make_observer(dropped: list[dict], sink: dict):
    """validate_replay.ValidateObserver + (a) outside-fill coverage per hash, (b) dropped trigger-modify
    targets resting in our book. CSV outputs are unchanged: it only adds."""
    pending = sorted(dropped, key=lambda d: (d["height"], d["tx_index"], d["item"]))

    class GAObserver(vr.ValidateObserver):
        def __init__(self, real_by_hash, alo_truth):
            super().__init__(real_by_hash, alo_truth)
            self.cover = defaultdict(Counter)       # hash kind ("zero"|"other") -> named/placeholder/unmatched
            self.cover_by_hash = defaultdict(Counter)
            self.i = 0
            self.dropped_status = Counter()
            self.phantom_oids: dict[int, dict] = {}
            sink["obs"] = self

        def _flush(self, rp, upto):
            while self.i < len(pending) and (pending[self.i]["height"], pending[self.i]["tx_index"]) < upto:
                d = pending[self.i]
                self.i += 1
                eng = rp.engine
                if isinstance(d["target"], str):
                    oids = list(eng.by_cloid.get((d["user"], d["asset"], d["target"]), []))
                    st = "cloid: resting in our book" if oids else "cloid: not in our book"
                else:
                    oids = []
                    if rp.resolver is not None:
                        saved, last = Counter(rp.resolver.stats), rp.resolver.last
                        r = rp.resolver(d["user"], d["asset"], d["target"])
                        rp.resolver.stats, rp.resolver.last = saved, last
                        oids = [r] if r is not None else []
                    st = "oid: resolves to a resting order" if oids else "oid: no resting order"
                self.dropped_status[st] += 1
                for oid in oids:
                    self.phantom_oids[oid] = d

        def before_action(self, rp, block, a):
            self._flush(rp, (a.block, a.tx_index))
            super().before_action(rp, block, a)

        def after_action(self, rp, block, a, fills, rejects, created):
            if isinstance(a, OutsideFill):
                kind = "zero" if a.hash == ZERO_HASH else "other"
                c = Counter()
                for f in fills:
                    c["placeholder" if f.maker_user == PLACEHOLDER else
                      "named" if f.maker_user == a.maker_user else "other"] += f.px * f.sz
                c["unmatched"] += a.px * (a.sz - sum(f.sz for f in fills))
                self.cover[kind].update(c)
                if kind == "other":
                    self.cover_by_hash[a.hash].update(c)
            super().after_action(rp, block, a, fills, rejects, created)

        def after_block(self, rp, block, minute):
            self._flush(rp, (block.height, 1 << 62))
            super().after_block(rp, block, minute)

        def extras(self) -> dict:
            ph = Counter()
            for rec in self.txs.values():
                real = Counter()
                for px, sz, m in rec.real:
                    real[(px, m)] += sz
                for px, sz, m, oid in rec.engine:
                    if oid in self.phantom_oids:
                        ph["engine_fills"] += 1
                        ph["engine_notional"] += px * sz
                        ph["matched_notional"] += min(sz, real[(px, m)]) * px
            return {"cover": {k: dict(v) for k, v in self.cover.items()},
                    "cover_by_hash": {k: dict(v) for k, v in self.cover_by_hash.items()},
                    "dropped_trigger_modify_status": dict(self.dropped_status),
                    "phantom_target_oids": len(self.phantom_oids), "phantom_fills": dict(ph)}

    return GAObserver


def run_one(window, hl_data, name, out: Path, dropped) -> dict:
    kw, _ = RUNS[name]
    sink: dict = {}
    res, rows = vr.run(window, hl_data, out_dir=out / name, quiet=True,
                       observer_cls=make_observer(dropped, sink), **BASE, **kw)
    keep = {k: v for k, v in res.items() if not isinstance(v, (dict, list))}
    data = dict(summary=keep, extras=sink["obs"].extras())
    (out / f"{name}.json").write_text(json.dumps(data, indent=1, default=str))
    return data


# ------------------------------------------------------------------ report

def load_rows(path: Path) -> list[dict]:
    with open(path) as f:
        return [r for r in csv.DictReader(f) if r["warmup"] == "0" and r["status"] in ("match", "mismatch")]


def report(out: Path, events, zero, runs: dict, n_blocks: int) -> None:
    rows = {name: load_rows(out / name / "txs.csv") for name in runs}
    idx = {name: index_rows(rs) for name, rs in rows.items()}
    sga_ev = sorted({(e["height"], e["coin"]) for e in events if e["kind"] == "SetGlobalAction"})
    big = sorted({(e["height"], e["coin"]) for e in events if e["notional_usd"] >= BIG_ZERO_USD
                  and e["kind"] == "SetGlobalAction"})
    sweeps = sorted({(s["height"], s["coin"]) for s in zero["sweeps"]})
    out_rows = []
    for label, evs in (("SetGlobalAction trade events", sga_ev), ("SetGlobalAction >= $10k", big),
                       ("zero-hash sweeps >= $10k", sweeps)):
        for n in HORIZONS:
            cells = local_cells(evs, n)
            for name in runs:
                tot_m = sum(v[0] for v in idx[name].values())
                tot_s = sum(v[1] for v in idx[name].values())
                exp_m = tot_m * len(cells) // (n_blocks * len(COINS))      # average per (block, coin) cell
                exp_s = tot_s * len(cells) // (n_blocks * len(COINS))
                m, s = local_error(idx[name], evs, n)
                out_rows.append(dict(events=label, n_events=len(evs), blocks_after=n, cells=len(cells), run=name,
                                     missed_usd=usd(m), spurious_usd=usd(s),
                                     expected_missed_usd=usd(exp_m), expected_spurious_usd=usd(exp_s)))
    with open(out / "local.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0]))
        w.writeheader()
        w.writerows(out_rows)

    # scoring: denominators if the SetGlobalAction trades were scored
    rn = sum(int(r["real_notional"]) for r in rows["base"])
    mn = sum(int(r["matched_notional"]) for r in rows["base"])
    en = sum(int(r["engine_notional"]) for r in rows["base"])
    x = sum(e["notional"] for e in events)
    sc = [dict(option="base (not scored, not applied)", real_usd=usd(rn), engine_usd=usd(en), matched_usd=usd(mn),
               recall=vr.rate(mn, rn), precision=vr.rate(mn, en))]
    for name in ("other-end", "other-start", "other-index"):
        if name not in runs:
            continue
        rs = rows[name]
        rn2, mn2, en2 = (sum(int(r[k]) for r in rs) for k in ("real_notional", "matched_notional", "engine_notional"))
        sc.append(dict(option=f"{name}: applied, not scored", real_usd=usd(rn2), engine_usd=usd(en2),
                       matched_usd=usd(mn2), recall=vr.rate(mn2, rn2), precision=vr.rate(mn2, en2)))
        cov = runs[name]["extras"]["cover"].get("other", {})
        named = cov.get("named", 0)
        sc.append(dict(option=f"{name}: applied + scored as rows (forced fills: named-maker part matches)",
                       real_usd=usd(rn2 + x), engine_usd=usd(en2 + x - cov.get("unmatched", 0)),
                       matched_usd=usd(mn2 + named), recall=vr.rate(mn2 + named, rn2 + x),
                       precision=vr.rate(mn2 + named, en2 + x - cov.get("unmatched", 0))))
    sc.append(dict(option="base + scored as real-only rows (engine never fills them)", real_usd=usd(rn + x),
                   engine_usd=usd(en), matched_usd=usd(mn), recall=vr.rate(mn, rn + x), precision=vr.rate(mn, en)))
    with open(out / "scoring.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(sc[0]))
        w.writeheader()
        w.writerows(sc)


def write_events(out: Path, events, zero) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "events.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(events[0]))
        w.writeheader()
        w.writerows(events)
    (out / "zero.json").write_text(json.dumps(zero, indent=1, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("window")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--out", default="handoff/globalaction")
    ap.add_argument("--runs", default=",".join(RUNS), help="comma list of " + ",".join(RUNS))
    ap.add_argument("--reuse", action="store_true", help="skip runs whose JSON already exists")
    a = ap.parse_args(argv)
    out = Path(a.out)
    events, zero = characterise(a.window, a.hl_data)
    write_events(out, events, zero)
    print(f"{len(events)} events, ${usd(sum(e['notional'] for e in events)):,}; zero-hash trades {zero['zero_hash_trades']}")
    dropped = _scan(a.window)[3]
    runs = {}
    for name in a.runs.split(","):
        p = out / f"{name}.json"
        if a.reuse and p.exists():
            runs[name] = json.loads(p.read_text())
            continue
        print(f"run {name}: validate_replay {BASE_FLAGS} {RUNS[name][1]}", flush=True)
        runs[name] = run_one(a.window, a.hl_data, name, out, dropped)
        s = runs[name]["summary"]
        print(f"  recall {s['all:fill_recall']} precision {s['all:fill_precision']}", flush=True)
    if "base" in runs:
        report(out, events, zero, runs, len(_block_files(Path(a.window))))
        print(f"wrote {out}/local.csv, scoring.csv")


if __name__ == "__main__":
    main()
