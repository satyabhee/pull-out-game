"""analysis/order_audit.py: the pure parts (cut candidates, cut and move policies). The dev20 run itself needs data."""
from analysis.order_audit import candidate_cuts, cut_policy, move_policy
from engine_py.types import Cancel, Place, Side, Tif


def act(i, kind, tif=Tif.ALO):
    base = dict(user=f"u{i}", asset=0, block=1, tx_index=i, item_index=0, hash=f"h{i}", priority=None)
    if kind == "cancel":
        return Cancel(oid=1, **base)
    return Place(side=Side.BUY, px=100, sz=1, tif=tif, cloid=None, reduce_only=False, **base)


def test_candidate_cuts_keep_one_position_per_separated_pair_set():
    # taker 2 with later actors 5 and 9: cuts in 3..5 separate both pairs, 6..9 only (2, 9); one of each kept
    assert candidate_cuts({(2, 5), (2, 9)}) == [3, 9]
    assert candidate_cuts(set()) == []


def test_cut_policy_is_class_sort_per_batch():
    acts = [act(0, "place", Tif.IOC), act(1, "cancel"), act(2, "place", Tif.ALO)]
    order = lambda pol: [a.tx_index for a in pol(list(acts))]
    assert order(cut_policy([])) == [2, 1, 0]                 # one batch: ALO, cancel, IOC
    assert order(cut_policy([1])) == [0, 2, 1]                # cut after the IOC: it runs first
    assert order(cut_policy([1, 2])) == [0, 1, 2]             # every boundary cut = listing order


def test_move_policy_moves_one_tx_before_the_taker():
    acts = [act(0, "place", Tif.IOC), act(1, "cancel"), act(2, "place", Tif.ALO)]
    assert [a.tx_index for a in move_policy(2, 0)(acts)] == [2, 0, 1]
