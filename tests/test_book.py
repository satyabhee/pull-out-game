"""Unit tests for levels, queues, cancels: every worked example in specs/01-book.md."""
from decimal import Decimal

import pytest

from data.parse import RawCancel, RawModify, RawPlace, parse_block
from engine_py.matching import MarketEngine
from engine_py.ordering import first_come, project_priority
from engine_py.replay import Replay
from engine_py.state import market_hash
from engine_py.types import (Cancel, EngineError, MarketConfig, Place, RejectReason, Side, Tif,
                             hyperliquid_perp_config)

CFG = MarketConfig("TEST", 1, Decimal("0.01"), Decimal("0.001"), Decimal("0.00001"),
                   min_price_tick=1, max_price_tick=10**9, max_order_lots=10**9, max_order_notional=10**15)
BUY, SELL = Side.BUY, Side.SELL


def eng():
    return MarketEngine(CFG)


def put(e, owner, side, px, sz, tif=Tif.GTC):
    return e.submit(Place(owner, side, px, sz, tif))


def fills(r):
    return [(f.size_lots, f.price_tick, f.maker_order_id) for f in r.fills]


# ---- section 0: units ----------------------------------------------------------------------------
def test_exact_conversion():
    assert CFG.to_ticks("101.25") == 10125
    assert CFG.to_lots("2.345") == 2345
    assert CFG.to_ticks("101.250000") == 10125            # same value, different format


@pytest.mark.parametrize("bad", ["101.255", "abc", "-1", "NaN", "Infinity", "1e400"])
def test_non_multiples_rejected_not_rounded(bad):
    assert CFG.to_ticks(bad) is None


def test_hyperliquid_units():
    btc, eth = hyperliquid_perp_config("BTC", 5), hyperliquid_perp_config("ETH", 4)
    assert btc.to_ticks("64502") == 645020 and btc.to_lots("0.115") == 11500
    assert eth.to_ticks("1813.4") == 181340 and eth.to_lots("9.1907") == 91907
    assert btc.to_lots("0.000001") is None


def test_notional_is_exact():
    assert CFG.notional_atoms(10125, 2345) == 10125 * 2345        # 1 tick * 1 lot = 1 atom here


# ---- section 3: price-time priority ---------------------------------------------------------------
def test_incoming_buy_example():
    e = eng()
    a = put(e, "s1", SELL, 100, 5, Tif.ALO).order_id
    b = put(e, "s2", SELL, 100, 5, Tif.ALO).order_id
    c = put(e, "s3", SELL, 101, 5, Tif.ALO).order_id
    r = put(e, "buyer", BUY, 101, 12)
    assert fills(r) == [(5, 100, a), (5, 100, b), (2, 101, c)]
    assert e.book.orders[c].remaining_lots == 3


def test_incoming_sell_example_fills_at_maker_price():
    e = eng()
    a = put(e, "b1", BUY, 102, 4, Tif.ALO).order_id
    b = put(e, "b2", BUY, 101, 3, Tif.ALO).order_id
    r = put(e, "seller", SELL, 101, 6)
    assert fills(r) == [(4, 102, a), (2, 101, b)]


def test_partial_fill_keeps_queue_position():
    e = eng()
    a = put(e, "s1", SELL, 100, 5, Tif.ALO).order_id
    b = put(e, "s2", SELL, 100, 5, Tif.ALO).order_id
    put(e, "x", BUY, 100, 2, Tif.IOC)
    r = put(e, "y", BUY, 100, 4, Tif.IOC)
    assert fills(r) == [(3, 100, a), (1, 100, b)]


def test_end_to_end_example_section_8():
    e = eng()
    ids = [put(e, f"s{i}", SELL, p, s, Tif.ALO).order_id for i, (p, s) in enumerate([(100, 4), (101, 3), (101, 5)])]
    r = put(e, "buyer", BUY, 101, 10)
    assert fills(r) == [(4, 100, ids[0]), (3, 101, ids[1]), (3, 101, ids[2])]
    assert [f.fill_sequence for f in r.fills] == [1, 2, 3]
    assert [f.index_in_block for f in r.fills] == [0, 1, 2]


# ---- section 4: time in force --------------------------------------------------------------------
def test_gtc_rests_remainder():
    e = eng()
    put(e, "s", SELL, 100, 2, Tif.ALO)
    r = put(e, "b", BUY, 100, 5)
    assert r.rested_lots == 3 and e.book.best_bid() == 100 and e.book.best_ask() is None


def test_ioc_cancels_remainder_example():
    e = eng()
    put(e, "s1", SELL, 100, 5, Tif.ALO)
    put(e, "s2", SELL, 101, 2, Tif.ALO)
    r = put(e, "b", BUY, 101, 10, Tif.IOC)
    assert [f[:2] for f in fills(r)] == [(5, 100), (2, 101)]
    assert r.cancelled_lots == 3 and r.rested_lots == 0 and len(e.book) == 0


def test_ioc_with_no_liquidity_still_gets_id_and_sequence():
    e = eng()
    r = put(e, "b", BUY, 100, 1, Tif.IOC)
    assert r.accepted and r.order_id == 1 and e.last_arrival_sequence == 1 and r.cancelled_lots == 1


@pytest.mark.parametrize("px,ok", [(100, False), (101, False), (99, True)])
def test_alo_rejected_when_touching_or_crossing(px, ok):
    e = eng()
    put(e, "s", SELL, 100, 1, Tif.ALO)
    seq = e.last_arrival_sequence
    r = put(e, "b", BUY, px, 1, Tif.ALO)
    assert r.accepted is ok
    if not ok:
        assert r.reject_reason == RejectReason.POST_ONLY_WOULD_TAKE
        assert r.order_id is None and e.last_arrival_sequence == seq      # no id, no sequence


def test_admission_rejects_consume_nothing():
    e = eng()
    assert put(e, "b", BUY, 0, 1).reject_reason == RejectReason.PRICE_OUT_OF_RANGE
    assert put(e, "b", BUY, 1, 0).reject_reason == RejectReason.SIZE_ZERO
    assert e.last_order_id == 0 and e.last_arrival_sequence == 0


# ---- section 5: self-trade prevention ------------------------------------------------------------
def test_self_trade_example():
    e = eng()
    a = put(e, "alice", SELL, 100, 3, Tif.ALO).order_id
    b = put(e, "bob", SELL, 101, 2, Tif.ALO).order_id
    c = put(e, "carol", SELL, 101, 4, Tif.ALO).order_id
    r = put(e, "bob", BUY, 102, 10)
    assert fills(r) == [(3, 100, a)]
    assert r.self_trade and r.cancelled_lots == 7 and r.rested_lots == 0
    assert b in e.book.orders and c in e.book.orders          # no skipping past Bob's own ask
    assert e.last_arrival_sequence == 4 and e.last_fill_sequence == 1


def test_self_owned_maker_outside_limit_is_not_reached():
    e = eng()
    put(e, "alice", SELL, 100, 1, Tif.ALO)
    put(e, "bob", SELL, 105, 1, Tif.ALO)
    r = put(e, "bob", BUY, 101, 3)
    assert not r.self_trade and r.rested_lots == 2


# ---- cancels, crossed book, hashing ----------------------------------------------------------------
def test_cancel_removes_and_consumes_no_sequence():
    e = eng()
    a = put(e, "s", SELL, 100, 1, Tif.ALO).order_id
    assert not e.cancel(Cancel("someone-else", a))
    assert e.cancel(Cancel("s", a)) and len(e.book) == 0 and e.last_arrival_sequence == 1
    assert not e.cancel(Cancel("s", a))


def test_crossed_book_is_an_engine_error():
    e = eng()
    put(e, "s", SELL, 100, 1, Tif.ALO)
    put(e, "b", BUY, 99, 1, Tif.ALO)
    e.book.orders[2].price_tick = 100                      # corrupt state on purpose
    e.book.bids[-100] = e.book.bids.pop(-99)
    with pytest.raises(EngineError):
        e.check_invariants()


def test_hash_ignores_external_ids_but_not_counters():
    e1, e2 = eng(), eng()
    e1.submit(Place("s", SELL, 100, 1, Tif.ALO, cloid="0xaa"))
    e2.submit(Place("s", SELL, 100, 1, Tif.ALO, cloid="0xbb", exchange_oid=7))
    assert market_hash(e1) == market_hash(e2)
    put(e1, "b", BUY, 1, 1, Tif.IOC)                       # no fill, but consumes id + sequence
    assert market_hash(e1) != market_hash(e2)


# ---- ordering and parsing --------------------------------------------------------------------------
def _p(ti, tif):
    return RawPlace(ti, 0, "u", 0, BUY, "1", "1", tif, False, None, "h")


def test_project_priority_phases():
    acts = [_p(0, Tif.GTC), RawCancel(1, 0, "u", 0, 5, None), _p(2, Tif.ALO),
            RawModify(3, 0, "u", 0, None, "0xc", _p(3, Tif.ALO))]
    order = [(type(a).__name__, a.tx_index) for a in project_priority(acts)]
    assert order == [("RawPlace", 2), ("RawCancel", 1), ("RawModify", 3), ("RawPlace", 0)]
    assert [a.tx_index for a in first_come(acts)] == [0, 1, 2, 3]


BLOCK = {"height": 10, "blockTime": 1000, "numTxs": 6, "txs": [
    {"user": "0xm", "hash": "0x1", "error": None, "action": {"type": "order", "grouping": "na", "orders": [
        {"a": 0, "b": False, "p": "64500", "s": "0.5", "r": False, "t": {"limit": {"tif": "Alo"}}, "c": "0xc1"},
        {"a": 7, "b": False, "p": "1", "s": "1", "r": False, "t": {"limit": {"tif": "Alo"}}}]}},
    {"user": "0xt", "hash": "0x2", "error": None, "action": {"type": "order", "grouping": "na", "orders": [
        {"a": 0, "b": True, "p": "64600", "s": "0.2", "r": False, "t": {"limit": {"tif": "Ioc"}}}]}},
    {"user": "0xt", "hash": "0x3", "error": "Action already expired", "action": {"type": "order", "grouping": "na",
        "orders": [{"a": 0, "b": True, "p": "64600", "s": "9", "r": False, "t": {"limit": {"tif": "Ioc"}}}]}},
    {"user": "0xm", "hash": "0x4", "error": None, "action": {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": "0xc1"}]}},
    {"user": "0xz", "hash": "0x5", "error": None, "action": {"type": "cancel", "cancels": [{"a": 0, "o": 123}]}},
    {"user": "0xz", "hash": "0x6", "error": None, "action": {"type": "order", "grouping": "na", "orders": [
        {"a": 1, "b": True, "p": "1800", "s": "1", "r": True, "t": {"trigger": {"isMarket": True, "triggerPx": "1700", "tpsl": "sl"}}}]}},
]}


def test_parse_real_shapes():
    pb = parse_block(BLOCK)
    kinds = [type(a).__name__ for a in pb.actions]
    assert kinds == ["RawPlace", "RawPlace", "RawCancel", "RawCancel"]
    assert pb.skipped == {"errored_tx": 1, "trigger_order": 1}


def test_replay_one_block():
    rp = Replay()
    r = rp.apply_block(BLOCK)
    # project order: ALO rests (0.5 BTC), cancels next (cloid cancel removes it; oid 123 unresolved),
    # then the IOC finds an empty book.
    assert r.stats["accepted:ALO"] == 1 and r.stats["cancel_resolved_by_cloid"] == 1
    assert r.stats["cancel_unresolved_oid"] == 1 and r.stats["ioc_remainder_cancelled"] == 1
    assert len(r.fills) == 0
    r2 = Replay(policy="first_come").apply_block(BLOCK)    # first come: the IOC takes 0.2 first
    assert len(r2.fills) == 1 and r2.fills[0].size_lots == 20000 and r2.fills[0].price_tick == 645000
    assert r2.taker_tx[("BTC", r2.fills[0].taker_order_id)] == "0x2"


def test_failed_block_is_not_committed():
    rp = Replay()
    rp.apply_block(BLOCK)
    before = rp.engines
    eng = rp.engines["BTC"]
    eng.last_arrival_sequence = 2**64 - 1                  # next admission must fail
    with pytest.raises(EngineError):
        rp.apply_block(BLOCK)
    assert rp.engines is before                            # nothing from the failed block was kept
