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
