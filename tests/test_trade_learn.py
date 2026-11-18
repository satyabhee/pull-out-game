"""Replay(trade_learn=...): each tx's real trades correct the book for later txs only."""
import pytest

from analysis import validate_replay as vr
from engine_py.matching import PLACEHOLDER
from engine_py.replay import Replay, listing
from engine_py.types import ASSETS, Side
from tests.window_fixture import A, B, M, T0, Window, order, place_tx

BTC = ASSETS[0]


def ticks(p):
    return BTC.px_to_ticks(p)


def lots(s):
    return BTC.sz_to_lots(s)


def level(r, side, px):
    lv = r.engine.books[0].side(side).get(ticks(px))
    return [] if lv is None else [(nd.order.user, nd.remaining) for nd in lv]


def sweep_window(tmp_path):
    """B rests 0.003 @ 60005 (the chain no longer has it); A buys 0.001 up to 60020 and the chain fills
    it from M at 60010, so everything better than 60010 was gone on chain."""
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [], [("60010", "0.002"), ("60020", "0.002")])
    w.block([place_tx(B, "0xb1", 0, order(buy=False, px="60005", sz="0.003", tif="Gtc"))], dt_ms=0)
    h = w.block([place_tx(A, "0xa1", 0, order(px="60020", sz="0.001", tif="Ioc"))], dt_ms=1000)
    w.trade("BTC", "B", "60010", "0.001", w.time_of(h), "0xa1", 1, A, M)
    w.block([], dt_ms=1500)
    return w.write()


def test_sweep_removes_what_the_real_taker_must_have_consumed(tmp_path):
    window, hl = sweep_window(tmp_path)
    off = list(Replay(window, hl, listing))
    r = Replay(window, hl, listing, trade_learn="sweep")
    on = list(r)
    assert [x[1] for x in on] == [x[1] for x in off]          # the tx itself is applied before learning
    assert level(r, Side.SELL, "60005") == []                  # B's remaining 0.002 was stale
    assert r.learn_stats["swept_known_lots:BTC"] == lots("0.002")
    assert level(r, Side.SELL, "60010") == [(PLACEHOLDER, lots("0.002"))]   # w itself is untouched
    r0 = Replay(window, hl, listing)
    list(r0)
    assert level(r0, Side.SELL, "60005") == [(B, lots("0.002"))]


def test_name_turns_the_front_placeholder_at_the_last_price_into_the_real_maker(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [], [("60010", "0.005")])
    h1 = w.block([place_tx(A, "0xa1", 0, order(px="60010", sz="0.001", tif="Ioc"))], dt_ms=0)
    w.trade("BTC", "B", "60010", "0.001", w.time_of(h1), "0xa1", 1, A, M)
    w.block([place_tx(A, "0xa2", 0, order(px="60010", sz="0.001", tif="Ioc"))], dt_ms=1000)
    window, hl = w.write()
    r = Replay(window, hl, listing, trade_learn="name")
    out = list(r)
    assert [f.maker_user for f in out[0][1]] == [PLACEHOLDER]      # scored before anything was learned
    assert [f.maker_user for f in out[1][1]] == [M]                # the later tx fills M's named order
    assert level(r, Side.SELL, "60010") == [(M, lots("0.003"))]
    assert r.learn_stats["named_orders:BTC"] == 1


def test_name_leaves_a_level_alone_when_the_maker_is_already_known_or_a_known_order_is_first(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [], [])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60010", sz="0.003", tif="Gtc"))], dt_ms=0)
    h = w.block([place_tx(A, "0xa1", 0, order(px="60010", sz="0.001", tif="Ioc"))], dt_ms=1000)
    w.trade("BTC", "B", "60010", "0.001", w.time_of(h), "0xa1", 1, A, M)
    window, hl = w.write()
    r = Replay(window, hl, listing, trade_learn="both")
    list(r)
    assert level(r, Side.SELL, "60010") == [(M, lots("0.002"))] and r.learn_stats["named_orders:BTC"] == 0


def test_bad_mode_fails_loudly_and_summary_reports_only_when_set(tmp_path):
    window, hl = sweep_window(tmp_path)
    with pytest.raises(ValueError):
        Replay(window, hl, listing, trade_learn="all")
    res, _ = vr.run(window, hl, quiet=True)
    assert not any(k.startswith(("trade_learn", "learn:")) for k in res)
    res, _ = vr.run(window, hl, quiet=True, trade_learn="sweep")
    assert res["trade_learn"] == "sweep" and res["learn:swept_known_lots:BTC"] == lots("0.002")
