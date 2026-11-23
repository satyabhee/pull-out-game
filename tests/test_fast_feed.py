"""data.parse.iter_fast: the 5-level fast book with order counts (A1)."""
import json

import pytest

from data.parse import iter_fast

T = 1795305600000                      # 2026-11-22 00:00:00 UTC


def write(tmp_path, recs):
    d = tmp_path / "2026-11-22" / "00"
    d.mkdir(parents=True)
    with open(d / "book_BTC_fast.jsonl", "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def rec(t, recv, fast=True, bid=("60182.0", "4.61584", 19)):
    lv = lambda px, sz, n: {"px": px, "sz": sz, "n": n}
    return {"recv_ms": recv, "msg": {"channel": "l2Book", "data": {
        "coin": "BTC", "time": t, "fast": fast,
        "levels": [[lv(*bid)], [lv("60183.0", "3.28983", 17)]]}}}


def test_iter_fast_keeps_counts_dedupes_and_orders_by_feed_time(tmp_path):
    # the second update arrives first (bursty receive times); a duplicate time keeps one copy
    write(tmp_path, [rec(T + 900, T + 1000, bid=("60181.0", "1", 3)), rec(T + 100, T + 1200), rec(T + 100, T + 1300)])
    out = list(iter_fast(tmp_path, "BTC", T, T + 5000))
    assert [b.time_ms for b in out] == [T + 100, T + 900]
    assert out[0].bids == ((601820, 461584, 19),) and out[0].asks == ((601830, 328983, 17),)   # BTC tick 0.1, lot 1e-5
    assert out[1].bids == ((601810, 100000, 3),)


def test_iter_fast_refuses_a_record_that_is_not_fast(tmp_path):
    write(tmp_path, [rec(T + 100, T + 200, fast=False)])
    with pytest.raises(ValueError):
        list(iter_fast(tmp_path, "BTC", T, T + 5000))
