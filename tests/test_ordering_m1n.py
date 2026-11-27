"""M1n ordering (gate 2 OFF arm, docs/gate2.md): batches from class descents, then send-time order inside each batch,
no class sort; txs without a send time keep their slot."""
from engine_py.ordering import batches_of, load_send_times, m1n_policy, reused_txs
from engine_py.types import Cancel, Place, Side, Tif


def place(tx, h, tif=Tif.GTC, item=0, block=7):
    return Place(user="u", asset=0, block=block, tx_index=tx, item_index=item, hash=h, priority=None,
                 side=Side.BUY, px=100, sz=1, tif=tif, cloid=None, reduce_only=False)


def cancel(tx, h, block=7):
    return Cancel(user="m", asset=0, block=block, tx_index=tx, item_index=0, hash=h, priority=None, oid=5)


def test_batches_cut_at_each_class_descent():
    # chain ranks (Yuta #55): cancel 0, ALO 1, IOC 2 | cancel 0, IOC 2  -> one rank drop -> two batches
    acts = [cancel(0, "c1"), place(1, "a", Tif.ALO), place(2, "t1", Tif.IOC), cancel(3, "c2"), place(4, "t2", Tif.IOC)]
    assert batches_of(acts) == {0: 0, 1: 0, 2: 0, 3: 1, 4: 1}


def test_taker_sent_first_moves_ahead_of_the_cancel():
    acts = [cancel(0, "c"), place(1, "t", Tif.IOC)]          # chain: cancel first (class sort)
    pol = m1n_policy({(7, "c"): 1_000, (7, "t"): 990})       # but the taker was sent 10 ms earlier
    assert [a.hash for a in pol(acts)] == ["t", "c"]
    assert pol.stats["txs_moved"] == 2 and pol.stats["txs_timed"] == 2


def test_no_send_time_keeps_its_slot_and_input_is_not_mutated():
    acts = [cancel(0, "c"), place(1, "x", Tif.ALO), place(2, "t", Tif.IOC)]
    before = list(acts)
    pol = m1n_policy({(7, "c"): 50, (7, "t"): 10})          # "x" untimed (stale nonce): keeps its slot
    assert [a.hash for a in pol(acts)] == ["t", "x", "c"]
    assert acts == before
    assert pol.stats["txs_untimed"] == 1


def test_never_reorders_across_batches():
    acts = [cancel(0, "c1"), place(1, "t1", Tif.IOC), cancel(2, "c2"), place(3, "t2", Tif.IOC)]
    pol = m1n_policy({(7, "c1"): 5, (7, "t1"): 4, (7, "c2"): 1, (7, "t2"): 0})
    assert [a.hash for a in pol(acts)] == ["t1", "c1", "t2", "c2"]


def test_items_of_one_tx_stay_together_in_order_and_ties_keep_listing_order():
    acts = [cancel(0, "c"), place(1, "t", Tif.IOC, item=0), place(1, "t", Tif.IOC, item=1), place(2, "u", Tif.IOC)]
    pol = m1n_policy({(7, "c"): 5, (7, "t"): 5, (7, "u"): 1})
    assert [(a.hash, a.item_index) for a in pol(acts)] == [("u", 0), ("c", 0), ("t", 0), ("t", 1)]


def test_blocks_without_send_times_are_unchanged():
    acts = [cancel(0, "c"), place(1, "t", Tif.IOC)]
    pol = m1n_policy({(8, "c"): 1})
    assert pol(acts) == acts and pol.stats["blocks_untouched"] == 1


def test_load_send_times(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("height,tx_hash,item_index,user,nonce,send_ms,calibrated\n"
                 "7,0xAB,0,0xu,100,95,1\n7,0xab,1,0xu,100,95,1\n8,0xcd,0,0xv,200,,0\n")
    assert load_send_times(p) == {(7, "0xab"): (95, None), (8, "0xcd"): (200, None)}   # empty send_ms -> raw nonce


def test_file_batches_are_used_and_must_be_contiguous(tmp_path):
    acts = [cancel(0, "c1"), place(1, "t1", Tif.IOC), cancel(2, "c2"), place(3, "t2", Tif.IOC)]
    one = m1n_policy({(7, "c1"): (5, 0), (7, "t1"): (4, 0), (7, "c2"): (1, 0), (7, "t2"): (0, 0)})
    assert [a.hash for a in one(acts)] == ["t2", "c2", "t1", "c1"]           # file says one batch: sort all four
    bad = m1n_policy({(7, "c1"): (5, 1), (7, "t1"): (4, 0)})
    import pytest
    with pytest.raises(ValueError):
        bad(acts)


def test_reused_txs(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("height,tx_hash,item_index,user,nonce,send_ms,calibrated\n"
                 "7,0xa,0,0xU,100,95,1\n7,0xb,0,0xu,100,95,1\n7,0xc,0,0xu,101,96,1\n8,0xd,0,0xu,100,95,1\n")
    assert reused_txs(p) == {(7, "0xa"), (7, "0xb")}


def test_load_send_times_accepts_integer_valued_decimals_and_rejects_fractions(tmp_path):
    # A5.2: the extension file writes pooled-offset rows as '1795101766784.0'
    import pytest
    p = tmp_path / "s.csv"
    p.write_text("height,tx_hash,item_index,user,nonce,send_ms,calibrated\n7,0xa,0,0xu,100,95.0,2\n")
    assert load_send_times(p) == {(7, "0xa"): (95, None)}
    p.write_text("height,tx_hash,item_index,user,nonce,send_ms,calibrated\n7,0xa,0,0xu,100,95.5,2\n")
    with pytest.raises(ValueError):
        load_send_times(p)
