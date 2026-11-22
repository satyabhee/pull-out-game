"""engine_py/replay.py deep_book: liquidity past the full snapshot's last level from the coarse (sf4) book, split
by what was observable, used only by orders whose own limit reaches past that level."""
import json

from engine_py.replay import Replay, listing
from engine_py.types import ASSETS, Side
from tests.window_fixture import A, B, T0, Window, order, place_tx

BTC = ASSETS[0]


def lots(x):
    return BTC.sz_to_lots(x)


def deep_window(tmp_path, taker, coarse_asks=(("60000", "0.001"), ("60010", "0.004"), ("60020", "0.004")), evidence=True):
    """Full book: asks 60000 x 0.001, 60001 x 0.001 (the last full level). Coarse asks: buckets of $10.
    An earlier full snapshot showed 0.002 resting at 60005 (evidence for where the bucket's size sits)."""
    w = Window(tmp_path)
    if evidence:
        w.book("BTC", T0 - 200_000, [("59000", "1")], [("60005", "0.002")])
    w.book("BTC", T0 - 50, [("59990", "0.01")], [("60000", "0.001"), ("60001", "0.001")])
    w.block([place_tx(A, "0xa1", 0, taker)], dt_ms=100)
    window, hl = w.write()
    hour = next(p for p in hl.glob("*/*") if p.is_dir())
    lv = lambda xs: [{"px": p, "sz": s, "n": 1} for p, s in xs]
    (hour / "book_BTC_sf4.jsonl").write_text(json.dumps({"msg": {"data": {"coin": "BTC", "time": T0 - 50, "levels": [
        lv([("59990", "0.01"), ("59980", "0.01")]), lv(list(coarse_asks))]}}}) + "\n")
    return window, hl


def taken(r):
    return sorted((BTC.ticks_to_px(f.px), BTC.lots_to_sz(f.sz)) for _, fills, _, _ in r for f in fills)


def asks(r):
    return [(BTC.ticks_to_px(px), BTC.lots_to_sz(lv.total)) for px, lv in r.engine.books[0].side(Side.SELL).items()]


def test_an_order_reaching_past_the_last_level_finds_the_deep_reserve_where_it_was_seen(tmp_path):
    window, hl = deep_window(tmp_path, order(px="60005", sz="0.004", tif="Ioc"))
    off = Replay(window, hl, listing)
    assert taken(off) == [("60000", "0.001"), ("60001", "0.001")]          # nothing past the 20 levels
    on = Replay(window, hl, listing, deep_book=True)
    assert taken(on) == [("60000", "0.001"), ("60001", "0.001"), ("60005", "0.002")]
    assert on._deep[(0, Side.SELL)][BTC.px_to_ticks("60005")] == lots("0.001")   # what it left stays in reserve
    assert asks(on) == []                                                  # ... and out of the book
    assert on.deep_stats["orders_reaching_past"] == 1 and on.deep_stats["consumed_lots"] == lots("0.002")


def test_without_evidence_the_bucket_is_spread_evenly(tmp_path):
    window, hl = deep_window(tmp_path, order(px="60010", sz="1", tif="Ioc"), evidence=False)
    r = Replay(window, hl, listing, deep_book=True)
    res = r._deep[(0, Side.SELL)]
    first = {p: q for p, q in res.items() if p <= BTC.px_to_ticks("60010")}
    assert sum(first.values()) == lots("0.003") and len(first) == 90       # 60001.1 .. 60010
    assert r.deep_stats["buckets_even"] == 3                               # two ask buckets, one bid bucket


def test_orders_that_do_not_reach_past_or_only_rest_never_see_it(tmp_path):
    for i, o in enumerate((order(px="60001", sz="0.004", tif="Ioc"), order(asset=0, buy=False, px="60100", tif="Alo"))):
        window, hl = deep_window(tmp_path / str(i), o)
        r = Replay(window, hl, listing, deep_book=True)
        list(r)
        assert r.deep_stats["orders_reaching_past"] == 0
