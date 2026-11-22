"""info.py: Hyperliquid info-API calls used by capture.py, with a per-IP weight budget.

Weights (docs, Nov 2026): orderStatus 2; userFillsByTime, historicalOrders, frontendOpenOrders 20, and
userFillsByTime / historicalOrders add 1 per 20 items returned. The IP limit is 1200 weight per minute;
INFO_WEIGHT_PER_MIN (default 600) keeps these lookups to half of it so nothing else on the IP is starved.

Every answer is stored raw by the caller; nothing here interprets it.
"""
import collections
import json
import os
import threading
import time
import urllib.request

URL = "https://api.hyperliquid.xyz/info"
WEIGHT_PER_MIN = int(os.environ.get("INFO_WEIGHT_PER_MIN", 600))
BASE_WEIGHT = {"orderStatus": 2, "clearinghouseState": 2, "spotClearinghouseState": 2, "userFillsByTime": 20, "historicalOrders": 20, "frontendOpenOrders": 20}
PER_20_ITEMS = {"userFillsByTime", "historicalOrders"}


def post(body, timeout=30):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


EXPECTED = {"historicalOrders": 120}         # 2000 orders returned: 20 + 2000 / 20; booked BEFORE the call


def expected_weight(kind):
    """Weight to reserve before a call: the most it usually costs, so parallel calls queue instead of bursting."""
    return EXPECTED.get(kind, weight_of(kind))


def weight_of(kind, answer=None):
    w = BASE_WEIGHT.get(kind, 20)
    if kind in PER_20_ITEMS and isinstance(answer, list):
        w += len(answer) // 20
    return w


class Budget:
    """Sliding one-minute weight budget. take(w) blocks until w fits; add(w) books extra weight learned
    from the answer (items returned). clock/sleep are injectable for tests."""

    def __init__(self, per_min=None, clock=time.time, sleep=time.sleep):
        self.per_min = WEIGHT_PER_MIN if per_min is None else per_min
        self.clock, self.sleep = clock, sleep
        self.used = collections.deque()                 # (time, weight)
        self.lock = threading.Lock()

    def _load(self, now):
        while self.used and self.used[0][0] <= now - 60:
            self.used.popleft()
        return sum(w for _, w in self.used)

    def take(self, w):
        while True:
            with self.lock:
                now = self.clock()
                load = self._load(now)
                if load + w <= self.per_min or not self.used:
                    self.used.append((now, w))
                    return
                wait = self.used[0][0] + 60 - now
            self.sleep(max(wait, 0.05))

    def add(self, w):
        if w > 0:
            with self.lock:
                self.used.append((self.clock(), w))


def user_fills_by_time(user, t0_ms, t1_ms):
    return post({"type": "userFillsByTime", "user": user, "startTime": int(t0_ms), "endTime": int(t1_ms),
                 "aggregateByTime": False})


def historical_orders(user):
    return post({"type": "historicalOrders", "user": user})


def frontend_open_orders(user):
    return post({"type": "frontendOpenOrders", "user": user})


def clearinghouse_state(user):
    return post({"type": "clearinghouseState", "user": user})


def spot_clearinghouse_state(user):
    return post({"type": "spotClearinghouseState", "user": user})


CALLS = {"userFillsByTime": user_fills_by_time, "historicalOrders": historical_orders,
         "frontendOpenOrders": frontend_open_orders, "clearinghouseState": clearinghouse_state,
         "spotClearinghouseState": spot_clearinghouse_state}
