"""replay/rust_book.RustBook (Rust copy-on-write book) vs engine_py.book.Book: same results, same errors, same
queues after every operation of random sequences. Skipped when the replay_core extension is not built."""
import copy

import pytest
from hypothesis import given, settings, strategies as st

from engine_py.book import Book, BookError
from engine_py.types import Order, Side, Tif

pytest.importorskip("replay_core.replay_core", reason="replay_core extension not built")
from replay.rust_book import RustBook  # noqa: E402


def state(book):
    out = []
    for side in (Side.BUY, Side.SELL):
        for lv in book.levels(side):
            out.append((side, lv.px, lv.total, len(lv), [(n.order, n.remaining) for n in lv]))
    return out, book.best_bid(), book.best_ask()


def make_order(oid, side, px, sz, tif, user):
    return Order(oid=oid, user=user, asset=0, side=side, px=px, sz=sz, tif=tif, cloid=f"c{oid % 4}" if oid % 3 else None,
                 seq=oid, block=7)


ops = st.lists(st.one_of(
    st.tuples(st.just("add"), st.booleans(), st.integers(95, 105), st.integers(1, 9), st.integers(0, 2),
              st.sampled_from(["tail", "front", "jumps"]), st.integers(0, 3)),
    st.tuples(st.just("remove"), st.integers(0, 50)),
    st.tuples(st.just("reduce"), st.integers(0, 50), st.integers(1, 10)),
    st.tuples(st.just("bad"), st.integers(0, 3)),
    st.tuples(st.just("copy")),
), min_size=1, max_size=60)


def call(book, fn, *a, **k):
    try:
        r = getattr(book, fn)(*a, **k)
        return ("ok", (r.order, r.remaining) if r is not None else None)
    except BookError as e:
        return ("BookError", str(e))
    except KeyError as e:
        return ("KeyError", str(e))


@settings(max_examples=400, deadline=None)
@given(ops)
def test_same_as_python_book(seq):
    py, rs = Book(0), RustBook(0)
    copies = []
    nxt = 1
    for op in seq:
        if op[0] == "add":
            _, buy, px, sz, tif, how, user = op
            side = Side.BUY if buy else Side.SELL
            px = min(px, 99) if buy else max(px, 100)                       # never crossed
            o = make_order(nxt, side, px, sz, (Tif.GTC, Tif.IOC, Tif.ALO)[tif], f"u{user}")
            nxt += 1
            kw = {"front": True} if how == "front" else {}
            if how == "jumps":
                seen = {"py": [], "rs": []}

                def jumps_for(name):
                    def j(node):
                        seen[name].append(node.order.oid)
                        return node.order.oid % 2 == 1                       # jump odd oids only
                    return j
                assert call(py, "add", o, sz, jumps=jumps_for("py")) == call(rs, "add", o, sz, jumps=jumps_for("rs"))
                assert seen["py"] == seen["rs"]                               # same nodes asked, same order
            else:
                assert call(py, "add", o, sz, **kw) == call(rs, "add", o, sz, **kw)
        elif op[0] in ("remove", "reduce"):
            oids = [n.order.oid for s in (Side.BUY, Side.SELL) for lv in py.levels(s) for n in lv]
            oid = oids[op[1] % len(oids)] if oids else 999
            args = (oid,) if op[0] == "remove" else (oid, op[2])
            assert call(py, op[0], *args) == call(rs, op[0], *args)
        elif op[0] == "bad":
            o = make_order(1 if nxt > 1 else 1, Side.BUY, 90, 5, Tif.GTC, "u")
            cases = [("add", (o, 5)), ("add", (o, 6)), ("remove", (12345,)), ("node", (12345,))]
            fn, a = cases[op[1]]
            assert call(py, fn, *a) == call(rs, fn, *a)
        else:
            copies.append((copy.deepcopy(rs), state(py)))
        assert state(py) == state(rs)
        py.check_invariants()
        rs.check_invariants()
    for c, frozen in copies:                                                  # copies never see later changes
        assert state(c) == frozen


def test_copy_on_write_levels():
    rs = RustBook(0)
    for oid, px in ((1, 99), (2, 98), (3, 97)):
        rs.add(make_order(oid, Side.BUY, px, 5, Tif.GTC, "u"), 5)
    twin = copy.deepcopy(rs)
    assert all(rs.shares_level_with(twin, Side.BUY, px) for px in (99, 98, 97))
    twin.reduce(2, 1)                                     # touches level 98 only
    assert rs.shares_level_with(twin, Side.BUY, 99) and rs.shares_level_with(twin, Side.BUY, 97)
    assert not rs.shares_level_with(twin, Side.BUY, 98)
    assert rs.node(2).remaining == 5 and twin.node(2).remaining == 4
