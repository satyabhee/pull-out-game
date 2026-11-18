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


def test_name_clip_names_only_the_just_filled_size_and_keeps_the_rest_anonymous_behind(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [], [("60010", "0.005")])
    h1 = w.block([place_tx(A, "0xa1", 0, order(px="60010", sz="0.001", tif="Ioc"))], dt_ms=0)
    w.trade("BTC", "B", "60010", "0.001", w.time_of(h1), "0xa1", 1, A, M)
    window, hl = w.write()
    r = Replay(window, hl, listing, trade_learn="both-clip")
    list(r)
    assert r._rules == {"sweep", "name-clip"}
    assert level(r, Side.SELL, "60010") == [(M, lots("0.001")), (PLACEHOLDER, lots("0.003"))]
    r.engine.books[0].check_invariants()


def test_queue_removes_what_rests_ahead_of_a_filled_makers_order(tmp_path):
    # Engine queue at 60010: placeholder 0.002, B 0.001, M 0.002. The chain fills A's 0.001 buy from M,
    # so on chain everything ahead of M was gone: the placeholder and B are stale.
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [], [("60010", "0.002")])
    w.block([place_tx(B, "0xb1", 0, order(buy=False, px="60010", sz="0.001", tif="Gtc")),
             place_tx(M, "0xm1", 1, order(buy=False, px="60010", sz="0.002", tif="Gtc"))], dt_ms=0)
    h = w.block([place_tx(A, "0xa1", 0, order(px="60010", sz="0.001", tif="Ioc"))], dt_ms=1000)
    w.trade("BTC", "B", "60010", "0.001", w.time_of(h), "0xa1", 1, A, M)
    window, hl = w.write()
    r = Replay(window, hl, listing, trade_learn="queue")
    out = list(r)
    assert [f.maker_user for f in out[1][1]] == [PLACEHOLDER]      # the tx itself: engine queue as it was
    assert level(r, Side.SELL, "60010") == [(M, lots("0.002"))]
    assert r.learn_stats["queue_placeholder_lots:BTC"] == lots("0.001")
    assert r.learn_stats["queue_known_lots:BTC"] == lots("0.001")


def test_trade_learn_rules_parse_and_reject_bad_combinations():
    assert Replay.parse_trade_learn("both") == {"sweep", "name"}
    assert Replay.parse_trade_learn("sweep,queue,name-clip") == {"sweep", "queue", "name-clip"}
    for bad in ("name,name-clip", "sweep,all", ""):
        with pytest.raises(ValueError):
            Replay.parse_trade_learn(bad)


def test_oid_guard_presets_reach_the_resolver_and_bad_ones_fail(tmp_path):
    from engine_py.replay import OID_GUARDS
    assert OID_GUARDS["cap3000"] == dict(ghost_guard=False, max_tol=3000)
    window, hl = sweep_window(tmp_path)
    with pytest.raises(ValueError):
        Replay(window, hl, listing, oid_guard="loose")
    res, _ = vr.run(window, hl, quiet=True, oid_guard="ratio50")
    assert res["oid_guard"] == "ratio50"
    res, _ = vr.run(window, hl, quiet=True)
    assert "oid_guard" not in res


def test_cli_default_is_sweep_queue_name_clip_and_off_disables_it(tmp_path):
    window, hl = sweep_window(tmp_path)
    vr.main([str(window), "--hl-data", str(hl), "--out", str(tmp_path / "d")])
    assert f"trade_learn,\"{vr.DEFAULT_TRADE_LEARN}\"" in (tmp_path / "d" / "summary.csv").read_text()
    vr.main([str(window), "--hl-data", str(hl), "--trade-learn", "off", "--out", str(tmp_path / "o")])
    assert "trade_learn" not in (tmp_path / "o" / "summary.csv").read_text()


def test_refill_back_puts_missing_snapshot_size_behind_known_orders(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [], [])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60010", sz="0.001", tif="Gtc"))], dt_ms=0)
    w.book("BTC", T0 + 500, [], [("60010", "0.003")])
    w.block([], dt_ms=1000)
    window, hl = w.write()
    front = Replay(window, hl, listing, reseed=True, keep_known=True)
    list(front)
    assert level(front, Side.SELL, "60010") == [(PLACEHOLDER, lots("0.002")), (M, lots("0.001"))]
    back = Replay(window, hl, listing, reseed=True, keep_known=True, refill_position="back")
    list(back)
    assert level(back, Side.SELL, "60010") == [(M, lots("0.001")), (PLACEHOLDER, lots("0.002"))]
    back.engine.books[0].check_invariants()
    with pytest.raises(ValueError):
        Replay(window, hl, listing, refill_position="middle")
