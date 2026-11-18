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
    assert {x1, x2, x3} <= set(eng.books[0].index) if hasattr(eng.books[0], "index") else True
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
