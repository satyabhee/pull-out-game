"""The explorer guard in collect/fetch_blocks.py: never request a possibly-archived block (fail closed),
and only one explorer client per machine. No network: Fetcher._raw is replaced by a stub."""
import json, os, sys, time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collect"))
import fetch_blocks as fb  # noqa: E402

NOW_MS = 1_800_000_000_000


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(fb.time, "time", lambda: NOW_MS / 1000)
    monkeypatch.setattr(fb.time, "sleep", lambda s: None)


def block(h, t_ms):
    return json.dumps({"type": "blockDetails", "blockDetails": {"height": h, "blockTime": t_ms, "numTxs": 0, "txs": []}}).encode()


def stub(fetcher, sent):
    def raw(h):
        sent.append(h)
        return 200, block(h, NOW_MS - 1000)
    fetcher._raw = raw


def head_at(height, age_min):
    return lambda: (height, NOW_MS - int(age_min * 60_000))


def test_no_reference_refuses_without_sending():
    f, sent = fb.Fetcher(lock=None), []
    stub(f, sent)
    with pytest.raises(fb.TooOld, match="age unknown"):
        f.get(1000)
    assert sent == []


def test_head_returning_none_refuses():
    f, sent = fb.Fetcher(head=lambda: None, lock=None), []
    stub(f, sent)
    with pytest.raises(fb.TooOld):
        f.get(1000)
    assert sent == []


def test_fresh_block_is_fetched():
    f, sent = fb.Fetcher(head=head_at(1_000_000, 0), lock=None), []
    stub(f, sent)
    f.get(1_000_000 - int(10 * 60 * fb.BPS))          # ~10 min old
    assert len(sent) == 1


def test_block_over_max_age_refused():
    f, sent = fb.Fetcher(head=head_at(1_000_000, 0), lock=None), []
    stub(f, sent)
    with pytest.raises(fb.TooOld, match="min old"):
        f.get(1_000_000 - int(121 * 60 * fb.BPS))
    assert sent == []


def test_stale_head_counts_its_own_age():
    # recorder stopped 2 h ago: a block 30 min before that header is ~150 min old
    f, sent = fb.Fetcher(head=head_at(1_000_000, 120), lock=None), []
    stub(f, sent)
    with pytest.raises(fb.TooOld):
        f.get(1_000_000 - int(30 * 60 * fb.BPS))
    assert sent == []


def test_far_reference_is_not_trusted():
    # a reference > MAX_REF_GAP_MIN away in blocks is too imprecise (BPS drift): treat age as unknown
    f, sent = fb.Fetcher(head=head_at(10_000_000, 0), lock=None), []
    stub(f, sent)
    with pytest.raises(fb.TooOld, match="age unknown"):
        f.get(10_000_000 - int((fb.MAX_REF_GAP_MIN + 1) * 60 * fb.BPS))


def test_fetched_block_becomes_reference():
    # the last fetched block (nearest height) dates the next one, even with no recorder head
    f, sent = fb.Fetcher(head=None, lock=None, max_age_min=120), []
    f.ref = (500, NOW_MS - 119 * 60_000)
    stub(f, sent)
    f.get(501)
    assert sent == [501] and f.ref == (501, NOW_MS - 1000)


def test_guard_runs_before_every_retry(monkeypatch):
    f, sent = fb.Fetcher(head=head_at(1_000_000, 0), lock=None), []
    ages = iter([0, 200])                             # fresh on try 1, "old" by try 2
    monkeypatch.setattr(f, "age_min", lambda h: next(ages))

    def raw(h):
        sent.append(h)
        return 429, b""
    f._raw = raw
    with pytest.raises(fb.TooOld):
        f.get(1)
    assert sent == [1]


def test_one_client_per_machine(tmp_path):
    lock = str(tmp_path / "explorer.lock")
    first = fb.Fetcher(lock=lock)
    with pytest.raises(fb.Busy):
        fb.Fetcher(lock=lock)
    first._lock.close()
    fb.Fetcher(lock=lock)                             # free again once the first client exits


def test_recorder_head_reads_newest_header(tmp_path):
    d = tmp_path / "2026-11-17" / "05"
    d.mkdir(parents=True)
    rows = [{"recv_ms": 1, "msg": {"height": h, "blockTime": 10 * h}} for h in (7, 8, 9)]
    (d / "blocks.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert fb.recorder_head(str(tmp_path)) == (9, 90)
    assert fb.recorder_head(str(tmp_path / "missing")) is None


def test_default_limit_is_30_min_and_the_request_age_is_recorded():
    f, sent = fb.Fetcher(head=head_at(1_000_000, 0), lock=None), []
    stub(f, sent)
    with pytest.raises(fb.TooOld):
        f.get(1_000_000 - int(31 * 60 * fb.BPS))
    f.get(1_000_000 - int(5 * 60 * fb.BPS))
    assert fb.MAX_AGE_MIN == 30 and 4.9 < f.last_age_min < 5.1


# ---- per-request log: response time, block age, outcome (no extra requests) ----------------------

def read_log(p):
    return [json.loads(x) for x in open(p)]


def test_every_request_is_logged_with_age_latency_and_outcome(tmp_path):
    log = tmp_path / "requests.jsonl"
    f, sent = fb.Fetcher(lock=None, head=head_at(1000, 0), request_log=str(log)), []
    stub(f, sent)
    f.get(1000 - 145)                                       # ~10 s old
    (r,) = read_log(log)
    assert r["height"] == 855 and r["status"] == 200 and r["outcome"] == "ok"
    assert r["age_s"] == pytest.approx(10.0, abs=0.1) and r["ms"] >= 0 and r["t"] == NOW_MS and r["bytes"] > 0


def test_refused_before_sending_is_not_logged_as_a_request(tmp_path):
    log = tmp_path / "requests.jsonl"
    f, sent = fb.Fetcher(lock=None, head=head_at(1000, 0), max_age_min=1, request_log=str(log)), []
    stub(f, sent)
    with pytest.raises(fb.TooOld):
        f.get(1000 - 14_500)                                # ~17 min old
    assert sent == [] and not log.exists()


def test_cap_and_429_responses_are_logged(tmp_path):
    log = tmp_path / "requests.jsonl"
    f = fb.Fetcher(lock=None, head=head_at(1000, 0), request_log=str(log))
    replies = iter([(429, b"rate"), (200, json.dumps({"type": "error", "message":
                   "More than 100 archived blocks queried in one day"}).encode())])
    f._raw = lambda h: next(replies)
    with pytest.raises(fb.ArchivedCap):
        f.get(990)
    assert [(r["status"], r["outcome"]) for r in read_log(log)] == [(429, "http_429"), (200, "archived_cap")]
