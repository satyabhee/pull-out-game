"""G1 triage measurement code: replay flags (snapshot_shift, outside_position), the throwaway class/priority
policies and stp pass-through in validate_replay, and analysis/g1_triage.py's helpers and attribution."""

import pytest

from analysis import g1_triage as tr
from analysis import validate_replay as vr
from engine_py.matching import PLACEHOLDER
from engine_py.replay import Observer, Replay, listing
from engine_py.types import ASSETS, Cancel, Place, RejectReason, Side, Tif
from tests.window_fixture import A, B, M, T0, ZERO, Window, cancel_tx, order, place_tx, tx

BTC = ASSETS[0]
N = "0x" + "e" * 40


def ticks(p):
    return BTC.px_to_ticks(p)


def lots(s):
    return BTC.sz_to_lots(s)


def pri_tx(user, h, p, *orders):
    return tx(user, {"type": "order", "grouping": {"p": p}, "orders": list(orders)}, h, 0)


# ------------------------------------------------------------------ replay flags

def snapshot_window(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.003")], [])
    for _ in range(4):
        w.block([])
    w.book("BTC", T0 + 150, [("60000", "0.001")], [])           # between block 1 (T0+100) and block 2
    return w.write()


class Spy(Observer):
    def __init__(self):
        self.calls = []

    def after_reconcile(self, rp, block, s, result, minute):
        self.calls.append((block.height, s.time_ms))


@pytest.mark.parametrize("shift,height,dropped", [(0, 1001, 0), (-1, 1000, 1), (1, 1002, 0)])
def test_snapshot_shift_moves_the_reconcile_block(tmp_path, shift, height, dropped):
    """(-1 also pushes the fixture's ETH snapshot at T0, block 0, out of the window.)"""
    spy = Spy()
    r = Replay(*snapshot_window(tmp_path), listing, observer=spy, snapshot_shift=shift)
    list(r)
    assert [h for h, t in spy.calls if t == T0 + 150] == [height]
    assert r.shift_dropped == dropped


def test_snapshot_shifted_out_of_the_window_is_dropped_and_counted(tmp_path):
    spy = Spy()
    r = Replay(*snapshot_window(tmp_path), listing, observer=spy, snapshot_shift=5)
    list(r)
    assert [t for _, t in spy.calls if t == T0 + 150] == [] and r.shift_dropped == 2   # + the ETH snapshot


def test_outside_position_start_applies_outside_fills_before_the_block(tmp_path):
    w = Window(tmp_path)
    h = w.block([place_tx(M, "0xm1", 0, order(px="60000", sz="0.001", tif="Gtc"))])
    w.trade("BTC", "A", "60000", "0.001", w.time_of(h), ZERO, 7, M, B)
    dirs = w.write()
    (_, f_end, r_end, _), = list(Replay(*dirs, listing))
    (_, f_start, r_start, _), = list(Replay(*dirs, listing, outside_position="start"))
    assert [f.maker_user for f in f_end] == [M] and r_end == []
    assert f_start == [] and [r.reason for r in r_start] == [RejectReason.NO_MAKER]
    with pytest.raises(ValueError):
        Replay(*dirs, listing, outside_position="middle")


# ------------------------------------------------------------------ H1 policies

def act(cls, tx_index, item=0, tif=Tif.IOC, p=None):
    meta = dict(user=A, block=1, tx_index=tx_index, item_index=item, hash=f"0x{tx_index}", priority=p, asset=0)
    if cls == "cancel":
        return Cancel(**meta, oid=1)
    return Place(**meta, side=Side.BUY, px=1, sz=1, tif=tif, cloid=None, reduce_only=False)


def test_class_policy_runs_alo_then_cancels_then_takers_in_listing_order():
    a = [act("ioc", 0), act("cancel", 1), act("alo", 2, tif=Tif.ALO), act("cancel", 3), act("gtc", 4, tif=Tif.GTC)]
    out = vr.class_policy(a)
    assert [x.tx_index for x in out] == [2, 1, 3, 0, 4]
    assert sorted(map(id, out)) == sorted(map(id, a)) and [x.tx_index for x in a] == [0, 1, 2, 3, 4]   # pure


def test_identification_controls_anti_reverses_classes_and_shuffle_is_repeatable():
    a = [act("ioc", 0), act("cancel", 1), act("alo", 2, tif=Tif.ALO), act("cancel", 3), act("gtc", 4, tif=Tif.GTC)]
    assert [x.tx_index for x in vr.anti_policy(a)] == [0, 4, 1, 3, 2]
    s1, s2 = vr.shuffle_policy(a), vr.shuffle_policy(a)
    assert [x.tx_index for x in s1] == [x.tx_index for x in s2] and sorted(x.tx_index for x in s1) == [0, 1, 2, 3, 4]


def test_a_tx_with_any_gtc_or_ioc_item_is_a_taker_tx():
    a = [act("alo", 0, item=0, tif=Tif.ALO), act("ioc", 0, item=1), act("cancel", 1)]
    assert [(x.tx_index, x.item_index) for x in vr.class_policy(a)] == [(1, 0), (0, 0), (0, 1)]


def test_priority_policy_sorts_only_takers_by_p_descending():
    a = [act("ioc", 0, p=5), act("alo", 1, tif=Tif.ALO, p=1), act("ioc", 2, p=50), act("ioc", 3),
         act("alo", 4, tif=Tif.ALO, p=9), act("cancel", 5)]
    assert [x.tx_index for x in vr.priority_policy(a)] == [1, 4, 5, 2, 0, 3]


def test_class_policy_lets_a_cancel_beat_a_taker_in_the_same_block(tmp_path):
    w = Window(tmp_path)
    w.anchors = None
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60010", sz="0.001", tif="Alo", cloid="0x1"))])
    w.block([place_tx(A, "0xa1", 0, order(px="60010", sz="0.001", tif="Ioc")),
             tx(M, {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": "0x1"}]}, "0xm2", 0)])
    dirs = w.write()
    fills = lambda pol: [f for _, fs, _, _ in Replay(*dirs, vr.POLICIES[pol]) for f in fs]
    assert len(fills("listing")) == 1 and fills("class") == []


def test_validate_run_passes_stp_and_reports_triage_flags_only_when_set(tmp_path):
    w = Window(tmp_path)
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60010"))])
    window, hl = w.write()
    res, _ = vr.run(window, hl, quiet=True)
    assert "stp" not in res and "snapshot_shift" not in res        # default outputs unchanged
    res, _ = vr.run(window, hl, quiet=True, stp="expire_maker", snapshot_shift=-1)
    assert res["stp"] == "expire_maker" and res["snapshot_shift"] == -1


# ------------------------------------------------------------------ g1_triage helpers

def test_failed_groups_sum_to_real_and_engine_minus_matched():
    real = [(100, 3, M), (100, 2, N), (101, 1, M)]
    eng = [(100, 1, M, 9), (100, 4, B, 8), (102, 2, M, 7)]
    missed, spur = tr.failed_groups(real, eng)
    mn = vr.matched_notional(real, eng, lambda f: (f[0], f[2]))
    assert sum(missed.values()) == vr.notional(real) - mn
    assert sum(spur.values()) == vr.notional([f[:3] for f in eng]) - mn
    assert missed == {(100, M): 200, (100, N): 200, (101, M): 101} and spur == {(100, B): 400, (102, M): 204}


def test_share_of_group_weights_by_lots():
    eng = [(100, 1, M, 1), (100, 3, M, 2), (101, 5, M, 3)]
    assert tr.share_of_group(eng, (100, M), lambda f: f[3] == 2) == 0.75


def test_tail_sort_reorders_only_orders_placed_in_the_last_400_ms():
    nodes = [("ph", None, None), ("old", 0, 1), ("a", 700, 1), ("b", 800, 9), ("c", 900, 9)]
    assert [n[0] for n in tr.tail_sorted(nodes, 1000)] == ["ph", "old", "b", "c", "a"]
    assert [n[0] for n in tr.tail_sorted(nodes, 2000)] == ["ph", "old", "a", "b", "c"]   # nothing recent


def test_taker_vs_touch_and_buckets():
    assert tr.taker_vs_touch(Side.BUY, 1010, 990, 1000) == (True, 100)
    assert tr.taker_vs_touch(Side.SELL, 995, 990, 1000) == (False, -51)   # floor
    assert tr.taker_vs_touch(Side.SELL, 990, None, 1000) == (None, None)
    assert [tr.dist_bucket(x) for x in (None, -1, 0, 3, 20, 101)] == \
        ["no touch", "short of touch", "at touch", "0-5 bps beyond", "0-20 bps beyond", "> 100 bps beyond"]


def test_dropped_modifies_finds_trigger_modifies_only(tmp_path):
    w = Window(tmp_path)
    trig = {"a": 0, "b": True, "p": "60000", "s": "0.001", "r": True,
            "t": {"trigger": {"isMarket": True, "triggerPx": "59000", "tpsl": "sl"}}}
    w.block([tx(M, {"type": "modify", "oid": 5, "order": trig}, "0xm1", 0),
             tx(M, {"type": "batchModify", "modifies": [{"oid": "0xc", "order": order()},
                                                         {"oid": 6, "order": trig}]}, "0xm2", 0),
             tx(M, {"type": "modify", "oid": 7, "order": trig}, "0xm3", 0, error="bad")])
    window, _ = w.write()
    assert [(d["target"], d["user"]) for d in tr.dropped_modifies(window)] == [(5, M), (6, M)]


def test_instrumented_run_attributes_a_queue_miss_that_the_alo_tail_sort_explains(tmp_path):
    """N's ALO and M's higher-priority ALO join the same level in one block; the taker 100 ms later fills
    N in the engine but M on chain: missed (M) is 'size ahead' and the 400 ms tail sort puts M first."""
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60050", "0.001")])
    w.block([pri_tx(N, "0xn1", 0, order(buy=False, px="60020")),
             pri_tx(M, "0xm1", 100, order(buy=False, px="60020"))])
    h = w.block([place_tx(A, "0xa1", 0, order(px="60020", tif="Ioc"))])
    w.trade("BTC", "B", "60020", "0.001", w.time_of(h), "0xa1", 1, A, M)
    window, hl = w.write()
    out = tr.run_instrumented(window, hl, tmp_path / "triage")
    n = ticks("60020") * lots("0.001")
    assert out["base"]["missed"] == n and out["base"]["spurious"] == n
    at = out["attribution"]
    assert at["H6 real maker resting behind other size"] == n
    assert at["H6 ALO tail sort by p (400 ms) puts real maker first"] == n
    assert at.get("H2 maker order < $10 (not reduce-only)", 0) == 0          # $60 order
    assert (tmp_path / "triage" / "first_mismatches.csv").exists()
    rows = tr.build_table(out["base"], {}, out, "cmd")
    assert any(r["hypothesis"].startswith("H6 ALO tail sort by p (400 ms) puts") and r["missed_share"] == "100.0%"
               for r in rows)


def test_small_maker_orders_are_attributed_to_the_min_value_rule(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60050", "0.001")])
    w.block([place_tx(N, "0xn1", 0, order(buy=False, px="60020", sz="0.0001"))])   # $6
    h = w.block([place_tx(A, "0xa1", 0, order(px="60020", sz="0.0001", tif="Ioc"))])
    window, hl = w.write()
    out = tr.run_instrumented(window, hl, tmp_path / "triage")
    n = ticks("60020") * lots("0.0001")
    assert out["base"]["spurious"] == n
    at = out["attribution"]
    assert at["H2 maker order < $10 (not reduce-only)"] == n
    assert at["H2 taker order < $10 (not reduce-only)"] == n
    assert at["H3 engine-only tx (all)"] == n
    assert out["admitted"]["under_10=True reduce_only=False"] == 1


def test_placeholder_fills_the_chain_did_not_make_are_attributed_to_h11(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60050", "0.002")])
    w.block([])
    w.block([place_tx(A, "0xa1", 0, order(px="60050", sz="0.002", tif="Ioc"))])   # chain: nothing
    window, hl = w.write()
    out = tr.run_instrumented(window, hl, tmp_path / "triage")
    n = ticks("60050") * lots("0.002")
    assert out["attribution"]["H11 spurious from placeholder liquidity"] == n
    assert ["H11 placeholder spurious by tx type", "engine-only tx", n] in out["context"]


def test_trades_of_a_non_book_tx_become_outside_fills_only_with_the_flag(tmp_path):
    from data.parse import Stats, iter_window
    w = Window(tmp_path)
    h = w.block([place_tx(M, "0xm1", 0, order(px="60000", sz="0.001", tif="Gtc")),
                 tx(B, {"type": "SetGlobalAction", "pxs": []}, "0xg1", 0)])
    w.trade("BTC", "A", "60000", "0.001", w.time_of(h), "0xg1", 7, M, B)   # a trigger fired by the oracle tx
    window, hl = w.write()
    (blk,) = list(iter_window(window, hl))
    assert [type(a).__name__ for a in blk.actions] == ["Place"]
    st = Stats()
    (blk,) = list(iter_window(window, hl, st, outside_other_txs=True))
    assert [type(a).__name__ for a in blk.actions] == ["Place", "OutsideFill"]
    assert blk.actions[1].maker_user == M and blk.actions[1].hash == "0xg1" and st["outside_fill_other_tx"] == 1
    (_, fills, rejects, _), = list(Replay(window, hl, listing, outside_other_txs=True))
    assert [f.maker_user for f in fills] == [M] and rejects == []
