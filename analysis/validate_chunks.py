"""Batch validation over many chunk folders: validate_replay on each chunk, rows pooled into one report.

    python -m analysis.validate_chunks <windows_dir_or_chunk_dir>... --hl-data <dir> [--out DIR] [--jobs N]
                                       [same replay options as validate_replay, e.g.
                                        --alo-gate touch --reseed --nearest-ratio 3 --touch-reconcile]

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
import csv
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from analysis import validate_replay as vr
from engine_py.replay import COINS

SETTINGS = ("policy", "outside_fill_position", "resolver", "alo_gate", "gate_max_age_ms", "evict_known",
            "nearest_ratio", "reseed", "touch_reconcile")
SCOPE_METRICS = ("scored", "matched", "match_rate", "match_rate_px_sz", "match_rate_real_linked_only",
                 "engine_only_txs", "fill_recall", "fill_precision", "fill_f1", "fill_recall_px_sz",
                 "fill_precision_px_sz")
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

def _run_one(job) -> dict:
    """One chunk -> {"chunk", "status": ok|failed, "res", "rows", "error"}. Top level so it pickles."""
    chunk, hl_data, policy_name, kw = job
    try:
        res, rows = vr.run(chunk, hl_data, policy_name, out_dir=None, quiet=True, **kw)
    except Exception as e:                       # recorded per chunk and reported, never dropped
        return {"chunk": Path(chunk).name, "status": "failed", "res": None, "rows": [],
                "error": f"{type(e).__name__}: {e}"}
    return {"chunk": Path(chunk).name, "status": "ok", "res": res, "rows": rows, "error": ""}


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


def run_batch(chunks, hl_data, policy_name="listing", out_dir=None, jobs=1, skipped=(), **kw):
    """Run every chunk and pool them. Returns (pooled result, pooled rows, per-chunk rows).
    `skipped` ([(folder, why)], from find_chunks) only feeds the counts and chunks.csv."""
    chunks, skipped = [Path(c) for c in chunks], list(skipped)
    job_list = [(str(c), str(hl_data), policy_name, kw) for c in chunks]
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            outs = list(ex.map(_run_one, job_list))      # map keeps input order
    else:
        outs = [_run_one(j) for j in job_list]
    res, rows = pool(outs, skipped, kw, policy_name)
    per_chunk = [chunk_row(o, _meta(c)) for o, c in zip(outs, chunks)]
    if out_dir is not None:
        skipped_rows = [{"chunk": d.name, "status": f"skipped: {why}"} for d, why in skipped]
        write_csvs(Path(out_dir), res, rows, per_chunk + skipped_rows)
    return res, rows, per_chunk


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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="chunk folders, or folders of them (e.g. windows/)")
    ap.add_argument("--out", help="directory for the CSVs (default: report/validate/chunks-<policy><options>)")
    ap.add_argument("--jobs", type=int, default=1, help="chunks replayed in parallel processes")
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    chunks, skipped = find_chunks(a.paths)
    if not chunks:
        raise SystemExit(f"no complete chunk folders under {' '.join(a.paths)} ({len(skipped)} skipped)")
    out = a.out or f"report/validate/chunks-{a.policy}{vr.options_tag(a)}"
    res, _, per_chunk = run_batch(chunks, a.hl_data, a.policy, out, a.jobs, skipped, **vr.replay_kwargs(a))
    print_report(res, per_chunk)
    print(f"\nCSVs in {out}")


if __name__ == "__main__":
    main()
