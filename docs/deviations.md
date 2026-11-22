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

**D2. `new_book(snapshot)` -> `new_book(window, hl_data, policy_name, options)`.** The engine is window-driven: it
seeds from the recorder snapshot nearest the window start, re-seeds at later snapshots (rule 8), gates ALOs on the
real touch and resolves cancels from window files. A `BookHandle` therefore wraps a whole `Replay`; `apply_block`
advances it one block via `Replay.step_block`. `book_from_replay(rp)` wraps a Replay built elsewhere (the scoring path).

**D3. `level_view` / `top_levels` take an `asset` and return integers.** The plan's signatures have no asset (one
handle holds BTC and ETH) and return float sizes. Project rule (AGENT_PLAN §2.1): no floats for prices or sizes. They
return `(lots, n)` and `(px ticks, lots, n)`; convert with `ASSETS[a].lots_to_sz / ticks_to_px` at the edge.

**D4. Rule 1 (no look-ahead) vs the baseline. Needs a decision.** The baseline's trade corrections
(`--trade-learn sweep,queue`, CLI default `--trade-learn-at tx`) read each tx's REAL trades right after that tx,
in the middle of its block, so later txs of the same block see a book corrected by printed trades.
`tests/test_lookahead.py` shows it: `--trade-learn-at block-end` passes the rule-1 guard, the baseline fails it
(marked `xfail(strict)`). Options: (a) keep the baseline as is (58.8%) and accept the deviation for validation
only; (b) make `block-end` the baseline (counterfactual-clean). Earlier dev20 cost of (b) on the frozen config
(Nov 21, `report/validate/tlcost20`): contested 0.0 pts (0 of 115 txs changed), uncontested -1.3 [-4.9, +1.0],
all-scored -0.8 [-3.0, +0.6]; not yet measured with `--p-priority docs`.

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
hook (`scripts/git-hooks/pre-commit`) is NOT installed automatically: `git config core.hooksPath` is shared by
every worktree of this repo, several of which other sessions commit from. Enable with
`git config core.hooksPath scripts/git-hooks` when wanted.

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
