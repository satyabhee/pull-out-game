# Open questions for Satya (overnight Nov 22-23)

**All five answered by Satya on Nov 22** (Q1: re-measure, keep block-end if contested CI includes ~0; Q2: split
shared inputs from per-guess state as M2's first step; Q3: Rust becomes the default; Q4: no more porting, add a
per-chunk setup cache instead; Q5: docs/problem_page.md added, write the Example tests). Kept below for the record.

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

**Blocked / done instead.** Built (a), commit `7af9cd2`: identical fills on dev20 (both correction settings) and in all
8 switch combinations. Measured since (`docs/m5_costs.md`): per-call engine work is only ~1.4x faster in Rust
(0.45 s -> 0.31 s per chunk) because each action still crosses Python->Rust through the wrapper, but an engine-state
copy is ~2,000x cheaper (43 ms -> 0.02 ms). What a full M2 particle copy costs today is dominated by the replay's
OTHER state (`copy_book` = 0.80-0.85 s in both backends: oid resolver facts, touch, ledger). Updated
recommendation: (c). For M2, first split the Replay's state into "shared window inputs" (decoded blocks, snapshots,
oid facts, touch events: never copied) and "per-guess state" (engine + a few counters), then profile N=16/64 and
port to Rust only what that profile shows. This needs your OK because it shapes M2 (not started, per scope).

## Q3. Switch the adapter's default backend to Rust?

**Why it matters.** The plan says the default changes "only after the user signs off". Everything above the adapter
would then run on Rust by default.

**Options.**
- (a) Switch now. Evidence: full dev20 parity (block-end and mid-block), 8 switch combinations, 1,500 random
  differential engine sequences, 400 book sequences, 3,000+400 subset cases; whole-run median 1.25x faster
  (subset stage; full-engine stage in `docs/m5_costs.md`).
- (b) Keep "py" as the default until M2 needs Rust (recommended: nothing tonight needs the speed, and the Python
  engine is the reference you read; switching is one argument default in `replay/adapter.py`).

**Blocked / done instead.** Nothing blocked; default left at "py" as the rules require.

## Q4. Plan's M5 stop rule ("< 3x on the hot path: stop and report")

**Why it matters.** It decides whether more of the replay is worth porting before M1.

**Evidence.** The one-guess hot path is the ledger subset search (85-92% of per-block time): Rust 7x faster there,
above the bar. The engine (matching) is ~2% of a chunk and only ~1.4x faster per call through the Python wrapper:
below the bar, so by the plan's rule nothing more should be ported for speed until M2 profiling says so. Setup
(Python loaders, 16-20 s of a ~23 s chunk) dominates one-guess runs and stays in Python by design.

**Options.** (a) Stop porting here, go to M1 after review (recommended); (b) port the per-block replay logic (Q2 b).

## Q5. Where is the "problem page" (Examples 1, 2, 5, 6, 7)?

**Why it matters.** M5 (and M1) ask for unit tests "built from the problem page's Examples 1, 2, 5, 6 and 7, each
asserting what the chain does". The page is not in the repo: `MAHITO_PLAN.md` only cross-references it (Ex 1 = the
400 ms priority rule, Ex 2 ~ P2 IOC margin, Ex 5 ~ P4 ALO margin, Ex 7 ~ P6 reduce-only, Ex 8 ~ P5 batches), and
`puzzles.py` covers P1-P6, not the Examples. Writing the tests from those one-line hints would mean inventing what each
example asserts.

**Options.** (a) Add the page (or paste the five examples) to the repo, and I turn each into a Rust + Python test
(recommended). (b) Accept rule-based tests instead: the Rust engine already has rule tests for the 400 ms priority
window (`engine::tests::docs_priority_jumps_only_young_lower_p_alos`), ALO would-cross, both self-trade modes,
cloid cancels and scheduleCancel; margin and reduce-only are outside the current engine (unobservable, per §1).

**Blocked / done instead.** The Example-based tests are not written. Rule-based tests and differential tests against
the Python engine cover the same engine paths.

# M1 checkpoint questions (Nov 22)

## Q6. Add `pyarrow` for parquet truth logs?

**Why it matters.** The plan says truth logs are compressed parquet; that needs `pyarrow` (a new dependency, ~100 MB
installed). **Options.** (a) Add pyarrow and switch `replay/sim/truth.py` (one function each way). (b) Keep lz4 JSON
lines (~0.9 MB per chunk, read in ~0.1 s; recommended until M3's Polars features need columnar files).
**Blocked / done instead.** Nothing blocked; lz4 JSON lines today.

## Q7. Fix the trade-correction "queue" rule the simulator caught?

**Why it matters.** With nothing hidden, the baseline's block-end trade corrections drop strict recall from 98.3% to
92.0% (docs/deviations.md D31): when the real maker filled with an old quote and also has a newer quote at that price,
the rule deletes every real order ahead of the newer one. On real data it is tuned to remove phantoms, so a fix could
move the real baseline either way. **Options.** (a) Restrict the rule to orders ahead of the maker's order that was
actually old enough to have been filled (first order placed before the tx's block); measure on dev20 + sim200.
(b) Leave it (recommended until (a) is measured: it is a Python engine behaviour change, your call).
**Blocked / done instead.** Nothing changed in the engine.

## Q8. Calibrate the sampler toward real statistics now, or in M2/M3?

**Why it matters.** The simulated market differs a lot from dev20 (5x the trades per block, levels with far fewer
orders, 10x fewer post-only margin rejections, no open-order cap; docs/m1_report.md). Simulator-based measurements
(M2 recovery rates) are only as good as this match. **Options.** (a) One tuning pass now (taker rates x0.2, more and
smaller MM orders per level, a broke quoter agent, an open-order cap), regenerate sim200 (90 s), re-run calibration
and the baseline (recommended: cheap, and M2 needs it). (b) Defer to M3 profiles. **Blocked / done instead.** The
default suite is the untuned v1; nothing in M1's done-criteria depends on the answer.

## Q9. Cache the scoring-side inputs too?

**Why it matters.** The setup cache made dev20 1.59x faster (355.6 -> 223.5 s). Most of the remaining ~10 s per
chunk is `analysis/validate_replay.run` reading the real trades, ALO truth and tracked-user streams from the recorder
files after the Replay is built. Caching them (same key scheme) would likely bring a dev20 run near the ~40 s of pure
block replay. **Options.** (a) Extend the cache to those inputs: a change inside the scoring module's input loading
(no change to scoring logic), so it needs your OK. (b) Leave scoring untouched (current). **Blocked / done instead.**
Nothing; the cache covers the built Replay only.
