"""A0: classify every contested miss of the current baseline by cause (MAHITO_PLAN §00; rule in docs/scoreboard.md,
written before this ran).

Runs the baseline (Python reference engine; Rust is byte-identical on it) on each chunk with an observer that, just
before every taker tx with printed trades, records: the opposite side's queues at every level (owner, engine oid,
remaining size), which of the tx's real makers hold an order at each price, and each such maker's window orders at
that price (resting, consumed by our fills and for which taker, or removed). After the run each contested miss is
classified at the first point where its real and engine fills diverge, in the rule's order:
ordering > placeholder > margin-rejected IOC/ALO > phantom > lost order > queue > other.

usage: python -m analysis.retriage <chunk-parent dir> --hl-data DIR --audit report/validate/order_audit_dev20
       --out DIR [--stp maker --extra "--touch-trades-first"]   (B2; default = B1)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analysis import validate_replay as vr
from analysis.contested_paired import scope_of
from engine_py.matching import PLACEHOLDER
from engine_py.types import Modify, Place, Tif
from replay.adapter import options_for, run_kwargs_from_options, split_policy

CAUSES = ("ordering inside a block", "placeholder maker", "margin-rejected IOC / ALO we filled", "phantom order",
          "lost order / wrong cancel target", "queue order at the right price", "other")


class TriageObserver(vr.ValidateObserver):
    last = None

    def __init__(self, real_by_hash, alo_truth):
        super().__init__(real_by_hash, alo_truth)
        TriageObserver.last = self
        self.snap: dict[str, dict] = {}                 # taker hash -> state just before its first action
        self.consumed: dict[int, list[str]] = defaultdict(list)   # engine maker oid -> taker hashes that filled it

    def before_action(self, rp, block, a):
        super().before_action(rp, block, a)
        self._rp = rp
        p = a.new if isinstance(a, Modify) else a
        if (not isinstance(p, Place) or p.tif is Tif.ALO or a.hash in self.snap
                or not self.real_by_hash.get(a.hash)):
            return
        book = rp.engine.books[p.asset]
        opp = p.side.opposite
        levels = {}
        for px, lv in book.side(opp).items():
            levels[px] = [(n.order.oid, n.order.user, n.remaining) for n in lv]
        makers = {t.maker for t in self.real_by_hash[a.hash]}
        window = defaultdict(list)                       # (maker, px) -> [(oid, resting?, consumers)]
        for oid, act in rp.placed_by.items():
            q = act.new if isinstance(act, Modify) else act
            if act.user in makers and q.asset == p.asset and q.side is opp:
                window[(act.user, q.px)].append((oid, oid in book, list(self.consumed.get(oid, ()))))
        self.snap[a.hash] = {"levels": levels, "window": dict(window), "block": block.height}

    def after_action(self, rp, block, a, fills, rejects, created):
        super().after_action(rp, block, a, fills, rejects, created)
        for f in fills:
            self.consumed[f.maker_order].append(f.taker_hash)


def first_divergence(real, engine):
    """(k, real fill, engine fill or None): the first position where (px, maker, sz) differ."""
    for k, r in enumerate(real):
        e = engine[k] if k < len(engine) else None
        if e is None or (e[0], e[2], e[1]) != (r[0], r[2], r[1]):
            return k, r, e
    return None


def classify(rec, snap, obs, rp, ordering_fix: bool):
    if ordering_fix:
        return CAUSES[0], {}
    d = first_divergence(rec.real, rec.engine)
    if d is None:
        return CAUSES[6], {"why": "fills equal in order (aggregation)"}
    _, (p, _, m), e = d
    info = {"maker": m[:10], "px": p, "engine": None if e is None else (e[0], e[2][:10] if e[2] else e[2])}
    if e is not None and e[2] == PLACEHOLDER:
        return CAUSES[1], info
    e_truth = None
    if e is not None and e[3] in rp.placed_by:
        act = rp.placed_by[e[3]]
        e_truth = obs.alo_truth.get((act.block, act.hash, act.item_index), {}).get("truth")
    if e_truth == "rejected_other":
        return CAUSES[2], dict(info, sub="ALO the chain rejected (margin etc.)")
    mine = snap["window"].get((m, p), [])
    resting = any(r for _, r, _ in mine) or any(u == m for _, u, _ in snap["levels"].get(p, []))
    eaten = [h for _, r, cons in mine if not r for h in cons]
    if eaten and any(not obs.real_by_hash.get(h) for h in eaten):
        return CAUSES[2], dict(info, sub="M's order consumed by our fill of an IOC that printed nothing")
    if e is not None and e_truth == "rejected_alo" and not resting:
        return CAUSES[3], dict(info, sub="engine maker's ALO was rejected on chain (would cross)")
    if mine and not resting and not eaten:
        return CAUSES[4], dict(info, sub=f"{len(mine)} window order(s) of M at p, none resting, none filled by us")
    if resting or eaten:
        return CAUSES[5], dict(info, sub="M rests at p" if resting else "M's order filled by another of our takers")
    return CAUSES[6], dict(info, sub="M never placed at p in the window; no placeholder fill")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks_dir")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--audit", required=True, help="order_audit output (blocks.csv: c_only = class sort fixes)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--stp", default="taker", choices=("taker", "maker"), help="maker for B2 (docs/baseline.md)")
    ap.add_argument("--extra", default="", help='extra replay options, e.g. "--touch-trades-first" for B2')
    a = ap.parse_args(argv)
    hl = Path(a.hl_data)
    policy, opts = split_policy(options_for(stp=a.stp) + a.extra.split())
    kw = run_kwargs_from_options(hl, opts)
    fix_blocks = {(r["chunk"], int(r["block"])) for r in csv.DictReader(open(Path(a.audit) / "blocks.csv"))
                  if int(r["c_only"]) > 0}
    out_rows = []
    for c in sorted(p for p in Path(a.chunks_dir).iterdir() if p.is_dir()):
        res, rows = vr.run(c, hl, policy, quiet=True, observer_cls=TriageObserver, **kw)
        obs = TriageObserver.last
        for r in rows:
            r["chunk"] = c.name
        scored = [r for r in rows if not r["warmup"] and r["status"] in ("match", "mismatch")]
        scope = scope_of(scored, Path(a.chunks_dir), False)
        for r in scored:
            if not scope.get((c.name, r["hash"])) or r["real_notional"] <= 0:
                continue
            missed = r["real_notional"] - r["matched_notional_full"]
            if missed <= 0:
                continue
            rec = obs.txs[r["hash"]]
            cause, info = classify(rec, obs.snap.get(r["hash"], {"window": {}, "levels": {}}), obs, obs._rp,
                                   (c.name, int(r["block"])) in fix_blocks)
            out_rows.append(dict(chunk=c.name, block=r["block"], hash=r["hash"], coin=r["coin"], cause=cause,
                                 missed=missed, real_notional=r["real_notional"], detail=json.dumps(info),
                                 real=r["real"], engine=r["engine"]))
        print(f"{c.name}: {sum(1 for x in out_rows if x['chunk'] == c.name)} contested misses", file=sys.stderr,
              flush=True)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "misses.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, list(out_rows[0]))
        w.writeheader()
        w.writerows(out_rows)
    tot = sum(x["missed"] for x in out_rows)
    print(f"contested misses {len(out_rows)}; missed notional {tot / 1e6:,.0f} USD")
    for cause in CAUSES:
        xs = [x for x in out_rows if x["cause"] == cause]
        print(f"  {cause:40} {len(xs):4d}  {sum(x['missed'] for x in xs) / tot:6.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
