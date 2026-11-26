import io
import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collect"))
import backoff  # noqa: E402


def _e429():
    return urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b""))


def test_delay_is_exponential_jittered_and_capped():
    assert backoff.delay(0, rng=lambda: 0.0) == 0.5 and backoff.delay(0, rng=lambda: 1.0) == 1.0
    assert backoff.delay(3, rng=lambda: 0.0) == 4.0 and backoff.delay(3, rng=lambda: 1.0) == 8.0
    assert backoff.delay(20, rng=lambda: 1.0) == 60.0 and backoff.delay(20, rng=lambda: 0.0) == 30.0


def test_retry_429_retries_counts_per_hour_and_returns(tmp_path):
    p = str(tmp_path / "c.json")
    calls, sleeps = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise _e429()
        return {"ok": 1}
    assert backoff.retry_429(fn, "info:x", sleep=sleeps.append, path=p) == {"ok": 1}
    assert len(calls) == 3 and len(sleeps) == 2 and all(0.5 <= s <= 2.0 for s in sleeps)
    d = json.load(open(p))
    assert list(d.values()) == [{"info:x": 2}]


def test_retry_429_gives_up_and_passes_other_errors(tmp_path):
    p = str(tmp_path / "c.json")
    with pytest.raises(urllib.error.HTTPError):
        backoff.retry_429(lambda: (_ for _ in ()).throw(_e429()), "s", tries=3, sleep=lambda s: None, path=p)
    assert list(json.load(open(p)).values()) == [{"s": 3}]
    n = []

    def other():
        n.append(1)
        raise urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b""))
    with pytest.raises(urllib.error.HTTPError):
        backoff.retry_429(other, "s", sleep=lambda s: None, path=p)
    assert len(n) == 1
