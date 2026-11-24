"""TOUCH AUDIT (RIKA R2 add-on; plan in handoff/RIKA.md, measurement only).

For every post-only (ALO) item with an SQD outcome (R1 truth: status.csv with columns tx_hash, item, text), compare
the chain's in-block touch at that item ("Post only order would have immediately matched, bbo was BID@ASK") and the
chain's decision (rejected / resting) with ours just before the item is applied in the replay:
  engine best bid / ask (all orders), best bid / ask of NAMED orders only (no placeholders), the ALO gate's touch
  (RealTouch: bid, ask, age), and our decision (rested / rejected).
Accepted ALOs (chain resting / filled) are included for the decision confusion table (they carry no bbo).

usage: python -m analysis.touch_audit STATUS.csv [...] --chunks DIR --only CHUNK --hl-data DIR [replay flags] --out F.json
       python -m analysis.touch_audit --summarize F1.json ...
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

from analysis import validate_replay as vr
from engine_py.types import ASSETS
from engine_py.matching import PLACEHOLDER
from engine_py.types import Modify, Place, Side, Tif

BBO = re.compile(r"bbo was ([0-9]+(?:\.[0-9]+)?)@([0-9]+(?:\.[0-9]+)?)")


def _named_best(book, side: Side):
    for lv in book.levels(side):
        if any(nd.order.user != PLACEHOLDER for nd in lv):
            return lv.px
    return None


def _level_kind(book, side: Side, px):
    lv = book.side(side).get(px) if px is not None else None
    if lv is None:
        return None
    users = {nd.order.user == PLACEHOLDER for nd in lv}
    return "placeholder-only" if users == {True} else "named-only" if users == {False} else "mixed"


def run_one(a) -> int:
    rows = {}
    for f in a.status:
        for r in csv.DictReader(open(f)):
            if r.get("chunk") != a.only or not r.get("tx_hash") or r.get("item") in (None, ""):
                continue
            rows[(r["tx_hash"].lower(), int(r["item"]))] = r
    out = []

    class Obs(vr.ValidateObserver):
        def before_action(self, rp, block, action):
            r = super().before_action(rp, block, action)
            p = action.new if isinstance(action, Modify) else action
            key = (action.hash.lower(), action.item_index)
            if isinstance(p, Place) and p.tif is Tif.ALO and key in rows:
                t = rows[key]
                book = rp.engine.books[p.asset]
                opp = p.side.opposite
                spec = ASSETS[p.asset]
                m = BBO.search(t.get("text") or "")
                tb = rp.touch.get(p.asset, block.time_ms) if getattr(rp, "touch", None) is not None else (None, None, None)
                self._cur = {
                    "chunk": a.only, "block": block.height, "tx_hash": action.hash, "item": action.item_index,
                    "user": action.user, "tracked": t.get("tracked"), "asset": p.asset,
                    "side": "B" if p.side is Side.BUY else "A", "px": p.px, "kind": type(action).__name__,
                    "chain_text": (t.get("text") or "")[:120], "chain_status": t.get("status"),
                    "chain_bid": spec.px_to_ticks(m.group(1)) if m else None,
                    "chain_ask": spec.px_to_ticks(m.group(2)) if m else None,
                    "eng_bid": book.best_bid(), "eng_ask": book.best_ask(),
                    "named_bid": _named_best(book, Side.BUY), "named_ask": _named_best(book, Side.SELL),
                    "opp_level_kind": _level_kind(book, opp, book.best_ask() if opp is Side.SELL else book.best_bid()),
                    "gate_bid": tb[0], "gate_ask": tb[1], "gate_age_ms": tb[2],
                }
            else:
                self._cur = None
            return r

        def after_action(self, rp, block, action, fills, rejects, created):
            r = super().after_action(rp, block, action, fills, rejects, created)
            if getattr(self, "_cur", None) is not None:
                rests = created is not None and any(created in bk for bk in rp.engine.books.values())
                self._cur["ours"] = "rested" if rests else ("rejected:" + ",".join(sorted({x.reason.name for x in rejects})))
                out.append(self._cur)
                self._cur = None
            return r

    vr.run(Path(a.chunks) / a.only, a.hl_data, a.policy, out_dir=None, quiet=True, observer_cls=Obs,
           **vr.replay_kwargs(a))
    Path(a.out).write_text(json.dumps({"chunk": a.only, "rows": out}, default=str))
    print(f"{a.only}: {len(out)} ALO rows", file=sys.stderr)
    return 0


def classify(r) -> str:
    """Cause class (RIKA.md plan) for a chain post-only reject that we did not reject the same way."""
    buy = r["side"] == "B"
    chain = r["chain_ask"] if buy else r["chain_bid"]
    ours = r["eng_ask"] if buy else r["eng_bid"]
    if chain is None:
        return "no bbo"
    if ours == chain:
        return "same opposite best"
    better = (ours is not None) and ((ours < chain) if buy else (ours > chain))
    if better:
        return "B placeholder liquidity on crossing side" if r["opp_level_kind"] == "placeholder-only" else \
            "A phantom liquidity on crossing side (ours better than chain)"
    return "C we lack the chain's best (ours worse / empty)"


def summarize(files):
    rows = [x for f in files for x in json.load(open(f))["rows"]]
    rej = [r for r in rows if "bbo was" in (r["chain_text"] or "")]
    acc = [r for r in rows if r["chain_status"] in ("open", "resting", "filled") or (r["chain_text"] or "").startswith("resting")]
    out = {"alo_rows": len(rows), "chain_postonly_rejects": len(rej), "chain_accepted": len(acc)}
    out["rejects: ours"] = dict(Counter(r["ours"].split(":")[0] for r in rej))
    out["accepted: ours"] = dict(Counter(r["ours"].split(":")[0] for r in acc))
    out["rejects: opposite best vs chain"] = dict(Counter(classify(r) for r in rej))
    adm = [r for r in rej if r["ours"] == "rested"]
    out["WE ADMITTED a chain reject: class"] = dict(Counter(classify(r) for r in adm))

    def gate_vs_chain(r):
        buy = r["side"] == "B"
        g = r["gate_ask"] if buy else r["gate_bid"]
        c = r["chain_ask"] if buy else r["chain_bid"]
        if g is None:
            return "gate abstain"
        return "gate == chain" if g == c else ("gate worse than chain (admits)" if ((g > c) if buy else (g < c)) else "gate better than chain")
    out["WE ADMITTED a chain reject: gate touch vs chain bbo"] = dict(Counter(gate_vs_chain(r) for r in adm))
    out["by tracked (admitted chain rejects)"] = dict(Counter(r["tracked"] for r in adm))
    out["chunks"] = len({r["chunk"] for r in rows})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("status", nargs="*")
    ap.add_argument("--chunks")
    ap.add_argument("--only")
    ap.add_argument("--out")
    ap.add_argument("--summarize", nargs="+")
    vr.add_replay_args(ap, hl_data_required=False)
    a = ap.parse_args(argv)
    if a.summarize:
        print(json.dumps(summarize(a.summarize), indent=1))
        return 0
    return run_one(a)


if __name__ == "__main__":
    sys.exit(main())
