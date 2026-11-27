"""Gate 2b (docs/gate2b.md) on hand-checkable synthetic conflicts."""
import math
from decimal import Decimal

import pytest

from analysis.gate2b import (MidSeries, crosses, maker_markout_bp, p_hat, queue_ahead, raw_batches, raw_class,
                             saved_sizes, taker_items, value_usd)
from engine_py.book import Book
from engine_py.types import Modify, Order, Place, Side, Tif


def tx(h, typ, tifs=()):
    a = {"type": typ}
    if typ == "order":
        a["orders"] = [{"t": {"limit": {"tif": t}}} for t in tifs]
    return {"hash": h, "action": a}


def test_p5_batches_cut_at_each_rank_drop_over_all_txs():
    # C A G | C G | O ... rank C0 A1 G2 O3; drops at index 3 (G -> C) only; O after G is no drop
    txs = [tx("0xC1", "cancel"), tx("0xa1", "order", ["Alo"]), tx("0xg1", "order", ["Alo", "Ioc"]),
           tx("0xc2", "cancelByCloid"), tx("0xg2", "order", ["Gtc"]), tx("0xo", "noop")]
    assert raw_batches(txs) == {"0xc1": 0, "0xa1": 0, "0xg1": 0, "0xc2": 1, "0xg2": 1, "0xo": 1}
    assert raw_class({"type": "modify", "order": {"t": {"limit": {"tif": "Alo"}}}}) == "A"
    assert raw_class({"type": "batchModify", "modifies": [{"order": {"t": {"trigger": {}}}}]}) == "G"


def order(oid, side, px, sz, seq, user="m"):
    return Order(oid=oid, user=user, asset=0, side=side, px=px, sz=sz, tif=Tif.ALO, cloid=None, seq=seq, block=1)


def test_queue_ahead_counts_better_levels_and_fifo_ahead_at_the_price():
    b = Book(0)
    b.add(order(1, Side.SELL, 100, 2, 1), 2)
    b.add(order(2, Side.SELL, 100, 3, 2), 3)
    b.add(order(3, Side.SELL, 101, 5, 3), 5)
    b.add(order(4, Side.SELL, 99, 4, 4), 4)       # better ask
    assert queue_ahead(b, 4) == 0
    assert queue_ahead(b, 1) == 4                  # the 99 level
    assert queue_ahead(b, 2) == 4 + 2
    assert queue_ahead(b, 3) == 4 + 2 + 3
    b.add(order(5, Side.BUY, 98, 1, 5), 1)
    b.add(order(6, Side.BUY, 97, 1, 6), 1)
    assert queue_ahead(b, 6) == 1


def test_crossing_and_taker_items():
    assert crosses(Side.BUY, 100, 100) and not crosses(Side.BUY, 99, 100)
    assert crosses(Side.SELL, 100, 100) and not crosses(Side.SELL, 101, 100)
    meta = dict(user="T", asset=0, block=1, item_index=0, priority=None)
    p_ioc = Place(**meta, tx_index=0, hash="0xT", side=Side.BUY, px=100, sz=3, tif=Tif.IOC, cloid=None,
                  reduce_only=False)
    p_alo = Place(**meta, tx_index=1, hash="0xA", side=Side.BUY, px=100, sz=3, tif=Tif.ALO, cloid=None,
                  reduce_only=False)
    m_gtc = Modify(**meta, tx_index=2, hash="0xM", target=7,
                   new=Place(**meta, tx_index=2, hash="0xM", side=Side.SELL, px=90, sz=2, tif=Tif.GTC, cloid=None,
                             reduce_only=False))
    assert [(t["hash"], t["user"], t["sz"]) for t in taker_items([p_ioc, p_alo, m_gtc])] == [("0xt", "t", 3),
                                                                                              ("0xm", "t", 2)]


def test_saved_sizes_upper_and_queue_reach():
    # r = 3 lots resting, takers Q = 5, queue ahead A = 4: UPPER min(3, 5) = 3; ESTIMATE clamp(5 - 4, 0, 3) = 1
    assert saved_sizes(3, 5, 4) == (3, 1)
    assert saved_sizes(3, 2, 4) == (2, 0)          # takers can't get past the queue
    assert saved_sizes(3, 10, 0) == (3, 3)


def test_l21_markout_and_value_hand_checked():
    s = MidSeries([0, 500, 10_500, 11_000], [Decimal(100), Decimal(100), Decimal(150), Decimal(101)])
    # ask maker at t = 1000: m(t-) = 100 (ts 500), m(t + 10 s) = 101 (ts 11_000; 10_500 is too early): mo = -1e4 * 1/100 = -100 bp
    assert maker_markout_bp(s, 1000, maker_is_bid=False) == pytest.approx(-100.0)
    assert maker_markout_bp(s, 1000, maker_is_bid=True) == pytest.approx(100.0)
    # 2 BTC saved at px 100, adverse 100 bp -> 2 * 100 * 0.01 = 2 USD; a favourable move saves nothing
    assert value_usd(Decimal(2), Decimal(100), -100.0) == pytest.approx(2.0)
    assert value_usd(Decimal(2), Decimal(100), +100.0) == 0.0
    assert value_usd(Decimal(2), Decimal(100), None) == 0.0
    # strictly before: a mid AT t is not m(t-)
    assert maker_markout_bp(MidSeries([1000, 11_000], [Decimal(1), Decimal(2)]), 1000, True) is None
    # no forward mid within the wait limit
    assert maker_markout_bp(MidSeries([0, 80_000], [Decimal(1), Decimal(2)]), 1000, True) is None


def test_trailing_vol_uses_only_the_10s_before_t():
    s = MidSeries([1000, 2000, 3000, 12_000], [Decimal(100), Decimal(101), Decimal(100), Decimal(200)])
    v, n = s.vol_bp(10_500)                        # window [500, 10_500): the first three mids only
    assert n == 3
    assert v == pytest.approx(1e4 * math.sqrt(math.log(1.01) ** 2 + math.log(100 / 101) ** 2))
    assert s.vol_bp(1500) == (None, 1)


def test_p_hat_ties_count_half():
    assert p_hat([(1, 2), (3, 2), (5, 5), (0, 9)]) == (pytest.approx((1 + 0 + 0.5 + 1) / 4), 4)
    assert p_hat([]) == (None, 0)
