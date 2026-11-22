"""Problem-page Examples 1, 2, 5, 6, 7 through the replay engine (both backends).

The examples assert what the CHAIN does (tests/chain_examples.py). The replay engine sees only the blocks, so where
an example depends on hidden account state (margin: Examples 2 and 5; positions: Example 7) the engine is expected
to fail it: those cases are strict xfails that document the known failure modes listed on the problem page (the
replay's margin gate and the M1 rules engine are where they are handled). Example 1 passes under the docs priority
rule (and fails under the old "known" rule); Example 6 passes with stp=maker and fails under the default stp=taker.
"""
import itertools

import pytest

from engine_py.matching import Engine
from engine_py.types import Place, Side, Tif
from tests.chain_examples import EXAMPLES

_i = itertools.count(1)
SIDE = {"B": Side.BUY, "A": Side.SELL}
TIF = {"Alo": Tif.ALO, "Gtc": Tif.GTC, "Ioc": Tif.IOC}


def _backends():
    try:
        import replay_core.replay_core  # noqa: F401
        return ["py", "rust"]
    except ImportError:
        return ["py"]


def make_engine(backend, **kw):
    if backend == "py":
        return Engine(**kw)
    from replay.rust_engine import RustEngine
    return RustEngine(**kw)


def run(ex, backend, **kw):
    """(fills of the scored step as [(maker, px, sz)], resting orders {user: [(side, px, sz)]})."""
    eng = make_engine(backend, **kw)
    scored = None
    for s in ex["steps"]:
        eng.block_time_ms = s["t"]
        a = Place(user=s["user"], block=1, tx_index=next(_i), item_index=0, hash=f"0x{next(_i)}",
                  priority=s.get("p", 0), asset=0, side=SIDE[s["side"]], px=s["px"], sz=s["sz"],
                  tif=TIF[s["tif"]], cloid=None, reduce_only=s.get("reduce_only", False))
        fills, _ = eng.apply(a)
        if s.get("scored"):
            scored = [(f.maker_user, f.px, f.sz) for f in fills]
    resting = {}
    for side in (Side.BUY, Side.SELL):
        for lv in eng.books[0].levels(side):
            for n in lv:
                resting.setdefault(n.order.user, []).append(("B" if side is Side.BUY else "A", lv.px, n.remaining))
    return scored, resting


def chain_says(name):
    e = EXAMPLES[name]["expect"]
    return e["fills"], e["resting"]


BACKENDS = _backends()


@pytest.mark.parametrize("backend", BACKENDS)
def test_ex1_docs_priority_matches_the_chain(backend):
    assert run(EXAMPLES["ex1_priority_window"], backend, p_priority="docs") == chain_says("ex1_priority_window")


@pytest.mark.parametrize("backend", BACKENDS)
def test_ex1_old_known_rule_gets_it_wrong(backend):
    fills, _ = run(EXAMPLES["ex1_priority_window"], backend, p_priority="known")
    assert fills == [("B", 100, 1)]                       # the bug the docs rule fixed


@pytest.mark.parametrize("backend", BACKENDS)
def test_ex6_expire_maker_matches_the_chain(backend):
    assert run(EXAMPLES["ex6_self_trade"], backend, stp="expire_maker") == chain_says("ex6_self_trade")


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.xfail(strict=True, reason="default stp=taker cancels the taker, the chain cancels the resting order")
def test_ex6_default_stp(backend):
    assert run(EXAMPLES["ex6_self_trade"], backend) == chain_says("ex6_self_trade")


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("name", ["ex2_ioc_no_margin", "ex5_alo_no_margin", "ex7_reduce_only_cancel"])
@pytest.mark.xfail(strict=True, reason="needs hidden account state (margin / position) the engine cannot see")
def test_examples_needing_account_state(backend, name):
    assert run(EXAMPLES[name], backend, p_priority="docs") == chain_says(name)
