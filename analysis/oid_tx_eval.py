"""Independent evaluation of the int-oid resolver (data/oids.py): tx structure, held-out truth, coverage.

    uv run python -m analysis.oid_tx_eval structure WINDOW [--probes HL_DATA]
    uv run python -m analysis.oid_tx_eval heldout WINDOW HL_DATA [--folds 3] [--nearest-ratio 3]
    uv run python -m analysis.oid_tx_eval coverage WINDOW HL_DATA [--nearest-ratio 3]

structure: item-count histograms per tx type (cancel txs never hold more than one oid; this is the
           measured fact behind the cross-tx grouping), plus what the ALO-probe answers say.
heldout:   anchors that locate in the blocks are split into folds; each fold is held out of the
           estimator, the window is replayed, and every resolver call for a held-out oid is
           compared with the engine order created at the anchor's true position:
           correct / wrong / abstained. `wrong` must be 0.
coverage:  resolver outcome counts of a full replay (the number to compare across resolver versions).
Integers only; nothing here touches the network.
"""

from __future__ import annotations

import argparse
import gzip
import json
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from data.oids import _order_items
from data.parse import _block_files, read_block
from engine_py.types import ASSETS, Modify, Place


def tx_structure(window) -> dict[str, Counter]:
    """type -> Counter(items per tx) over successful txs (order / cancel / cancelByCloid / batchModify / modify)."""
    out: dict[str, Counter] = defaultdict(Counter)
    for _, p in _block_files(Path(window)):
        for tx in read_block(p)["txs"]:
            if tx["error"] is not None:
                continue
            a = tx["action"]
            t = a.get("type")
            if t == "order":
                out[t][len(a["orders"])] += 1
            elif t in ("cancel", "cancelByCloid"):
                out[t][len(a["cancels"])] += 1
            elif t == "batchModify":
                out[t][len(a["modifies"])] += 1
            elif t == "modify":
                out[t][1] += 1
    return dict(out)


def probe_rows(hl_data) -> list[dict]:
    rows = []
    for f in sorted(Path(hl_data).glob("*/*/alo_status.jsonl*")):
        op = gzip.open if f.suffix == ".gz" else open
        with op(f, "rt") as fh:
            rows += [json.loads(line)["msg"] for line in fh if line.strip()]
    return rows


def probe_summary(rows: list[dict]) -> dict:
    """Status mix, whether every answered item carries an oid, multi-item txs, and the block span."""
    st = Counter(r["answer"].get("order", {}).get("status", r["answer"].get("status")) for r in rows)
    with_oid = sum(1 for r in rows if r["answer"].get("order", {}).get("order", {}).get("oid"))
    answered = sum(1 for r in rows if r["answer"].get("status") == "order")
    return dict(rows=len(rows), statuses=dict(st), answered=answered, answered_with_oid=with_oid,
                multi_item_txs=sum(1 for r in rows if r["alo_items_in_tx"] > 1),
                blocks=(min((r["block"] for r in rows), default=None), max((r["block"] for r in rows), default=None)))


def locate_anchors(window) -> list[dict]:
    """Anchors (anchors.json answers) that match exactly one placement: [{user, oid, pos, k}]."""
    window = Path(window)
    af = window / "anchors.json"
    if not af.exists():
        return []
    want: dict[tuple, tuple[str, int]] = {}
    for a in json.loads(af.read_text()):
        ans = a.get("answer", {})
        if ans.get("status") != "order":
            continue
        o = ans["order"]["order"]
        asset = next((s.asset for s in ASSETS.values() if s.name == o["coin"]), None)
        if asset is None:
            continue
        want[(a["user"].lower(), asset, o["side"] == "B", Decimal(o["limitPx"]), Decimal(o["origSz"]),
              int(o["timestamp"]), o.get("cloid"))] = (a["user"], int(o["oid"]))
    found: dict[tuple, list] = defaultdict(list)
    k = 0
    for h, p in _block_files(window):
        for t, tx in enumerate(read_block(p)["txs"]):
            if tx["error"] is not None:
                continue
            for i, it in enumerate(_order_items(tx["action"])):
                if it.get("a") in ASSETS:
                    try:
                        key = (tx["user"].lower(), it["a"], bool(it["b"]), Decimal(it["p"]), Decimal(it["s"]),
                               int(tx["time"]), it.get("c"))
                    except Exception:
                        key = None
                    if key in want:
                        found[want[key]].append(((h, t, i), k))
                k += 1
    return [dict(user=u, oid=o, pos=v[0][0], k=v[0][1]) for (u, o), v in sorted(found.items()) if len(v) == 1]


class _Recorder:
    """Replay observer: engine oid created at each position."""

    def __init__(self):
        self.created: dict[tuple, int] = {}

    def before_action(self, rp, block, a): ...
    def after_reconcile(self, rp, block, s, result, minute): ...
    def after_block(self, rp, block, minute): ...

    def after_action(self, rp, block, a, fills, rejects, created):
        if created is not None and isinstance(a, (Place, Modify)):
            self.created[(a.block, a.tx_index, a.item_index)] = created


def _replay(window, hl_data, anchors_path, truth: dict[tuple, tuple] | None, nearest_ratio: int, replay_kw: dict | None = None, **resolver_kw):
    """One full replay. truth: (user, asset, hl_oid) -> pos. Returns (resolver stats, verdict Counter)."""
    from engine_py.replay import Replay, listing
    rec = _Recorder()
    rp = Replay(window, hl_data, listing, anchors=anchors_path, observer=rec, nearest_ratio=nearest_ratio,
                **(replay_kw or {}))
    verdicts: Counter = Counter()
    if rp.resolver is None:
        return {}, verdicts
    for k, v in resolver_kw.items():
        setattr(rp.resolver, k, v)
    inner = rp.resolver
    calls: list[tuple] = []

    def wrapped(user, asset, hl_oid):
        before = Counter(inner.stats)
        r = inner(user, asset, hl_oid)
        calls.append((user, asset, hl_oid, r, sorted((inner.stats - before).keys())))
        return r

    rp.engine.resolve_oid = wrapped
    for _ in rp:
        pass
    for user, asset, hl_oid, r, how in calls:
        pos = (truth or {}).get((user, asset, hl_oid))
        if pos is None:
            continue
        true_oid = rec.created.get(pos)
        v = "correct" if r is not None and r == true_oid else "abstained" if r is None else "WRONG"
        verdicts[v] += 1
        verdicts[f"{v} via {'+'.join(how)}"] += 1
    return dict(inner.stats), verdicts


def heldout(window, hl_data, folds=3, nearest_ratio=3, resolver_kw=None) -> dict:
    window = Path(window)
    located = locate_anchors(window)
    anchors = json.loads((window / "anchors.json").read_text())
    total, stats_all = Counter(), []
    for f in range(folds):
        held = {(a["user"], a["oid"]): a for i, a in enumerate(located) if i % folds == f}
        keep = [a for a in anchors if (a["user"], int(a.get("answer", {}).get("order", {}).get("order", {}).get("oid", -1)))
                not in held]
        truth = {(a["user"], ASSET_OF(anchors, a), a["oid"]): a["pos"] for a in held.values()}
        with tempfile.TemporaryDirectory() as td:
            ap = Path(td) / "anchors.json"
            ap.write_text(json.dumps(keep))
            stats, v = _replay(window, hl_data, ap, truth, nearest_ratio, None, **(resolver_kw or {}))
        total += v
        stats_all.append(stats)
    return dict(located=len(located), folds=folds, verdicts=dict(total), fold_stats=stats_all)


def ASSET_OF(anchors, loc) -> int:
    for a in anchors:
        if a["user"] == loc["user"] and a.get("answer", {}).get("order", {}).get("order", {}).get("oid") == loc["oid"]:
            coin = a["answer"]["order"]["order"]["coin"]
            return next(s.asset for s in ASSETS.values() if s.name == coin)
    raise KeyError(loc)


def coverage(window, hl_data, nearest_ratio=3, resolver_kw=None, **replay_kw) -> dict:
    """replay_kw: Replay options, e.g. reseed=True, keep_known=True (lead-in chunks)."""
    stats, _ = _replay(window, hl_data, None, None, nearest_ratio, replay_kw, **(resolver_kw or {}))
    total = sum(stats.get(k, 0) for k in ("resolved", "no_candidate", "ambiguous", "prewindow"))
    return dict(stats=stats, lookups=total,
                shares={k: f"{100 * stats.get(k, 0) / total:.1f}%" for k in ("resolved", "no_candidate", "ambiguous", "prewindow")}
                if total else {})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["structure", "heldout", "coverage"])
    ap.add_argument("window")
    ap.add_argument("hl_data", nargs="?")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--nearest-ratio", type=int, default=3)
    ap.add_argument("--probes")
    ap.add_argument("--baseline", action="store_true", help="coverage/heldout: switch the joint layers off (joint_tx/joint_block)")
    ap.add_argument("--chunks", action="store_true", help="coverage: WINDOW is a folder of lead-in chunks (reseed+keep_known)")
    a = ap.parse_args(argv)
    if a.mode == "structure":
        for t, c in sorted(tx_structure(a.window).items()):
            print(t, sorted(c.items())[:12])
        if a.probes:
            print(probe_summary(probe_rows(a.probes)))
    elif a.mode == "heldout":
        print(json.dumps(heldout(a.window, a.hl_data, a.folds, a.nearest_ratio,
                                      dict(joint_tx=False, joint_block=False) if a.baseline else None), indent=1))
    else:
        rk = dict(joint_tx=False, joint_block=False) if a.baseline else {}
        if a.chunks:
            tot: Counter = Counter()
            for w in sorted(p for p in Path(a.window).iterdir() if (p / "window.json").exists()):
                tot.update(coverage(w, a.hl_data, a.nearest_ratio, rk, reseed=True, keep_known=True)["stats"])
            n = sum(tot.get(k, 0) for k in ("resolved", "no_candidate", "ambiguous", "prewindow"))
            print(json.dumps(dict(stats=dict(tot), lookups=n), indent=1))
        else:
            print(json.dumps(coverage(a.window, a.hl_data, a.nearest_ratio, rk), indent=1))


if __name__ == "__main__":
    main()
