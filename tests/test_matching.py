"""Matching tests: one test per rule, every fill checked against the maker's price."""

import pytest

from engine_py.matching import PLACEHOLDER, CrossedBookError, Engine
from engine_py.types import (
    Cancel, CancelByCloid, Modify, OutsideFill, Place, RejectReason, ScheduleCancel, Side, Tif,
    ZERO_HASH,
)

B, S = Side.BUY, Side.SELL
_i = iter(range(1, 100_000))


def meta(user, block=1):
    return dict(user=user, block=block, tx_index=next(_i), item_index=0, hash=f"0x{user}", priority=None)


def place(user, side, px, sz, tif=Tif.GTC, cloid=None, block=1):
    return Place(**meta(user, block), asset=0, side=side, px=px, sz=sz, tif=tif,
                 cloid=cloid, reduce_only=False)


@pytest.fixture
def eng():
    return Engine(check_every_action=True)


def resting(eng, side):
    book = eng.books[0]
    return [(lv.px, n.order.user, n.remaining) for lv in book.levels(side) for n in lv]


# ---- GTC / IOC / ALO decision table ------------------------------------------------

def test_gtc_no_cross_rests(eng):
    eng.apply(place("m", S, 105, 10))
    fills, rejects = eng.apply(place("t", B, 100, 5))
    assert (fills, rejects) == ([], [])
    assert resting(eng, B) == [(100, "t", 5)]


def test_gtc_partial_fill_rests_remainder_at_limit(eng):
    eng.apply(place("m", S, 100, 4))
    fills, _ = eng.apply(place("t", B, 101, 10))
    assert [(f.px, f.sz, f.maker_user) for f in fills] == [(100, 4, "m")]
    assert resting(eng, B) == [(101, "t", 6)] and resting(eng, S) == []


def test_ioc_partial_fill_cancels_remainder(eng):
    eng.apply(place("m", S, 100, 5))
    eng.apply(place("m2", S, 101, 2))
    fills, rejects = eng.apply(place("t", B, 101, 10, Tif.IOC))
    assert [(f.px, f.sz) for f in fills] == [(100, 5), (101, 2)]
    assert rejects == [] and resting(eng, B) == []


def test_ioc_zero_fill_is_not_an_error(eng):
    eng.apply(place("m", S, 105, 5))
    assert eng.apply(place("t", B, 100, 5, Tif.IOC)) == ([], [])
    assert resting(eng, B) == [] and resting(eng, S) == [(105, "m", 5)]


@pytest.mark.parametrize("px", [105, 106], ids=["touch", "through"])
def test_alo_that_would_cross_is_rejected_whole(eng, px):
    eng.apply(place("m", S, 105, 5))
    p = place("t", B, px, 5, Tif.ALO)
    fills, rejects = eng.apply(p)
    assert fills == [] and [(r.action, r.reason) for r in rejects] == [(p, RejectReason.ALO_WOULD_CROSS)]
    assert resting(eng, S) == [(105, "m", 5)] and resting(eng, B) == []


def test_alo_rests_when_it_does_not_cross(eng):
    eng.apply(place("m", S, 105, 5))
    assert eng.apply(place("t", B, 104, 5, Tif.ALO)) == ([], [])
    assert resting(eng, B) == [(104, "t", 5)]


# ---- price-time priority ---------------------------------------------------------------

def test_spec_example_incoming_buy(eng):
    eng.apply(place("A", S, 100, 5))
    eng.apply(place("Bb", S, 100, 5))
    eng.apply(place("C", S, 101, 5))
    fills, _ = eng.apply(place("t", B, 101, 12))
    assert [(f.maker_user, f.px, f.sz) for f in fills] == [("A", 100, 5), ("Bb", 100, 5), ("C", 101, 2)]
    assert [f.seq for f in fills] == [1, 2, 3]


def test_spec_example_incoming_sell_fills_at_maker_prices(eng):
    eng.apply(place("A", B, 102, 4))
    eng.apply(place("Bb", B, 101, 3))
    fills, _ = eng.apply(place("t", S, 101, 6))
    assert [(f.maker_user, f.px, f.sz) for f in fills] == [("A", 102, 4), ("Bb", 101, 2)]
    assert resting(eng, B) == [(101, "Bb", 1)]


def test_real_g0_example_fills_at_maker_price(eng):
    # BTC IOC sell limited at 64764.0 filled at 64893.0, the resting buyer's price.
    eng.apply(place("maker", B, 648930, 906))
    fills, _ = eng.apply(place("taker", S, 647640, 906, Tif.IOC))
    assert [(f.px, f.sz, f.maker_user, f.taker_user) for f in fills] == [(648930, 906, "maker", "taker")]


# ---- self-trade --------------------------------------------------------------------------

def test_self_trade_cancel_taker_matches_spec_example(eng):
    eng.apply(place("alice", S, 100, 3))
    eng.apply(place("bob", S, 101, 2))
    eng.apply(place("carol", S, 101, 4))
    p = place("bob", B, 102, 10)
    fills, rejects = eng.apply(p)
    assert [(f.maker_user, f.px, f.sz) for f in fills] == [("alice", 100, 3)]
    assert [r.reason for r in rejects] == [RejectReason.SELF_TRADE]
    assert resting(eng, S) == [(101, "bob", 2), (101, "carol", 4)]   # makers untouched
    assert resting(eng, B) == []                                     # remainder never rests


def test_self_trade_expire_maker_option():
    eng = Engine(stp="expire_maker", check_every_action=True)
    eng.apply(place("bob", S, 101, 2))
    eng.apply(place("carol", S, 101, 4))
    fills, rejects = eng.apply(place("bob", B, 102, 10))
    assert [(f.maker_user, f.sz) for f in fills] == [("carol", 4)] and rejects == []
    assert resting(eng, B) == [(102, "bob", 6)]


# ---- cancels, modify, scheduleCancel ----------------------------------------------------

def test_cancel_by_cloid(eng):
    eng.apply(place("u", B, 100, 5, Tif.ALO, cloid="0xc1"))
    c = CancelByCloid(**meta("u"), asset=0, cloid="0xc1")
    assert eng.apply(c) == ([], [])
    assert resting(eng, B) == []
    assert [r.reason for r in eng.apply(c)[1]] == [RejectReason.UNKNOWN_ORDER]  # already gone


def test_cancel_by_cloid_only_for_its_owner(eng):
    eng.apply(place("u", B, 100, 5, Tif.ALO, cloid="0xc1"))
    _, rejects = eng.apply(CancelByCloid(**meta("other"), asset=0, cloid="0xc1"))
    assert [r.reason for r in rejects] == [RejectReason.UNKNOWN_ORDER]
    assert resting(eng, B) == [(100, "u", 5)]


def test_int_oid_cancel_without_resolver_is_unknown(eng):
    eng.apply(place("u", B, 100, 5))
    _, rejects = eng.apply(Cancel(**meta("u"), asset=0, oid=607819318775))
    assert [r.reason for r in rejects] == [RejectReason.UNKNOWN_ORDER]


def test_int_oid_cancel_with_resolver():
    eng = Engine(resolve_oid=lambda user, asset, hl_oid: 1 if hl_oid == 42 else None,
                 check_every_action=True)
    eng.apply(place("u", B, 100, 5))                  # engine oid 1
    assert eng.apply(Cancel(**meta("u"), asset=0, oid=42)) == ([], [])
    assert resting(eng, B) == []


def test_modify_loses_queue_priority(eng):
    eng.apply(place("u", B, 100, 5, cloid="0xa"))
    eng.apply(place("v", B, 100, 5))
    new = place("u", B, 100, 3, cloid="0xb")
    eng.apply(Modify(**meta("u"), asset=0, target="0xa", new=new))
    assert resting(eng, B) == [(100, "v", 5), (100, "u", 3)]


def test_modify_with_unknown_target_still_places(eng):
    new = place("u", B, 100, 3)
    m = Modify(**meta("u"), asset=0, target=999, new=new)
    _, rejects = eng.apply(m)
    assert [r.reason for r in rejects] == [RejectReason.UNKNOWN_ORDER]
    assert resting(eng, B) == [(100, "u", 3)]


def test_schedule_cancel_fires_at_end_block(eng):
    eng.apply(place("u", B, 100, 5))
    eng.apply(place("u", S, 110, 5))
    eng.apply(place("v", B, 99, 5))
    eng.apply(ScheduleCancel(**meta("u"), at_ms=1000))
    eng.end_block(1, 999)
    assert len(eng.books[0].index) == 3
    eng.end_block(2, 1000)
    assert resting(eng, B) == [(99, "v", 5)] and resting(eng, S) == []


def test_schedule_cancel_without_time_clears(eng):
    eng.apply(place("u", B, 100, 5))
    eng.apply(ScheduleCancel(**meta("u"), at_ms=1000))
    eng.apply(ScheduleCancel(**meta("u"), at_ms=None))
    eng.end_block(2, 5000)
    assert resting(eng, B) == [(100, "u", 5)]


# ---- placeholders and outside fills -------------------------------------------------------

def test_placeholders_sit_ahead_of_window_orders(eng):
    eng.seed(0, S, 100, 4)
    eng.apply(place("m", S, 100, 5))
    fills, _ = eng.apply(place("t", B, 100, 6))
    assert [(f.maker_user, f.sz) for f in fills] == [(PLACEHOLDER, 4), ("m", 2)]


def test_seeding_a_crossed_book_fails_loudly(eng):
    eng.seed(0, B, 100, 1)
    with pytest.raises(CrossedBookError):
        eng.seed(0, S, 100, 1)


def outside(maker, taker_side, px, sz, tid=1):
    return OutsideFill(user="liq", block=1, tx_index=10_000, item_index=0, hash=ZERO_HASH,
                       priority=None, asset=0, maker_user=maker, taker_side=taker_side,
                       px=px, sz=sz, tid=tid)


def test_outside_fill_named_maker_first_then_placeholder(eng):
    eng.seed(0, B, 100, 5)
    eng.apply(place("m", B, 100, 3))
    eng.apply(place("other", B, 100, 3))
    fills, rejects = eng.apply(outside("m", S, 100, 6))
    assert [(f.maker_user, f.sz, f.taker_order) for f in fills] == [("m", 3, None), (PLACEHOLDER, 3, None)]
    assert rejects == []
    assert resting(eng, B) == [(100, PLACEHOLDER, 2), (100, "other", 3)]


def test_outside_fill_without_maker_is_rejected(eng):
    eng.apply(place("other", B, 100, 3))
    fills, rejects = eng.apply(outside("m", S, 100, 2))
    assert fills == [] and [r.reason for r in rejects] == [RejectReason.NO_MAKER]
    assert resting(eng, B) == [(100, "other", 3)]


# ---- determinism ---------------------------------------------------------------------------

def test_same_actions_same_fills():
    def run():
        eng = Engine(check_every_action=True)
        out = []
        for a in [place("a", S, 100, 5), place("b", S, 101, 5), place("c", B, 101, 7, Tif.IOC),
                  place("d", B, 99, 2, Tif.ALO), ScheduleCancel(**meta("b"), at_ms=10)]:
            out.append(eng.apply(a)[0])
        eng.end_block(1, 10)
        return [(f.maker_order, f.px, f.sz, f.seq) for fs in out for f in fs], resting(eng, B), resting(eng, S)
    assert run() == run()


# ---- rule 8: snapshot reconciliation -------------------------------------------------------

def test_reconcile_trims_placeholders_newest_first_and_never_known_orders(eng):
    eng.seed(0, B, 100, 5)                       # older placeholder
    eng.apply(place("m", B, 100, 3))             # known order
    eng.seed(0, B, 100, 4)                       # newer placeholder
    out = eng.reconcile(0, bids=[(100, 4)], asks=[])
    # ours 12, snapshot 4: gap 8 -> newer placeholder 4, older placeholder 4; known order kept
    assert out == {"trimmed": 8, "drift": 0, "missing": 0, "evicted": 0, "evicted_orders": 0}
    assert resting(eng, B) == [(100, PLACEHOLDER, 1), (100, "m", 3)]
    eng.books[0].check_invariants()


def test_reconcile_reports_drift_when_placeholders_run_out(eng):
    eng.seed(0, S, 105, 2)
    eng.apply(place("m", S, 105, 6))
    out = eng.reconcile(0, bids=[], asks=[(105, 3)])
    assert out == {"trimmed": 2, "drift": 3, "missing": 0, "evicted": 0, "evicted_orders": 0}
    assert resting(eng, S) == [(105, "m", 6)]


def test_reconcile_range_and_missing(eng):
    eng.seed(0, B, 103, 5)                       # above the snapshot's best bid: snapshot says 0
    eng.seed(0, B, 90, 5)                        # below the snapshot's range: untouched
    out = eng.reconcile(0, bids=[(101, 7), (100, 1)], asks=[])
    assert out == {"trimmed": 5, "drift": 0, "missing": 8, "evicted": 0, "evicted_orders": 0}  # nothing added for missing
    assert resting(eng, B) == [(90, PLACEHOLDER, 5)]


def test_reconcile_evicts_known_orders_oldest_first_when_asked(eng):
    eng.seed(0, S, 105, 2)
    eng.apply(place("old", S, 105, 2, Tif.ALO, cloid="0xo"))
    eng.apply(place("mid", S, 105, 3))
    eng.apply(place("new", S, 105, 4))
    out = eng.reconcile(0, bids=[], asks=[(105, 5)], known="oldest")
    # ours 11, snapshot 5: placeholder 2, then drift 4 -> "old" removed (2), "mid" reduced by 2
    assert out == {"trimmed": 2, "drift": 4, "missing": 0, "evicted": 4, "evicted_orders": 1}
    assert resting(eng, S) == [(105, "mid", 1), (105, "new", 4)]
    assert "old" not in eng.by_user and not eng.by_cloid    # indices follow the removal
    eng.books[0].check_invariants()


def test_reconcile_evicts_newest_first_and_rejects_unknown_order(eng):
    eng.apply(place("old", S, 105, 2))
    eng.apply(place("new", S, 105, 4))
    out = eng.reconcile(0, bids=[], asks=[(105, 2)], known="newest")
    assert (out["evicted"], out["evicted_orders"]) == (4, 1)
    assert resting(eng, S) == [(105, "old", 2)]
    with pytest.raises(ValueError):
        eng.reconcile(0, bids=[], asks=[], known="random")


def test_evict_removes_a_known_order_and_counts_it(eng):
    eng.apply(place("m", B, 100, 3, Tif.ALO, cloid="0xc"))
    (oid,) = list(eng.books[0].index)
    assert eng.evict(oid).user == "m"
    assert eng.books[0].best_bid() is None and not eng.by_cloid and eng.stats["evicted_by_gate"] == 1


def test_clear_removes_every_order_on_one_asset_only(eng):
    eng.seed(0, B, 100, 5)
    eng.apply(place("m", S, 105, 3, Tif.ALO, cloid="0xc"))
    eng.seed(1, B, 50, 1)
    assert eng.clear(0) == {"cleared_known": 1, "cleared_placeholder": 1}
    assert eng.books[0].best_bid() is None and eng.books[0].best_ask() is None
    assert eng.books[1].best_bid() == 50 and not eng.by_user and not eng.by_cloid


def test_duplicate_cloid_is_unresolved_never_guessed(eng):
    eng.apply(place("u", B, 100, 5, Tif.ALO, cloid="0xdup"))
    eng.apply(place("u", B, 99, 5, Tif.ALO, cloid="0xdup"))
    _, rejects = eng.apply(CancelByCloid(**meta("u"), asset=0, cloid="0xdup"))
    assert [r.reason for r in rejects] == [RejectReason.UNKNOWN_ORDER]
    assert eng.stats["cloid_ambiguous"] == 1 and len(eng.books[0].index) == 2
    # once one of them is gone, the cloid is unique again and resolves
    eng.apply(place("t", S, 100, 5, Tif.IOC))
    assert eng.apply(CancelByCloid(**meta("u"), asset=0, cloid="0xdup")) == ([], [])
    assert resting(eng, B) == []
