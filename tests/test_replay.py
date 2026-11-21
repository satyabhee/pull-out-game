"""engine_py/replay.py: placeholders, policy, outside fills at end of block, scheduleCancel,
rule 8 reconcile timing, the int-oid resolver, and the run() contract."""

import json

import pytest

from engine_py import replay
from engine_py.matching import PLACEHOLDER
from engine_py.replay import Observer, PolicyError, Replay, listing
from engine_py.types import ASSETS, Modify, OutsideFill, Place, RejectReason, Side
from tests.window_fixture import A, B, M, T0, ZERO, Window, cancel_tx, order, place_tx, schedule_tx, tx
N_ = "0x" + "e" * 40

BTC = ASSETS[0]


def run_on(dirs, policy, **kw):
    window, hl = dirs
    return replay.run(window, policy, hl_data=hl, **kw)


def lots(s):
    return BTC.sz_to_lots(s)


def ticks(p):
    return BTC.px_to_ticks(p)


def test_placeholders_sit_ahead_of_window_orders(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60010", "0.001")])   # before block 0: never reconciled
    w.block([place_tx(A, "0xa1", 0, order(px="60000", sz="0.001", tif="Gtc"))])
    w.block([place_tx(B, "0xb1", 0, order(buy=False, px="60000", sz="0.001", tif="Ioc"))])
    out = list(run_on(w.write(), listing))
    (h1, fills, rejects, state_hash), = out[1:]
    assert [f.maker_user for f in fills] == [PLACEHOLDER]     # the seeded level fills first
    assert state_hash is None and rejects == []


def test_run_yields_one_tuple_per_block_and_calls_the_hasher(tmp_path):
    w = Window(tmp_path)
    for _ in range(3):
        w.block([])
    seen = []

    def hasher(eng, prev, height):
        seen.append((prev, height))
        return height.to_bytes(8, "big")

    out = list(run_on(w.write(), listing, hasher=hasher))
    assert [o[0] for o in out] == [1000, 1001, 1002]
    assert [o[3] for o in out] == [h.to_bytes(8, "big") for h in (1000, 1001, 1002)]
    assert seen[0] == (None, 1000) and seen[1] == ((1000).to_bytes(8, "big"), 1001)


def test_policy_is_applied_but_outside_fills_stay_at_end(tmp_path):
    w = Window(tmp_path)
    h = w.block([place_tx(M, "0xm1", 0, order(px="60000", sz="0.001", tif="Gtc")),
                 place_tx(A, "0xa1", 0, order(buy=False, px="60000", sz="0.001", tif="Ioc"))])
    w.trade("BTC", "A", "60000", "0.001", w.time_of(h), ZERO, 7, M, B)   # outside fill against M
    seen = []

    def reverse(actions):
        seen.append([type(a).__name__ for a in actions])
        return list(reversed(actions))

    r = Replay(*w.write(), reverse)
    (_, fills, rejects, _), = list(r)
    assert seen == [["Place", "Place"]]                       # the policy never sees the outside fill
    # reversed: A's IOC runs first and finds nothing; then M rests; then the outside fill takes M
    assert [(f.maker_user, f.taker_hash) for f in fills] == [(M, ZERO)]
    assert r.outside_fill_log == [(h, 7, 0)]


def test_outside_fill_before_the_maker_would_find_no_maker(tmp_path):
    """Contrast with the end-of-block rule: the same trade one block EARLIER than M's order misses."""
    w = Window(tmp_path)
    h0 = w.block([])
    w.block([place_tx(M, "0xm1", 0, order(px="60000", sz="0.001", tif="Gtc"))])
    w.trade("BTC", "A", "60000", "0.001", w.time_of(h0), ZERO, 7, M, B)
    out = list(run_on(w.write(), listing))
    assert [r.reason for r in out[0][2]] == [RejectReason.NO_MAKER]


def test_schedule_cancel_runs_after_outside_fills(tmp_path):
    w = Window(tmp_path)
    w.block([place_tx(M, "0xm1", 0, order(px="60000", sz="0.002", tif="Gtc"))])
    h = w.block([schedule_tx(M, "0xm2", 0, T0)])              # due at this block's end
    w.trade("BTC", "A", "60000", "0.001", w.time_of(h), ZERO, 7, M, B)
    r = Replay(*w.write(), listing)
    out = list(r)
    assert sum(f.sz for f in out[1][1]) == lots("0.001")     # filled before the expiry
    assert r.engine.books[0].best_bid() is None                # then the rest was cancelled


def test_policy_must_return_a_permutation(tmp_path):
    w = Window(tmp_path)
    w.block([place_tx(A, "0xa1", 0, order())])
    dirs = w.write()
    with pytest.raises(PolicyError):
        list(run_on(dirs, lambda acts: acts[:-1]))
    out = list(run_on(dirs, lambda acts: acts[:-1], check_permutation=False))   # measurement runs
    assert out[0][2] == []


def test_reconcile_applies_at_the_last_block_not_after_the_snapshot(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0, [("60000", "0.003")], [])
    w.block([], dt_ms=0)
    w.block([], dt_ms=1000)
    w.book("BTC", T0 + 500, [("60000", "0.001")], [])          # between block 0 and block 1
    calls = []

    class Spy(Observer):
        def after_reconcile(self, rp, block, snapshot, result, minute):
            calls.append((block.height, snapshot.time_ms, dict(result)))

    r = Replay(*w.write(), listing, observer=Spy())
    list(r)
    # the seed snapshot (T0) is reconciled at block 0 (nothing to trim), the later one also at block 0
    assert [(h, t) for h, t, _ in calls if t == T0 + 500] == [(1000, T0 + 500)]
    assert calls[-1][2]["trimmed"] == lots("0.002")
    assert r.engine.books[0].side(Side.BUY)[ticks("60000")].total == lots("0.001")
    assert r.reconciled[("BTC", "trimmed")] == lots("0.002")


def test_int_oid_cancel_goes_through_the_resolver(tmp_path):
    w = Window(tmp_path)
    h = w.block([place_tx(A, "0xa1", 0, order(px="60000", tif="Gtc"))])
    w.block([cancel_tx(A, "0xa2", 0, 0, 555000)])
    w.anchors = [{"oid": 555000, "height": h, "tx": 0, "item": 0}]
    r = Replay(*w.write(), listing)
    out = list(r)
    assert out[1][2] == [] and r.resolver.stats["resolved"] == 1
    assert r.engine.books[0].best_bid() is None
    out = list(Replay(*(w.dir, w.hl), listing, use_resolver=False))
    assert [x.reason for x in out[1][2]] == [RejectReason.UNKNOWN_ORDER]


def test_placed_maps_engine_oids_to_actions_and_warmup_comes_from_window_json(tmp_path):
    w = Window(tmp_path, warmup_blocks=1)
    w.block([place_tx(A, "0xa1", 0, order(px="60000"))])
    r = Replay(*w.write(), listing)
    list(r)
    (oid, act), = r.placed.items()
    assert act.hash == "0xa1" and r.engine.books[0].node(oid).order.user == A
    assert r.warmup_end_height == 1001 and r.seed_offsets == {"BTC": 0, "ETH": 0}
    assert r.placed_by[oid] is act and not any(isinstance(a, OutsideFill) for a in r.placed.values())


def test_placed_holds_a_modifys_new_order(tmp_path):
    w = Window(tmp_path)
    w.block([place_tx(A, "0xa1", 0, order(px="60000", cloid="0x01"))])
    w.block([tx(A, {"type": "modify", "oid": "0x01", "order": order(px="59990", cloid="0x02")}, "0xa2", 0)])
    r = Replay(*w.write(), listing)
    list(r)
    (oid, p), = [(o, p) for o, p in r.placed.items() if o in r.engine.books[0]]
    assert isinstance(p, Place) and p.px == ticks("59990") and isinstance(r.placed_by[oid], Modify)


# ------------------------------------------------------------------ G1 fixes: touch gate (#1), eviction (#2)

from engine_py.replay import RealTouch, iter_bbo   # noqa: E402
from data.parse import BookSnapshot, Trade          # noqa: E402


def snap(t, bid, ask, asset=0):
    return BookSnapshot(asset=asset, time_ms=t, bids=((bid, 1),), asks=((ask, 1),))


def trade(t, side, px, tid=1, asset=0):
    return Trade(asset=asset, taker_side=side, px=px, sz=1, time_ms=t, hash=ZERO, tid=tid, buyer=A, seller=B)


def test_real_touch_follows_snapshots_and_trades_and_scores_itself():
    rt = RealTouch([snap(100, 10, 12), snap(400, 14, 16)], [trade(200, Side.BUY, 13), trade(300, Side.SELL, 15, 2)])
    assert rt.get(0, 50) == (None, None, None)
    rt.advance(150)
    assert rt.get(0, 150) == (10, 12, 50)
    rt.advance(250)                       # buy at 13: ask was 13
    assert rt.get(0, 250)[:2] == (10, 13)
    rt.advance(350)                       # sell at 15: bid 15, ask pushed above it
    assert rt.get(0, 350)[:2] == (15, 16)
    rt.advance(400)                       # events AT the block time are not used yet
    assert rt.get(0, 400)[:2] == (15, 16)
    rt.advance(401)
    assert rt.get(0, 401)[:2] == (14, 16) and rt.errors[0] == [(1, 0)]


T_0430 = 1794976200000                               # 2026-11-18 04:30:00 UTC


def test_iter_bbo_reads_the_recorded_feed(tmp_path):
    d = tmp_path / "2026-11-18" / "04"
    d.mkdir(parents=True)
    rec = {"channel": "bbo", "data": {"coin": "BTC", "time": T_0430 + 5,
                                      "bbo": [{"px": "64407.0", "sz": "1", "n": 2}, None]}}
    (d / "bbo_BTC.jsonl").write_text(json.dumps({"recv_ms": T_0430 + 300, "msg": rec}) + "\n")
    assert list(iter_bbo(tmp_path, "BTC", T_0430, T_0430 + 10)) == [(T_0430 + 5, 0, (ticks("64407"), lots("1"), 2), None)]
    assert list(iter_bbo(tmp_path, "BTC", T_0430 + 6, T_0430 + 10)) == []


def test_iter_bbo_opens_only_the_hours_around_the_range(tmp_path):
    far = tmp_path / "2026-11-18" / "09"
    far.mkdir(parents=True)
    (far / "bbo_BTC.jsonl.gz").write_bytes(b"not gzip")       # raises if opened
    assert list(iter_bbo(tmp_path, "BTC", T_0430, T_0430 + 10)) == []


def gate_window(tmp_path, snap_bid, snap_ask, txs, dt_ms=100):
    w = Window(tmp_path)
    w.book("BTC", T0 - 10, [(snap_bid, "0.001")], [(snap_ask, "0.001")])   # the seed (nearest T0)
    w.block([], dt_ms=0)
    w.block(txs, dt_ms=dt_ms)
    return w


def test_gate_rejects_an_alo_crossing_the_real_touch_even_if_our_book_would_admit(tmp_path):
    w = gate_window(tmp_path, "60000", "60010", [place_tx(A, "0xa1", 0, order(px="60006"))])
    w.book("BTC", T0 + 60, [("60000", "0.001")], [("60005", "0.001")])   # real ask moved to 60005
    r = Replay(*w.write(), listing, alo_gate="touch")
    out = list(r)
    assert [x.reason for x in out[1][2]] == [RejectReason.ALO_WOULD_CROSS]
    assert r.engine.books[0].best_ask() == ticks("60010")              # our book alone would admit 60006
    assert r.gate_stats["reject"] == 1 and r.gate_stats["reject_engine_would_admit"] == 1


def test_gate_admits_and_evicts_what_cannot_have_been_there(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("59990", "0.001")], [("60020", "0.001")])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60000", tif="Gtc"))], dt_ms=0)  # phantom ask
    w.block([place_tx(A, "0xa1", 0, order(px="60000"))], dt_ms=100)    # real touch: ask 60020
    r = Replay(*w.write(), listing, alo_gate="touch")
    out = list(r)
    assert out[1][2] == []                                              # admitted
    assert r.engine.books[0].best_bid() == ticks("60000") and r.engine.books[0].best_ask() == ticks("60020")
    assert r.gate_stats["admit_engine_would_reject"] == 1 and r.gate_stats["evicted_orders"] == 1
    assert M not in r.engine.by_user
    out = list(Replay(w.dir, w.hl, listing))                            # without the gate: rejected
    assert [x.reason for x in out[1][2]] == [RejectReason.ALO_WOULD_CROSS]


def test_gate_abstains_when_the_touch_is_stale(tmp_path):
    w = gate_window(tmp_path, "60000", "60010", [place_tx(A, "0xa1", 0, order(px="60010"))], dt_ms=5000)
    r = Replay(*w.write(), listing, alo_gate="touch", gate_max_age_ms=1000)
    out = list(r)
    assert r.gate_stats["abstain"] == 1
    assert [x.reason for x in out[1][2]] == [RejectReason.ALO_WOULD_CROSS]   # engine rule: crosses our 60010


def test_gate_reject_of_a_modify_still_cancels_its_target(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("59990", "0.001")], [("60010", "0.001")])
    w.block([place_tx(A, "0xa1", 0, order(px="59990", cloid="0x01"))], dt_ms=0)
    w.block([tx(A, {"type": "modify", "oid": "0x01", "order": order(px="60010", cloid="0x02")}, "0xa2", 0)])
    r = Replay(*w.write(), listing, alo_gate="touch")
    out = list(r)
    assert [x.reason for x in out[1][2]] == [RejectReason.ALO_WOULD_CROSS]
    assert A not in r.engine.by_user                                    # the target is gone


def test_replay_passes_known_eviction_to_rule_8(tmp_path):
    w = Window(tmp_path)
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60010", sz="0.003", tif="Gtc"))], dt_ms=0)
    w.block([], dt_ms=1000)
    w.book("BTC", T0 + 500, [], [("60010", "0.001")])
    r = Replay(*w.write(), listing, reconcile_known="oldest")
    list(r)
    assert r.reconciled[("BTC", "drift")] == lots("0.002") and r.reconciled[("BTC", "evicted")] == lots("0.002")
    assert r.engine.books[0].side(Side.SELL)[ticks("60010")].total == lots("0.001")


def test_unknown_gate_name_fails_loudly(tmp_path):
    w = Window(tmp_path)
    w.block([])
    with pytest.raises(ValueError):
        Replay(*w.write(), listing, alo_gate="magic")


def test_reseed_rebuilds_the_book_from_each_snapshot(tmp_path):
    w = Window(tmp_path)
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60010", sz="0.003", tif="Gtc"))], dt_ms=0)
    w.block([], dt_ms=1000)
    w.book("BTC", T0 + 500, [("59990", "0.002")], [("60010", "0.001")])
    seen = []

    class Spy(Observer):
        def after_reconcile(self, rp, block, snapshot, result, minute):
            if snapshot.time_ms == T0 + 500:
                seen.append(dict(result))

    r = Replay(*w.write(), listing, reseed=True, observer=Spy())
    list(r)
    (res,) = seen
    assert res["drift"] == lots("0.002") and res["cleared_known"] == 1 and res["reseed"] == 1
    book = r.engine.books[0]
    assert [(nd.order.user, nd.remaining) for nd in book.side(Side.SELL)[ticks("60010")]] == [(PLACEHOLDER, lots("0.001"))]
    assert book.best_bid() == ticks("59990") and M not in r.engine.by_user
    assert r.warmup_end_height == 1000


# ------------------------------------------------------------------ per-block touch reconcile (bbo)

def touch_window(tmp_path, bbo_bid, bbo_ask, txs0, bbo_time=T0 + 50):
    w = Window(tmp_path)
    w.book("BTC", T0 - 10, [("60000", "0.003")], [("60010", "0.001")])
    w.block(txs0, dt_ms=0)
    if bbo_bid is not None or bbo_ask is not None:
        w.bbo("BTC", bbo_time, bbo_bid, bbo_ask)
    w.block([], dt_ms=100)
    return w


def test_touch_reconcile_trims_a_stale_placeholder_at_the_touch(tmp_path):
    w = touch_window(tmp_path, ("60000", "0.001", 1), ("60010", "0.001", 1), [])
    r = Replay(*w.write(), listing, touch_reconcile=True)
    list(r)
    assert r.engine.books[0].side(Side.BUY)[ticks("60000")].total == lots("0.001")
    assert r.touch_reconciled[("BTC", "trimmed")] == lots("0.002")


def test_touch_reconcile_evicts_a_phantom_better_than_the_real_touch(tmp_path):
    w = touch_window(tmp_path, ("60000", "0.003", 1), ("60010", "0.001", 1),
                     [place_tx(M, "0xm1", 0, order(px="60005", tif="Alo"))])      # rests at 60005: not real
    r = Replay(*w.write(), listing, touch_reconcile=True)
    list(r)
    assert r.engine.books[0].best_bid() == ticks("60000") and M not in r.engine.by_user
    assert r.touch_reconciled[("BTC", "evicted_orders")] == 1


def test_touch_reconcile_changes_nothing_when_the_book_agrees(tmp_path):
    w = touch_window(tmp_path, ("60000", "0.003", 1), ("60010", "0.001", 1), [])
    r = Replay(*w.write(), listing, touch_reconcile=True)
    list(r)
    got = {k: v for k, v in r.touch_reconciled.items() if k[0] == "BTC" and v}
    assert got == {("BTC", "abstain_no_bbo"): 1} and r.engine.books[0].side(Side.BUY)[ticks("60000")].total == lots("0.003")


def test_touch_reconcile_abstains_without_bbo(tmp_path):
    w = touch_window(tmp_path, None, None, [])
    r = Replay(*w.write(), listing, touch_reconcile=True)
    list(r)
    assert r.touch_reconciled[("BTC", "abstain_no_bbo")] == 2
    assert r.engine.books[0].side(Side.BUY)[ticks("60000")].total == lots("0.003")


def test_touch_n_cap_evicts_extra_known_orders_oldest_first(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 10, [], [("60020", "0.001")])
    w.block([place_tx(M, "0xm1", 0, order(px="60000", sz="0.001")),
             place_tx(N_, "0xn1", 0, order(px="60000", sz="0.001"))], dt_ms=0)
    w.bbo("BTC", T0 + 50, ("60000", "0.002", 1), ("60020", "0.001", 1))   # same size, but one order
    w.block([], dt_ms=100)
    r = Replay(*w.write(), listing, touch_reconcile=True, touch_n_cap=True)
    list(r)
    assert [nd.order.user for nd in r.engine.books[0].side(Side.BUY)[ticks("60000")]] == [N_]
    assert r.touch_reconciled[("BTC", "n_capped")] == 1


def test_touch_refill_adds_a_missing_touch_level(tmp_path):
    # bbo after block 0: a better ask at 60008 that we never saw placed (rested before the window)
    w = touch_window(tmp_path, ("60000", "0.003", 1), ("60008", "0.004", 2), [])
    r = Replay(*w.write(), listing, touch_refill="front")
    list(r)
    level = r.engine.books[0].side(Side.SELL)[ticks("60008")]
    assert [(nd.order.user, nd.remaining) for nd in level] == [(PLACEHOLDER, lots("0.004"))]
    assert r.touch_reconciled[("BTC", "refilled")] == lots("0.004")
    assert r.engine.books[0].side(Side.SELL)[ticks("60010")].total == lots("0.001")   # deeper level kept


def test_touch_refill_tops_up_a_level_behind_known_orders(tmp_path):
    w = touch_window(tmp_path, ("60000", "0.003", 1), ("60010", "0.003", 2),
                     [place_tx(M, "0xm1", 0, order(buy=False, px="60010", sz="0.001", tif="Alo"))])
    r = Replay(*w.write(), listing, touch_refill="back")
    list(r)
    level = r.engine.books[0].side(Side.SELL)[ticks("60010")]
    assert level.total == lots("0.003") and [nd.order.user for nd in level][-1] == PLACEHOLDER
    assert [nd.order.user for nd in level][:2] == [PLACEHOLDER, M]


def test_touch_refill_bad_mode_fails_loudly(tmp_path):
    w = Window(tmp_path)
    w.block([])
    with pytest.raises(ValueError):
        Replay(*w.write(), listing, touch_refill="middle")


def test_gate_uses_the_bbo_touch(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 10, [("60000", "0.001")], [("60020", "0.001")])
    w.block([], dt_ms=0)
    w.bbo("BTC", T0 + 50, ("60000", "0.001", 1), ("60005", "0.001", 1))
    w.block([place_tx(A, "0xa1", 0, order(px="60010"))], dt_ms=100)       # crosses the real ask 60005
    r = Replay(*w.write(), listing, alo_gate="touch")
    out = list(r)
    assert [x.reason for x in out[1][2]] == [RejectReason.ALO_WOULD_CROSS]
    assert r.gate_stats["reject_engine_would_admit"] == 1


def test_touch_evict_bad_mode_fails_loudly(tmp_path):
    w = Window(tmp_path)
    w.block([])
    with pytest.raises(ValueError):
        Replay(*w.write(), listing, touch_reconcile=True, touch_evict="random")


def test_touch_evict_newest_keeps_the_older_known_order(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 10, [], [("60020", "0.001")])
    w.block([place_tx(M, "0xm1", 0, order(px="60000", sz="0.001")),
             place_tx(N_, "0xn1", 0, order(px="60000", sz="0.001"))], dt_ms=0)
    w.bbo("BTC", T0 + 50, ("60000", "0.001", 1), ("60020", "0.001", 1))
    w.block([], dt_ms=100)
    r = Replay(*w.write(), listing, touch_reconcile=True, touch_evict="newest")
    list(r)
    assert [nd.order.user for nd in r.engine.books[0].side(Side.BUY)[ticks("60000")]] == [M]
