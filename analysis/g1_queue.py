"""G1 deep dive on MISSED fills (H6+): why is the real maker's order absent from our book, or behind other
size, when the chain filled it? Measurement only: no engine behavior changes.

    python -m analysis.g1_queue <window> --hl-data <dir> [--out DIR] [validate_replay flags]

Default flags = the G1 base (`--alo-gate touch --reseed --nearest-ratio 3`). One instrumented run. On the
scored, mismatched txs, every missed (px, maker) group (validate_replay strict scoring; pre-re-seed makers
are WILDCARD and only counted) is split by the real maker's state in our book just before the tx:

  absent      the maker quoted that side/price since the last re-seed but holds nothing there now. The
              fate of the maker's LATEST placement there decides the cause: ALO rejected (touch gate or our
              own book), removed (cancel by cloid / by int oid, unique or nearest resolve; modify; scheduleCancel;
              gate-evict; self-trade expire), filled out earlier by the engine (in a tx where the chain also
              filled that maker there, or not), or the placement never rested (a taker order).
  size ahead  the missed notional is split over what sits ahead, by lots: placeholder, or a known order
              classified by evidence that it was a phantom (chain rejected it per alo_status; later evicted by
              gate / rule 8; reduce-only; the owner had an unresolved cancel before the tx) or by its owner's
              later cancel (same block / next block / 2-5 / 6-12 / > 12 blocks after the tx) or neither.
  modify      for every missed group with the maker resting: would the maker be first in the queue if a
              modify at the same side and price kept the order's queue position (any size / size decrease)?
              Computed from the queue alone (all orders on the level, placeholders keep theirs).

Writes queue.json and queue.md to --out (default report/validate/<window>-queue).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from analysis import g1_triage as tr
from analysis import validate_replay as vr
from engine_py.matching import PLACEHOLDER
from engine_py.types import Cancel, CancelByCloid, Modify, OutsideFill, Place, RejectReason, Tif

MICRO = tr.MICRO
ABSENT, AHEAD = "absent", "size ahead"


# ------------------------------------------------------------------ pure helpers (tested)

def block_bucket(d: int | None) -> str:
    """Blocks from the tx to a later event."""
    if d is None:
        return "never"
    if d <= 0:
        return "same block"
    if d == 1:
        return "next block"
    return "2-5 blocks" if d <= 5 else "6-12 blocks" if d <= 12 else "> 12 blocks"


def absent_cause(events, removed, filled_out, after_seq, before_seq) -> tuple[str, int | None]:
    """Cause for a maker absent at (side, px) just before seq `before_seq`.
    events: [(seq, fate, oid)] placements by the maker at that side and price, ascending seq.
    removed: oid -> dict(kind, seq, ...); filled_out: oid -> dict(seq, ...).
    Uses the latest placement in (after_seq, before_seq). Returns (cause, oid of that placement)."""
    ev = [e for e in events if after_seq < e[0] < before_seq]
    if not ev:
        return "no placement since re-seed", None
    seq, fate, oid = ev[-1]
    if fate != "rested":
        return fate, oid
    r, f = removed.get(oid), filled_out.get(oid)
    if r is not None and r["seq"] < before_seq:
        return "removed: " + r["kind"], oid
    if f is not None and f["seq"] < before_seq:
        return "filled out earlier by the engine", oid
    return "rested, fate unknown (bug?)", oid


def origin_seq(oid, pred, order_seq, strict: bool) -> int:
    """Engine seq the order would keep if a same-side, same-price modify kept queue position.
    pred: oid -> (predecessor oid, same_px, size_decrease)."""
    seen = set()
    while oid in pred and oid not in seen:
        seen.add(oid)
        p, same, dec = pred[oid]
        if not same or (strict and not dec) or p not in order_seq:
            break
        oid = p
    return order_seq[oid]


def first_if_modify_keeps(nodes, maker_oid, pred, order_seq, strict: bool) -> tuple[bool, bool]:
    """nodes: [(oid, user, lots)] queue order. (would the maker be first, would it move up) when every
    same-price modify keeps its origin's seq. Orders without a known seq (placeholders) keep their place
    (they are ahead of every window order on the level anyway)."""
    eff = {o: origin_seq(o, pred, order_seq, strict) if o in order_seq else None for o, _, _ in nodes}
    i = next(k for k, n in enumerate(nodes) if n[0] == maker_oid)
    me = eff[maker_oid]
    if me is None:
        return i == 0, False
    ahead = [n for n in nodes if n[0] != maker_oid and (eff[n[0]] is None or eff[n[0]] < me)]
    return not ahead, len(ahead) < i


def ahead_kind(info: dict, tx_block: int, tx_seq: int) -> str:
    """Primary class of one known order ahead of the real maker (precedence = strongest phantom evidence)."""
    if info.get("alo_truth") in ("rejected_alo", "rejected_other"):
        return "phantom: chain rejected it (alo_status)"
    rem = info.get("removed")
    after = rem is not None and rem["seq"] > tx_seq
    if after and rem["kind"] in tr.EVIDENCE_REMOVALS:
        return "phantom evidence: later evicted by gate / rule 8"
    if info.get("reduce_only"):
        return "reduce-only (maybe no position)"
    if info.get("missed_cancel_before"):
        return "owner had an unresolved cancel before the tx"
    if after and rem["kind"].startswith(("cancel", "modify")):
        return f"owner later {rem['kind'].split()[0]}: {block_bucket(rem['block'] - tx_block)}"
    if after and rem["kind"] == "scheduleCancel":
        return f"owner scheduleCancel later: {block_bucket(rem['block'] - tx_block)}"
    f = info.get("filled_out")
    if f is not None and f["seq"] > tx_seq:
        return "filled later by the engine"
    if after:
        return f"later removed: {rem['kind']}"
    return "still resting / cleared at re-seed"


# ------------------------------------------------------------------ instrumented run

class QueueEngine(tr.OidTracingEngine):
    """OidTracingEngine + the action kind behind each removal (cloid / int-oid unique / nearest, modify,
    self-trade expire), and the remaining size of orders a modify removed."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.outer = None                   # the replay action being applied (set by the observer)
        self.outer_seq = 0
        self.inner = None                   # the action passed to apply (gate: a modify's cancel half)
        self.removed_info: dict[int, dict] = {}
        self.removed_in_action: list[tuple] = []     # (order, remaining) removed by the current action
        self._nearest0 = 0

    def apply(self, action):
        prev, self.inner = self.inner, action
        try:
            return super().apply(action)
        finally:
            self.inner = prev

    def _cancel_hl_oid(self, *a, **k):
        self._nearest0 = self.resolve_oid.stats["resolved_nearest"] if hasattr(self.resolve_oid, "stats") else 0
        return super()._cancel_hl_oid(*a, **k)

    def _kind(self) -> str:
        why = self.why
        if why != "cancel/modify":
            return why
        act, outer = self.inner, self.outer
        if isinstance(act, Place) and not isinstance(outer, Modify):
            return "self-trade expire"
        nearest = hasattr(self.resolve_oid, "stats") and self.resolve_oid.stats["resolved_nearest"] > self._nearest0
        if isinstance(outer, Modify):
            how = "cloid" if isinstance(outer.target, str) else "oid nearest" if nearest else "oid unique"
            return f"modify by {how}"
        if isinstance(act, CancelByCloid):
            return "cancel by cloid"
        if isinstance(act, Cancel):
            return f"cancel by oid {'nearest' if nearest else 'unique'}"
        return "cancel/modify (other)"

    def _remove(self, oid):
        rem = self._book_of(oid).node(oid).remaining
        o = super()._remove(oid)
        self.removed_info[oid] = dict(kind=self._kind(), seq=self.outer_seq, block=self.block)
        self.removed_in_action.append((o, rem))
        return o


class PriorityEngine(QueueEngine):
    """Measurement only (M3): a modify that re-places at the SAME side and price keeps the old order's queue
    slot (mode "decrease": only when the new size <= the old remaining size; "any": always). Implemented by
    moving the new node into the old node's place after the cancel + place; the oid and seq are the new ones,
    so every id-based structure is unchanged. Not an engine change (engine_py is untouched)."""

    def __init__(self, *a, modify_keeps: str = "decrease", **k):
        super().__init__(*a, **k)
        if modify_keeps not in ("decrease", "any"):
            raise ValueError(modify_keeps)
        self.modify_keeps = modify_keeps
        self._old = None
        self.moved = Counter()

    def apply(self, action):
        self._old = None
        try:
            return super().apply(action)
        finally:
            self._old = None

    def _remove(self, oid):
        if isinstance(self.inner, Modify) and self._old is None:
            nd = self._book_of(oid).node(oid)
            self._old = (nd.next, nd.level, nd.order.side, nd.order.px, nd.remaining)
        return super()._remove(oid)

    def _rest(self, order, remaining):
        super()._rest(order, remaining)
        old, self._old = self._old, None
        if old is None or not isinstance(self.inner, Modify):
            return
        nxt, level, side, px, rem = old
        if order.side is not side or order.px != px:
            self.moved["new px or side"] += 1
            return
        if self.modify_keeps == "decrease" and order.sz > rem:
            self.moved["same px, size increase (not moved)"] += 1
            return
        node = self.books[order.asset].node(order.oid)
        if nxt is None or nxt.order is None or nxt.level is not level or node.level is not level:
            self.moved["same px, already last"] += 1
            return
        level.unlink(node)
        prev = nxt.prev
        prev.next, node.prev, node.next, nxt.prev = node, prev, nxt, node
        node.level = level
        level.total += node.remaining
        level.count += 1
        self.moved["same px, moved to old slot"] += 1


class QueueObserver(tr.TriageObserver):
    force_admit: frozenset = frozenset()     # M4: users whose ALOs the touch gate always admits

    def __init__(self, real_by_hash, alo_truth):
        super().__init__(real_by_hash, alo_truth)
        self.hist: dict[tuple, list] = defaultdict(list)   # (user, asset, side, px) -> [(seq, fate, oid)]
        self.filled_out: dict[int, dict] = {}              # maker oid -> fill that emptied it
        self.pred: dict[int, tuple] = {}                    # modify-created oid -> (removed oid, same px, decrease)
        self.order_seq: dict[int, int] = {}                 # engine oid -> engine Order.seq (rested orders)
        self.order_info: dict[int, dict] = {}               # engine oid -> placement facts
        self.unknown_cloid: dict[tuple, list] = defaultdict(list)   # (user, asset) -> [seq] cloid misses
        self.queue_at_tx: dict[tuple, dict] = {}            # (hash, px, maker) -> nodes + maker oid
        self._gate0 = Counter()

    def before_action(self, rp, block, a):
        super().before_action(rp, block, a)
        if self.force_admit and not getattr(rp, "_force_wrapped", False):
            orig = rp._gate
            users = self.force_admit

            def gate(blk, p, _orig=orig):
                return True if p.user in users else _orig(blk, p)
            rp._gate, rp._force_wrapped = gate, True
        eng = rp.engine
        eng.outer, eng.outer_seq, eng.removed_in_action = a, self.seq, []
        self._gate0 = Counter(rp.gate_stats)

    def _level(self, rp, block, t) -> dict:
        out = super()._level(rp, block, t)
        level = rp.engine.books[t.asset].side(t.taker_side.opposite).get(t.px)
        nodes = [(nd.order.oid, nd.order.user, nd.remaining) for nd in (level if level is not None else ())]
        mine = next((o for o, u, _ in nodes if u == t.maker), None)
        out.update(nodes=nodes, maker_oid=mine)
        for o, u, _ in nodes:                       # placeholders: engine seq (they are not in order_seq)
            if u == PLACEHOLDER:
                self.order_seq.setdefault(o, rp.engine.books[t.asset].node(o).order.seq)
        return out

    def after_action(self, rp, block, a, fills, rejects, created):
        super().after_action(rp, block, a, fills, rejects, created)
        eng = rp.engine
        reasons = {r.reason for r in rejects}
        p = a.new if isinstance(a, Modify) else a
        if isinstance(p, Place):
            book = eng.books[p.asset]
            if created is not None and created in book:
                fate = "rested"
                self.order_seq[created] = book.node(created).order.seq
                t = self.alo_truth.get((a.block, a.hash, a.item_index))
                self.order_info[created] = dict(reduce_only=p.reduce_only, tif=p.tif.name, seq=self.seq,
                                                user=a.user, asset=p.asset, block=a.block,
                                                alo_truth=t["truth"] if t else None, modify=isinstance(a, Modify))
            elif p.tif is Tif.ALO and RejectReason.ALO_WOULD_CROSS in reasons:
                if rp.gate_stats["reject"] > self._gate0["reject"]:
                    own = rp.gate_stats["reject_engine_would_admit"] > self._gate0["reject_engine_would_admit"]
                    fate = "ALO rejected by touch gate" + (" (our book would admit)" if own else " (our book agrees)")
                else:
                    fate = "ALO rejected by our book (gate abstained)"
                t = self.alo_truth.get((a.block, a.hash, a.item_index))
                if t is not None:
                    fate += f" [chain: {t['truth']}]"
            else:
                fate = "placed as taker, nothing rested"
            self.hist[(a.user, p.asset, p.side, p.px)].append((self.seq, fate, created))
            if isinstance(a, Modify) and created is not None:
                for o, rem in eng.removed_in_action:
                    if o.user == a.user and o.oid != created and eng.removed_info[o.oid]["kind"].startswith("modify"):
                        self.pred[created] = (o.oid, o.side is p.side and o.px == p.px, p.sz <= rem)
                        break
        if isinstance(a, (CancelByCloid, Modify)) and RejectReason.UNKNOWN_ORDER in reasons:
            if isinstance(a, CancelByCloid) or isinstance(a.target, str):
                self.unknown_cloid[(a.user, a.asset)].append(self.seq)
        for f in fills:
            if f.maker_order not in eng.books[a.asset] and f.maker_order not in self.filled_out:
                self.filled_out[f.maker_order] = dict(seq=self.seq, hash=a.hash, block=block.height,
                                                      outside=isinstance(a, OutsideFill), px=f.px,
                                                      maker=f.maker_user)

    def missed_cancel_before(self, oid, tx_seq) -> bool:
        """The order's owner sent a cancel/modify we could not resolve between its placement and tx_seq."""
        info = self.order_info.get(oid)
        if info is None:
            return False
        k = (info["user"], info["asset"])
        return (any(info["seq"] < s < tx_seq for s, _ in self.unresolved.get(k, ()))
                or any(info["seq"] < s < tx_seq for s in self.unknown_cloid.get(k, ())))


def analyze(rows, obs: QueueObserver, eng: QueueEngine) -> dict:
    tot = Counter()
    absent = Counter()
    absent_any = Counter()                  # any placement since re-seed had this fate (not exclusive)
    absent_cancel_timing = Counter()        # removed-by-owner absents: blocks from the removal to the tx
    ahead = Counter()
    ahead_detail = Counter()
    modify = Counter()
    n_groups = Counter()
    gate_users: set = set()
    for row in rows:
        if row["warmup"] or row["status"] != "mismatch":
            continue
        rec = obs.txs[row["hash"]]
        real, engine = obs.effective(rec)
        missed, _ = tr.failed_groups(real, engine)
        side = rec.side.opposite
        for (px, maker), n in missed.items():
            if maker == vr.WILDCARD:
                tot["pre-re-seed maker (px/size scored)"] += n
                continue
            lv = obs.level_at_tx.get((rec.hash, px, maker), {"state": "not captured"})
            tot[lv["state"]] += n
            n_groups[lv["state"]] += 1
            if lv["state"] == ABSENT:
                evs = obs.hist.get((maker, rec.asset, side, px), [])
                cause, oid = absent_cause(evs, eng.removed_info, obs.filled_out, rec.reseed_seq, rec.seq)
                if cause == "filled out earlier by the engine":
                    f = obs.filled_out[oid]
                    if f["outside"]:
                        cause += ": outside fill"
                    else:
                        r = obs.txs.get(f["hash"])
                        hit = r is not None and any(p == f["px"] and m == maker for p, _, m in r.real)
                        cause += (": chain also filled this maker there (size short)" if hit
                                  else ": chain did NOT fill this maker there (spurious)")
                if cause.startswith("removed:"):
                    r = eng.removed_info[oid]
                    absent_cancel_timing[(cause, block_bucket(rec.block - r["block"]))] += n
                absent[cause] += n
                if cause.startswith("ALO rejected by touch gate"):
                    gate_users.add(maker)
                for k in {fate.split(" [")[0] for s, fate, _ in evs if rec.reseed_seq < s < rec.seq}:
                    absent_any[k] += n
            elif lv["state"] in (AHEAD, "nothing ahead"):
                if lv["state"] == AHEAD:
                    q_tot = sum(q for *_, q in lv["ahead"])
                    for o, user, q in lv["ahead"]:
                        if user == PLACEHOLDER:
                            k = "placeholder"
                        else:
                            info = dict(obs.order_info.get(o, {}))
                            info["removed"] = eng.removed_info.get(o)
                            info["filled_out"] = obs.filled_out.get(o)
                            info["missed_cancel_before"] = obs.missed_cancel_before(o, rec.seq)
                            k = ahead_kind(info, rec.block, rec.seq)
                            ahead_detail[("reduce-only", str(bool(info.get("reduce_only"))))] += n * q / q_tot
                            ahead_detail[("alo_status", str(info.get("alo_truth")))] += n * q / q_tot
                            ahead_detail[("placed by modify", str(bool(info.get("modify"))))] += n * q / q_tot
                            ahead_detail[("tif", str(info.get("tif")))] += n * q / q_tot
                            if user == maker:
                                k += " [maker's own older order]"
                        ahead[k] += n * q / q_tot
                mo = lv.get("maker_oid")
                for strict, name in ((False, "same px, any size"), (True, "same px, size decrease")):
                    first, up = first_if_modify_keeps(lv["nodes"], mo, obs.pred, obs.order_seq, strict)
                    modify[(name, "maker first")] += n * (first and lv["state"] == AHEAD)
                    modify[(name, "maker moves up")] += n * (up and lv["state"] == AHEAD)
                    modify[(name, "maker loses first place")] += n * (not first and lv["state"] == "nothing ahead")
    modifies = Counter()
    for o, (p, same, dec) in obs.pred.items():
        modifies[("same px" if same else "new px") + (", size decrease" if same and dec else "")] += 1
    return dict(state=dict(tot), groups=dict(n_groups), absent=dict(absent), absent_any=dict(absent_any),
                absent_removal_timing={f"{c} | {b}": v for (c, b), v in absent_cancel_timing.items()},
                ahead=dict(ahead), ahead_detail={f"{a} | {b}": v for (a, b), v in ahead_detail.items()},
                modify_keeps_priority={f"{a} | {b}": v for (a, b), v in modify.items()},
                modify_counts=dict(modifies), absent_gate_users=sorted(gate_users))


def run(window, hl_data, out_dir, modify_keeps=None, force_admit=(), **flags) -> dict:
    holder = []

    def make(*a):
        holder.append(QueueObserver(*a))
        holder[-1].force_admit = frozenset(force_admit)
        return holder[-1]

    stp = flags.pop("stp", "cancel_taker")
    eng = PriorityEngine(stp=stp, modify_keeps=modify_keeps) if modify_keeps else QueueEngine(stp=stp)
    res, rows = vr.run(window, hl_data, quiet=True, observer_cls=make, engine=eng, **flags)
    obs = holder[0]
    out = dict(flags={**flags, "modify_keeps": modify_keeps, "force_admit_users": len(force_admit)},
               modify_moved=dict(getattr(eng, "moved", {})), base=tr.summarize_run(res, rows),
               gate=dict(obs.rp.gate_stats), resolver=dict(obs.rp.resolver.stats) if obs.rp.resolver else {},
               **analyze(rows, obs, eng))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "queue.json").write_text(json.dumps(out, indent=1, default=str))
    (Path(out_dir) / "queue.md").write_text(to_md(out))
    return out


def to_md(out) -> str:
    b = out["base"]
    m = b["missed"]
    lines = [f"flags {out['flags']}", f"recall {b['recall']} precision {b['precision']}; missed ${tr.usd(m)}, "
             f"spurious ${tr.usd(b['spurious'])} (real ${tr.usd(b['real'])}, engine ${tr.usd(b['engine'])})", ""]
    for title, d in (("missed by real maker state", out["state"]), ("ABSENT: fate of the latest placement", out["absent"]),
                     ("ABSENT: any placement since re-seed had this fate (overlapping)", out["absent_any"]),
                     ("ABSENT removed: removal block vs tx block", out["absent_removal_timing"]),
                     ("SIZE AHEAD: missed notional split by lots ahead", out["ahead"]),
                     ("SIZE AHEAD: known orders ahead, facts", out["ahead_detail"]),
                     ("modify keeps queue position (counterfactual from the queue)", out["modify_keeps_priority"])):
        lines += [f"### {title}", "", "| cause | $ | % missed |", "|---|---:|---:|"]
        lines += [f"| {k} | {tr.usd(v)} | {tr.pct(v, m)} |" for k, v in sorted(d.items(), key=lambda x: -x[1])]
        lines.append("")
    lines += ["modify counts (engine-created by a modify): " + json.dumps(out["modify_counts"]),
              "groups by state: " + json.dumps(out["groups"]), "gate: " + json.dumps(out["gate"]),
              "resolver: " + json.dumps(out["resolver"])]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("window")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--out")
    ap.add_argument("--nearest-ratio", type=int, default=3)
    ap.add_argument("--no-gate", action="store_true", help="drop --alo-gate touch from the base")
    ap.add_argument("--no-reseed", action="store_true")
    ap.add_argument("--modify-keeps", choices=["decrease", "any"], help="M3 flag: same-px modify keeps queue slot")
    ap.add_argument("--force-admit-from", help="M4 flag: queue.json of a base run; the touch gate admits every ALO "
                    "of its absent_gate_users (oracle)")
    a = ap.parse_args(argv)
    flags = dict(alo_gate=None if a.no_gate else "touch", reseed=not a.no_reseed, nearest_ratio=a.nearest_ratio)
    out_dir = Path(a.out or f"report/validate/{Path(a.window).name}-queue")
    users = json.loads(Path(a.force_admit_from).read_text())["absent_gate_users"] if a.force_admit_from else ()
    print(to_md(run(a.window, a.hl_data, out_dir, modify_keeps=a.modify_keeps, force_admit=users, **flags)))
    print(f"files in {out_dir}")


if __name__ == "__main__":
    main()
