"""analysis/g1_fill_trace.py: real fills are traced to the state of the real maker's order."""
from analysis import g1_fill_trace as ft
from tests.window_fixture import A, M, T0, Window, order, place_tx

N = "0x" + "e" * 40


def test_real_fill_from_an_alo_we_rejected_is_traced_to_the_rejection(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60010", "0.001")], [])                     # our bid at 60010 blocks N's ALO
    w.block([place_tx(N, "0xn1", 0, order(buy=False, px="60010")),        # rejected: crosses our bid
             place_tx(M, "0xm1", 0, order(buy=False, px="60020"))])
    h = w.block([place_tx(A, "0xb1", 0, order(px="60020", tif="Ioc"))])   # engine fills M at 60020
    w.trade("BTC", "B", "60010", "0.001", w.time_of(h), "0xb1", 1, A, N)  # chain: N's order at 60010
    window, hl = w.write()
    states, _ = ft.run(window, hl, quiet=True)
    assert states == {"ALO rejected": 1}


def test_engine_removals_are_labelled():
    eng = ft.TracingEngine()
    from engine_py.types import Side
    oid = eng.seed(0, Side.BUY, 100, 5)
    eng.evict(oid)
    assert eng.removals[-1][-1] == "gate-evict"
    eng.seed(0, Side.BUY, 100, 5)
    eng.clear(0)
    assert eng.removals[-1][-1] == "re-seed clear"
