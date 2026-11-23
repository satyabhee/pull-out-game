"""Rust vs Python parity on seeded SIMULATED chunks, for every stp x order x correction switch combination.

Needs no real data, so CI runs it on every push (real-data parity is tests/test_switch_parity.py and replay.parity,
which skip without ~/hl-data). The simulated chunks carry every hidden-event path the replay has to handle
(pre-window orders, int-oid and cloid cancels, margin and open-order-cap rejections, batch cuts, burned oids), and
are read through the real loaders.
"""
import itertools

import pytest

from replay.adapter import SWITCHES, EngineAdapter, options_for
from replay.sim.config import SimConfig
from replay.sim.generate import chunk_name, generate_chunk

pytest.importorskip("replay_core.replay_core", reason="replay_core extension not built")
COMBOS = list(itertools.product(*(sorted(SWITCHES[s]) for s in ("stp", "order", "correction"))))
SEEDS = (11, 12)


@pytest.fixture(scope="module")
def sim_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("sim")
    for s in SEEDS:
        generate_chunk(SimConfig(seed=s, n_blocks=80, lead_in=30), root)
    return root


def fills(backend, window, hl, opts):
    ad = EngineAdapter(backend)
    book = ad.new_book(window, hl, opts, cache=False)
    out = []
    while book.next_height is not None:
        h = book.next_height
        for taker, fs in ad.apply_block(book).items():
            out += [(h, taker, f.maker_order, f.maker_user, f.px, f.sz, f.seq) for f in fs]
    return out


@pytest.mark.parametrize("stp,order,correction", COMBOS)
def test_rust_equals_python_on_sim_chunks(sim_root, stp, order, correction):
    opts = options_for(stp=stp, order=order, correction=correction)
    for s in SEEDS:
        window = sim_root / "windows" / chunk_name(SimConfig(seed=s, n_blocks=80))
        py = fills("py", window, sim_root / "hl_data", opts)
        rs = fills("rust", window, sim_root / "hl_data", opts)
        assert py and py == rs, (s, stp, order, correction)
