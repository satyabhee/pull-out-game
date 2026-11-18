"""analysis/validate_chunks.py: find chunk folders, run validate_replay on each, pool the rows."""

import csv
import json
from collections import Counter

from analysis import validate_chunks as vc
from analysis import validate_replay as vr
from tests.window_fixture import A, B, H0, M, T0, Window, order, place_tx

N = "0x" + "e" * 40
PX = "60010"


def chunk(root, i, real_maker, kind="chunk", status="complete"):
    """Chunk i: M then N rest an ALO sell at PX, then an IOC buy takes M under time priority.
    The real fill names `real_maker`: M -> strict match, N -> mismatch (px/size still match)."""
    w = Window(root, name=f"c{i}", h0=H0 + 100 * i, t0=T0 + 10_000 * i, kind=kind, status=status)
    for coin in ("BTC", "ETH"):
        w.book(coin, w.t0 - 50, [], [])                      # a chunk starts just after its snapshot
    w.block([place_tx(M, f"0xm{i}", 0, order(buy=False, px=PX, tif="Alo")),
             place_tx(N, f"0xn{i}", 0, order(buy=False, px=PX, tif="Alo"))])
    h = w.block([place_tx(A, f"0xa{i}", 0, order(buy=True, px=PX, tif="Ioc"))])
    w.trade("BTC", "B", PX, "0.001", w.time_of(h), f"0xa{i}", i, A, real_maker)
    return w.write()


def csv_rows(path):
    return list(csv.DictReader(open(path)))


# ------------------------------------------------------------------ discovery

def test_find_chunks_keeps_complete_chunks_and_says_why_others_are_skipped(tmp_path):
    good, hl = chunk(tmp_path, 0, M)
    chunk(tmp_path, 1, M, status="fetching")
    chunk(tmp_path, 2, M, kind=None)                          # a full window, not a chunk
    (tmp_path / "windows" / "stray").mkdir()                  # no window.json
    chunks, skipped = vc.find_chunks([tmp_path / "windows"])
    assert chunks == [good]
    assert {p.name: why for p, why in skipped} == {"c1": "status fetching", "c2": "kind window",
                                                   "stray": "no window.json"}


def test_find_chunks_accepts_chunk_folders_directly_in_height_order(tmp_path):
    c0, _ = chunk(tmp_path, 0, M)
    c1, _ = chunk(tmp_path, 1, M)
    assert vc.find_chunks([c1, c0]) == ([c0, c1], [])
    assert vc.find_chunks([c0, tmp_path / "windows"]) == ([c0, c1], [])   # no duplicates


# ------------------------------------------------------------------ pooling

def test_pooled_metrics_come_from_pooled_rows_not_averaged(tmp_path):
    c0, hl = chunk(tmp_path, 0, M)                            # match
    c1, _ = chunk(tmp_path, 1, N)                             # strict mismatch, px/size match
    c2, _ = chunk(tmp_path, 2, M)                             # match
    res, rows, per_chunk = vc.run_batch([c0, c1, c2], hl)
    assert [r["chunk"] for r in rows] == ["c0", "c1", "c2"]
    assert res["chunks_ok"] == 3 and res["chunks_failed"] == 0
    assert res["all:scored"] == 3 and res["all:matched"] == 2 and res["all:match_rate"] == "0.6667"
    assert res["all:match_rate_px_sz"] == "1.0000"
    assert res["all:fill_recall"] == "0.6667" and res["BTC:scored"] == 3 and res["ETH:scored"] == 0
    assert res["g1_target"] == "fail"
    assert res["mismatch_table"][("ordering", "BTC")] == 1
    assert [c["all:matched"] for c in per_chunk] == [1, 0, 1]


def test_a_failing_chunk_is_recorded_and_the_rest_still_run(tmp_path):
    c0, hl = chunk(tmp_path, 0, M)
    c1, _ = chunk(tmp_path, 1, M)
    (c1 / f"{H0 + 100}.json.gz").write_bytes(b"not gzip")
    res, rows, per_chunk = vc.run_batch([c0, c1], hl)
    assert res["chunks_ok"] == 1 and res["chunks_failed"] == 1 and res["all:scored"] == 1
    bad = per_chunk[1]
    assert bad["chunk"] == "c1" and bad["status"] == "failed" and bad["error"]


def test_fix_options_reach_every_chunk_and_the_pooled_summary(tmp_path):
    c0, hl = chunk(tmp_path, 0, M)
    c1, _ = chunk(tmp_path, 1, M)
    res, rows, _ = vc.run_batch([c0, c1], hl, alo_gate="touch", reseed=True, nearest_ratio=3)
    assert res["alo_gate"] == "touch" and res["reseed"] == "every snapshot"
    assert res["resolver"] == "off" and res["nearest_ratio"] == ""   # no anchors.json in these chunks
    assert res["horizons"][0]["horizon"] == "0-2s" and res["horizons"][0]["scored"] == res["all:scored"]
    alone = [vr.run(c, hl, quiet=True, alo_gate="touch", reseed=True, nearest_ratio=3)[0] for c in (c0, c1)]
    expected = Counter(alone[0]["gate_stats"]) + Counter(alone[1]["gate_stats"])
    assert res["gate_stats"] == dict(sorted(expected.items())) and sum(expected.values()) > 0
    assert res["all:scored"] == alone[0]["all:scored"] + alone[1]["all:scored"]


def test_jobs_give_the_same_result_as_one_process(tmp_path):
    paths = [chunk(tmp_path, i, M if i % 2 else N)[0] for i in range(4)]
    hl = tmp_path / "hl_data"
    one = vc.run_batch(paths, hl, jobs=1)
    two = vc.run_batch(paths, hl, jobs=2)
    assert one == two


# ------------------------------------------------------------------ CSVs + CLI

def test_csv_outputs(tmp_path):
    c0, hl = chunk(tmp_path, 0, M)
    c1, _ = chunk(tmp_path, 1, N)
    out = tmp_path / "out"
    vc.run_batch([c0, c1], hl, out_dir=out, reseed=True, skipped=[(tmp_path / "x", "status fetching")])
    assert {p.name for p in out.iterdir()} == {"summary.csv", "per_asset.csv", "horizon.csv", "chunks.csv",
                                               "txs.csv", "mismatches.csv", "alo_truth.csv"}
    summary = dict(csv.reader(open(out / "summary.csv")))
    assert summary["chunks_ok"] == "2" and summary["chunks_skipped"] == "1" and summary["all:scored"] == "2"
    per_asset = csv_rows(out / "per_asset.csv")
    assert [r["scope"] for r in per_asset] == ["all", "BTC", "ETH"]
    assert per_asset[0]["scored"] == "2" and per_asset[0]["fill_recall"] == "0.5000"
    chunks = csv_rows(out / "chunks.csv")
    assert [(c["chunk"], c["status"]) for c in chunks] == [("c0", "ok"), ("c1", "ok"), ("x", "skipped: status fetching")]
    txs = csv_rows(out / "txs.csv")
    assert [t["chunk"] for t in txs] == ["c0", "c1"] and list(txs[0])[0] == "chunk"
    assert csv_rows(out / "horizon.csv")[0]["scored"] == "2"
    assert next(r for r in csv_rows(out / "mismatches.csv") if r["category"] == "ordering")["total"] == "1"


def test_cli_finds_chunks_and_names_the_output_after_the_options(tmp_path, monkeypatch, capsys):
    chunk(tmp_path, 0, M)
    chunk(tmp_path, 1, M, status="fetching")
    monkeypatch.chdir(tmp_path)
    vc.main(["windows", "--hl-data", "hl_data", "--alo-gate", "touch", "--reseed", "--nearest-ratio", "3"])
    out = tmp_path / "report" / "validate" / "chunks-listing-gate-nr3-reseed"
    summary = dict(csv.reader(open(out / "summary.csv")))
    assert summary["chunks_ok"] == "1" and summary["chunks_skipped"] == "1" and summary["alo_gate"] == "touch"
    text = capsys.readouterr().out
    assert "chunks: 1 ok, 0 failed, 1 skipped" in text and "PER-FILL STRICT recall" in text


def test_cli_with_no_chunks_says_so(tmp_path, capsys):
    (tmp_path / "windows").mkdir()
    (tmp_path / "hl_data").mkdir()
    try:
        vc.main([str(tmp_path / "windows"), "--hl-data", str(tmp_path / "hl_data")])
    except SystemExit as e:
        assert "no complete chunk folders" in str(e.code)
    else:
        raise AssertionError("expected SystemExit")


def test_window_json_kind_written_by_capture_is_what_we_filter_on(tmp_path):
    c0, _ = chunk(tmp_path, 0, M)
    assert json.loads((c0 / "window.json").read_text())["kind"] == "chunk"
