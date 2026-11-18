"""G1 triage: how much failed-fill notional could each replay error source explain (other than the
stale best bid/ask, which the bbo work handles)? Measurement only: no engine behavior changes.

    python -m analysis.g1_triage <window> --hl-data <dir> [--out DIR] [--jobs 3] [--reuse]

Base config = `validate_replay --alo-gate touch --reseed --nearest-ratio 3`. On the base run's scored
txs, failed-fill notional is split in two (strict per (price, maker) groups, as in validate_replay):
  missed   = real - matched    (what recall loses)
  spurious = engine - matched  (what precision loses)
Rows are of two kinds:
  variant      the window is rerun with one measurement flag; the row is the DROP in missed / spurious
               notional vs the base run (negative = the variant is worse). Causal for that flag.
  attribution  the instrumented base run marks failed groups that carry the hypothesis' signature;
               the row is their notional. An UPPER BOUND: marked, not proven caused.
H5 is skipped when the window has no recorded bbo. H10 (scoring artifacts) is a hand check of
first_mismatches.csv plus the real notional that never reaches a scored row. H11 (placeholder liquidity
that owners' cancels cannot reach) came out of the H10 hand check.
Writes triage.csv, triage.md (the table), context.csv (supporting splits), first_mismatches.csv (H10)
and one JSON per run to --out. A run takes ~2-3 min; --jobs (default 3) run at once.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from analysis import validate_replay as vr
from analysis.g1_fill_trace import TracingEngine
from data.parse import _block_files, read_block
from engine_py.matching import PLACEHOLDER
from engine_py.types import ASSETS, Modify, Place, RejectReason, Side

BASE = dict(alo_gate="touch", reseed=True, nearest_ratio=3)
BASE_FLAGS = "--alo-gate touch --reseed --nearest-ratio 3"
MIN_ORDER_USD = 10                 # Hyperliquid: order value px * sz >= $10 (exception: reduce-only exact close)
MICRO = 1_000_000                  # ticks x lots = micro-USD for BTC and ETH (validate_replay.notional)
ALO_TAIL_MS = 400                  # docs (priority fees): the queue tail placed in the last 400 ms sorts by p
EVIDENCE_REMOVALS = ("gate-evict", "rule8-evict", "touch-reconcile")

# run name -> (validate_replay.run kwargs on top of BASE, hypothesis, validate_replay CLI flags)
VARIANTS = {
    "class": (dict(policy_name="class"), "H1 in-block order: cancels before GTC/IOC (class sort)", "--policy class"),
    "priority": (dict(policy_name="priority"), "H1 in-block order: class sort + GTC/IOC by grouping.p",
                 "--policy priority"),
    "shift-1": (dict(snapshot_shift=-1), "H4 snapshots applied 1 block earlier", "--snapshot-shift -1"),
    "shift+1": (dict(snapshot_shift=1), "H4 snapshots applied 1 block later", "--snapshot-shift 1"),
    "outside-start": (dict(outside_position="start"), "H7 outside fills at block start",
                      "--outside-position start"),
    "outside-other": (dict(outside_other_txs=True), "H9 trades of non-book txs (SetGlobalAction) as outside fills",
                      "--outside-other-txs"),
    "stp-expire": (dict(stp="expire_maker"), "H8 self-trade: expire maker instead of cancel taker",
                   "--stp expire_maker"),
}


# ------------------------------------------------------------------ pure helpers (tested)

def groups(fills) -> Counter:
    """(px, maker) -> lots, for fills (px, sz, maker, ...)."""
    g = Counter()
    for f in fills:
        g[(f[0], f[2])] += f[1]
    return g


def failed_groups(real, engine) -> tuple[dict, dict]:
    """Per (px, maker): missed notional max(0, real - engine) * px and spurious max(0, engine - real) * px.
    Their sums equal real - matched and engine - matched (validate_replay.matched_notional)."""
    r, e = groups(real), groups(engine)
    missed = {k: (q - e.get(k, 0)) * k[0] for k, q in r.items() if q > e.get(k, 0)}
    spur = {k: (q - r.get(k, 0)) * k[0] for k, q in e.items() if q > r.get(k, 0)}
    return missed, spur


def share_of_group(engine, key, pred) -> float:
    """Fraction of the engine lots in group `key` whose fill satisfies pred(fill). (Analysis output only;
    the fraction never feeds back into the engine.)"""
    fs = [f for f in engine if (f[0], f[2]) == key]
    tot = sum(f[1] for f in fs)
    return sum(f[1] for f in fs if pred(f)) / tot if tot else 0.0


def tail_sorted(nodes, now_ms, tail_ms=ALO_TAIL_MS) -> list:
    """Hyperliquid's ALO queue rule (docs, priority fees): the tail of a level placed within the past
    `tail_ms` is sorted by priority rate, highest first. nodes: [(key, placed_ms | None, p | None)] in our
    queue order; placeholders carry placed_ms None (old). The tail starts at the first recent order; the
    sort is stable for equal p."""
    cut = next((i for i, (_, t, _) in enumerate(nodes) if t is not None and now_ms - t <= tail_ms), len(nodes))
    return nodes[:cut] + sorted(nodes[cut:], key=lambda n: -(n[2] or 0))


def taker_vs_touch(side, px, bid, ask) -> tuple[bool | None, int | None]:
    """(crosses the real touch?, how far the limit is beyond it in bps; negative = short of it)."""
    ref = ask if side is Side.BUY else bid
    if ref is None:
        return None, None
    beyond = (px - ref) if side is Side.BUY else (ref - px)
    return beyond >= 0, beyond * 10_000 // ref


def dist_bucket(bps) -> str:
    if bps is None:
        return "no touch"
    if bps < 0:
        return "short of touch"
    for hi in (0, 5, 20, 100):
        if bps <= hi:
            return f"0-{hi} bps beyond" if hi else "at touch"
    return "> 100 bps beyond"


def dropped_modifies(window) -> list[dict]:
    """BTC/ETH modify items that parse.py drops because the new order is a trigger: the cancel half of
    those modifies is lost with them. Raw scan of the window's blocks (error-free txs only)."""
    out = []
    for h, path in _block_files(Path(window)):
        for tx in read_block(path)["txs"]:
            act = tx["action"]
            if act.get("type") not in ("modify", "batchModify") or tx["error"] is not None:
                continue
            for m in ([act] if act["type"] == "modify" else act["modifies"]):
                o = m["order"]
                if o["a"] in ASSETS and "trigger" in o["t"]:
                    out.append(dict(block=h, user=tx["user"].lower(), asset=o["a"], target=m["oid"]))
    return out


# ------------------------------------------------------------------ instrumented run

class OidTracingEngine(TracingEngine):
    """TracingEngine + why each engine oid was removed (orders filled out are never 'removed')."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.removed_why: dict[int, str] = {}

    def _remove(self, oid):
        o = super()._remove(oid)
        self.removed_why[oid] = self.why
        return o


class TriageObserver(vr.ValidateObserver):
    def __init__(self, real_by_hash, alo_truth):
        super().__init__(real_by_hash, alo_truth)
        self.rp = None
        self.taker: dict[str, dict] = {}            # hash -> taker order facts (first non-ALO item)
        self.level_at_tx: dict[tuple, dict] = {}    # (hash, px, maker) -> real maker's queue before the tx
        self.captured: set = set()
        self.self_trade_txs: set = set()
        self.admitted = Counter()                   # resting orders admitted: (< $10, reduce_only) -> n
        self.block_time: dict[int, int] = {}

    def _open_tx(self, rp, block, a, p):
        if a.hash not in self.txs:
            bid, ask, _ = rp.touch.get(p.asset, block.time_ms) if rp.touch is not None else (None, None, None)
            self.taker[a.hash] = dict(tif=p.tif.name, reduce_only=p.reduce_only, side=p.side, px=p.px,
                                      notional=p.px * p.sz, bid=bid, ask=ask)
        super()._open_tx(rp, block, a, p)

    def before_action(self, rp, block, a):
        if self.rp is None:
            self.rp = rp
            self.block_time = {b.height: b.time_ms for b in rp.blocks}
        if isinstance(a, (Place, Modify)) and a.hash in self.real_by_hash and a.hash not in self.captured:
            self.captured.add(a.hash)
            for t in self.real_by_hash[a.hash]:
                self.level_at_tx.setdefault((a.hash, t.px, t.maker), self._level(rp, block, t))
        super().before_action(rp, block, a)

    def _level(self, rp, block, t) -> dict:
        level = rp.engine.books[t.asset].side(t.taker_side.opposite).get(t.px)
        nodes = []                                  # (oid, placed ms | None, p, user, lots)
        for nd in (level if level is not None else ()):
            o = nd.order
            act = rp.placed_by.get(o.oid)
            nodes.append((o.oid, None if o.user == PLACEHOLDER else self.block_time.get(o.block),
                          act.priority if act is not None else None, o.user, nd.remaining))
        mine = [i for i, n in enumerate(nodes) if n[3] == t.maker]
        if not mine:
            return dict(state="absent")
        i = mine[0]
        ts = tail_sorted([n[:3] for n in nodes], block.time_ms)
        new_i = next(j for j, n in enumerate(ts) if n[0] == nodes[i][0])
        return dict(state="nothing ahead" if i == 0 else "size ahead", ahead=[(n[0], n[3], n[4]) for n in nodes[:i]],
                    tail_first=i > 0 and new_i == 0, tail_moves=new_i < i)

    def after_action(self, rp, block, a, fills, rejects, created):
        super().after_action(rp, block, a, fills, rejects, created)
        if any(r.reason is RejectReason.SELF_TRADE for r in rejects):
            self.self_trade_txs.add(a.hash)
        if created is not None and created in rp.engine.books[a.asset]:
            p = rp.placed[created]
            self.admitted[(p.px * p.sz < MIN_ORDER_USD * MICRO, p.reduce_only)] += 1

    def small(self, oid) -> bool:
        p = self.rp.placed.get(oid)
        return p is not None and p.px * p.sz < MIN_ORDER_USD * MICRO and not p.reduce_only


def scored_rows(rows):
    return [r for r in rows if not r["warmup"] and r["status"] in ("match", "mismatch")]


def summarize_run(res, rows) -> dict:
    """Small, JSON-safe summary of one validate run."""
    sc = scored_rows(rows)
    rn, en, mn = (sum(r[k] for r in sc) for k in ("real_notional", "engine_notional", "matched_notional"))
    early = [r for r in sc if r["horizon_ms"] != "" and r["horizon_ms"] < 2000]
    rn2, mn2 = sum(r["real_notional"] for r in early), sum(r["matched_notional"] for r in early)
    return dict(real=rn, engine=en, matched=mn, missed=rn - mn, spurious=en - mn,
                recall=res["all:fill_recall"], precision=res["all:fill_precision"],
                recall_0_2s=vr.rate(mn2, rn2), missed_0_2s=rn2 - mn2, scored=len(sc),
                shift_dropped=res.get("shift_dropped", 0))


def attribute(rows, obs, eng, drops) -> tuple[Counter, Counter]:
    """Attribution rows and context splits over the scored, mismatched txs of the instrumented run."""
    at, ctx = Counter(), Counter()
    drop_by = defaultdict(list)
    for d in drops:
        drop_by[(d["user"], d["asset"])].append(d["block"])
    for row in rows:
        if row["warmup"] or row["status"] != "mismatch":
            continue
        rec = obs.txs[row["hash"]]
        real, engine = obs.effective(rec)
        missed, spur = failed_groups(real, engine)
        tk = obs.taker.get(rec.hash)

        for (px, maker), n in missed.items():                       # H6: the real maker's queue
            if maker == vr.WILDCARD:
                ctx[("missed by real maker state", "pre-re-seed maker (px/size scored)")] += n
                continue
            lv = obs.level_at_tx.get((rec.hash, px, maker), {"state": "not captured"})
            ctx[("missed by real maker state", lv["state"])] += n
            if lv["state"] != "size ahead":
                continue
            tot = sum(q for *_, q in lv["ahead"])
            for oid, user, q in lv["ahead"]:
                why = eng.removed_why.get(oid)
                kind = ("placeholder" if user == PLACEHOLDER
                        else "known, later evicted by gate/rule 8" if why in EVIDENCE_REMOVALS
                        else "known, later cancelled/modified by owner" if why == "cancel/modify"
                        else "known, filled/cleared at re-seed/still resting" if why in (None, "re-seed clear")
                        else f"known, later removed by {why}")
                ctx[("H6 size ahead of the real maker (missed notional, split by lots ahead)", kind)] += n * q / tot
            at["H6 real maker resting behind other size"] += n
            at["H6 ALO tail sort by p (400 ms) moves real maker up"] += n * lv["tail_moves"]
            at["H6 ALO tail sort by p (400 ms) puts real maker first"] += n * lv["tail_first"]

        for key, n in spur.items():                                 # H2 / H9 / H11: the engine maker orders
            if key[1] == vr.WILDCARD:
                at["H11 spurious from placeholder liquidity"] += n
                ctx[("H11 placeholder spurious by tx type", "engine-only tx" if not rec.real else "tx with real fills")] += n
            at["H2 maker order < $10 (not reduce-only)"] += n * share_of_group(engine, key, lambda f: obs.small(f[3]))
            at["H9 maker had a dropped trigger-modify earlier"] += n * share_of_group(
                engine, key, lambda f: any(b <= rec.block for b in drop_by.get((f[2], rec.asset), ())))
        sp = sum(spur.values())
        if tk is not None and tk["notional"] < MIN_ORDER_USD * MICRO and not tk["reduce_only"]:
            at["H2 taker order < $10 (not reduce-only)"] += sp
        if rec.hash in obs.self_trade_txs:
            at["H8 SELF_TRADE reject in tx (missed)"] += sum(missed.values())
            at["H8 SELF_TRADE reject in tx (spurious)"] += sp
        if not rec.real and tk is not None:                          # H3: the chain made no fill at all
            crosses, bps = taker_vs_touch(tk["side"], tk["px"], tk["bid"], tk["ask"])
            ctx[("H3 engine-only spurious by taker tif", tk["tif"])] += sp
            ctx[("H3 engine-only spurious by taker reduce-only", str(tk["reduce_only"]))] += sp
            ctx[("H3 engine-only spurious by taker limit vs real touch", dist_bucket(bps))] += sp
            at["H3 engine-only tx (all)"] += sp
            at["H3 engine-only tx, taker crossed the real touch"] += sp * bool(crosses)
            at["H3 engine-only tx, taker crossed + reduce-only"] += sp * (bool(crosses) and tk["reduce_only"])
    for row in scored_rows(rows):                                    # H3 reference population
        tk = obs.taker.get(row["hash"])
        if not row["engine_only"] and tk is not None:
            ctx[("H3 reference: real notional of real-fill txs by taker reduce-only", str(tk["reduce_only"]))] += \
                row["real_notional"]
            ctx[("H3 reference: real notional of real-fill txs by taker tif", tk["tif"])] += row["real_notional"]
    return at, ctx


def run_instrumented(window, hl_data, out_dir) -> dict:
    holder = []

    def make(*a):
        holder.append(TriageObserver(*a))
        return holder[-1]

    eng = OidTracingEngine()
    vdir = Path(out_dir) / "validate-base"
    res, rows = vr.run(window, hl_data, quiet=True, observer_cls=make, engine=eng, out_dir=vdir, **BASE)
    obs = holder[0]
    drops = dropped_modifies(window)
    at, ctx = attribute(rows, obs, eng, drops)
    in_rows = {r["hash"] for r in rows}
    ps = obs.rp.parse_stats
    out = dict(
        base=summarize_run(res, rows), attribution={k: round(v) for k, v in at.items()},
        context=[[t, k, round(v)] for (t, k), v in sorted(ctx.items())],
        admitted={f"under_10={s} reduce_only={r}": n for (s, r), n in sorted(obs.admitted.items())},
        self_trade_txs=len(obs.self_trade_txs), self_trade_expired=eng.stats.get("self_trade_expired_maker", 0),
        parse={k: v for k, v in sorted(ps.items()) if k.startswith(("skip_", "errored:", "outside_fill_no_block"))},
        dropped_trigger_modifies=len(drops),
        bbo_files=len(list(Path(hl_data).glob("*/*/bbo_*.jsonl*"))),
        real_linked_txs_not_in_window=res["real_linked_txs_not_in_window"],
        real_notional_unscored=sum(t.px * t.sz for h, ts in obs.real_by_hash.items() if h not in in_rows for t in ts),
        mismatch_table={f"{c}|{k}": v for (c, k), v in res["mismatch_table"].items()})
    shutil.copy(vdir / "first_mismatches.csv", Path(out_dir) / "first_mismatches.csv")
    (Path(out_dir) / "run-instrumented.json").write_text(json.dumps(out, indent=1))
    return out


def run_variant(window, hl_data, name, out_dir) -> dict:
    res, rows = vr.run(window, hl_data, quiet=True, **{**BASE, **VARIANTS[name][0]})
    out = summarize_run(res, rows)
    (Path(out_dir) / f"run-{name}.json").write_text(json.dumps(out, indent=1))
    return out


# ------------------------------------------------------------------ table

def usd(micro) -> str:
    return f"{micro / MICRO:,.0f}"


def pct(n, d) -> str:
    return f"{100 * n / d:.1f}%" if d else ""


def build_table(base, variants, inst, cmd) -> list[dict]:
    rows = []
    vcmd = f"python -m analysis.validate_replay <window> --hl-data <dir> {BASE_FLAGS}"
    for name, (_, hyp, flags) in VARIANTS.items():
        v = variants.get(name)
        if v is None:
            continue
        dm, ds = base["missed"] - v["missed"], base["spurious"] - v["spurious"]
        rows.append(dict(hypothesis=hyp, kind="variant", missed_explained=usd(dm), missed_share=pct(dm, base["missed"]),
                         spurious_explained=usd(ds), spurious_share=pct(ds, base["spurious"]),
                         note=f"recall {v['recall']} precision {v['precision']} (base {base['recall']} / "
                              f"{base['precision']}); 0-2 s recall {v['recall_0_2s']} (base {base['recall_0_2s']})"
                              + (f"; {v['shift_dropped']} snapshots shifted out" if v.get("shift_dropped") else ""),
                         command=f"{vcmd} {flags}"))
    at = inst["attribution"]
    m, s = base["missed"], base["spurious"]
    att = [
        ("H2 maker order < $10 (not reduce-only)", "spurious",
         f"admitted resting orders: {inst['admitted']}"),
        ("H2 taker order < $10 (not reduce-only)", "spurious", ""),
        ("H3 engine-only tx (all)", "spurious", "all spurious notional in txs with no real fill"),
        ("H3 engine-only tx, taker crossed the real touch", "spurious",
         "chain made no fill though the taker crossed the estimated touch (taker reject OR stale touch)"),
        ("H3 engine-only tx, taker crossed + reduce-only", "spurious", ""),
        ("H5 within-block touch moves", None, f"skipped: {inst['bbo_files']} bbo files for this window"),
        ("H6 real maker resting behind other size", "missed", "see context.csv for what sits ahead"),
        ("H6 ALO tail sort by p (400 ms) moves real maker up", "missed", ""),
        ("H6 ALO tail sort by p (400 ms) puts real maker first", "missed", ""),
        ("H8 SELF_TRADE reject in tx (missed)", "missed", f"{inst['self_trade_txs']} txs with a SELF_TRADE reject"),
        ("H8 SELF_TRADE reject in tx (spurious)", "spurious", ""),
        ("H9 maker had a dropped trigger-modify earlier", "spurious",
         f"{inst['dropped_trigger_modifies']} trigger-modifies dropped; parse skips {inst['parse']}"),
        ("H11 spurious from placeholder liquidity", "spurious",
         "snapshot liquidity is anonymous: owners' cancels cannot remove it before the next re-seed "
         "(found in the H10 hand check; overlaps the bbo work at the touch)"),
        ("H10 real notional never scored", None,
         f"{usd(inst['real_notional_unscored'])} $ real notional in txs with no row; "
         f"{inst['real_linked_txs_not_in_window']} linked txs not in window; first 20 rows: first_mismatches.csv"),
    ]
    for key, kind, note in att:
        n = at.get(key, 0)
        rows.append(dict(hypothesis=key, kind="attribution" if kind else "info",
                         missed_explained=usd(n) if kind == "missed" else "",
                         missed_share=pct(n, m) if kind == "missed" else "",
                         spurious_explained=usd(n) if kind == "spurious" else "",
                         spurious_share=pct(n, s) if kind == "spurious" else "", note=note, command=cmd))
    return rows


def write_outputs(out: Path, base, rows, inst) -> str:
    cols = ["hypothesis", "kind", "missed_explained", "missed_share", "spurious_explained", "spurious_share",
            "note", "command"]
    with open(out / "triage.csv", "w", newline="") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(rows)
    with open(out / "context.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["split", "value", "notional_usd"])
        w.writerows([t, k, usd(v)] for t, k, v in inst["context"])
    md = [f"Base: recall {base['recall']}, precision {base['precision']}; missed ${usd(base['missed'])}, "
          f"spurious ${usd(base['spurious'])} (of real ${usd(base['real'])}, engine ${usd(base['engine'])}).", "",
          "| hypothesis | kind | missed $ | % missed | spurious $ | % spurious | note |",
          "|---|---|---:|---:|---:|---:|---|"]
    md += [f"| {r['hypothesis']} | {r['kind']} | {r['missed_explained']} | {r['missed_share']} | "
           f"{r['spurious_explained']} | {r['spurious_share']} | {r['note']} |" for r in rows]
    text = "\n".join(md) + "\n"
    (out / "triage.md").write_text(text)
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("window")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--out", help="default: report/validate/<window>-triage")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--reuse", action="store_true", help="load finished runs' JSON from --out")
    a = ap.parse_args(argv)
    out = Path(a.out or f"report/validate/{Path(a.window).name}-triage")
    out.mkdir(parents=True, exist_ok=True)
    cmd = f"python -m analysis.g1_triage <window> --hl-data <dir>"
    names = ["instrumented", *VARIANTS]
    done = {n: json.loads((out / f"run-{n}.json").read_text()) for n in names
            if a.reuse and (out / f"run-{n}.json").exists()}
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        futs = {n: (ex.submit(run_instrumented, a.window, a.hl_data, out) if n == "instrumented"
                    else ex.submit(run_variant, a.window, a.hl_data, n, out)) for n in names if n not in done}
        for n, f in futs.items():
            done[n] = f.result()
    inst = done["instrumented"]
    base = inst["base"]
    rows = build_table(base, {n: done[n] for n in VARIANTS}, inst, cmd)
    print(write_outputs(out, base, rows, inst))
    print(f"files in {out}")


if __name__ == "__main__":
    main()
