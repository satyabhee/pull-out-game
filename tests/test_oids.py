"""data/oids.py: oid estimation from anchors and the never-guess int-oid resolver."""
import pytest

from data.oids import OidEstimator, OidResolver
from engine_py.matching import Engine
from engine_py.types import Cancel, Place, Side, Tif

META = dict(block=1, tx_index=0, item_index=0, hash="0x1", priority=None)


def est(anchors, counts=None):
    return OidEstimator(counts or {}, anchors)


def test_interpolates_between_anchors_with_integers():
    e = est([(0, 1000), (100, 1110)])          # 110 ids for 100 counted items
    assert e.estimate_count(50) == 1055
    assert e.estimate_count(0) == 1000 and e.estimate_count(100) == 1110
    assert isinstance(e.estimate_count(33), int)


def test_extrapolates_with_nearest_pair_slope():
    e = est([(0, 1000), (100, 1110), (200, 1210)])
    assert e.estimate_count(300) == 1310       # slope of the last pair (1.0)
    assert e.estimate_count(-100) == 890       # slope of the first pair (1.1)


def test_single_anchor_uses_slope_one_and_no_anchor_gives_none():
    assert est([(10, 500)]).estimate_count(15) == 505
    assert est([]).estimate_count(15) is None


def test_inconsistent_anchors_rejected():
    with pytest.raises(ValueError):
        est([(0, 1000), (10, 990)])


def test_leave_one_out_on_a_straight_line_is_exact():
    e = est([(k, 1000 + 2 * k) for k in (0, 10, 20, 30)])
    assert e.leave_one_out() == [0, 0, 0, 0]


def test_tolerance_grows_with_distance_to_anchor():
    e = est([(0, 1000), (1000, 2000)])
    assert e.tolerance(0) == 50 and e.tolerance(500) == 75


def place(eng, res, user, pos, px, side=Side.BUY):
    p = Place(user=user, block=pos[0], tx_index=pos[1], item_index=pos[2], hash="0x1", priority=None,
              asset=0, side=side, px=px, sz=1, tif=Tif.ALO, cloid=None, reduce_only=False)
    before = eng.next_oid
    eng.apply(p)
    res.note(p, before)
    return before


def setup(counts, anchors):
    eng = Engine()
    res = OidResolver(eng, OidEstimator(counts, anchors))
    eng.resolve_oid = res
    return eng, res


def test_resolves_unique_candidate_and_cancels_it():
    counts = {(1, 0, 0): 0, (1, 1, 0): 1000}
    eng, res = setup(counts, [(0, 5000), (1000, 6000)])
    oid = place(eng, res, "u", (1, 1, 0), 100)
    _, rej = eng.apply(Cancel(user="u", **{**META, "block": 2}, asset=0, oid=6003))
    assert rej == [] and oid not in eng.books[0] and res.stats["resolved"] == 1 and res.last == oid


def test_two_candidates_within_tolerance_is_ambiguous_never_guessed():
    counts = {(1, 0, 0): 0, (1, 1, 0): 1000, (1, 2, 0): 1010}
    eng, res = setup(counts, [(0, 5000), (2000, 7000)])
    a = place(eng, res, "u", (1, 1, 0), 100)
    b = place(eng, res, "u", (1, 2, 0), 99)
    _, rej = eng.apply(Cancel(user="u", **META, asset=0, oid=6005))
    assert len(rej) == 1 and a in eng.books[0] and b in eng.books[0]
    assert res.stats["ambiguous"] == 1


def test_other_users_orders_are_never_candidates():
    counts = {(1, 0, 0): 0, (1, 1, 0): 1000}
    eng, res = setup(counts, [(0, 5000), (1000, 6000)])
    place(eng, res, "someone_else", (1, 1, 0), 100)
    _, rej = eng.apply(Cancel(user="u", **META, asset=0, oid=6000))
    assert len(rej) == 1 and res.stats["no_candidate"] == 1


def test_oid_before_the_window_is_prewindow():
    eng, res = setup({(1, 0, 0): 0}, [(0, 5000)])
    _, rej = eng.apply(Cancel(user="u", **META, asset=0, oid=4000))
    assert len(rej) == 1 and res.stats["prewindow"] == 1


def test_from_files_is_built_once_per_process_and_rebuilt_when_an_input_changes(tmp_path, monkeypatch):
    import json
    from data import oids, parse as P
    from tests.window_fixture import A, Window, order, place_tx
    oids.clear_cache()
    w = Window(tmp_path)
    w.block([place_tx(A, "0xa", 0, order())])
    window, _ = w.write()
    anchors = window / "anchors.json"
    anchors.write_text(json.dumps([{"oid": 500, "height": 1000, "tx": 0, "item": 0}]))
    reads = []
    real = P.read_block
    monkeypatch.setattr(oids, "read_block", lambda p: reads.append(p) or real(p))
    e1 = OidEstimator.from_files(window, anchors)
    e2 = OidEstimator.from_files(window, anchors)
    assert e1 is e2 and len(reads) == 1 and e1.anchors == [(0, 500)]
    anchors.write_text(json.dumps([{"oid": 600, "height": 1000, "tx": 0, "item": 0}]))
    assert OidEstimator.from_files(window, anchors).anchors == [(0, 600)] and len(reads) == 2
    oids.clear_cache()


# ------------------------------------------------------------------ joint resolution (agent 3)
def joint_setup(ks, anchors, block_items, nearest_ratio=0, **kw):
    """User 'u' places one order per count k in ks (block 1, one tx each); block_items = the user's
    int-oid cancel items of block 2."""
    counts = {(1, i, 0): k for i, k in enumerate(ks)}
    counts[(1, 99, 0)] = max(a[0] for a in anchors)
    e = OidEstimator(counts, anchors, block_groups={(2, "u"): [(0, o) for o in block_items]})
    eng = Engine()
    res = OidResolver(eng, e, nearest_ratio=nearest_ratio, **kw)
    eng.resolve_oid = res
    placed = [place(eng, res, "u", (1, i, 0), 100 + i) for i in range(len(ks))]
    return eng, res, placed


def cancel2(eng, o):
    return eng.apply(Cancel(user="u", **{**META, "block": 2}, asset=0, oid=o))[1]


def test_block_group_spacing_resolves_what_the_per_order_rule_calls_ambiguous():
    # estimates 5000, 5020, 5060: cancel 5000 has two candidates alone, but with 5060 only (X1, X3) is consistent
    eng, res, (x1, x2, x3) = joint_setup([0, 20, 60], [(0, 5000), (1000, 6000)], [5000, 5060])
    assert cancel2(eng, 5000) == [] and x1 not in eng.books[0]
    assert cancel2(eng, 5060) == [] and x3 not in eng.books[0] and x2 in eng.books[0]
    assert res.stats["resolved_block"] == 2 and res.stats["resolved"] == 2 and res.stats["ambiguous"] == 0


def test_without_the_joint_layer_the_same_cancel_is_ambiguous():
    eng, res, (x1, x2, x3) = joint_setup([0, 20, 60], [(0, 5000), (1000, 6000)], [5000, 5060], joint_block=False)
    assert len(cancel2(eng, 5000)) == 1 and x1 in eng.books[0] and res.stats["ambiguous"] == 1


def test_two_consistent_assignments_fall_back_and_never_guess():
    # adjacent counts 0 and 1 are within the pair slack: (X1, X3) and (X2, X3) both fit
    eng, res, (x1, x2, x3) = joint_setup([0, 1, 40], [(0, 5000), (1000, 6000)], [5000, 5040])
    assert len(cancel2(eng, 5000)) == 1
    assert x1 in eng.books[0] and x2 in eng.books[0]
    assert res.stats["resolved_block"] == 0 and res.stats["block_conflict"] >= 1


def test_drift_inside_the_slack_still_matches_and_outside_does_not():
    # 1% invisible ids between counts 0 and 60: cancel oids 5000 and 5061 (d=1) resolve; 5000 and 5080 do not
    eng, res, (x1, x2, x3) = joint_setup([0, 20, 60], [(0, 5000), (1000, 6000)], [5000, 5061])
    assert cancel2(eng, 5000) == [] and x1 not in eng.books[0]
    eng, res, (x1, x2, x3) = joint_setup([0, 20, 60], [(0, 5000), (1000, 6000)], [5000, 5080])
    assert len(cancel2(eng, 5000)) == 1 and res.stats["resolved_block"] == 0


def test_a_single_item_group_is_not_joint():
    eng, res, _ = joint_setup([0, 20, 60], [(0, 5000), (1000, 6000)], [5000])
    assert len(cancel2(eng, 5000)) == 1 and res.stats["resolved_block"] == 0


def test_estimator_reads_block_groups_from_blocks(tmp_path):
    from tests.window_fixture import A, Window, cancel_tx, order, place_tx
    w = Window(tmp_path)
    w.block([place_tx(A, "0xa", 0, order(), order()), cancel_tx(A, "0xb", 0, 0, 111), cancel_tx(A, "0xc", 0, 0, 222),
             cancel_tx("0xother", "0xd", 0, 0, 333)])
    window, _ = w.write()
    est = OidEstimator.from_window(window, [])
    assert sorted(est.block_groups[(1000, A.lower())]) == [(0, 111), (0, 222)]
    assert est.groups == {}                         # no tx holds two oids


def test_in_tx_group_uses_exact_spacing_inside_a_placement_tx():
    # one placement tx with items at counts 0..2 (consecutive oids), plus a distractor tx; cancel tx holds oids o, o+2
    counts = {(1, 0, 0): 0, (1, 0, 1): 1, (1, 0, 2): 2, (1, 1, 0): 3, (1, 9, 0): 1000}
    e = OidEstimator(counts, [(0, 5000), (1000, 6000)], groups={(2, "u"): [[(0, 5000), (0, 5002)]]})
    eng = Engine()
    res = OidResolver(eng, e, joint_block=False)
    eng.resolve_oid = res
    xs = [place(eng, res, "u", p, 100 + i) for i, p in enumerate([(1, 0, 0), (1, 0, 1), (1, 0, 2), (1, 1, 0)])]
    assert cancel2(eng, 5000) == [] and xs[0] not in eng.books[0]
    assert cancel2(eng, 5002) == [] and xs[2] not in eng.books[0] and xs[1] in eng.books[0]
    assert res.stats["resolved_group"] == 2


# ------------------------------------------- stress worlds with random-walk drift (copied from agent 4's repro)
import random as _random


class GWorld:
    """Users place single orders at counts (true oid = base + k + random-walk invisible ids) and cancel groups
    of them in block 2; truth is known by construction."""

    def __init__(self, orders, groups, drift_per_mille, walk_seed, **kw):
        self.base = 5_000_000
        rng, extra, last = _random.Random(walk_seed), 0, 0
        self.true = [0] * len(orders)
        for i in sorted(range(len(orders)), key=lambda j: orders[j][1]):
            extra += sum(1 for _ in range(orders[i][1] - last) if rng.random() < drift_per_mille / 1000)
            last = orders[i][1]
            self.true[i] = self.base + orders[i][1] + extra
        counts = {(1, i, 0): k for i, (_, k) in enumerate(orders)}
        kmax = max(k for _, k in orders) + 1000
        counts[(1, len(orders), 0)] = kmax
        anchors = [(0, self.base), (kmax, self.base + kmax + kmax * drift_per_mille // 1000)]
        bg = {(2, u): [(0, self.true[i]) for i in idx] for u, idx in groups.items()}
        self.eng = Engine()
        self.res = OidResolver(self.eng, OidEstimator(counts, anchors, block_groups=bg), **kw)
        self.eng.resolve_oid = self.res
        self.eo = []
        for i, (u, k) in enumerate(orders):
            a = Place(user=u, block=1, asset=0, side=Side.BUY, px=1000 + i, sz=1, tif=Tif.ALO, cloid=None,
                      reduce_only=False, tx_index=i, item_index=0, hash="0x1", priority=None)
            before = self.eng.next_oid
            self.eng.apply(a)
            self.res.note(a, before)
            self.eo.append(before)

    def run(self, groups):
        correct = wrong = 0
        for u, idx in groups.items():
            for i in idx:
                snap = {e for e in self.eo if e in self.eng.books[0]}
                self.eng.apply(Cancel(user=u, block=2, asset=0, oid=self.true[i], tx_index=0, item_index=0,
                                      hash="0x2", priority=None))
                gone = snap - {e for e in self.eo if e in self.eng.books[0]}
                if gone == {self.eo[i]}:
                    correct += 1
                elif gone:
                    wrong += 1
        return correct, wrong


def stress_world(seed, **kw):
    rng = _random.Random(seed)
    users = ["u1", "u2", "u3"]
    n = rng.choice([15, 40, 80])
    orders = [(rng.choice(users), k) for k in sorted(rng.sample(range(50, 1900), n))]
    groups = {}
    for u in users:
        mine = [i for i, (uu, _) in enumerate(orders) if uu == u]
        if len(mine) >= 2:
            groups[u] = sorted(rng.sample(mine, min(len(mine), rng.randint(2, 6))))
    return GWorld(orders, groups, rng.choice([10, 30, 50]), seed, **kw), groups


@pytest.mark.parametrize("seed", [392, 838, 961])
def test_margin_guard_removes_the_known_wrong_resolutions(seed):
    w, groups = stress_world(seed)
    assert w.run(groups)[1] == 0


def test_stress_worlds_have_zero_wrong_and_the_joint_layer_still_fires():
    correct = wrong = fired = 0
    for seed in range(1500):
        w, groups = stress_world(seed)
        c, bad = w.run(groups)
        correct, wrong, fired = correct + c, wrong + bad, fired + w.res.stats["resolved_block"]
    assert wrong == 0 and fired > 0


def ghost_setup(ks_resting, ks_gone, ghost_guard=True):
    """User 'u' placed orders at counts ks_resting (they rest) and ks_gone (already left the book)."""
    allk = sorted(ks_resting + ks_gone)
    counts = {(1, i, 0): k for i, k in enumerate(allk)}
    e = OidEstimator(counts, [(0, 5000), (1000, 6000)], placers={("u", 0): allk})
    eng = Engine()
    res = OidResolver(eng, e, ghost_guard=ghost_guard)
    eng.resolve_oid = res
    rest = {}
    for i, k in enumerate(allk):
        if k in ks_resting:
            rest[k] = place(eng, res, "u", (1, i, 0), 100 + i)
    return eng, res, rest


def test_ghost_guard_abstains_when_a_departed_placement_is_also_within_tolerance():
    eng, res, rest = ghost_setup([10], [30])            # estimates 5010 and 5030, tolerance 50
    assert len(cancel2(eng, 5010)) == 1 and rest[10] in eng.books[0]
    assert res.stats["ghost"] == 1 and res.stats["resolved"] == 0


def test_ghost_guard_is_silent_when_the_candidate_is_the_only_placement_in_range():
    eng, res, rest = ghost_setup([10], [400])
    assert cancel2(eng, 5010) == [] and rest[10] not in eng.books[0] and res.stats["ghost"] == 0


def test_ghost_guard_can_be_turned_off():
    eng, res, rest = ghost_setup([10], [30], ghost_guard=False)
    assert cancel2(eng, 5010) == [] and res.stats["resolved"] == 1


def test_estimator_collects_every_placement_per_user_and_asset(tmp_path):
    from tests.window_fixture import A, Window, order, place_tx
    w = Window(tmp_path)
    w.block([place_tx(A, "0xa", 0, order(), order(asset=1)), place_tx(A, "0xb", 0, order())])
    window, _ = w.write()
    est = OidEstimator.from_window(window, [])
    assert est.placers[(A.lower(), 0)] == [0, 2] and est.placers[(A.lower(), 1)] == [1]
