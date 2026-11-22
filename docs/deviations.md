# Deviations from MAHITO_PLAN.md forced by the real code

The real engine wins (plan §0). Each entry: what the plan says, what the code does, what was done.
**Needs a decision** marks entries that are Satya's call; until then the code follows the current engine.

## M0 · Idle

**D0. Branch.** The plan's work branch `capture-guard` was 85 commits behind `data-streams`, where the engine lives.
By Satya's choice (Nov 22), M0 runs on sub-branch `mahito` = `capture-guard` + a clean merge of `data-streams`
(a322eec). `capture-guard` is unchanged.

**D1. Baseline number.** The plan's §1 says contested strict is ~51-53% on final240. The current engine (frozen config
+ `--p-priority docs`, i.e. the plan's failure-mode #1 fix) measures **58.8%** contested strict (union scope; 0.513 ->
0.588, +7.5 [+5.7, +9.8]), 63.2% real-makers scope, 62.7% all-scored on final240
(`handoff/pdocs_final240.paired.txt`). The adapter's baseline is that config (`replay/adapter.BASELINE_OPTIONS`).

**D2. `new_book(snapshot)` -> `new_book(window, hl_data, options)`** (options may include `--policy`). The engine is window-driven: it
seeds from the recorder snapshot nearest the window start, re-seeds at later snapshots (rule 8), gates ALOs on the
real touch and resolves cancels from window files. A `BookHandle` therefore wraps a whole `Replay`; `apply_block`
advances it one block via `Replay.step_block`. `book_from_replay(rp)` wraps a Replay built elsewhere (the scoring path).

**D3. `level_view` / `top_levels` take an `asset` and return integers.** The plan's signatures have no asset (one
handle holds BTC and ETH) and return float sizes. Project rule (AGENT_PLAN §2.1): no floats for prices or sizes. They
return `(lots, n)` and `(px ticks, lots, n)`; convert with `ASSETS[a].lots_to_sz / ticks_to_px` at the edge.

**D4. Rule 1 (no look-ahead) vs the baseline. DECIDED Nov 22: block-end is the official baseline.** The 58.8%
engine's trade corrections (`--trade-learn sweep,queue`, `--trade-learn-at tx`) read each tx's REAL trades right
after it, mid-block, which breaks rule 1. Satya's rule: measure block-end + `--p-priority docs` on dev20; if the
contested CI includes ~0, block-end becomes the baseline. Result (`report/validate/m0_tl20`, paired chunk-cluster
bootstrap 4000, `handoff/m0/tl20.paired.txt`):

| maker-strict per-tx, dev20 | n / chunks | mid-block -> block-end | diff [95% CI] |
|---|---|---|---|
| contested (union) | 112 / 18 | 0.592 -> 0.592 | +0.0 [0.0, 0.0] (no tx changed) |
| contested (real makers) | 92 / 17 | 0.661 -> 0.661 | +0.0 [0.0, 0.0] |
| uncontested | 196 / 20 | 0.730 -> 0.710 | -2.0 [-5.4, 0.0] |
| all scored | 308 / 20 | 0.680 -> 0.667 | -1.3 [-3.5, 0.0] |

So `replay.adapter.BASELINE_OPTIONS` = `options_for(stp="taker", order="proposer", correction="blockend")`, and
`tests/test_lookahead.py` requires it to pass rule 1 (and requires the guard to catch `correction="midblock"`).
The final240 58.8% is recorded as the **"mid-block correction"** number; final240 has not been re-run with
block-end (Satya: ask before any final240 run).

**D5. Self-trade.** Plan §1: the chain cancels the RESTING order and keeps matching (`expire_maker`). The engine's
default and the baseline use `stp="cancel_taker"` (cancel the taker's remainder). Not changed in M0 (scoring must
stay identical); flag for M5/M1, where the rules engine is written from the rules.

**D6. In-block class sort.** Plan §1: txs run in class order (no GTC/IOC, then cancels, then GTC/IOC) per
consensus batch. The baseline runs `listing` (proposer order); `class` exists as `analysis/validate_replay.class_policy`.
Which policy is "actual" is Satya's decision (AGENT_PLAN §5.3); unchanged here.

**D7. `BranchContext`.** Fields defined (`cancel_targets`, `rejected`, `batch_cuts`, `priority_mode`), but
`apply_block` raises `NotImplementedError` for a non-empty context until M2 adds the hooks. Empty context = today's
engine exactly.

**D8. `copy_book`.** Deep copy of the whole Replay (engine, resolver, touch, learn state), sharing the decoded
blocks and snapshots and giving the copy a no-op observer. Cost in `docs/baseline_costs.md`. M2's copy-on-write
levels replace it (plan: Rust core).

**D9. Commit guards.** `make check` runs the secret check, the full pytest suite (incl. rule-1 and adapter tests)
and, once `replay_core/` exists, cargo test + clippy. Rust-vs-Python parity is skipped until M5. The pre-commit
hook (`scripts/git-hooks/pre-commit`) is enabled for THIS worktree only (Satya, Nov 22:
`git config extensions.worktreeConfig true; git config --worktree core.hooksPath scripts/git-hooks`); the other
worktrees (other sessions) are unaffected (checked: `~/book-replay-data` sees no hooksPath).

**D10. Private-key check.** A bare `0x` + 64-hex string is not refused: Hyperliquid tx hashes have exactly that shape.
The check refuses 64-hex values assigned to key-like names, PEM blocks, `.env*` / `*.key` / `*.pem` files and model
files over 1 MB. Keys must come from env vars (rule 4).

**D11. Order histories.** The plan cites "1.73M order histories". On disk: `windows/<chunk>/hist_orders.json` in 382
windows, 4.39M `historicalOrders` records for 66 users, overlapping between chunks (each answer is a user's recent
history). The de-duplicated count was not computed in M0.

**D12. `l2Book fast`.** Recorded (`book_{COIN}_fast.jsonl`) but no loader exists, and dev20 / confirm68 / final240
predate the recording (Nov 21 22:56 UTC). Answer to the plan's open question: not covered; M2's feed likelihood would
use bbo only on those sets.

**D13. Layout.** `replay/` is a new top-level package next to the engine's `engine_py/`, `data/`, `analysis/`
(not a move of the engine). `replay/parity.py` is added (not in the plan's layout).

## M5 · Isomer

**D14. What is hot.** Plan M5 ports book + matching first. Measured on 3 dev20 chunks (plain timers, baseline
config): per chunk, setup (Python loaders, decoding, oid facts) 16.5-20.0 s; blocks 4.1-11.6 s, of which the touch
ledger's subset search (`engine_py/replay.py::unique_subset`) is 3.4-10.6 s (85-92%) and `Engine.apply`
(matching) 0.26-0.45 s (~2% of the chunk). So M5 ports `unique_subset` first (pure function, exact port) and the book
/ matching second, with copy-on-write from the start (Satya, decision 3). The book/matching port cannot pay off for
one-guess replays (<= 0.45 s of ~25 s per chunk); its value is M2, where per-guess work multiplies.

**D15. Build.** The crate has its own `replay_core/pyproject.toml` (maturin backend) instead of making the root
`pyproject.toml` (hatchling, the engine's package) build it: `pip install -e .` keeps installing the Python engine;
the crate installs with `make rust` (= `maturin develop --uv --release` into this worktree's `.venv`; the venv has
no pip, hence `--uv`). `pyo3/extension-module` is an optional cargo feature that only maturin enables, so
`cargo test` links. Versions pinned: pyo3 0.23.4 (abi3-py312), proptest 1.6.0, maturin 1.8.1. The compiled module is
`replay_core.replay_core` (the crate folder at the repo root would otherwise shadow it as a namespace package).

**D16. Switches (Satya, decision 2).** `replay.adapter.options_for(stp, order, correction)` maps each switch to an
option the Python engine already has: stp taker|maker -> `--stp cancel_taker|expire_maker`; order
proposer|class_sort -> `--policy listing|class`; correction midblock|blockend -> `--trade-learn-at tx|block-end`.
All 8 combinations exist in Python; `tests/test_switch_parity.py` checks Rust = Python for each on the fast test
set (first 40 blocks of one dev20 chunk). It takes ~2 min, so it runs at checkpoints (`make checkpoint`,
`RUN_SLOW=1`), not on every commit. Per-switch accuracy on dev20 is measured after M5, as decided.

**D17. Crossing granularity.** The plan wants one Python->Rust crossing per block. Today the ported piece is called
once per touch level per block (<= 4 calls per block), well under the per-tx crossing the plan forbids. When the
engine itself moves to Rust, the replay's per-action Python logic (ALO gate on the real touch, taker rules, oid
resolver callback) sits between engine calls; whether that logic moves too or the engine is called per action is
decided by profiling the ported book (D14 says matching is not the bottleneck today).

**D18. Timings.** Satya: M5 timings are the median of >= 3 runs with data capture paused. The ingest processes
(`record_hl.py`, `capture.py`) are not touched by these agents, so timing runs wait until Satya pauses capture.
Correctness runs (parity) do not need it.

**D19. Book port shape.** The Rust book (`replay_core/src/book.rs`) keeps each queue as a `Vec` (head = index 0)
instead of the Python doubly linked list: same order, same results, simpler code; cancel is O(level size) instead of
O(1), with levels of at most a few dozen orders. The priority rule (`jumps`) stays a Python callable evaluated by
`replay/rust_book.py`, which hands Rust the resulting queue position. Level objects are live views and iteration
snapshots the queue (callers only remove the node they are on).

**D20. `im` crate.** Persistent maps for copy-on-write (`im = 15.1.0`, on the plan's dependency list).

**D21. Engine port shape.** `replay_core/src/engine.rs` ports `engine_py/matching.py` function by function on
copy-on-write books; every engine map (`by_user`, `by_cloid`, priority metadata, schedules, stats) is an `im`
persistent map, so a whole-engine copy is cheap too. `replay/rust_engine.py` keeps in Python only: the dispatch
`if`-chain of `Engine.apply`, building Fill/Reject objects, the `check_every_action` option and the int-oid
resolver call (the resolver is a Python object; Rust never calls back into Python). `by_user` is exposed as a live
ordered view with exactly the operations the replay and resolver use (`get`, `setdefault(...)[oid] = None`, `in`,
iteration in insertion order).

**D22. Node views are snapshots.** A Python `Node` is one shared object, so a reduce made through one reference is
visible through every other. `RustBook` node views carry the remaining size read when they were made. The only
place this can differ is `Engine._outside_fill` when the named maker is literally `"placeholder"` (each placeholder
would be listed twice; Python then fails with a 0-lot Fill, Rust with "not resting"). Outside fills always name a
`0x` address, so this input does not occur; the differential test excludes it.

**D24. Random cross-engine tests use Hypothesis, not proptest.** The plan asks for proptest-generated random blocks
run through both engines. The reference engine is Python, so the cross-engine comparison runs on the Python side with
Hypothesis (`tests/test_rust_engine.py`, `tests/test_rust_book.py`, `tests/test_replay_core.py`); proptest checks the
Rust-internal properties (book invariants and copies, subset search vs brute force).

**D23. Parity runner switches.** `python -m replay.parity` takes `--stp/--order/--correction` (default = the
baseline switches); `make checkpoint` runs full dev20 parity under both `correction` settings (block-end vs
`report/validate/m0_blockend20`, mid-block vs `report/validate/m0_base20`), so Q1's answer does not change coverage.

## After the M5 checkpoint (Nov 22, Satya's answers)

**D25. Default backend is Rust (Q3).** `EngineAdapter()` and `python -m replay.parity` now default to
`backend="rust"`; `"py"` stays the reference that every Rust result is checked against. There was no CI in the repo:
`.github/workflows/check.yml` runs the same `make check` on GitHub (Rust build, cargo test + clippy, all pytest
incl. the Rust-vs-Python differential tests; dev20-data tests skip there). Untested on GitHub until it is pushed.

**D26. Problem page source.** `docs/problem_page.md` was not in the repo on Nov 22, so it was transcribed from the
"Replay the Order Book" artifact (version of Nov 22) with P4 case 2 corrected to `after = (6.5, 4)`. The Examples
are data in `tests/chain_examples.py` (what the chain does), shared with M1's rules engine. Through the replay engine:
Example 1 passes (docs priority), Example 6 passes with `stp=maker` and is a strict xfail under the default
`stp=taker`, Examples 2, 5, 7 are strict xfails (hidden margin / position state the engine cannot see).
