"""A4a --placeholder-split: snapshot levels become n equal placeholders (engine_py.matching.split_equal, refill)."""
import pytest

from engine_py.matching import PLACEHOLDER, Engine, split_equal
from engine_py.types import Side


def test_split_equal_is_exact_and_deterministic():
    assert split_equal(10, 3) == [4, 3, 3] and sum(split_equal(10**9 + 7, 13)) == 10**9 + 7
    assert split_equal(5, 1) == [5]
    with pytest.raises(ValueError):
        split_equal(2, 3)


def test_refill_with_counts_adds_one_placeholder_per_uncovered_order():
    eng = Engine()
    eng.refill(0, [(1000, 10)], [(1001, 7)], counts=((3,), (1,)))
    bids = [(nd.order.user, nd.remaining) for nd in eng.books[0].side(Side.BUY)[1000]]
    asks = [(nd.order.user, nd.remaining) for nd in eng.books[0].side(Side.SELL)[1001]]
    assert bids == [(PLACEHOLDER, 4), (PLACEHOLDER, 3), (PLACEHOLDER, 3)] and asks == [(PLACEHOLDER, 7)]


def test_refill_without_counts_is_unchanged():
    eng = Engine()
    eng.refill(0, [(1000, 10)], [], counts=None)
    assert [nd.remaining for nd in eng.books[0].side(Side.BUY)[1000]] == [10]


def test_refill_counts_subtract_orders_already_held():
    eng = Engine()
    eng.seed(0, Side.BUY, 1000, 4)                       # one order already at the level
    eng.refill(0, [(1000, 10)], [], counts=((3,), ()))   # snapshot: 3 orders, 10 lots -> 2 more orders, 6 lots
    assert [nd.remaining for nd in eng.books[0].side(Side.BUY)[1000]] == [3, 3, 4]
