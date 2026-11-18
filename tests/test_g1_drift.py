"""analysis/g1_drift.py: the drop-reduce-only measurement policy (moved onto engine_py/replay)."""
from collections import Counter

from analysis.g1_drift import drop_reduce_only_policy
from engine_py.types import Cancel, CancelByCloid, Modify, Place, Side, Tif

META = dict(user="0xa", block=1, tx_index=0, priority=None, asset=0, hash="0x1")


def place(i, ro, tif=Tif.ALO):
    return Place(**META, item_index=i, side=Side.SELL, px=10, sz=1, tif=tif, cloid=None, reduce_only=ro)


def test_drops_resting_reduce_only_and_keeps_a_modifys_cancel_half():
    keep, ro, ro_ioc = place(0, False), place(1, True), place(2, True, Tif.IOC)
    mod_cloid = Modify(**META, item_index=3, target="0xc", new=place(3, True))
    mod_oid = Modify(**META, item_index=4, target=77, new=place(4, True))
    n = Counter()
    out = drop_reduce_only_policy(n)([keep, ro, ro_ioc, mod_cloid, mod_oid])
    assert out[:2] == [keep, ro_ioc]
    assert isinstance(out[2], CancelByCloid) and out[2].cloid == "0xc" and out[2].item_index == 3
    assert isinstance(out[3], Cancel) and out[3].oid == 77
    assert n["dropped_reduce_only"] == 3
