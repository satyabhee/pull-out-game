"""collect/alo_probe.py: which items of a live tx get sampled."""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collect"))
from alo_probe import alo_items  # noqa: E402

ALO = {"limit": {"tif": "Alo"}}


def tx(action, error=None):
    return {"user": "0xAB", "action": action, "error": error, "time": 1, "block": 2, "hash": "0x1"}


def test_picks_btc_eth_alo_items_with_cloid_and_keeps_item_numbering():
    orders = [{"a": 5, "t": ALO, "c": "0x1"}, {"a": 0, "t": ALO}, {"a": 1, "t": ALO, "c": "0x2"},
              {"a": 0, "t": {"limit": {"tif": "Gtc"}}, "c": "0x3"}]
    got = alo_items(tx({"type": "order", "orders": orders}))
    assert [(i, o["c"]) for i, o in got] == [(2, "0x2")]


def test_modify_and_batch_modify():
    assert len(alo_items(tx({"type": "modify", "oid": 1, "order": {"a": 0, "t": ALO, "c": "0x9"}}))) == 1
    bm = {"type": "batchModify", "modifies": [{"oid": 1, "order": {"a": 1, "t": ALO, "c": "0x8"}}]}
    assert [i for i, _ in alo_items(tx(bm))] == [0]


def test_errored_tx_triggers_and_other_actions_skipped():
    assert alo_items(tx({"type": "order", "orders": [{"a": 0, "t": ALO, "c": "0x1"}]}, error="x")) == []
    assert alo_items(tx({"type": "order", "orders": [{"a": 0, "t": {"trigger": {}}, "c": "0x1"}]})) == []
    assert alo_items(tx({"type": "cancel", "cancels": []})) == []
    assert alo_items("not a tx") == []
