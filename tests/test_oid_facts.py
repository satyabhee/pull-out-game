"""data/oid_facts.py + data/oids.py: real order-id facts (info lookups, recorder user streams) become anchors,
exact oid -> placement matches and side/price filters for int-oid cancels."""
import json

from analysis import validate_replay as vr
from data import oid_facts
from data.oids import OidEstimator
from engine_py.replay import Replay, listing
from engine_py.types import ASSETS, Side
from tests.window_fixture import A, B, M, T0, Window, cancel_tx, order, place_tx

BTC = ASSETS[0]


def resting(r, user):
    return sorted((BTC.ticks_to_px(nd.order.px), nd.order.side is Side.BUY)
                  for lv in r.engine.books[0].levels(Side.BUY) for nd in lv if nd.order and nd.order.user == user) + \
        sorted((BTC.ticks_to_px(nd.order.px), False)
               for lv in r.engine.books[0].levels(Side.SELL) for nd in lv if nd.order and nd.order.user == user)


def hist(user, oid, px, sz, ts, side="B", status="canceled"):
    return {"user": user, "kind": "historicalOrders", "answer": [
        {"order": {"coin": "BTC", "side": side, "limitPx": px, "sz": "0.0", "origSz": sz, "oid": oid,
                   "timestamp": ts, "cloid": None}, "status": status, "statusTimestamp": ts + 5}]}


def two_orders_then_cancel(tmp_path, cancel_oid=777):
    """A rests two buys in one tx; a later tx cancels by int oid. No anchors.json at all."""
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [], [])
    h = w.block([place_tx(A, "0xa1", 0, order(px="60000"), order(px="59990"))], dt_ms=0)
    w.block([cancel_tx(A, "0xa2", 0, 0, cancel_oid)], dt_ms=500)
    window, hl = w.write()
    return w, window, hl, w.time_of(h)


def test_a_fact_order_resolves_its_cancel_exactly_without_any_anchor_file(tmp_path):
    w, window, hl, ts = two_orders_then_cancel(tmp_path)
    (window / "hist_orders.json").write_text(json.dumps([hist(A, 777, "59990.0", "0.001", ts)]))
    off = Replay(window, hl, listing)
    list(off)
    assert off.resolver is None and len(resting(off, A)) == 2       # no anchors: the cancel cannot be resolved
    on = Replay(window, hl, listing, oid_facts=True)
    list(on)
    assert resting(on, A) == [("60000", True)]
    assert on.resolver.stats["resolved_exact"] == 1
    assert on.resolver.est.exact == {777: (w.h0, 0, 1)}


def test_exact_never_falls_back_to_a_guess_when_the_target_is_gone(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [], [])
    h = w.block([place_tx(A, "0xa1", 0, order(px="60000", tif="Ioc"))], dt_ms=0)     # IOC: never rests
    w.block([place_tx(A, "0xa2", 0, order(px="60000"))], dt_ms=100)
    w.block([cancel_tx(A, "0xa3", 0, 0, 500)], dt_ms=500)
    window, hl = w.write()
    (window / "hist_orders.json").write_text(json.dumps([hist(A, 500, "60000.0", "0.001", w.time_of(h))]))
    r = Replay(window, hl, listing, oid_facts=True)
    list(r)
    assert r.resolver.stats["exact_gone"] == 1 and resting(r, A) == [("60000", True)]


def test_taker_fill_is_located_by_tx_hash_and_becomes_an_anchor(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [], [("60010", "0.002")])
    h = w.block([place_tx(A, "0xa1", 0, order(px="59000"), order(px="60020", tif="Ioc"))], dt_ms=0)
    w.trade("BTC", "B", "60010", "0.001", w.time_of(h), "0xa1", 1, A, M)
    window, hl = w.write()
    fills = [{"user": A, "answer": [{"coin": "BTC", "px": "60010.0", "sz": "0.001", "side": "B", "time": w.time_of(h),
                                     "hash": "0xa1", "oid": 4242, "crossed": True, "tid": 1}]}]
    (window / "fills.json").write_text(json.dumps(fills))
    est = OidEstimator.from_window(window, [], facts=oid_facts.load(window, hl))
    assert est.exact == {4242: (w.h0, 0, 1)} and est.anchors == [(1, 4242)]        # the IOC item, count 1
    assert est.estimate_count(0) == 4241                                           # the resting buy before it


def test_maker_fill_and_order_facts_give_side_price_and_conflicts_are_dropped(tmp_path):
    w, window, hl, ts = two_orders_then_cancel(tmp_path)
    facts = oid_facts.OidFacts(
        fills=[{"user": A.lower(), "asset": 0, "buy": True, "px": "60000", "oid": 9, "hash": "0xz", "crossed": False,
                "time": ts, "source": "fills"}],
        orders=[{"user": A.lower(), "asset": 0, "buy": True, "px": "1", "sz": "1", "timestamp": ts - 99, "oid": 11,
                 "cloid": None, "status": None, "source": "hist"},
                {"user": A.lower(), "asset": 0, "buy": True, "px": "2", "sz": "1", "timestamp": ts - 99, "oid": 11,
                 "cloid": None, "status": None, "source": "ws"}])
    est = OidEstimator.from_window(window, [], facts=facts)
    assert est.oid_px[9] == (A.lower(), 0, True, BTC.px_to_ticks("60000"), None)
    assert est.oid_px[11] is None                                                  # two sources disagree


def test_a_fact_older_than_the_window_is_prewindow(tmp_path):
    w, window, hl, ts = two_orders_then_cancel(tmp_path, cancel_oid=5)
    (window / "open_orders.json").write_text(json.dumps([{"user": A, "answer": [
        {"coin": "BTC", "side": "B", "limitPx": "60000.0", "sz": "0.001", "origSz": "0.001", "oid": 5,
         "timestamp": T0 - 10_000}]}]))
    r = Replay(window, hl, listing, oid_facts=True)
    list(r)
    assert r.resolver.stats["known_prewindow"] == 1 and len(resting(r, A)) == 2


def test_consistent_keeps_the_longest_increasing_set():
    assert OidEstimator.consistent([(0, 10), (1, 12), (2, 5), (3, 14), (3, 15)]) == [(0, 10), (1, 12)]
    assert OidEstimator.consistent([(0, 10), (5, 20), (6, 21)]) == [(0, 10), (5, 20), (6, 21)]
    assert OidEstimator.consistent([]) == []


def test_recorder_user_streams_are_loaded_within_the_window(tmp_path):
    w, window, hl, ts = two_orders_then_cancel(tmp_path)
    hour = next(p for p in hl.glob("*/*") if p.is_dir())
    rows = [{"recv_ms": ts, "msg": {"user": A, "data": [{"order": {"coin": "BTC", "side": "B", "limitPx": "59990.0",
            "sz": "0.001", "origSz": "0.001", "oid": 777, "timestamp": ts}, "status": "open", "statusTimestamp": ts}]}},
            {"recv_ms": ts, "msg": {"user": A, "data": [{"order": {"coin": "SOL", "side": "B", "limitPx": "1",
             "sz": "1", "origSz": "1", "oid": 1, "timestamp": ts}, "status": "open"}]}}]
    (hour / "user_orders.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
    (hour / "user_fills.jsonl").write_text(json.dumps({"recv_ms": ts, "msg": {"user": B, "data": {"user": B, "fills": [
        {"coin": "BTC", "px": "60000.0", "sz": "0.001", "side": "A", "time": ts, "hash": "0xq", "oid": 31,
         "crossed": False}]}}}) + "\n")
    f = oid_facts.load(window, hl)
    assert [(o["oid"], o["source"]) for o in f.orders] == [(777, "ws")]
    assert [(x["oid"], x["buy"]) for x in f.fills] == [(31, False)]
    r = Replay(window, hl, listing, oid_facts=True)
    list(r)
    assert r.resolver.stats["resolved_exact"] == 1


def test_cli_uses_facts_by_default_and_reports_them(tmp_path):
    w, window, hl, ts = two_orders_then_cancel(tmp_path)
    (window / "hist_orders.json").write_text(json.dumps([hist(A, 777, "59990.0", "0.001", ts)]))
    vr.main([str(window), "--hl-data", str(hl), "--out", str(tmp_path / "d")])
    s = (tmp_path / "d" / "summary.csv").read_text()
    assert "oid_facts,True" in s and "facts:exact,1" in s
    vr.main([str(window), "--hl-data", str(hl), "--no-oid-facts", "--out", str(tmp_path / "n")])
    assert "oid_facts" not in (tmp_path / "n" / "summary.csv").read_text()


def level_users(r, side, px):
    lv = r.engine.books[0].side(side).get(BTC.px_to_ticks(px))
    return [(nd.order.user, BTC.lots_to_sz(nd.remaining)) for nd in lv] if lv is not None else []


def prewindow_book(tmp_path, open_answer=(), ws_rows=()):
    """Snapshot bid 60000 x 0.005; A cancels real oid 5 in-window. Facts say what oid 5 was."""
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [("60000", "0.005")], [("60100", "0.001")])
    w.block([cancel_tx(A, "0xa1", 0, 0, 5)], dt_ms=500)
    window, hl = w.write()
    if open_answer:
        (window / "open_orders.json").write_text(json.dumps([{"user": A, "answered_at": (T0 + 5000) / 1000,
                                                               "answer": list(open_answer)}]))
    if ws_rows:
        hour = next(p for p in hl.glob("*/*") if p.is_dir())
        (hour / "user_orders.jsonl").write_text("".join(json.dumps(x) + "\n" for x in ws_rows))
    return w, window, hl


def oo(oid, px="60000.0", sz="0.002", ts=T0 - 10_000):
    return {"coin": "BTC", "side": "B", "limitPx": px, "sz": sz, "origSz": sz, "oid": oid, "timestamp": ts}


def test_a_prewindow_order_takes_its_share_of_the_placeholder_and_its_cancel_finds_it(tmp_path):
    w, window, hl = prewindow_book(tmp_path, ws_rows=[
        {"recv_ms": T0 + 500, "msg": {"user": A, "data": [{"order": oo(5), "status": "canceled",
                                                             "statusTimestamp": T0 + 500}]}}])
    off = Replay(window, hl, listing, oid_facts=True)
    list(off)
    assert level_users(off, Side.BUY, "60000") == [("placeholder", "0.005")]       # the cancel could not land
    on = Replay(window, hl, listing, oid_facts=True, name_prewindow=True)
    assert level_users(on, Side.BUY, "60000") == [("placeholder", "0.003"), (A.lower(), "0.002")]
    list(on)
    assert on.resolver.stats["resolved_named"] == 1
    assert level_users(on, Side.BUY, "60000") == [("placeholder", "0.003")]


def test_named_orders_queue_by_real_oid_and_anonymous_rest_can_go_behind(tmp_path):
    w, window, hl = prewindow_book(tmp_path, open_answer=[oo(9, sz="0.001"), oo(4, sz="0.001")])
    r = Replay(window, hl, listing, oid_facts=True, name_prewindow=True, named_anon="back")
    assert r.named == {4: r.named[4], 9: r.named[9]} and r.named[4] < r.named[9]
    assert level_users(r, Side.BUY, "60000") == [(A.lower(), "0.001"), (A.lower(), "0.001"), ("placeholder", "0.003")]


def test_gone_before_the_snapshot_is_not_named_and_deeper_prices_are_added_outright(tmp_path):
    w, window, hl = prewindow_book(tmp_path, open_answer=[oo(7, px="59000.0")], ws_rows=[
        {"recv_ms": T0 - 60, "msg": {"user": A, "data": [{"order": oo(5), "status": "canceled",
                                                            "statusTimestamp": T0 - 60}]}}])
    r = Replay(window, hl, listing, oid_facts=True, name_prewindow=True)
    assert 5 not in r.named and level_users(r, Side.BUY, "60000") == [("placeholder", "0.005")]
    assert level_users(r, Side.BUY, "59000") == [(A.lower(), "0.002")] and r.name_stats["named_deep"] == 1


def test_a_price_inside_the_snapshot_range_it_does_not_show_is_a_conflict(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [("60000", "0.005"), ("59990", "0.001")], [("60100", "0.001")])
    w.block([cancel_tx(A, "0xa1", 0, 0, 5)], dt_ms=500)
    window, hl = w.write()
    (window / "open_orders.json").write_text(json.dumps([{"user": A, "answered_at": (T0 + 5000) / 1000,
                                                           "answer": [oo(3, px="59995.0")]}]))
    r = Replay(window, hl, listing, oid_facts=True, name_prewindow=True)
    assert r.named == {} and r.name_stats["named_conflict"] == 1


def test_stream_order_truth_joins_tracked_statuses_by_cloid_and_by_shape(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [], [("60100", "0.001")])
    h = w.block([place_tx(A, "0xa1", 0, order(px="60000", cloid="0x01"), order(px="59990"))], dt_ms=0)
    window, hl = w.write()
    ts = w.time_of(h)
    hour = next(p for p in hl.glob("*/*") if p.is_dir())
    o = lambda px, oid, c=None: {"coin": "BTC", "side": "B", "limitPx": px, "sz": "0.001", "origSz": "0.001",
                                  "oid": oid, "timestamp": ts, "cloid": c}
    rows = [{"recv_ms": ts, "msg": {"user": A, "data": [
        {"order": o("60000.0", 1, "0x01"), "status": "badAloPxRejected", "statusTimestamp": ts},
        {"order": o("59990.0", 2), "status": "open", "statusTimestamp": ts}]}}]
    (hour / "user_orders.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
    r = Replay(window, hl, listing, oid_facts=True)
    t = vr.stream_order_truth(r, hl)
    assert {k[2]: v["truth"] for k, v in t.items()} == {0: "rejected_alo", 1: "accepted"}


def test_fact_sources_can_be_left_out_for_ablations(tmp_path):
    import pytest
    w, window, hl, ts = two_orders_then_cancel(tmp_path)
    (window / "hist_orders.json").write_text(json.dumps([hist(A, 777, "59990.0", "0.001", ts)]))
    assert len(oid_facts.load(window, hl).orders) == 1
    assert oid_facts.load(window, hl, exclude=("hist",)).orders == []
    with pytest.raises(ValueError):
        oid_facts.load(window, hl, exclude=("nope",))
    r = Replay(window, hl, listing, oid_facts=True, facts_exclude=("hist",))
    list(r)
    assert r.resolver.stats.get("resolved_exact", 0) == 0          # the only fact was left out
