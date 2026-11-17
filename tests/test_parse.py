"""Decoder tests, built from real txs and trades (live window, Nov 17)."""

import gzip
import json

import pytest

from data.parse import Stats, iter_trades, iter_window, nearest_book
from engine_py.types import (
    Cancel, CancelByCloid, Modify, OutsideFill, Place, ScheduleCancel, Side, Tif, ZERO_HASH,
)

TAKER = "0xf6d9174125c78eebf3ce880afd704bd1dcc33e13"
MAKER = "0xa3b5671871105a2aa47be6a5a76c68de70ed0600"
TX_HASH = "0x0f62fec9909e1dbf10dc0448f8b4980209d100af2b913c91b32baa1c4f91f7a9"
T = 1794894400551


def tx(action, user=TAKER, h="0x01", error=None, block=1224258712):
    return {"time": T, "user": user, "action": action, "block": block, "hash": h, "error": error}


# Real tx (block 1224258712, tx 4287): BTC IOC sell that took liquidity.
REAL_IOC = tx({"type": "order", "grouping": "na", "orders": [
    {"a": 0, "b": False, "p": "64764", "s": "0.00906", "r": False,
     "t": {"limit": {"tif": "Ioc"}}, "c": "0x77541557f21a4b11b7d6c674286c55dd"}]}, h=TX_HASH)


def write_block(d, height, txs, time_ms=T):
    body = {"type": "blockDetails", "blockDetails": {
        "height": height, "blockTime": time_ms, "hash": "0xb", "proposer": "0xp",
        "numTxs": len(txs), "txs": txs}}
    with gzip.open(d / f"{height}.json.gz", "wt") as f:
        json.dump(body, f)


def one_block(tmp_path, txs, **kw):
    write_block(tmp_path, 1224258712, txs)
    stats = Stats()
    blocks = list(iter_window(tmp_path, stats=stats, **kw))
    assert len(blocks) == 1
    return blocks[0].actions, stats


def test_real_ioc_order(tmp_path):
    (p,), _ = one_block(tmp_path, [REAL_IOC])
    assert isinstance(p, Place)
    assert (p.asset, p.side, p.px, p.sz, p.tif) == (0, Side.SELL, 647640, 906, Tif.IOC)
    assert p.cloid == "0x77541557f21a4b11b7d6c674286c55dd" and p.user == TAKER
    assert (p.block, p.tx_index, p.item_index, p.priority) == (1224258712, 0, 0, None)


def test_cancel_kinds_and_their_key_names(tmp_path):
    txs = [
        tx({"type": "cancel", "cancels": [{"a": 0, "o": 607819318775}, {"a": 110015, "o": 1}], "f": True}),
        tx({"type": "cancelByCloid", "cancels": [{"asset": 1, "cloid": "0xfc4e"}, {"asset": 159, "cloid": "0xaa"}], "f": True}),
    ]
    acts, _ = one_block(tmp_path, txs)
    assert [type(a) for a in acts] == [Cancel, CancelByCloid]   # other assets dropped
    assert acts[0].oid == 607819318775 and acts[1].cloid == "0xfc4e" and acts[1].asset == 1


def test_modify_targets_int_or_cloid(tmp_path):
    order = {"a": 1, "b": True, "p": "1836.9", "s": "1.114", "r": False, "t": {"limit": {"tif": "Alo"}}}
    txs = [tx({"type": "modify", "oid": 607819312706, "order": order}),
           tx({"type": "batchModify", "modifies": [{"oid": "0xdba6", "order": order},
                                                   {"oid": 5, "order": {**order, "a": 110107}}]})]
    acts, _ = one_block(tmp_path, txs)
    assert [type(a) for a in acts] == [Modify, Modify]
    assert acts[0].target == 607819312706 and acts[1].target == "0xdba6"
    assert acts[1].new.px == 183690 and acts[1].new.tif is Tif.ALO


def test_schedule_cancel_with_and_without_time(tmp_path):
    acts, _ = one_block(tmp_path, [tx({"type": "scheduleCancel", "time": 1794894427572}),
                                   tx({"type": "scheduleCancel"})])
    assert [(type(a), a.at_ms) for a in acts] == [(ScheduleCancel, 1794894427572), (ScheduleCancel, None)]


def test_skips_and_counts(tmp_path):
    trig = {"a": 0, "b": False, "p": "64000", "s": "0.001", "r": True,
            "t": {"trigger": {"isMarket": True, "triggerPx": "64000", "tpsl": "sl"}}}
    txs = [
        tx({"type": "order", "grouping": "normalTpsl", "orders": [trig]}),
        tx({"type": "order", "grouping": "na", "orders": [
            {"a": 0, "b": True, "p": "63597.59", "s": "0.00077", "r": False, "t": {"limit": {"tif": "Gtc"}}},
            {"a": 0, "b": True, "p": "64245", "s": "0", "r": False, "t": {"limit": {"tif": "Ioc"}}}]}),
        tx({"type": "order", "grouping": "na", "orders": [REAL_IOC["action"]["orders"][0]]},
           error="Action already expired"),
        tx({"type": "noop", "nonce": 1}),
    ]
    acts, s = one_block(tmp_path, txs)
    assert acts == ()
    assert (s["skip_trigger"], s["skip_invalid_precision"], s["skip_zero_px_or_sz"]) == (1, 1, 1)
    assert s["errored:order"] == 1 and s["ignored:noop"] == 1


def test_frontend_market_and_priority(tmp_path):
    txs = [tx({"type": "order", "grouping": {"p": 302}, "orders": [
        {"a": 0, "b": True, "p": "70000", "s": "0.001", "r": False, "t": {"limit": {"tif": "FrontendMarket"}}}]})]
    (p,), _ = one_block(tmp_path, txs)
    assert p.tif is Tif.IOC and p.priority == 302


def test_heights_sorted_numerically(tmp_path):
    for h in (1000, 999, 10000):
        write_block(tmp_path, h, [])
    assert [b.height for b in iter_window(tmp_path)] == [999, 1000, 10000]


def test_error_response_saved_as_block_is_refused(tmp_path):
    with gzip.open(tmp_path / "5.json.gz", "wt") as f:
        json.dump("More than 100 archived blocks queried in one day", f)
    with pytest.raises((ValueError, AttributeError)):
        list(iter_window(tmp_path))


def test_incomplete_block_is_refused(tmp_path):
    write_block(tmp_path, 7, [REAL_IOC])
    path = tmp_path / "7.json.gz"
    body = json.load(gzip.open(path, "rt"))
    body["blockDetails"]["numTxs"] = 2
    with gzip.open(path, "wt") as f:
        json.dump(body, f)
    with pytest.raises(ValueError, match="incomplete"):
        list(iter_window(tmp_path))


# ---- recorder files -----------------------------------------------------------

def write_hour(root, stem, records, gz=False):
    hour = root / "2026-11-17" / "05"
    hour.mkdir(parents=True, exist_ok=True)
    path = hour / f"{stem}.jsonl{'.gz' if gz else ''}"
    opener = gzip.open if gz else open
    with opener(path, "wt") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def trade(h, tid, side="A", px="64893.0", sz="0.00906", t=T):
    return {"coin": "BTC", "side": side, "px": px, "sz": sz, "time": t, "hash": h,
            "tid": tid, "users": [MAKER, TAKER]}


def test_trades_link_taker_and_dedupe(tmp_path):
    rec = {"recv_ms": T, "msg": {"channel": "trades", "data": [trade(TX_HASH, 778960509408088)]}}
    write_hour(tmp_path, "trades_BTC", [rec, rec])                 # duplicate line
    write_hour(tmp_path, "trades_BTC", [rec], gz=True)             # gz copy of the same hour
    (t,) = list(iter_trades(tmp_path, ("BTC",), T, T))
    assert (t.taker, t.maker, t.px, t.sz, t.taker_side) == (TAKER, MAKER, 648930, 906, Side.SELL)
    assert t.hash == TX_HASH   # equals the real IOC's tx hash


def test_zero_hash_trades_become_outside_fills_at_block_end(tmp_path):
    hl = tmp_path / "hl"
    win = tmp_path / "win"
    win.mkdir()
    rec = {"recv_ms": T, "msg": {"channel": "trades", "data": [
        trade(ZERO_HASH, 9, side="B"), trade(ZERO_HASH, 3, side="B"), trade(TX_HASH, 5)]}}
    write_hour(hl, "trades_BTC", [rec])
    write_block(win, 1224258712, [REAL_IOC])
    (block,) = list(iter_window(win, hl_data=hl))
    place, of1, of2 = block.actions
    assert isinstance(place, Place)
    assert all(isinstance(a, OutsideFill) for a in (of1, of2))
    assert (of1.tid, of2.tid) == (3, 9)                            # ordered by tid
    assert of1.tx_index == 1 and (of1.item_index, of2.item_index) == (0, 1)
    assert of1.user == MAKER and of1.maker_user == TAKER           # side B: taker is users[0]


def test_nearest_book(tmp_path):
    def snap(t, bid):
        return {"recv_ms": t, "msg": {"channel": "l2Book", "data": {"coin": "BTC", "time": t, "levels": [
            [{"px": bid, "sz": "0.5", "n": 2}], [{"px": "65013.0", "sz": "1", "n": 1}]]}}}
    write_hour(tmp_path, "book_BTC", [snap(T - 900, "65012.0"), snap(T + 5000, "65000.0")])
    b = nearest_book(tmp_path, "BTC", T)
    assert b.time_ms == T - 900 and b.bids == ((650120, 50000),) and b.asks == ((650130, 100000),)
