"""collect/capture.py: snapshot-aligned chunks, background writer, per-day fetch counts, cap wait.
No network: the fetcher and orderStatus are stubs."""
import calendar
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collect"))
import capture as cap  # noqa: E402
from fetch_blocks import ArchivedCap  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "WIN_DIR", str(tmp_path / "windows"))
    monkeypatch.setattr(cap, "LOG", str(tmp_path / "capture.log"))
    return tmp_path


def test_plan_chunk_starts_after_the_last_block_at_or_before_the_snapshot():
    headers = [(100, 1000), (101, 1070), (102, 1140), (103, 1210)]
    assert cap.plan_chunk(1100, headers, 30) == (102, 131)
    assert cap.plan_chunk(1070, headers, 5) == (102, 106)       # block at exactly the snapshot time
    assert cap.plan_chunk(1300, headers, 5) is None             # no newer header yet: not settled
    assert cap.plan_chunk(900, headers, 5) is None              # snapshot older than every header


def test_seconds_until_utc_midnight():
    ts = calendar.timegm((2026, 11, 18, 23, 59, 0))
    assert cap.seconds_until_utc_midnight(ts) == 60 + 120
    assert cap.seconds_until_utc_midnight(ts, margin_s=0) == 60


def test_count_fetch_rolls_over_per_utc_day(monkeypatch):
    state = {"day": "2000-01-01", "fetched_today": 999, "max_age_today_s": 5000}
    cap.count_fetch(state, 0.5)
    cap.count_fetch(state, 0.25)
    assert state["fetched_today"] == 2 and state["max_age_today_s"] == 30.0 and state["day"] != "2000-01-01"


def test_tail_lines_skips_cut_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(json.dumps({"i": i}) for i in range(100)) + '\n{"i": 10')
    rows = cap.tail_lines(str(p), nbytes=60)
    assert rows and rows[-1] == {"i": 99} and all("i" in r for r in rows)


def test_writer_saves_and_asks_in_the_background_and_drain_waits(tmp_path):
    asked = []
    w = cap.Writer(lookup=lambda u, o: asked.append((u, o)) or {"status": "order"}, info_gap_s=0)
    for i in range(5):
        w.save(str(tmp_path / f"{i}.json.gz"), b'{"x": %d}' % i)
    w.ask("0xu", 7, 123)
    w.drain()
    assert sorted(os.listdir(tmp_path)) == [f"{i}.json.gz" for i in range(5)]
    assert asked == [("0xu", 7)] and w.snapshot()[0]["cancel_height"] == 123


def test_writer_reports_a_failed_save(tmp_path):
    w = cap.Writer(saver=lambda p, d: (_ for _ in ()).throw(OSError("disk full")))
    w.save("x", b"")
    with pytest.raises(RuntimeError, match="disk full"):
        w.drain()


class StubFetcher:
    def __init__(self, cap_at=None, ages=None):
        self.cap_at, self.last_age_min = cap_at, None
        self.ages = ages or {}

    def age_min(self, h):
        return self.ages.get(h, 0.1)

    def get(self, h):
        if h == self.cap_at:
            raise ArchivedCap("More than 100 archived blocks queried in one day")
        self.last_age_min = self.age_min(h)
        txs = [{"user": "0xu", "error": None, "action": {"type": "cancel", "cancels": [{"a": 0, "o": 5}]}}]
        b = {"height": h, "blockTime": 1000 + h, "numTxs": 1, "txs": txs}
        return b, json.dumps({"blockDetails": b}).encode()


def test_chunk_capture_writes_blocks_meta_and_counts(tmp_home, monkeypatch):
    monkeypatch.setattr(cap, "ANCHOR_MODE", "targets")           # the legacy int-oid cancel-target lookups
    state, w = {}, cap.Writer(lookup=lambda u, o: {"status": "order", "order": {}}, info_gap_s=0)
    meta = {"kind": "chunk", "snapshot_time": 999}
    status = cap.capture(StubFetcher(ages={11: 0.5}), 10, 14, meta, writer=w, state=state, warmup=0, max_anchors=2)
    d = tmp_home / "windows" / "10-14"
    m = json.loads((d / "window.json").read_text())
    assert status == "complete" and m["warmup_end_height"] == 10 and m["kind"] == "chunk"
    assert sorted(p.name for p in d.glob("*.json.gz")) == [f"{h}.json.gz" for h in range(10, 15)]
    assert json.load(gzip.open(d / "12.json.gz", "rt"))["blockDetails"]["height"] == 12
    assert m["max_age_s"] == 30.0 and state["fetched_today"] == 5
    assert m["first_block_time"] == 1010 and m["last_block_time"] == 1014
    assert m["anchors_asked"] == 1                     # (user, oid) asked once even if cancelled again


def test_cap_records_where_and_how_much(tmp_home):
    state = {}
    meta = {}
    status = cap.capture(StubFetcher(cap_at=12), 10, 14, meta, writer=cap.Writer(), state=state, warmup=0)
    assert status == "archived_cap"
    assert meta["cap_at"]["height"] == 12 and meta["cap_at"]["fetched_today"] == 2


# ---- lead-in: fetch LEAD_IN_BLOCKS before the chunk, score from the chunk start -------------------

def test_lead_in_moves_the_fetch_start_and_scoring_starts_at_the_chunk():
    assert cap.with_lead_in((102, 131), 0) == (102, 131, 0)
    assert cap.with_lead_in((102, 131), 50) == (52, 131, 50)


def test_lead_in_chunk_meta_marks_the_scoring_start(tmp_home):
    state, w = {}, cap.Writer(lookup=lambda u, o: {"status": "order", "order": {}}, info_gap_s=0)
    start, end, warmup = cap.with_lead_in((10, 14), 3)
    meta = {"kind": "chunk", "snapshot_time": 999, "lead_in_blocks": warmup}
    assert cap.capture(StubFetcher(), start, end, meta, writer=w, state=state, warmup=warmup, max_anchors=2) == "complete"
    m = json.loads((tmp_home / "windows" / "7-14" / "window.json").read_text())
    assert (m["start_height"], m["warmup_end_height"], m["lead_in_blocks"]) == (7, 10, 3)


def test_a_resumed_chunk_keeps_its_lead_in():
    assert cap.resume_warmup({"kind": "chunk", "lead_in_blocks": 150}) == 150
    assert cap.resume_warmup({"kind": "chunk"}) == 0
    assert cap.resume_warmup({}) is None                    # full windows: the WARMUP_BLOCKS default


# ---- anchors by (user, cloid) for placements inside the fetched blocks ------------------------------

def px_tx(user, h, items, kind="order", error=None):
    action = ({"type": "order", "orders": items, "grouping": "na"} if kind == "order" else
              {"type": "modify", "oid": 1, "order": items[0]} if kind == "modify" else
              {"type": "batchModify", "modifies": [{"oid": 1, "order": o} for o in items]})
    return {"time": 5000 + h, "user": user, "action": action, "block": h, "hash": "0x%x" % h, "error": error}


def item(cloid="0xc1", a=0, tif="Alo", buy=True, px="60000", sz="0.001"):
    o = {"a": a, "b": buy, "p": px, "s": sz, "r": False, "t": {"limit": {"tif": tif}}}
    if cloid:
        o["c"] = cloid
    return o


def test_placement_items_numbering_and_filters():
    trig = {"a": 0, "b": True, "p": "1", "s": "1", "r": False, "c": "0xt", "t": {"trigger": {}}}
    items = [item("0x1"), item(None), item("0x3", a=5), trig, item("0x5", tif="Ioc")]
    assert [(i, o["c"]) for i, o in cap.placement_items(px_tx("0xu", 1, items))] == [(0, "0x1"), (4, "0x5")]
    assert [(i, o["c"]) for i, o in cap.placement_items(px_tx("0xu", 1, [item("0x7")], kind="modify"))] == [(0, "0x7")]
    assert [(i, o["c"]) for i, o in cap.placement_items(px_tx("0xu", 1, [item("0x8"), item("0x9")], kind="batchModify"))] \
        == [(0, "0x8"), (1, "0x9")]
    assert cap.placement_items(px_tx("0xu", 1, [item("0x1")], error="rejected")) == []
    assert cap.placement_items({"user": "0xu", "error": None, "action": {"type": "cancel", "cancels": []}}) == []


def block_of(h, txs):
    return {"height": h, "txs": txs}


def test_picker_cloid_takes_the_middle_candidate_of_the_segment_and_never_repeats():
    p = cap.AnchorPicker("cloid")
    for h in (10, 11, 12):
        p.feed(h, block_of(h, [px_tx("0xu", h, [item(f"0x{h}")])]))
    user, cloid, extra = p.take()
    assert (user, cloid) == ("0xu", "0x11")
    assert extra == {"kind": "cloid", "placed_height": 11, "tx": 0, "item": 0}
    assert "height" not in extra                                    # data/oids.py reads "height" as its own format
    assert p.take() is None                                         # candidates were cleared
    p.feed(13, block_of(13, [px_tx("0xu", 13, [item("0x11")])]))    # the same cloid again: already asked
    assert p.take() is None


def test_picker_cloid_skips_a_cloid_that_appears_twice_in_the_fetched_blocks():
    p = cap.AnchorPicker("cloid")
    p.feed(10, block_of(10, [px_tx("0xu", 10, [item("0xdup")]), px_tx("0xv", 10, [item("0xone")])]))
    p.feed(11, block_of(11, [px_tx("0xu", 11, [item("0xdup")])]))
    assert p.take()[:2] == ("0xv", "0xone")


def test_picker_rejects_unknown_mode():
    with pytest.raises(ValueError):
        cap.AnchorPicker("nope")


def test_picker_targets_mode_keeps_the_legacy_choice():
    p = cap.AnchorPicker("targets")
    busy = {"time": 1, "user": "0xb", "error": None, "action": {"type": "cancel", "cancels": [{"a": 0, "o": 1}, {"a": 1, "o": 2}]}}
    quiet = {"time": 1, "user": "0xq", "error": None, "action": {"type": "cancel", "cancels": [{"a": 0, "o": 9}]}}
    p.feed(10, block_of(10, [busy, quiet]))
    assert p.take() == ("0xq", 9, {"cancel_height": 10})


class PlacementFetcher(StubFetcher):
    """Every block holds two cloid placements by distinct users (unique cloids)."""

    def get(self, h):
        txs = [px_tx("0xu%d" % (h % 5), h, [item(f"0x{h:04x}a", px=str(60000 + h))]),
               px_tx("0xw%d" % (h % 3), h, [item(f"0x{h:04x}b", a=1, px=str(2000 + h))])]
        b = {"height": h, "blockTime": 5000 + h, "numTxs": 2, "txs": txs}
        return b, json.dumps({"blockDetails": b}).encode()


def answer_from_tx(tx):
    o = tx["action"]["orders"][0]
    return {"status": "order", "order": {"order": {
        "coin": "BTC" if o["a"] == 0 else "ETH", "side": "B" if o["b"] else "A", "limitPx": o["p"] + ".0",
        "origSz": o["s"], "oid": 9_000_000 + tx["block"], "timestamp": tx["time"], "cloid": o["c"]}, "status": "open"}}


def test_chunk_capture_asks_by_cloid_spread_over_the_blocks_and_the_estimator_can_use_the_answers(tmp_home):
    from data.oids import OidEstimator
    blocks = {}
    asked = []

    def lookup(user, key):
        asked.append((user, key))
        h = int(key[2:6], 16)
        tx = next(t for t in PlacementFetcher().get(h)[0]["txs"] if t["action"]["orders"][0]["c"] == key)
        return answer_from_tx(tx)

    w = cap.Writer(lookup=lookup, info_gap_s=0)
    meta = {"kind": "chunk"}
    start, end = 1000, 1179                                          # 150 lead-in + 30 chunk blocks
    assert cap.capture(PlacementFetcher(), start, end, meta, writer=w, state={}, warmup=150, max_anchors=20) == "complete"
    d = tmp_home / "windows" / f"{start}-{end}"
    anchors = json.loads((d / "anchors.json").read_text())
    assert len(anchors) == 20 and meta["anchors_found"] == 20 and len(asked) == 20
    assert len({a["oid"] for a in anchors}) == 20                    # cloids, all different
    heights = [a["placed_height"] for a in anchors]
    assert heights == sorted(heights) and all(b - a == 9 for a, b in zip(heights, heights[1:]))   # every 9th block
    assert heights[0] < start + 9 and heights[-1] >= end - 9         # spread over the whole fetch, lead-in included
    assert all("height" not in a and a["kind"] == "cloid" for a in anchors)
    est = OidEstimator.from_window(d, anchors)                       # the real consumer locates every one of them
    assert len(est.anchors) == 20


def test_cloid_mode_never_asks_more_than_the_budget_and_resumes_without_repeats(tmp_home):
    asked = []
    w = cap.Writer(lookup=lambda u, o: asked.append(o) or {"status": "unknownOid"}, info_gap_s=0)
    cap.capture(PlacementFetcher(), 1000, 1059, {"kind": "chunk"}, writer=w, state={}, warmup=0, max_anchors=6)
    assert len(asked) == 6
    w2 = cap.Writer(lookup=lambda u, o: asked.append(o) or {"status": "unknownOid"}, info_gap_s=0)
    cap.capture(PlacementFetcher(), 1000, 1059, {"kind": "chunk"}, writer=w2, state={}, warmup=0, max_anchors=6)
    assert len(asked) == 6                                           # all blocks on disk, 6 on record: nothing new


def test_lookups_are_paced_and_default_gap_is_two_seconds(tmp_path):
    import inspect
    import time
    assert inspect.signature(cap.Writer.__init__).parameters["info_gap_s"].default == 2.0
    times = []
    w = cap.Writer(lookup=lambda u, o: times.append(time.time()) or {"status": "order"}, info_gap_s=0.05)
    for i in range(4):
        w.ask("0xu", f"0x{i}", None, {"kind": "cloid"})
    w.drain()
    assert all(b - a >= 0.045 for a, b in zip(times, times[1:])) and len(times) == 4


def test_default_mode_is_cloid():
    assert cap.ANCHOR_MODE == "cloid"


# ---------------------------------------------------------------- info lookups (fills, open orders, history)
def trade(users, side="B", px="60000", sz="0.01", t=1010):
    return {"coin": "BTC", "side": side, "px": px, "sz": sz, "time": t, "hash": "0xh", "tid": 1, "users": users}


def test_user_pickers_are_deterministic():
    tr = [trade(["0xa", "0xm"]), trade(["0xb", "0xm"]), trade(["0xa", "0xn"], side="A", sz="1")]
    assert cap.busiest_users(tr, 2) == ["0xa", "0xm"]                 # 2 trades each, ties by address
    assert cap.top_makers(tr, 2) == ["0xa", "0xm"]                    # taker sold 1 BTC to 0xa: 0xa is the biggest maker
    assert cap.trade_rows([{"msg": {"channel": "trades", "data": tr}}], 1000, 1005) == []


def test_int_cancel_users_counts_btc_eth_int_oid_targets_only():
    b = {"txs": [{"user": "0xU", "error": None, "action": {"type": "cancel", "cancels": [{"a": 0, "o": 5}, {"a": 7, "o": 6}]}},
                 {"user": "0xu", "error": None, "action": {"type": "modify", "oid": 9, "order": {"a": 1}}},
                 {"user": "0xv", "error": None, "action": {"type": "modify", "oid": "0xcloid", "order": {"a": 1}}},
                 {"user": "0xw", "error": "x", "action": {"type": "cancel", "cancels": [{"a": 0, "o": 5}]}}]}
    assert cap.int_cancel_users(b) == {"0xu": 2}


class FakeBudget:
    def __init__(self):
        self.took, self.added = [], []

    def take(self, w):
        self.took.append(w)

    def add(self, w):
        self.added.append(w)


def test_chunk_capture_asks_fills_history_and_open_orders_and_saves_them(tmp_home):
    calls = []

    def call(kind):
        def f(user, *args):
            calls.append((kind, user, args))
            return [{"oid": 1}] * 45 if kind == "userFillsByTime" else []
        return f
    budget = FakeBudget()
    w = cap.Writer(lookup=lambda u, o: {"status": "unknownOid"}, info_gap_s=0, budget=budget,
                   calls={k: call(k) for k in ("userFillsByTime", "historicalOrders", "frontendOpenOrders")})
    trades = [trade(["0xa", "0xm"], t=1011), trade(["0xb", "0xm"], t=1012), trade(["0xc", "0xz"], t=5000)]
    assert cap.ask_open_orders(w, 999, trades=trades, n=1) == ["0xm"]
    extras = {"fill_users": 2, "hist_users": 1, "trades": lambda t0, t1: [t for t in trades if t0 <= t["time"] <= t1]}
    meta = {"kind": "chunk", "snapshot_time": 999}
    assert cap.capture(StubFetcher(), 10, 14, meta, writer=w, state={}, warmup=0, max_anchors=0, extras=extras) == "complete"
    d = tmp_home / "windows" / "10-14"
    fills = json.loads((d / "fills.json").read_text())
    assert [(f["user"], f["args"]) for f in fills] == [("0xm", [10, 6014]), ("0xa", [10, 6014])]
    assert len(fills[0]["answer"]) == 45 and fills[0]["t0"] == 1010
    hist = json.loads((d / "hist_orders.json").read_text())
    assert [(h["user"], h["int_cancels"]) for h in hist] == [("0xu", 5)]
    opn = json.loads((d / "open_orders.json").read_text())
    assert [(o["user"], o["snapshot_time"]) for o in opn] == [("0xm", 999)]
    assert meta["info_userFillsByTime"] == 2 and meta["info_frontendOpenOrders"] == 1 and meta["info_historicalOrders_errors"] == 0
    assert sorted(budget.took) == [20, 20, 20, 20] and budget.added.count(2) == 2   # 45 fills -> +2 weight each


def test_info_errors_are_kept_and_counted(tmp_home):
    def boom(user, *a):
        raise OSError("429")
    w = cap.Writer(lookup=lambda u, o: {}, info_gap_s=0, budget=FakeBudget(), calls={"historicalOrders": boom})
    meta = {"kind": "chunk"}
    cap.capture(StubFetcher(), 10, 11, meta, writer=w, state={}, warmup=0, max_anchors=0, extras={"hist_users": 1})
    assert meta["info_historicalOrders_errors"] == 1


def test_legacy_capture_asks_nothing_extra(tmp_home):
    w = cap.Writer(lookup=lambda u, o: {}, info_gap_s=0, budget=FakeBudget(), calls={})
    meta = {}
    cap.capture(StubFetcher(), 10, 11, meta, writer=w, state={}, warmup=0, max_anchors=0)
    assert not any(k.startswith("info_") for k in meta)
    assert not (tmp_home / "windows" / "10-11" / "fills.json").exists()


def test_budget_waits_when_a_minute_is_full():
    import info
    now = [0.0]
    slept = []
    b = info.Budget(per_min=50, clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    b.take(20); b.take(20)
    b.take(20)                                            # 60 > 50: waits until the first entry is a minute old
    assert slept and now[0] >= 60
    assert info.weight_of("userFillsByTime", [{}] * 45) == 22 and info.weight_of("orderStatus") == 2


def test_canceller_tally_rolls_over_recent_chunks(tmp_path):
    t = cap.CancellerTally(path=str(tmp_path / "c.json"), keep=2)
    t.add({"0xa": 5})
    t.add({"0xb": 3, "0xa": 1})
    assert t.add({"0xb": 4}) == {"0xb": 7, "0xa": 1}                  # the first chunk dropped out
    assert json.loads((tmp_path / "c.json").read_text())["chunks"] == 2


def test_history_skips_users_already_streamed_and_meta_keeps_cancellers(tmp_home):
    calls = []
    w = cap.Writer(lookup=lambda u, o: {}, info_gap_s=0, budget=FakeBudget(),
                   calls={"historicalOrders": lambda u: calls.append(u) or []})
    meta = {"kind": "chunk"}
    cap.capture(StubFetcher(), 10, 12, meta, writer=w, state={}, warmup=0, max_anchors=0,
                extras={"hist_users": 1, "tracked": lambda: ["0xu"]})
    assert calls == [] and meta["int_cancel_users"] == {"0xu": 3}


def test_open_orders_are_prefetched_just_before_the_predicted_snapshot():
    import collect.capture as cap
    clock = {"t": 1000.0}
    slept = []
    def sleep(s):
        slept.append(s); clock["t"] += s
    asked = []
    w = cap.Writer(calls={"frontendOpenOrders": lambda u: (asked.append((u, clock["t"])), [])[1]},
                   budget=cap.info.Budget(per_min=10_000))
    trades = [{"users": ["0x" + "a" * 40, "0x" + "b" * 40], "side": "B", "coin": "BTC", "px": "1", "sz": "1"}]
    got = cap.prefetch_open_orders(w, last_snap=995_000, period_ms=12_000, lead_ms=1500,
                                   now=lambda: clock["t"], sleep=sleep, trades=trades)
    assert slept == [pytest.approx(5.5)]                  # 995 s + 12 s - 1.5 s = 1005.5 s
    assert [u for u, _ in asked] == ["0x" + "b" * 40] and got is not None
    assert w.take_info()["frontendOpenOrders"][0]["prefetch"] is True


def test_hist_end_users_take_contested_first_skip_live_and_stale_files(tmp_path):
    import json as _json
    import collect.capture as cap
    p = tmp_path / "t.json"
    p.write_text(_json.dumps({"time": 1_000_000, "contested_scores": {"0xc1": 2, "0xc2": 1},
                              "cancel_scores": {"0xa1": 9, "0xc1": 5}, "counts": {"0xz": 1}}))
    assert cap.hist_end_users(3, p, exclude=["0xC2"], now=1000.5) == ["0xc1", "0xa1", "0xz"]
    assert cap.hist_end_users(3, p, now=1_000_000 / 1000 + 4000) == []          # older than an hour


def test_hist_is_asked_once_the_last_block_exists():
    import collect.capture as cap
    clock = {"t": 0.0}
    seen = {"h": 99}
    def sleep(s):
        clock["t"] += s
        seen["h"] += 1                                    # one new block per wait
    asked = []
    w = cap.Writer(calls={"historicalOrders": lambda u: (asked.append((u, seen["h"])), [])[1]},
                   budget=cap.info.Budget(per_min=10_000))
    cap.ask_hist_at_end(w, 102, ["0xa", "0xb"], headers=lambda: [(seen["h"], 0)], now=lambda: clock["t"],
                        sleep=sleep)
    assert sorted(u for u, _ in asked) == ["0xa", "0xb"] and min(h for _, h in asked) >= 102
    assert all(r["at_chunk_end"] for r in w.take_info()["historicalOrders"])


def test_int_oid_targets_take_btc_eth_cancels_and_modifies_only():
    import collect.capture as cap
    b = {"txs": [
        {"user": "0xa", "error": None, "action": {"type": "cancel", "cancels": [{"a": 0, "o": 5}, {"a": 7, "o": 6}]}},
        {"user": "0xb", "error": None, "action": {"type": "batchModify", "modifies": [
            {"oid": 9, "order": {"a": 1}}, {"oid": "0xc1", "order": {"a": 1}}]}},
        {"user": "0xc", "error": "x", "action": {"type": "cancel", "cancels": [{"a": 0, "o": 8}]}}]}
    assert cap.int_oid_targets(b) == [("0xa", 5), ("0xb", 9)]


def test_hist_users_put_recent_cancellers_after_the_top_contested_makers(tmp_path):
    import json as _json
    import collect.capture as cap
    p = tmp_path / "t.json"
    p.write_text(_json.dumps({"time": 1_000_000, "contested_scores": {"0xc1": 3, "0xc2": 2, "0xc3": 1, "0xc4": 1},
                              "cancel_scores": {"0xk": 1}}))
    got = cap.hist_end_users(6, p, now=1000.0, recent={"0xr1": 5, "0xr2": 9, "0xc1": 7})
    assert got == ["0xc1", "0xc2", "0xc3", "0xr2", "0xr1", "0xk"]
    assert cap.hist_end_users(2, tmp_path / "missing.json", recent={"0xr": 1}) == ["0xr"]


def test_unpaced_lookups_skip_the_gap():
    import collect.capture as cap
    seen = []
    w = cap.Writer(lookup=lambda u, o: (seen.append(o), {"status": "order"})[1], info_gap_s=100,
                   budget=cap.info.Budget(per_min=10_000))
    for o in range(3):
        w.ask("0xa", o, 1, {"kind": "target"}, paced=False)
    w.drain()
    assert seen == [0, 1, 2] and all(a["kind"] == "target" for a in w.snapshot())


def test_scored_block_cancel_targets_are_looked_up_unpaced_and_skip_covered_users(tmp_home, monkeypatch):
    monkeypatch.setattr(cap, "TARGET_LOOKUPS", 10)

    class F(StubFetcher):
        def get(self, h):
            self.last_age_min = 0.1
            txs = [{"user": "0xu", "error": None, "action": {"type": "cancel", "cancels": [{"a": 0, "o": 100 + h}]}},
                   {"user": "0xcov", "error": None, "action": {"type": "cancel", "cancels": [{"a": 1, "o": 200 + h}]}}]
            b = {"height": h, "blockTime": 1000 + h, "numTxs": 2, "txs": txs}
            return b, json.dumps({"blockDetails": b}).encode()
    asked = []
    w = cap.Writer(lookup=lambda u, o: (asked.append((u, o)), {"status": "unknownOid"})[1], info_gap_s=100,
                   budget=cap.info.Budget(per_min=10_000))
    extras = {"fill_users": 0, "hist_users": 0, "trades": lambda a, b: [], "tracked": lambda: [],
              "covered": lambda: ["0xCOV"]}
    meta = {"kind": "chunk"}
    assert cap.capture(F(), 10, 14, meta, writer=w, state={}, warmup=2, max_anchors=0, extras=extras) == "complete"
    assert asked == [("0xu", 112), ("0xu", 113), ("0xu", 114)]          # scored blocks only, covered user skipped,
    assert meta["target_lookups"] == 3 and meta["target_candidates"] == 6  # and no 100-s gap between them


def test_ioc_users_counts_btc_eth_iocs_including_rejected():
    ioc = {"a": 0, "b": True, "p": "1", "s": "1", "r": False, "t": {"limit": {"tif": "Ioc"}}}
    alo = {**ioc, "t": {"limit": {"tif": "Alo"}}}
    block = {"txs": [
        {"user": "0xAA", "error": None, "action": {"type": "order", "orders": [ioc, ioc, alo]}},
        {"user": "0xbb", "error": "rejected", "action": {"type": "order", "orders": [ioc]}},
        {"user": "0xcc", "action": {"type": "order", "orders": [{**ioc, "a": 5}]}},          # other asset
        {"user": "0xdd", "action": {"type": "batchModify", "modifies": [{"oid": 1, "order": ioc}]}},
    ]}
    assert dict(cap.ioc_users(block)) == {"0xaa": 2, "0xbb": 1, "0xdd": 1}


def test_margin_users_prev_iocs_first_then_recent_takers():
    trades = [{"users": ["0xT1", "0xM1"], "side": "B", "px": "100", "sz": "1"},
              {"users": ["0xM2", "0xT2"], "side": "A", "px": "100", "sz": "5"},
              {"users": ["0xi1", "0xM3"], "side": "B", "px": "100", "sz": "9"}]
    assert cap.margin_users({"0xi1": 3, "0xi2": 7}, trades, 4) == ["0xi2", "0xi1", "0xt2", "0xt1"]


def test_alo_placements_btc_eth_alo_only():
    blk = {"blockTime": 5, "txs": [
        {"user": "0xA", "action": {"type": "order", "orders": [
            {"a": 0, "b": True, "p": "100", "s": "1", "t": {"limit": {"tif": "Alo"}}},
            {"a": 0, "b": True, "p": "100", "s": "1", "t": {"limit": {"tif": "Ioc"}}},
            {"a": 7, "b": True, "p": "100", "s": "1", "t": {"limit": {"tif": "Alo"}}}]}},
        {"user": "0xB", "action": {"type": "modify", "oid": 1, "order":
            {"a": 1, "b": False, "p": "20.5", "s": "1", "t": {"limit": {"tif": "Alo"}}}}}]}
    assert cap.alo_placements(blk) == [("0xa", 0, True, 100.0, 5), ("0xb", 1, False, 20.5, 5)]


def test_alo_touch_users_within_bp_of_last_trade():
    trades = [{"coin": "BTC", "time": 10, "px": "10000"}, {"coin": "BTC", "time": 20, "px": "10100"},
              {"coin": "ETH", "time": 10, "px": "2000"}]
    pl = [("0xa", 0, True, 10000.5, 15),      # 0.5 bp from 10000: touch
          ("0xa", 0, True, 10000.5, 25),      # last trade now 10100: far
          ("0xb", 0, False, 10100.0, 20),     # exactly the last trade
          ("0xc", 1, True, 2000.0, 5),        # before ETH's first trade: not counted
          ("0xd", 1, True, 1999.0, 11)]       # 5 bp away
    assert cap.alo_touch_users(pl, trades, bp=1) == {"0xa": 1, "0xb": 1}


def test_alo_margin_users_half_and_half_ioc_users_first():
    touch = {"0xt1": 9, "0xt2": 5, "0xt3": 1, "0xi1": 20}
    sus = ["0xs1", "0xt1", "0xs2"]
    got = cap.alo_margin_users(touch, sus, 4, exclude=["0xI1"])
    assert got == {"touch": ["0xt1", "0xt2"], "suspect": ["0xs1", "0xs2"]}
    assert cap.alo_margin_users(touch, [], 4, exclude=["0xi1"]) == {"touch": ["0xt1", "0xt2", "0xt3"], "suspect": []}
    assert cap.alo_margin_users({}, sus, 2) == {"touch": [], "suspect": ["0xs1", "0xt1"]}


def test_ask_margin_spot_only_for_ioc_users():
    asked = []

    class W:
        def ask_info_now(self, kind, users, extra=None):
            asked.append((kind, list(users)))
            return 1.0
    assert cap.ask_margin(W(), 0, ["0xi"], ["0xa"]) == 1.0
    assert asked == [("clearinghouseState", ["0xi", "0xa"]), ("spotClearinghouseState", ["0xi"])]
    asked.clear()
    assert cap.ask_margin(W(), 0, [], ["0xa"]) and asked == [("clearinghouseState", ["0xa"])]
    assert cap.ask_margin(W(), 0, [], []) is None


def test_load_suspects(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"time": 1, "users": ["0xAB", "0xcd"]}))
    assert cap.load_suspects(str(p)) == ["0xab", "0xcd"]
    assert cap.load_suspects(str(tmp_path / "none.json")) == []


# ---- 2026-11-23 open-margin capture: resting-order margin + suspect orderStatus lookups

def cs_answer(av, mu, ntl, wd):
    return {"marginSummary": {"accountValue": av, "totalMarginUsed": mu, "totalNtlPos": ntl}, "withdrawable": wd}


def test_order_margin_matches_the_live_probe():
    from decimal import Decimal as D
    # 0x3ff5 on 2026-11-23: 8 resting BTC sells 900008.63 at 13x -> 69231.43; base is 10% of notional
    free, om = cap.order_margin(cs_answer("14966555.8377960008", "5272632.3617690001", "68544220.7029999942",
                                          "8042902.3345210003"))
    assert round(om, 2) == D("69231.43")
    assert free == D("14966555.8377960008") - D("5272632.3617690001") - om
    _, om0 = cap.order_margin(cs_answer("150133.574824", "960.606014", "38424.24058", "146291.150766"))
    assert round(om0, 2) == 0                                        # no orders: gap is the 10% notional rule
    assert cap.order_margin({"error": "x"}) is None and cap.order_margin(None) is None


def test_open_margin_users_keeps_users_with_order_margin_least_free_first():
    recs = [{"user": "0xa", "answer": cs_answer("1000", "100", "1000", "800")},     # om 100, free 800
            {"user": "0xb", "answer": cs_answer("1000", "100", "1000", "500")},     # om 400, free 500
            {"user": "0xc", "answer": cs_answer("1000", "100", "1000", "900")},     # om 0: skipped
            {"user": "0xd", "answer": {"error": "429"}}]
    out = cap.open_margin_users(recs, 5)
    assert [u for u, _, _ in out] == ["0xb", "0xa"] and out[0][2] == "400"
    assert cap.open_margin_users(recs, 1) == [out[0]]


def test_ask_open_margin_later_stores_both_answers_and_drain_waits():
    calls = {"clearinghouseState": lambda u: {"cs": u}, "frontendOpenOrders": lambda u: [{"oid": 1, "u": u}]}
    budget = FakeBudget()
    w = cap.Writer(lookup=lambda u, o: {}, info_gap_s=0, budget=budget, calls=calls)
    slept = []
    cap.ask_open_margin_later(w, [("0xa", "5", "1"), ("0xb", "6", "2")], 777, at_s=100.0,
                              now=lambda: 90.0, sleep=slept.append)
    w.drain()
    recs = sorted(w.take_info()["margin_open"], key=lambda r: r["user"])
    assert slept == [10.0] and budget.took == [22, 22]
    assert recs[0]["answer"] == {"clearinghouseState": {"cs": "0xa"}, "frontendOpenOrders": [{"oid": 1, "u": "0xa"}]}
    assert recs[1]["snapshot_time"] == 777 and recs[1]["plan_free"] == "6" and recs[1]["plan_order_margin"] == "2"
    assert w.snapshot() == []                                        # never an anchor


def test_status_lookups_are_info_records_not_anchors(tmp_path):
    w = cap.Writer(lookup=lambda u, o: {"status": "order", "o": o}, info_gap_s=0, budget=FakeBudget())
    w.ask_status("0xu", "0xc1", {"placed_height": 5})
    w.drain()
    got = w.take_info()["suspect_status"]
    assert got[0]["oid"] == "0xc1" and got[0]["placed_height"] == 5 and got[0]["answer"]["o"] == "0xc1"
    assert w.snapshot() == []
    meta = {}
    cap.save_info(str(tmp_path), {"suspect_status": got}, meta)
    assert os.path.exists(tmp_path / "suspect_status.json") and meta["info_suspect_status"] == 1


def test_suspect_candidates_and_round_robin_pick():
    b = block_of(9, [px_tx("0xS1", 9, [item("0x1"), item("0x2", tif="Gtc"), item("0x3")]),
                     px_tx("0xother", 9, [item("0x4")]),
                     px_tx("0xS2", 9, [item("0x5")])])
    cands = cap.suspect_candidates(b, 9, {"0xs1", "0xs2"})
    assert [c[4] for c in cands] == ["0x1", "0x3", "0x5"] and cands[0][:3] == (9, 0, 0)
    rank = {"0xs2": 0, "0xs1": 1}
    assert [c[4] for c in cap.pick_suspect_lookups(cands, rank, 2)] == ["0x5", "0x3"]
    assert [c[4] for c in cap.pick_suspect_lookups(cands, rank, 9)] == ["0x5", "0x3", "0x1"]
    assert [c[4] for c in cap.pick_suspect_lookups(cands, rank, 9, skip={("0xS1", "0x3")})] == ["0x5", "0x1"]
    import collections
    seen = collections.Counter({("0xs2", "0x5"): 2})
    assert [c[4] for c in cap.pick_suspect_lookups(cands, rank, 9, seen=seen)] == ["0x3", "0x1"]


def test_chunk_capture_asks_suspect_lookups_in_scored_blocks_only_and_keeps_anchors_unchanged(tmp_home, monkeypatch):
    sus = tmp_home / "suspects.json"
    sus.write_text(json.dumps({"users": ["0xu1", "0xu3"]}))
    monkeypatch.setattr(cap, "SUSPECTS_FILE", str(sus))
    asked = []
    w = cap.Writer(lookup=lambda u, k: asked.append((u, k)) or {"status": "order"}, info_gap_s=0)
    base_meta = {"kind": "chunk"}
    cap.capture(PlacementFetcher(), 1000, 1179, base_meta, writer=w, state={}, warmup=150, max_anchors=20)
    d = tmp_home / "windows" / "1000-1179"
    base_anchors = (d / "anchors.json").read_text()
    for f in d.iterdir():
        f.unlink()
    monkeypatch.setattr(cap, "SUSPECT_LOOKUPS", 4)
    asked.clear()
    meta = {"kind": "chunk"}
    assert cap.capture(PlacementFetcher(), 1000, 1179, meta, writer=w, state={}, warmup=150, max_anchors=20) == "complete"
    strip = lambda t: [{k: v for k, v in a.items() if k != "asked_at"} for a in json.loads(t)]
    assert strip((d / "anchors.json").read_text()) == strip(base_anchors)   # the replay's input is unchanged
    st = json.loads((d / "suspect_status.json").read_text())
    assert meta["suspect_lookups"] == 4 == len(st) and meta["suspect_candidates"] == 12   # 30 scored blocks, h%5 in {1,3}
    assert [r["user"] for r in st] == ["0xu1", "0xu3", "0xu1", "0xu3"]
    assert all(r["placed_height"] >= 1150 for r in st)
    assert not {(r["user"], r["oid"]) for r in st} & {(a["user"], a["oid"]) for a in json.loads(base_anchors)}


class IocFetcher(StubFetcher):
    """Every block: one BTC IOC with a cloid, one ETH IOC without a cloid, one ALO, one errored IOC."""

    def get(self, h):
        txs = [px_tx("0xi%d" % (h % 3), h, [item(f"0x{h:04x}i", tif="Ioc")]),
               px_tx("0xn", h, [item(None, a=1, tif="Ioc")]),
               px_tx("0xa", h, [item(f"0x{h:04x}a")]),
               px_tx("0xe", h, [item(f"0x{h:04x}e", tif="Ioc")], error="rejected")]
        b = {"height": h, "blockTime": 5000 + h, "numTxs": len(txs), "txs": txs}
        return b, json.dumps({"blockDetails": b}).encode()


def test_ioc_items_takes_btc_eth_iocs_with_cloid_from_ok_txs():
    b, _ = IocFetcher().get(1200)
    assert cap.ioc_items(b, 1200) == [(1200, 0, 0, "0xi0", "0x04b0i", 6200)]


class FreeBudget:
    def __init__(self):
        self.taken = 0

    def take(self, w):
        self.taken += w


def test_truth_asker_caps_skips_stale_and_writes_its_file(tmp_path):
    asked = []
    now = [10.0]
    bud, tb = FreeBudget(), FreeBudget()
    t = cap.TruthAsker(str(tmp_path / "truth_status.json"), 3, bud,
                       lookup=lambda u, c: asked.append((u, c)) or {"status": "order", "order": {"status": "iocCancelRejected"}},
                       truth_bud=tb, clock=lambda: now[0], stale_s=5)
    t.feed([(1, 0, 0, "0xa", "0xc1", 9_000), (2, 0, 0, "0xb", "0xc2", 1_000), (3, 0, 0, "0xc", "0xc3", 9_500),
            (4, 0, 0, "0xd", "0xc4", 9_900)])                     # the 4th is over the cap
    t.close(); t.join(5)
    out = json.loads((tmp_path / "truth_status.json").read_text())
    assert asked == [("0xa", "0xc1"), ("0xc", "0xc3")]          # 0xc2 was 9 s old at asking time: skipped
    assert out["queued"] == 3 and out["counts"] == {"over_cap": 1, "skipped_stale": 1, "status:iocCancelRejected": 2}
    assert [r["placed_height"] for r in out["rows"]] == [1, 3]
    assert bud.taken == 4 and tb.taken == 4                      # weight 2 per call, booked in both budgets


def test_chunk_capture_truth_ioc_asks_scored_iocs_and_keeps_anchors_unchanged(tmp_home, monkeypatch):
    calls = []
    w = cap.Writer(lookup=lambda u, k: {"status": "order"}, info_gap_s=0)
    cap.capture(IocFetcher(), 1000, 1179, {"kind": "chunk"}, writer=w, state={}, warmup=150, max_anchors=20)
    d = tmp_home / "windows" / "1000-1179"
    base = (d / "anchors.json").read_text()
    for f in d.iterdir():
        f.unlink()
    monkeypatch.setattr(cap, "TRUTH_IOC", 10)
    monkeypatch.setattr(cap, "order_status", lambda u, c: calls.append((u, c)) or {"status": "unknownOid"})
    monkeypatch.setattr(cap, "_TRUTH_BUDGET", cap.info.Budget(per_min=10_000))
    made = []
    orig = cap.TruthAsker

    class Spy(orig):
        def __init__(self, *a, **k):
            k["stale_s"] = 1e12
            super().__init__(*a, **k)
            made.append(self)
    monkeypatch.setattr(cap, "TruthAsker", Spy)
    meta = {"kind": "chunk"}
    assert cap.capture(IocFetcher(), 1000, 1179, meta, writer=w, state={}, warmup=150, max_anchors=20) == "complete"
    made[0].join(10)
    strip = lambda t: [{k: v for k, v in a.items() if k != "asked_at"} for a in json.loads(t)]
    assert strip((d / "anchors.json").read_text()) == strip(base)
    out = json.loads((d / "truth_status.json").read_text())
    assert meta["truth_ioc_queued"] == 10 == out["queued"] and len(calls) == 10
    assert all(r["placed_height"] >= 1150 for r in out["rows"])  # scored blocks only
    assert out["counts"]["over_cap"] == 20 and out["counts"]["status:unknownOid"] == 10


def test_truth_asker_skips_users_covered_by_history(tmp_path):
    asked = []
    t = cap.TruthAsker(str(tmp_path / "t.json"), 5, FreeBudget(), lookup=lambda u, c: asked.append(u) or {"status": "unknownOid"},
                       truth_bud=FreeBudget(), clock=lambda: 0.0, stale_s=1e9, skip=["0xAA"])
    t.feed([(1, 0, 0, "0xaa", "0xc1", 0), (1, 1, 0, "0xbb", "0xc2", 0)])
    t.close(); t.join(5)
    assert asked == ["0xbb"] and json.loads((tmp_path / "t.json").read_text())["counts"]["skipped_hist_user"] == 1


def test_truth_asker_skips_a_user_after_five_unknowns_and_reprobes(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "UNKNOWN_STREAK", cap.collections.Counter())
    asked = []
    t = cap.TruthAsker(str(tmp_path / "t.json"), 100, FreeBudget(), lookup=lambda u, c: asked.append(c) or {"status": "unknownOid"},
                       truth_bud=FreeBudget(), clock=lambda: 0.0, stale_s=1e9)
    t.feed([(1, k, 0, "0xbusy", f"0x{k}", 0) for k in range(40)])
    t.close(); t.join(5)
    assert asked == ["0x0", "0x1", "0x2", "0x3", "0x4", "0x29"]   # 5 asked, then 1 probe after 25 skips
    assert json.loads((tmp_path / "t.json").read_text())["counts"]["skipped_unanswerable_user"] == 34


def test_open_orders_file_name_is_configurable():
    assert cap.INFO_FILES["frontendOpenOrders"] == cap.OPEN_ORDERS_FILE


class FakeWriter:
    def __init__(self, answers):
        import collections as _c, threading as _t
        self.info, self.lock, self.pending = _c.defaultdict(list), _t.Lock(), []
        self.budget = FreeBudget()
        self.asked = []
        self.calls = {"frontendOpenOrders": lambda u: self.asked.append(u) or answers[u]}


def oo(n, coin="BTC"):
    return [{"coin": coin if i % 2 == 0 else "SOL", "side": "B", "limitPx": "1", "sz": "1", "oid": i, "timestamp": i,
             "tif": "Alo", "reduceOnly": False} for i in range(n)]


def test_ask_open_book_probes_then_picks_big_books_compactly():
    w = FakeWriter({"0xa": oo(1200), "0xb": oo(10), "0xc": oo(3000), "0xd": oo(5)})
    cache = {"0xc": (2900, 0.0)}
    th = cap.ask_open_book(w, ["0xA", "0xb", "0xc", "0xd"], 123, n_pick=2, n_probe=2, min_count=1000,
                           now=lambda: 100.0, cache=cache)
    th.join(5)
    assert w.asked == ["0xa", "0xb", "0xc"]                    # 2 probes (a, b), then pick c (a was just probed)
    recs = w.info["open_book_users"]
    assert [(r["user"], r["role"], r["total_open"]) for r in recs] == [("0xa", "probe", 1200), ("0xb", "probe", 10),
                                                                     ("0xc", "pick", 3000)]
    assert len(recs[0]["btc_eth"]) == 600 and recs[0]["btc_eth"][0][0] == "BTC"   # SOL rows dropped
    assert cache["0xa"][0] == 1200 and cache["0xc"][0] == 3000


def test_open_book_records_save_through_save_info(tmp_path):
    w = FakeWriter({"0xa": oo(4)})
    cap.ask_open_book(w, ["0xa"], 1, n_pick=1, n_probe=1, min_count=1, now=lambda: 0.0, cache={}).join(5)
    meta = {}
    cap.save_info(str(tmp_path), dict(w.info), meta)
    assert (tmp_path / "open_book_users.json").exists() and meta["info_open_book_users"] == 1
