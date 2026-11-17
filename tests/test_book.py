"""Unit tests for levels, queues, cancels. Invariants are checked after every operation."""

import pytest

from engine_py.book import Book, BookError
from engine_py.types import Order, Side, Tif

_oid = iter(range(1, 10_000))


def order(side, px, sz=10, user="0xu"):
    oid = next(_oid)
    return Order(oid=oid, user=user, asset=0, side=side, px=px, sz=sz, tif=Tif.GTC,
                 cloid=None, seq=oid, block=1)


def queue(book, side, px):
    return [n.order.oid for n in book.side(side)[px]]


@pytest.fixture
def book():
    return Book(0)


def test_empty(book):
    assert book.best_bid() is None and book.best_ask() is None
    book.check_invariants()


def test_add_and_best_prices(book):
    for side, px in [(Side.BUY, 100), (Side.BUY, 102), (Side.BUY, 101),
                     (Side.SELL, 105), (Side.SELL, 103), (Side.SELL, 104)]:
        book.add(order(side, px), 10)
        book.check_invariants()
    assert book.best_bid() == 102 and book.best_ask() == 103
    assert [lv.px for lv in book.levels(Side.BUY)] == [102, 101, 100]
    assert [lv.px for lv in book.levels(Side.SELL)] == [103, 104, 105]


def test_fifo_within_level(book):
    a, b, c = (order(Side.BUY, 100) for _ in range(3))
    for o in (a, b, c):
        book.add(o, 10)
    assert queue(book, Side.BUY, 100) == [a.oid, b.oid, c.oid]
    assert book.best_level(Side.BUY).total == 30


@pytest.mark.parametrize("which", [0, 1, 2], ids=["head", "middle", "tail"])
def test_cancel_positions(book, which):
    orders = [order(Side.SELL, 105, sz=5 + i) for i in range(3)]
    for o in orders:
        book.add(o, o.sz)
    book.remove(orders[which].oid)
    book.check_invariants()
    assert queue(book, Side.SELL, 105) == [o.oid for i, o in enumerate(orders) if i != which]
    assert book.best_level(Side.SELL).total == sum(o.sz for i, o in enumerate(orders) if i != which)
    assert book.best_ask() == 105


def test_cancel_last_in_level_removes_level(book):
    best, other = order(Side.BUY, 101), order(Side.BUY, 100)
    book.add(best, 10)
    book.add(other, 10)
    book.remove(best.oid)
    book.check_invariants()
    assert 101 not in book.bids and book.best_bid() == 100
    book.remove(other.oid)
    book.check_invariants()
    assert book.best_bid() is None and len(book.index) == 0


def test_partial_and_full_reduce(book):
    o = order(Side.SELL, 103, sz=10)
    book.add(o, 10)
    assert book.reduce(o.oid, 4).remaining == 6
    book.check_invariants()
    assert book.best_level(Side.SELL).total == 6
    assert book.reduce(o.oid, 6).remaining == 0
    book.check_invariants()
    assert book.best_ask() is None and o.oid not in book


def test_partial_fill_keeps_queue_position(book):
    a, b = order(Side.SELL, 103), order(Side.SELL, 103)
    book.add(a, 10)
    book.add(b, 10)
    book.reduce(a.oid, 3)
    assert queue(book, Side.SELL, 103) == [a.oid, b.oid]


@pytest.mark.parametrize("bad", ["overfill", "unknown_cancel", "duplicate", "bad_remaining"])
def test_impossible_operations_fail_loudly(book, bad):
    o = order(Side.BUY, 100, sz=10)
    book.add(o, 10)
    with pytest.raises(BookError):
        if bad == "overfill":
            book.reduce(o.oid, 11)
        elif bad == "unknown_cancel":
            book.remove(999_999)
        elif bad == "duplicate":
            book.add(o, 10)
        else:
            book.add(order(Side.BUY, 100, sz=5), 6)


def test_crossed_book_is_detected(book):
    book.add(order(Side.BUY, 100), 10)
    book.add(order(Side.SELL, 100), 10)   # the book itself allows it; the check must not
    with pytest.raises(BookError, match="crossed"):
        book.check_invariants()
