"""replay/rust_engine.RustEngine (Rust) vs engine_py.matching.Engine (Python reference), differential.

Random sequences of every operation the replay uses (Place GTC/IOC/ALO with priority bids, int-oid cancels through
a resolver, cloid cancels, modifies, scheduleCancel, outside fills, end_block, seed, reconcile, refill, clear,
evict, direct _new_order/_rest, direct book add/reduce + by_user.setdefault as the replay does), under every engine
setting (stp, p_priority mode, evict_arrival). After every step: same fills, same rejects, same errors, same books
(queue order and remaining), same by_user order, same counters and stats. Skipped without the extension.
"""
import copy
import itertools
import pickle

import pytest
from hypothesis import event, given, settings, strategies as st

from engine_py.book import BookError
from engine_py.matching import Engine
from engine_py.types import (Cancel, CancelByCloid, Modify, OutsideFill, Place, ScheduleCancel, Side, Tif,
                             ZERO_HASH)

pytest.importorskip("replay_core.replay_core", reason="replay_core extension not built")
from replay.rust_engine import RustEngine  # noqa: E402

USERS = ["u0", "u1", "u2", "placeholderish"]
_i = itertools.count(1)


def meta(user, block, p=None):
    return dict(user=user, block=block, tx_index=next(_i), item_index=0, hash=f"0x{user}{block}", priority=p)


def place(d, block, asset=0):
    user = d.draw(st.sampled_from(USERS))
    side = d.draw(st.sampled_from([Side.BUY, Side.SELL]))
    tif = d.draw(st.sampled_from([Tif.GTC, Tif.IOC, Tif.ALO]))
    px = d.draw(st.integers(96, 104))
    sz = d.draw(st.integers(1, 9))
    cloid = d.draw(st.sampled_from([None, "c1", "c2"]))
    p = d.draw(st.sampled_from([None, 0, 1, 5, 9]))
    return Place(**meta(user, block, p), asset=asset, side=side, px=px, sz=sz, tif=tif, cloid=cloid,
                 reduce_only=False)


def snapshot_levels(d, side):
    n = d.draw(st.integers(0, 3))
    lo, hi = (94, 99) if side is Side.BUY else (101, 106)
    return [(d.draw(st.integers(lo, hi)), d.draw(st.integers(1, 12))) for _ in range(n)]


def state(e):
    books = []
    for a in sorted(e.books):
        for side in (Side.BUY, Side.SELL):
            books.append([(lv.px, lv.total, len(lv), [(n.order, n.remaining) for n in lv])
                          for lv in e.books[a].levels(side)])
    users = {u: list(e.by_user.get(u, {})) for u in USERS + ["placeholder", "named"]}
    stats = {k: v for k, v in e.stats.items()}
    return books, users, e.next_oid, e.next_seq, e.next_fill_seq, e.block, stats, dict(e.schedules)


def outcome(fn):
    try:
        r = fn()
    except BookError as err:          # CrossedBookError is a BookError: compare the exact class too
        return ("error", type(err).__name__, str(err))
    except (ValueError, KeyError) as err:
        return ("error", type(err).__name__, str(err))
    if isinstance(r, tuple) and len(r) == 2 and isinstance(r[0], list):
        fills, rejects = r
        return ("ok", fills, [(x.action, x.reason) for x in rejects])
    return ("ok", r)


@settings(max_examples=1500, deadline=None)
@given(st.data())
def test_rust_engine_equals_python(d):
    stp = d.draw(st.sampled_from(["cancel_taker", "expire_maker"]))
    pp = d.draw(st.sampled_from([None, "always", "known", "knownpos", "docs", "docsall", 70]))
    evarr = d.draw(st.booleans())
    py = Engine(stp=stp, p_priority=pp, evict_arrival=evarr)
    rs = RustEngine(stp=stp, p_priority=pp, evict_arrival=evarr)
    for e in (py, rs):                       # resolver stub: HL oid = engine oid, only the user's own order
        e.resolve_oid = (lambda eng: (lambda user, asset, hl: hl if hl in eng.books[asset]
                                      and eng.books[asset].node(hl).order.user == user else None))(e)
    block, t = 1, 0
    copies = []
    steps = 0
    for _ in range(d.draw(st.integers(1, 40))):
        op = d.draw(st.sampled_from(["place", "place", "place", "cancel", "cloid", "modify", "schedule", "outside",
                                     "end", "time", "seed", "reconcile", "refill", "clear", "evict", "rest",
                                     "named", "reduce", "copy", "pickle"]))
        resting = sorted((n.order.oid, n.order.user, n.remaining) for a in py.books for s in (Side.BUY, Side.SELL)
                         for lv in py.books[a].levels(s) for n in lv)
        pick = d.draw(st.integers(0, 10**6))
        some_oid, some_user, some_rem = resting[pick % len(resting)] if resting else (777, "u0", 1)
        if op in ("evict", "reduce") and not resting:
            continue
        if op == "place":
            a = place(d, block)
            fns = [lambda e, a=a: e.apply(a)]
        elif op == "cancel":
            u = d.draw(st.sampled_from([some_user, some_user, "u1"]))
            a = Cancel(**meta(u, block), asset=0, oid=d.draw(st.sampled_from([some_oid, some_oid, 999])))
            fns = [lambda e, a=a: e.apply(a)]
        elif op == "cloid":
            a = CancelByCloid(**meta(d.draw(st.sampled_from(USERS)), block), asset=0,
                              cloid=d.draw(st.sampled_from(["c1", "c2", "zz"])))
            fns = [lambda e, a=a: e.apply(a)]
        elif op == "modify":
            new = place(d, block)
            target = d.draw(st.sampled_from([some_oid, "c1", "c2", 999]))
            a = Modify(**meta(new.user, block), asset=0, target=target, new=new)
            fns = [lambda e, a=a: e.apply(a)]
        elif op == "schedule":
            a = ScheduleCancel(**meta(d.draw(st.sampled_from(USERS)), block),
                               at_ms=d.draw(st.sampled_from([None, t, t + 50])))
            fns = [lambda e, a=a: e.apply(a)]
        elif op == "outside":
            a = OutsideFill(user=ZERO_HASH[:10], block=block, tx_index=next(_i), item_index=0, hash=ZERO_HASH,
                            priority=None, asset=0, maker_user=d.draw(st.sampled_from(USERS)),
                            taker_side=d.draw(st.sampled_from([Side.BUY, Side.SELL])), px=d.draw(st.integers(96, 104)),
                            sz=d.draw(st.integers(1, 12)), tid=next(_i))
            fns = [lambda e, a=a: e.apply(a)]
        elif op == "end":
            t += d.draw(st.integers(0, 120))
            block += 1
            fns = [lambda e, h=block, tt=t: e.end_block(h, tt)]
        elif op == "time":
            t += d.draw(st.integers(0, 300))
            fns = [lambda e, tt=t: setattr(e, "block_time_ms", tt)]
        elif op == "seed":
            side = d.draw(st.sampled_from([Side.BUY, Side.SELL]))
            px = d.draw(st.integers(94, 99)) if side is Side.BUY else d.draw(st.integers(101, 106))
            sz = d.draw(st.integers(1, 9))
            fns = [lambda e, side=side, px=px, sz=sz: e.seed(0, side, px, sz)]
        elif op == "reconcile":
            bids, asks = snapshot_levels(d, Side.BUY), snapshot_levels(d, Side.SELL)
            known = d.draw(st.sampled_from([None, "oldest", "newest"]))
            fns = [lambda e, b=bids, k=asks, kn=known: e.reconcile(0, b, k, known=kn)]
        elif op == "refill":
            bids, asks = snapshot_levels(d, Side.BUY), snapshot_levels(d, Side.SELL)
            fns = [lambda e, b=bids, k=asks: e.refill(0, b, k)]
        elif op == "clear":
            fns = [lambda e: e.clear(0)]
        elif op == "evict":
            fns = [lambda e, o=some_oid: e.evict(o)]
        elif op == "rest":                    # Replay._rest_named: _new_order + _rest without p
            side = d.draw(st.sampled_from([Side.BUY, Side.SELL]))
            px = d.draw(st.integers(90, 95)) if side is Side.BUY else d.draw(st.integers(105, 110))
            q = d.draw(st.integers(1, 5))
            fns = [lambda e, side=side, px=px, q=q: e._rest(e._new_order("named", 0, side, px, q, Tif.GTC, "nc"), q)]
        elif op == "named":                   # Replay._learn: _new_order + books.add(front) + by_user.setdefault
            side = d.draw(st.sampled_from([Side.BUY, Side.SELL]))
            px = d.draw(st.integers(90, 95)) if side is Side.BUY else d.draw(st.integers(105, 110))
            q = d.draw(st.integers(1, 5))

            def named(e, side=side, px=px, q=q):
                order = e._new_order("named", 0, side, px, q, Tif.GTC, None)
                e.books[0].add(order, q, front=True)
                e.by_user.setdefault("named", {})[order.oid] = None
            fns = [named]
        elif op == "reduce":                  # direct book reduce (placeholder trims)
            q = min(d.draw(st.integers(1, 3)), some_rem)
            fns = [lambda e, o=some_oid, q=q: e.books[0].reduce(o, q).remaining if o in e.books[0] else None]
        elif op == "pickle":                  # setup cache: a pickled + reloaded engine is the same engine
            resolver, rs.resolve_oid = rs.resolve_oid, None          # (the test's resolver is a lambda)
            rs = pickle.loads(pickle.dumps(rs, protocol=5))
            rs.resolve_oid = (lambda eng: (lambda user, asset, hl: hl if hl in eng.books[asset]
                                           and eng.books[asset].node(hl).order.user == user else None))(rs)
            assert state(rs) == state(py)
            event("pickled mid-sequence")
            continue
        else:
            copies.append((copy.deepcopy(rs), state(py)))
            continue
        r_py, r_rs = outcome(lambda: fns[0](py)), outcome(lambda: fns[0](rs))
        assert r_py == r_rs, (op, r_py, r_rs)
        if r_py[0] == "error":
            event(f"ended on {op} error: {r_py[1]}")
            return                            # partial changes before an error are not compared further
        steps += 1
        if r_py[0] == "ok" and len(r_py) == 3 and r_py[1]:
            event("had a fill")
        assert state(py) == state(rs), op
    event(f"steps compared: {min(steps // 10 * 10, 40)}+")
    for c, frozen in copies:                  # copies never see later changes
        assert state(c) == frozen


def test_resolver_naming_another_users_order_raises_the_same():
    for e in (Engine(), RustEngine()):
        e.resolve_oid = lambda user, asset, hl: hl
    py, rs = Engine(), RustEngine()
    for e in (py, rs):
        e.resolve_oid = lambda user, asset, hl: hl
        e.apply(Place(**meta("u0", 1), asset=0, side=Side.BUY, px=99, sz=1, tif=Tif.ALO, cloid=None,
                      reduce_only=False))
    a = Cancel(**meta("u1", 1), asset=0, oid=1)
    assert outcome(lambda: py.apply(a)) == outcome(lambda: rs.apply(a)) == \
        ("error", "BookError", "resolver mapped u1's oid 1 to u0's order")
