"""replay/adapter.py (M0). Real-data tests use one dev20 chunk and are skipped when the data is not on disk."""
import os
from itertools import islice
from pathlib import Path

import pytest

from analysis import validate_replay as vr
from engine_py.replay import Replay
from engine_py.types import Side
from replay.adapter import (BASELINE_OPTIONS, EMPTY, BranchContext, EngineAdapter, options_for, replay_args,
                            run_kwargs_from_options, split_policy)

M3 = Path(os.environ.get("BOOK_REPLAY_DATA", Path.home() / "hl-data" / "mac3"))
CHUNK = M3 / "dev20" / "1227159597-1227159806"
HL = M3 / "hl_data"
needs_data = pytest.mark.skipif(not CHUNK.exists() or not HL.exists(), reason="dev20 data not on this machine")
N = 40                                              # blocks compared (keeps the test ~1 min)


def key(f):
    return (f.taker_hash, f.maker_order, f.maker_user, f.px, f.sz, f.block, f.seq)


def test_empty_context():
    assert EMPTY.is_empty()
    assert not BranchContext(priority_mode="known").is_empty()


def test_backends():
    assert EngineAdapter().backend == "rust"        # default since Nov 22 (Q3); "py" is the reference
    assert "--trade-learn-at" in BASELINE_OPTIONS and BASELINE_OPTIONS[BASELINE_OPTIONS.index("--trade-learn-at") + 1] == "block-end"
    with pytest.raises(ValueError):
        options_for(order="fifo")
    with pytest.raises(ValueError):
        EngineAdapter(backend="cpp")


@needs_data
def test_rust_backend_same_fills_as_python():
    pytest.importorskip("replay_core.replay_core", reason="replay_core extension not built")
    outs = {}
    for backend in ("py", "rust"):
        ad = EngineAdapter(backend)
        book = ad.new_book(CHUNK, HL)
        fills = []
        for _ in range(N):
            for fs in ad.apply_block(book).values():
                fills += [key(f) for f in fs]
        outs[backend] = fills
    assert outs["py"] == outs["rust"] and outs["py"]


def fresh_replay(options=BASELINE_OPTIONS) -> Replay:
    """A new Replay as validate_replay.run would build it. Captured args carry their own Engine, so they are
    never reused across Replays."""
    policy, opts = split_policy(options)
    a, k = replay_args(CHUNK, HL, policy, **run_kwargs_from_options(HL, opts))
    return Replay(*a, **k)


@needs_data
def test_replay_args_restores_scoring_module():
    fresh_replay()
    assert vr.Replay is Replay


@needs_data
def test_apply_block_equals_engine_iteration():
    ref = [f for _, fills, _, _ in islice(iter(fresh_replay()), N) for f in fills]
    ad = EngineAdapter("py")                        # wraps a Python-engine Replay
    book = ad.book_from_replay(fresh_replay())
    got = []
    for _ in range(N):
        for fs in ad.apply_block(book, book.next_height).values():
            got += fs
    assert sorted(map(key, got), key=lambda k: k[-1]) == [key(f) for f in ref]
    assert book.copies == 0                         # no copies without branching


@needs_data
def test_new_book_and_errors():
    ad = EngineAdapter()
    book = ad.new_book(CHUNK, HL)
    first = book.next_height
    with pytest.raises(ValueError):
        ad.apply_block(book, first + 1)
    with pytest.raises(NotImplementedError):
        ad.apply_block(book, first, BranchContext(priority_mode="known"))
    ad.apply_block(book, first)
    assert book.next_height == first + 1 or book.next_height > first
    for asset in (0, 1):
        for side in (Side.BUY, Side.SELL):
            top = ad.top_levels(book, asset, side, 5)
            pxs = [t[0] for t in top]
            assert pxs == sorted(pxs, reverse=side is Side.BUY)
            for px, sz, n in top:
                assert ad.level_view(book, asset, side, px) == (sz, n) and sz > 0 and n > 0
    bid = ad.top_levels(book, 0, Side.BUY, 1)[0][0]
    ask = ad.top_levels(book, 0, Side.SELL, 1)[0][0]
    assert bid < ask                                # never crossed
    assert ad.level_view(book, 0, Side.BUY, ask + 10_000) == (0, 0)


def _backends():
    try:
        import replay_core.replay_core  # noqa: F401
        return ["py", "rust"]
    except ImportError:
        return ["py"]


@needs_data
@pytest.mark.parametrize("backend", _backends())
def test_copy_is_independent_and_identical(backend):
    ad = EngineAdapter(backend)
    book = ad.new_book(CHUNK, HL)
    for _ in range(10):
        ad.apply_block(book)
    twin = ad.copy_book(book)
    assert book.copies == 1
    out_twin = [ad.apply_block(twin) for _ in range(15)]      # advance the copy first
    out_orig = [ad.apply_block(book) for _ in range(15)]      # the original must be unaffected by it
    norm = lambda outs: [{h: [key(f) for f in fs] for h, fs in o.items()} for o in outs]
    assert norm(out_twin) == norm(out_orig)
    assert twin.replay.blocks is book.replay.blocks           # decoded inputs shared, not copied
