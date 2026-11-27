"""Gate 2 (docs/gate2.md): ON = B3 vs OFF = B3 + --order m1n on the listed blocks; V_race / V_absorb / V_net from
10 s maker markouts, chunk-cluster bootstrap.

usage: python -m analysis.gate2 run <blocks.csv> --out DIR --jobs N --hl-data ~/hl-data/mac3/hl_data <B3 flags> \
           --order m1n --send-times <send_times.csv>     (ON = the same flags without --order)
       python -m analysis.gate2 run ... --one-block   (A3 primary: OFF reorders one listed block per replay)
       python -m analysis.gate2 score DIR [--first-listed]
"""
from __future__ import annotations

import bisect
import csv
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

HORIZON_MS = 10_000
MID_MAX_WAIT_MS = 60_000
BOOT_REPS, BOOT_SEED = 10_000, 20261127


@dataclass(frozen=True)
class GFill:
    chunk: str
    height: int
    taker_hash: str
    maker: str              # maker identity (docs/gate2.md 3)
    maker_user: str
    maker_side: str         # "B" maker bought, "S" maker sold
    px: Decimal
    sz: Decimal
    coin: str
    time_ms: int


def markout(fl: GFill, mid10: Decimal) -> Decimal:
    """Maker's 10 s markout in USD; positive = good for the maker."""
    d = (fl.px - mid10) if fl.maker_side == "S" else (mid10 - fl.px)
    return d * fl.sz


def block_values(on: list[GFill], off: list[GFill], raced: set[str], mid10) -> dict:
    """V_race / V_absorb / V_net for one block (sums ON minus OFF). mid10(coin, time_ms) -> Decimal | None."""
    out = dict(V_race=Decimal(0), V_absorb=Decimal(0), n_race_on=0, n_race_off=0, n_absorb_on=0, n_absorb_off=0,
               ntl_on=Decimal(0), ntl_off=Decimal(0), dropped_no_mid=0,
               **{f"{x}_{g}_{arm}": Decimal(0) for x in ("m", "ntl") for g in ("race", "absorb") for arm in ("on", "off")})
    key = lambda x: (x.taker_hash, x.maker, x.maker_side, x.px, x.sz)
    out["changed"] = sorted(map(key, on)) != sorted(map(key, off))
    for arm, fills, sign in (("on", on, 1), ("off", off, -1)):
        for fl in fills:
            m = mid10(fl.coin, fl.time_ms)
            if m is None:
                out["dropped_no_mid"] += 1
                continue
            grp = "race" if fl.maker in raced else "absorb"
            out[f"V_{grp}"] += sign * markout(fl, m)
            out[f"n_{grp}_{arm}"] += 1
            out[f"ntl_{arm}"] += fl.px * fl.sz
            out[f"m_{grp}_{arm}"] += markout(fl, m)                  # bps reporting (A3 item 6)
            out[f"ntl_{grp}_{arm}"] += fl.px * fl.sz
    out["V_net"] = out["V_race"] + out["V_absorb"]
    return out


def cluster_boot(per_chunk: dict[str, float], reps: int = BOOT_REPS, seed: int = BOOT_SEED) -> tuple:
    """(point = sum over chunks, lo, hi): percentile 95% CI of the sum, resampling chunks with replacement."""
    keys = sorted(per_chunk)
    vals = [per_chunk[k] for k in keys]
    point = sum(vals)
    if not vals:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    sims = sorted(sum(vals[rng.randrange(len(vals))] for _ in vals) for _ in range(reps))
    return point, sims[int(0.025 * reps)], sims[min(reps - 1, int(0.975 * reps))]


def bps_diff(m_on, n_on, m_off, n_off):
    """ON - OFF notional-weighted mean markout in bps (1e4 x sum markout / sum notional); None if an arm has none."""
    if not n_on or not n_off:
        return None
    return 1e4 * (m_on / n_on - m_off / n_off)


def cluster_boot_ratio(per_chunk: dict[str, tuple], reps: int = BOOT_REPS, seed: int = BOOT_SEED) -> tuple:
    """bps_diff of the summed (m_on, n_on, m_off, n_off), chunks resampled with numerator and denominator together.
    (point, lo, hi); None values where undefined."""
    keys = sorted(per_chunk)
    vals = [per_chunk[k] for k in keys]
    tot = lambda vs: [sum(v[i] for v in vs) for i in range(4)]
    point = bps_diff(*tot(vals)) if vals else None
    if point is None:
        return None, None, None
    rng = random.Random(seed)
    sims = [bps_diff(*tot([vals[rng.randrange(len(vals))] for _ in vals])) for _ in range(reps)]
    sims = sorted(x for x in sims if x is not None)
    return point, sims[int(0.025 * len(sims))], sims[min(len(sims) - 1, int(0.975 * len(sims)))]


class Mids:
    """mid10 lookups from the recorded bbo (identical in both arms)."""

    def __init__(self, hl_data, t0_ms, t1_ms):
        from engine_py.replay import iter_bbo
        from engine_py.types import ASSETS
        self.series = {}
        for asset, spec in sorted(ASSETS.items()):
            ts, mids = [], []
            for t, _a, b, k in iter_bbo(hl_data, spec.name, t0_ms, t1_ms + HORIZON_MS + MID_MAX_WAIT_MS):
                if b and k:
                    ts.append(t)
                    mids.append((Decimal(spec.ticks_to_px(b[0])) + Decimal(spec.ticks_to_px(k[0]))) / 2)
            self.series[spec.name] = (ts, mids)

    def __call__(self, coin, t_ms):
        ts, mids = self.series[coin]
        i = bisect.bisect_left(ts, t_ms + HORIZON_MS)
        if i >= len(ts) or ts[i] > t_ms + HORIZON_MS + MID_MAX_WAIT_MS:
            return None
        return mids[i]


def _dump(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")


# ------------------------------------------------------------------ runner (both arms per chunk)

def _ident(rp, oid: int) -> str:
    a = rp.placed_by.get(oid)
    return f"{a.block}:{a.hash.lower()}:{a.item_index}" if a is not None else f"PH:{oid}"


def observer_for(heights: frozenset):
    """A ValidateObserver that also records, on the listed heights only, every fill (with maker identity) and the
    maker orders that a cancel / cancelByCloid / modify of their owner removed (raced candidates)."""
    from analysis.validate_replay import ValidateObserver
    from engine_py.types import ASSETS, Cancel, CancelByCloid, Modify, Side

    class Gate2Observer(ValidateObserver):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.g_fills, self.g_raced, self.g_txs, self._snap = [], [], set(), None

        def before_action(self, rp, block, a):
            super().before_action(rp, block, a)
            if block.height in heights:
                self.g_txs.add((block.height, a.hash.lower()))
            if block.height in heights and isinstance(a, (Cancel, CancelByCloid, Modify)):
                self._snap = {o: _ident(rp, o) for o in rp.engine.by_user.get(a.user, {})}

        def after_action(self, rp, block, a, fills, rejects, created):
            super().after_action(rp, block, a, fills, rejects, created)
            if block.height not in heights:
                return
            spec = ASSETS[a.asset] if fills else None    # ScheduleCancel has no asset (and never fills)
            for fl in fills:
                self.g_fills.append(dict(
                    height=block.height, taker_hash=fl.taker_hash.lower(), maker=_ident(rp, fl.maker_order),
                    maker_user=fl.maker_user.lower(), maker_side="S" if fl.taker_side is Side.BUY else "B",
                    px=str(spec.ticks_to_px(fl.px)), sz=str(spec.lots_to_sz(fl.sz)), coin=spec.name,
                    time_ms=block.time_ms))
            if self._snap is not None:
                now = rp.engine.by_user.get(a.user, {})
                filled = {fl.maker_order for fl in fills}
                for o, ident in sorted(self._snap.items()):
                    if o not in now and o not in filled:
                        self.g_raced.append(dict(height=block.height, maker=ident, by=a.hash.lower()))
                self._snap = None

    return Gate2Observer


def _run_chunk(job) -> dict:
    """One chunk, both arms. Top level so it pickles."""
    from analysis import validate_replay as vr
    from analysis.validate_chunks import _free_chunk_caches
    chunk, hl_data, heights, kw_on, kw_off = job
    out = {"chunk": Path(chunk).name, "arms": {}}
    for arm, kw in (("on", kw_on), ("off", kw_off)):
        obs_cls = observer_for(frozenset(heights))
        holder = {}

        class Cls(obs_cls):                          # keep a handle on the instance run() builds
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                holder["obs"] = self
        try:
            res, rows = vr.run(chunk, hl_data, "listing", out_dir=None, quiet=True, observer_cls=Cls, **kw)
        except Exception as e:                       # recorded and reported, never dropped
            out["arms"][arm] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
            continue
        o = holder["obs"]
        out["arms"][arm] = {"status": "ok", "res": res, "rows": rows, "fills": o.g_fills, "raced": o.g_raced,
                           "txs": sorted(o.g_txs)}
    _free_chunk_caches()
    return out


def _run_arm(job) -> dict:
    """A3 one-block mode: one arm of one chunk, recording only `heights`. Top level so it pickles."""
    from analysis import validate_replay as vr
    from analysis.validate_chunks import _free_chunk_caches
    tag, chunk, hl_data, heights, kw = job
    holder = {}

    class Cls(observer_for(frozenset(heights))):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            holder["obs"] = self
    try:
        res, rows = vr.run(chunk, hl_data, "listing", out_dir=None, quiet=True, observer_cls=Cls, **kw)
        o = holder["obs"]
        arm = {"status": "ok", "res": res, "rows": rows, "fills": o.g_fills, "raced": o.g_raced, "txs": sorted(o.g_txs)}
    except Exception as e:                           # recorded and reported, never dropped
        arm = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    _free_chunk_caches()
    return {"tag": tag, "chunk": Path(chunk).name, "arm": arm}


def _one_block_send_times(src, out_dir: Path, height: int) -> Path:
    """The send-time file restricted to one height: M1n then reorders only that block (others: no timed tx)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{height}.csv"
    with open(src, newline="") as fh, open(p, "w", newline="") as fo:
        rd = csv.DictReader(fh)
        wr = csv.DictWriter(fo, fieldnames=rd.fieldnames)
        wr.writeheader()
        wr.writerows(r for r in rd if int(r["height"]) == height)
    return p


def _assemble_one_block(parts: list[dict]) -> list[dict]:
    """Per chunk: ON from its single ON replay; OFF = the per-block OFF replays, each contributing only its block."""
    by: dict[str, dict] = {}
    for p in sorted(parts, key=lambda p: (p["chunk"], p["tag"])):
        r = by.setdefault(p["chunk"], {"chunk": p["chunk"], "arms": {}, "off_parts": []})
        if p["tag"] == "on":
            r["arms"]["on"] = p["arm"]
        else:
            r["off_parts"].append(p["arm"])
    for r in by.values():
        offs = r.pop("off_parts")
        bad = [x for x in offs if x["status"] != "ok"]
        r["arms"]["off"] = ({"status": "failed", "error": "; ".join(x["error"] for x in bad)} if bad else
                            {"status": "ok", "fills": [f for x in offs for f in x["fills"]],
                             "raced": [f for x in offs for f in x["raced"]],
                             "txs": sorted({tuple(t) for x in offs for t in x["txs"]})})
    return [by[k] for k in sorted(by)]


def run_main(argv):
    import argparse
    import hashlib
    import pickle
    from concurrent.futures import ProcessPoolExecutor
    from analysis import validate_replay as vr
    from analysis.validate_chunks import _meta, chunk_row, pool, write_csvs
    ap = argparse.ArgumentParser()
    ap.add_argument("blocks")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--one-block", action="store_true",
                    help="A3 primary: OFF replayed once per listed block, only that block reordered and recorded")
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    if a.order != "m1n" or not a.send_times:
        sys.exit("gate2 run: pass the OFF arm as --order m1n --send-times FILE (ON = the same flags without it)")
    kw_off = vr.replay_kwargs(a)
    a.order = "listing"
    kw_on = vr.replay_kwargs(a)
    by_chunk: dict[str, set] = {}
    with open(a.blocks, newline="") as fh:
        for r in csv.DictReader(fh):
            by_chunk.setdefault(r["chunk_dir"], set()).add(int(r["height"]))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    meta = {"blocks_csv": a.blocks, "blocks_sha256": hashlib.sha256(open(a.blocks, "rb").read()).hexdigest(),
            "send_times": a.send_times, "send_times_sha256": hashlib.sha256(open(a.send_times, "rb").read()).hexdigest(),
            "chunks": len(by_chunk), "listed_blocks": sum(map(len, by_chunk.values())), "argv": argv,
            "mode": "one_block" if a.one_block else "all_listed"}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    if a.one_block:
        jobs = []
        for c, hs in sorted(by_chunk.items()):
            jobs.append(("on", c, a.hl_data, sorted(hs), kw_on))
            for h in sorted(hs):
                st = _one_block_send_times(a.send_times, out / "send_times_by_height", h)
                jobs.append((f"off:{h}", c, a.hl_data, [h], dict(kw_off, send_times=str(st))))
        with ProcessPoolExecutor(a.jobs) as ex:
            parts = list(ex.map(_run_arm, jobs)) if a.jobs > 1 else list(map(_run_arm, jobs))
        results = _assemble_one_block(parts)
        pickle.dump(results, open(out / "results.pkl", "wb"))
        outs = [{"chunk": r["chunk"], "status": r["arms"]["on"]["status"], "res": r["arms"]["on"].get("res"),
                 "rows": r["arms"]["on"].get("rows", []), "error": r["arms"]["on"].get("error", "")} for r in results]
        res, rows = pool(outs, [], kw_on, "listing")
        chunks = {Path(c).name: Path(c) for c in by_chunk}
        write_csvs(out / "on", res, rows, [chunk_row(o, _meta(chunks[o["chunk"]])) for o in outs])
        print(f"gate2 run --one-block: {len(results)} chunks, {len(jobs)} replays -> {out}")
        return
    jobs = [(c, a.hl_data, sorted(h), kw_on, kw_off) for c, h in sorted(by_chunk.items())]
    with ProcessPoolExecutor(a.jobs) as ex:
        results = list(ex.map(_run_chunk, jobs)) if a.jobs > 1 else list(map(_run_chunk, jobs))
    pickle.dump(results, open(out / "results.pkl", "wb"))
    for arm in ("on", "off"):
        outs = [{"chunk": r["chunk"], "status": r["arms"][arm]["status"], "res": r["arms"][arm].get("res"),
                 "rows": r["arms"][arm].get("rows", []), "error": r["arms"][arm].get("error", "")} for r in results]
        res, rows = pool(outs, [], kw_on if arm == "on" else kw_off, "listing")
        chunks = {Path(c).name: Path(c) for c in by_chunk}
        write_csvs(out / arm, res, rows, [chunk_row(o, _meta(chunks[o["chunk"]])) for o in outs])
    print(f"gate2 run: {len(results)} chunks -> {out}")


def _score_pass(results, failed, hl_data, exclude=None, only=None) -> tuple[dict, Counter, list]:
    """exclude: (height, hash) set. A fill is dropped from BOTH arms when its taker tx is in it, or its maker is a
    raced order whose racing cancel tx is in it (sensitivity row, docs/gate2.md 5)."""
    per = {k: {} for k in ("V_race", "V_absorb", "V_net")}
    tot, rows_out = Counter(), []
    for r in results:
        if r["chunk"] in failed:
            continue
        on, off = r["arms"]["on"], r["arms"]["off"]
        gf = lambda x: GFill(chunk=r["chunk"], px=Decimal(x["px"]), sz=Decimal(x["sz"]),
                             **{k: x[k] for k in ("height", "taker_hash", "maker", "maker_user", "maker_side",
                                                  "coin", "time_ms")})
        heights = sorted({x["height"] for x in on["fills"] + off["fills"] + on["raced"] + off["raced"]})
        if only is not None:                         # A3: e.g. the first listed block of each chunk
            heights = [h for h in heights if h in only.get(r["chunk"], ())]
        times = [x["time_ms"] for x in on["fills"] + off["fills"]]
        mids = Mids(hl_data, min(times), max(times)) if times else None
        acc = Counter()
        for h in heights:
            races = [x for x in on["raced"] + off["raced"] if x["height"] == h]
            raced = {x["maker"] for x in races}
            bad_makers = {x["maker"] for x in races if exclude is not None and (h, x["by"]) in exclude}
            keep = lambda x: exclude is None or ((h, x["taker_hash"]) not in exclude and x["maker"] not in bad_makers)
            fon = [gf(x) for x in on["fills"] if x["height"] == h and keep(x)]
            foff = [gf(x) for x in off["fills"] if x["height"] == h and keep(x)]
            if exclude is not None:
                acc["fills_excluded"] += sum(1 for x in on["fills"] + off["fills"] if x["height"] == h and not keep(x))
            v = block_values(fon, foff, raced, mids)
            for k, val in v.items():
                acc[k] += int(val) if isinstance(val, bool) else val
            acc["raced_orders"] += len(raced)
            acc["blocks"] += 1
        for k in ("V_race", "V_absorb", "V_net"):
            per[k][r["chunk"]] = float(acc[k])
        for g in ("race", "absorb"):
            per.setdefault(f"bps_{g}", {})[r["chunk"]] = tuple(float(acc[f"{x}_{g}_{arm}"]) for arm in ("on", "off")
                                                                for x in ("m", "ntl"))
        tot.update(acc)
        rows_out.append({"chunk": r["chunk"], **{k: str(v) for k, v in acc.items()}})
    return per, tot, rows_out


def _lines(title, per, tot, rows_out):
    out = [f"[{title}] chunks scored {len(per['V_race'])} (chunks with any difference: see 'chunks changed'); blocks with fills/races {tot['blocks']}, blocks changed "
           f"{tot['changed']}, chunks changed {sum(1 for x in rows_out if int(x.get('changed', '0')) > 0)}; "
           f"raced orders {tot['raced_orders']}; dropped (no mid) {tot['dropped_no_mid']}"
           + (f"; fills excluded {tot['fills_excluded']}" if "fills_excluded" in tot else "")]
    for k in ("V_race", "V_absorb", "V_net"):
        pt, lo, hi = cluster_boot(per[k])
        out.append(f"  {k:8s} {pt:+12.2f} USD  95% CI [{lo:+.2f}, {hi:+.2f}] (chunk bootstrap {BOOT_REPS}, seed {BOOT_SEED})")
    for g in ("race", "absorb"):
        vals = per.get(f"bps_{g}", {})
        m_on, n_on, m_off, n_off = (sum(v[i] for v in vals.values()) for i in range(4))
        on_b = f"{1e4 * m_on / n_on:+.3f}" if n_on else "n/a"
        off_b = f"{1e4 * m_off / n_off:+.3f}" if n_off else "n/a"
        pt, lo, hi = cluster_boot_ratio(vals)
        diff = f"{pt:+.3f} bps  95% CI [{lo:+.3f}, {hi:+.3f}]" if pt is not None else "n/a"
        out.append(f"  {g:6s} mean markout (notional-weighted) ON {on_b} / OFF {off_b} bps; ON - OFF {diff} (reporting only)")
    out.append(f"  n race fills ON / OFF {tot['n_race_on']} / {tot['n_race_off']}; absorb fills ON / OFF "
               f"{tot['n_absorb_on']} / {tot['n_absorb_off']}; notional ON {float(tot['ntl_on']):.0f} OFF "
               f"{float(tot['ntl_off']):.0f} USD")
    return out


def score_main(argv):
    import pickle
    from engine_py.ordering import load_send_times, reused_txs
    first = "--first-listed" in argv
    d = Path([x for x in argv if not x.startswith("--")][0])
    meta = json.loads((d / "meta.json").read_text())
    only = None
    if first:                                        # A3: first listed block of each chunk (35 blocks)
        only = {}
        with open(meta["blocks_csv"], newline="") as fh:
            for row in csv.DictReader(fh):
                only.setdefault(Path(row["chunk_dir"]).name, []).append(int(row["height"]))
        only = {c: {min(hs)} for c, hs in only.items()}
    sfx = "_first" if first else ""
    results = pickle.load(open(d / "results.pkl", "rb"))
    hl_data = meta["argv"][meta["argv"].index("--hl-data") + 1]
    failed = [r["chunk"] for r in results if any(r["arms"][x]["status"] != "ok" for x in ("on", "off"))]
    timed = set(load_send_times(meta["send_times"]))
    reused = reused_txs(meta["send_times"])
    seen = {tuple(t) for r in results if r["chunk"] not in failed for t in r["arms"]["on"]["txs"]}
    exclude = {t for t in seen if t not in timed} | reused
    lines = [f"gate2 score{' --first-listed' if first else ''} ({meta.get('mode', 'all_listed')}): {d} (blocks sha256 {meta['blocks_sha256'][:12]}, send times {meta['send_times_sha256'][:12]})",
             f"chunks failed {len(failed)} {failed}; listed-block txs {len(seen)}: untimed (stale/counter/future) "
             f"{len(seen - timed)}, reused-nonce {len(reused & seen)}"]
    per, tot, rows = _score_pass(results, failed, hl_data, only=only)
    lines += _lines("primary", per, tot, rows)
    per_x, tot_x, rows_x = _score_pass(results, failed, hl_data, exclude, only=only)
    lines += _lines("sensitivity: stale/reused-nonce actions excluded", per_x, tot_x, rows_x)
    (d / f"score{sfx}.txt").write_text("\n".join(lines) + "\n")
    _dump(d / f"per_chunk{sfx}.jsonl", rows)
    _dump(d / f"per_chunk_excl{sfx}.jsonl", rows_x)
    print("\n".join(lines))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        run_main(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "score":
        score_main(sys.argv[2:])
    else:
        sys.exit(__doc__)
