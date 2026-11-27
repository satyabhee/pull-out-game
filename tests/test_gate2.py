"""Gate 2 estimator on hand-checkable blocks (docs/gate2.md section 7)."""
from decimal import Decimal as D

from analysis.gate2 import GFill, block_values, cluster_boot, markout


def f(maker, side, px, sz, user="m"):
    # maker side "S" = maker sold (taker bought)
    return GFill(chunk="c", height=7, taker_hash="t", maker=maker, maker_user=user, maker_side=side,
                 px=D(px), sz=D(sz), coin="BTC", time_ms=0)


def test_markout_sign():
    assert markout(f("a", "S", "100", "2"), D("99")) == D("2")      # sold at 100, mid fell to 99: +1 x 2
    assert markout(f("a", "B", "100", "2"), D("99")) == D("-2")     # bought at 100, mid 99: -1 x 2


def test_dodged_fill_and_absorbed_fill():
    # Maker A quotes ask 100 x 1 and cancels it in the block (raced); maker B rests ask 101 x 1 behind.
    # A taker buys 1. ON (cancel first): taker fills B at 101. OFF (taker first): taker fills A at 100 (stale).
    # Price then rises: mid10 = 103.
    on = [f("B", "S", "101", "1", user="b")]
    off = [f("A", "S", "100", "1", user="a")]
    mid = {("BTC", 0): D("103")}
    v = block_values(on, off, raced={"A"}, mid10=lambda c, t: mid[(c, t)])
    # A: ON 0, OFF (100 - 103) * 1 = -3 -> V_race = 0 - (-3) = +3 (dodged)
    # B: ON (101 - 103) * 1 = -2, OFF 0 -> V_absorb = -2 (absorbed)
    assert v["V_race"] == D(3) and v["V_absorb"] == D(-2) and v["V_net"] == D(1)
    assert (v["n_race_on"], v["n_race_off"], v["n_absorb_on"], v["n_absorb_off"]) == (0, 1, 1, 0)


def test_no_op_block_is_zero():
    same = [f("A", "S", "100", "1"), f("B", "B", "99", "2")]
    v = block_values(same, list(same), raced=set(), mid10=lambda c, t: D("100"))
    assert v["V_race"] == 0 and v["V_absorb"] == 0 and v["V_net"] == 0 and v["changed"] is False


def test_fill_without_mid_is_dropped_and_counted():
    v = block_values([f("A", "S", "100", "1")], [], raced=set(), mid10=lambda c, t: None)
    assert v["V_absorb"] == 0 and v["dropped_no_mid"] == 1


def test_cluster_boot_is_seeded_and_resamples_chunks():
    per_chunk = {"a": 1.0, "b": 3.0, "c": 2.0}
    r1 = cluster_boot(per_chunk, reps=500, seed=1)
    r2 = cluster_boot(per_chunk, reps=500, seed=1)
    assert r1 == r2 and r1[0] == 6.0 and 3.0 <= r1[1] <= r1[2] <= 9.0
