"""Gate 2 A6 orderings (docs/gate2.md A6): cancels-last and seeded random, M1n's batches and movable set."""
import pytest

from engine_py.ordering import a6_policy, m1n_policy
from engine_py.types import Cancel, Place, Side, Tif


def place(tx, h, tif=Tif.GTC, item=0, block=7):
    return Place(user="u", asset=0, block=block, tx_index=tx, item_index=item, hash=h, priority=None,
                 side=Side.BUY, px=100, sz=1, tif=tif, cloid=None, reduce_only=False)


def cancel(tx, h, block=7):
    return Cancel(user="m", asset=0, block=block, tx_index=tx, item_index=0, hash=h, priority=None, oid=5)


def test_cancels_last_puts_gtc_ioc_first_then_alo_then_cancels_inside_a_batch():
    # chain batch: C C A G G (class-sorted); cancels-last: G G A C C, ties in listing order
    acts = [cancel(0, "c1"), cancel(1, "c2"), place(2, "a", Tif.ALO), place(3, "g1", Tif.IOC), place(4, "g2", Tif.GTC)]
    send = {(7, h): 1 for h in ("c1", "c2", "a", "g1", "g2")}
    pol = a6_policy(send, "cancels_last")
    assert [a.hash for a in pol(acts)] == ["g1", "g2", "a", "c1", "c2"]


def test_cancels_last_never_crosses_batches_and_untimed_keep_slots():
    acts = [cancel(0, "c1"), place(1, "x", Tif.ALO), place(2, "t1", Tif.IOC), cancel(3, "c2"), place(4, "t2", Tif.IOC)]
    send = {(7, "c1"): (1, 0), (7, "t1"): (1, 0), (7, "c2"): (1, 1), (7, "t2"): (1, 1)}   # "x" untimed
    before = list(acts)
    assert [a.hash for a in a6_policy(send, "cancels_last")(acts)] == ["t1", "x", "c1", "t2", "c2"]
    assert acts == before


def test_random_is_a_seeded_permutation_inside_batches_and_repeats():
    acts = [cancel(i, f"c{i}") for i in range(6)] + [cancel(6 + i, f"d{i}") for i in range(4)]
    send = {(7, f"c{i}"): (1, 0) for i in range(6)} | {(7, f"d{i}"): (1, 1) for i in range(4)}
    out = [a.hash for a in a6_policy(send, "random", seed=3)(acts)]
    assert sorted(out[:6]) == [f"c{i}" for i in range(6)] and sorted(out[6:]) == [f"d{i}" for i in range(4)]
    assert out == [a.hash for a in a6_policy(send, "random", seed=3)(acts)]            # same seed, same order
    outs = {tuple(a.hash for a in a6_policy(send, "random", seed=s)(acts)) for s in range(20)}
    assert len(outs) > 1                                                                # seeds differ


def test_items_stay_together_and_blocks_without_times_are_unchanged():
    acts = [cancel(0, "c"), place(1, "t", Tif.IOC, item=0), place(1, "t", Tif.IOC, item=1)]
    send = {(7, "c"): 1, (7, "t"): 1}
    assert [(a.hash, a.item_index) for a in a6_policy(send, "cancels_last")(acts)] == [("t", 0), ("t", 1), ("c", 0)]
    pol = a6_policy({(8, "c"): 1}, "random", seed=0)
    assert pol(acts) == acts and pol.stats["blocks_untouched"] == 1


def test_bad_modes_fail_loudly_and_m1n_is_untouched():
    with pytest.raises(ValueError):
        a6_policy({}, "fifo")
    with pytest.raises(ValueError):
        a6_policy({}, "random")
    acts = [cancel(0, "c"), place(1, "t", Tif.IOC)]
    assert [a.hash for a in m1n_policy({(7, "c"): 1_000, (7, "t"): 990})(acts)] == ["t", "c"]
