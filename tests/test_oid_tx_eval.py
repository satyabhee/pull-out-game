"""Adversarial tests for the int-oid resolver: whatever it does, it may abstain but never resolve wrongly.

Written against the public contract only: engine.resolve_oid(user, asset, hl_oid) -> engine oid | None,
OidResolver.note(action, engine_oid). Truth is known by construction (true hl oid = estimate + drift)."""
import random

import pytest

from analysis import oid_tx_eval as ev
from data.oids import OidEstimator, OidResolver
from engine_py.matching import Engine
from engine_py.types import Place, Side, Tif

META = dict(tx_index=0, item_index=0, hash="0x1", priority=None)


class World:
    """Orders at counted positions k with a linear anchor line (slope 1, so estimate(k) = base + k)."""

    def __init__(self, n_items=100_000, base=1_000_000):
        self.base = base
        self.counts = {(1, k, 0): k for k in range(0, n_items, 1)} if n_items <= 2000 else {}
        self.eng = Engine()
        self.anchors = [(0, base), (n_items, base + n_items)]
        self.est = OidEstimator(self.counts, self.anchors)
        self.res = OidResolver(self.eng, self.est)
        self.eng.resolve_oid = self.res
        self.px = 100_000
        self.truth: dict[int, int] = {}                   # engine oid -> true hl oid

    def place(self, user, k, drift=0, side=Side.BUY):
        self.counts[(1, k, 0)] = k
        self.px += 1 if side == Side.BUY else 0
        a = Place(user=user, block=1, asset=0, side=side, px=self.px if side == Side.BUY else 10**7 - self.px,
                  sz=1, tif=Tif.ALO, cloid=None, reduce_only=False, **{**META, "tx_index": k})
        before = self.eng.next_oid
        self.eng.apply(a)
        self.res.note(a, before)
        self.truth[before] = self.base + k + drift
        return before

    def cancel_result(self, user, true_hl):
        return self.res(user, 0, true_hl)


def check(world, user, eng_oid):
    r = world.cancel_result(user, world.truth[eng_oid])
    assert r is None or r == eng_oid, "WRONG resolution"
    return r


def test_two_users_interleaved_never_cross_users():
    w = World(2000)
    a = [w.place("u", 100 + 3 * i) for i in range(5)]
    b = [w.place("v", 101 + 3 * i) for i in range(5)]
    for o in a:
        r = check(w, "u", o)
        assert r in (None, o)
    for o in b:
        assert check(w, "v", o) in (None, o)


def test_two_clusters_identical_spacing_must_abstain():
    w = World(2000)
    c1 = [w.place("u", 500 + 10 * i) for i in range(3)]
    c2 = [w.place("u", 530 + 10 * i) for i in range(3)]          # same spacing, 30 ids away: inside tolerance
    for o in c1 + c2:
        assert w.cancel_result("u", w.truth[o]) is None


def test_drift_within_tolerance_is_never_wrong():
    w = World(2000)
    os_ = [w.place("u", 100 + 200 * i, drift=2 * i) for i in range(8)]   # 1% drift, orders far apart
    for o in os_:
        assert check(w, "u", o) in (None, o)


def test_orders_closer_than_drift_abstain_or_are_correct():
    w = World(2000)
    a = w.place("u", 100, drift=40)
    b = w.place("u", 120, drift=40)
    for o in (a, b):
        assert check(w, "u", o) in (None, o)


def test_cancelled_target_gone_after_reseed_does_not_hit_a_neighbour():
    w = World(2000)
    a = w.place("u", 100)
    b = w.place("u", 400)
    w.eng.evict(a)                                      # a re-seed dropped a
    assert w.cancel_result("u", w.truth[a]) is None
    assert check(w, "u", b) in (None, b)


def test_modify_new_order_gets_its_own_estimate():
    w = World(2000)
    a = w.place("u", 100)
    b = w.place("u", 105)                               # a modify's new order (new oid)
    assert w.cancel_result("u", w.truth[b]) in (None, b)
    assert w.cancel_result("u", w.truth[a]) in (None, a)


@pytest.mark.parametrize("seed", range(5))
def test_random_worlds_zero_wrong_when_noise_is_within_tolerance(seed):
    rng = random.Random(seed)
    w = World(2000)
    users = ["u1", "u2", "u3"]
    made = []
    ks = rng.sample(range(10, 1990), 60)
    for k in ks:
        u = rng.choice(users)
        tol = w.est.tolerance(k)
        made.append((u, w.place(u, k, drift=rng.randint(-tol, tol))))
    wrong = 0
    for u, o in made:
        r = w.cancel_result(u, w.truth[o])
        wrong += r is not None and r != o
    assert wrong == 0


def test_structure_counts_on_a_synthetic_window(tmp_path):
    from tests.window_fixture import A, Window, cancel_tx, order, place_tx
    w = Window(tmp_path)
    w.block([place_tx(A, "0xa", 1000, order(), order()), cancel_tx(A, "0xb", 1000, 0, 5)])
    window, _ = w.write()
    s = ev.tx_structure(window)
    assert s["order"] == {2: 1} and s["cancel"] == {1: 1}


def test_probe_summary_counts_oids_and_multi_item():
    rows = [dict(alo_items_in_tx=1, block=5, answer={"status": "order", "order": {"order": {"oid": 7}, "status": "open"}}),
            dict(alo_items_in_tx=3, block=9, answer={"status": "unknownOid"})]
    s = ev.probe_summary(rows)
    assert s["answered"] == 1 and s["answered_with_oid"] == 1 and s["multi_item_txs"] == 1 and s["blocks"] == (5, 9)
