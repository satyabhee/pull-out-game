"""analysis/anchor_density.py: reproducible subsets, anchor location, held-out checking, summary."""
import json

from analysis import anchor_density as ad
from tests.window_fixture import A, Window, cancel_tx, order, place_tx


def make_window(tmp_path, anchor_format="height"):
    w = Window(tmp_path)
    for i, px in enumerate(("59000", "59001", "59002")):
        w.block([place_tx(A, f"0x{i + 1}", 0, order(px=px, cloid=f"0xc{i}"))])
    w.block([cancel_tx(A, "0x9", 0, 0, 6000)])                     # int-oid cancel of the middle placement
    if anchor_format == "height":
        w.anchors = [{"oid": o, "height": 1000 + i, "tx": 0, "item": 0} for i, o in enumerate((5000, 6000, 7000))]
    else:
        w.anchors = [{"user": A, "oid": o, "answer": {"status": "order", "order": {"order": {
            "coin": "BTC", "side": "B", "limitPx": f"{59000 + i}.0", "origSz": "0.001", "oid": o,
            "timestamp": w.time_of(1000 + i), "cloid": f"0xc{i}"}}}} for i, o in enumerate((5000, 6000, 7000))]
    return w.write()


def test_choose_subset_is_reproducible_sorted_and_without_replacement():
    located = [{"user": "u", "oid": 1000 + i, "pos": (i, 0, 0), "k": i} for i in range(42)]
    a, b = ad.choose_subset(located, 10, 7), ad.choose_subset(located, 10, 7)
    assert a == b and len(a) == 10 and len({x["oid"] for x in a}) == 10
    assert a == sorted(a, key=lambda x: x["k"])
    assert ad.choose_subset(located, 10, 8) != a
    assert ad.choose_subset(located, 42, 7) == located and ad.choose_subset(located, 99, 7) == located


def test_dist_bucket_edges():
    assert [ad.dist_bucket(d) for d in (0, 99, 100, 999, 10000, 10**9)] == ["<100", "<100", "<300", "<1000", ">=10000", ">=10000"]


def test_locate_anchors_height_and_capture_formats_agree(tmp_path):
    (d1, _), (d2, _) = make_window(tmp_path / "a"), make_window(tmp_path / "b", "capture")
    for d in (d1, d2):
        counts, located = ad.locate_anchors(d, json.loads((d / "anchors.json").read_text()))
        assert [(x["oid"], x["pos"], x["k"], x["user"]) for x in located] == [
            (5000, (1000, 0, 0), 0, A), (6000, (1001, 0, 0), 1, A), (7000, (1002, 0, 0), 2, A)]
        assert counts[(1002, 0, 0)] == 2


def test_heldout_cancel_resolves_to_its_true_placement_with_zero_wrong(tmp_path):
    d, hl = make_window(tmp_path)
    counts, located = ad.locate_anchors(d, json.loads((d / "anchors.json").read_text()))
    # the cancel (oid 6000) targets the anchor left out of the subset: truth is position (1001, 0, 0)
    sub = [located[0], located[2]]
    r = ad.run_one(d, hl, counts, located, sub)
    assert r["outcome"] == {"resolved": 1}
    assert r["heldout"] == {"total": 1, "resolved": 1, "correct": 1}
    assert r["by_bucket"] == {"<3000|resolved": 1}      # target is 1000 oids from the nearest anchor in use


def test_with_every_anchor_in_use_nothing_is_held_out(tmp_path):
    d, hl = make_window(tmp_path)
    counts, located = ad.locate_anchors(d, json.loads((d / "anchors.json").read_text()))
    r = ad.run_one(d, hl, counts, located, located)
    assert r["heldout"] == {} and r["outcome"] == {"resolved": 1}


def test_wrong_resolution_is_counted_when_truth_disagrees(tmp_path):
    d, hl = make_window(tmp_path)
    counts, located = ad.locate_anchors(d, json.loads((d / "anchors.json").read_text()))
    lied = [dict(x) for x in located]
    lied[1]["pos"] = (1000, 0, 0)                       # pretend oid 6000 was really the FIRST placement
    r = ad.run_one(d, hl, counts, lied, [located[0], located[2]])
    assert r["heldout"]["WRONG"] == 1 and "correct" not in r["heldout"]
    w, = r["wrong_cases"]
    assert w["true_pos"] == [1000, 0, 0] and w["chose_pos"] == [1001, 0, 0] and w["cancel_oid"] == 6000


def test_summarize_and_bucket_table():
    rows = [{"n": 5, "outcome": {"resolved": 1, "ambiguous": 3}, "heldout": {"total": 2, "correct": 1, "WRONG": 0},
             "by_bucket": {"<100|resolved": 1, "<100|ambiguous": 3}},
            {"n": 5, "outcome": {"resolved": 3, "ambiguous": 1}, "heldout": {"total": 1, "correct": 1},
             "by_bucket": {"<100|resolved": 3, ">=10000|ambiguous": 1}}]
    s = ad.summarize(rows)[0]
    assert s["n"] == 5 and s["runs"] == 2 and s["cancels"] == 4
    assert s["resolved"] == {"mean_bp": 5000, "min_bp": 2500, "max_bp": 7500}
    assert s["heldout"] == {"total": 3, "correct": 2, "WRONG": 0}
    assert ad.bucket_table(rows)["<100"] == {"resolved": 4, "ambiguous": 3}
    assert ad.fmt_bp(1234) == "12.34%"
