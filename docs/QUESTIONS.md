# Open questions for Satya (overnight Nov 22-23)

Each entry: the question, why it matters, options with a recommendation and evidence, what is blocked vs what
was done instead. Most important first.

## Q1. Should the default baseline be block-end (as committed) or mid-block?

**Why it matters.** Every parity test and every "baseline" number uses the default. Commit `283f077` (before you
left) set the default to block-end corrections, following your decision 1 ("if the contested CI includes ~0,
block-end becomes the official baseline"; measured contested +0.0 [0.0, 0.0] on dev20). Tonight's instructions say
"the block-end vs mid-block baseline choice stays MINE: measure and report it, don't switch it" and "never change
any default away from the current baseline". Your "go" did not say which one you meant.

**Options.**
- (a) Keep block-end as the default (as committed). Recommended: it is what your decision 1 asked for, it is the
  counterfactual-clean setting (rule 1 test passes), and it cost nothing on contested txs (dev20 contested
  +0.0 [0.0, 0.0], uncontested -2.0 [-5.4, 0.0], all-scored -1.3 [-3.5, 0.0]; `handoff/m0/tl20.paired.txt`).
- (b) Revert the default to mid-block (the 58.8% final240 configuration) and keep block-end as the switch
  `correction=blockend`. Then `tests/test_lookahead.py` would mark the baseline as a known rule-1 violation again.

**Blocked / done instead.** Nothing is blocked: I left the code exactly as committed (no further switch in either
direction), and every Rust parity check runs under BOTH settings (`correction=blockend` and `midblock`), so either
answer is already covered. Reverting is a one-line change in `replay/adapter.py` (`BASELINE_SWITCHES`).

## Q2. Rust engine shape: per-action drop-in, or the whole block step in Rust?

**Why it matters.** The plan wants one Python->Rust crossing per block. The Python replay calls the engine per action
and reaches into its internals between actions (ALO gate on the real touch, ledger evictions, placeholder
refills, trade corrections, the oid resolver called back from inside a cancel). A per-block crossing means porting
that replay logic (engine_py/replay.py, 1,250 lines; data/oids.py resolver, 700 lines) too.

**Options.**
- (a) Tonight: the engine as a per-action drop-in with the Python engine's exact API (`books`, levels, nodes,
  `by_user`, `apply`, `reconcile`, ...), Replay unchanged. Recommended: smallest change, exact parity is
  testable per layer, and D14 measured matching at ~2% of a chunk, so per-action crossing costs little today.
- (b) Port the per-block replay logic to Rust as well (gate, ledger, learn, resolver) so a block is one call.
  Needed for M2 speed (N particles x per-block work), much larger and riskier.
- (c) Hybrid: (a) now, then move only what M2's profile shows hot.

**Blocked / done instead.** Building (a). Nothing in (a) prevents (b) or (c) later.
