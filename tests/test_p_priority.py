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


def test_docs_example_zero_fee_jumped_in_window_then_locked():
    # priority-fees doc: A (p 0) at t', B (p > 0) at t' + T - 1 -> B, A; C (p' > p) at t' + T + 1 -> B, A, C
    eng = run("docs", [(0, place("0xa", Side.SELL, 100, 1)), (399, place("0xb", Side.SELL, 100, 1, p=3)),
                       (401, place("0xc", Side.SELL, 100, 1, p=9))])
    assert queue(eng, 100) == ["0xb", "0xa", "0xc"]


def test_docs_alo_only_docsall_any_tif():
    seq = [(0, place("0xa", Side.SELL, 100, 1, tif=Tif.GTC)), (10, place("0xb", Side.SELL, 100, 1, p=3))]
    assert queue(run("docs", seq), 100) == ["0xa", "0xb"]
    assert queue(run("docsall", seq), 100) == ["0xb", "0xa"]


def test_docs_window_configurable():
    # I01: "docs:<ms>" changes only T; docs:400 == docs
    seq = [(0, place("0xa", Side.SELL, 100, 1)), (350, place("0xb", Side.SELL, 100, 1, p=3))]
    assert queue(run("docs:400", seq), 100) == queue(run("docs", seq), 100) == ["0xb", "0xa"]
    assert queue(run("docs:300", seq), 100) == ["0xa", "0xb"]          # 350 ms old: locked under T = 300
    seq2 = [(0, place("0xa", Side.SELL, 100, 1)), (450, place("0xb", Side.SELL, 100, 1, p=3))]
    assert queue(run("docs", seq2), 100) == ["0xa", "0xb"]
    assert queue(run("docs:500", seq2), 100) == ["0xb", "0xa"]
    assert queue(run("docsall:500", [(0, place("0xa", Side.SELL, 100, 1, tif=Tif.GTC)),
                                      (450, place("0xb", Side.SELL, 100, 1, p=3))]), 100) == ["0xb", "0xa"]
    with pytest.raises(ValueError):
        Engine(p_priority="docs:-1")


def test_touch_trades_first_keeps_post_block_bbo():
    """I25: a block's trades and its post-block bbo share the block time; with trades_first the bbo (state AFTER the
    block) wins, so a level the block consumed is not reported as the touch. Default keeps the historical order."""
    from engine_py.replay import RealTouch
    from engine_py.types import Side

    class T:
        def __init__(self, t, px):
            self.time_ms, self.asset, self.seq, self.taker_side, self.px = t, 0, 0, Side.BUY, px
    trades = [T(1000, 100)]                         # a taker buy consumed the ask at 100 during block t=1000
    bbos = [(1000, 0, (95, 1, 1), (104, 1, 1))]     # after the block: bid 95, ask 104
    old = RealTouch((), trades, bbos)
    old.advance(1100)
    assert old.get(0, 1100)[:2] == (95, 100)        # B1: the intra-block trade price overwrites the bbo
    new = RealTouch((), trades, bbos, trades_first=True)
    new.advance(1100)
    assert new.get(0, 1100)[:2] == (95, 104)


def test_phantom_sink_jumps_flagged_and_flagged_never_jumps():
    # I27c: a later non-flagged order goes ahead of trailing flagged orders; a flagged order rests at the tail
    eng = Engine(p_priority="docs")
    eng.sink = lambda u: u == "0xbad"
    for a in (place("0xa", Side.SELL, 100, 1, p=1), place("0xbad", Side.SELL, 100, 1, p=50),
              place("0xb", Side.SELL, 100, 1, p=0), place("0xbad", Side.SELL, 100, 1, p=99)):
        eng.block_time_ms = 0
        eng.apply(a)
    assert queue(eng, 100) == ["0xa", "0xb", "0xbad", "0xbad"]


def test_phantom_sink_off_is_unchanged():
    eng = Engine(p_priority="docs")
    for a in (place("0xa", Side.SELL, 100, 1, p=1), place("0xbad", Side.SELL, 100, 1, p=50)):
        eng.block_time_ms = 0
        eng.apply(a)
    assert queue(eng, 100) == ["0xbad", "0xa"]          # docs p jump within the window


def test_px_sigfig_rule():
    # I33: HL rejects a non-integer price with more than 5 significant figures; integers always allowed
    from engine_py.replay import Replay
    mk = lambda px, asset=0: Place(user="0xa", asset=asset, block=1, tx_index=0, item_index=0, hash="0x" + "1" * 64,
                                   priority=None, side=Side.BUY, px=px, sz=1, tif=Tif.ALO, cloid=None, reduce_only=False)
    assert Replay._over_sigfig(mk(609672))          # BTC 60967.2: 6 significant figures
    assert not Replay._over_sigfig(mk(609080))      # BTC 60908: integer
    assert not Replay._over_sigfig(mk(171280, 1))   # ETH 1712.80 = 1712.8: 5 significant figures
    assert Replay._over_sigfig(mk(171285, 1))       # ETH 1712.85: 6
