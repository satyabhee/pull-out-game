"""replay/sim/rules_engine.py (M1): the simulated chain reproduces every problem-page Example exactly, and the chain
rules one by one."""
from decimal import Decimal

import pytest

from replay.sim.rules_engine import Account, CancelItem, Chain, OrderItem, Tx
from tests.chain_examples import EXAMPLES


def run_example(ex):
    accts = {u: Account(free_margin=Decimal(str(a["free_margin"])), position={0: a["position"]})
             for u, a in ex["accounts"].items()}
    ch = Chain(accts, min_notional=Decimal(0))        # the examples use abstract units (px 100 ticks, 1 lot)
    scored = None
    for h, s in enumerate(ex["steps"], 1):
        tx = Tx(index=0, user=s["user"], kind="order", p=s.get("p", 0), hash=f"h{h}",
                items=[OrderItem(0, s["side"], s["px"], s["sz"], s["tif"], reduce_only=s.get("reduce_only", False))])
        trades = ch.run_block(h, s["t"], [[tx]])
        if s.get("scored"):
            scored = [((t.seller if t.taker_side == "B" else t.buyer), t.px, t.sz) for t in trades]
    resting = {}
    for side in ("B", "A"):
        for px, _, _ in ch.levels(0, side):
            for o in ch.queue(0, side, px):
                resting.setdefault(o.user, []).append((side, px, o.sz))
    return scored, resting


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_problem_page_examples(name):
    ex = EXAMPLES[name]
    assert run_example(ex) == (ex["expect"]["fills"], ex["expect"]["resting"])


def order(user, side, px, sz, tif, idx=0, p=0, h="h"):
    return Tx(index=idx, user=user, kind="order", p=p, hash=h, items=[OrderItem(0, side, px, sz, tif)])


def chain():
    return Chain(min_notional=Decimal(0))


def test_p5_class_sort_is_per_batch():
    """P5: [0] T IOC buy, [1] A cancels its ask, [2] B ALO ask. One batch: T buys from B. Cut after [0]: from A."""
    def run(batches_of):
        ch = chain()
        oid_a = ch.seed("A", 0, "A", 100, 1, t_ms=0)
        txs = [order("T", "B", 100, 1, "Ioc", idx=0, h="t"),
               Tx(index=1, user="A", kind="cancel", items=[CancelItem(0, oid=oid_a)], hash="c"),
               order("B", "A", 100, 1, "Alo", idx=2, h="b")]
        trades = ch.run_block(1, 1000, batches_of(txs))
        return [t.seller for t in trades]
    assert run(lambda t: [t]) == ["B"]
    assert run(lambda t: [t[:1], t[1:]]) == ["A"]


def test_priority_window_sorts_only_the_young_tail():
    ch = chain()
    ch.run_block(1, 0, [[order("old", "A", 100, 1, "Alo", p=0)]])
    ch.run_block(2, 1000, [[order("y1", "A", 100, 1, "Alo", p=3)]])
    ch.run_block(3, 1100, [[order("y2", "A", 100, 1, "Alo", p=9)]])     # jumps y1 (100 ms younger), not old
    ch.run_block(4, 1200, [[order("y3", "A", 100, 1, "Alo", p=9)]])     # equal p: behind y2 (arrival)
    assert [o.user for o in ch.queue(0, "A", 100)] == ["old", "y2", "y3", "y1"]


def test_self_trade_cancels_resting_and_keeps_matching():
    ch = chain()
    ch.run_block(1, 0, [[order("U", "A", 100, 1, "Alo"), order("V", "A", 100, 1, "Alo", idx=1)]])
    trades = ch.run_block(2, 10, [[order("U", "B", 100, 1, "Ioc")]])
    assert [(t.seller, t.sz) for t in trades] == [("V", 1)]
    assert [s for s in ch.truth if s.get("status") == "selfTradeCanceled"]


def test_every_item_takes_an_oid_rejected_ones_too_and_burns_count():
    ch = chain()
    ch.run_block(1, 0, [[order("M", "A", 100, 1, "Alo")]])                  # oid 1
    ch.run_block(2, 10, [[order("X", "B", 101, 1, "Alo")]])                 # oid 2: would cross -> rejected
    ch.burn_oids(3, 2)                                                       # oids 3-5 invisible
    ch.run_block(3, 20, [[order("Y", "B", 99, 1, "Alo")]])                  # oid 6
    assert ch.orders[2].status == "badAloPxRejected" and ch.orders[6].user == "Y"


def test_ioc_without_fill_and_min_notional():
    ch = Chain()                                                             # real $10 minimum
    ch.run_block(1, 0, [[order("M", "A", 6_000_000, 100, "Alo")]])           # BTC: 60,000 x 0.001 = $60
    ch.run_block(2, 1, [[order("T", "B", 5_000_000, 100, "Ioc")]])           # no cross
    ch.run_block(3, 2, [[order("S", "B", 6_000_000, 1, "Ioc")]])             # $0.6 < $10
    assert ch.orders[2].status == "iocCancelRejected" and ch.orders[3].status == "minTradeNtlRejected"


def test_cancel_by_cloid_and_error_when_nothing_matches():
    ch = chain()
    ch.run_block(1, 0, [[Tx(0, "U", "order", [OrderItem(0, "B", 99, 1, "Alo", cloid="c1")])]])
    good = Tx(0, "U", "cancelByCloid", [CancelItem(0, cloid="c1")])
    bad = Tx(0, "U", "cancelByCloid", [CancelItem(0, cloid="c1")])
    ch.run_block(2, 10, [[good]])
    ch.run_block(3, 20, [[bad]])
    assert good.error is None and bad.error and not ch.levels(0, "B")
