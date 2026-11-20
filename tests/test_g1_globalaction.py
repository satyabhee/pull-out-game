"""H9b deep dive: other_position measurement flag (data/parse + engine_py/replay) and the helpers in
analysis/g1_globalaction.py."""

import pytest

from analysis import g1_globalaction as ga
from data.parse import Stats, iter_window
from engine_py.replay import Replay, listing
from engine_py.types import ASSETS, OutsideFill, Place, Side, Tif
from tests.window_fixture import A, B, M, T0, ZERO, Window, order, place_tx, tx

BTC = ASSETS[0]
G = "0x" + "9" * 40                    # the validator sending SetGlobalAction
T = "0x" + "7" * 40                    # a triggered taker


def sga(h):
    return tx(G, {"type": "SetGlobalAction", "pxs": [["60000", "60001"], ["2000", "2000"]]}, h, 0)


def three_tx_window(tmp_path):
    """tx0: M rests an ask 0.001 @ 60000; tx1: SetGlobalAction whose trigger takes all of it;
    tx2: A's IOC buys 0.001 @ 60000. Chain: the trigger got M's order."""
    w = Window(tmp_path)
    h = w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60000", sz="0.001", tif="Gtc")),
                 sga("0xg1"),
                 place_tx(A, "0xa1", 0, order(px="60000", sz="0.001", tif="Ioc"))])
    w.trade("BTC", "B", "60000", "0.001", w.time_of(h), "0xg1", 7, T, M)
    return w.write()


def takers_of(window, hl, **kw):
    (_, fills, rejects, _), = list(Replay(window, hl, listing, outside_other_txs=True, **kw))
    return [f.taker_user for f in fills], [r.reason.name for r in rejects]


@pytest.mark.parametrize("position,takers,rejects", [
    ("end", [A], ["NO_MAKER"]),            # A's IOC takes M's order; the trigger fill finds nothing
    ("start", [A], ["NO_MAKER"]),          # the trigger fires before M's order exists
    ("index", [T], []),                    # at tx1: the trigger takes M's order, A's IOC finds nothing
])
def test_other_position_places_set_global_action_fills(tmp_path, position, takers, rejects):
    window, hl = three_tx_window(tmp_path)
    assert takers_of(window, hl, other_position=position) == (takers, rejects)


def test_other_at_tx_index_sets_the_source_tx_index_and_keeps_zero_hash_at_the_end(tmp_path):
    w = Window(tmp_path)
    h = w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60000", sz="0.002", tif="Gtc")),
                 sga("0xg1"),
                 place_tx(A, "0xa1", 0, order(px="59000", sz="0.001", tif="Gtc"))])
    w.trade("BTC", "B", "60000", "0.001", w.time_of(h), "0xg1", 9, T, M)
    w.trade("BTC", "B", "60000", "0.0005", w.time_of(h), "0xg1", 3, T, M)
    w.trade("BTC", "B", "60000", "0.0005", w.time_of(h), ZERO, 1, B, M)
    window, hl = w.write()
    st = Stats()
    (blk,) = list(iter_window(window, hl, st, outside_other_txs=True, other_at_tx_index=True))
    of = [a for a in blk.actions if isinstance(a, OutsideFill)]
    assert [(a.hash, a.tx_index, a.item_index, a.tid) for a in of] == [
        (ZERO, 3, 0, 1), ("0xg1", 1, 0, 9), ("0xg1", 1, 1, 3)]        # stream order, not tid order
    assert st["outside_fill_other_tx"] == 1 and st["action:OutsideFill"] == 3
    (blk_default,) = list(iter_window(window, hl, outside_other_txs=True))
    assert [a.tx_index for a in blk_default.actions if isinstance(a, OutsideFill)] == [3, 3, 3]


def test_other_position_needs_the_flag(tmp_path):
    window, hl = three_tx_window(tmp_path)
    with pytest.raises(ValueError):
        Replay(window, hl, listing, other_position="index")
    with pytest.raises(ValueError):
        list(iter_window(window, hl, other_at_tx_index=True))
    with pytest.raises(ValueError):
        Replay(window, hl, listing, outside_other_txs=True, other_position="middle")


def test_index_position_with_a_reordering_policy_goes_before_the_first_later_tx():
    """Unit test of Replay._sequence: fills go before the first ordered action with a larger tx_index."""
    rp = Replay.__new__(Replay)
    rp.other_position, rp.outside_position = "index", "end"
    mk = lambda tx_i: Place(user=A, block=1, tx_index=tx_i, item_index=0, hash=f"0x{tx_i}", priority=None,
                            asset=0, side=Side.BUY, px=1, sz=1, tif=Tif.GTC, cloid=None, reduce_only=False)
    ofill = lambda tx_i, h: OutsideFill(user=T, block=1, tx_index=tx_i, item_index=0, hash=h, priority=None,
                                        asset=0, maker_user=M, taker_side=Side.BUY, px=1, sz=1, tid=tx_i)
    a0, a2, a5 = mk(0), mk(2), mk(5)
    g1, z = ofill(1, "0xg"), ofill(9, ZERO)
    assert rp._sequence([a5, a0, a2], [g1, z]) == [g1, a5, a0, a2, z]   # before the first later-tx action
    assert rp._sequence([a0], [g1, z]) == [a0, g1, z]
    rp.other_position = "start"
    assert rp._sequence([a0, a2], [z, g1]) == [g1, a0, a2, z]
    rp.outside_position = "start"
    assert rp._sequence([a0, a2], [z, g1]) == [g1, z, a0, a2]


def test_default_replay_is_unchanged_by_the_new_parameter(tmp_path):
    window, hl = three_tx_window(tmp_path)
    a = [(h, f, r) for h, f, r, _ in Replay(window, hl, listing, outside_other_txs=True)]
    b = [(h, f, r) for h, f, r, _ in Replay(window, hl, listing, outside_other_txs=True, other_position="end")]
    assert a == b


# ------------------------------------------------------------------ analysis helpers

def test_local_error_sums_blocks_from_the_event_through_n_after():
    rows = [dict(block=100, coin="BTC", real_notional=10, engine_notional=4, matched_notional=4),
            dict(block=101, coin="BTC", real_notional=0, engine_notional=7, matched_notional=0),
            dict(block=103, coin="BTC", real_notional=5, engine_notional=5, matched_notional=5),
            dict(block=101, coin="ETH", real_notional=9, engine_notional=0, matched_notional=0),
            dict(block=99, coin="BTC", real_notional=3, engine_notional=0, matched_notional=0)]
    idx = ga.index_rows(rows)
    assert ga.local_error(idx, [(100, "BTC")], 1) == (6, 7)
    assert ga.local_error(idx, [(100, "BTC")], 0) == (6, 0)
    assert ga.local_error(idx, [(100, "BTC"), (101, "BTC")], 1) == (6, 7)      # blocks counted once
    assert ga.local_error(idx, [(100, None)], 1) == (15, 7)                     # None = both coins


def test_trigger_px_parsing_and_crossing():
    t = {"trigger": {"isMarket": True, "triggerPx": "60010", "tpsl": "sl"}}
    from decimal import Decimal
    assert ga.trigger_px(t) == Decimal("60010")
    assert ga.crossed(BTC.px_to_ticks("60000"), BTC.px_to_ticks("60020"), BTC.px_to_ticks("60010"))
    assert ga.crossed(BTC.px_to_ticks("60020"), BTC.px_to_ticks("60000"), BTC.px_to_ticks("60010"))
    assert not ga.crossed(BTC.px_to_ticks("60000"), BTC.px_to_ticks("60005"), BTC.px_to_ticks("60010"))


def test_characterise_reports_sweep_depth_and_trigger_owner(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("59990", "1")], [("60000", "0.001"), ("60005", "0.001")])
    w.block([sga("0xg0"),
             tx(T, {"type": "order", "grouping": "na",
                    "orders": [{"a": 0, "b": True, "p": "60100", "s": "0.002", "r": True,
                                "t": {"trigger": {"isMarket": True, "triggerPx": "60000.5", "tpsl": "sl"}}}]},
                "0xt1", 0)])
    h = w.block([place_tx(B, "0xb1", 0, order(px="59000", sz="0.001", tif="Gtc")),
                 tx(G, {"type": "SetGlobalAction", "pxs": [["60001", "60002"], ["2000", "2000"]]}, "0xg1", 0)])
    w.trade("BTC", "B", "60000", "0.001", w.time_of(h), "0xg1", 5, T, M)
    w.trade("BTC", "B", "60005", "0.001", w.time_of(h), "0xg1", 2, T, B)
    window, hl = w.write()
    ev, zero = ga.characterise(window, hl)
    (e,) = ev
    assert (e["height"], e["tx_index"], e["num_txs"], e["kind"], e["coin"], e["fills"]) == (1001, 1, 2,
                                                                                           "SetGlobalAction", "BTC", 2)
    assert e["taker_side"] == "BUY" and e["distinct_px"] == 2
    assert e["touch_ask_before"] == "60000" and e["worst_ticks_beyond_touch"] == BTC.px_to_ticks("60005") - BTC.px_to_ticks("60000")
    assert e["takers_with_trigger_in_window"] == 1 and e["takers_trigger_crossed_by_oracle"] == 1
    assert e["oracle_move_bps"] == "0.17"          # 60000 -> 60001
    assert zero.get("zero_hash_trades", 0) == 0 and zero["dropped_trigger_modifies"] == 0
