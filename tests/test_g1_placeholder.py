"""H11 placeholder deep dive: replay placeholder measurement flags (ttl, behind, cap next, keep known),
validate_replay pass-through, and analysis/g1_placeholder.py helpers + attribution."""

import pytest

from analysis import g1_placeholder as gp
from analysis import validate_replay as vr
from engine_py.matching import PLACEHOLDER
from engine_py.replay import Replay, listing
from engine_py.types import ASSETS, Side
from tests.window_fixture import A, B, M, T0, Window, order, place_tx, tx

BTC = ASSETS[0]
CL = "0x" + "1" * 32


def ticks(p):
    return BTC.px_to_ticks(p)


def lots(s):
    return BTC.sz_to_lots(s)


def cloid_cancel_tx(user, h, cloid):
    return tx(user, {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": cloid}]}, h, 0)


def level(r, side, px):
    lv = r.engine.books[0].side(side).get(ticks(px))
    return [] if lv is None else [(nd.order.user, nd.remaining) for nd in lv]


# ------------------------------------------------------------------ replay flags

def test_ph_meta_records_snapshot_time_rank_and_size(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001"), ("59990", "0.002")], [("60010", "0.003")])
    w.block([])
    r = Replay(*w.write(), listing)
    metas = sorted(m for m in r.ph_meta.values() if m[3] == 0)
    assert metas == [(T0 - 100, 0, lots("0.001"), 0), (T0 - 100, 0, lots("0.003"), 0), (T0 - 100, 1, lots("0.002"), 0)]


def test_placeholder_ttl_removes_only_expired_placeholders(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60010", "0.001")])
    w.block([], dt_ms=0)
    w.book("BTC", T0 + 500, [("60000", "0.001")], [("60010", "0.001")])
    w.block([], dt_ms=1000)
    w.block([], dt_ms=1600)
    window, hl = w.write()
    r = Replay(window, hl, listing, reseed=True, placeholder_ttl_ms=1000)
    it = iter(r)
    next(it)                            # block 0: the +500 snapshot re-seeds after it (the -100 seed is cleared)
    next(it)                            # block 1 at +1000: nothing older than 1000 ms yet
    assert r.ph_stats["ttl_expired_orders"] == 0 and level(r, Side.BUY, "60000") == [(PLACEHOLDER, lots("0.001"))]
    next(it)                            # block 2 at +1600: the +500 re-seed is 1100 ms old: both sides expire
    assert level(r, Side.BUY, "60000") == [] and level(r, Side.SELL, "60010") == []
    assert r.ph_stats["ttl_expired_orders"] == 2 and r.ph_stats["ttl_expired_lots:BTC"] == 2 * lots("0.001")


def test_placeholder_queue_behind_lets_known_orders_fill_first(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [], [("60010", "0.001")])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60010", sz="0.001", tif="Gtc"))])
    w.block([place_tx(A, "0xa1", 0, order(px="60010", sz="0.001", tif="Ioc"))])
    window, hl = w.write()
    (_, f, _, _), = list(Replay(window, hl, listing))[1:]
    assert [x.maker_user for x in f] == [PLACEHOLDER]
    r = Replay(window, hl, listing, placeholder_queue="behind")
    (_, f, _, _), = list(r)[1:]
    assert [x.maker_user for x in f] == [M] and r.ph_stats["moved_behind"] == 1
    assert level(r, Side.SELL, "60010") == [(PLACEHOLDER, lots("0.001"))]
    assert all(m[0] == T0 - 100 for m in r.ph_meta.values() if m[3] == 0)   # meta follows the moved placeholder


def test_placeholder_cap_next_uses_the_next_snapshot_plus_real_trades(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [], [])
    w.block([], dt_ms=0)
    w.book("BTC", T0 + 500, [], [("60010", "0.005"), ("60020", "0.004"), ("60090", "0.001")])
    w.block([], dt_ms=1000)
    w.trade("BTC", "B", "60010", "0.001", T0 + 1500, "0x" + "0" * 64, 1, B, M)   # 1 lot traded at 60010
    w.book("BTC", T0 + 2000, [], [("60010", "0.001"), ("60030", "0.001")])     # 60020 gone; 60090 beyond
    w.block([], dt_ms=2500)
    window, hl = w.write()
    r = Replay(window, hl, listing, reseed=True, placeholder_cap="next")
    it = iter(r)
    next(it)                            # block 0, then the +500 snapshot is re-seeded, capped by the +2000 one
    assert level(r, Side.SELL, "60010") == [(PLACEHOLDER, lots("0.002"))]     # min(5, 1 + 1 traded)
    assert level(r, Side.SELL, "60020") == []                                  # min(4, 0)
    assert level(r, Side.SELL, "60090") == [(PLACEHOLDER, lots("0.001"))]     # outside the next range: kept
    assert r.ph_stats["cap_removed_lots:BTC"] == lots("0.003") + lots("0.004")


def test_reseed_keep_known_keeps_orders_seeds_missing_size_ahead_and_cancels_reach_them(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [], [])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60010", sz="0.001", tif="Gtc", cloid=CL))], dt_ms=0)
    w.book("BTC", T0 + 500, [], [("60010", "0.003")])
    w.block([], dt_ms=1000)
    w.block([cloid_cancel_tx(M, "0xm2", CL)], dt_ms=1500)
    window, hl = w.write()
    r = Replay(window, hl, listing, reseed=True, reseed_keep_known=True)
    it = iter(r)
    next(it), next(it)
    assert level(r, Side.SELL, "60010") == [(PLACEHOLDER, lots("0.002")), (M, lots("0.001"))]
    assert r.reconciled[("BTC", "refilled")] == lots("0.002")     # alias of keep_known: Engine.refill counts it
    (_, _, rejects, _) = next(it)
    assert rejects == [] and level(r, Side.SELL, "60010") == [(PLACEHOLDER, lots("0.002"))]
    rr = Replay(window, hl, listing, reseed=True)                  # plain re-seed: the cancel finds nothing
    (_, _, rejects, _) = list(rr)[2]
    assert [x.reason.name for x in rejects] == ["UNKNOWN_ORDER"]


@pytest.mark.parametrize("kw", [dict(reseed_keep_known=True), dict(placeholder_queue="front"),
                                dict(placeholder_cap="prev")])
def test_bad_placeholder_flags_fail_loudly(tmp_path, kw):
    w = Window(tmp_path)
    w.block([])
    with pytest.raises(ValueError):
        Replay(*w.write(), listing, **kw)


def test_validate_reports_placeholder_flags_only_when_set(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [], [("60010", "0.001")])
    w.block([])
    window, hl = w.write()
    res, _ = vr.run(window, hl, quiet=True)
    assert not any(k.startswith(("placeholder_", "ph:", "reseed_keep")) for k in res)
    res, _ = vr.run(window, hl, quiet=True, reseed=True, placeholder_ttl_ms=5, placeholder_queue="behind",
                    placeholder_cap="next", reseed_keep_known=False)
    assert (res["placeholder_ttl_ms"], res["placeholder_queue"], res["placeholder_cap"]) == (5, "behind", "next")
    vr.main([str(window), "--hl-data", str(hl), "--reseed", "--reseed-keep-known", "--placeholder-ttl-ms", "9",
             "--out", str(tmp_path / "o")])
    assert "reseed_keep_known,True" in (tmp_path / "o" / "summary.csv").read_text()


def test_keep_known_scores_a_pre_reseed_known_order_on_price_and_size(tmp_path):
    # M rested before the snapshot; with keep-known the engine fills M's (kept) order, the chain fills M too,
    # but M did not re-quote after the re-seed, so both sides are WILDCARD: a match, not a name mismatch.
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [], [])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60010", sz="0.001", tif="Gtc"))], dt_ms=0)
    w.book("BTC", T0 + 500, [], [("60010", "0.001")])
    w.block([], dt_ms=1000)
    h = w.block([place_tx(A, "0xa1", 0, order(px="60010", sz="0.001", tif="Ioc"))], dt_ms=1500)
    w.trade("BTC", "B", "60010", "0.001", w.time_of(h), "0xa1", 1, A, M)
    window, hl = w.write()
    _, rows = vr.run(window, hl, quiet=True, reseed=True, reseed_keep_known=True)
    (r,) = [x for x in rows if x["hash"] == "0xa1"]
    assert r["status"] == "match" and r["engine"].endswith(M[:10])


# ------------------------------------------------------------------ helpers

def test_buckets_and_touch_position():
    assert gp.age_bucket(0) == "0-500 ms" and gp.age_bucket(999) == "500-2000 ms" and gp.age_bucket(9000) == ">= 5000 ms"
    assert gp.rank_bucket(0) == "level 0" and gp.rank_bucket(3) == "levels >= 3" and gp.rank_bucket(30) == "levels >= 3"
    t = ticks
    assert gp.touch_position(Side.SELL, t("60000"), t("59990"), t("60010")) == "better than real touch (stale touch)"
    assert gp.touch_position(Side.SELL, t("60010"), None, t("60010")) == "at real touch"
    assert gp.touch_position(Side.SELL, t("60011"), None, t("60010")) == "behind touch, <= 1 bps"
    assert gp.touch_position(Side.BUY, t("59900"), t("60000"), None) == "behind touch, > 5 bps"
    assert gp.touch_position(Side.BUY, t("60000"), None, t("1")) == "no touch estimate"


def test_withdrawn_lots_and_split_precedence():
    assert gp.withdrawn_lots(10, 3, 2) == 5 and gp.withdrawn_lots(10, 9, 2) == 0
    sp = gp.split_group(10, {"cloid": 2, "int oid": 1}, 3, ("in range", 3))
    assert sp == {"ghost cancel (cloid)": 2, "ghost cancel (int oid)": 1, "queue: chain filled a post-re-seed maker": 3,
                  "withdrawn by next snapshot (net, look-ahead)": 3, "still there in next snapshot": 1}
    sp = gp.split_group(4, {"cloid": 9}, 0, ("beyond", 0))
    assert sp["ghost cancel (cloid)"] == 4 and sum(sp.values()) == 4
    assert gp.split_group(5, {}, 0, ("beyond", 0))["beyond next snapshot depth / none"] == 5


def test_trade_index_counts_lots_in_half_open_window():
    from data.parse import Trade
    trs = [Trade(0, Side.BUY, 100, 2, 10, "h", 1, B, M), Trade(0, Side.BUY, 100, 3, 20, "h", 2, B, M)]
    ix = gp.TradeIndex(trs)
    assert ix.lots(0, Side.SELL, 100, 10, 20) == 2 and ix.lots(0, Side.SELL, 100, 0, 21) == 5
    assert ix.lots(0, Side.BUY, 100, 0, 99) == 0


# ------------------------------------------------------------------ attribution

def test_owner_cancel_of_a_cleared_order_is_attributed_as_ghost_cancel(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("59990", "0.001")], [])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60010", sz="0.001", tif="Gtc", cloid=CL))], dt_ms=0)
    w.book("BTC", T0 + 500, [("59990", "0.001")], [("60010", "0.001")])    # M's order is now a placeholder
    w.block([], dt_ms=1000)
    w.block([cloid_cancel_tx(M, "0xm2", CL)], dt_ms=1200)                 # M pulls it: we cannot apply that
    w.block([place_tx(A, "0xa1", 0, order(px="60010", sz="0.001", tif="Ioc"))], dt_ms=1400)   # chain: no fill
    w.book("BTC", T0 + 2000, [("59990", "0.001")], [("60020", "0.001")])
    w.block([], dt_ms=2500)
    window, hl = w.write()
    out = gp.run_instrumented(window, hl, tmp_path)
    at = out["attribution"]
    n = ticks("60010") * lots("0.001")
    assert at["total"]["spurious placeholder"] == n
    assert {k: v for k, v in at["split"].items() if v} == {"ghost cancel (cloid)": n}
    assert at["marginals"]["age since its snapshot"] == {"500-2000 ms": n}
    assert at["marginals"]["rank in its snapshot"] == {"level 0": n}
    assert at["unknown_cancels"] == {"cloid matched_ghost=True": 1}


def test_without_a_cancel_the_next_snapshot_decides_withdrawn_vs_still_there(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("59990", "0.001")], [])
    w.block([], dt_ms=0)
    w.book("BTC", T0 + 500, [("59990", "0.001")], [("60010", "0.003")])
    w.block([], dt_ms=1000)
    w.block([place_tx(A, "0xa1", 0, order(px="60010", sz="0.003", tif="Ioc"))], dt_ms=1400)   # chain: no fill
    w.book("BTC", T0 + 2000, [("59990", "0.001")], [("60010", "0.001")])   # 2 of 3 lots pulled
    w.block([], dt_ms=2500)
    window, hl = w.write()
    at = gp.run_instrumented(window, hl, tmp_path)["attribution"]
    px = ticks("60010")
    assert at["split"]["withdrawn by next snapshot (net, look-ahead)"] == 2 * lots("0.001") * px
    assert at["split"]["still there in next snapshot"] == lots("0.001") * px


def test_keep_known_refill_placeholders_get_ph_meta_and_expire_with_ttl(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [], [])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60010", sz="0.001", tif="Gtc", cloid=CL))], dt_ms=0)
    w.book("BTC", T0 + 500, [], [("60010", "0.003")])
    w.block([], dt_ms=1000)
    w.block([], dt_ms=5000)
    window, hl = w.write()
    r = Replay(window, hl, listing, reseed=True, keep_known=True, placeholder_ttl_ms=1000)
    it = iter(r)
    next(it), next(it)
    (ph,) = [nd.order.oid for nd in r.engine.books[0].side(Side.SELL)[ticks("60010")] if nd.order.user == PLACEHOLDER]
    assert r.ph_meta[ph][1:3] == (0, lots("0.002"))
    next(it)
    assert level(r, Side.SELL, "60010") == [(M, lots("0.001"))] and r.ph_stats["ttl_expired_orders"] == 1


def test_wildcard_pre_reseed_known_is_off_by_default_and_reported_when_on(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [], [("60010", "0.001")])
    w.block([])
    window, hl = w.write()
    res, _ = vr.run(window, hl, quiet=True, reseed=True, keep_known=True)
    assert "wildcard_pre_reseed_known" not in res
    res, _ = vr.run(window, hl, quiet=True, reseed=True, keep_known=True, wildcard_pre_reseed_known=True)
    assert res["wildcard_pre_reseed_known"] is True
