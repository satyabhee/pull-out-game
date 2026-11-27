"""Gate 2 A6 (docs/gate2.md A6): ON, M1n, cancels-last and random (seeds 0..19) on the A3 primary blocks, one-block
mode, descriptive rows next to M1n.

usage: python -m analysis.gate2_policies run <blocks.csv> --out DIR --jobs N --hl-data H --send-times F <B3 flags>
       python -m analysis.gate2_policies score DIR [--primary DIR_OF_SIGNED_RUN]

run: one ON replay per chunk, then one OFF replay per listed block per policy, in the order ON, m1n, cancels_last,
random0..random19. Each replay is saved as it finishes (DIR/parts/), so a stopped run resumes and finished seeds can
be scored. score: per policy, DIR/<policy>/{results.pkl, meta.json} scored with the unchanged `analysis.gate2 score`;
the L61-style maker-strict contested row vs ON (listed blocks only); the seed mean and spread; the M1n check.
"""
from __future__ import annotations

import csv
import hashlib
import json
import pickle
import sys
from pathlib import Path

N_SEEDS = 20


def policies(n_seeds=N_SEEDS):
    return [("m1n", "m1n", None), ("cancels_last", "cancels_last", None)] + \
           [(f"random{s}", "random", s) for s in range(n_seeds)]


def _part_path(out: Path, tag: str, chunk: str) -> Path:
    return out / "parts" / f"{tag.replace(':', '_')}__{Path(chunk).name}.pkl"


def _work(job):
    """Run one arm and save it; OFF parts keep only their own block's tx rows (the L61 row reads those)."""
    from analysis.gate2 import _run_arm
    out, tag, chunk, hl_data, heights, kw = job
    p = _part_path(Path(out), tag, chunk)
    if p.exists():
        return tag, chunk, "cached"
    part = _run_arm((tag.split("|")[-1], chunk, hl_data, heights, kw))
    if part["arm"]["status"] == "ok" and tag != "on":
        part["arm"]["rows"] = [r for r in part["arm"]["rows"] if int(r["block"]) in set(heights)]
    part["policy_tag"] = tag
    tmp = p.with_suffix(".tmp")
    pickle.dump(part, open(tmp, "wb"))
    tmp.rename(p)
    return tag, chunk, part["arm"]["status"]


def run_main(argv):
    import argparse
    from concurrent.futures import ProcessPoolExecutor
    from analysis import validate_replay as vr
    from analysis.gate2 import _one_block_send_times, group_chunks
    ap = argparse.ArgumentParser()
    ap.add_argument("blocks")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=N_SEEDS)
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    if a.order != "listing" or not a.send_times:
        sys.exit("gate2_policies run: pass --send-times FILE and no --order (the runner sets each policy's order)")
    out = Path(a.out)
    (out / "parts").mkdir(parents=True, exist_ok=True)
    kw_on = vr.replay_kwargs(a)
    kws = {}
    for name, order, seed in policies(a.seeds):
        a.order, a.order_seed = order, seed
        kws[name] = vr.replay_kwargs(a)
    a.order, a.order_seed = "listing", None
    with open(a.blocks, newline="") as fh:
        by_chunk = group_chunks(list(csv.DictReader(fh)))
    meta = {"blocks_csv": a.blocks, "blocks_sha256": hashlib.sha256(open(a.blocks, "rb").read()).hexdigest(),
            "send_times": a.send_times, "send_times_sha256": hashlib.sha256(open(a.send_times, "rb").read()).hexdigest(),
            "chunks": len(by_chunk), "listed_blocks": sum(map(len, by_chunk.values())), "argv": argv,
            "mode": "one_block", "policies": [p[0] for p in policies(a.seeds)],
            "chunk_dirs": {Path(c).name: c for c in by_chunk}}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    jobs = [(str(out), "on", c, a.hl_data, sorted(hs), kw_on) for c, hs in sorted(by_chunk.items())]
    for name, _o, _s in policies(a.seeds):
        for c, hs in sorted(by_chunk.items()):
            for h in sorted(hs):
                st = _one_block_send_times(a.send_times, out / "send_times_by_height", h)
                jobs.append((str(out), f"{name}|off:{h}", c, a.hl_data, [h], dict(kws[name], send_times=str(st))))
    n = 0
    with ProcessPoolExecutor(a.jobs) as ex:
        for tag, chunk, status in ex.map(_work, jobs):
            n += 1
            if status not in ("ok", "cached"):
                print(f"FAILED {tag} {chunk}", flush=True)
            if n % 50 == 0:
                print(f"{n}/{len(jobs)} replays", flush=True)
    print(f"gate2_policies run: {len(jobs)} replays -> {out}")


def _load_parts(out: Path):
    parts = {}
    for p in sorted((out / "parts").glob("*.pkl")):
        part = pickle.load(open(p, "rb"))
        parts.setdefault(part["policy_tag"].split("|")[0], []).append(part)
    return parts


def _complete(name, parts, meta):
    need = meta["listed_blocks"] if name != "on" else meta["chunks"]
    return len(parts.get(name, [])) == need


def score_main(argv):
    from analysis import gate2
    from analysis.contested_paired import paired, per_tx, scope_of
    out = Path(argv[0])
    primary = Path(argv[argv.index("--primary") + 1]) if "--primary" in argv else None
    meta = json.loads((out / "meta.json").read_text())
    parts = _load_parts(out)
    if not _complete("on", parts, meta):
        sys.exit("gate2_policies score: ON is not complete")
    names = [p for p in meta["policies"] if _complete(p, parts, meta)]
    pending = [p for p in meta["policies"] if p not in names]
    lines = [f"gate2 A6 across policies: {out}", f"complete policies: {names}", f"pending: {pending}"]
    on_rows = [{"chunk": pt["chunk"], **r} for pt in parts["on"] if pt["arm"]["status"] == "ok"
               for r in pt["arm"]["rows"]]
    # chunk dirs for the contested scope (block files), by window name
    cdir = out / "l61_chunks"
    cdir.mkdir(exist_ok=True)
    for name, d in meta["chunk_dirs"].items():
        if not (cdir / name).exists():
            (cdir / name).symlink_to(d)
    from analysis.contested import load as load_txs

    def write_txs(variant, rows):
        d = out / "l61" / variant
        d.mkdir(parents=True, exist_ok=True)
        hdr = list(on_rows[0])
        with open(d / "txs.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=hdr, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return load_txs(d)

    ref_rows = write_txs("on", on_rows)
    scope = scope_of(ref_rows, cdir, False)
    keys_c = [k for k, c in scope.items() if c]
    ref = per_tx(ref_rows, "matched_notional_full")
    summary = {}
    for name in names:
        d = out / name
        d.mkdir(exist_ok=True)
        ps = [dict(pt, tag="on") for pt in parts["on"]] + \
             [dict(pt, tag=pt["policy_tag"].split("|")[1]) for pt in parts[name]]
        results = gate2._assemble_one_block(ps)
        pickle.dump(results, open(d / "results.pkl", "wb"))
        (d / "meta.json").write_text(json.dumps(dict(meta, policy=name), indent=1))
        try:
            gate2.score_main([str(d)])
        except SystemExit as e:
            if e.code:
                lines.append(f"{name}: gate2 score exit {e.code} (INVALID)")
        off_rows = [{"chunk": pt["chunk"], **r} for pt in parts[name] if pt["arm"]["status"] == "ok"
                    for r in pt["arm"]["rows"]]
        var = per_tx(write_txs(name, off_rows), "matched_notional_full")
        l61 = paired(ref, var, keys_c, 4000)
        pc = [json.loads(x) for x in open(d / "per_chunk.jsonl")]
        summary[name] = {"per_chunk": {r["chunk"]: {k: float(r.get(k, 0)) for k in ("V_race", "V_absorb", "V_net")}
                                       for r in pc},
                         "n_race_on": sum(int(r.get("n_race_on", 0)) for r in pc),
                         "n_race_off": sum(int(r.get("n_race_off", 0)) for r in pc),
                         "changed_chunks": sum(1 for r in pc if int(r.get("changed", 0)) > 0),
                         "l61": l61}
    lines.append("")
    lines.append("policy | V_race | V_absorb | V_net (USD, 95% CI chunk bootstrap) | raced fills ON/OFF | changed chunks | "
                 "maker-strict contested OFF - ON (n txs, chunks)")
    for name in names:
        s = summary[name]
        v = [gate2.cluster_boot({c: x[k] for c, x in s["per_chunk"].items()})
             for k in ("V_race", "V_absorb", "V_net")]
        l = s["l61"]
        lines.append(f"{name} | " + " | ".join(f"{pt:+.2f} [{lo:+.2f}, {hi:+.2f}]" for pt, lo, hi in v) +
                     f" | {s['n_race_on']}/{s['n_race_off']} | {s['changed_chunks']} | "
                     f"{100 * l['diff']:+.1f} pt [{100 * l['lo']:+.2f}, {100 * l['hi']:+.2f}] "
                     f"(ON {100 * l['ref']:.1f}% -> {100 * l['var']:.1f}%; n {l['n']}, {l['chunks']} chunks)")
    seeds = [n for n in names if n.startswith("random")]
    if seeds:
        import statistics
        lines.append("")
        lines.append(f"RANDOM over {len(seeds)} of {N_SEEDS} seeds: mean of point estimates (min, max, SD across seeds); "
                     f"CI = chunk bootstrap of the per-chunk seed mean")
        chunks = sorted(summary[seeds[0]]["per_chunk"])
        for k in ("V_race", "V_absorb", "V_net"):
            pts = [sum(x[k] for x in summary[s]["per_chunk"].values()) for s in seeds]
            avg = {c: sum(summary[s]["per_chunk"][c][k] for s in seeds) / len(seeds) for c in chunks}
            pt, lo, hi = gate2.cluster_boot(avg)
            sd = statistics.stdev(pts) if len(pts) > 1 else 0.0
            lines.append(f"  {k:8s} mean {statistics.mean(pts):+.2f} (min {min(pts):+.2f}, max {max(pts):+.2f}, "
                         f"SD {sd:.2f}); seed-mean CI {pt:+.2f} [{lo:+.2f}, {hi:+.2f}]")
        ro = [summary[s]["n_race_off"] for s in seeds]
        lines.append(f"  raced fills OFF mean {statistics.mean(ro):.1f} (min {min(ro)}, max {max(ro)})")
        ds = [summary[s]["l61"]["diff"] * 100 for s in seeds]
        lines.append(f"  maker-strict contested OFF - ON mean {statistics.mean(ds):+.2f} pt (min {min(ds):+.2f}, "
                     f"max {max(ds):+.2f}); per-seed CIs in the table above")
    if primary is not None and "m1n" in names:
        strip = lambda p: [x for x in open(p).read().splitlines()[1:]]
        same = strip(primary / "score.txt") == strip(out / "m1n" / "score.txt")
        lines.append("")
        lines.append(f"M1n check vs the signed primary ({primary}/score.txt, all lines but the path): "
                     f"{'IDENTICAL' if same else 'DIFFERENT -> A6 is pending, quote nothing'}")
    (out / "score_a6.txt").write_text("\n".join(lines) + "\n")
    (out / "summary_a6.json").write_text(json.dumps({k: {kk: vv for kk, vv in v.items()} for k, v in summary.items()},
                                                    indent=1))
    print("\n".join(lines))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run":
        run_main(sys.argv[2:])
    elif cmd == "score":
        score_main(sys.argv[2:])
    else:
        sys.exit(__doc__)
