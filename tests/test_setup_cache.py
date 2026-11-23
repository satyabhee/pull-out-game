"""replay/setup_cache.py: the key changes with chunk, data, code and config; a cache hit replays exactly like a
fresh build."""
import json
import os

import pytest

from replay import setup_cache as sc
from tests.test_adapter import CHUNK, HL, N, key, needs_data


def test_plain_refuses_objects():
    assert sc._plain({"b": (1, None), "a": [True, 2.5]}) == {"a": [True, 2.5], "b": [1, None]}
    with pytest.raises(TypeError):
        sc._plain(object())


def test_chunk_fingerprint_changes_with_any_file(tmp_path):
    (tmp_path / "window.json").write_text(json.dumps({"first_block_time": 0}))
    (tmp_path / "5.json.gz").write_bytes(b"x")
    a = sc.chunk_fingerprint(tmp_path)
    (tmp_path / "anchors.json").write_text("[]")
    b = sc.chunk_fingerprint(tmp_path)
    (tmp_path / "6.json.gz").write_bytes(b"y")
    c = sc.chunk_fingerprint(tmp_path)
    assert a != b != c


def test_data_fingerprint_changes_when_a_recorder_file_grows(tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    t = 1795100000000
    (w / "window.json").write_text(json.dumps({"first_block_time": t, "last_block_time": t + 10_000}))
    import time
    hour = tmp_path / "hl" / time.strftime("%Y-%m-%d/%H", time.gmtime(t / 1000))
    hour.mkdir(parents=True)
    f = hour / "trades_BTC.jsonl"
    f.write_text("a\n")
    a = sc.data_fingerprint(w, tmp_path / "hl")
    f.write_text("a\nb\n")
    assert sc.data_fingerprint(w, tmp_path / "hl") != a


class _E:
    stp, p_priority, evict_arrival, check_every_action = "cancel_taker", "docs", True, False


def _policy():
    pass


def test_config_fingerprint_changes_with_options_engine_and_backend():
    args = ("w", "h", _policy)
    base = sc.config_fingerprint(args, {"engine": _E(), "ledger_k": 99}, "rust")
    assert base == sc.config_fingerprint(args, {"engine": _E(), "ledger_k": 99}, "rust")
    assert base != sc.config_fingerprint(args, {"engine": _E(), "ledger_k": 3}, "rust")
    assert base != sc.config_fingerprint(args, {"engine": _E(), "ledger_k": 99}, "py")
    e = _E()
    e.stp = "expire_maker"
    assert base != sc.config_fingerprint(args, {"engine": e, "ledger_k": 99}, "rust")


@needs_data
@pytest.mark.parametrize("backend", ["py", "rust"])
def test_hit_replays_exactly_like_a_fresh_build(backend, tmp_path, monkeypatch):
    if backend == "rust":
        pytest.importorskip("replay_core.replay_core")
    from replay.adapter import EngineAdapter
    monkeypatch.setenv("BOOK_REPLAY_SETUP_CACHE", str(tmp_path))
    ad = EngineAdapter(backend)
    outs = []
    for cache in (False, True, True):                     # fresh build, cache miss (stores), cache hit (loads)
        before = dict(sc.stats)
        book = ad.new_book(CHUNK, HL, cache=cache)
        if cache and outs and len(outs) == 2:
            assert sc.stats["hit"] == before["hit"] + 1
        fills = []
        for _ in range(N):
            for fs in ad.apply_block(book).values():
                fills += [key(f) for f in fs]
        outs.append(fills)
    assert outs[0] == outs[1] == outs[2] and outs[0]
    assert len(list(tmp_path.glob("*.pkl.lz4"))) == 1


def test_eviction_keeps_the_cache_under_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOK_REPLAY_SETUP_CACHE_GB", str(2.5e-9))   # 2.5 bytes
    for i in range(3):
        p = tmp_path / f"{i}.pkl.lz4"
        p.write_bytes(b"12")
        os.utime(p, (i, i))
    sc._evict(tmp_path, keep=tmp_path / "2.pkl.lz4")
    assert sorted(p.name for p in tmp_path.glob("*.pkl.lz4")) == ["2.pkl.lz4"]


@needs_data
@pytest.mark.skipif(os.environ.get("RUN_SLOW") != "1", reason="slow (3 full dev20 runs): set RUN_SLOW=1")
def test_every_dev20_score_identical_with_and_without_cache(tmp_path, monkeypatch):
    """Q9: scores are byte-identical without the cache, on a cache miss (build + store) and on a cache hit."""
    from replay import parity
    monkeypatch.setenv("BOOK_REPLAY_SETUP_CACHE", str(tmp_path / "cache"))
    dev20, hl = CHUNK.parent, HL
    outs = {}
    for name, extra in (("nocache", ["--no-cache"]), ("miss", []), ("hit", [])):
        outs[name] = tmp_path / name
        parity.main([str(dev20), "--hl-data", str(hl), "--out", str(outs[name]),
                     "--baseline", str(outs["nocache"]), *extra])
    assert sc.stats["hit"] >= 20
    assert parity.compare(outs["miss"], outs["nocache"]) == []
    assert parity.compare(outs["hit"], outs["nocache"]) == []
    assert len(list(outs["nocache"].glob("*.csv"))) >= 6
