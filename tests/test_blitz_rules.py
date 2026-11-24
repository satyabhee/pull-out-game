"""Off-by-default BLITZ rules: I37 (users pinned at their open-order cap) and I34 (gate abstains after an in-block
sweep through its touch)."""

import json

from engine_py.replay import Replay, listing
from tests.window_fixture import A, H0, M, T0, Window, cancel_tx, order, place_tx

BTC_PX = "60000"


def _snap(root, name, user, total, answered_s):
    d = root / "windows" / name
    d.mkdir(parents=True)
    (d / "open_book_users.json").write_text(json.dumps([{"user": user, "total_open": total, "answered_at": answered_s,
                                                         "btc_eth": []}]))


def _resting(r, user):
    return sum(1 for bk in r.engine.books.values() for lv in bk.bids.values() for nd in lv if nd.order.user == user)


def _cap_window(tmp_path, total_prev=1500, total_last=1500):
    _snap(tmp_path, "900-905", M, total_prev, (T0 - 60_000) / 1000)
    _snap(tmp_path, "950-955", M, total_last, (T0 - 30_000) / 1000)
    w = Window(tmp_path, name=f"{H0}-{H0 + 2}")
    w.block([place_tx(M, "0x01", H0, order(px="59990")), place_tx(A, "0x02", H0, order(px="59980"))])
    w.block([cancel_tx(M, "0x03", H0 + 1, 0, 999), place_tx(M, "0x04", H0 + 1, order(px="59970"))])
    w.block([])
    return w.write()


def test_cap_pinned_user_cannot_add_orders(tmp_path):
    window, hl = _cap_window(tmp_path)
    off = Replay(window, hl, listing)
    list(off)
    assert _resting(off, M) == 2
    for mode, expect in (("removed", 0), ("any", 1)):      # V2: a failed cancel frees nothing; V1: any cancel frees one
        r = Replay(window, hl, listing, cap_windows=str(tmp_path / "windows"), cap_free=mode)
        list(r)
        assert r.cap_pinned == {M.lower()}
        assert _resting(r, M) == expect
        assert _resting(r, A) == 1                           # other users untouched


def test_cap_not_pinned_when_count_moves_or_is_small(tmp_path):
    window, hl = _cap_window(tmp_path, total_prev=1500, total_last=1490)
    r = Replay(window, hl, listing, cap_windows=str(tmp_path / "windows"))
    list(r)
    assert r.cap_pinned == set() and _resting(r, M) == 2


def test_cap_ignores_snapshots_answered_after_chunk_start(tmp_path):
    _snap(tmp_path, "900-905", M, 1500, (T0 - 60_000) / 1000)
    _snap(tmp_path, "950-955", M, 1500, (T0 + 5_000) / 1000)   # answered after the chunk began: not usable
    w = Window(tmp_path, name=f"{H0}-{H0 + 1}")
    w.block([place_tx(M, "0x01", H0, order(px="59990"))])
    w.block([])
    r = Replay(*w.write(), listing, cap_windows=str(tmp_path / "windows"))
    list(r)
    assert r.cap_pinned == set()


def _sweep_window(tmp_path):
    from tests.window_fixture import B
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("59990", "0.001")], [("60000", "0.001")])
    w.bbo("BTC", T0 - 50, ("59990", "0.001", 1), ("60000", "0.001", 1))
    w.block([])
    t1 = w.block([place_tx(B, "0x0a", H0 + 1, order(px="60000", tif="Ioc")),     # sweeps the 60000 ask
                  place_tx(A, "0x0b", H0 + 1, order(px="60000"))])                # then an ALO bid at 60000
    w.trade("BTC", "B", "60000", "0.001", w.time_of(t1), "0x0a", 7, B, "0x" + "0" * 40)
    w.block([])
    return w.write()


def _alo_rested(r):
    return any(nd.order.user == A for bk in r.engine.books.values() for lv in bk.bids.values() for nd in lv)


def test_gate_insweep_defers_to_engine_after_an_in_block_sweep(tmp_path):
    window, hl = _sweep_window(tmp_path)
    off = Replay(window, hl, listing, alo_gate="touch")
    list(off)
    assert not _alo_rested(off)                   # the previous block's touch (ask 60000) says the ALO crosses
    on = Replay(window, hl, listing, alo_gate="touch", gate_insweep=True)
    list(on)
    assert _alo_rested(on)                        # an earlier tx swept 60000: the engine's book decides (no cross)
    assert on.gate_stats["abstain_insweep"] == 1
