"""L5-lite MultiLite (engine_py/multi_lite.py) on a real Engine with a stub Replay: forks, evidence, collapse."""
from collections import Counter
from types import SimpleNamespace

from data.parse import Trade
from engine_py.matching import PLACEHOLDER, Engine
from engine_py.multi_lite import MultiLite
from engine_py.types import Cancel, Place, Reject, RejectReason, Side, Tif

PX = 600_000


def setup(users=("a", "b", "c"), sizes=(5, 5, 5), placeholder=0):
    eng = Engine()
    rp = SimpleNamespace(engine=eng, placed={}, learn_stats=Counter(), _trade_named={})
    if placeholder:
        o = eng._new_order(PLACEHOLDER, 0, Side.SELL, PX, placeholder, Tif.GTC, None)
        eng._rest(o, placeholder)
    oids = []
    for u, q in zip(users, sizes):
        o = eng._new_order(u, 0, Side.SELL, PX, q, Tif.GTC, None)
        eng._rest(o, q)
        rp.placed[o.oid] = object()
        oids.append(o.oid)
    return eng, rp, MultiLite(rp), oids


def level(eng):
    lv = eng.books[0].side(Side.SELL).get(PX)
    return [] if lv is None else [(nd.order.user, nd.remaining) for nd in lv]


def snap(n):
    return SimpleNamespace(asset=0, bids=[], asks=[(PX, 15)], bid_n=(), ask_n=(n,))


def trade(maker, sz, h="0xt", seq=1):
    return Trade(asset=0, taker_side=Side.BUY, px=PX, sz=sz, time_ms=0, hash=h, tid=seq, buyer="taker",
                 seller=maker, seq=seq)


def test_no_fork_when_count_fits():
    eng, rp, ml, _ = setup()
    ml.on_snapshot(snap(3), height=1)
    assert ml.forks == {} and level(eng) == [("a", 5), ("b", 5), ("c", 5)]


def test_phantom_fork_keeps_b1_queue_until_evidence():
    eng, rp, ml, oids = setup()
    ml.on_snapshot(snap(2), height=1)
    f = ml.forks[(0, Side.SELL, PX)]
    assert len(f.alts) == 4 and f.primary == frozenset()          # keep-all + drop each of 3 candidates
    assert level(eng) == [("a", 5), ("b", 5), ("c", 5)]          # engine still holds B1's queue


def test_trade_evidence_collapses_to_the_consistent_drop():
    """Real taker of 5 lots filled from b: a must be the phantom (it is first in our queue)."""
    eng, rp, ml, oids = setup()
    ml.on_snapshot(snap(2), height=1)
    taker = Place(user="taker", asset=0, block=2, tx_index=0, item_index=0, hash="0xt", priority=None,
                  side=Side.BUY, px=PX, sz=5, tif=Tif.IOC, cloid=None, reduce_only=False)
    ml.before_action(taker)                       # snapshot before the tx
    eng.apply(taker)                              # engine (primary = keep all) fills a: recorded before evidence
    ml.on_real_trades("0xt", [trade("b", 5)])
    assert (0, Side.SELL, PX) not in ml.forks     # collapsed
    assert rp.learn_stats["ml_phantom_e1"] == 1


def test_owner_cancel_drops_that_candidates_phantom_alternative():
    eng, rp, ml, oids = setup()
    ml.on_snapshot(snap(2), height=1)
    c = Cancel(user="c", asset=0, block=2, tx_index=0, item_index=0, hash="0xc", priority=None, oid=0)
    info = ml.before_action(c)
    eng._remove(oids[2])                          # c's own cancel removes it
    ml.after_action(c, [], info)
    f = ml.forks.get((0, Side.SELL, PX))
    assert f is not None and all(oids[2] not in alt for alt in f.alts)
    assert rp.learn_stats["ml_phantom_e2_cancel"] == 1


def test_feed_counts_collapse_after_three_updates():
    eng, rp, ml, oids = setup()
    ml.on_snapshot(snap(2), height=1)
    for h in (2, 3, 4):
        ml.on_snapshot(snap(2), height=h)
    assert (0, Side.SELL, PX) not in ml.forks
    assert len(level(eng)) == 2                   # one candidate dropped: the count now equals n
    assert rp.learn_stats["ml_phantom_closed:feed"] == 1


def test_owner_fork_relabels_on_trade_evidence():
    eng, rp, ml, _ = setup(users=(), sizes=())
    named = eng._new_order("m1", 0, Side.SELL, PX, 10, Tif.GTC, None)
    eng._rest(named, 10)
    ml.owner_fork(0, Side.SELL, PX, named.oid, ["m1", "m2"], height=1)
    assert level(eng) == [("m1", 10)]
    taker = Place(user="taker", asset=0, block=2, tx_index=0, item_index=0, hash="0xt", priority=None,
                  side=Side.BUY, px=PX, sz=3, tif=Tif.IOC, cloid=None, reduce_only=False)
    ml.before_action(taker)
    eng.apply(taker)
    ml.on_real_trades("0xt", [trade("m2", 3)])
    assert level(eng) == [("m2", 7)]              # relabelled; the fill already recorded stays m1's
    assert rp.learn_stats["ml_owner_e1"] == 1


def test_owner_unresolvable_cancel_removes_the_piece():
    eng, rp, ml, _ = setup(users=(), sizes=())
    named = eng._new_order("m1", 0, Side.SELL, PX, 10, Tif.GTC, None)
    eng._rest(named, 10)
    ml.owner_fork(0, Side.SELL, PX, named.oid, ["m1", "m2"], height=1)
    c = Cancel(user="m2", asset=0, block=2, tx_index=0, item_index=0, hash="0xc", priority=None, oid=0)
    info = ml.before_action(c)
    ml.after_action(c, [Reject(c, RejectReason.UNKNOWN_ORDER)], info)
    assert level(eng) == [] and (0, Side.SELL, PX) not in ml.forks


def test_deterministic_tie_breaks():
    a = setup()
    b = setup()
    for eng, rp, ml, _ in (a, b):
        ml.on_snapshot(snap(2), height=1)
        for h in (2, 3, 4):
            ml.on_snapshot(snap(2), height=h)
    assert level(a[0]) == level(b[0])
