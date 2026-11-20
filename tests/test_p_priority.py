"""Engine p_priority: at one price, higher grouping.p is served first (analysis/p_priority.py)."""
import pytest

from engine_py.matching import Engine
from engine_py.types import Place, Side, Tif

N = iter(range(1, 10_000))


def place(user, side, px, sz, tif=Tif.ALO, p=None, block=1):
    return Place(user=user, asset=0, block=block, tx_index=next(N), item_index=0, hash="0x" + "1" * 64,
                 priority=p, side=side, px=px, sz=sz, tif=tif, cloid=None, reduce_only=False)


def queue(eng, px, side=Side.SELL):
    return [n.order.user for n in eng.books[0].side(side)[px]]


def run(mode, times_and_orders):
    eng = Engine(p_priority=mode)
    for t, a in times_and_orders:
        eng.block_time_ms = t
        eng.apply(a)
    return eng


def test_default_is_fifo():
    eng = run(None, [(0, place("0xa", Side.SELL, 100, 1, p=1)), (0, place("0xb", Side.SELL, 100, 1, p=9))])
    assert queue(eng, 100) == ["0xa", "0xb"]


def test_always_orders_by_p_then_arrival():
    eng = run("always", [(0, place("0xa", Side.SELL, 100, 1, p=1)), (0, place("0xb", Side.SELL, 100, 1, p=9)),
                         (5000, place("0xc", Side.SELL, 100, 1, p=9)), (6000, place("0xd", Side.SELL, 100, 1))])
    assert queue(eng, 100) == ["0xb", "0xc", "0xa", "0xd"]


def test_window_only_jumps_recent_orders():
    eng = run(500, [(0, place("0xa", Side.SELL, 100, 1, p=1)), (1000, place("0xb", Side.SELL, 100, 1, p=2)),
                    (1400, place("0xc", Side.SELL, 100, 1, p=9))])
    assert queue(eng, 100) == ["0xa", "0xc", "0xb"]   # c jumps b (400 ms) but not a (1400 ms)


def test_placeholders_count_as_p0():
    eng = Engine(p_priority=500)
    eng.seed(0, Side.SELL, 100, 5)
    eng.apply(place("0xb", Side.SELL, 100, 1, p=9))
    assert queue(eng, 100) == ["placeholder", "0xb"]
    eng = Engine(p_priority="always")
    eng.seed(0, Side.SELL, 100, 5)
    eng.apply(place("0xb", Side.SELL, 100, 1, p=9))
    assert queue(eng, 100) == ["0xb", "placeholder"]


def test_fill_follows_p_order():
    eng = run("always", [(0, place("0xa", Side.SELL, 100, 1, p=1)), (0, place("0xb", Side.SELL, 100, 1, p=9))])
    fills, _ = eng.apply(place("0xt", Side.BUY, 100, 1, tif=Tif.IOC))
    assert [f.maker_user for f in fills] == ["0xb"]
    assert eng.p_meta.keys() == {eng.books[0].side(Side.SELL)[100].first().order.oid}


def test_bad_mode():
    with pytest.raises(ValueError):
        Engine(p_priority="sometimes")
