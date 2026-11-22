"""Problem-page Examples 1, 2, 5, 6, 7 (docs/problem_page.md) as data: what the CHAIN does.

Shared by the replay-engine tests (tests/test_examples_engine.py) and, from M1 on, the simulator's rules engine
(replay/sim/rules_engine.py), which must reproduce every example exactly. Prices are ticks and sizes lots (ints).

Each example: `accounts` (hidden state the chain sees: free margin in USD, signed position in lots), a list of
`steps` applied in order, and `expect` = the fills the chain prints for the step marked `scored`, as
[(maker, px, sz)], plus the orders the chain leaves resting at the end ({user: [(side, px, sz)]}).
Step: dict(user, kind="order", side "B"/"A", px, sz, tif "Alo"/"Gtc"/"Ioc", p=0, t=ms, reduce_only=False).
"""
from __future__ import annotations

EXAMPLES = {
    # Example 1: a fee bid placed 2 s later cannot jump an order that has rested over 400 ms.
    "ex1_priority_window": dict(
        accounts={},
        steps=[
            dict(user="A", side="A", px=100, sz=1, tif="Alo", p=0, t=0),
            dict(user="B", side="A", px=100, sz=1, tif="Alo", p=5, t=2000),
            dict(user="T", side="B", px=100, sz=1, tif="Ioc", t=2100, scored=True),
        ],
        expect=dict(fills=[("A", 100, 1)], resting={"B": [("A", 100, 1)]}),
    ),
    # Example 2: an IOC from an account with no free margin is rejected (perpMarginRejected): no fill, and the
    # liquidity stays for the next real taker.
    "ex2_ioc_no_margin": dict(
        accounts={"bot": {"free_margin": 0.0, "position": 0}},
        steps=[
            dict(user="M", side="A", px=100, sz=2, tif="Alo", t=0),
            dict(user="bot", side="B", px=100, sz=1, tif="Ioc", t=100, scored=True),
        ],
        expect=dict(fills=[], resting={"M": [("A", 100, 2)]}),
    ),
    # Example 5: a post-only order from an account without margin never rests, so the next taker fills against
    # the real maker one tick up.
    "ex5_alo_no_margin": dict(
        accounts={"Z": {"free_margin": 0.0, "position": 0}},
        steps=[
            dict(user="M", side="A", px=101, sz=1, tif="Alo", t=0),
            dict(user="Z", side="A", px=100, sz=1, tif="Alo", t=100),
            dict(user="T", side="B", px=101, sz=1, tif="Ioc", t=200, scored=True),
        ],
        expect=dict(fills=[("M", 101, 1)], resting={}),
    ),
    # Example 6: on a self-trade the chain cancels the RESTING order and keeps matching.
    "ex6_self_trade": dict(
        accounts={},
        steps=[
            dict(user="U", side="A", px=100, sz=1, tif="Alo", t=0),
            dict(user="V", side="A", px=100, sz=1, tif="Alo", t=10),
            dict(user="U", side="B", px=100, sz=1, tif="Ioc", t=20, scored=True),
        ],
        expect=dict(fills=[("V", 100, 1)], resting={}),
    ),
    # Example 7: a reduce-only order the position can no longer reduce is canceled by the chain with no action
    # (reduceOnlyCanceled), so a later taker fills against the next real maker.
    "ex7_reduce_only_cancel": dict(
        accounts={"U": {"free_margin": 1e9, "position": 1}},          # U is long 1 lot
        steps=[
            dict(user="U", side="A", px=100, sz=1, tif="Alo", t=0, reduce_only=True),
            dict(user="V", side="A", px=101, sz=1, tif="Alo", t=10),
            dict(user="W", side="B", px=99, sz=1, tif="Alo", t=20),
            dict(user="U", side="A", px=99, sz=1, tif="Ioc", t=30),      # U sells 1 to W: now flat
            dict(user="T", side="B", px=101, sz=1, tif="Ioc", t=40, scored=True),
        ],
        expect=dict(fills=[("V", 101, 1)], resting={}),
    ),
}
