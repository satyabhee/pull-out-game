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


def test_known_never_jumps_placeholders_but_jumps_in_window_p0():
    eng = Engine(p_priority="known")
    eng.seed(0, Side.SELL, 100, 5)
    eng.apply(place("0xa", Side.SELL, 100, 1))            # in window, no grouping.p -> p 0
    eng.apply(place("0xb", Side.SELL, 100, 1, p=9))
    assert queue(eng, 100) == ["placeholder", "0xb", "0xa"]


def test_known_does_not_jump_orders_rested_without_p_meta():
    eng = Engine(p_priority="known")
    named = eng._new_order("0xn", 0, Side.SELL, 100, 1, Tif.GTC, None)
    eng._rest(named, 1)                                    # named pre-window order (replay._rest_named path)
    eng.apply(place("0xb", Side.SELL, 100, 1, p=9))
    assert queue(eng, 100) == ["0xn", "0xb"]


def test_window_mode_unchanged_for_p0_orders():
    eng = run(500, [(0, place("0xa", Side.SELL, 100, 1, p=0)), (100, place("0xb", Side.SELL, 100, 1, p=9))])
    assert queue(eng, 100) == ["0xa", "0xb"]


def test_evict_arrival_trims_oldest_arrival_not_queue_head():
    for flag, left in ((False, ["0xa"]), (True, ["0xb"])):
        eng = Engine(p_priority="always", evict_arrival=flag)
        eng.apply(place("0xa", Side.SELL, 100, 1, p=1))
        eng.apply(place("0xb", Side.SELL, 100, 1, p=9))    # queue: b, a; arrival: a, b
        eng.reconcile(0, [], [(100, 1)], known="oldest")
        assert queue(eng, 100) == left


def test_knownpos_never_jumps_p0_orders_but_jumps_lower_positive_p():
    eng = Engine(p_priority="knownpos")
    eng.apply(place("0xa", Side.SELL, 100, 1))            # no grouping.p -> p 0: keeps its place
    eng.apply(place("0xb", Side.SELL, 100, 1, p=3))
    eng.apply(place("0xc", Side.SELL, 100, 1, p=9))       # jumps b (3 < 9), not a (p 0)
    assert queue(eng, 100) == ["0xa", "0xc", "0xb"]
