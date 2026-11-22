"""Accuracy on what this setup can reproduce: score only txs whose real outcome needs no data a node alone has.

A scored tx is OUT of scope (needs a node) when, for any of its real fills:
  hidden_maker     the maker never placed an order on that side and price in our data (window + lead-in) before
                   this tx: it is an anonymous pre-window order (snapshot placeholder); only a node knows whose it
                   is and where it queues;
  unresolved       someone who had quoted at one of the tx's real fill levels (asset, side, price) made an int-oid
                   cancel/modify afterwards, before this tx, that the resolver could not tie to an order: whether
                   their order still rests there (and so who is first in the queue) needs exact order ids.
Txs with no real fills (engine-only or both empty) stay in scope. The test never looks at what the engine did with
this tx, only at what our data holds about the real makers, so it cannot pick the txs the engine got right.

Reports, for all scored txs and for the reproducible subset: the G1 metric (wildcard-strict fill recall,
target 80%), full-strict recall / precision, exact-match tx rate, and the share of real notional in scope.

usage: python -m analysis.reproducible <window or chunk-parent dirs...> --hl-data H [replay options as validate_replay]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from analysis import validate_replay as vr
from data.oids import OidEstimator
from engine_py.types import Side


class _Obs(vr.ValidateObserver):
    last = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rp = None
        self.placed = defaultdict(list)       # (user, asset, side, px) -> [(seq, engine oid)] in-window placements
        _Obs.last = self

    def after_action(self, rp, block, a, fills, rejects, created):
        self.rp = rp
        if created is not None:
            p = a.new if hasattr(a, "new") else a
            self.placed[(a.user, p.asset, p.side, p.px)].append((self.seq, created))
        return super().after_action(rp, block, a, fills, rejects, created)


def level_index(obs) -> dict:
    """(asset, side, px) -> [(user, quote seqs)] from the observer's quote history."""
    idx = defaultdict(list)
    for (u, asset, side, px), seqs in obs.quote_seqs.items():
        idx[(asset, side, px)].append((u, seqs))
    return idx


def target_class(obs, u, asset, side, px, s, hl, exact, oid_px) -> str | None:
    """Could the unresolved cancel (user u, real oid hl, at seq s) have removed an order at this level?
    None = no; "prewindow" = it names an order placed before our data (anonymous liquidity, level unknown unless a
    fact gives its price); "in_window" = one of u's in-window orders at this level is a candidate by its estimated
    oid (resolver tolerance), or no estimate exists to rule it out."""
    if not _could_hit(hl, exact, oid_px, side, px):
        return None
    res = obs.rp.resolver if obs.rp is not None else None
    k = oid_px.get(hl)
    if res is not None and res.first_hl is not None and hl < res.first_hl - res.est.tolerance(0, res.base, res.per_mille):
        return "prewindow"
    if k is not None and k[4] is not None and res is not None and res.first_ts is not None and k[4] < res.first_ts:
        return "prewindow"
    if res is None:
        return "in_window"
    cands = [x for sq, x in obs.placed.get((u, asset, side, px), ()) if sq < s]
    if not cands:
        return None                                    # u had no in-window order here: only its pre-window ones
    for x in cands:
        est = res.hl.get(x)
        if est is None or abs(est[0] - hl) <= est[1]:
            return "in_window"
    return None


def out_of_scope(obs, rec, levels=None, exact=(), oid_px=None, prewindow_is_gap=False) -> set[str]:
    """Why this tx needs a node (empty = reproducible). Inputs only: our data about its real fill levels.
    exact / oid_px (data/oids.OidEstimator, with order-id facts): an unresolved cancel whose real oid is located
    (its order is known and simply no longer rests in our book) or whose target is known to be on another side or
    price cannot change this level, so it is not a gap here."""
    levels = level_index(obs) if levels is None else levels
    oid_px = oid_px or {}
    why = set()
    side = rec.side.opposite
    for px in {px for px, _, _ in rec.real}:
        for u, seqs in levels.get((rec.asset, side, px), ()):
            if seqs[0] >= rec.seq:
                continue
            for s, hl in obs.unresolved.get((u, rec.asset), ()):
                if not seqs[0] < s < rec.seq:
                    continue
                c = target_class(obs, u, rec.asset, side, px, s, hl, exact, oid_px)
                if c == "in_window" or (c == "prewindow" and prewindow_is_gap):
                    why.add("unresolved_" + c)
    named = obs.rp.named_levels if obs.rp is not None else ()
    for px, _, m in rec.real:
        if (m, rec.asset, side, px) in named:
            continue                                   # its pre-window order there is named from a fact
        if obs.first_quote.get((m, rec.asset, side, px), rec.seq) >= rec.seq:
            why.add("hidden_maker")
    return why


def _could_hit(hl, exact, oid_px, side, px) -> bool:
    if hl in exact:
        return False
    k = oid_px.get(hl)
    return k is None or (k[2] == (side is Side.BUY) and k[3] == px)


def windows_of(paths) -> list[Path]:
    out = []
    for p in map(Path, paths):
        if (p / "window.json").exists():
            out.append(p)
        else:
            out += sorted(d for d in p.iterdir() if (d / "window.json").exists())
    return out


def measure(window, hl_data, policy, kw, acc) -> None:
    res, rows = vr.run(window, hl_data, policy, quiet=True, observer_cls=_Obs, **kw)
    obs = _Obs.last
    byhash = {r["hash"]: r for r in rows}
    levels = level_index(obs)
    exact, oid_px = (), {}
    if kw.get("oid_facts") and kw.get("use_resolver", True):
        anchors = Path(window) / "anchors.json"
        est = OidEstimator.from_files(window, anchors if anchors.exists() else None, facts=True, hl_data=hl_data)
        exact, oid_px = est.exact, est.oid_px
    for rec in obs.txs.values():
        r = byhash.get(rec.hash)
        if r is None or r["warmup"] or r["status"] not in ("match", "mismatch"):
            continue
        why = out_of_scope(obs, rec, levels, exact, oid_px)
        for k in why:
            acc["why:" + k]["txs"] += 1
            acc["why:" + k]["real"] += r["real_notional"]
        for name in ("all",) + (() if why else ("reproducible",)):
            a = acc[name]
            a["txs"] += 1
            a["match"] += r["status"] == "match"
            a["match_full"] += r["match_full"]
            a["real"] += r["real_notional"]
            a["engine"] += r["engine_notional"]
            a["matched"] += r["matched_notional"]
            a["matched_full"] += r["matched_notional_full"]


def report(acc) -> str:
    tot = acc["all"]
    lines = [f"{'scope':13} {'txs':>5} {'real$ share':>11} {'G1 recall':>9} {'recall full':>11} {'prec full':>9} "
             f"{'tx exact':>8} {'tx full':>7}"]
    for name in ("all", "reproducible"):
        a = acc[name]
        if not a["txs"]:
            continue
        lines.append(f"{name:13} {a['txs']:5d} {a['real'] / max(tot['real'], 1):11.3f} {a['matched'] / max(a['real'], 1):9.4f} "
                     f"{a['matched_full'] / max(a['real'], 1):11.4f} {a['matched_full'] / max(a['engine'], 1):9.4f} "
                     f"{a['match'] / a['txs']:8.4f} {a['match_full'] / a['txs']:7.4f}")
    for k in sorted(k for k in acc if k.startswith("why:")):
        lines.append(f"  out of scope, {k[4:]:13} {acc[k]['txs']:5d} txs, {acc[k]['real'] / max(tot['real'], 1):.3f} of real $")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("windows", nargs="+")
    vr.add_replay_args(ap)
    a = ap.parse_args(argv)
    kw = vr.replay_kwargs(a)
    acc = defaultdict(lambda: defaultdict(int))
    for w in windows_of(a.windows):
        measure(w, a.hl_data, a.policy, kw, acc)
    print(report(acc))


if __name__ == "__main__":
    sys.exit(main())
