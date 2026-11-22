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
