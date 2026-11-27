"""data/catalog.py: ingest gaps keep chunks out of every chunk set."""
import json

from analysis.validate_chunks import find_chunks
from data.catalog import _load, chunk_gap, gap_touched

GAP0, GAP1 = 1795340815000, 1795342328000          # 09:46:55 - 10:12:08 UTC, Nov 22 (the last gap)


def window(tmp_path, name, t0, t1):
    d = tmp_path / name
    d.mkdir()
    (d / "window.json").write_text(json.dumps({"kind": "chunk", "status": "complete", "start_height": int(name),
                                               "first_block_time": t0, "last_block_time": t1}))
    return d


def test_gap_file_is_sorted_and_positive():
    gaps, before, after = _load()
    assert all(a < b for a, b in gaps) and list(gaps) == sorted(gaps) and before > 0 and after > 0


def test_margins():
    _, before, after = _load()
    assert gap_touched(GAP0 + 1, GAP0 + 2) is not None                       # inside
    assert gap_touched(GAP1 + before // 2, GAP1 + before // 2 + 10) is not None  # starts soon after: lookback
    assert gap_touched(GAP0 - after // 2 - 10, GAP0 - after // 2) is not None    # ends soon before: lag
    assert gap_touched(GAP1 + before + 1, GAP1 + before + 10_000) is None
    assert gap_touched(GAP0 - after - 10_000, GAP0 - after - 1) is None


def test_find_chunks_skips_gap_chunks(tmp_path):
    ok = window(tmp_path, "1", GAP1 + 3_600_000, GAP1 + 3_610_000)
    bad = window(tmp_path, "2", GAP0 + 60_000, GAP0 + 70_000)
    chunks, skipped = find_chunks([tmp_path])
    assert chunks == [ok] and skipped == [(bad, "ingest gap")]
    assert chunk_gap(bad) == (GAP0, GAP1) and chunk_gap(ok) is None


def test_open_gap_touches_every_later_chunk(tmp_path, monkeypatch):
    import data.catalog as cat
    start = 1_900_000_000_000                                 # fixture gap, still open (end_ms null)
    f = tmp_path / "gaps.json"
    f.write_text(json.dumps({"margin": {"before_ms": 900_000, "after_ms": 300_000},
                             "gaps": [{"start_ms": start, "end_ms": None}]}))
    monkeypatch.setattr(cat, "GAPS_FILE", f)
    cat._load.cache_clear()
    try:
        assert cat.gap_touched(start + 86_400_000, start + 86_410_000) == (start, cat.OPEN_END)
        assert cat.gap_touched(start - 3_600_000, start - 3_590_000) is None
    finally:
        cat._load.cache_clear()


def test_nov26_gap_is_closed():
    gaps, _, _ = _load()
    assert (1795714565000, 1795734886000) in gaps             # 17:36:05 - 23:14:46Z, 20,321 s (Yuta #61/#64)
