"""What does a lead-in buy the int-oid resolver? (measurement only; engine_py, data and the other analysis files are untouched)

    python -m analysis.resolver_report chunks WINDOWS_DIR --hl-data HL_DATA [--out DIR] [--jobs 2] [--nearest-ratio 3]
                                              [--drops 0,50,100,150]
    python -m analysis.resolver_report sim FULL_WINDOW --hl-data HL_DATA [--out DIR] [--jobs 2] [--leads 0,50,100,150]
                                              [--scored-blocks 30] [--spacing 180]

`chunks` replays every complete lead-in chunk (window.json lead_in_blocks > 0) under these variants, all with
reseed + keep_known (the lead-in configuration) and the same resolver settings:
  lead_in     the chunk as captured (lead-in blocks replayed, unscored)
  skip_lead   Replay(skip_lead_in=True): only the scored blocks are replayed. The oid estimator still reads every
              block file on disk, so it can locate anchors in the lead-in even though those orders are not replayed
  drop<N>     a filtered copy of the window without the first N lead-in blocks (block files symlinked into --out,
              window.json lead_in_blocks and anchors.json adjusted, scored blocks identical). drop150 is what a plain
              chunk without lead-in looks like (estimator included). Nothing in engine_py/replay.py is involved.
Per chunk and variant it reports the resolver outcomes of the WHOLE replay (Replay.resolver.stats, lead-in lookups
included) and of the SCORED part only (blocks >= warmup_end_height), the anchors the estimator located, and the
validate_replay mismatch counts, in particular the "unresolved cancel" category.

Target period of int-oid cancels (scored part): the chunk's own orderStatus anchors give pairs (real oid, order
timestamp), even when the anchored order was placed before the window and so cannot be located. The oid at a block
time t is ESTIMATED as O_a + rate * (t - T_a) for the anchor nearest in time, with `rate` the median ids per second over
chunks that have two anchors at least 3 s apart; a band of 500 ids + 15% of the extrapolated distance marks "uncertain".
Every target-period number is an estimate built that way, never a measurement.

`sim` answers "how much lead-in would a chunk need if its anchors were locatable": it cuts synthetic chunks from the
full window (same scored blocks, lead-in of L blocks before them), keeps the full window's real located anchors that fall
inside the chunk, and reports scored-part resolver outcomes by L. The anchors are the full window's (collected over
6,100 blocks), NOT the 2-per-chunk anchors the chunk capture asks for.

Outputs (CSV + resolver_report.md, in --out, default report/validate/resolver/<mode>): see write_* below.
Everything is integer arithmetic except the percentages printed in the tables.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import statistics
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from analysis import validate_replay as vr
from data.oids import OidEstimator
from data.parse import _block_files
from engine_py.replay import Observer, Replay, listing
from engine_py.types import Cancel, Modify

OUTCOMES = ("resolved", "no_candidate", "ambiguous", "prewindow")     # resolved_nearest is a subset of resolved
CLASSES = ("before_lead_in", "uncertain_lead_in_start", "lead_in", "uncertain_scored_start", "scored_part",
           "unclassified")
BAND_BASE, BAND_PCT = 500, 15
MIN_PAIR_GAP_MS = 3000


# ------------------------------------------------------------------ window cropping

def crop_window(src, dst, first_height: int, *, last_height: int | None = None, lead_in_blocks: int | None = None,
                warmup_end_height: int | None = None, anchors: list | None = None) -> Path:
    """A window folder at `dst` holding src's blocks first_height..last_height as symlinks (never a copy of data,
    never a write into src), a window.json adjusted to match and an anchors.json (src's own unless `anchors`)."""
    src, dst = Path(src), Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    kept = [(h, p) for h, p in _block_files(src) if h >= first_height and (last_height is None or h <= last_height)]
    if not kept:
        raise ValueError(f"{src}: no blocks from {first_height}")
    for h, p in kept:
        os.symlink(p.resolve(), dst / p.name)
    meta = json.loads((src / "window.json").read_text()) if (src / "window.json").exists() else {}
    meta.update(start_height=kept[0][0], end_height=kept[-1][0], cropped_from=src.name,
                kind="chunk", status="complete")
    if warmup_end_height is not None:
        meta["warmup_end_height"] = warmup_end_height
    if lead_in_blocks is not None:
        meta["lead_in_blocks"] = lead_in_blocks
    (dst / "window.json").write_text(json.dumps(meta))
    if anchors is not None:
        (dst / "anchors.json").write_text(json.dumps(anchors))
    elif (src / "anchors.json").exists():
        shutil.copyfile(src / "anchors.json", dst / "anchors.json")
    return dst


# ------------------------------------------------------------------ the observer

def int_target(action) -> int | None:
    """The Hyperliquid oid an action asks to cancel, if it is a plain int (cloid targets do not use the resolver)."""
    if isinstance(action, Cancel):
        return action.oid
    if isinstance(action, Modify) and isinstance(action.target, int) and not isinstance(action.target, bool):
        return action.target
    return None


class CancelObserver(Observer):
    """One row per int-oid cancel/modify target in the scored part: how the resolver answered, and (if it
    resolved) which block the resolved order was placed in."""

    def __init__(self, warmup_end: int):
        self.warmup_end = warmup_end
        self.rows: list[dict] = []
        self._pre: Counter | None = None

    def before_action(self, replay, block, action):
        self._pre = Counter(replay.resolver.stats) if replay.resolver is not None and int_target(action) is not None \
            else None

    def after_action(self, replay, block, action, fills, rejects, created):
        oid = int_target(action)
        if oid is None or block.height < self.warmup_end:
            return
        outcome, target_block = "no_resolver", None
        if self._pre is not None:
            diff = Counter(replay.resolver.stats)
            diff.subtract(self._pre)
            hit = [k for k in OUTCOMES if diff[k] > 0]
            outcome = hit[0] if len(hit) == 1 else "not_looked_up"
            if outcome == "resolved":
                target_block = replay.placed_by[replay.resolver.last].block
        self.rows.append({"height": block.height, "time_ms": block.time_ms, "hl_oid": oid, "outcome": outcome,
                          "target_block": target_block})


# ------------------------------------------------------------------ oid <-> time estimate from anchors

def anchor_points(anchors: list) -> list[tuple[int, int]]:
    """(real oid, order timestamp ms) of every orderStatus answer that returned an order."""
    pts = []
    for a in anchors:
        ans = a.get("answer", {})
        if ans.get("status") == "order":
            o = ans["order"]["order"]
            pts.append((int(o["oid"]), int(o["timestamp"])))
    return sorted(set(pts))


def pooled_rate_per_s(point_lists: list[list[tuple[int, int]]], min_gap_ms: int = MIN_PAIR_GAP_MS) -> int | None:
    """Median ids per second over every chunk's anchor pairs at least min_gap_ms apart (integer; None if no pair)."""
    rates = []
    for pts in point_lists:
        by_t = sorted(pts, key=lambda p: p[1])
        for (o0, t0), (o1, t1) in zip(by_t, by_t[1:]):
            if t1 - t0 >= min_gap_ms and o1 > o0:
                rates.append((o1 - o0) * 1000 // (t1 - t0))
    return int(statistics.median(rates)) if rates else None


class TargetPeriod:
    """Estimated placement period of an int oid in one chunk (see module docstring). All estimates."""

    def __init__(self, points, rate_per_s, lead_start_ms, scored_start_ms):
        self.points, self.rate = points, rate_per_s
        self.bounds = {}
        if points and rate_per_s:
            for name, t in (("lead", lead_start_ms), ("scored", scored_start_ms)):
                self.bounds[name] = self.oid_at(t)

    def oid_at(self, t_ms: int) -> tuple[int, int]:
        """(estimated oid, band) at block time t_ms."""
        o, ta = min(self.points, key=lambda p: (abs(t_ms - p[1]), p))
        est = o + self.rate * (t_ms - ta) // 1000
        return est, BAND_BASE + abs(est - o) * BAND_PCT // 100

    def classify(self, hl_oid: int) -> str:
        if not self.bounds:
            return "unclassified"
        (b0, w0), (b1, w1) = self.bounds["lead"], self.bounds["scored"]
        if hl_oid < b0 - w0:
            return "before_lead_in"
        if hl_oid <= b0 + w0:
            return "uncertain_lead_in_start"
        if hl_oid < b1 - w1:
            return "lead_in"
        if hl_oid <= b1 + w1:
            return "uncertain_scored_start"
        return "scored_part"


# ------------------------------------------------------------------ one replay

def replay_variant(window, hl_data, nearest_ratio: int, *, skip_lead_in: bool = False) -> tuple[Replay, CancelObserver]:
    rp = Replay(window, hl_data, listing, reseed=True, keep_known=True, nearest_ratio=nearest_ratio,
                skip_lead_in=skip_lead_in)
    obs = CancelObserver(rp.warmup_end_height)
    rp.observer = obs
    for _ in rp:
        pass
    return rp, obs


def summarize_replay(rp: Replay, obs: CancelObserver) -> dict:
    whole = dict(rp.resolver.stats) if rp.resolver is not None else {}
    scored = Counter(r["outcome"] for r in obs.rows)
    return {"located": len(rp.resolver.est.anchors) if rp.resolver is not None else 0,
            "whole": whole, "scored": dict(scored), "scored_lookups": len(obs.rows),
            "resolved_nearest": whole.get("resolved_nearest", 0)}


def category_counts(window, hl_data, nearest_ratio: int, skip_lead_in: bool) -> dict:
    res, rows = vr.run(window, hl_data, "listing", out_dir=None, quiet=True, reseed=True, keep_known=True,
                       nearest_ratio=nearest_ratio, skip_lead_in=skip_lead_in)
    scored = vr.scored_rows(rows)
    table, flags = vr.mismatch_tables(scored)
    mism = sum(r["status"] == "mismatch" for r in scored)
    return {"scored_txs": len(scored), "matched": sum(r["status"] == "match" for r in scored),
            "matched_full": sum(r["match_full"] for r in scored), "mismatched": mism,
            "first_category": {c: sum(table[(c, k)] for k in vr.COINS) for c in vr.CATEGORIES},
            "flagged": dict(flags)}


# ------------------------------------------------------------------ chunks mode

def _chunk_job(job) -> dict:
    chunk, hl_data, out_root, nearest_ratio, drops, rate = job
    chunk = Path(chunk)
    meta = json.loads((chunk / "window.json").read_text())
    anchors = json.loads((chunk / "anchors.json").read_text()) if (chunk / "anchors.json").exists() else []
    lead = int(meta["lead_in_blocks"])
    out: dict = {"chunk": chunk.name, "lead_in_blocks": lead, "anchors_asked": meta.get("anchors_asked", len(anchors)),
                 "anchors_found": meta.get("anchors_found", ""), "variants": {}}
    runs = {"lead_in": (chunk, False), "skip_lead": (chunk, True)}
    for d in drops:
        if 0 < d <= lead:
            runs[f"drop{d}"] = (crop_window(chunk, Path(out_root) / "crops" / f"{chunk.name}-drop{d}",
                                            meta["start_height"] + d, lead_in_blocks=lead - d), False)
    for name, (win, skip) in runs.items():
        rp, obs = replay_variant(win, hl_data, nearest_ratio, skip_lead_in=skip)
        v = summarize_replay(rp, obs)
        if name == "lead_in":
            pts = anchor_points(anchors)
            t_lead = rp.blocks[0].time_ms
            t_scored = next(b.time_ms for b in rp.blocks if b.height >= rp.warmup_end_height)
            tp = TargetPeriod(pts, rate, t_lead, t_scored)
            out["bounds"] = {k: list(x) for k, x in tp.bounds.items()}
            out["anchor_ages_s"] = [(t_lead - t) / 1000 for _, t in pts]
        v["category"] = category_counts(win, hl_data, nearest_ratio, skip)
        out["variants"][name] = v
        # per scored cancel: estimated period x outcome (same cancels in every variant)
        cls = Counter((tp.classify(r["hl_oid"]), r["outcome"]) for r in obs.rows)
        v["by_class"] = {f"{c}|{o}": n for (c, o), n in sorted(cls.items())}
        v["resolved_target"] = dict(Counter("lead_in" if r["target_block"] < rp.warmup_end_height else "scored_part"
                                            for r in obs.rows if r["outcome"] == "resolved"))
    return out


def run_chunks(windows, hl_data, out, nearest_ratio=3, drops=(50, 100, 150), jobs=2) -> dict:
    from analysis.validate_chunks import find_chunks
    chunks, skipped = find_chunks([windows])
    chunks = [c for c in chunks if int(json.loads((c / "window.json").read_text()).get("lead_in_blocks", 0)) > 0]
    if not chunks:
        raise SystemExit("no complete lead-in chunks")
    pt = [anchor_points(json.loads((c / "anchors.json").read_text())) if (c / "anchors.json").exists() else []
          for c in chunks]
    rate = pooled_rate_per_s(pt)
    out = Path(out)
    jobs_list = [(str(c), str(hl_data), str(out), nearest_ratio, tuple(drops), rate) for c in chunks]
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            results = list(ex.map(_job_safe, jobs_list))
    else:
        results = [_job_safe(j) for j in jobs_list]
    return {"results": results, "skipped": [(d.name, why) for d, why in skipped], "rate_per_s": rate,
            "nearest_ratio": nearest_ratio, "drops": list(drops)}


def _job_safe(job):
    try:
        return _chunk_job(job)
    except Exception as e:                      # recorded and reported, never dropped
        return {"chunk": Path(job[0]).name, "error": f"{type(e).__name__}: {e}", "variants": {}}


# ------------------------------------------------------------------ sim mode

def _sim_job(job) -> dict:
    full, hl_data, out_root, scored_first, scored_blocks, leads, anchors, nearest_ratio = job
    out = {"scored_first": scored_first, "leads": {}}
    heights = [h for h, _ in _block_files(Path(full))]
    avail = set(heights)
    for L in leads:
        first = scored_first - L
        if first not in avail or scored_first + scored_blocks - 1 not in avail:
            continue
        win = crop_window(full, Path(out_root) / "crops" / f"{scored_first}-lead{L}", first,
                          last_height=scored_first + scored_blocks - 1, lead_in_blocks=L,
                          warmup_end_height=scored_first, anchors=anchors)
        rp, obs = replay_variant(win, hl_data, nearest_ratio)
        out["leads"][L] = summarize_replay(rp, obs)
        shutil.rmtree(win)                      # symlinks only; keeps report/ small
    return out


def run_sim(full, hl_data, out, leads=(0, 50, 100, 150), scored_blocks=30, spacing=180, nearest_ratio=3, jobs=2):
    full = Path(full)
    heights = [h for h, _ in _block_files(full)]
    anchors = json.loads((full / "anchors.json").read_text())
    starts = list(range(heights[0] + max(leads), heights[-1] - scored_blocks + 2, spacing))
    jl = [(str(full), str(hl_data), str(out), s, scored_blocks, tuple(leads), anchors, nearest_ratio) for s in starts]
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            results = list(ex.map(_sim_safe, jl))
    else:
        results = [_sim_safe(j) for j in jl]
    full_est = OidEstimator.from_files(full, full / "anchors.json")
    return {"results": results, "leads": list(leads), "scored_blocks": scored_blocks, "spacing": spacing,
            "full_anchors_located": len(full_est.anchors), "nearest_ratio": nearest_ratio}


def _sim_safe(job):
    try:
        return _sim_job(job)
    except Exception as e:
        return {"scored_first": job[3], "error": f"{type(e).__name__}: {e}", "leads": {}}


# ------------------------------------------------------------------ pooling and output

def pct(n, d) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def pool_variant(results, variant) -> dict:
    tot = {"located": 0, "scored": Counter(), "whole": Counter(), "lookups": 0, "chunks": 0, "with_located": 0}
    for r in results:
        v = r.get("variants", {}).get(variant)
        if v is None:
            continue
        tot["chunks"] += 1
        tot["located"] += v["located"]
        tot["with_located"] += v["located"] > 0
        tot["scored"].update(v["scored"])
        tot["whole"].update(v["whole"])
        tot["lookups"] += v["scored_lookups"]
    return tot


def _write(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def write_chunks(data: dict, out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    res = [r for r in data["results"] if "error" not in r]
    failed = [r for r in data["results"] if "error" in r]
    names = ["lead_in", "skip_lead", *[f"drop{d}" for d in data["drops"]]]
    names = [n for n in names if any(n in r["variants"] for r in res)]
    md = [f"# Lead-in effect on the int-oid resolver (chunks)\n",
          f"chunks ok {len(res)}, failed {len(failed)}, skipped {len(data['skipped'])}; nearest_ratio "
          f"{data['nearest_ratio']}; pooled oid rate {data['rate_per_s']} ids/s (estimate)\n"]
    for r in failed:
        md.append(f"- FAILED {r['chunk']}: {r['error']}")
    # per chunk
    header = ["chunk", "anchors_asked", "anchors_found"]
    for n in names:
        header += [f"{n}:located", f"{n}:whole_lookups", f"{n}:whole_resolved", f"{n}:scored_lookups",
                   f"{n}:scored_resolved", f"{n}:scored_no_candidate", f"{n}:scored_ambiguous",
                   f"{n}:scored_prewindow"]
    rows = []
    for r in res:
        row = [r["chunk"], r["anchors_asked"], r["anchors_found"]]
        for n in names:
            v = r["variants"].get(n)
            if v is None:
                row += [""] * 8
                continue
            row += [v["located"], sum(v["whole"].get(k, 0) for k in OUTCOMES), v["whole"].get("resolved", 0),
                    v["scored_lookups"], *(v["scored"].get(k, 0) for k in OUTCOMES)]
        rows.append(row)
    _write(out / "per_chunk.csv", header, rows)
    # pooled outcomes
    prow = []
    md += ["\n## 1. Resolver outcomes, pooled over chunks\n",
           "`whole` = every lookup of the replay (lead-in lookups included for lead_in); `scored` = blocks >= "
           "warmup_end_height only.\n",
           "| variant | chunks | chunks with a located anchor | anchors located | whole lookups | whole resolved | "
           "scored lookups | resolved | no_candidate | ambiguous | prewindow |", "|" + "---|" * 11]
    for n in names:
        t = pool_variant(res, n)
        wl = sum(t["whole"].get(k, 0) for k in OUTCOMES)
        row = [n, t["chunks"], t["with_located"], t["located"], wl, t["whole"].get("resolved", 0), t["lookups"],
               *(t["scored"].get(k, 0) for k in OUTCOMES)]
        prow.append(row)
        md.append("| " + " | ".join(str(x) for x in [row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                                                      *(f"{row[7 + i]} ({pct(row[7 + i], t['lookups'])})"
                                                        for i in range(4))]) + " |")
    _write(out / "pooled_outcomes.csv", ["variant", "chunks", "chunks_with_located", "anchors_located",
                                         "whole_lookups", "whole_resolved", "scored_lookups", *OUTCOMES], prow)
    # target period
    md += ["\n## 2. Scored-part int-oid cancels by ESTIMATED target placement (lead_in variant outcomes)\n",
           "Estimate: anchor (oid, timestamp) nearest in time + pooled ids/s; see module docstring. "
           "`uncertain_*` = within the estimate band of a boundary.\n",
           "| estimated target period | cancels | share | resolved | no_candidate | ambiguous | prewindow |",
           "|" + "---|" * 7]
    by = Counter()
    for r in res:
        for key, n in r["variants"]["lead_in"]["by_class"].items():
            by[tuple(key.split("|"))] += n
    tot = sum(by.values())
    crow = []
    for c in CLASSES:
        n = sum(v for (cc, _), v in by.items() if cc == c)
        outs = [by.get((c, o), 0) for o in OUTCOMES]
        crow.append([c, n, *outs])
        md.append(f"| {c} | {n} | {pct(n, tot)} | " + " | ".join(str(x) for x in outs) + " |")
    _write(out / "target_period.csv", ["period", "cancels", *OUTCOMES], crow)
    before = sum(v for (cc, _), v in by.items() if cc == "before_lead_in")
    known = tot - sum(v for (cc, _), v in by.items() if cc == "unclassified")
    md.append(f"\nPlaced before the lead-in starts (estimate; no replay of this chunk can hold them): {before} of "
              f"{known} classified cancels ({pct(before, known)}); at or after the lead-in start: {known - before} "
              f"({pct(known - before, known)}).")
    rt = Counter()
    for r in res:
        for n in names:
            rt.update({(n, k): v for k, v in r["variants"][n].get("resolved_target", {}).items()})
    md.append(f"\nResolved cancels by exact target block (resolver match, not an estimate): "
              f"{dict(sorted(rt.items())) or 'none'}\n")
    # anchor ages
    ages = sorted(a for r in res for a in r.get("anchor_ages_s", []))
    if ages:
        md += ["## Anchor targets: age at the first lead-in block (s; negative = placed before the lead-in starts)\n",
               f"{len(ages)} answered anchors; quartiles (s) {ages[len(ages) // 4]:.1f} / {ages[len(ages) // 2]:.1f} / "
               f"{ages[3 * len(ages) // 4]:.1f}. Anchors in the first blocks of a chunk target orders placed before "
               f"the chunk by construction; located anchors (lead_in): "
               f"{sum(r['variants']['lead_in']['located'] for r in res)}.\n"]
        _write(out / "anchor_ages.csv", ["age_s_before_lead_in_start"], [[f"{a:.3f}"] for a in ages])
    # categories
    md += ["## 3. Mismatch categories (validate_replay definitions, scored txs, pooled)\n",
           "| variant | scored txs | matched | matched (full) | mismatched | first category 'unresolved cancel' | "
           "flagged 'unresolved cancel' (any precedence) |", "|" + "---|" * 7]
    krow = []
    for n in names:
        agg = Counter()
        for r in res:
            c = r["variants"][n]["category"]
            agg["scored_txs"] += c["scored_txs"]
            agg["matched"] += c["matched"]
            agg["matched_full"] += c["matched_full"]
            agg["mismatched"] += c["mismatched"]
            agg["first"] += c["first_category"].get("unresolved cancel", 0)
            agg["flag"] += c["flagged"].get("unresolved cancel", 0)
        krow.append([n, *(agg[k] for k in ("scored_txs", "matched", "matched_full", "mismatched", "first", "flag"))])
        md.append(f"| {n} | " + " | ".join(str(agg[k]) for k in
                                           ("scored_txs", "matched", "matched_full", "mismatched", "first", "flag"))
                  + " |")
    _write(out / "categories.csv", ["variant", "scored_txs", "matched", "matched_full", "mismatched",
                                    "first_category_unresolved_cancel", "flagged_unresolved_cancel"], krow)
    (out / "resolver_report.md").write_text("\n".join(md) + "\n")
    return "\n".join(md)


def write_sim(data: dict, out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    res = [r for r in data["results"] if "error" not in r]
    failed = [r for r in data["results"] if "error" in r]
    md = ["# Lead-in length vs resolution (synthetic chunks cut from the full window)\n",
          f"{len(res)} scored parts of {data['scored_blocks']} blocks every {data['spacing']} blocks; full window "
          f"located anchors {data['full_anchors_located']}; nearest_ratio {data['nearest_ratio']}\n"]
    for r in failed:
        md.append(f"- FAILED start {r['scored_first']}: {r['error']}")
    md += ["| lead-in blocks | chunks | chunks with a located anchor | anchors located | scored lookups | resolved | "
           "no_candidate | ambiguous | prewindow |", "|" + "---|" * 9]
    rows = []
    for L in data["leads"]:
        t = {"located": 0, "w": 0, "scored": Counter(), "lookups": 0, "n": 0}
        for r in res:
            v = r["leads"].get(L)
            if v:
                t["n"] += 1
                t["located"] += v["located"]
                t["w"] += v["located"] > 0
                t["scored"].update(v["scored"])
                t["lookups"] += v["scored_lookups"]
        s = t["scored"]
        rows.append([L, t["n"], t["w"], t["located"], t["lookups"], *(s.get(k, 0) for k in OUTCOMES)])
        md.append(f"| {L} | {t['n']} | {t['w']} | {t['located']} | {t['lookups']} | "
                  + " | ".join(f"{s.get(k, 0)} ({pct(s.get(k, 0), t['lookups'])})" for k in OUTCOMES) + " |")
    _write(out / "sim_by_lead.csv", ["lead_in_blocks", "chunks", "chunks_with_located", "anchors_located",
                                     "scored_lookups", *OUTCOMES], rows)
    (out / "resolver_report.md").write_text("\n".join(md) + "\n")
    return "\n".join(md)


def _ints(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in s.split(",") if x != "")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    for name in ("chunks", "sim"):
        p = sub.add_parser(name)
        p.add_argument("path", help="windows folder of lead-in chunks (chunks) or the full window folder (sim)")
        p.add_argument("--hl-data", required=True)
        p.add_argument("--out")
        p.add_argument("--jobs", type=int, default=2, help="processes (default 2: the machine is shared)")
        p.add_argument("--nearest-ratio", type=int, default=3)
    sub.choices["chunks"].add_argument("--drops", default="50,100,150", type=_ints)
    sub.choices["sim"].add_argument("--leads", default="0,50,100,150", type=_ints)
    sub.choices["sim"].add_argument("--scored-blocks", type=int, default=30)
    sub.choices["sim"].add_argument("--spacing", type=int, default=180)
    a = ap.parse_args(argv)
    out = Path(a.out or f"report/validate/resolver/{a.mode}")
    if a.mode == "chunks":
        data = run_chunks(a.path, a.hl_data, out, a.nearest_ratio, a.drops, a.jobs)
        print(write_chunks(data, out))
    else:
        data = run_sim(a.path, a.hl_data, out, a.leads, a.scored_blocks, a.spacing, a.nearest_ratio, a.jobs)
        print(write_sim(data, out))
    print(f"\nCSVs and resolver_report.md in {out}")


if __name__ == "__main__":
    main()
