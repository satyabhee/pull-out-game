"""analysis/resolver_report.py: window cropping, per-cancel resolver outcomes, oid-time estimate, pooled output."""

import json
from collections import Counter

import pytest

from analysis import resolver_report as rr
from data.parse import _block_files
from engine_py.types import Cancel, CancelByCloid, Modify, Place
from tests.window_fixture import A, T0, Window, cancel_tx, order, place_tx, tx

BASE = 555000
GAP = 200               # ids between the two placements: farther apart than the resolver tolerance (50)


def lead_in_window(tmp_path, **kw):
    """2 lead-in blocks (two placements by A, anchored at ids 555000 and 555200) + 1 scored block with
    three int-oid cancels: the first lead-in order, the second, and an oid far below the window."""
    w = Window(tmp_path, warmup_blocks=2, kind="chunk", meta={"lead_in_blocks": 2}, **kw)
    h0 = w.block([place_tx(A, "0xa1", 0, order(px="60000", tif="Gtc"))])
    w.block([place_tx(A, "0xa2", 0, order(px="59990", tif="Gtc"))])
    w.block([cancel_tx(A, "0xc1", 0, 0, BASE), cancel_tx(A, "0xc2", 0, 0, BASE + GAP),
             cancel_tx(A, "0xc3", 0, 0, 400000)])
    w.anchors = [{"oid": BASE, "height": h0, "tx": 0, "item": 0}, {"oid": BASE + GAP, "height": h0 + 1, "tx": 0, "item": 0}]
    return w


def test_int_target_only_for_int_oids():
    meta = dict(user=A, asset=0, block=1, tx_index=0, item_index=0, hash="0x", priority=None)
    new = Place(**meta, side=None, px=1, sz=1, tif=None, cloid=None, reduce_only=False)
    assert rr.int_target(Cancel(**meta, oid=7)) == 7
    assert rr.int_target(CancelByCloid(**meta, cloid="0x01")) is None
    assert rr.int_target(Modify(**meta, target=9, new=new)) == 9
    assert rr.int_target(Modify(**meta, target="0x01", new=new)) is None
    assert rr.int_target(new) is None


def test_crop_window_symlinks_blocks_and_adjusts_meta_without_touching_the_source(tmp_path):
    src, _ = lead_in_window(tmp_path).write()
    before = sorted((p.name, p.stat().st_size) for p in src.iterdir())
    dst = rr.crop_window(src, tmp_path / "out" / "crop", 1001, lead_in_blocks=1)
    assert [h for h, _ in _block_files(dst)] == [1001, 1002]
    assert all((dst / p.name).is_symlink() for _, p in _block_files(dst))
    meta = json.loads((dst / "window.json").read_text())
    assert (meta["start_height"], meta["end_height"], meta["lead_in_blocks"], meta["warmup_end_height"]) == \
        (1001, 1002, 1, 1002)
    assert (dst / "anchors.json").read_text() == (src / "anchors.json").read_text()
    assert before == sorted((p.name, p.stat().st_size) for p in src.iterdir())      # source untouched
    dst2 = rr.crop_window(src, tmp_path / "out" / "crop", 1000, last_height=1001, anchors=[])   # re-crop overwrites
    assert [h for h, _ in _block_files(dst2)] == [1000, 1001] and (dst2 / "anchors.json").read_text() == "[]"
    with pytest.raises(ValueError):
        rr.crop_window(src, tmp_path / "out" / "none", 5000)


def test_scored_cancels_report_outcome_and_target_block(tmp_path):
    win, hl = lead_in_window(tmp_path).write()
    rp, obs = rr.replay_variant(win, hl, nearest_ratio=0)
    out = {r["hl_oid"]: r for r in obs.rows}
    assert set(out) == {BASE, BASE + GAP, 400000} and all(r["height"] == 1002 for r in out.values())
    assert out[BASE]["outcome"] == "resolved" and out[BASE]["target_block"] == 1000       # placed in the lead-in
    assert out[BASE + GAP]["outcome"] == "resolved" and out[BASE + GAP]["target_block"] == 1001
    assert out[400000]["outcome"] == "prewindow" and out[400000]["target_block"] is None
    v = rr.summarize_replay(rp, obs)
    assert v["located"] == 2 and v["scored_lookups"] == 3 and v["scored"] == {"resolved": 2, "prewindow": 1}
    assert v["whole"]["resolved"] == 2          # lead-in blocks hold no cancels, so whole == scored here


def test_lead_in_cancels_are_not_counted_as_scored(tmp_path):
    w = Window(tmp_path, warmup_blocks=2, kind="chunk", meta={"lead_in_blocks": 2})
    w.block([cancel_tx(A, "0xc0", 0, 0, 123456)])                 # lead-in block: replayed, never scored
    w.block([])
    w.block([cancel_tx(A, "0xc1", 0, 0, 123457)])
    w.anchors = []
    win, hl = w.write()
    rp, obs = rr.replay_variant(win, hl, nearest_ratio=0)
    assert [r["hl_oid"] for r in obs.rows] == [123457]
    assert rr.summarize_replay(rp, obs)["whole"] == {"no_candidate": 2}      # whole replay counts both lookups


def test_skip_lead_in_and_dropped_lead_in_lose_the_lead_in_targets(tmp_path):
    win, hl = lead_in_window(tmp_path).write()
    _, obs = rr.replay_variant(win, hl, nearest_ratio=0, skip_lead_in=True)
    assert Counter(r["outcome"] for r in obs.rows) == {"no_candidate": 2, "prewindow": 1}   # not replayed: none rests
    crop = rr.crop_window(win, tmp_path / "out" / "d1", 1001, lead_in_blocks=1)   # first anchor's block is gone
    rp, obs = rr.replay_variant(crop, hl, nearest_ratio=0)
    assert rp.resolver.est.anchors == [(0, BASE + GAP)]
    assert {r["hl_oid"]: r["outcome"] for r in obs.rows} == {BASE: "prewindow", BASE + GAP: "resolved",
                                                              400000: "prewindow"}


def test_anchor_points_pooled_rate_and_period_classes():
    ans = lambda oid, ts: {"answer": {"status": "order", "order": {"order": {"oid": oid, "timestamp": ts}}}}
    pts = rr.anchor_points([ans(1000, 10_000), {"answer": {"status": "unknownOid"}}, ans(1000, 10_000),
                            ans(1000 + 10_000 * 5, 15_000)])
    assert pts == [(1000, 10_000), (51000, 15_000)]                   # deduplicated, order answers only
    assert rr.pooled_rate_per_s([pts, [(0, 0), (500, 1000)]]) == 10_000     # pairs < 3 s apart are ignored
    assert rr.pooled_rate_per_s([[(1, 1)]]) is None
    tp = rr.TargetPeriod([(1_000_000, 100_000)], 10_000, lead_start_ms=101_000, scored_start_ms=111_000)
    (b0, w0), (b1, w1) = tp.bounds["lead"], tp.bounds["scored"]
    assert b0 == 1_010_000 and w0 == 500 + 10_000 * 15 // 100
    assert b1 == 1_110_000 and w1 == 500 + 110_000 * 15 // 100
    assert [tp.classify(x) for x in (b0 - w0 - 1, b0, b0 + w0 + 1, b1 - w1 - 1, b1, b1 + w1 + 1)] == [
        "before_lead_in", "uncertain_lead_in_start", "lead_in", "lead_in", "uncertain_scored_start", "scored_part"]
    assert rr.TargetPeriod([], 10_000, 0, 1).classify(5) == "unclassified"
    assert rr.TargetPeriod([(1, 1)], None, 0, 1).classify(5) == "unclassified"


def test_run_chunks_end_to_end_writes_tables(tmp_path):
    win, hl = lead_in_window(tmp_path).write()
    data = rr.run_chunks(win.parent, hl, tmp_path / "out", nearest_ratio=0, drops=(1,), jobs=1)
    assert [r["chunk"] for r in data["results"]] == ["w"] and "error" not in data["results"][0]
    v = data["results"][0]["variants"]
    assert set(v) == {"lead_in", "skip_lead", "drop1"}
    assert v["lead_in"]["scored"] == {"resolved": 2, "prewindow": 1} and v["drop1"]["scored"] == {"resolved": 1, "prewindow": 2}
    md = rr.write_chunks(data, tmp_path / "out" / "tables")
    assert "| lead_in | 1 | 1 | 2 |" in md and "unresolved cancel" in md
    for name in ("per_chunk.csv", "pooled_outcomes.csv", "target_period.csv", "categories.csv", "resolver_report.md"):
        assert (tmp_path / "out" / "tables" / name).exists()


def test_chunk_errors_are_recorded_not_dropped(tmp_path):
    r = rr._job_safe((str(tmp_path / "missing"), str(tmp_path), str(tmp_path), 0, (), None))
    assert r["chunk"] == "missing" and r["error"] and r["variants"] == {}


def test_sim_cuts_the_same_scored_blocks_at_each_lead(tmp_path):
    w = Window(tmp_path, kind="window")
    for i in range(6):
        w.block([place_tx(A, f"0xa{i}", 0, order(px=str(60000 - i), tif="Gtc"))])
    w.block([cancel_tx(A, "0xc", 0, 0, BASE + 5 * GAP)])
    w.anchors = [{"oid": BASE, "height": 1000, "tx": 0, "item": 0},
                 {"oid": BASE + 5 * GAP, "height": 1005, "tx": 0, "item": 0}]
    full, hl = w.write()
    data = rr.run_sim(full, hl, tmp_path / "out", leads=(0, 3, 6), scored_blocks=1, spacing=7, nearest_ratio=0, jobs=1)
    (r,) = data["results"]
    assert r["scored_first"] == 1006 and set(r["leads"]) == {0, 3, 6}
    assert {L: v["scored_lookups"] for L, v in r["leads"].items()} == {0: 1, 3: 1, 6: 1}
    assert r["leads"][0]["scored"] == {"no_candidate": 1}          # no lead-in: the anchored order is not in the crop
    assert r["leads"][6]["located"] == 2 and r["leads"][6]["scored"] == {"resolved": 1}
    md = rr.write_sim(data, tmp_path / "out" / "tables")
    assert "| 6 | 1 | 1 | 2 | 1 |" in md
    assert not list((tmp_path / "out" / "crops").glob("*"))        # synthetic crops are removed after use
