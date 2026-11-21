"""Batch validation over many chunk folders: validate_replay on each chunk, rows pooled into one report.

    python -m analysis.validate_chunks <windows_dir_or_chunk_dir>... --hl-data <dir> [--out DIR] [--jobs N, default 4]
                                       [same replay options as validate_replay, e.g.
                                        --alo-gate touch --reseed --nearest-ratio 3 --touch-reconcile]

Compare configurations in one pass with --variant (repeatable; each adds options to the base ones,
'' = the base options alone). Each chunk is decoded once and replayed per variant; output goes to one
CSV folder per variant plus compare.csv (variant x scope):

    python -m analysis.validate_chunks windows/ --hl-data hl_data --alo-gate touch --reseed --nearest-ratio 3 \
        --variant '' --variant '--touch-reconcile'

A chunk is a window folder whose window.json says kind="chunk" (collect/capture.py, CAPTURE_MODE=chunks):
~30 blocks right after an L2 snapshot. Each argument is either a chunk folder or a folder of them
(e.g. windows/); only complete chunks run, everything else is listed as skipped with the reason.
Chunks run in start-height order; a chunk that raises is recorded as failed (error text in chunks.csv
and the printout) and the others still run.

Pooled metrics are recomputed from the pooled tx rows with validate_replay's own definitions
(scope_metrics / mismatch_tables / horizon_table), never averaged across chunks, so a chunk with many
fills weighs more than one with few. Counters (gate, resolver, touch reconcile, ALO truth) are summed.

Outputs (CSV, in --out, default report/validate/chunks-<policy><options>):
  summary.csv     metric,value: chunk counts, options, pooled all/BTC/ETH metrics, g1_target, summed counters
  per_asset.csv   one row per scope (all, BTC, ETH): scored, matches, per-fill recall/precision/F1
  horizon.csv     pooled match / fill recall by time since re-seed (empty without --reseed)
  chunks.csv      one row per chunk: status (ok / failed / skipped: why), its own headline metrics, error
  txs.csv         every chunk's tx rows, with a leading `chunk` column
  mismatches.csv  pooled mismatch table by category
  alo_truth.csv   summed engine-decision vs alo_status truth
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import shlex
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from analysis import validate_replay as vr
from engine_py.replay import COINS

DEFAULT_JOBS = min(4, os.cpu_count() or 1)   # CLI default; parallel output is byte-identical to --jobs 1
SETTINGS = ("policy", "outside_fill_position", "resolver", "alo_gate", "gate_max_age_ms", "evict_known",
            "nearest_ratio", "reseed", "touch_reconcile", "lead_in")
SCOPE_METRICS = ("scored", "matched", "match_rate", "match_rate_px_sz", "match_rate_real_linked_only",
                 "engine_only_txs", "fill_recall", "fill_precision", "fill_f1", "fill_recall_px_sz",
                 "fill_precision_px_sz", "match_rate_full", "fill_recall_full", "fill_precision_full")
CHUNK_METRICS = ("scored", "matched", "fill_recall", "fill_precision")
SUMMED = ("gate_stats", "resolver_stats", "touch_reconciled", "alo_confusion")


# ------------------------------------------------------------------ discovery

def _meta(d: Path) -> dict | None:
    p = d / "window.json"
    return json.loads(p.read_text()) if p.exists() else None


def find_chunks(paths) -> tuple[list[Path], list[tuple[Path, str]]]:
    """(complete chunk folders in start-height order, [(folder, why skipped)]).
    Each path is a chunk folder itself or a folder whose subfolders are chunks."""
    candidates: list[Path] = []
    for p in map(Path, paths):
        if (p / "window.json").exists():
            candidates.append(p)
        else:
            candidates.extend(sorted(c for c in p.iterdir() if c.is_dir()))
    chunks, skipped, seen = [], [], set()
    for d in candidates:
        key = d.resolve()
        if key in seen:
            continue
        seen.add(key)
        meta = _meta(d)
        if meta is None:
            skipped.append((d, "no window.json"))
        elif meta.get("kind", "window") != "chunk":
            skipped.append((d, f"kind {meta.get('kind', 'window')}"))
        elif meta.get("status") != "complete":
            skipped.append((d, f"status {meta.get('status')}"))
        else:
            chunks.append((meta.get("start_height", 0), d.name, d))
    return [d for *_, d in sorted(chunks)], skipped


# ------------------------------------------------------------------ running

def _run_one(job) -> list[dict]:
    """One chunk under every variant -> [{"chunk", "status": ok|failed, "res", "rows", "error"}] in variant
    order. Top level so it pickles. Variants after the first reuse the decoded blocks and recorder lines
    (data.parse caches), so extra variants cost only the replay itself."""
    chunk, hl_data, policy_name, variants = job
    outs = []
    for _, kw in variants:
        try:
            res, rows = vr.run(chunk, hl_data, policy_name, out_dir=None, quiet=True, **kw)
        except Exception as e:                   # recorded per chunk and reported, never dropped
            outs.append({"chunk": Path(chunk).name, "status": "failed", "res": None, "rows": [],
                         "error": f"{type(e).__name__}: {e}"})
            continue
        outs.append({"chunk": Path(chunk).name, "status": "ok", "res": res, "rows": rows, "error": ""})
    _free_chunk_caches()
    return outs


def _free_chunk_caches() -> None:
    """Drop the per-process caches once a chunk's variants are done (decoded window, recorder files, oid estimator):
    the next chunk would only reuse the recorder hours they share, and keeping them made a worker's RSS climb
    chunk after chunk (overnight_2026-11-21 Gate 1). Outputs are unchanged: caches only skip re-reading."""
    import gc
    from data import oids, parse
    parse.clear_caches()
    oids.clear_cache()
    gc.collect()


def chunk_row(out: dict, meta: dict | None) -> dict:
    res = out["res"] or {}
    row = {"chunk": out["chunk"], "status": out["status"],
           "start_height": (meta or {}).get("start_height", ""), "end_height": (meta or {}).get("end_height", ""),
           "snapshot_time": (meta or {}).get("snapshot_time", ""), "blocks": res.get("blocks", "")}
    for scope in ("all", *COINS):
        for m in CHUNK_METRICS:
            row[f"{scope}:{m}"] = res.get(f"{scope}:{m}", "")
    row["error"] = out["error"]
    return row


def pool(outs: list[dict], skipped: list[tuple[Path, str]], kw: dict, policy_name: str) -> tuple[dict, list]:
    """Pooled result dict and pooled tx rows (each with a leading `chunk` field)."""
    ok = [o for o in outs if o["status"] == "ok"]
    rows = [{"chunk": o["chunk"], **r} for o in ok for r in o["rows"]]
    scored = vr.scored_rows(rows)
    res: dict = {"chunks_ok": len(ok), "chunks_failed": len(outs) - len(ok), "chunks_skipped": len(skipped),
                 "blocks": sum(o["res"]["blocks"] for o in ok)}
    if ok:
        res.update({k: ok[0]["res"][k] for k in SETTINGS})
    else:
        res.update({"policy": policy_name, **{k: v for k, v in kw.items() if k in SETTINGS}})
    res.update(vr.scope_metrics(scored))
    res["warmup_txs"] = sum(r["warmup"] for r in rows)
    res["excluded_real_maker_prewindow"] = sum(r["status"].startswith("excluded") and not r["warmup"] for r in rows)
    res["mismatch_table"], res["flag_table"] = vr.mismatch_tables(scored)
    res["horizons"] = vr.horizon_table(scored) if kw.get("reseed") else []
    for name in SUMMED:
        total = Counter()
        for o in ok:
            total.update(o["res"][name])
        res[name] = dict(sorted(total.items()))
    touch = Counter()
    for o in ok:
        for coin, d in o["res"]["touch_errors"].items():
            touch[(coin, "n")] += d["n"]
            touch[(coin, "exact")] += d["exact"]
    res["touch_errors"] = {f"{c}:{k}": v for (c, k), v in sorted(touch.items())}
    return res, rows


def _run_all(chunks, hl_data, policy_name, variants, jobs) -> list[list[dict]]:
    """[chunk][variant] outputs. Each worker gets a contiguous run of chunks so neighbours share
    cached recorder hours."""
    job_list = [(str(c), str(hl_data), policy_name, variants) for c in chunks]
    if jobs > 1 and len(job_list) > 1:
        size = -(-len(job_list) // jobs)
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            return list(ex.map(_run_one, job_list, chunksize=size))   # map keeps input order
    return [_run_one(j) for j in job_list]


def _pool_variant(outs, chunks, skipped, kw, policy_name):
    res, rows = pool(outs, skipped, kw, policy_name)
    per_chunk = [chunk_row(o, _meta(c)) for o, c in zip(outs, chunks)]
    return res, rows, per_chunk


def run_variants(chunks, hl_data, policy_name="listing", variants=(("base", {}),), out_dir=None, jobs=1,
                 skipped=()) -> dict[str, tuple[dict, list, list]]:
    """Every chunk under every variant ([(name, replay kwargs)]) in one pass: {name: (pooled result,
    pooled rows, per-chunk rows)}. With out_dir: one CSV folder per variant plus compare.csv."""
    chunks, skipped, variants = [Path(c) for c in chunks], list(skipped), list(variants)
    names = [n for n, _ in variants]
    if len(set(names)) != len(names):
        raise ValueError(f"variant names must be unique: {names}")
    grid = _run_all(chunks, hl_data, policy_name, variants, jobs)
    got = {name: _pool_variant([g[i] for g in grid], chunks, skipped, kw, policy_name)
           for i, (name, kw) in enumerate(variants)}
    if out_dir is not None:
        out_dir = Path(out_dir)
        for name, (res, rows, per_chunk) in got.items():
            write_csvs(out_dir / name, res, rows, per_chunk + _skipped_rows(skipped))
        write_compare(out_dir / "compare.csv", got)
    return got


def run_batch(chunks, hl_data, policy_name="listing", out_dir=None, jobs=1, skipped=(), **kw):
    """Run every chunk under one set of replay options and pool them. Returns (pooled result, pooled
    rows, per-chunk rows). `skipped` ([(folder, why)], from find_chunks) only feeds the counts and chunks.csv."""
    chunks, skipped = [Path(c) for c in chunks], list(skipped)
    outs = [g[0] for g in _run_all(chunks, hl_data, policy_name, [("", kw)], jobs)]
    res, rows, per_chunk = _pool_variant(outs, chunks, skipped, kw, policy_name)
    if out_dir is not None:
        write_csvs(Path(out_dir), res, rows, per_chunk + _skipped_rows(skipped))
    return res, rows, per_chunk


def _skipped_rows(skipped):
    return [{"chunk": d.name, "status": f"skipped: {why}"} for d, why in skipped]


# ------------------------------------------------------------------ output

def write_csvs(out: Path, res: dict, rows: list, chunk_rows: list) -> None:
    out.mkdir(parents=True, exist_ok=True)

    def write(name, header, data):
        with open(out / name, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(data)

    scalars = [(k, v) for k, v in res.items() if not isinstance(v, (dict, list))]
    for name in ("gate_stats", "resolver_stats", "touch_reconciled"):
        scalars += [(f"{name.split('_')[0]}:{k}", v) for k, v in res[name].items()]
    scalars += [(f"touch:{k}", v) for k, v in res["touch_errors"].items()]
    write("summary.csv", ["metric", "value"], scalars)
    write("per_asset.csv", ["scope", *SCOPE_METRICS],
          [[scope, *(res[f"{scope}:{m}"] for m in SCOPE_METRICS)] for scope in ("all", *COINS)])
    hz = res["horizons"]
    write("horizon.csv", list(hz[0]) if hz else ["horizon"], [list(r.values()) for r in hz])
    header = list(chunk_rows[0]) if chunk_rows and chunk_rows[0]["status"] in ("ok", "failed") else ["chunk", "status"]
    write("chunks.csv", header, [[r.get(k, "") for k in header] for r in chunk_rows])
    write("txs.csv", list(rows[0]) if rows else ["chunk", "hash"], [list(r.values()) for r in rows])
    total = sum(res["mismatch_table"].values())
    write("mismatches.csv", ["category", *COINS, "total", "share", "flagged (any precedence)"],
          [[cat, *(res["mismatch_table"][(cat, c)] for c in COINS),
            sum(res["mismatch_table"][(cat, c)] for c in COINS),
            vr.rate(sum(res["mismatch_table"][(cat, c)] for c in COINS), total),
            res["flag_table"].get(cat, "")] for cat in vr.CATEGORIES])
    write("alo_truth.csv", ["engine_decision", "truth", "n"],
          [[e, t, n] for (e, t), n in sorted(res["alo_confusion"].items())])


COMPARE_METRICS = ("scored", "matched", "match_rate", "match_rate_px_sz", "fill_recall", "fill_precision",
                   "fill_f1", "fill_recall_px_sz", "engine_only_txs", "match_rate_full", "fill_recall_full",
                   "fill_precision_full")


def write_compare(path: Path, got: dict) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["variant", "scope", *COMPARE_METRICS])
        for name, (res, _, _) in got.items():
            for scope in ("all", *COINS):
                w.writerow([name, scope, *(res[f"{scope}:{m}"] for m in COMPARE_METRICS)])


def print_compare(got: dict) -> None:
    """Strict (pre-re-seed makers as wildcards), full strict (no wildcards: an engine placeholder fill never
    matches) and per-fill notional recall/precision, per variant and scope."""
    print(f"{'variant':<34} {'scope':<4} {'tx strict':>13} {'tx full':>7} {'recall':>7} {'recall full':>11} "
          f"{'precision':>9} {'prec full':>9} {'eng-only':>8}")
    for name, (res, _, _) in got.items():
        for scope in ("all", *COINS):
            g = lambda m: res[f"{scope}:{m}"] or "n/a"
            print(f"{name:<34} {scope:<4} {res[f'{scope}:matched']:>4}/{res[f'{scope}:scored']:<4}{g('match_rate'):>5} "
                  f"{g('match_rate_full'):>7} {g('fill_recall'):>7} {g('fill_recall_full'):>11} "
                  f"{g('fill_precision'):>9} {g('fill_precision_full'):>9} {res[f'{scope}:engine_only_txs']:>8}")


def print_report(res: dict, per_chunk: list) -> None:
    print(f"chunks: {res['chunks_ok']} ok, {res['chunks_failed']} failed, {res['chunks_skipped']} skipped; "
          f"blocks {res['blocks']}")
    for c in per_chunk:
        if c["status"] == "failed":
            print(f"  FAILED {c['chunk']}: {c['error']}")
    print("options: " + "  ".join(f"{k} {res.get(k, '') if res.get(k, '') != '' else '-'}" for k in SETTINGS))
    print(f"G1 target ({res['g1_target_rule']}): {res['g1_target'] or 'n/a'}")
    for scope in ("all", *COINS):
        print(f"  {scope:<4} PER-FILL STRICT recall {res[f'{scope}:fill_recall'] or 'n/a'}  "
              f"precision {res[f'{scope}:fill_precision'] or 'n/a'}  F1 {res[f'{scope}:fill_f1'] or 'n/a'}  "
              f"(px/size recall {res[f'{scope}:fill_recall_px_sz'] or 'n/a'})  "
              f"txs matched {res[f'{scope}:matched']}/{res[f'{scope}:scored']} ({res[f'{scope}:match_rate'] or 'n/a'})")
    print(f"  excluded (real maker pre-window): {res['excluded_real_maker_prewindow']}; warm-up txs: {res['warmup_txs']}")
    total = sum(res["mismatch_table"].values())
    print("\nmismatch table (pooled)")
    for cat in vr.CATEGORIES:
        n = sum(res["mismatch_table"][(cat, c)] for c in COINS)
        print(f"  {cat:<34}" + "".join(f"{c} {res['mismatch_table'][(cat, c)]:>5}  " for c in COINS)
              + f"total {n:>5} ({vr.rate(n, total) or 'n/a'})")
    print(f"\nALO engine vs truth: {res['alo_confusion'] or 'none'}")
    if res["horizons"]:
        print("\nby time since re-seed:")
        for h in res["horizons"]:
            print(f"  {h['horizon']:>6}  match {h['matched']:>4}/{h['scored']:<5} ({h['match_rate'] or 'n/a'})  "
                  f"fill recall {h['fill_recall'] or 'n/a'} (px/size {h['fill_recall_px_sz'] or 'n/a'})")


def _join_variant_values(argv: list[str]) -> list[str]:
    """`--variant --touch-reconcile` -> `--variant=--touch-reconcile` (argparse would read the value as an option)."""
    out, it = [], iter(argv)
    for tok in it:
        if tok == "--variant":
            out.append("--variant=" + next(it, ""))
        else:
            out.append(tok)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="chunk folders, or folders of them (e.g. windows/)")
    ap.add_argument("--out", help="directory for the CSVs (default: report/validate/chunks-<policy><options>)")
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                    help=f"chunks replayed in parallel processes (default {DEFAULT_JOBS}; 1 = one process)")
    ap.add_argument("--variant", action="append", default=[], metavar="OPTIONS",
                    help="replay options added to the base options, e.g. --variant '--touch-reconcile'; "
                         "repeat to compare several configurations in one pass ('' = the base options)")
    vr.add_replay_args(ap)
    a = ap.parse_args(_join_variant_values(sys.argv[1:] if argv is None else list(argv)))
    chunks, skipped = find_chunks(a.paths)
    if not chunks:
        raise SystemExit(f"no complete chunk folders under {' '.join(a.paths)} ({len(skipped)} skipped)")
    if not a.variant:
        out = a.out or f"report/validate/chunks-{a.policy}{vr.options_tag(a)}"
        res, _, per_chunk = run_batch(chunks, a.hl_data, a.policy, out, a.jobs, skipped, **vr.replay_kwargs(a))
        print_report(res, per_chunk)
        print(f"\nCSVs in {out}")
        return
    vp = argparse.ArgumentParser(prog="--variant", argument_default=argparse.SUPPRESS)
    vr.add_replay_args(vp, hl_data_required=False)
    variants = []
    for v in a.variant:
        ns = vp.parse_args(shlex.split(v), namespace=copy.copy(a))
        variants.append((vr.options_tag(ns).lstrip("-") or "base", vr.replay_kwargs(ns)))
    names = [n for n, _ in variants]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise SystemExit(f"two variants have the same options: {', '.join(dup)}")
    out = a.out or f"report/validate/chunks-{a.policy}{vr.options_tag(a)}-compare"
    got = run_variants(chunks, a.hl_data, a.policy, variants, out, a.jobs, skipped)
    first = next(iter(got.values()))[0]
    print(f"chunks: {first['chunks_ok']} ok, {first['chunks_failed']} failed, {first['chunks_skipped']} skipped; "
          f"blocks {first['blocks']}")
    for name, (res, _, per_chunk) in got.items():
        for c in per_chunk:
            if c["status"] == "failed":
                print(f"  FAILED {name} {c['chunk']}: {c['error']}")
    print()
    print_compare(got)
    print(f"\nCSVs in {out}/<variant>/, comparison in {out}/compare.csv")


if __name__ == "__main__":
    main()
