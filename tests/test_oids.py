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
