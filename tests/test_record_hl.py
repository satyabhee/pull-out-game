"""collect/record_hl.py: the bbo feed is subscribed and routed to its own per-coin file."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collect"))
import record_hl  # noqa: E402


class FakeSink:
    def __init__(self):
        self.got = []

    def write(self, m):
        self.got.append(m)


def test_subscribes_to_bbo_per_coin():
    subs = record_hl.api_subscriptions(["BTC", "ETH"])
    assert {"type": "bbo", "coin": "BTC"} in subs and {"type": "bbo", "coin": "ETH"} in subs
    assert len(subs) == 6


def test_routes_each_channel_to_its_sink():
    trades, books, bbos = ({"BTC": FakeSink()} for _ in range(3))
    other = FakeSink()
    msgs = [{"channel": "bbo", "data": {"coin": "BTC", "time": 1, "bbo": [None, None]}},
            {"channel": "trades", "data": [{"coin": "BTC"}]},
            {"channel": "l2Book", "data": {"coin": "BTC"}},
            {"channel": "bbo", "data": {"coin": "SOL"}},
            {"channel": "subscriptionResponse", "data": {}}]
    for m in msgs:
        record_hl.route_api(m, trades, books, bbos, other)
    assert [len(x["BTC"].got) for x in (trades, books, bbos)] == [1, 1, 1]
    assert other.got == [msgs[3]]
