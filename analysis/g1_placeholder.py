"""G1 deep dive H11: spurious fills against placeholder (anonymous snapshot) liquidity. Measurement only.

    python -m analysis.g1_placeholder <window> --hl-data <dir> [--out DIR] [--reuse] [--only NAME ...]

Base config = `validate_replay --alo-gate touch --reseed --nearest-ratio 3` (analysis/g1_triage.BASE).
With --reseed every snapshot clears the asset's book and re-seeds it as placeholders, so ALL liquidity
older than the last snapshot is anonymous: owners' cancels cannot reach it until the next snapshot.

1. Instrumented base run. For every scored, mismatched tx, the spurious (px, WILDCARD) groups
   (g1_triage.failed_groups on the wildcard-scored fills) are split two ways:
   marginals (each lot split by the engine placeholder fills that make up the group):
     age      time from the placeholder's snapshot to the tx's block
     rank     the price's level in that snapshot side (0 = the snapshot's best)
     touch    placeholder price vs the estimated real touch before the block (RealTouch: snapshots +
              trades; no bbo recorded for this window) -> overlap with the stale-touch work
     asset, tx type (engine-only tx vs tx that also had real fills)
   a precedence split (first that applies, per lot):
     ghost cancel   the owner of an order that was resting in our book when the snapshot cleared it
                    cancelled/modified it after the re-seed (our cancel found no order); those lots at
                    that (asset, side, px) were pulled on chain but still sit in our placeholder.
                    By id type: cloid / int oid (int oid matched by the resolver's estimate +- tol).
     queue          at that price the chain filled a maker that quoted after the re-seed, which sits
                    behind the placeholder in our queue (real liquidity, wrong priority)
     withdrawn      LOOK-AHEAD: the next snapshot of the asset shows the level smaller than the seeded
                    size minus real trades there in between (net lower bound of what makers pulled)
     still there    the next snapshot still shows the size: not explained by withdrawal
                    (cancel-and-replace by a maker we cannot see, taker reject, stale touch ...)
     beyond depth   the price is outside the next snapshot's levels, or no later snapshot
2. Variant runs with replay measurement flags (each a full rerun; one at a time):
     ttl<N>    placeholders expire N ms after their snapshot
     behind    known orders get time priority over placeholders at the same level
     capnext   LOOK-AHEAD: each re-seeded level capped at next snapshot size + real trades until it
     keepknown known orders survive the re-seed (rule 8 oldest-first eviction), only missing size is
               seeded, ahead of them -> owners' cancels keep working (approximates capture-guard
               Replay(keep_known=True) without Engine.refill / Book.add(front=True))
Writes placeholder.md / placeholder.json and run-<name>.json to --out (default
report/validate/<window>-placeholder).
"""

from __future__ import annotations

import argparse
import bisect
import json
from collections import Counter, defaultdict
from pathlib import Path

from analysis import g1_triage as tri
from analysis import validate_replay as vr
from data.parse import iter_trades
from engine_py.matching import PLACEHOLDER
from engine_py.replay import COINS, TOUCH_LOOKBACK_MS
from engine_py.types import ASSETS, Cancel, CancelByCloid, Modify, RejectReason, Side

MICRO = tri.MICRO
AGE_EDGES_MS = (500, 2000, 5000)
RANK_EDGES = (0, 1, 2)
FLAG_CMD = {
    "ttl500": (dict(placeholder_ttl_ms=500), "--placeholder-ttl-ms 500"),
    "ttl2000": (dict(placeholder_ttl_ms=2000), "--placeholder-ttl-ms 2000"),
    "ttl5000": (dict(placeholder_ttl_ms=5000), "--placeholder-ttl-ms 5000"),
    "behind": (dict(placeholder_queue="behind"), "--placeholder-queue behind"),
    "capnext": (dict(placeholder_cap="next"), "--placeholder-cap next"),
    "capnext+behind": (dict(placeholder_cap="next", placeholder_queue="behind"),
                       "--placeholder-cap next --placeholder-queue behind"),
    "keepknown": (dict(reseed_keep_known=True), "--reseed-keep-known"),
}
SPLIT_ORDER = ("ghost cancel (cloid)", "ghost cancel (int oid)", "queue: chain filled a post-re-seed maker",
               "withdrawn by next snapshot (net, look-ahead)", "still there in next snapshot",
               "beyond next snapshot depth / none")


# ------------------------------------------------------------------ pure helpers (tested)

def age_bucket(ms: int) -> str:
    lo = 0
    for hi in AGE_EDGES_MS:
        if ms < hi:
            return f"{lo}-{hi} ms"
        lo = hi
    return f">= {lo} ms"


def rank_bucket(rank: int) -> str:
    lo = 0
    for hi in RANK_EDGES:
        if rank <= hi:
            return f"level {lo}" if lo == hi else f"levels {lo}-{hi}"
        lo = hi + 1
    return f"levels >= {lo}"


def touch_position(maker_side: Side, px: int, bid: int | None, ask: int | None) -> str:
    """Where a maker price sits vs the estimated real touch of its own side."""
    ref = bid if maker_side is Side.BUY else ask
    if ref is None:
        return "no touch estimate"
    deeper = (ref - px) if maker_side is Side.BUY else (px - ref)   # > 0: behind the touch
    if deeper < 0:
        return "better than real touch (stale touch)"
    if deeper == 0:
        return "at real touch"
    bps = deeper * 10_000 / ref
    for hi in (1, 2, 5):
        if bps <= hi:
            return f"behind touch, <= {hi} bps"
    return "behind touch, > 5 bps"


def withdrawn_lots(seed_lots: int, next_lots: int, traded: int) -> int:
    """Net lower bound of seeded size that makers pulled before the next snapshot."""
    return max(0, seed_lots - traded - next_lots)


def split_group(lots: int, ghost: dict[str, int], queue: int, next_state: tuple[str, int]) -> Counter:
    """Precedence split of one spurious placeholder group. ghost: {"cloid": lots, "int oid": lots} already
    capped at availability; queue: named real lots missed at that price; next_state: (kind, withdrawn
    lots) with kind "in range" | "beyond"."""
    out = Counter()
    left = lots
    for kind in ("cloid", "int oid"):
        q = min(left, ghost.get(kind, 0))
        out[f"ghost cancel ({kind})"] += q
        left -= q
    q = min(left, queue)
    out["queue: chain filled a post-re-seed maker"] += q
    left -= q
    kind, w = next_state
    if kind != "in range":
        out["beyond next snapshot depth / none"] += left
        return out
    q = min(left, w)
    out["withdrawn by next snapshot (net, look-ahead)"] += q
    out["still there in next snapshot"] += left - q
    return out


class TradeIndex:
    """(asset, maker side, px) -> sorted [(time, lots)] of real trades."""

    def __init__(self, trades):
        self.by = defaultdict(list)
        for t in trades:
            self.by[(t.asset, t.taker_side.opposite, t.px)].append((t.time_ms, t.sz))
        for v in self.by.values():
            v.sort()

    def lots(self, asset, maker_side, px, t0, t1) -> int:
        """Lots traded with t0 <= time < t1."""
        v = self.by.get((asset, maker_side, px), [])
        return sum(q for _, q in v[bisect.bisect_left(v, (t0, -1)):bisect.bisect_left(v, (t1, -1))])


# ------------------------------------------------------------------ instrumented observer

class PlaceholderObserver(vr.ValidateObserver):
    """ValidateObserver + touch at each tx + ghosts: known orders resting when a re-seed cleared them,
    and the unknown-order cancels after it that can be pinned to one of them."""

    def __init__(self, real_by_hash, alo_truth):
        super().__init__(real_by_hash, alo_truth)
        self.rp = None
        self.touch: dict[str, tuple] = {}            # hash -> (bid, ask) estimate before the block
        self.ghost_cloid: dict[tuple, dict] = {}     # (user, asset, cloid) -> ghost
        self.ghost_user: dict[tuple, list] = defaultdict(list)   # (user, asset) -> ghosts (int-oid search)
        self.pulled: dict[tuple, list] = defaultdict(list)       # (asset, side, px) -> [(seq, lots, kind)]
        self.unknown_cancels = Counter()             # (id kind, matched ghost?) -> n

    def _open_tx(self, rp, block, a, p):
        if a.hash not in self.txs and rp.touch is not None:
            self.touch[a.hash] = rp.touch.get(p.asset, block.time_ms)[:2]
        super()._open_tx(rp, block, a, p)

    def before_reseed(self, rp, block, s):
        self.rp = rp
        hl = rp.resolver.hl if rp.resolver is not None else {}
        for side in (Side.BUY, Side.SELL):
            for lv in rp.engine.books[s.asset].levels(side):
                for nd in lv:
                    o = nd.order
                    if o.user == PLACEHOLDER:
                        continue
                    g = dict(asset=o.asset, side=o.side, px=o.px, lots=nd.remaining, est=hl.get(o.oid), live=True)
                    if o.cloid is not None:
                        self.ghost_cloid[(o.user, o.asset, o.cloid)] = g
                    self.ghost_user[(o.user, o.asset)].append(g)

    def _pin(self, a) -> tuple[dict | None, str]:
        target = a.cloid if isinstance(a, CancelByCloid) else a.oid if isinstance(a, Cancel) else a.target
        if isinstance(target, str):
            g = self.ghost_cloid.get((a.user, a.asset, target))
            return (g if g is not None and g["live"] else None), "cloid"
        best = None
        for g in self.ghost_user.get((a.user, a.asset), ()):
            if g["live"] and g["est"] is not None and abs(g["est"][0] - target) <= g["est"][1]:
                if best is None or abs(g["est"][0] - target) < abs(best["est"][0] - target):
                    best = g
        return best, "int oid"

    def after_action(self, rp, block, a, fills, rejects, created):
        super().after_action(rp, block, a, fills, rejects, created)
        if self.rp is None:
            self.rp = rp
        if isinstance(a, (Cancel, CancelByCloid, Modify)) and any(r.reason is RejectReason.UNKNOWN_ORDER
                                                                  for r in rejects):
            g, kind = self._pin(a)
            self.unknown_cancels[(kind, g is not None)] += 1
            if g is not None:
                g["live"] = False
                self.pulled[(g["asset"], g["side"], g["px"])].append((self.seq, g["lots"], kind))


# ------------------------------------------------------------------ attribution

def attribute(rows, obs, rp, trades: TradeIndex) -> dict:
    snaps = defaultdict(list)                         # asset -> [(time, snapshot)]
    for s in rp.snapshots:
        snaps[s.asset].append((s.time_ms, s))
    snap_times = {a: [t for t, _ in v] for a, v in snaps.items()}
    btime = {b.height: b.time_ms for b in rp.blocks}
    used = Counter()                                  # (asset, side, px, reseed_seq, kind) -> ghost lots used
    marg = defaultdict(Counter)
    split = Counter()
    split_by = defaultdict(Counter)
    tot = Counter()
    for row in rows:
        if row["warmup"] or row["status"] != "mismatch":
            continue
        rec = obs.txs[row["hash"]]
        real, engine = obs.effective(rec)
        missed, spur = tri.failed_groups(real, engine)
        tot["spurious"] += sum(spur.values())
        side = rec.side.opposite
        t = btime[rec.block]
        bid, ask = obs.touch.get(rec.hash, (None, None))
        for (px, mk), n in spur.items():
            if mk != vr.WILDCARD:
                tot["spurious named"] += n
                continue
            tot["spurious placeholder"] += n
            lots = n // px
            fills = [(sz, oid) for fpx, sz, m, oid in engine if fpx == px and m == vr.WILDCARD]
            flots = sum(sz for sz, _ in fills)
            coin = ASSETS[rec.asset].name
            for sz, oid in fills:
                st, rank, _, _ = rp.ph_meta[oid]
                w = n * sz / flots
                marg["age since its snapshot"][age_bucket(t - st)] += w
                marg["rank in its snapshot"][rank_bucket(rank)] += w
                marg["vs estimated real touch"][touch_position(side, px, bid, ask)] += w
                marg["asset"][coin] += w
                marg["tx type"]["engine-only tx" if not rec.real else "tx with real fills"] += w
                marg["asset x touch"][f"{coin}: {touch_position(side, px, bid, ask)}"] += w
            st, rank, seed_lots, _ = rp.ph_meta[fills[0][1]]
            ghost = {}
            for kind in ("cloid", "int oid"):
                key = (rec.asset, side, px, rec.reseed_seq, kind)
                avail = sum(q for s_, q, k in obs.pulled.get((rec.asset, side, px), ())
                            if k == kind and rec.reseed_seq < s_ < rec.seq) - used[key]
                ghost[kind] = min(max(avail, 0), lots)  # `used` is settled below, once the split is known
            queue = sum(v // px for (gpx, m), v in missed.items() if gpx == px and m != vr.WILDCARD)
            i = bisect.bisect_right(snap_times.get(rec.asset, []), t)
            if i >= len(snaps.get(rec.asset, [])):
                nstate = ("beyond", 0)
            else:
                nt, ns = snaps[rec.asset][i]
                levels = ns.bids if side is Side.BUY else ns.asks
                nmap = dict(levels)
                inr = bool(levels) and ((px >= min(nmap)) if side is Side.BUY else (px <= max(nmap)))
                traded = trades.lots(rec.asset, side, px, st, nt)
                nstate = ("in range", withdrawn_lots(seed_lots, nmap.get(px, 0), traded)) if inr else ("beyond", 0)
            sp = split_group(lots, ghost, queue, nstate)
            for kind in ("cloid", "int oid"):
                used[(rec.asset, side, px, rec.reseed_seq, kind)] += sp[f"ghost cancel ({kind})"]
            for k, q in sp.items():
                split[k] += q * px
                split_by[f"{coin}: {touch_position(side, px, bid, ask)}"][k] += q * px
    return dict(total=dict(tot), marginals={k: dict(v) for k, v in marg.items()}, split=dict(split),
                split_by_touch={k: dict(v) for k, v in split_by.items()},
                unknown_cancels={f"{k} matched_ghost={m}": n for (k, m), n in sorted(obs.unknown_cancels.items())},
                ghost_lots_pulled={ASSETS[a].name: sum(q for (aa, _, _), v in obs.pulled.items() if aa == a
                                                       for _, q, _ in v) for a in ASSETS})


# ------------------------------------------------------------------ runs

def _run(window, hl_data, observer_cls, **kw):
    holder = []

    def make(*a):
        holder.append(observer_cls(*a))
        return holder[-1]

    res, rows = vr.run(window, hl_data, quiet=True, observer_cls=make, **{**tri.BASE, **kw})
    return res, rows, holder[0]


def wildcard_spurious(rows, obs) -> tuple[int, int]:
    """(spurious notional in WILDCARD groups, in named groups) over scored mismatches."""
    w = k = 0
    for row in rows:
        if row["warmup"] or row["status"] != "mismatch":
            continue
        rec = obs.txs[row["hash"]]
        _, spur = tri.failed_groups(*obs.effective(rec))
        for (_, m), n in spur.items():
            if m == vr.WILDCARD:
                w += n
            else:
                k += n
    return w, k


def run_instrumented(window, hl_data, out: Path) -> dict:
    res, rows, obs = _run(window, hl_data, PlaceholderObserver)
    rp = obs.rp
    trades = TradeIndex(iter_trades(hl_data, COINS, rp.t0 - TOUCH_LOOKBACK_MS, rp.t1 + TOUCH_LOOKBACK_MS))
    d = dict(base=tri.summarize_run(res, rows), attribution=attribute(rows, obs, rp, trades))
    (out / "run-instrumented.json").write_text(json.dumps(d, indent=1))
    return d


def run_variant(window, hl_data, name, out: Path) -> dict:
    res, rows, obs = _run(window, hl_data, vr.ValidateObserver, **FLAG_CMD[name][0])
    d = tri.summarize_run(res, rows)
    d["spurious_placeholder"], d["spurious_named"] = wildcard_spurious(rows, obs)
    d["ph_stats"] = {k: v for k, v in res.items() if k.startswith("ph:")}
    (out / f"run-{name}.json").write_text(json.dumps(d, indent=1))
    return d


# ------------------------------------------------------------------ report

def usd(x) -> str:
    return f"{x / MICRO:,.0f}"


def report(inst, variants) -> str:
    b, at = inst["base"], inst["attribution"]
    sp = b["spurious"]
    ph = at["total"]["spurious placeholder"]
    L = [f"Base: recall {b['recall']} precision {b['precision']}; real ${usd(b['real'])} engine ${usd(b['engine'])} "
         f"missed ${usd(b['missed'])} spurious ${usd(sp)}; placeholder spurious ${usd(ph)} "
         f"({100 * ph / sp:.1f}% of spurious).", ""]
    for title, d in at["marginals"].items():
        L += [f"### placeholder spurious by {title}", "", "| value | $ | % of placeholder spurious |", "|---|---:|---:|"]
        L += [f"| {k} | {usd(v)} | {100 * v / ph:.1f}% |" for k, v in sorted(d.items(), key=lambda kv: -kv[1])]
        L.append("")
    L += ["### precedence split (first that applies)", "", "| cause | $ | % of placeholder spurious |", "|---|---:|---:|"]
    L += [f"| {k} | {usd(at['split'].get(k, 0))} | {100 * at['split'].get(k, 0) / ph:.1f}% |" for k in SPLIT_ORDER]
    L += ["", "### precedence split by asset and position vs estimated real touch", "",
          "| asset: touch | " + " | ".join(SPLIT_ORDER) + " |", "|---|" + "---:|" * len(SPLIT_ORDER)]
    for k, d in sorted(at["split_by_touch"].items(), key=lambda kv: -sum(kv[1].values())):
        L.append(f"| {k} | " + " | ".join(usd(d.get(c, 0)) for c in SPLIT_ORDER) + " |")
    L += ["", f"unknown-order cancels/modifies after a re-seed: {at['unknown_cancels']}",
          f"ghost lots pulled (matched): {at['ghost_lots_pulled']}", ""]
    if variants:
        L += ["### variants (each a full rerun of the base config + the flag)", "",
              "| variant | recall | precision | missed $ | spurious $ | placeholder spurious $ | 0-2 s recall | flags |",
              "|---|---:|---:|---:|---:|---:|---:|---|",
              f"| base | {b['recall']} | {b['precision']} | {usd(b['missed'])} | {usd(sp)} | {usd(ph)} | "
              f"{b['recall_0_2s']} | {tri.BASE_FLAGS} |"]
        for n, v in variants.items():
            L.append(f"| {n} | {v['recall']} | {v['precision']} | {usd(v['missed'])} | {usd(v['spurious'])} | "
                     f"{usd(v['spurious_placeholder'])} | {v['recall_0_2s']} | {tri.BASE_FLAGS} {FLAG_CMD[n][1]} |")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("window")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--out")
    ap.add_argument("--reuse", action="store_true", help="load finished runs' JSON from --out")
    ap.add_argument("--only", nargs="*", help="runs to do: instrumented and/or variant names (default: all)")
    a = ap.parse_args(argv)
    out = Path(a.out or f"report/validate/{Path(a.window).name}-placeholder")
    out.mkdir(parents=True, exist_ok=True)
    names = a.only if a.only is not None else ["instrumented", *FLAG_CMD]
    done = {}
    for n in ["instrumented", *FLAG_CMD]:
        f = out / f"run-{n}.json"
        if (a.reuse or n not in names) and f.exists():
            done[n] = json.loads(f.read_text())
        elif n in names:                               # one replay at a time
            done[n] = (run_instrumented(a.window, a.hl_data, out) if n == "instrumented"
                       else run_variant(a.window, a.hl_data, n, out))
            print(f"done {n}", flush=True)
    if "instrumented" not in done:
        return
    text = report(done["instrumented"], {n: done[n] for n in FLAG_CMD if n in done})
    (out / "placeholder.md").write_text(text)
    (out / "placeholder.json").write_text(json.dumps(done, indent=1))
    print(text)
    print(f"files in {out}")


if __name__ == "__main__":
    main()
