"""analysis/g1_queue.py: missed-fill deep dive (absent / size ahead / modify keeps priority)."""

from analysis import g1_queue as q
from engine_py.types import ASSETS
from tests.window_fixture import A, M, T0, Window, order, place_tx, tx

BTC = ASSETS[0]
N = "0x" + "e" * 40
CL = "0x" + "1" * 32


def n_of(px, sz):
    return BTC.px_to_ticks(px) * BTC.sz_to_lots(sz)


def cloid_cancel_tx(user, h, cloid):
    return tx(user, {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": cloid}]}, h, 0)


def modify_tx(user, h, target, new):
    return tx(user, {"type": "modify", "oid": target, "order": new}, h, 0)


# ------------------------------------------------------------------ pure helpers

def test_block_bucket():
    assert [q.block_bucket(d) for d in (None, 0, 1, 3, 12, 13)] == \
        ["never", "same block", "next block", "2-5 blocks", "6-12 blocks", "> 12 blocks"]


def test_absent_cause_uses_the_latest_placement_since_the_re_seed():
    events = [(5, "rested", 1), (8, "ALO rejected by our book (gate abstained)", None), (12, "rested", 2)]
    removed = {2: dict(kind="cancel by cloid", seq=14, block=3)}
    assert q.absent_cause(events, removed, {}, 0, 20) == ("removed: cancel by cloid", 2)
    assert q.absent_cause(events, removed, {}, 0, 10) == ("ALO rejected by our book (gate abstained)", None)
    assert q.absent_cause(events, {}, {1: dict(seq=6)}, 0, 7) == ("filled out earlier by the engine", 1)
    assert q.absent_cause(events, {}, {}, 12, 20) == ("no placement since re-seed", None)


def test_modify_keeping_priority_puts_a_same_price_modified_order_back_in_front():
    nodes = [(9, "ph", 5), (3, "x", 1), (4, "maker", 1)]     # placeholder, x, then maker (modified from 1)
    seqs = {9: 0, 1: 10, 3: 20, 4: 30}
    pred = {4: (1, True, False)}                               # same px, size increase
    assert q.first_if_modify_keeps(nodes, 4, pred, seqs, strict=False) == (False, True)   # x jumps behind
    assert q.first_if_modify_keeps(nodes, 4, pred, seqs, strict=True) == (False, False)   # not a decrease
    assert q.first_if_modify_keeps(nodes[1:], 4, pred, seqs, strict=False) == (True, True)


def test_ahead_kind_precedence():
    assert q.ahead_kind(dict(alo_truth="rejected_alo", reduce_only=True), 10, 100).startswith("phantom: chain")
    ev = dict(removed=dict(kind="gate-evict", seq=101, block=12))
    assert q.ahead_kind(ev, 10, 100) == "phantom evidence: later evicted by gate / rule 8"
    assert q.ahead_kind(dict(reduce_only=True), 10, 100) == "reduce-only (maybe no position)"
    assert q.ahead_kind(dict(missed_cancel_before=True), 10, 100) == "owner had an unresolved cancel before the tx"
    c = dict(removed=dict(kind="cancel by cloid", seq=101, block=11))
    assert q.ahead_kind(c, 10, 100) == "owner later cancel: next block"
    assert q.ahead_kind(dict(filled_out=dict(seq=150)), 10, 100) == "filled later by the engine"
    assert q.ahead_kind({}, 10, 100) == "still resting / cleared at re-seed"


# ------------------------------------------------------------------ instrumented runs

def test_absent_maker_removed_by_its_own_cloid_cancel(tmp_path):
    """M rests 0.001 @ 60020 with a cloid, cancels it, then the chain fills M there: absent, removed by cloid."""
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60050", "0.001")])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60020", cloid=CL))])
    w.block([cloid_cancel_tx(M, "0xm2", CL)])
    h = w.block([place_tx(A, "0xa1", 0, order(px="60020", tif="Ioc"))])
    w.trade("BTC", "B", "60020", "0.001", w.time_of(h), "0xa1", 1, A, M)
    window, hl = w.write()
    out = q.run(window, hl, tmp_path / "q", alo_gate=None, reseed=True)
    n = n_of("60020", "0.001")
    assert out["state"] == {"absent": n}
    assert out["absent"] == {"removed: cancel by cloid": n}
    assert out["absent_removal_timing"] == {"removed: cancel by cloid | next block": n}
    assert (tmp_path / "q" / "queue.md").exists()


def test_size_ahead_split_by_owner_cancel_timing(tmp_path):
    """N rests ahead of M at 60020; the taker fills N in the engine but M on chain; N cancels next block."""
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60050", "0.001")])
    w.block([place_tx(N, "0xn1", 0, order(buy=False, px="60020", sz="0.002", cloid=CL)),
             place_tx(M, "0xm1", 0, order(buy=False, px="60020"))])
    h = w.block([place_tx(A, "0xa1", 0, order(px="60020", tif="Ioc"))])
    w.block([cloid_cancel_tx(N, "0xn2", CL)])
    w.trade("BTC", "B", "60020", "0.001", w.time_of(h), "0xa1", 1, A, M)
    window, hl = w.write()
    out = q.run(window, hl, tmp_path / "q", alo_gate=None, reseed=True)
    n = n_of("60020", "0.001")
    assert out["state"] == {"size ahead": n}
    assert out["ahead"] == {"owner later cancel: next block": n}


def test_absent_maker_filled_out_by_a_spurious_engine_fill(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60050", "0.001")])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60020"))])
    w.block([place_tx(N, "0xn9", 0, order(px="60020", tif="Ioc"))])          # engine fills M; chain did not
    h = w.block([place_tx(A, "0xa1", 0, order(px="60020", tif="Ioc"))])
    w.trade("BTC", "B", "60020", "0.001", w.time_of(h), "0xa1", 1, A, M)
    window, hl = w.write()
    out = q.run(window, hl, tmp_path / "q", alo_gate=None, reseed=True)
    n = n_of("60020", "0.001")
    assert out["absent"] == {"filled out earlier by the engine: chain did NOT fill this maker there (spurious)": n}


def test_modify_same_price_counted_and_counterfactual(tmp_path):
    """M rests, N rests behind, M modifies to a smaller size at the same price (moves behind N); the chain
    fills M. Under 'modify keeps priority' M would be first again."""
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60050", "0.001")])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60020", sz="0.002", cloid=CL))])
    w.block([place_tx(N, "0xn1", 0, order(buy=False, px="60020"))])
    w.block([modify_tx(M, "0xm2", CL, order(buy=False, px="60020", sz="0.001", cloid=CL))])
    h = w.block([place_tx(A, "0xa1", 0, order(px="60020", tif="Ioc"))])
    w.trade("BTC", "B", "60020", "0.001", w.time_of(h), "0xa1", 1, A, M)
    window, hl = w.write()
    out = q.run(window, hl, tmp_path / "q", alo_gate=None, reseed=True)
    n = n_of("60020", "0.001")
    assert out["modify_counts"] == {"same px, size decrease": 1}
    mk = out["modify_keeps_priority"]
    assert mk["same px, any size | maker first"] == n
    assert mk["same px, size decrease | maker first"] == n


def _modify_window(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60050", "0.001")])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60020", sz="0.002", cloid=CL))])
    w.block([place_tx(N, "0xn1", 0, order(buy=False, px="60020"))])
    w.block([modify_tx(M, "0xm2", CL, order(buy=False, px="60020", sz="0.001", cloid=CL))])
    h = w.block([place_tx(A, "0xa1", 0, order(px="60020", tif="Ioc"))])
    w.trade("BTC", "B", "60020", "0.001", w.time_of(h), "0xa1", 1, A, M)
    return w.write()


def test_modify_keeps_flag_fixes_the_missed_fill_and_default_does_not(tmp_path):
    window, hl = _modify_window(tmp_path)
    base = q.run(window, hl, tmp_path / "b", alo_gate=None, reseed=True)
    flag = q.run(window, hl, tmp_path / "f", alo_gate=None, reseed=True, modify_keeps="decrease")
    assert base["base"]["missed"] > 0 and flag["base"]["missed"] == 0
    assert flag["modify_moved"] == {"same px, moved to old slot": 1}


def test_modify_keeps_decrease_mode_ignores_a_size_increase(tmp_path):
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60050", "0.001")])
    w.block([place_tx(M, "0xm1", 0, order(buy=False, px="60020", sz="0.001", cloid=CL))])
    w.block([place_tx(N, "0xn1", 0, order(buy=False, px="60020"))])
    w.block([modify_tx(M, "0xm2", CL, order(buy=False, px="60020", sz="0.002", cloid=CL))])
    window, hl = w.write()
    out = q.run(window, hl, tmp_path / "d", alo_gate=None, reseed=True, modify_keeps="decrease")
    assert out["modify_moved"] == {"same px, size increase (not moved)": 1}
    out = q.run(window, hl, tmp_path / "a", alo_gate=None, reseed=True, modify_keeps="any")
    assert out["modify_moved"] == {"same px, moved to old slot": 1}


def test_force_admit_users_are_listed_and_bypass_the_gate(tmp_path):
    """A recorded touch says the ALO would cross, so the gate rejects it and the real maker is absent;
    force-admitting that user rests it."""
    w = Window(tmp_path)
    w.book("BTC", T0 - 100, [("60000", "0.001")], [("60050", "0.001")])
    w.bbo("BTC", T0 - 10, bid=("60000", "0.001", 1), ask=("60010", "0.001", 1))
    w.block([place_tx(M, "0xm1", 0, order(buy=True, px="60010", cloid=CL, tif="Alo"))])
    h = w.block([place_tx(A, "0xa1", 0, order(buy=False, px="60010", tif="Ioc"))])
    w.trade("BTC", "A", "60010", "0.001", w.time_of(h), "0xa1", 1, M, A)
    window, hl = w.write()
    base = q.run(window, hl, tmp_path / "b", alo_gate="touch", reseed=True)
    assert M in base["absent_gate_users"]
    flag = q.run(window, hl, tmp_path / "f", alo_gate="touch", reseed=True, force_admit=[M])
    assert flag["base"]["missed"] < base["base"]["missed"]
