"""Touch-ledger solver: exact per-level inference of the resting queue from public data. MEASUREMENT ONLY.

Not the replay engine. No placeholders, no re-seed, no gate, no oid resolver, no trade-learn. The idea:
the recorded bbo feed is stamped with the exact block time and carries (px, sz in lots, order count n) of
both touches whenever they change. A price level that was observed EMPTY (outside the touch) at block b0
holds, at any later block, only orders placed after b0. Its contents are then pinned by integer
arithmetic: every later bbo observation of that price gives sum(remaining) == sz and count == n
(or "empty" when the touch is worse than it). Unknowns per order are only:
  - was it accepted? (post-only cross, margin, reduce-only... the reason does not matter)
  - which of a user's orders did an int-oid cancel / int-oid modify hit (or none at this level)?
Everything else is deterministic: cancel by cloid, queue order (grouping.p desc, then arrival; measured
95.5% / 98.9% in analysis/p_priority.py), and fills (recorded trades: each consumes the queue head, whose
owner must be the trade's maker). The solver enumerates hypotheses (deduplicated live sets) block by block
and drops every one an observation or a fill contradicts.

Scoring (causal): for each contested scored tx (same tx set and "contested" definition as
analysis/contested_paired.py, from the reference variant's txs.csv), the level is the tx's FIRST fill price.
Hypotheses use observations only up to the end of the previous block, plus the tx's own block actions that
execute before it (class order: post-only / non-trading, then cancels, then takers; in listing order inside a
class). Its own fills and the end-of-block observation are NOT used for its prediction.
Metric at that price level: maker overlap = sum over makers min(real lots, predicted lots) / real lots,
averaged over surviving hypotheses (each equally weighted). The same metric is computed for a replay
variant from its txs.csv (`real` / `engine` columns), so both are scored identically on identical txs.

Coverage classes per tx: fresh (level born inside the chunk, solved), not_fresh (never observed empty in the
chunk: pre-window orders could be there; NOT solved, reported), contradiction (no hypothesis survives: the
unexplained residual = what public data + these rules cannot reconcile), overflow (too many hypotheses).

usage: python -m analysis.touch_ledger <chunk-parent> --hl-data H --ref-dir <variant dir> --cmp-dir <variant dir>
       [--max-states 20000] [--assume-unchanged] [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analysis.contested import ASSET_OF, block_actors, load, users_of
from data.parse import iter_trades, iter_window
from engine_py.replay import COINS, iter_bbo
from engine_py.types import ASSETS, Cancel, CancelByCloid, Modify, Place, Side, Tif

ZERO = "0x" + "0" * 64
CLASS_POST, CLASS_CANCEL, CLASS_TAKE = 0, 1, 2


# ------------------------------------------------------------------ data per chunk

def tx_class(items) -> int:
    """Hyperliquid in-block class of a tx: 1 if it cancels/modifies, 2 if it can take (GTC / IOC), else 0."""
    if any(isinstance(a, (Cancel, CancelByCloid)) for a in items):
        return CLASS_CANCEL
    if any(isinstance(a, Modify) for a in items):
        return CLASS_CANCEL
    if any(isinstance(a, Place) and a.tif is not Tif.ALO for a in items):
        return CLASS_TAKE
    return CLASS_POST


def exec_order(actions):
    by_tx = defaultdict(list)
    for a in actions:
        by_tx[a.tx_index].append(a)
    cls = {k: tx_class(v) for k, v in by_tx.items()}
    return sorted(actions, key=lambda a: (cls[a.tx_index], a.tx_index, a.item_index))


class Chunk:
    def __init__(self, path: Path, hl_data: Path):
        self.blocks = [b for b in iter_window(path) if True]
        self.heights = [b.height for b in self.blocks]
        self.by_height = {b.height: b for b in self.blocks}
        t0, t1 = self.blocks[0].time_ms, self.blocks[-1].time_ms
        self.height_at = {}
        for b in self.blocks:
            self.height_at.setdefault(b.time_ms, []).append(b.height)
        # bbo observations keyed by (asset, height): (bid, ask) each (px, sz, n) or None
        self.obs = {}
        for coin in COINS:
            for t, asset, bid, ask in iter_bbo(hl_data, coin, t0, t1):
                hs = self.height_at.get(t)
                if hs and len(hs) == 1:
                    self.obs[(asset, hs[0])] = (bid, ask)
        # trades: by hash (taker txs) and zero-hash by height
        # trades with no decoded BTC/ETH action in their block (zero hash: liquidations / TWAP / triggers;
        # other txs: e.g. SetGlobalAction) are applied at the end of their block
        self.trades_by_hash = defaultdict(list)
        self.zero_by_height = defaultdict(list)
        hashes = {b.height: {a.hash for a in b.actions} for b in self.blocks}
        for tr in iter_trades(hl_data, COINS, t0, t1):
            hs = self.height_at.get(tr.time_ms)
            h = hs[0] if hs and len(hs) == 1 else None
            if tr.hash != ZERO and h is not None and tr.hash in hashes[h]:
                self.trades_by_hash[tr.hash].append(tr)
            elif tr.hash != ZERO and h is None:
                self.trades_by_hash[tr.hash].append(tr)
            elif h is not None:
                self.zero_by_height[h].append(tr)
        for v in self.trades_by_hash.values():
            v.sort(key=lambda t: t.seq)
        for v in self.zero_by_height.values():
            v.sort(key=lambda t: t.seq)


def level_empty(obs, side: Side, px: int) -> bool | None:
    """From one bbo observation: True if the level must be empty, False if it is the touch, None if deeper."""
    bid, ask = obs
    if side is Side.BUY:
        if ask is not None and ask[0] <= px:
            return True
        if bid is None or bid[0] < px:
            return True
        return False if bid[0] == px else None
    if bid is not None and bid[0] >= px:
        return True
    if ask is None or ask[0] > px:
        return True
    return False if ask[0] == px else None


# ------------------------------------------------------------------ hypotheses

# an order in a hypothesis: (qkey, user, remaining, cloid, uid); qkey = (-p, arrival) orders the queue
def consume(state, maker: str, sz: int):
    """The queue head must belong to `maker` and hold >= sz. Returns the new state or None."""
    if not state:
        return None
    head = state[0]
    if (maker is not None and head[1] != maker) or head[2] < sz:
        return None
    if head[2] == sz:
        return state[1:]
    return ((head[0], head[1], head[2] - sz, head[3], head[4]),) + state[1:]


def insert(state, order):
    out = list(state)
    i = len(out)
    while i > 0 and out[i - 1][0] > order[0]:
        i -= 1
    out.insert(i, order)
    return tuple(out)


class Solver:
    """Hypotheses are pairs (current queue, queue at the scored tx's start or None before it)."""

    def __init__(self, ch: Chunk, asset: int, side: Side, px: int, max_states: int, assume_unchanged: bool):
        self.ch, self.asset, self.side, self.px = ch, asset, side, px
        self.max_states, self.assume_unchanged = max_states, assume_unchanged
        self.states = {((), None)}
        self.uid = 0
        self.mark = None

    def relevant(self, tr) -> bool:
        return tr.asset == self.asset and tr.px == self.px and tr.taker_side is self.side.opposite

    def mark_x(self):
        self.mark = True
        self.states = {(c, c) for c, _ in self.states}

    def _branch(self, f):
        new = set()
        for c, x in self.states:
            for t in f(c):
                if t is not None:
                    new.add((t, x))
        self.states = new

    def prune(self, target, suf):
        n, sz = target
        ac, asz, rc, rsz, unb = suf
        keep = set()
        for c, x in self.states:
            cn, cs = len(c), sum(o[2] for o in c)
            if not (cn - rc <= n <= cn + ac):
                continue
            if not (sz <= cs + asz) or (not unb and cs - rsz > sz):
                continue
            keep.add((c, x))
        self.states = keep

    def add_size(self, p: Place, source, prev_obs) -> int | None:
        """Lots this placement would rest at this level if accepted, or None if it cannot rest here."""
        if p.side is not self.side or p.px != self.px or p.tif is Tif.IOC:
            return None
        sz = p.sz
        if p.tif is not Tif.ALO:                         # GTC / FrontendMarket: its own taker fills first
            if p.tif is not Tif.GTC:
                return None
            sz -= sum(t.sz for t in self.ch.trades_by_hash.get(source.hash, ())
                      if t.taker == source.user and t.taker_side is p.side)
            return sz if sz > 0 else None
        # no crossing filter: in-block order of cancels / fills vs post-only adds is not assumed (measured:
        # the previous block's opposite touch wrongly rejects real orders); acceptance is left to the arithmetic
        return sz

    def apply_action(self, a, arrival, prev_obs):
        user = a.user
        if isinstance(a, Place):
            self._place(a, a, arrival, prev_obs)
        elif isinstance(a, CancelByCloid):
            self._branch(lambda s: [tuple(o for o in s if not (o[1] == user and o[3] == a.cloid))])
        elif isinstance(a, Cancel):
            self._cancel_int(user)
        elif isinstance(a, Modify):
            if isinstance(a.target, str):
                self._branch(lambda s: [tuple(o for o in s if not (o[1] == user and o[3] == a.target))])
            else:
                self._cancel_int(user)
            self._place(a.new, a, arrival, prev_obs)

    def _cancel_int(self, user):
        def f(s):
            out = [s]                                   # target not at this level / already gone
            for i, o in enumerate(s):
                if o[1] == user:
                    out.append(s[:i] + s[i + 1:])
            return out
        self._branch(f)

    def _place(self, p: Place, source, arrival, prev_obs):
        sz = self.add_size(p, source, prev_obs)
        if sz is None:
            return
        self.uid += 1
        order = ((-(p.priority or 0), arrival), p.user, sz, p.cloid, self.uid)
        self._branch(lambda s: [s, insert(s, order)])    # rejected / accepted

    def apply_trades(self, trades, anonymous=False):
        for tr in trades:
            if not self.relevant(tr):
                continue
            maker = None if anonymous else (tr.seller if tr.taker_side is Side.BUY else tr.buyer)
            self._branch(lambda s: [consume(s, maker, tr.sz)])

    def observe(self, obs):
        e = level_empty(obs, self.side, self.px)
        if e is True:
            self.states = {(c, x) for c, x in self.states if not c}
        elif e is False:
            bid, ask = obs
            _, sz, n = bid if self.side is Side.BUY else ask
            self.states = {(c, x) for c, x in self.states if len(c) == n and sum(o[2] for o in c) == sz}


# ------------------------------------------------------------------ one scored tx

def block_ops(ch: Chunk, sv: "Solver", hh: int, prev_obs):
    """This level's operations in block hh, in execution order: ("act", action, arrival) and ("trades", list)."""
    ops = []
    acts = exec_order(ch.by_height[hh].actions)
    for k, a in enumerate(acts):
        if getattr(a, "asset", None) == sv.asset:
            ops.append(("act", a, (hh, k)))
        if k + 1 == len(acts) or acts[k + 1].hash != a.hash:
            trs = [t for t in ch.trades_by_hash.get(a.hash, ()) if sv.relevant(t)]
            if trs:
                ops.append(("trades", trs, a.hash))
    z = [t for t in ch.zero_by_height.get(hh, ()) if sv.relevant(t)]
    if z:
        ops.append(("trades", z, None))
    return ops


def op_bounds(sv: "Solver", op, prev_obs):
    """(max orders added, max lots added, max orders removed, max lots removed or None = unbounded by state)."""
    kind, x, _ = op
    if kind == "trades":
        return 0, 0, len(x), sum(t.sz for t in x)
    a = x
    add = sv.add_size(a.new if isinstance(a, Modify) else a, a, prev_obs) if isinstance(a, (Place, Modify)) else None
    rem = 1 if isinstance(a, (Cancel, CancelByCloid, Modify)) else 0
    return (1 if add else 0), (add or 0), rem, (None if rem else 0)


def predict(ch: Chunk, row, max_states, assume_unchanged, smoothed=False):
    """-> (status, predicted maker->lots per hypothesis, real maker->lots, n_states)"""
    h = int(row["block"])
    asset = ASSET_OF[row["coin"]]
    trades = [t for t in ch.trades_by_hash.get(row["hash"], ()) if t.asset == asset]
    if not trades:
        return "no_trades", None, None, 0
    first = trades[0]
    side, px = first.taker_side.opposite, first.px
    real = Counter()
    for t in trades:
        if t.px == px:
            real[(t.seller if t.taker_side is Side.BUY else t.buyer)[:10]] += t.sz
    i = ch.heights.index(h)
    birth = None
    for hh in reversed(ch.heights[:i]):
        o = ch.obs.get((asset, hh))
        if o is not None and level_empty(o, side, px) is True:
            birth = hh
            break
    if birth is None:
        return "not_fresh", None, real, 0
    sv = Solver(ch, asset, side, px, max_states, assume_unchanged)
    prev_obs = ch.obs.get((asset, birth))
    for hh in ch.heights[ch.heights.index(birth) + 1:i + 1]:
        scored = hh == h
        o = ch.obs.get((asset, hh))
        if o is None and assume_unchanged:
            o = prev_obs
        target = None
        if o is not None and (not scored or smoothed):
            e = level_empty(o, side, px)
            if e is True:
                target = (0, 0)
            elif e is False:
                lv = o[0] if side is Side.BUY else o[1]
                target = (lv[2], lv[1])
        ops = block_ops(ch, sv, hh, prev_obs)
        bounds = [op_bounds(sv, op, prev_obs) for op in ops]
        suffix = [(0, 0, 0, 0, False)] * (len(ops) + 1)
        for j in range(len(ops) - 1, -1, -1):
            ac, asz, rc, rsz = bounds[j]
            nac, nasz, nrc, nrsz, unb = suffix[j + 1]
            suffix[j] = (nac + ac, nasz + asz, nrc + rc, nrsz + (rsz or 0), unb or rsz is None)
        for j, (kind, x, tag) in enumerate(ops):
            if scored and kind == "act" and x.hash == row["hash"] and sv.mark is None:
                if not smoothed:
                    break
                sv.mark_x()
            if kind == "act":
                sv.apply_action(x, tag, prev_obs)
            else:
                sv.apply_trades(x, anonymous=scored and tag == row["hash"])
            if target is not None:
                sv.prune(target, suffix[j + 1])
            if len(sv.states) > max_states:
                print(f"    overflow: block +{hh - birth} of {h - birth}, op {j}/{len(ops)}, target {target}, scored {scored}, states {len(sv.states)}", file=sys.stderr)
                return "overflow", None, real, len(sv.states)
            if not sv.states:
                return "contradiction", None, real, 0
        if scored:
            if sv.mark is None:
                sv.mark_x()
            if smoothed and target is not None:
                sv.prune(target, (0, 0, 0, 0, False))
            break
        if o is not None:
            sv.observe(o)
            prev_obs = o
        if not sv.states:
            return "contradiction", None, real, 0
    if not sv.states:
        return "contradiction", None, real, 0
    starts = {x for _, x in sv.states}
    preds = []
    for s in starts:
        need, got, q = sum(real.values()), Counter(), list(s)
        while need > 0 and q:
            o = q.pop(0)
            take = min(need, o[2])
            got[o[1][:10]] += take
            need -= take
        preds.append(got)
    return "fresh", preds, real, len(starts)


def overlap(real: Counter, pred: Counter) -> float:
    tot = sum(real.values())
    return sum(min(v, pred.get(k, 0)) for k, v in real.items()) / tot if tot else float("nan")


def engine_at_px(row, px_str_real):
    """maker->lots-ish at the first real price from a txs.csv 'sz@px maker; ...' column (decimal sizes)."""
    out = Counter()
    for part in row.split(";"):
        bits = part.strip().split()
        if len(bits) == 2 and "@" in bits[0]:
            sz, px = bits[0].split("@")
            if px == px_str_real:
                out[bits[1].lower()[:10]] += float(sz)
    return out


def first_px(field):
    for part in field.split(";"):
        bits = part.strip().split()
        if len(bits) == 2 and "@" in bits[0]:
            return bits[0].split("@")[1]
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks_dir")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--ref-dir", required=True, help="reference variant dir (tx set + contested definition)")
    ap.add_argument("--cmp-dir", action="append", default=[], help="variant dirs to score the same way")
    ap.add_argument("--max-states", type=int, default=20000)
    ap.add_argument("--assume-unchanged", action="store_true", help="no bbo message at a block = touch unchanged")
    ap.add_argument("--smoothed", action="store_true",
                    help="also use the scored block's end observation (its own fills applied without maker names)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--out", help="per-tx CSV")
    a = ap.parse_args(argv)
    chunks_dir, hl = Path(a.chunks_dir), Path(a.hl_data)
    ref = load(Path(a.ref_dir))
    cmp_rows = {Path(d).name: {(r["chunk"], r["hash"]): r for r in load(Path(d))} for d in a.cmp_dir}
    # contested, as in contested_paired (reference engine makers + real makers)
    rows, cache = [], {}
    for r in ref:
        if r["real_notional"] <= 0:
            continue
        key = (r["chunk"], int(r["block"]))
        if key not in cache:
            p = chunks_dir / r["chunk"] / f"{r['block']}.json.gz"
            cache = {key: block_actors(p) if p.exists() else None}
        acts = cache[key]
        if acts is None:
            continue
        if (users_of(r["real"]) | users_of(r["engine"])) & acts.get(ASSET_OF[r["coin"]], set()):
            rows.append(r)
    if a.limit:
        rows = rows[:a.limit]
    by_chunk = defaultdict(list)
    for r in rows:
        by_chunk[r["chunk"]].append(r)
    out, status = [], Counter()
    for ci, (c, rs) in enumerate(sorted(by_chunk.items())):
        ch = Chunk(chunks_dir / c, hl)
        for r in rs:
            st, preds, real, n = predict(ch, r, a.max_states, a.assume_unchanged, a.smoothed)
            status[st] += 1
            rec = {"chunk": c, "hash": r["hash"], "status": st, "states": n}
            if preds:
                ov = [overlap(real, p) for p in preds]
                rec["ledger"] = sum(ov) / len(ov)
                rec["unique_maker"] = int(len({tuple(sorted(p.items())) for p in preds}) == 1)
            px = first_px(r["real"])
            realf = engine_at_px(r["real"], px)
            for name, m in cmp_rows.items():
                cr = m.get((c, r["hash"]))
                if cr is not None and realf:
                    tot = sum(realf.values())
                    ef = engine_at_px(cr["engine"], px)
                    rec[name] = sum(min(v, ef.get(k, 0)) for k, v in realf.items()) / tot
            out.append(rec)
        print(f"  chunk {ci + 1}/{len(by_chunk)} {c}: {dict(status)}", file=sys.stderr)
    if a.out:
        keys = sorted({k for r in out for k in r})
        with open(a.out, "w", newline="") as fh:
            w = csv.DictWriter(fh, keys)
            w.writeheader()
            w.writerows(out)
    n = len(out)
    print(f"contested txs {n} in {len(by_chunk)} chunks; status: " +
          ", ".join(f"{k} {v} ({v / n:.1%})" for k, v in status.most_common()))
    fresh = [r for r in out if r["status"] == "fresh"]
    if fresh:
        print(f"fresh solved: unique up to maker {sum(r['unique_maker'] for r in fresh)}/{len(fresh)}; "
              f"mean states {sum(r['states'] for r in fresh) / len(fresh):.1f}")
    # same-tx comparison, first price level, chunk bootstrap
    def boot(rs, f):
        by = defaultdict(list)
        for r in rs:
            by[r["chunk"]].append(f(r))
        ks = sorted(by)
        rnd = random.Random(5)
        mean = lambda xs: sum(xs) / len(xs)
        est = mean([x for k in ks for x in by[k]])
        bs = sorted(mean([x for k in (rnd.choice(ks) for _ in ks) for x in by[k]]) for _ in range(a.boot))
        return est, bs[int(.025 * a.boot)], bs[int(.975 * a.boot) - 1]
    for name in cmp_rows:
        for label, rs in (("fresh-solved txs", [r for r in fresh if name in r]),):
            if not rs:
                continue
            l = boot(rs, lambda r: r["ledger"])
            e = boot(rs, lambda r: r[name])
            d = boot(rs, lambda r: r["ledger"] - r[name])
            print(f"{label} (n={len(rs)}), first-level maker overlap: ledger {l[0]:.3f} [{l[1]:.3f},{l[2]:.3f}] | "
                  f"{name} {e[0]:.3f} [{e[1]:.3f},{e[2]:.3f}] | diff {d[0]:+.3f} [{d[1]:+.3f},{d[2]:+.3f}]")
        allr = [r for r in out if name in r]
        e = boot(allr, lambda r: r[name])
        print(f"all contested (n={len(allr)}), {name} first-level overlap {e[0]:.3f} [{e[1]:.3f},{e[2]:.3f}]")


if __name__ == "__main__":
    sys.exit(main())
