"""M5 parity per switch combination (Satya, Nov 22): every stp x order x correction setting that exists in Python
gives identical fills with backend "rust" and "py" on the fast test set (one dev20 chunk, first N blocks).

Slow (16 replays, ~5 min): runs only with RUN_SLOW=1; run at every milestone checkpoint. The full dev20 parity of
the baseline setting is replay/parity.py --backend rust.
"""
import itertools
import os

import pytest

from replay.adapter import SWITCHES, EngineAdapter, options_for
from tests.test_adapter import CHUNK, HL, N, fresh_replay, key, needs_data

pytestmark = [
    needs_data,
    pytest.mark.skipif(os.environ.get("RUN_SLOW") != "1", reason="slow: set RUN_SLOW=1"),
]
COMBOS = list(itertools.product(*(sorted(SWITCHES[s]) for s in ("stp", "order", "correction"))))


@pytest.mark.parametrize("stp,order,correction", COMBOS)
def test_rust_equals_python(stp, order, correction):
    pytest.importorskip("replay_core.replay_core", reason="replay_core extension not built")
    opts = options_for(stp=stp, order=order, correction=correction)
    outs = {}
    for backend in ("py", "rust"):
        ad = EngineAdapter(backend)
        book = ad.book_from_replay(fresh_replay(opts))
        fills = []
        for _ in range(N):
            for fs in ad.apply_block(book).values():
                fills += [key(f) for f in fs]
        outs[backend] = fills
    assert outs["rust"] == outs["py"] and outs["py"], (stp, order, correction)
