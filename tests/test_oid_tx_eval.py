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


# ------------------------------------------------------------------ cross-tx grouping (agent 3's joint layers)
from engine_py.types import Cancel  # noqa: E402


class GWorld:
    """Users place single orders at counts ks (true oid = base + k + drift(k)); in block 2 each user cancels
    a group of them. `cancel_group` returns {engine oid: result} with truth known by construction."""

    def __init__(self, orders, groups, drift_per_mille=0, anchors=None, walk_seed=None, **kw):
        """orders: [(user, k)]; groups: {user: [order indices cancelled in block 2, in tx order]}."""
        self.base = 5_000_000
        self.true = [self.base + k + k * drift_per_mille // 1000 for _, k in orders]
        if walk_seed is not None:                      # invisible ids as a random walk (Poisson-like), not a straight line
            rng, extra, last = random.Random(walk_seed), 0, 0
            for i in sorted(range(len(orders)), key=lambda j: orders[j][1]):
                dk = orders[i][1] - last
                extra += sum(1 for _ in range(dk) if rng.random() < drift_per_mille / 1000)
                last = orders[i][1]
                self.true[i] = self.base + orders[i][1] + extra
        counts = {(1, i, 0): k for i, (_, k) in enumerate(orders)}
        kmax = max(k for _, k in orders) + 1000
        counts[(1, len(orders), 0)] = kmax
        anchors = anchors or [(0, self.base), (kmax, self.base + kmax + kmax * drift_per_mille // 1000)]
        anchors = sorted(set(anchors))
        bg = {(2, u): [(0, self.true[i]) for i in idx] for u, idx in groups.items()}
        self.est = OidEstimator(counts, anchors, block_groups=bg)
        self.eng = Engine()
        self.res = OidResolver(self.eng, self.est, **kw)
        self.eng.resolve_oid = self.res
        self.eo = []
        for i, (u, k) in enumerate(orders):
            a = Place(user=u, block=1, asset=0, side=Side.BUY, px=1000 + i, sz=1, tif=Tif.ALO, cloid=None,
                      reduce_only=False, tx_index=i, item_index=0, hash="0x1", priority=None)
            before = self.eng.next_oid
            self.eng.apply(a)
            self.res.note(a, before)
            self.eo.append(before)

    def cancel(self, i, user):
        """Cancel order i by its true hl oid in block 2; returns the engine oid that left the book or None."""
        before = set(self.eng.books[0].index) if hasattr(self.eng.books[0], "index") else None
        self.eng.apply(Cancel(user=user, block=2, asset=0, oid=self.true[i], tx_index=0, item_index=0,
                              hash="0x2", priority=None))
        return self.eo[i] not in self.eng.books[0]

    def run(self, groups):
        """-> (correct, wrong): a cancel is wrong when another order of that user (or user) left the book."""
        correct = wrong = 0
        for u, idx in groups.items():
            for i in idx:
                snap = {e for e in self.eo if e in self.eng.books[0]}
                self.cancel(i, u)
                gone = snap - {e for e in self.eo if e in self.eng.books[0]}
                if gone == {self.eo[i]}:
                    correct += 1
                elif gone:
                    wrong += 1
        return correct, wrong


def test_xtx_two_users_interleaved_same_counts_no_wrong():
    orders = [("u", 100), ("v", 101), ("u", 130), ("v", 131), ("u", 170), ("v", 171)]
    groups = {"u": [0, 2, 4], "v": [1, 3, 5]}
    w = GWorld(orders, groups)
    c, wrong = w.run(groups)
    assert wrong == 0


def test_xtx_two_clusters_identical_spacing_never_resolves():
    # u rests two clusters with identical internal spacing 25 ids apart; the cancel group names one of them.
    orders = [("u", 100), ("u", 120), ("u", 160), ("u", 125), ("u", 145), ("u", 185)]
    groups = {"u": [0, 1, 2]}
    w = GWorld(orders, groups)
    c, wrong = w.run(groups)
    assert wrong == 0 and c == 0, "ambiguous clusters must abstain, never guess"


@pytest.mark.parametrize("per_mille", [10, 30, 50])
def test_xtx_drift_one_to_five_percent_no_wrong(per_mille):
    orders = [("u", 1000 + 200 * i) for i in range(6)] + [("u", 1100 + 200 * i) for i in range(6)]
    groups = {"u": [0, 2, 4, 6]}
    w = GWorld(orders, groups, drift_per_mille=per_mille)
    assert w.run(groups)[1] == 0


def test_xtx_group_with_one_member_evicted_by_reseed_no_wrong():
    orders = [("u", 100), ("u", 120), ("u", 160), ("u", 124)]
    groups = {"u": [0, 1, 2]}
    w = GWorld(orders, groups)
    w.eng.evict(w.eo[1])                                 # a re-seed dropped the middle order
    snap_before = {e for e in w.eo if e in w.eng.books[0]}
    for i in (0, 1, 2):
        w.cancel(i, "u")
    gone = snap_before - {e for e in w.eo if e in w.eng.books[0]}
    assert gone <= {w.eo[0], w.eo[2]}                    # never the decoy at k=124 (a wrong neighbour)


@pytest.mark.parametrize("seed", range(20))
def test_xtx_random_worlds_zero_wrong(seed):
    rng = random.Random(1000 + seed)
    users = ["u1", "u2", "u3"]
    ks = sorted(rng.sample(range(50, 1900), 40))
    orders = [(rng.choice(users), k) for k in ks]
    groups = {}
    for u in users:
        mine = [i for i, (uu, _) in enumerate(orders) if uu == u]
        groups[u] = sorted(rng.sample(mine, min(len(mine), rng.randint(2, 5))))
    w = GWorld(orders, groups, drift_per_mille=rng.choice([0, 10, 30, 50]))
    assert w.run(groups)[1] == 0


@pytest.mark.parametrize("seed", range(20))
def test_xtx_random_walk_drift_zero_wrong(seed):
    rng = random.Random(5000 + seed)
    users = ["u1", "u2"]
    orders = [(rng.choice(users), k) for k in sorted(rng.sample(range(50, 1900), 50))]
    groups = {}
    for u in users:
        mine = [i for i, (uu, _) in enumerate(orders) if uu == u]
        groups[u] = sorted(rng.sample(mine, min(len(mine), rng.randint(2, 6))))
    w = GWorld(orders, groups, drift_per_mille=rng.choice([10, 30, 50]), walk_seed=seed)
    assert w.run(groups)[1] == 0


def _stress_world(seed):
    rng = random.Random(seed)
    users = ["u1", "u2", "u3"]
    n = rng.choice([15, 40, 80])
    orders = [(rng.choice(users), k) for k in sorted(rng.sample(range(50, 1900), n))]
    groups = {}
    for u in users:
        mine = [i for i, (uu, _) in enumerate(orders) if uu == u]
        if len(mine) >= 2:
            groups[u] = sorted(rng.sample(mine, min(len(mine), rng.randint(2, 6))))
    return GWorld(orders, groups, drift_per_mille=rng.choice([10, 30, 50]), walk_seed=seed), groups


# seeds that produced wrong resolutions before agent3's margin guard (c038b42); now must be 0
@pytest.mark.parametrize("seed", [392, 838, 961])
def test_xtx_known_wrong_resolution_seeds(seed):
    w, groups = _stress_world(seed)
    assert w.run(groups)[1] == 0


def test_locate_anchors_finds_the_unique_placement(tmp_path):
    from tests.window_fixture import A, Window, order, place_tx
    w = Window(tmp_path)
    h = w.block([place_tx(A, "0xa", 1000, order(px="60000", sz="0.001", cloid="0x01"), order(px="60001", sz="0.001"))])
    ans = {"status": "order", "order": {"status": "canceled", "order": {
        "coin": "BTC", "side": "B", "limitPx": "60001.0", "origSz": "0.001", "oid": 777,
        "timestamp": w.time_of(h)}}}
    w.anchors = [{"user": A, "oid": 777, "answer": ans}]
    window, _ = w.write()
    loc = ev.locate_anchors(window)
    assert loc == [dict(user=A, oid=777, pos=(h, 0, 1), k=1)]
