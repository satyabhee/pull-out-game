"""Property-based tests (Hypothesis) for the book's invariants (specs/01-book.md sections 3-7).

Random sequences of places (GTC/IOC/ALO, several owners, a narrow price band so orders cross often)
and cancels must always:
- leave the book uncrossed, with every resting order 0 < remaining <= size, FIFO by arrival;
- conserve lots: every accepted taker's size = filled + rested + cancelled;
- fill at the maker's price, within the taker's limit;
- give identical fills and state hashes when run twice.
"""
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from engine_py.matching import MarketEngine
from engine_py.state import market_hash
from engine_py.types import Cancel, MarketConfig, Place, Side, Tif

CFG = MarketConfig("PROP", 1, Decimal("1"), Decimal("1"), Decimal("1"), 1, 10**6, 10**6, 10**12)

place = st.tuples(st.just("place"), st.sampled_from(["a", "b", "c"]), st.sampled_from([Side.BUY, Side.SELL]),
                  st.integers(95, 105), st.integers(1, 10), st.sampled_from([Tif.GTC, Tif.IOC, Tif.ALO]))
cancel = st.tuples(st.just("cancel"), st.sampled_from(["a", "b", "c"]), st.integers(1, 60))
ops = st.lists(st.one_of(place, place, cancel), max_size=60)


def run(seq):
    e, log = MarketEngine(CFG), []
    for op in seq:
        if op[0] == "place":
            _, owner, side, px, sz, tif = op
            r = e.submit(Place(owner, side, px, sz, tif))
            if r.accepted:
                filled = sum(f.size_lots for f in r.fills)
                assert filled + r.rested_lots + r.cancelled_lots == sz
                for f in r.fills:
                    assert (f.price_tick <= px) if side == Side.BUY else (f.price_tick >= px)
                    assert f.maker_owner_id != owner
                if tif == Tif.ALO:
                    assert not r.fills and r.rested_lots == sz
            log.append((r.accepted, r.order_id, [(f.maker_order_id, f.price_tick, f.size_lots) for f in r.fills or []]))
        else:
            _, owner, oid = op
            log.append(e.cancel(Cancel(owner, oid)))
        e.check_full()
    return e, log


@settings(max_examples=300, deadline=None)
@given(ops)
def test_invariants_hold(seq):
    run(seq)


@settings(max_examples=100, deadline=None)
@given(ops)
def test_deterministic(seq):
    e1, l1 = run(seq)
    e2, l2 = run(seq)
    assert l1 == l2 and market_hash(e1) == market_hash(e2)
