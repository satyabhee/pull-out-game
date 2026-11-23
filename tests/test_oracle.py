"""ORACLE switches (look-ahead by design, diagnostic only): decisions from a chain-truth status CSV."""
from engine_py.oracle import Oracle


class A:
    def __init__(self, h, item):
        self.hash, self.item_index = h, item


def _csv(tmp_path):
    p = tmp_path / "status.csv"
    p.write_text("set,chunk,user,tx_hash,item,oid,status,status_time_ms\n"
                 "sim,c1,0xa,0xAA,0,11,badAloPxRejected,\n"
                 "sim,c1,0xa,0xbb,1,12,iocCancelRejected,\n"
                 "sim,c1,0xa,0xcc,0,13,perpMarginRejected,\n"
                 "sim,c1,0xa,0xdd,0,14,reduceOnlyCanceled,5000\n"
                 "sim,c2,0xa,0xee,0,15,badAloPxRejected,\n")
    return p


def test_oracle_decisions(tmp_path):
    o = Oracle(_csv(tmp_path), "c1", {"status", "cancel", "ioc"})
    assert o.skip_place(A("0xaa", 0), is_ioc=False) == "oracle_status_reject"      # hash case-insensitive
    assert o.skip_place(A("0xbb", 1), is_ioc=True) is None                         # accepted, nothing matched
    assert o.ioc_known_accepted(A("0xbb", 1))
    assert o.skip_place(A("0xcc", 0), is_ioc=True) == "oracle_ioc_reject"
    assert o.skip_place(A("0xee", 0), is_ioc=False) is None                        # other chunk filtered out
    o.note_created(A("0xdd", 0), None, 7)
    assert o.cancel_target(14) == 7 and o.cancel_target(99) is None
    assert o.due_cancels(4999) == [] and o.due_cancels(5000) == [7]


def test_oracle_kinds_are_separate(tmp_path):
    o = Oracle(_csv(tmp_path), "c1", {"ioc"})
    assert o.skip_place(A("0xaa", 0), is_ioc=False) is None                        # O1 off
    assert o.cancel_target(14) is None                                             # O3 off
