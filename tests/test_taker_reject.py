"""taker_reject rules (Replay._taker_skip / _taker_learn) on a bare Replay, no window needed."""
from collections import Counter

from engine_py.replay import Replay
from engine_py.types import ASSETS, Place, Side, Tif


def bare(rules, real=None):
    r = Replay.__new__(Replay)
    r.taker_reject = frozenset(rules)
    r.taker_stats, r._taker_blocked, r._blk_ioc = Counter(), set(), {}
    r._real_trades = real or {}
    return r


def ioc(user, px, sz, h="0xa"):
    a = ASSETS[0]
    return Place(user=user, asset=0, block=1, tx_index=0, item_index=0, hash=h, priority=None,
                 side=Side.BUY, px=a.px_to_ticks(px), sz=a.sz_to_lots(sz), tif=Tif.IOC, cloid=None, reduce_only=False)


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
