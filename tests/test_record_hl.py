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


def test_maker_tally_picks_top_maker_notional_and_resets():
    t = record_hl.MakerTally()
    t.feed([{"side": "B", "px": "100", "sz": "2", "users": ["0xT", "0xM"]},      # taker bought: seller made
            {"side": "A", "px": "100", "sz": "1", "users": ["0xN", "0xT"]},
            {"side": "A", "px": "100", "sz": "1", "users": ["0xA", "0xT"]}, {"bad": 1}])
    assert t.pick(2) == ["0xm", "0xa"] and t.pick(2) == []


def test_user_and_sigfig_subscriptions():
    assert record_hl.user_subscriptions("0xu") == [{"type": "orderUpdates", "user": "0xu"},
                                                   {"type": "userFills", "user": "0xu"}]
    assert record_hl.sigfig_subscriptions(["BTC"], "4") == [{"type": "l2Book", "coin": "BTC", "nSigFigs": 4}]


def test_route_user_tags_the_user_and_trades_feed_the_tally():
    orders, fills, other = FakeSink(), FakeSink(), FakeSink()
    record_hl.route_user({"channel": "orderUpdates", "data": [{"order": {"oid": 1}, "status": "open"}]}, "0xu",
                         orders, fills, other)
    record_hl.route_user({"channel": "userFills", "data": {"user": "0xu", "fills": []}}, "0xu", orders, fills, other)
    record_hl.route_user({"channel": "subscriptionResponse"}, "0xu", orders, fills, other)
    assert orders.got == [{"user": "0xu", "data": [{"order": {"oid": 1}, "status": "open"}]}]
    assert fills.got[0]["user"] == "0xu" and other.got == []
    tally = record_hl.MakerTally()
    record_hl.route_api({"channel": "trades", "data": [{"coin": "BTC", "side": "B", "px": "1", "sz": "1",
                                                        "users": ["0xa", "0xb"]}]},
                        {"BTC": FakeSink()}, {}, {}, FakeSink(), tally)
    assert tally.pick(1) == ["0xb"]


def test_track_users_swaps_connections_and_logs_each_set():
    import asyncio

    class Task:
        def __init__(self, u):
            self.u, self.cancelled = u, False

        def cancel(self):
            self.cancelled = True

    started, tracked = [], FakeSink()
    tally = record_hl.MakerTally()
    picks = [["0xb", "0xc"]]
    tally.pick = lambda n: picks.pop(0) if picks else []
    rounds = []

    async def sleep(s):
        rounds.append(s)
        if len(rounds) >= 3:
            raise asyncio.CancelledError

    def start(u):
        t = Task(u)
        started.append(t)
        return t
    try:
        asyncio.run(record_hl.track_users(tally, FakeSink(), FakeSink(), tracked, FakeSink(), n=2, every_s=5,
                                          initial=["0xa", "0xb"], start=start, sleep=sleep))
    except asyncio.CancelledError:
        pass
    assert [t.u for t in started] == ["0xa", "0xb", "0xc"]
    assert started[0].cancelled and not started[1].cancelled
    assert [m["users"] for m in tracked.got] == [["0xa", "0xb"], ["0xb", "0xc"], ["0xb", "0xc"]]


def test_pick_users_prefers_fresh_cancellers_and_falls_back_to_makers(tmp_path):
    import json
    t = record_hl.MakerTally()
    t.feed([{"side": "B", "px": "1", "sz": "1", "users": ["0xt", "0xmaker"]}])
    p = tmp_path / "cancellers.json"
    p.write_text(json.dumps({"time": 1_000_000, "counts": {"0xc1": 5, "0xc2": 9, "0xc3": 9}}))
    assert record_hl.pick_users(t, 2, by="cancels", path=p, now=1000) == ["0xc2", "0xc3"]
    t.feed([{"side": "B", "px": "1", "sz": "1", "users": ["0xt", "0xmaker"]}])
    assert record_hl.pick_users(t, 2, by="cancels", path=p, now=1000 + 7200) == ["0xmaker"]    # stale file
    assert record_hl.pick_users(t, 2, by="cancels", path=tmp_path / "none.json") == []
    t.feed([{"side": "B", "px": "1", "sz": "1", "users": ["0xt", "0xmaker"]}])
    assert record_hl.pick_users(t, 2, by="makers", path=p, now=1000) == ["0xmaker"]


def test_randomized_tracking_picks_an_arm_and_its_list(tmp_path, monkeypatch):
    import json as _json
    import record_hl as rh
    p = tmp_path / "t.json"
    p.write_text(_json.dumps({"time": 0, "counts": {"0xm": 3, "0xc": 2}, "cancel_scores": {"0xc": 9, "0xz": 1}}))
    monkeypatch.setattr(rh, "TRACK_ARMS", ["contested", "cancels"])
    seen = {}
    for minute in range(40):
        users = rh.pick_users(rh.MakerTally(), 1, by="cancels", path=p, now=minute * 60)
        seen[rh.LAST_ARM["arm"]] = users
    assert seen == {"contested": ["0xm"], "cancels": ["0xc"]}


def test_users_past_the_dedicated_ones_share_one_connection(monkeypatch):
    import asyncio
    monkeypatch.setattr(record_hl, "TRACK_DEDICATED", 2)

    class Task:
        def __init__(self, u):
            self.u, self.cancelled = u, False

        def cancel(self):
            self.cancelled = True

    started, shared, tracked = [], [], FakeSink()
    rounds = []

    async def sleep(s):
        rounds.append(s)
        if len(rounds) >= 1:
            raise asyncio.CancelledError
    try:
        asyncio.run(record_hl.track_users(record_hl.MakerTally(), FakeSink(), FakeSink(), tracked, FakeSink(), n=4,
                                          initial=["0xa", "0xb", "0xc", "0xd"], start=lambda u: started.append(u) or Task(u),
                                          start_shared=lambda us: shared.append(us) or Task(us), sleep=sleep))
    except asyncio.CancelledError:
        pass
    assert started == ["0xa", "0xb"] and shared == [("0xc", "0xd")]
    assert tracked.got[-1]["users"] == ["0xa", "0xb"] and tracked.got[-1]["shared"] == ["0xc", "0xd"]
    orders, fills = FakeSink(), FakeSink()
    record_hl.route_shared({"channel": "orderUpdates", "data": [{"order": {}}]}, ("0xc", "0xd"), orders, fills, FakeSink())
    record_hl.route_shared({"channel": "userFills", "data": {"user": "0xd", "fills": []}}, ("0xc", "0xd"), orders, fills,
                           FakeSink())
    assert orders.got[0]["user"] == "" and orders.got[0]["shared"] == ["0xc", "0xd"]
    assert fills.got[0]["user"] == "0xd"


def test_fast_subscriptions_route_to_fast_sinks():
    import collect.record_hl as r
    subs = r.fast_subscriptions(["BTC", "ETH"])
    assert subs == [{"type": "l2Book", "coin": "BTC", "fast": True}, {"type": "l2Book", "coin": "ETH", "fast": True}]

    class S(list):
        def write(self, m):
            self.append(m)
    fast, other = {"BTC": S()}, S()
    m = {"channel": "l2Book", "data": {"coin": "BTC", "time": 1, "levels": [[{"px": "1", "sz": "1", "n": 1}], []]}}
    r.route_api(m, {}, fast, {}, other)
    assert fast["BTC"] == [m] and other == []
