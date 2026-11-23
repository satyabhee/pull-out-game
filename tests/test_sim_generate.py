"""M1 generator: chunks come from a seed (same seed -> same bytes), the masked files are readable by the real
loaders and consistent with the truth log, and the full-visibility property test: with nothing hidden, the replay
engine reproduces the simulated chain exactly (strict recall 1.0)."""
import filecmp
import gzip
import json
from collections import Counter
from pathlib import Path

import pytest

from analysis import validate_replay as vr
from analysis.validate_replay import scored_rows
from data.parse import iter_trades, iter_window
from replay.sim.config import SimConfig
from replay.sim.generate import chunk_name, generate_chunk
from replay.sim.truth import read_truth

SMALL = dict(n_blocks=60, lead_in=30)


def tree_equal(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    return all(tree_equal(a / d, b / d) for d in cmp.common_dirs)


def test_same_seed_same_bytes(tmp_path):
    for r in ("a", "b"):
        generate_chunk(SimConfig(seed=7, **SMALL), tmp_path / r)
    for sub in ("windows", "truth"):
        assert tree_equal(tmp_path / "a" / sub, tmp_path / "b" / sub)
    # recorder files: same bytes too
    assert tree_equal(tmp_path / "a" / "hl_data", tmp_path / "b" / "hl_data")
    generate_chunk(SimConfig(seed=8, **SMALL), tmp_path / "c")
    assert not tree_equal(tmp_path / "a" / "truth", tmp_path / "c" / "truth")


def test_masked_files_match_truth(tmp_path):
    cfg = SimConfig(seed=3, **SMALL)
    info = generate_chunk(cfg, tmp_path)
    window = tmp_path / "windows" / chunk_name(cfg)
    truth = read_truth(tmp_path / "truth" / f"{window.name}.parquet")
    blocks = list(iter_window(window))                                     # the real decoder reads every block
    assert len(blocks) == cfg.n_blocks and [b.height for b in blocks] == sorted(b.height for b in blocks)
    meta = json.loads((window / "window.json").read_text())
    trades = list(iter_trades(tmp_path / "hl_data", ["BTC", "ETH"], meta["first_block_time"], meta["last_block_time"]))
    fills = [r for r in truth if r["kind"] == "fill"]
    assert len(trades) == len(fills) == info["trades"] > 0
    assert Counter((t.hash, t.maker, t.px, t.sz) for t in trades) == \
        Counter((f["hash"], f["maker"], f["px"], f["sz"]) for f in fills)
    # anchors name a tx by its LISTING position (the explorer lists execution order since L1); truth keys by hash
    placed = {(r["height"], r["hash"], r["item"]): r["oid"] for r in truth if r["kind"] == "placed"}
    listed = {b.height: {a.tx_index: a.hash for a in b.actions} for b in blocks}
    anchors = json.loads((window / "anchors.json").read_text())
    assert len(anchors) == cfg.anchors_per_chunk and all(
        placed[(a["height"], listed[a["height"]][a["tx"]], a["item"])] == a["oid"] for a in anchors)
    kinds = Counter(r["kind"] for r in truth)
    assert kinds["placed"] == info["orders"] and kinds["block"] == cfg.n_blocks and kinds["burned_oids"] > 0
    statuses = Counter(r["status"] for r in truth if r["kind"] == "order_status")
    assert statuses["perpMarginRejected"] > 0 and statuses["canceled"] > 0     # hidden events do happen
    with gzip.open(next(window.glob("*.json.gz")), "rt") as f:
        assert "oid" not in json.dumps(json.load(f)["blockDetails"]["txs"][0]["action"].get("orders", []))


def test_full_visibility_replay_is_exact(tmp_path):
    """Plan M1 property test: nothing hidden -> the replay engine reaches strict recall 1.0 (chain rules:
    class order, self-trade cancels the resting order, docs priority; trade corrections off)."""
    cfg = SimConfig.full_visibility(seed=0, n_blocks=120, lead_in=40)
    generate_chunk(cfg, tmp_path)
    res, rows = vr.run(tmp_path / "windows" / chunk_name(cfg), tmp_path / "hl_data", "class", quiet=True,
                       stp="expire_maker", p_priority="docs", oid_facts=True, trade_learn=None)
    scored = scored_rows(rows)
    real = sum(r["real_notional"] for r in scored)
    assert real > 0 and len(scored) > 20
    assert sum(r["matched_notional_full"] for r in scored) == real
    assert all(r["match_full"] for r in scored)


def test_truth_parquet_roundtrip_is_exact(tmp_path):
    from replay.sim.truth import read_config, write_truth
    recs = [{"kind": "config", "seed": 3, "markets": {0: (1, 2, 3)}},
            {"kind": "placed", "oid": 1, "cloid": None, "reduce_only": False},       # None present
            {"kind": "block", "height": 7, "batches": [[0, 1], [2]]},                 # nested list
            {"kind": "order_status", "oid": 1, "status": "canceled", "by": "0xab"},
            {"kind": "order_status", "oid": 2, "status": "reduceOnlyCanceled", "silent": True}]
    write_truth(tmp_path / "t.parquet", recs)
    assert read_truth(tmp_path / "t.parquet") == recs[1:]
    assert read_config(tmp_path / "t.parquet") == {"kind": "config", "seed": 3, "markets": {"0": [1, 2, 3]}}


def test_full_visibility_corrections_with_tx_evidence_are_exact(tmp_path):
    """Q7 (docs/deviations.md D33): trade corrections that use only what each tx shows never remove a real order:
    with nothing hidden, block-end corrections keep strict recall at exactly 1.0."""
    cfg = SimConfig.full_visibility(seed=1, n_blocks=120, lead_in=40)
    generate_chunk(cfg, tmp_path)
    res, rows = vr.run(tmp_path / "windows" / chunk_name(cfg), tmp_path / "hl_data", "class", quiet=True,
                       stp="expire_maker", p_priority="docs", oid_facts=True, trade_learn="sweep,queue",
                       trade_learn_at="block-end", learn_evidence="tx")
    scored = scored_rows(rows)
    assert scored and all(r["match_full"] for r in scored)


def test_truth_study_rule_prefers_closest_difficulty_that_keeps_stats():
    """docs/m1_truth_ordering.md: closest baseline contested strict wins, unless it breaks a statistic in band."""
    from replay.sim import truth_study as ts
    real = {k: 1.0 for k in ts.TARGETS} | {ts.HARD: 0.59}
    ok = {k: 1.0 for k in ts.TARGETS}
    cands = {ts.DEFAULT: ok | {ts.HARD: 0.20}, "a": ok | {ts.HARD: 0.50}, "b": ok | {ts.HARD: 0.58}}
    cands["b"][ts.TARGETS[0]] = 2.0                       # closest, but breaks a statistic the default keeps
    assert ts.choose(real, cands)[0] == "a"
    cands["b"][ts.TARGETS[0]] = 1.0
    assert ts.choose(real, cands)[0] == "b"


def test_listing_is_execution_order_class_sorted_per_batch(tmp_path):
    """L1 (docs/l1_listing_order.md): the explorer lists txs in execution order, so each consensus batch's listing is
    class-sorted (0 = no GTC/IOC, 1 = cancel, 2 = GTC/IOC) and a block shows at most (batches - 1) class descents."""
    from analysis.listing_runs import block_classes, descents
    cfg = SimConfig(seed=4, **SMALL)
    generate_chunk(cfg, tmp_path)
    window = tmp_path / "windows" / chunk_name(cfg)
    truth = read_truth(tmp_path / "truth" / f"{window.name}.parquet")
    n_batches = {r["height"]: len(r["batches"]) for r in truth if r["kind"] == "block"}
    for f in window.glob("*.json.gz"):
        h = int(f.name.split(".")[0])
        assert descents(block_classes(f)) <= n_batches[h] - 1


def test_full_visibility_multi_lite_is_exact(tmp_path):
    """L5-lite (docs/scoreboard.md): with nothing hidden, --multi-lite (forks + their collapses) keeps strict
    recall at exactly 1.0 on top of the tx-evidence corrections, with re-seeding on so the phantom hook can fire."""
    cfg = SimConfig.full_visibility(seed=2, n_blocks=120, lead_in=40)
    generate_chunk(cfg, tmp_path)
    kw = dict(stp="expire_maker", p_priority="docs", oid_facts=True, trade_learn="sweep,queue",
              trade_learn_at="block-end", learn_evidence="tx", reseed=True, keep_known=True)
    res, rows = vr.run(tmp_path / "windows" / chunk_name(cfg), tmp_path / "hl_data", "class", quiet=True,
                       multi_lite=True, **kw)
    scored = scored_rows(rows)
    assert scored and all(r["match_full"] for r in scored)
