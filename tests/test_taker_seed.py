from analysis.taker_seed import build


def test_seed_uses_only_earlier_chunks_in_window():
    rows = [{"chunk": "c2", "snapshot_time": "2000000", "taker_blocked_end": "b"},
            {"chunk": "c1", "snapshot_time": "1000000", "taker_blocked_end": "a;x"},
            {"chunk": "c3", "snapshot_time": "9000000", "taker_blocked_end": "c"}]
    s = build(rows, window_ms=1_800_000)
    assert s == {"c1": [], "c2": ["a", "x"], "c3": []}      # c3: earlier chunks are > 30 min before
