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
    placed = {(r["height"], r["tx"], r["item"]): r["oid"] for r in truth if r["kind"] == "placed"}
    anchors = json.loads((window / "anchors.json").read_text())
    assert len(anchors) == cfg.anchors_per_chunk and all(placed[(a["height"], a["tx"], a["item"])] == a["oid"]
                                                         for a in anchors)
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
