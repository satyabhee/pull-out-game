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


def test_chunk_capture_writes_blocks_meta_and_counts(tmp_home):
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
