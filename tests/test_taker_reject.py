"""taker_reject rules (Replay._taker_skip / _taker_learn) on a bare Replay, no window needed."""
from collections import Counter

from engine_py.replay import Replay
from engine_py.types import ASSETS, Place, Side, Tif


def bare(rules, real=None, margin=None):
    r = Replay.__new__(Replay)
    r._margin = margin or {}
    r.taker_reject = frozenset(rules)
    r.taker_stats, r._taker_blocked, r._blk_ioc = Counter(), set(), {}
    r._real_trades = real or {}
    return r


def ioc(user, px, sz, h="0xa", side=Side.BUY, reduce_only=False):
    a = ASSETS[0]
    return Place(user=user, asset=0, block=1, tx_index=0, item_index=0, hash=h, priority=None,
                 side=side, px=a.px_to_ticks(px), sz=a.sz_to_lots(sz), tif=Tif.IOC, cloid=None,
                 reduce_only=reduce_only)


def test_min10():
    r = bare({"min10"})
    assert r._taker_skip(ioc("u", "60000", "0.00016")) == "skip_min10"     # 9.6 USD
    assert r._taker_skip(ioc("u", "60000", "0.00017")) is None              # 10.2 USD


def test_learn_blocks_until_real_fill():
    r = bare({"learn"}, real={"0xb": ["trade"]})
    r._blk_ioc = {"0xa": ("u", True)}           # engine filled, no real trade -> block
    r._taker_learn()
    assert r._taker_skip(ioc("u", "60000", "1")) == "skip_learn"
    assert r._taker_skip(ioc("v", "60000", "1")) is None
    r._blk_ioc = {"0xb": ("u", True)}           # a later tx of u really filled -> unblock
    r._taker_learn()
    assert r._taker_skip(ioc("u", "60000", "1")) is None


def test_margin_gate():
    coin = ASSETS[0].name                       # BTC, max leverage 40
    r = bare({"margin"}, margin={"broke": (-5.0, {coin: 0.5}), "rich": (1000.0, {}), "thin": (10.0, {})})
    assert r._taker_skip(ioc("broke", "60000", "0.01")) == "skip_margin"          # grows a long
    assert r._taker_skip(ioc("broke", "60000", "0.3", side=Side.SELL)) is None    # reduces it
    assert r._taker_skip(ioc("broke", "60000", "0.7", side=Side.SELL)) == "skip_margin"   # flips: 0.2 new short
    assert r._taker_skip(ioc("broke", "60000", "1", reduce_only=True)) is None
    assert r._taker_skip(ioc("rich", "60000", "0.5")) is None                     # needs 750 of 1000
    assert r._taker_skip(ioc("thin", "60000", "0.01")) == "skip_margin"           # needs 600/40 = 15 > 10
    assert r._taker_skip(ioc("thin", "60000", "0.005")) is None                   # needs 7.5
    assert r._taker_skip(ioc("nobody", "60000", "1")) is None and r.taker_stats["margin_unknown"] == 1


def test_load_margin(tmp_path):
    import json
    from engine_py.replay import load_margin
    st = {"marginSummary": {"accountValue": "25", "totalMarginUsed": "32"},
          "assetPositions": [{"position": {"coin": "BTC", "szi": "-0.02"}}]}
    json.dump([{"user": "0xAB", "answer": st}, {"user": "0xcd", "answer": {"error": "x"}}],
              open(tmp_path / "clearinghouseState.json", "w"))
    json.dump([{"user": "0xab", "answer": {"tokenToAvailableAfterMaintenance": [[0, "18.5"]]}}],
              open(tmp_path / "spotClearinghouseState.json", "w"))
    assert load_margin(tmp_path) == {"0xab": (-7.0, {"BTC": -0.02})}
    assert load_margin(tmp_path, spot=True) == {"0xab": (11.5, {"BTC": -0.02})}
    assert load_margin(tmp_path / "missing") == {}


def test_seeded_user_blocked_until_real_fill():
    r = bare({"learn"}, real={"0xb": ["trade"]})
    r._taker_blocked = {"u"}                    # seeded from an earlier chunk
    assert r._taker_skip(ioc("u", "60000", "1")) == "skip_learn"
    r._blk_ioc = {"0xb": ("u", True)}           # a skipped IOC of u really traded -> unblock
    r._taker_learn()
    assert r._taker_skip(ioc("u", "60000", "1")) is None


def test_hist_rejects_match_by_cloid_or_shape_within_2s(tmp_path):
    import json
    from engine_py.replay import load_hist_rejects, hist_rejected
    rec = lambda status, cloid=None, ts=1_000_000: {"status": status, "order": {
        "coin": "BTC", "side": "B", "limitPx": "60000", "sz": "0.0", "origSz": "0.01", "timestamp": ts, "cloid": cloid}}
    json.dump([{"user": "0xU", "answer": [rec("perpMarginRejected"), rec("iocCancelRejected", ts=5_000_000),
                                          rec("perpMarginRejected", cloid="0xAB", ts=9_000_000)]}],
              open(tmp_path / "hist_orders.json", "w"))
    rej = load_hist_rejects(tmp_path)
    assert hist_rejected(rej, ioc("0xu", "60000", "0.01"), 1_001_500)
    assert not hist_rejected(rej, ioc("0xu", "60000", "0.01"), 1_003_000)          # > 2 s away
    assert not hist_rejected(rej, ioc("0xu", "60000", "0.01"), 5_000_000)          # iocCancelRejected: engine decides
    assert not hist_rejected(rej, ioc("0xu", "60000", "0.02"), 1_000_000)          # other size
    assert load_hist_rejects(tmp_path / "none") == {}
