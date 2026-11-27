from analysis.gate2_input import action_class, batches


def _o(tif):
    return {"a": 0, "b": True, "p": "1", "s": "1", "r": False, "t": {"limit": {"tif": tif}}}


def test_action_class():
    assert action_class({"type": "cancelByCloid", "cancels": []}) == "C"
    assert action_class({"type": "order", "orders": [_o("Alo"), _o("Alo")]}) == "A"
    assert action_class({"type": "order", "orders": [_o("Alo"), _o("Ioc")]}) == "G"
    assert action_class({"type": "order", "orders": [{"a": 0, "t": {"trigger": {}}}]}) == "G"
    assert action_class({"type": "batchModify", "modifies": [{"oid": 1, "order": _o("Alo")}]}) == "A"
    assert action_class({"type": "noop"}) == "O"


def test_batches_split_on_rank_drop():
    assert batches(list("CCAAGOCAG")) == [0, 0, 0, 0, 0, 0, 1, 1, 1]
    assert batches(list("CAGO")) == [0, 0, 0, 0]
