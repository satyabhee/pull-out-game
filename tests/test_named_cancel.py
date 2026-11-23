"""A4(b2) Replay(named_cancel=True): an unresolvable cancel of user U removes U's ONE trade-named piece on that asset."""
from engine_py.matching import PLACEHOLDER
from engine_py.replay import Replay, listing
from engine_py.types import ASSETS, Side
from tests.window_fixture import A, M, T0, Window, order, place_tx, tx

BTC = ASSETS[0]


def level(r, px):
    lv = r.engine.books[0].side(Side.SELL).get(BTC.px_to_ticks(px))
    return [] if lv is None else [(nd.order.user, nd.remaining) for nd in lv]


def cloid_cancel_tx(user, h, height, cloid="0x" + "ab" * 16):
    return tx(user, {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": cloid}]}, h, height)


def window(tmp_path, cancel_user):
    """A takes 0.001 @ 60010 from M (the placeholder's front becomes M's piece by `name`); then cancel_user sends a
    cloid cancel the book cannot resolve."""
    w = Window(tmp_path)
    w.book("BTC", T0 - 50, [], [("60010", "0.005")])
    h1 = w.block([place_tx(A, "0xa1", 0, order(px="60010", sz="0.001", tif="Ioc"))], dt_ms=0)
    w.trade("BTC", "B", "60010", "0.001", w.time_of(h1), "0xa1", 1, A, M)
    w.block([cloid_cancel_tx(cancel_user, "0xc1", 0)], dt_ms=1000)
    w.block([], dt_ms=1000)
    return w.write()


def run(tmp_path, cancel_user, named_cancel):
    win, hl = window(tmp_path, cancel_user)
    r = Replay(win, hl, listing, trade_learn="name", named_cancel=named_cancel)
    list(r)
    return r


def test_owner_cancel_removes_the_named_piece(tmp_path):
    r = run(tmp_path, M, True)
    assert all(u != M for u, _ in level(r, "60010"))
    assert r.learn_stats["named_cancel_removed:BTC"] == 1


def test_off_by_default(tmp_path):
    r = run(tmp_path, M, False)
    assert any(u == M for u, _ in level(r, "60010"))
    assert r.learn_stats["named_cancel_removed:BTC"] == 0


def test_other_users_cancel_does_nothing(tmp_path):
    r = run(tmp_path, A, True)
    assert any(u == M for u, _ in level(r, "60010"))
    assert r.learn_stats["named_cancel_none:BTC"] == 1


def test_ambiguous_when_two_named_pieces(tmp_path):
    r = Replay.__new__(Replay)
    from collections import Counter

    class Book:
        def __contains__(self, oid):
            return True

    class Eng:
        books = {0: Book()}

        def _remove(self, oid):
            raise AssertionError("must not remove when ambiguous")

    r.engine, r.learn_stats, r._trade_named = Eng(), Counter(), {(M, 0): [7, 9]}

    class C:
        user, asset = M, 0
    r._cancel_named(C())
    assert r.learn_stats["named_cancel_ambiguous:BTC"] == 1
