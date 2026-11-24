"""I42 (lead L12; measurement only): our order COUNT at the touch vs the public bbo order count n, at every taker tx.

Before the first action of each taker tx (IOC / GTC / market), on the side it would hit: the latest recorded bbo level
before the block (RealTouch.bbo: previous block's post-block state, (px, sz, n)) and our engine level at that px:
named orders placed BEFORE this block (this block's earlier placements cannot be in the previous block's n),
placeholder nodes. Class per tx:
  phantom-certain : our NAMED orders at that px > n (we hold orders the chain does not)
  missing         : no placeholder at that px and our named orders < n
  equal           : named orders == n and no placeholder
  placeholder     : a placeholder sits at that px (count unknowable)
  off-touch       : our best on that side differs from the bbo px, or no bbo yet
Then (summarize): per class, share among contested misses vs contested hits of the run itself (txs.csv + scope),
and the difference in the phantom-certain rate with a chunk-bootstrap 95% CI (rule in IDEAS I42).

usage: python -m analysis.count_audit <chunk-parent> --only CHUNK --hl-data DIR [replay flags] --out F.json
       python -m analysis.count_audit --summarize F1.json ... --run-dir RUN --chunks DIR
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analysis import validate_replay as vr
from engine_py.matching import PLACEHOLDER
from engine_py.types import Modify, Place, Side, Tif


def run_one(a) -> int:
    out = {}

    class Obs(vr.ValidateObserver):
        def before_action(self, rp, block, action):
            p = action.new if isinstance(action, Modify) else action
            if isinstance(p, Place) and p.tif is not Tif.ALO and action.hash not in out:
                hit = p.side.opposite
                bbo = rp.touch.bbo.get(p.asset) if getattr(rp, "touch", None) is not None else None
                lvl = (bbo[1] if hit is Side.BUY else bbo[2]) if bbo else None
                book = rp.engine.books[p.asset]
                best = book.best_bid() if hit is Side.BUY else book.best_ask()
                if lvl is None or best != lvl[0]:
                    cls, named, ph, n = "off-touch", None, None, (lvl[2] if lvl else None)
                else:
                    nodes = list(book.side(hit)[best])
                    ph = sum(1 for nd in nodes if nd.order.user == PLACEHOLDER)
                    # compare like with like: the bbo is the PREVIOUS block's post-block state, so count only named
                    # orders placed before this block (this block's earlier placements cannot be in n)
                    named = sum(1 for nd in nodes if nd.order.user != PLACEHOLDER and nd.order.block < block.height)
                    n = lvl[2]
                    cls = ("phantom-certain" if named > n else "placeholder" if ph else
                           "missing" if named < n else "equal")
                top = []
                if cls != "off-touch":
                    uc = Counter((nd.order.user or "")[:10] for nd in nodes
                                 if nd.order.user != PLACEHOLDER and nd.order.block < block.height)
                    top = uc.most_common(3)
                out[action.hash] = {"cls": cls, "named": named, "placeholders": ph, "n": n, "top_users": top}
            return super().before_action(rp, block, action)

    vr.run(Path(a.chunks) / a.only, a.hl_data, a.policy, out_dir=None, quiet=True, observer_cls=Obs,
           **vr.replay_kwargs(a))
    Path(a.out).write_text(json.dumps({"chunk": a.only, "txs": out}))
    print(f"{a.only}: {len(out)} taker txs", file=sys.stderr)
    return 0


def summarize(files, run_dir: Path, chunks: Path, boot=4000, seed=11) -> dict:
    from analysis.contested import load
    from analysis.contested_paired import per_tx, scope_of
    cls = {}
    for f in files:
        d = json.load(open(f))
        for h, x in d["txs"].items():
            cls[(d["chunk"], h)] = x["cls"]
    rows = load(run_dir)
    scope = scope_of(rows, chunks, False)
    val = per_tx(rows, "matched_notional_full")
    keys = [k for k, s in scope.items() if s and k in val and k in cls]
    out = {"contested_with_class": len(keys)}
    for tag, sel in (("misses", [k for k in keys if val[k] < 1.0]), ("hits", [k for k in keys if val[k] >= 1.0])):
        c = Counter(cls[k] for k in sel)
        out[tag] = {"n": len(sel), **{k: round(v / len(sel), 3) for k, v in c.most_common()}} if sel else {"n": 0}
    by = defaultdict(lambda: [0, 0, 0, 0])            # chunk -> [miss ph, miss n, hit ph, hit n]
    for k in keys:
        m = val[k] < 1.0
        b = by[k[0]]
        b[0 if m else 2] += cls[k] == "phantom-certain"
        b[1 if m else 3] += 1
    chunks_l = sorted(by)

    def diff(cs):
        t = [sum(by[c][i] for c in cs) for i in range(4)]
        return (t[0] / t[1] if t[1] else 0.0) - (t[2] / t[3] if t[3] else 0.0)
    est = diff(chunks_l)
    rnd = random.Random(seed)
    bs = sorted(diff([rnd.choice(chunks_l) for _ in chunks_l]) for _ in range(boot))
    out["phantom-certain rate: misses - hits [95% CI]"] = [round(est, 3), round(bs[int(0.025 * boot)], 3),
                                                          round(bs[int(0.975 * boot) - 1], 3)]
    out["chunks"] = len(chunks_l)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks", nargs="?")
    ap.add_argument("--only")
    ap.add_argument("--out")
    ap.add_argument("--summarize", nargs="+")
    ap.add_argument("--run-dir")
    vr.add_replay_args(ap, hl_data_required=False)
    a = ap.parse_args(argv)
    if a.summarize:
        print(json.dumps(summarize(a.summarize, Path(a.run_dir), Path(a.chunks)), indent=1))
        return 0
    return run_one(a)


if __name__ == "__main__":
    sys.exit(main())
