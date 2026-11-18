"""collect/archive_probe.py: plan within a request budget, first + immediate repeat per block, stop at the
cap, one log row per request. No network: the fetcher is a stub."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collect"))
import archive_probe as ap  # noqa: E402
from fetch_blocks import ArchivedCap  # noqa: E402

HEAD = (1_000_000, 1_800_000_000_000)          # (height, blockTime ms) of the newest recorder header


def test_plan_is_deterministic_young_first_and_at_the_right_ages():
    p1 = ap.plan(HEAD, buckets_min=[2, 10], per_bucket=2, control=1, seed=7)
    assert p1 == ap.plan(HEAD, buckets_min=[2, 10], per_bucket=2, control=1, seed=7)
    assert [x["bucket_min"] for x in p1] == [0, 2, 2, 10, 10]                 # control (fresh) first
    for x in p1:
        target = HEAD[0] - round(x["bucket_min"] * 60 * ap.BPS)
        assert abs(x["height"] - target) <= ap.JITTER_BLOCKS
    assert len({x["height"] for x in p1}) == len(p1)


def test_plan_refuses_to_exceed_the_budget():
    with pytest.raises(ValueError, match="budget"):
        ap.plan(HEAD, buckets_min=[2, 5, 10, 20, 40, 80], per_bucket=5, control=3, seed=1, max_requests=45)


class Stub:
    def __init__(self, cap_after=None, slow_first=()):
        self.calls, self.cap_after, self.slow_first, self.seen = [], cap_after, set(slow_first), set()

    def get(self, h, tries=1):
        assert tries == 1                                   # never hidden retries
        self.calls.append(h)
        if self.cap_after is not None and len(self.calls) > self.cap_after:
            raise ArchivedCap("More than 100 archived blocks queried in one day")
        self.seen.add(h)
        return {"height": h}, b"{}"

    def age_min(self, h):
        return (HEAD[0] - h) / ap.BPS / 60


def test_run_requests_each_block_twice_and_logs_every_request(tmp_path):
    plan = ap.plan(HEAD, buckets_min=[5], per_bucket=2, control=1, seed=3)
    out = tmp_path / "probe.jsonl"
    stub = Stub()
    summary = ap.run(stub, plan, out, clock=iter(range(0, 10_000, 7)).__next__)
    rows = [json.loads(x) for x in open(out)]
    assert stub.calls == [h for x in plan for h in (x["height"], x["height"])]
    assert [r["attempt"] for r in rows] == ["first", "repeat"] * 3
    assert all(r["outcome"] == "ok" and r["ms"] == 7 for r in rows)
    assert summary == {"requests": 6, "cap_hit": False}


def test_run_stops_at_the_cap_and_records_where(tmp_path):
    plan = ap.plan(HEAD, buckets_min=[5, 20], per_bucket=2, control=0, seed=3)
    out = tmp_path / "probe.jsonl"
    summary = ap.run(Stub(cap_after=3), plan, out, clock=iter(range(0, 10_000, 5)).__next__)
    rows = [json.loads(x) for x in open(out)]
    assert len(rows) == 4 and rows[-1]["outcome"] == "archived_cap" and rows[-1]["attempt"] == "repeat"
    assert summary == {"requests": 4, "cap_hit": True, "cap_at": {"bucket_min": 5, "attempt": "repeat",
                                                                    "request_no": 4}}


def test_report_compares_first_and_repeat_latency_per_bucket():
    rows = [{"bucket_min": b, "attempt": a, "ms": ms, "outcome": "ok"}
            for b, a, ms in [(0, "first", 300), (0, "repeat", 290), (20, "first", 2400), (20, "repeat", 310),
                             (20, "first", 2600), (20, "repeat", 330)]]
    text = ap.report(rows)
    assert "20 min" in text and "first median 2500 ms" in text and "repeat median 320 ms" in text
