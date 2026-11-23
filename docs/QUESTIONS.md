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

# Plan v2 session questions (Nov 23)

## Q10. A2 (lead-in 300 / 600) needs blocks we do not have. How should we get them?

**Why it matters.** A2 is the only item that could move the baseline tonight. The plan calls it "only a config
change", but the lead-in is part of each captured chunk: capture.py fetches `LEAD_IN_BLOCKS` (150 since Nov 22
04:20; 75 before) blocks before the snapshot-aligned span, and nothing else. Measured tonight
(`handoff/plan_v2/a2_blocked.txt`): 1,149 captured chunks, lead-in 150 (953) or 75 (196), no two chunks adjacent (the
smallest gap between one chunk's end and the next one's start is 1,386 blocks). So no chunk of dev20 or dev-fast can be
replayed with 300 or 600 lead-in blocks from data on disk. Fetching the missing blocks now is not possible: they are
1-5 days old, and the fetcher refuses any block older than 30 min (the explorer's archived-block cap per IP per day
would otherwise stop the live capture until 00:00 UTC), and the explorer lock belongs to the running capture.

**Options.**
- (a) Change the live capture to `LEAD_IN_BLOCKS=600` (the env var already exists; it needs a capture restart, a
  gap of a few seconds in chunk capture only: the recorder keeps running). Each chunk then fetches 810 instead of 210
  blocks (~12 min instead of ~3 min at ~1.1 blocks/s), so the capture produces ~4x fewer chunks per hour. A 600-block
  chunk also gives 300 and 150 for free (`--lead-in-keep 300 / 150`), so A2 becomes a PAIRED test on the same
  scored blocks. ~20 chunks = ~4 h of capture. **Recommended**, but it touches the capture service and costs chunk
  volume, so it is your call.
- (b) Alternate: every k-th chunk with 600, the rest 150 (needs a small capture.py change: also your call).
- (c) Drop A2 for now. The history slope says the gain is small: Nov 20 (older engine, held lead20, pooled
  strict): 0 blocks 34.5%, 75 blocks 47.6%, 150 blocks 48.4%; the plan's +2.6 is 150 vs 75 on contested.

**Blocked / done instead.** A2's rule is in docs/scoreboard.md; no run, no baseline change. Nothing touched in the
capture. The new 600-block chunks would also need the dev-fast selection rule re-run on them (a "dev-fast-600" set).

## Q11. A0's ranking hangs on one tx, and the biggest cause has no A item. Which order for A3-A7?

**Why it matters.** The written rule orders A3-A7 by share of missed contested notional. On dev20 one 30-BTC sweep
(1.37 M USD of 2.28 M) is 60% of all missed notional; it alone puts A3 (IOC margin) first. Without it A3 has 0%.
By count, 26 of 59 misses (44%; 48% of notional without the outlier) are "queue order at the right price": the real
maker rests at that price in our book, but our queue fills others first. No A item targets that (A4 splits
placeholder levels; it does not reorder known orders).

**Options.**
- (a) Follow the rule literally: A3, A4, A5, A6, A7.
- (b) Rank by misses (count), which is robust to one tx: A4 (placeholders + pre-window "other": 18 misses) > A5 (14)
  > A3 (1) > A6 / A7 (0); and add an item for queue order among known orders (e.g. audit priority / evict-arrival /
  ledger choices on the 26 queue misses). **Recommended**: 44% of misses sit in a cause no item touches, and dev20's
  small contested set (n 113) makes notional shares fragile.
- (c) Re-run A0 on dev-fast (50 contested txs) before deciding: cheap (~25 min), more evidence.

**Blocked / done instead.** Nothing started on A3-A7 (out of tonight's scope). A0's counts and examples are in
docs/scoreboard.md.

## Q12. Commit attribution

A peer session (data-streams) says your rule is no Claude attribution in commits; my session instructions add a
`Co-Authored-By: Claude` trailer, and commits on mahito so far carry it (incl. d7339e6, plan v2). I have not
rewritten any history. Options: (a) keep the trailer; (b) drop it from now on (I need it in CLAUDE.md or memory to
override the session instruction). Your call.

# Session 3 questions (Nov 23)

## Q13. A4(a) "passes" the adoption rule with zero effect. Adopt, or hold until (b)?

`--placeholder-split` leaves every dev20 and dev-fast score byte-identical (+0.0 [0.0, 0.0] everywhere), so it
formally meets the rule (contested CI lower bound 0 >= 0, dev-fast upper bound 0 >= 0). Adopting it means porting it
to the Rust engine's refill for parity, for no measured gain. **Options.** (a) Adopt now (rule as written). (b) Hold it
as a switch and adopt only together with A4(b) (naming owners of the pieces), which is what can change scores
(**recommended**: a no-op should not move the baseline). A4(b)/(c) were not built tonight: A8's finding (21 of 26
queue misses are post-only orders the chain never held) took the session, and (b) needs a piece-level naming rule
written first. **Done instead:** nothing adopted; the switch is in the code, default off.

## Q14. A8 points at A6. Move A6 up?

A8's 26 queue misses are mostly phantoms: E's post-only order sits in our book, the chain never filled it and never
saw a cancel, and the only two checkable ones were perpMarginRejected. That is A6 (margin-rejected post-only orders),
currently ranked after A4, A5 and A3. **Options.** (a) Keep the order A4 > A5 > A3 > A6. (b) Put A6 right after A4
(**recommended**: by count it would now cover ~21 of A0's 59 contested misses, more than any other item). A6 needs
the alo_reject classifier (training on the order histories: Yuta owns the hist/margin data tests, so the split of
work needs your call too).

**Answers (Satya, Nov 23).** Q13: (b), hold `--placeholder-split` until A4(b) exists. Q14: yes, A6 moves to first, but
rule-based first: data-streams (Yuta) builds a margin gate for post-only placements (`--alo-reject margin`, same rule
as the IOC margin gate); the alo_reject classifier comes only after, if the rule leaves misses.

# Session 4 questions (Nov 23)

## Q15. A4(b) formally passes but moves nothing contested. Adopt, or hold?

A4(b1) (`--placeholder-split` + trade-learn `name`) vs B1: maker-strict dev20 contested +0.5 [+0.0, +1.9] (one tx),
uncontested +3.0 [+0.0, +6.3]; dev-fast contested +0.0 [0, 0], uncontested -1.1 [-7.2, +3.4]. (b2) (`--named-cancel`)
removes 40 / 30 named pieces but changes no scored tx, so it equals (b1). By the rule written before the run, (b1)
passes (contested lower bound 0 >= 0; dev-fast contested upper bound 0 >= 0), and the tie rule picks (b1).
Against it: the contested target does not move on either set; named pieces get the right maker only 67% (dev20) /
46% (dev-fast) of the time they fill; and per-tx recall drops by about what strict gains (a relabelling, see the
scoreboard). **Options.** (a) Adopt (b1) as B2 (the rule as written). Needs a Rust port first: the Rust parity run fails on every chunk with
`TypeError: RustEngine.refill() got an unexpected keyword argument 'counts'` (A4(a)'s split exists only in the Python
engine's refill; replay_core's refill takes no counts). Naming itself is in the Replay layer.
(b) Hold (b1) and (b2) as switches; A4 stops here, (c) size habits is not built; move on to A6 / A5
(**recommended**: no contested effect, and a 46% naming hit rate on the newer set is close to a coin flip).
(c) Change the adoption rule to require a contested gain (lower bound > 0, not >= 0) so no-op-on-contested switches
cannot pass; that would also have held A4(a).

**Answer (Satya, Nov 23).** (b): hold (b1) / (b2). Rule change (scoreboard, before the next run): adopt only if dev20
contested point estimate > 0 AND lower bound >= 0 AND dev-fast contested upper bound >= 0; owner-naming switches
also need naming precision >= 70% on dev20 and dev-fast. Ownership: mahito owns A4 (placeholders / pre-window naming) and A5 (exact oid counting + resolver);
Yuta sizes unresolved-cancel misses on dev_margin60 and sends cases; A6 (post-only margin gate) is Yuta's, not mahito's.
Data-gated rule tightened (overnight instructions): where the switch cannot act, byte-identical OR every differing tx
traced to a case where its data exists, plus contested not worse; and a passing pre-registered confirm where it can.

## Q16. A5: exact oid counting fails (89% same-block). Follow up, or close A5?

The pre-registered test found no scope where every order item takes the next oid (best: 89.1% of same-block pairs;
bar 99%), so no resolver was built. Ids the blocks don't show appear inside blocks, about one per few hundred items.
Exploratory hints (after the verdict): ids are consecutive within one tx; non-filling IOCs seem to take extra ids
(+0.15 each) and modifies of missing targets none. **Options.** (a) Close A5; keep today's resolver (**recommended**:
A0 put "lost order / wrong cancel target" at 1.1% of missed contested notional, and a probabilistic count is what the
interpolation resolver already does). (b) Pre-register a corrected count (IOC / modify weights fitted on dev20,
tested on dev-fast) as a better interpolation prior, then rerun the resolver's held-out check. (c) Wait for Yuta's
unmatched-cancel cases from dev_margin60 and decide from them whether cancel matching is worth more work.

