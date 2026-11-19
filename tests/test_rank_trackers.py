"""analysis/rank_trackers.py: users scored by cancels that need (or got) exact order ids."""
import json

from analysis import rank_trackers as rt
from analysis import validate_replay as vr
from tests.window_fixture import A, B, T0, Window, cancel_tx, order, place_tx


def test_ambiguous_and_exact_count_duplicates_do_not(tmp_path):
    w = Window(tmp_path / "w")
    w.book("BTC", T0 - 50, [], [])
    w.block([place_tx(A, "0xa1", 0, order(px="60000"), order(px="59990")),
             place_tx(B, "0xb1", 0, order(px="59980"))], dt_ms=0)
    w.block([cancel_tx(A, "0xa2", 0, 0, 5000), cancel_tx(B, "0xb2", 0, 0, 900), cancel_tx(B, "0xb3", 0, 0, 900)],
            dt_ms=500)
    window, hl = w.write()
    t = w.time_of(w.h0)
    hist = lambda u, oid, px: {"user": u, "kind": "historicalOrders", "answer": [{"order": {
        "coin": "BTC", "side": "B", "limitPx": px, "sz": "0.0", "origSz": "0.001", "oid": oid, "timestamp": t,
        "cloid": None}, "status": "canceled", "statusTimestamp": t + 500}]}
    (window / "hist_orders.json").write_text(json.dumps([hist(B, 900, "59980.0")]))
    m = json.loads((window / "window.json").read_text())
    (window / "window.json").write_text(json.dumps({**m, "kind": "chunk", "status": "complete"}))
    ap = __import__("argparse").ArgumentParser()
    vr.add_replay_args(ap)
    kw = vr.replay_kwargs(ap.parse_args(["--hl-data", str(hl)]))
    score, parts, used, contested = rt.rank(window.parent, hl, "listing", kw, 6)
    assert used == [window.name]
    assert score[B.lower()] == 1                       # exact once; the repeat (exact_gone) counts nothing
    assert parts["resolved_exact"] == 1
    assert score[A.lower()] == 1 and parts["no_candidate"] == 1   # far from every estimate: unresolvable
    out = tmp_path / "t.json"
    rt.write(out, score, used, now_ms=5)
    d = json.loads(out.read_text())
    assert d["time"] == 5 and d["counts"][B.lower()] == 1


def test_contested_makers_take_the_first_slots_then_the_cancel_ranking():
    from collections import Counter
    score = Counter({"c1": 9, "c2": 8, "m1": 7, "c3": 1})
    contested = Counter({"m1": 0.9, "m2": 0.5, "m3": 0.0})
    assert rt.order(score, contested, 2)[:5] == ["m1", "m2", "c1", "c2", "c3"]
    assert rt.order(score, Counter(), 3)[:3] == ["c1", "c2", "m1"]
