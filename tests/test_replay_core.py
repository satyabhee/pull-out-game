"""replay_core (Rust, M5) vs the Python reference. Skipped when the extension is not built
(build: `cd replay_core && maturin develop --uv --release`)."""
import random

import pytest
from hypothesis import given, settings, strategies as st

from engine_py.replay import unique_subset as py_subset

rc = pytest.importorskip("replay_core.replay_core", reason="replay_core extension not built")


@settings(max_examples=3000, deadline=None)
@given(st.lists(st.integers(1, 40), max_size=14), st.integers(0, 14), st.integers(0, 300), st.integers(1, 400))
def test_same_answer_as_python(sizes, k, target, budget):
    assert rc.unique_subset(sizes, k, target, budget) == py_subset(sizes, k, target, budget)


def test_real_scale_levels():
    """Touch-level shapes like the ledger sees (up to 60 orders, lot sizes up to 1e9, k up to 99)."""
    rnd = random.Random(7)
    seen = set()
    for _ in range(400):
        m = rnd.randint(1, 60)
        sizes = [rnd.choice([rnd.randint(1, 10**9), rnd.randint(1, 50) * 1000]) for _ in range(m)]
        k = rnd.randint(0, m)
        pick = rnd.sample(range(m), k)
        target = sum(sizes[i] for i in pick) + rnd.choice([0, 0, 0, 1, -1000])
        got = rc.unique_subset(sizes, k, target, 300_000)
        assert got == py_subset(sizes, k, target, 300_000)
        seen.add(got if isinstance(got, str) else "unique")
    assert {"unique", "budget", "none"} <= seen     # the cases that matter are exercised


def test_overflow_is_refused():
    with pytest.raises(OverflowError):
        rc.unique_subset([2**62, 2**62], 1, 2**62)
