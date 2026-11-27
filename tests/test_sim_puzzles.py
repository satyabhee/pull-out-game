"""M1 puzzle tests: each P1-P6 worked example (docs/problem_page.md) is built in the simulated chain, written
through the observation mask, and read back with the REAL loaders (data.parse, engine_py.replay.load_margin,
anchors.json). The visible output must equal what the puzzle says you see, and the hidden truth must be among the
answers of the reference solutions in puzzles.py (the oracles)."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

import puzzles
from data.parse import decode_tx, iter_books, iter_trades, read_block, Stats
from engine_py.replay import load_margin
from engine_py.types import ASSETS, Cancel, Place
from replay.sim import mask
from replay.sim.rules_engine import Account, CancelItem, Chain, OrderItem, Tx

T0 = 1_830_297_601_000                          # 2028-01-01 00:00:01 UTC
BTC = ASSETS[0]
px = lambda usd: BTC.px_to_ticks(str(usd))
lots = lambda btc: BTC.sz_to_lots(str(btc))
btc = lambda n: float(BTC.lots_to_sz(n))


class Scene:
    """A hand-built chunk: chain + recorder + explorer blocks under tmp_path."""

    def __init__(self, tmp: Path, accounts=None, min_notional=None):
        self.root = Path(tmp)
        self.window = self.root / "windows" / "w"
        self.window.mkdir(parents=True)
        kw = {} if min_notional is None else {"min_notional": Decimal(min_notional)}
        self.chain = Chain(accounts or {}, **kw)
        self.rec = mask.Recorder(self.root, lag_ms=1)

    def block(self, height, t, batches):
        txs = [tx for b in batches for tx in b]
        for tx in txs:
            tx.hash = mask.tx_hash(0, height, tx.index)
        trades = self.chain.run_block(height, t, batches)
        mask.write_block(self.window, height, t, 0, sorted(txs, key=lambda tx: tx.index))
        self.rec.trades(trades, t)
        self.rec.book(self.chain, t, "fast")
        return trades

    def done(self):
        self.rec.write()

    # ---- what the replay sees
    def actions(self, height):
        b = read_block(self.window / f"{height}.json.gz")
        return [a for i, tx in enumerate(b["txs"]) for a in decode_tx(tx, i, Stats())]


def fast(scene, t, side, price):
    """(size BTC, order count) of one level in the recorded fast book at time t, read back with the real loader
    (count from the recorded line: BookSnapshot keeps (px, sz) only)."""
    hour = scene.root / "hl_data"
    line = next(json.loads(l) for f in hour.rglob("book_BTC_fast.jsonl") for l in open(f)
                if json.loads(l)["msg"]["data"]["time"] == t)
    levels = line["msg"]["data"]["levels"][0 if side == "B" else 1]
    lv = next((x for x in levels if BTC.px_to_ticks(x["px"]) == price), None)
    snap = next(iter_books(hour, "BTC", t, t, suffix="_fast"))      # the real loader reads the same line
    assert any(p == price for p, _ in (snap.bids if side == "B" else snap.asks)) == (lv is not None)
    return (float(lv["sz"]), lv["n"]) if lv else (0.0, 0)


def order(i, user, side, price, size, tif, p=0, ro=False):
    return Tx(i, user, "order", [OrderItem(0, side, px(price), lots(size), tif, reduce_only=ro)], p=p)


# --------------------------------------------------------------------------- P1
def test_p1_name_the_anonymous_orders(tmp_path):
    sc = Scene(tmp_path)
    ch = sc.chain
    for user, size in (("0xm", 2.5), ("x", 3.0), ("y", 1.5)):            # hidden queue at ask 100.0
        ch.seed(user, 0, "A", px(100.0), lots(size), t_ms=T0 - 5000)
    x_oid = [o for o in ch.orders.values() if o.user == "x"][0].oid
    sc.rec.book(ch, T0, "fast")
    sc.block(1, T0 + 1, [[order(0, "u", "A", 100.0, 2.0, "Alo")]])
    sc.block(2, T0 + 2, [[Tx(0, "x", "cancel", [CancelItem(0, oid=x_oid)])]])        # int-oid cancel: no link
    sc.block(3, T0 + 3, [[order(0, "t", "B", 100.0, 1.5, "Ioc")]])
    sc.done()
    # visible: the snapshot level, the feed updates, decoded placements at 100, public trades
    assert fast(sc, T0, "A", px(100.0)) == (7.0, 3)
    updates = [(k, *fast(sc, T0 + k, "A", px(100.0))) for k in (1, 2, 3)]
    assert updates == [(1, 9.0, 4), (2, 6.0, 3), (3, 4.5, 3)]
    named = [(1, a.user, "place", btc(a.sz)) for a in sc.actions(1) if isinstance(a, Place) and a.px == px(100.0)]
    assert named == [(1, "u", "place", 2.0)]
    assert not [a for a in sc.actions(2) if isinstance(a, Place)]                      # t2: no decoded placement
    trades = [(3, t.maker, btc(t.sz)) for t in iter_trades(tmp_path / "hl_data", ["BTC"], T0, T0 + 10)]
    assert trades == [(3, "0xm", 1.5)]
    # oracle: the hidden truth is one of the queues that fit
    qs = puzzles.name_placeholders((100.0, 7.0, 3), [u for u in updates], named, trades)
    assert len(qs) == 12 and [("0xm", 2.5), (None, 3.0), (None, 1.5)] in qs


# --------------------------------------------------------------------------- P2
def test_p2_will_the_chain_accept_this_ioc(tmp_path):
    accts = {"a": Account(Decimal(-7), {0: lots(0.05)}), "b": Account(Decimal(8000), {}),
             "c": Account(Decimal(0), {})}
    sc = Scene(tmp_path, accts)
    ch = sc.chain
    ch.seed("mm", 0, "A", px(60000), lots(5), t_ms=T0 - 1000)
    ch.seed("mm", 0, "B", px(59999), lots(5), t_ms=T0 - 1000)
    mask.write_accounts(sc.window, accts, ["a", "b"], ch)                            # C has no snapshot
    orders = [("a", "B", 0.01, 60000), ("a", "A", 0.02, 59999), ("b", "B", 1.0, 60000), ("c", "B", 0.001, 60000)]
    accepted = []
    for h, (u, side, size, price) in enumerate(orders, 1):
        trades = sc.block(h, T0 + h, [[order(0, u, side, price, size, "Ioc")]])
        accepted.append(bool(trades))
    sc.done()
    assert accepted == [False, True, True, False]                                    # the puzzle's answer
    seen = load_margin(sc.window)                                                    # what the replay sees
    assert seen == {"a": (Decimal("-7"), {"BTC": Decimal("0.05")}), "b": (Decimal("8000"), {})}   # Decimals (merge: data-streams load_margin)
    snap = {"A": (25, 32, {"BTC": 0.05}), "B": (10_000, 2_000, {})}                  # the puzzle's numbers
    hist = [("C", True, False)] * 3
    oracle = [puzzles.will_accept((u.upper(), "BTC", s, z, p, False), snap, hist) for u, s, z, p in orders]
    assert oracle == accepted


# --------------------------------------------------------------------------- P3
def test_p3_which_order_does_this_cancel_mean(tmp_path):
    sc = Scene(tmp_path, min_notional=0)
    ch = sc.chain
    ch.next_oid = 4797                       # oid layout chosen so the anchors read exactly as in the puzzle
    counts = {**{h: 20 for h in range(90, 101)}, **{h: 20 if h < 111 else 19 for h in range(101, 118)},
              118: 25, 119: 22, 120: 2}             # 119: 22 + 2 burned oids
    where = {}
    for h in range(90, 121):
        txs = []
        for i in range(counts[h]):
            u, price = ("u", 99.0) if (h, i) in ((90, 0), (105, 0), (118, 0)) else ("filler", 90.0 - (i % 5))
            txs.append(order(i, u, "B", price, 0.001, "Alo"))
        if h == 119:                                                                 # two invisible events
            ch.burn_oids(2, h)
        sc.block(h, T0 + h, [txs])
        for name, pos in (("a", (90, 0)), ("b", (105, 0)), ("c", (118, 0))):
            if pos[0] == h:
                where[name] = [o.oid for o in ch.orders.values() if o.user == "u"][-1]
    oid_at = {(r["height"], r["tx"]): r["oid"] for r in ch.truth if r["kind"] == "placed"}
    anchors = [((100, 3), oid_at[(100, 3)]), ((120, 1), oid_at[(120, 1)])]
    (sc.window / "anchors.json").write_text(json.dumps([{"oid": o, "height": h, "tx": t, "item": 0}
                                                         for (h, t), o in anchors]))
    sc.block(121, T0 + 121, [[Tx(0, "u", "cancel", [CancelItem(0, oid=where["c"])])]])
    sc.done()
    seen = json.loads((sc.window / "anchors.json").read_text())
    assert [(a["height"], a["tx"], a["oid"]) for a in seen] == [(100, 3, 5000), (120, 1, 5400)]
    cancel = [a for a in sc.actions(121) if isinstance(a, Cancel)][0]
    assert cancel.oid == 5350 and where["c"] == 5350                                 # the chain's target is c
    assert puzzles.resolve_cancel(cancel.oid, {"a": (90, 0), "b": (105, 0), "c": (118, 0)},
                                  [((100, 3), 5000), ((120, 1), 5400)]) == "c"


# --------------------------------------------------------------------------- P4
@pytest.mark.parametrize("case,sizes,after,truth", [
    (1, (1.0, 2.0, 0.5), (6.5, 4), [{"b"}]),
    (2, (1.0, 1.0, 0.5), (6.5, 4), [{"a"}, {"b"}]),          # corrected: after = (6.5, 4) (page printed 7.5)
])
def test_p4_which_post_only_orders_never_rested(tmp_path, case, sizes, after, truth):
    accts = {"a": Account(), "b": Account(Decimal(0)), "c": Account()}                # B has no margin
    sc = Scene(tmp_path, accts, min_notional=0)
    ch = sc.chain
    ch.seed("m1", 0, "A", px(100.0), lots(3.0), t_ms=T0 - 1000)
    ch.seed("m2", 0, "A", px(100.0), lots(2.0), t_ms=T0 - 1000)
    sc.rec.book(ch, T0, "fast")
    sc.block(1, T0 + 1, [[order(i, u, "A", 100.0, z, "Alo") for i, (u, z) in enumerate(zip("abc", sizes))]])
    sc.done()
    before, seen_after = fast(sc, T0, "A", px(100.0)), fast(sc, T0 + 1, "A", px(100.0))
    assert before == (5.0, 2) and seen_after == after
    placements = [(a.user, btc(a.sz)) for a in sc.actions(1) if isinstance(a, Place)]
    assert placements == list(zip("abc", sizes))
    rejected = {o.user for o in ch.orders.values() if o.status == "perpMarginRejected"}
    fits = puzzles.which_rejected(before, placements, seen_after)
    assert rejected == {"b"} and rejected in fits and fits == truth


# --------------------------------------------------------------------------- P5
def test_p5_where_are_the_batch_boundaries(tmp_path):
    sc = Scene(tmp_path, min_notional=0)
    ch = sc.chain
    a_oid = ch.seed("a", 0, "A", px(100.0), lots(1.0), t_ms=T0 - 1000)
    txs = [order(0, "t", "B", 100.0, 1.0, "Ioc"), Tx(1, "a", "cancel", [CancelItem(0, oid=a_oid)]),
           order(2, "b", "A", 100.0, 1.0, "Alo")]
    sc.block(1, T0 + 1, [txs[:1], txs[1:]])                                          # hidden: cut before [1]
    sc.done()
    raw = read_block(sc.window / "1.json.gz")["txs"]                                 # explorer: one merged list
    assert [(i, tx["user"], tx["action"]["type"], tx["error"] is not None) for i, tx in enumerate(raw)] == \
        [(0, "t", "order", False), (1, "a", "cancel", True), (2, "b", "order", False)]
    # A's cancel ran after T filled A's order, so the chain rejected the action: the explorer shows it with an
    # error (data.parse drops errored txs from the replay's input). That error is itself evidence of the cut.
    printed = [(0, t.maker) for t in iter_trades(tmp_path / "hl_data", ["BTC"], T0, T0 + 10)]
    assert printed == [(0, "a")]
    seen = [(0, "T", "order", "Ioc", "B", 100, 1.0), (1, "A", "cancel", None, "A", 100, 1.0),
            (2, "B", "order", "Alo", "A", 100, 1.0)]
    assert puzzles.batch_boundaries(seen, {100: [("A", 1.0)]}, [(0, "A")]) == [1]   # the hidden cut


# --------------------------------------------------------------------------- P6
def test_p6_when_do_reduce_only_orders_vanish(tmp_path):
    accts = {"u": Account(position={0: -lots(1.0)})}                                 # U short 1.0 BTC (hidden)
    sc = Scene(tmp_path, accts, min_notional=0)
    ch = sc.chain
    ch.seed("mm", 0, "A", px(100.0), lots(1.0), t_ms=T0 - 1000)
    sc.block(1, T0 + 1, [[order(0, "u", "B", 99.0, 0.6, "Alo", ro=True), order(1, "u", "B", 98.0, 0.6, "Alo", ro=True)]])
    sc.block(2, T0 + 2, [[order(0, "u", "B", 100.0, 1.0, "Ioc")]])                   # t1: U buys 1.0 as a taker
    sc.done()
    silent = [r["oid"] for r in ch.truth if r.get("status") == "reduceOnlyCanceled"]
    assert len(silent) == 2 and not ch.levels(0, "B")                                # both canceled at t1
    resting = {f"r{i + 1}": ("B", btc(a.sz)) for i, a in enumerate(sc.actions(1)) if a.reduce_only}
    fills = [(1, t.taker_side.value, btc(t.sz)) for t in iter_trades(tmp_path / "hl_data", ["BTC"], T0, T0 + 10)
             if t.taker == "u"]
    assert resting == {"r1": ("B", 0.6), "r2": ("B", 0.6)} and fills == [(1, "B", 1.0)]
    assert puzzles.silent_cancels(resting, fills, position=-1.0) == [(1, "r1", True), (1, "r2", True)]
