"""analysis/validate_replay.py: scored population, match rules, every mismatch category,
the ALO ground-truth join and the CSV outputs."""

import csv
import gzip
import json

from analysis import validate_replay as vr
from tests.window_fixture import A, B, C, M, T0, ZERO, Window, cancel_tx, order, place_tx, tx

N = "0x" + "e" * 40
PX = "60010"


def alo_sell(user, h, px=PX, sz="0.001", cloid=None):
    return place_tx(user, h, 0, order(buy=False, px=px, sz=sz, tif="Alo", cloid=cloid))


def ioc_buy(user, h, px=PX, sz="0.001"):
    return place_tx(user, h, 0, order(buy=True, px=px, sz=sz, tif="Ioc"))


def real_buy(w, height, h, maker, sz="0.001", px=PX, tid=1, taker=A):
    w.trade("BTC", "B", px, sz, w.time_of(height), h, tid, taker, maker)


def run(w, tmp_path, alo_status=None, out=True, **kw):
    window, hl = w.write(alo_status)
    return vr.run(window, hl, out_dir=tmp_path / "out" if out else None, quiet=True, **kw)


def row(rows, h):
    (r,) = [r for r in rows if r["hash"] == h]
    return r


def two_makers_then_taker(w):
    """M then N rest an ALO sell at PX; A's IOC buy takes M under time priority."""
    w.block([alo_sell(M, "0xm1"), alo_sell(N, "0xn1")])
    return w.block([ioc_buy(A, "0xa1")])


# ------------------------------------------------------------------ pure pieces

def test_alo_truth_classes():
    order_ans = lambda st: {"status": "order", "order": {"order": {}, "status": st}}
    assert vr.alo_truth_class(order_ans("badAloPxRejected")) == "rejected_alo"
    assert vr.alo_truth_class(order_ans("perpMarginRejected")) == "rejected_other"
    for st in ("open", "filled", "canceled"):
        assert vr.alo_truth_class(order_ans(st)) == "accepted"
    assert vr.alo_truth_class({"status": "unknownOid"}) == "unknown"


def test_load_alo_status_reads_plain_and_gz_filters_heights_and_skips_a_cut_line(tmp_path):
    rows = [{"block": h, "hash": f"0x{h}", "item": 0, "answer": {"status": "order",
             "order": {"order": {}, "status": "badAloPxRejected"}}} for h in (5, 6, 99)]
    d1, d2 = tmp_path / "2026-11-18" / "03", tmp_path / "2026-11-18" / "04"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    with gzip.open(d1 / "alo_status.jsonl.gz", "wt") as f:
        f.write(json.dumps({"msg": rows[0]}) + "\n")
    (d2 / "alo_status.jsonl").write_text(json.dumps({"msg": rows[1]}) + "\n" + json.dumps({"msg": rows[2]})
                                         + "\n" + '{"msg": {"blo')
    got = vr.load_alo_status(tmp_path, range(0, 10))
    assert set(got) == {(5, "0x5", 0), (6, "0x6", 0)}
    assert got[(5, "0x5", 0)] == {"truth": "rejected_alo", "status": "badAloPxRejected"}


def test_match_is_per_price_and_maker_px_sz_ignores_makers():
    real = [(10, 3, M), (10, 2, N)]
    assert vr.is_match(real, [(10, 2, N, 7), (10, 3, M, 8)])
    assert not vr.is_match(real, [(10, 5, M, 7)]) and vr.is_match_px_sz(real, [(10, 5, M, 7)])
    assert not vr.is_match_px_sz(real, [(11, 5, M, 7)])


def test_categorize_follows_the_precedence():
    assert vr.categorize({"ordering", "margin rejection (unobservable)"}) == "ordering"
    assert vr.categorize({"unresolved cancel", "outside-fill timing"}) == "outside-fill timing"
    assert vr.categorize({vr.INFO_ALO_REJECTED}) == "real bug (unexplained)"
    assert vr.categorize(set()) == "real bug (unexplained)"


# ------------------------------------------------------------------ end to end

def test_exact_match_and_warmup_is_not_scored(tmp_path):
    w = Window(tmp_path, warmup_blocks=2)
    w.block([alo_sell(M, "0xm1"), alo_sell(N, "0xn1", sz="0.002")])
    h1 = w.block([ioc_buy(A, "0xa1")])                       # in warm-up
    h2 = w.block([ioc_buy(B, "0xb1")])                       # scored
    real_buy(w, h1, "0xa1", M, tid=1)
    real_buy(w, h2, "0xb1", N, tid=2, taker=B)
    res, rows = run(w, tmp_path)
    assert row(rows, "0xa1")["warmup"] == 1 and row(rows, "0xa1")["status"] == "match"
    assert row(rows, "0xb1")["warmup"] == 0 and row(rows, "0xb1")["status"] == "match"
    assert res["all:scored"] == 1 and res["all:match_rate"] == "1.0000" and res["warmup_txs"] == 1


def test_real_maker_without_an_in_window_quote_is_excluded(tmp_path):
    w = Window(tmp_path)
    h = two_makers_then_taker(w)
    real_buy(w, h, "0xa1", C)                                # C never quoted in the window
    res, rows = run(w, tmp_path)
    assert row(rows, "0xa1")["status"] == "excluded: real maker pre-window"
    assert res["all:scored"] == 0 and res["excluded_real_maker_prewindow"] == 1


def test_placeholder_fill_is_unknown_pre_window_order(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [], [(PX, "0.001")])
    w.block([alo_sell(N, "0xn1")])                           # rests behind the placeholder
    h = w.block([ioc_buy(A, "0xa1")])
    real_buy(w, h, "0xa1", N)
    _, rows = run(w, tmp_path)
    r = row(rows, "0xa1")
    assert r["category"] == "unknown pre-window order" and r["match_px_sz"] == 1


def test_ordering_beats_margin_when_sizes_agree(tmp_path):
    w = Window(tmp_path)
    h = two_makers_then_taker(w)
    real_buy(w, h, "0xa1", N)                                # chain gave the fill to N
    _, rows = run(w, tmp_path)
    r = row(rows, "0xa1")
    assert r["category"] == "ordering"
    assert "margin rejection (unobservable)" in r["flags"]


def test_engine_maker_absent_from_chain_is_margin_rejection(tmp_path):
    w = Window(tmp_path)
    h = two_makers_then_taker(w)
    real_buy(w, h, "0xa1", N, sz="0.0005")                   # different size too
    _, rows = run(w, tmp_path)
    assert row(rows, "0xa1")["category"] == "margin rejection (unobservable)"


def test_engine_only_fill_is_a_scored_mismatch(tmp_path):
    w = Window(tmp_path)
    two_makers_then_taker(w)                                 # no real trade at all
    res, rows = run(w, tmp_path)
    r = row(rows, "0xa1")
    assert r["engine_only"] == 1 and r["status"] == "mismatch"
    assert res["all:engine_only_txs"] == 1 and res["all:match_rate_real_linked_only"] == ""


def test_unresolved_int_oid_cancel_that_may_target_the_maker_order(tmp_path):
    """M rests two orders whose estimated oids are both near the cancel's oid: the resolver
    calls it ambiguous (never guesses), so the engine keeps M's order and fills it."""
    w = Window(tmp_path)
    h0 = w.block([place_tx(M, "0xm1", 0, order(buy=False, px=PX), order(buy=False, px="60020")),
                  alo_sell(N, "0xn1")])
    w.block([cancel_tx(M, "0xm2", 0, 0, 5000)])
    h = w.block([ioc_buy(A, "0xa1")])
    w.anchors = [{"oid": 5000, "height": h0, "tx": 0, "item": 0}]
    real_buy(w, h, "0xa1", N)
    res, rows = run(w, tmp_path)
    assert res["resolver_stats"]["ambiguous"] == 1
    assert row(rows, "0xa1")["category"] == "unresolved cancel"
    pm = [r for r in res["per_minute"] if r["coin"] == "BTC"][0]
    assert (pm["oid_cancels"], pm["oid_unknown"]) == (1, 1)


def test_missed_cloid_cancel_is_not_pinned_to_an_order(tmp_path):
    w = Window(tmp_path)
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px=PX, cloid="0x01")), alo_sell(N, "0xn1")])
    w.block([tx(M, {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": "0x02"}]}, "0xm2", 0)])
    h = w.block([ioc_buy(A, "0xa1")])
    real_buy(w, h, "0xa1", N)
    res, rows = run(w, tmp_path)
    assert "unresolved cancel" not in row(rows, "0xa1")["flags"]
    pm = [r for r in res["per_minute"] if r["coin"] == "BTC"][0]
    assert (pm["cloid_cancels"], pm["cloid_unknown"]) == (1, 1)


def test_outside_fill_in_the_same_block(tmp_path):
    w = Window(tmp_path)
    h = two_makers_then_taker(w)
    real_buy(w, h, "0xa1", N)
    w.trade("BTC", "B", "61000", "0.001", w.time_of(h), ZERO, 9, B, C)   # zero-hash, finds no maker
    _, rows = run(w, tmp_path)
    assert row(rows, "0xa1")["category"] == "outside-fill timing"


def alo_answer(block, h, status, item=0):
    return {"block": block, "hash": h, "item": item,
            "answer": {"status": "order", "order": {"order": {}, "status": status}}}


def test_wrong_alo_decision_engine_rested_what_the_chain_rejected(tmp_path):
    w = Window(tmp_path)
    h = two_makers_then_taker(w)
    real_buy(w, h, "0xa1", N)
    res, rows = run(w, tmp_path, alo_status=[alo_answer(1000, "0xm1", "badAloPxRejected"),
                                             alo_answer(1000, "0xn1", "open")])
    assert row(rows, "0xa1")["category"] == "wrong ALO decision"
    assert res["alo_confusion"] == {("accepted", "rejected_alo"): 1, ("accepted", "accepted"): 1}


def test_engine_rejected_a_real_makers_alo(tmp_path):
    """N's ALO crosses a placeholder bid in our book and is rejected; the chain filled N."""
    def build(sub):
        w = Window(tmp_path / sub)
        w.book("BTC", T0 - 100, [(PX, "0.001")], [])
        w.block([alo_sell(N, "0xn1")])
        h = w.block([ioc_buy(A, "0xa1")])
        real_buy(w, h, "0xa1", N)
        return w

    _, rows = run(build("no_truth"), tmp_path / "no_truth")
    r = row(rows, "0xa1")
    assert r["category"] == "real bug (unexplained)" and vr.INFO_ALO_REJECTED in r["flags"]
    res, rows = run(build("truth"), tmp_path / "truth", alo_status=[alo_answer(1000, "0xn1", "open")])
    assert row(rows, "0xa1")["category"] == "wrong ALO decision"
    assert res["alo_confusion"] == {("rejected", "accepted"): 1}


def test_csv_outputs(tmp_path):
    w = Window(tmp_path)
    h = two_makers_then_taker(w)
    real_buy(w, h, "0xa1", N)
    run(w, tmp_path)
    out = tmp_path / "out"
    names = {p.name for p in out.iterdir()}
    assert names == {"summary.csv", "per_minute.csv", "mismatches.csv", "txs.csv", "alo_truth.csv",
                     "first_mismatches.csv"}
    summary = dict(csv.reader(open(out / "summary.csv")))
    assert summary["all:scored"] == "1" and summary["all:match_rate"] == "0.0000"
    assert summary["policy"] == "listing"
    mism = list(csv.DictReader(open(out / "mismatches.csv")))
    assert [m["category"] for m in mism] == list(vr.CATEGORIES)
    assert next(m for m in mism if m["category"] == "ordering")["total"] == "1"
    first = list(csv.DictReader(open(out / "first_mismatches.csv")))
    assert len(first) == 1 and first[0]["book_before"].startswith("SELL side: 60010")
    pm = list(csv.DictReader(open(out / "per_minute.csv")))
    assert {r["coin"] for r in pm} >= {"BTC"} and "resting_known_orders" in pm[0]


def test_fix_options_reach_the_replay_and_the_summary(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("59990", "0.001")], [("60020", "0.001")])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60000", tif="Gtc"))], dt_ms=0)
    w.block([place_tx(A, "0xa2", 0, order(px="60000"))], dt_ms=100)
    w.book("BTC", T0 + 500, [("60000", "0.001")], [("60020", "0.001")])
    w.block([], dt_ms=1000)
    window, hl = w.write()
    res, _ = vr.run(window, hl, out_dir=tmp_path / "out", quiet=True, alo_gate="touch", evict_known="oldest")
    assert res["alo_gate"] == "touch" and res["evict_known"] == "oldest"
    assert res["gate_stats"]["evicted_orders"] == 1
    assert res["touch_errors"]["BTC"] == {"n": 1, "exact": 0, "median_ticks": 100, "p90_ticks": 100}   # 10 USD, tick 0.1
    summary = dict(csv.reader(open(tmp_path / "out" / "summary.csv")))
    assert summary["gate:evicted_orders"] == "1" and summary["touch:BTC:n"] == "1"


def test_notional_metrics_are_integer_and_partial():
    real = [(10, 3, M), (11, 2, N)]
    engine = [(10, 2, M, 1), (11, 2, C, 2)]
    assert vr.notional(real) == 52 and vr.notional(engine) == 42
    assert vr.matched_notional(real, engine, lambda f: (f[0], f[2])) == 20          # only M's 2 @ 10
    assert vr.matched_notional(real, engine, lambda f: f[0]) == 42                  # px/size: 2@10 + 2@11


def test_summary_reports_fill_recall_and_precision(tmp_path):
    w = Window(tmp_path)
    h = two_makers_then_taker(w)
    real_buy(w, h, "0xa1", M, sz="0.0005")              # engine filled 0.001 from M: half was real
    res, rows = run(w, tmp_path)
    assert res["all:fill_recall"] == "1.0000" and res["all:fill_precision"] == "0.5000"


def test_nearest_ratio_reaches_the_resolver(tmp_path):
    w = Window(tmp_path)
    h0 = w.block([place_tx(M, "0xm1", 0, order(buy=False, px=PX))])
    w.anchors = [{"oid": 5000, "height": h0, "tx": 0, "item": 0}]
    res, _ = vr.run(*w.write(), quiet=True, nearest_ratio=3)
    assert res["nearest_ratio"] == 3


def reseed_window(tmp_path, quote_after_reseed):
    """Snapshot at T0+500 re-seeds an ask of 0.001 at PX. N's real order there predates it (or not)."""
    w = Window(tmp_path)
    w.block([alo_sell(N, "0xn1")], dt_ms=0)
    w.book("BTC", T0 + 500, [], [(PX, "0.001")])
    txs = [alo_sell(N, "0xn2", px="60020")] + ([alo_sell(N, "0xn3")] if quote_after_reseed else [])
    w.block(txs, dt_ms=1000)
    h = w.block([ioc_buy(A, "0xa1")], dt_ms=1500)
    real_buy(w, h, "0xa1", N)
    return w


def test_reseed_scores_pre_reseed_liquidity_on_price_and_size(tmp_path):
    res, rows = run(reseed_window(tmp_path, quote_after_reseed=False), tmp_path, reseed=True)
    r = row(rows, "0xa1")
    assert r["status"] == "match" and r["horizon_ms"] == 1000 and res["reseed"] == "every snapshot"
    assert res["horizons"][0]["horizon"] == "0-2s" and res["horizons"][0]["matched"] == 1


def test_reseed_scores_orders_placed_after_it_strictly(tmp_path):
    # N re-quoted at PX after the re-seed, but the engine filled the placeholder first: strict mismatch
    _, rows = run(reseed_window(tmp_path, quote_after_reseed=True), tmp_path, reseed=True)
    r = row(rows, "0xa1")
    assert r["status"] == "mismatch" and r["match_px_sz"] == 1
