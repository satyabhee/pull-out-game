"""G2 determinism: per-block state hashes (engine_py/state.py) are identical across two runs with different
PYTHONHASHSEED values, and between the Rust and Python backends. Fast: seeded SIMULATED chunks (replay.sim), no real data.

Restart from snapshot is not built (tests/test_snapshot.py, docs/determinism_check.md).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from engine_py import state
from engine_py.matching import Engine

ROOT = Path(__file__).resolve().parents[1]
N_BLOCKS = 60


def test_encoding_is_hand_checkable():
    e = Engine()
    assert state.encode_books(e) == b"A0|B|S|A1|B|S"
    h0 = state.block_hash(e, None, 1)
    assert h0 == state.block_hash(e, "", 1)                       # first link: prev = ""
    assert state.block_hash(e, h0, 2) != h0                       # the chain carries order
    assert state.block_hash(e, h0, 2) == state.block_hash(e, h0, 99)   # height is not hashed


def _hashes(backend, window, hl, opts):
    from replay.adapter import EngineAdapter
    ad = EngineAdapter(backend)
    book = ad.new_book(window, hl, opts, cache=False)
    book.replay.hasher = state.block_hash
    out = []
    while book.next_height is not None:
        h = book.next_height
        ad.apply_block(book)
        out.append((h, book.replay._state_hash))
    return out


@pytest.fixture(scope="module")
def sim():
    pytest.importorskip("replay.sim.generate", reason="replay.sim not on this branch (unified only)")
    from replay.sim.config import SimConfig
    from replay.sim.generate import chunk_name, generate_chunk
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="g2sim"))
    cfg = SimConfig(seed=21, n_blocks=N_BLOCKS, lead_in=20)
    generate_chunk(cfg, root)
    return root / "windows" / chunk_name(SimConfig(seed=21, n_blocks=N_BLOCKS)), root / "hl_data"


SCRIPT = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tests.test_determinism import _hashes
from replay.adapter import BASELINE_OPTIONS
print(json.dumps(_hashes("py", Path(sys.argv[2]), Path(sys.argv[3]), BASELINE_OPTIONS)))
"""


def test_two_runs_different_hash_seeds_identical(sim):
    window, hl = sim
    runs = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT), str(window), str(hl)], env=env,
                           capture_output=True, text=True, check=True, cwd=ROOT)
        runs.append(json.loads(r.stdout.strip().splitlines()[-1]))
    assert len(runs[0]) >= N_BLOCKS and all(h for _, h in runs[0])
    assert runs[0] == runs[1]


def test_rust_equals_python_per_block_hashes(sim):
    pytest.importorskip("replay_core.replay_core", reason="replay_core extension not built")
    from replay.adapter import BASELINE_OPTIONS, options_for
    window, hl = sim
    for opts in (BASELINE_OPTIONS, options_for(order="class_sort")):
        py = _hashes("py", window, hl, opts)
        rs = _hashes("rust", window, hl, opts)
        assert py and py == rs, opts


def test_hash_sees_an_ordering_change(sim):
    """The hash is not blind: a different ordering policy changes the books, so the chain must diverge."""
    from replay.adapter import BASELINE_OPTIONS, options_for
    window, hl = sim
    a = _hashes("py", window, hl, BASELINE_OPTIONS)
    b = _hashes("py", window, hl, options_for(order="class_sort"))
    assert [x for x in a] != [x for x in b]
