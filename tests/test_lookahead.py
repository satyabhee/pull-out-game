"""MAHITO_PLAN rule 1 (no look-ahead): a block's evidence (its printed trades) may update state only AFTER that
block's fills are recorded. The replay reads a tx's real trades in Replay._learn (trade_learn); the guard fails
when any book action of block h is applied after a _learn of block h, i.e. when real trades of h were read
before out[h] was complete.

The baseline (58.8% config) runs trade_learn per tx (CLI default --trade-learn-at tx), which reads real trades
MID-block: that case is marked xfail(strict) until Satya decides (docs/deviations.md D4). --trade-learn-at
block-end must pass.
"""
import os
from itertools import islice
from pathlib import Path

import pytest

from engine_py.replay import Replay
from replay.adapter import BASELINE_OPTIONS, replay_args, run_kwargs_from_options

M3 = Path(os.environ.get("BOOK_REPLAY_DATA", Path.home() / "hl-data" / "mac3"))
CHUNK = M3 / "dev20" / "1227159597-1227159806"
HL = M3 / "hl_data"
pytestmark = pytest.mark.skipif(not CHUNK.exists() or not HL.exists(), reason="dev20 data not on this machine")
N = 60


def violations(options) -> tuple[int, int]:
    """(blocks where a book action followed a real-trade read in the same block, blocks with any read)."""
    a, k = replay_args(CHUNK, HL, "listing", **run_kwargs_from_options(HL, options))
    rp = Replay(*a, **k)
    state = {"block": None, "read": False, "bad": set(), "reads": set()}
    learn, apply = rp._learn, rp._apply

    def guarded_learn(tx_hash):
        state["read"] = True
        state["reads"].add(state["block"])
        return learn(tx_hash)

    def guarded_apply(block, action):
        if state["block"] != block.height:
            state.update(block=block.height, read=False)
        elif state["read"]:
            state["bad"].add(block.height)
        return apply(block, action)

    rp._learn, rp._apply = guarded_learn, guarded_apply
    for _ in islice(iter(rp), N):
        pass
    return len(state["bad"]), len(state["reads"])


def test_block_end_learning_reads_no_trades_before_fills():
    bad, reads = violations([*BASELINE_OPTIONS, "--trade-learn-at", "block-end"])
    assert reads > 0                                  # the guard is live: trades are read
    assert bad == 0


@pytest.mark.xfail(strict=True, reason="baseline reads real trades mid-block (deviation D4, awaiting decision)")
def test_baseline_reads_no_trades_before_fills():
    bad, _ = violations(list(BASELINE_OPTIONS))
    assert bad == 0
