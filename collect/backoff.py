"""HTTP 429 back-off shared by the capture's explorer fetcher and info lookups (Satya, NS-14, Nov 26).

delay(attempt): exponential back-off with "equal" jitter: d = min(CAP_S, BASE_S * 2**attempt), sleep d/2 + uniform(0, d/2),
so parallel workers throttled together don't retry together, every retry waits at least d/2, and the cap is 60 s.
count_429(source): adds one to http429_hourly.json next to the capture ({"YYYY-MM-DDTHH": {source: n}}), the per-hour
429 count. Thread-safe; a failed write never breaks a lookup.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error

BASE_S, CAP_S = 1.0, 60.0
COUNTS = os.environ.get("HTTP429_COUNTS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "http429_hourly.json")
_lock = threading.Lock()


def delay(attempt: int, rng=random.random) -> float:
    d = min(CAP_S, BASE_S * 2 ** attempt)
    return d / 2 + rng() * d / 2


def count_429(source: str, path: str | None = None, now=time.time) -> None:
    p = path or COUNTS
    hour = time.strftime("%Y-%m-%dT%H", time.gmtime(now()))
    with _lock:
        try:
            d = json.load(open(p)) if os.path.exists(p) else {}
        except ValueError:
            d = {}
        d.setdefault(hour, {})
        d[hour][source] = d[hour].get(source, 0) + 1
        try:
            tmp = p + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(d, fh, indent=0, sort_keys=True)
            os.replace(tmp, p)
        except OSError:
            pass


def retry_429(fn, source: str, tries: int = 6, sleep=time.sleep, path: str | None = None):
    """Call fn(); on urllib HTTPError 429 count it, sleep delay(attempt) and retry (at most `tries` calls).
    Any other error, or the last 429, is raised as before."""
    for attempt in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == tries - 1:
                if e.code == 429:
                    count_429(source, path)
                raise
            count_429(source, path)
            sleep(delay(attempt))
