# Order-book replay: four extensions ("Mahito" plan)

Implementation spec for Claude Code. It adds four components to the Hyperliquid
order-book replay ("Replay the Order Book"): a simulator, a particle filter,
trader profiles and a testnet probe lab.

---

## 0. How to work through this document

- **The plan now runs in two tracks, and §00 overrides the milestone order below.** Every work session pairs a **lab** step with an **accuracy** step and ends with `docs/scoreboard.md` updated. Done so far: M0 · Idle, M5 · Isomer, M1 · Transfigure (all tagged).
- At the end of each milestone, stop and report: what you built, the test results, and anything in this spec that turned out wrong for the real code. Wait for review before the next milestone.
- If the real engine contradicts this spec, the real engine wins. Write the difference down in `docs/deviations.md`, don't force the spec onto it.
- Ask before changing anything in the scoring code, the answer key, or the chunk sets (dev20, 68-chunk confirm, 240-chunk final).
- Python, matching the existing engine. Use type hints and dataclasses, and `pytest` for tests.

## 0b. Git workflow

The user is working on a feature branch. Rules:
- **Stay on the current branch.** Never commit to, push to, rebase or force-push `main`. Merging into `main` is the user's job. If you need a sub-branch, branch off the current one and say so.
- **One commit per finished step** (a component plus its tests), with clear messages: `m5: port match() to rust, parity on dev20`.
- **Tag every milestone checkpoint** once the user approves it: `m0-idle`, `m5-isomer`, `m1-transfigure`, `m2-multiplicity`, `m3-polymorphic`, `m4-self-embodiment`. Each one is a point to go back to.
- **Before every commit**, all of these must pass, as a pre-commit hook or a `make check` target:
  - the look-ahead test (rule 1)
  - Rust vs Python identical fills on the fast test set (a few dev20 chunks)
  - `pytest` for the touched modules
  - `cargo test` and `cargo clippy -D warnings` if Rust changed

  The full dev20 parity check runs at each milestone checkpoint.
- **Never commit:**
  - raw or simulated data, truth logs, recorder output, model files over 1 MB
  - `.env` files or keys: `HL_TESTNET_KEY_*` and anything like a private key

  Add these to `.gitignore` in M0. Add a check that refuses a commit containing something that looks like a hex private key.
- **Keep this file current:** record in `docs/deviations.md` any change to this spec that the real code forces, and commit it together with the change.

---

## 00. Two-track plan (v2, Nov 22): improve the lab AND the score

**Why this changed:** M0, M5 and M1 made the lab faster and more trustworthy, but contested strict on dev20 is still about 0.587, the same as before the plan. The big gaps (placeholders, margin-rejected IOCs, lost orders) are **missing-information** problems. From now on, every lab step must make an accuracy step possible, and every session must try to move the score.

### Current baseline (keep this definition exact)
Real engine on `mahito`, Rust backend, block-end correction, `--p-priority docs`, `--learn-evidence tx`, `order=proposer` (explorer listing order), `stp=taker`. dev20 contested strict ≈ 0.587. The old 0.588 on final240 used mid-block correction and is kept for the record only. Every change is compared with **this** baseline, and `docs/scoreboard.md` records each time the baseline moves.

### Session rules
1. **Pairing.** Each session does one or more **A** (accuracy) items **and** one or more **L** (lab) items. Do not spend a whole session on lab work unless every open A item is blocked; if so, say which A item it unblocks.
2. **Write the decision rule down before the run.** Every A item writes its adoption rule into `docs/scoreboard.md` before looking at results. The default rule: adopt if dev20 contested strict improves with a paired 95% CI lower bound ≥ 0 **and** uncontested doesn't drop by more than 1 point (CI upper bound). On dev-fast (A1), the same rule applies.
3. **Each fix goes behind a switch**, default off, then becomes the baseline only if it passes its rule. Update the Rust/Python parity checks for the new baseline.
4. **The scoreboard** (`docs/scoreboard.md`) holds one row per attempt: date, item, switch, rule, dev20 contested / uncontested with CIs, dev-fast too if run, adopted (y/n), commit. Failed attempts stay in the table. They're results too.
5. **Hold-out sets:** confirm68 runs only with the user's OK. final240 runs only once per semester-level claim, with the user's OK.
6. **Budget:** each A item should take at most one session to get a first measured result. If it doesn't, write what's blocking it in `docs/QUESTIONS.md` and move on.

### Accuracy track (A), in priority order
**Order since Nov 23 (Satya, from A0 ranked by miss count): A8 > A4 > A5 > A3 > A6 > A7; A2 deferred.**
- **A0 · Re-triage (do first).** Classify every contested miss on the current baseline by cause: placeholder maker, margin-rejected IOC or ALO we filled, lost order / wrong cancel target, queue order at the right price, phantom order (we hold, chain doesn't), ordering inside a block, other. Report counts, share of missed notional, and 3 examples per cause. This replaces the old 135-miss breakdown and **sets the order of A2–A7**: work on the biggest causes first.
- **A1 · dev-fast set.** Build a new development set (~20 chunks) from windows recorded after Nov 21 22:56 UTC, so it has the 5-level `l2Book fast` feed and no ingest gaps. Add a loader for that feed. Score the baseline on it. From then on, report dev-fast next to dev20 for every A item. (dev20 still decides adoption, until the user says otherwise.)
- **A2 · Lead-in length.** Try 300 and 600 blocks (150 beat 75 by +2.6). This is only a config change. Watch the runtime cost. **Deferred (Nov 23):** chunks hold only their captured 150-block lead-in; longer ones need a capture change (QUESTIONS Q10), not made for now.
- **A3 · IOC margin classifier (P2).** The first piece of M3 · Polymorphic, trained on **real** labels. Train `ioc_reject` on the de-duplicated order histories (time-based split, calibrated, leakage test, as in M3). In replay: if the predicted rejection probability is ≥ a cutoff chosen on training days, skip the IOC. This replaces the `learn` rule's first-IOC leak. Report engine-only trades too (213 before).
- **A4 · Placeholder owners (lightweight P1).**
  - (a) Split each snapshot level into its `n` orders using the feed's count changes, as P1's logic does, but keep only the single most likely queue (no particle filter).
  - (b) Name owners from later trades and cancels.
  - (c) Break ties with each account's habitual order sizes.
  - (d) On dev-fast, extend the touch ledger to all 5 levels with sizes and counts.
- **A5 · Exact oid counting (P3).** If L1 confirms that the listing is execution order, oids may be countable exactly between anchors (P3's follow-up question). Test whether every order item takes the next oid (rejected orders, triggers, both coins) and use counting in place of interpolation where it holds.
- **A6 · Margin-rejected post-only orders (P4).** Train the `alo_reject` classifier, the same way as A3. On dev-fast, use deeper levels to remove post-only orders that never rested.
- **A7 · Silent reduce-only cancels (P6).** Small. Only if A0 shows it's worth any points.
- **A8 · Queue order among known orders.** (Added Nov 23; A0's largest cause by count: 26 of 59 contested misses.) The real maker rests at the right price in our book but our queue fills others first. Split the misses into sub-causes (modify semantics, where post-snapshot named orders sit vs placeholders, the 400 ms / priority window, placement order within a block, partial fills), check each against the chain's real fills, and put a fix for each supported one behind a switch.

### Lab track (L), each item tied to what it unblocks
- **L1 · Is the listing execution order?** (Unblocks A5 and the simulator's truth ordering; it's also IE 498's key premise.) Count runs of the class sequence per block vs a shuffled null, with the decision rule written down first. Check whether the 6 conflicting blocks' run boundaries fall inside their one-cut ranges. Then set the simulator truth to per-batch class sort with a measured batch rate, or to proposer, by the rule.
- **L2 · Simulator difficulty.** (Unblocks M2 tuning.) Add maker concentration and baseline contested strict as calibration targets, and calibrate toward dev20 and dev-fast. Stop after 3 rounds and report.
- **L3 · Arrival-order source.** (Unblocks the IE 498 counterfactual.) List any captured data with per-tx arrival times or mempool data. Don't build anything.
- **L4 · Minimal M4 ordering probe.** (Unblocks IE 498 and confirms L1 directly.) The testnet experiment: a cancel and an IOC in the same block, in both listing orders. Only with the user's testnet keys and OK.
- **L5 · M2 · Multiplicity, gated.** Build M2 only if A0 shows that misses needing several hypotheses at once (ambiguous queue splits, ambiguous cancel targets) are **≥ 30% of missed contested notional** (Nov 23: count only misses caused by real ambiguity, not queue-order misses among known orders) after A2–A4 have landed. Otherwise M2 waits until after the semester.

### What happened to the original milestones
- M2 → **L5**, gated on A0.
- M3 → split. Its classifiers become **A3 and A6** (now). The full profile store comes later, only if A4(c) shows size habits help.
- M4 → **L4**, minimal probe only, for IE 498.
- Hard rules (§2) and budgets (§2b) still apply to everything.

---

## 1. Context

**The problem.** We have a snapshot of the order book (per price level: total size and order count only) and the blocks of transactions after it. We replay the blocks through a matching engine and output each trade's fills `(maker, px, sz)`. Scoring is **strict recall**: a fill counts only if the maker is right too. The headline number is the average over *contested* trades, where more than one maker could have filled.

**Current state.** Contested strict is about 51–53% on the 240-chunk final set (BTC 48%, ETH 58%), and all-trades strict is 56.5%. There is a 150-block lead-in that is replayed but not scored.

**Chain rules the engine must follow:**
- Inside a block, txs run in three classes: actions with no GTC/IOC order first, then cancels, then GTC/IOC orders. Within a class they keep proposer order. **This applies per consensus batch.** Explorer blocks may merge several batches without marking the boundaries.
- A post-only (ALO) order that would cross is rejected (`badAloPxRejected`).
- At one price, post-only orders placed in the last 400 ms are sorted by priority bid `p`. Older orders keep their place.
- On a self-trade, the chain cancels the **resting** order and keeps matching.
- The chain also checks margin, a $10 minimum, open-interest caps and the oracle band. None of these show up in the blocks.

**Known failure modes**, ordered by cost:
1. **Queue priority:** a higher bid jumps an order older than 400 ms. Caused 89 of the last 135 misses. A `--p-priority docs` fix is being tested.
2. **IOC margin rejections:** IOCs from accounts with no free margin. The `learn` rule cut engine-only trades from 1,383 to 213, but it still lets each bot's first IOC through.
3. **Snapshot placeholders:** orders from the snapshot with no known owner. Caused 74% of engine-only trades in the Nov 18 triage.
4. **Lost orders:** cancels by oid that get matched to the wrong order. Part of 46 lost real orders, spread over 7 causes.
5. **Margin-rejected post-only orders:** 1.5% of maker orders.
6. Self-trade handling.
7. Silent reduce-only cancels.
8. Merged batches inside one explorer block.

**The six hidden-information puzzles** (P1–P6) map to failures 3, 2, 4, 5, 8 and 7 in that order. Toy solutions are in `puzzles.py` (attached separately). Use them as unit-test oracles for the small cases. **They don't scale:** P1 lists every way to split a level and P5 tries every set of cut points, so neither works on real-size inputs.

**Known bug in the puzzle page:** P4 case 2 prints `after = (7.5, 4)`, but its answer only works for `(6.5, 4)`. Use `(6.5, 4)` in tests.

---

## 2. Hard rules (invariants — never break these)

1. **No look-ahead.** A block's evidence (its feed updates and printed trades) may update state only **after** that block's fills are recorded. Write a test that fails if anything reads `real_trades[h]` before `out[h]` is set.
2. **Profiles are walk-forward.** For each chunk, profiles are built only from data timestamped before that chunk starts.
3. **N=1 reproduces the baseline.** With one particle and branching turned off, the particle filter must give **byte-identical** fills to the current engine on dev20. This is the regression test for M2.
4. **The probe lab never touches mainnet by default.** The sender refuses to start unless the endpoint is the testnet URL. Mainnet needs an explicit `--network mainnet --i-understand` flag plus a config file the user writes by hand. Never hard-code keys: read them from environment variables, and never log or commit them.
5. **Don't change the scoring.** All new metrics are added next to the existing ones.

---

## 2b. Budget rules (all milestones)

Every milestone has a compute, memory, disk or money budget. The numbers below are **starting defaults to measure against, not targets**. Put each one in config, measure it, and report it. Every milestone report ends with a **budget table**: each limit, its default, the measured value, and whether it was hit. If a limit gets in the way of accuracy, say so and propose a new value. Don't quietly raise it.

### Measure first
Before M0's done, record the baseline's runtime per chunk, peak memory and disk use on dev20 in `docs/baseline_costs.md`. Every multiplier below is relative to these numbers.

### M0 · Idle
- Adapter overhead: **≤ 1.2×** the baseline's runtime, and no extra book copies when no branching is used.

### M1 · Transfigure
- Generating one simulated chunk: **≤ the baseline's replay time for one chunk.**
- Truth logs: compressed parquet. Default disk cap **5 GB** for all simulated data. Reuse seeds instead of storing duplicate chunks.
- Default suite of **200 simulated chunks**. Add more only when a confidence interval is too wide to decide something, and say so in the report.

### M2 · Multiplicity (the main cost risk)
These layers are mandatory, applied in this order:
1. **Merge guesses that lead to the same fills.** Key each guess by a hash of the state a taker could hit: owners and sizes of the front orders at every level within reach. Same key → merge them and add their weights. Example: P1's 12 queues become 4 (0xM at the front with 2.0, 2.5, 3.0 or 3.5), and stay 1 until a taker buys more than 2.0.
2. **Keep uncertainty with its price level.** Store "level X has k options" and "cancel Y has k options" separately. Combine them only when one tx touches both. Never multiply out every combination into full books.
3. **Branch only when needed.** Split only when a tx reaches the uncertain state and the choice changes its fills. Until then, the uncertainty is a note on the level.
4. **Share everything that's the same.**
   - Books are copy-on-write: a guess copies only the levels it changes.
   - Within a block, match each tx once per distinct state at the levels it touches, then apply that result to every guess in the group.
5. **Hard limits** (in config):
   - default max guesses `N_max = 64`
   - when over the cap, keep the top `N_max` by weight
   - drop guesses whose weight is below 1e-4 of the total
   - resample when the effective sample size falls below `N_max/2`
   - cap new branches at 16 per block
   - an uncertainty **expires** once its level is used up, or once the feed agrees with one option for 3 updates in a row; then collapse it to that option

Limits:
- Runtime **≤ 10× baseline** at the default settings on dev20.
- Peak memory **≤ 4× baseline**.
- Log every block: guesses held, distinct states after merging, time spent matching, peak memory. Report the median and 99th percentile.

Warning signs to report:
- the cap is hit in more than 5% of blocks: the merge key is probably too strict, or branching happens too early
- distinct states stay at or above `N_max/2`

### M3 · Polymorphic
- Per account: top-k sizes with **k = 8**, resting times and reaction times as fixed-size quantile sketches (no raw lists), counters for everything else. Aim for **≤ 2 KB per account**.
- Accounts with fewer than 50 orders get no profile of their own; they use their account type's profile.
- Rebuilding profiles for one chunk: **≤ 0.5×** the baseline's replay time for that chunk. Each online update during replay costs the same no matter how much history exists.

### M4 · Self-Embodiment
- Money (on testnet too, so the same code is safe later):
  - **≤ $15** notional per order (the $10 minimum plus room for price moves)
  - **≤ $500** open notional across all accounts
  - **≤ $2,000** total notional per run
  - the run stops itself when any limit is hit
- Rate:
  - **≤ 20 orders per minute** per account, and never above the exchange's published API rate limits (look them up and write them into config)
  - back off and retry when rate-limited; never retry a request that may already have gone through
- Recorder:
  - feed window of ±5 s per order
  - default disk cap **2 GB** per experiment
- Mainnet (only once the user approves, per rule 4): the user sets the budgets by hand. Nothing carries over from the testnet config.

### Evaluation
- Simulator and dev20 runs are free to do.
- The 68-chunk and 240-chunk runs each need user approval. Before asking, give an estimated runtime based on the dev20 numbers.
- Search over settings: **≤ 20 configurations per milestone** on dev20. Choose settings on dev20 and confirm them only on the held-out sets, so the confirm sets aren't overfit.

---

## 3. Overall architecture

```
                 ┌─────────────┐
                 │ Chain data  │  blocks, bbo/l2 feed, public trades, account snapshots
                 └──────┬──────┘
                        ▼
 ┌──────────────┐  ┌───────────────────────┐  ┌──────────────┐
 │ Probe lab #1 │─▶│  Particle filter #2   │◀─│ Profiles #3  │
 │ (anchors,    │  │  N candidate books    │  │ (branch odds,│
 │  rule config)│  └──────────┬────────────┘  │  likelihoods)│
 └──────────────┘             ▼               └──────────────┘
 ┌──────────────┐  ┌───────────────────────┐
 │ Simulator #4 │─▶│ Fills + confidence    │  (simulator = test bench for everything)
 └──────────────┘  └───────────────────────┘
```

Directory layout. Adjust it to the repo's conventions and record any change in `docs/deviations.md`:

```
replay/
  adapter.py          # M0: thin wrapper over the existing engine (backend="py" | "rust")
  sim/                # M1
    sampler.py  rules_engine.py  mask.py  truth.py  calibrate.py
  pf/                 # M2
    particle.py  branch.py  likelihood.py  filter.py   # copy-on-write books and state hashing live in replay_core
  profiles/           # M3
    features.py  store.py  priors.py
  probe/              # M4
    spec.py  sender.py  recorder.py  analyze/{anchors,queue,rules}.py  specs/*.yaml
  eval/
    harness.py  diagnostics.py
replay_core/          # M5: Rust crate (PyO3 + maturin)
  Cargo.toml  src/{book,matching,rules,block,hash}.rs  benches/
pyproject.toml
tests/
docs/deviations.md  docs/rust_walkthrough.md
```

---

## M0 · Idle — Discovery and adapter (do this first, no new features)

The real engine's location and structure are unknown. The reference sketch has `Order`, `Book` (`levels`, `by_oid`, `best`, `rest`, `remove`), `match`, `place`, `reconcile`, `tx_class` and `Solution.replayBook`, plus a loop that runs once per block.

1. Find the real equivalents of each piece. Write `docs/engine_map.md`: piece → file/function → notes.
2. Find the data loaders: snapshot, blocks, bbo / `l2Book fast` feed, public trades, account snapshots, `orderStatus` anchors, and the 1.73M order histories.
3. Find the scoring code and the chunk lists (dev20, 68-chunk confirm, 240-chunk final).
4. Write `replay/adapter.py` exposing:
   ```python
   class EngineAdapter:
       def new_book(self, snapshot) -> BookHandle: ...
       def copy_book(self, book: BookHandle) -> BookHandle: ...
       def apply_block(self, book, block, ctx: BranchContext) -> dict[TxHash, list[Fill]]: ...
       def level_view(self, book, side, px) -> tuple[float, int]: ...   # (sz, n)
       def top_levels(self, book, side, k=5) -> list[tuple[float, float, int]]: ...
   ```
   `BranchContext` carries the choices a particle has made: which order a cancel targets, which orders get rejected, where batch cuts go, and the queue-priority mode. With an empty context, the engine runs exactly as it does today.
5. Run dev20 through the adapter and confirm the score is unchanged.

**Done when:** `engine_map.md` exists, `adapter.py` has tests, and dev20's score through the adapter equals the baseline exactly.

---

## M5 · Isomer — Rust core for the hot path (PyO3)

**Purpose:** build the hot path in Rust from early on: matching now, particle bookkeeping later. It's the same engine in a different language: the Python version stays as the reference the Rust version is checked against.

**When:** right after M0 · Idle, before M1. Port the **existing** engine first: book, matching, chain rules and `apply_block`, with no new features. Check Rust against Python until fills are identical. Then every later milestone builds its hot-path code on the Rust core:
- **M2:** copy-on-write levels, state hashing for merging guesses, and running each tx once per distinct state are written in Rust from the start. The particle weighing and resampling logic stays in Python unless profiling says otherwise. Rule 3 (N=1 gives identical fills) is checked against the **Python** engine.
- **M1:** the simulator's rules engine stays in **Python** on purpose. It's an independent second implementation, so it can catch bugs in the Rust core.

Profile while porting (`py-spy` or `cProfile` on the Python side, `cargo bench` / `criterion` on the Rust side).

### What moves to Rust, and what stays in Python
| Rust crate `replay_core` | Python stays |
|---|---|
| `Order`, `Level`, `Book` with copy-on-write levels (`Arc<Level>`, or the `im` crate's persistent maps) | data loaders, chunk lists, scoring |
| `tx_class` sort, `place`, `match`, cancel/modify, self-trade rule, 400 ms priority | profiles, feature extraction (Polars), classifiers |
| `apply_block(book, block, ctx)`, run once per distinct state | particle weighing and resampling logic (move it too if profiling says so) |
| state hashing for merging guesses that lead to the same fills (M2 layer 1) | probe lab (M4) entirely |
| — | the simulator's `rules_engine` (M1), kept in Python as an independent check |
| — | evaluation, reports, plots |

### Interface rules
- Build with **PyO3 + maturin**, and add a `pyproject.toml` so `pip install -e .` builds the crate.
- **Cross between Python and Rust once per block or chunk, not per tx.** Pass a whole block's txs as one array (or Arrow/NumPy buffers) and get all its fills back in one call. Crossing per tx would cost more than Rust saves.
- **Use whole numbers inside Rust:** prices as integer ticks, sizes as integer lots. Convert at the boundary with exact decimal parsing, and do the reverse conversion the same way so results come back identical.
- Release the GIL inside `apply_block`. Use `rayon` to run independent distinct states, and separate chunks, in parallel.
- The Python adapter (M0) picks the backend: `EngineAdapter(backend="py" | "rust")`. Everything above the adapter stays the same.

### Correctness: check Rust against Python
- **Byte-identical fills on dev20**, Rust vs Python, before M1 starts. Once M1 exists, also on the 200 simulated chunks. Once M2 exists, also with branching on (N=1). Any difference fails the build.
- `proptest`-generated random blocks run through both engines, with the results compared, as part of CI.
- Unit tests built from the problem page's Examples 1, 2, 5, 6 and 7 (shared with M1's rules-engine tests later).
- `cargo clippy -D warnings`, and no `unsafe` without a comment explaining it.

### Budget
- Report runtime and peak memory per chunk on dev20 for Python vs Rust. Once M2 exists, repeat at `N_max` in {1, 64, 256}. **No assumed speedup:** measure it. If Rust isn't faster by enough to matter (less than 3× on the hot path), stop and report instead of porting more.

### For the user's learning
Keep the crate small and readable: comment each module with the chain rule it implements, and write `docs/rust_walkthrough.md` explaining how the crate is laid out, how PyO3 connects it to Python, and how to run the tests.

**Done when:** the Rust and Python engines give identical fills on dev20 and on the property tests, the budget table with Python-vs-Rust timings exists, and the adapter's default backend is switched to Rust only after the user signs off.

---

## M1 · Transfigure — Simulator (#4)

**Purpose:** generate chains with known hidden state, hide them the way the real data is hidden, and measure how well inference recovers them. It also measures things the real data can't, like the cost of merged batches (P5).

```
Hidden-state sampler ─┐
                      ├─▶ Synthetic chain (truth log) ─▶ Observation mask ─▶ Replay/filter ─▶ Score vs truth
Chain rules engine ───┘                 └──────────────── truth ────────────────────────────────▲
```

### Components
- **`sampler.py`** draws, from configurable distributions:
  - snapshot queues: each level's size and count split into owned orders
  - accounts: types (market maker, bot, retail), size habits, cancel speed, margin state, leverage
  - order flow: Poisson arrivals per account type, prices around the touch, tif mix
  - batch cuts inside blocks, at a configurable rate
  - margin rejections: post-only and IOC, at configurable rates

  Accept profile objects from M3 when they exist; until then, use hand-set defaults.
- **`rules_engine.py`** is an independent implementation of the §1 rules:
  - class sort per batch
  - post-only orders that would cross are rejected
  - the 400 ms priority window
  - margin checks
  - self-trade cancels the resting order
  - reduce-only orders canceled when they can't reduce
  - $10 minimum

  Write it from the rules, not by copying the replay engine, so the two can catch each other's bugs.
- **`truth.py`** records every hidden fact: each order's oid, owner, queue position, accepted or rejected with the reason, batch membership, silent cancels, and every fill.
- **`mask.py`** produces exactly what the real replay sees:
  - explorer blocks with batches merged
  - a snapshot that has only `(px, sz, n)` per level
  - `l2Book` 5-level feed updates every ~0.54 s
  - bbo per block
  - public trades with maker addresses
  - placement oids hidden except about 50 `orderStatus` anchors per chunk
  - account snapshots for 40 accounts, taken about 7 s after the chunk starts

  Every mask parameter lives in a config.
- **`calibrate.py`** compares simulated statistics with real ones:
  - how much level size and count change between feed updates
  - trades per block
  - margin rejection rate for post-only orders (real: 1.5%)
  - `tooManyOpenOrdersRejected` rate (real: 0.14%)
  - engine-only trade rate

  It outputs a table of real vs simulated values with the gap for each.

### Tests
- Rules-engine unit tests built from the problem page's Examples 1, 2, 5, 6 and 7. Each asserts what the chain does.
- Puzzle tests: build each of P1–P6's worked examples in the simulator, mask it, and check that the visible output equals what the puzzle says you see.
- Property test: with the mask set to show everything and no randomness, the existing replay engine should reach about 100% strict on simulated chains. Anything less means a disagreement between the two engines. Write each one down.

**Done when:** simulated chunks generate from a seed, every test passes, a calibration report exists, and the current engine has a strict-recall baseline measured on simulated chunks.

---

## M2 · Multiplicity — Particle filter (#2)

**Purpose:** stop committing to one guess. Keep N copies of the book, each a different set of guesses, and let the evidence remove the wrong ones.

```
Profiles ─▶ Branch ─▶ Replay block (every particle) ─▶ Record fills (weighted vote)
                                                              │
               Block evidence (feed, printed trades) ─▶ Weigh particles ─▶ Prune/resample ─▶ next block
```

### Data model
```python
@dataclass
class Particle:
    book: BookHandle            # copy-on-write; see cow_book.py
    ctx: BranchContext          # the guesses this particle has made so far
    blocked: set[str]           # per-particle learn-rule state
    margin: dict[str, float]    # free-margin estimate per account (if tracked)
    log_w: float
```

### Branch points (`branch.py`)
Each point has a hook. Each hook returns a list of `(choice, prior_prob)`.

| Hook | Puzzle | What it branches on |
|---|---|---|
| `split_snapshot_level` | P1 | how a level's size divides into its `n` orders, plus owner guesses. **Sample a few splits**, never list them all. Weight toward sizes that owners with a matching profile reuse. |
| `cancel_target` | P3 | which resting order a cancel means. Rule out orders outside the oid bracket, then rank by interpolated oid. |
| `alo_rejected` | P4, Ex 5 | whether a post-only placement was margin-rejected. |
| `ioc_rejected` | P2, Ex 2 | whether an IOC was margin-rejected. The prior comes from M3. Replaces the yes/no `learn` rule inside the filter. |
| `batch_cuts` | P5, Ex 8 | where batch cuts fall inside a block. The default prior is no cuts. Only branch when a cut would change who fills. |
| `silent_cancel` | P6, Ex 7 | whether a reduce-only order disappears. |
| `priority_mode` | Ex 1 | docs mode vs the current "known" mode. Global per particle at first. |

Control: follow the M2 budget rules in §2b. Merge guesses that lead to the same fills, keep uncertainty with its price level, branch only when needed, share identical state, and apply the hard limits.

### Likelihood (`likelihood.py`)
Score each particle **after** its fills are recorded (rule 1). The log-likelihood adds up:
- feed agreement at each level shown: an error model on the size gap and the count gap (start with a discrete distribution, tune it on the simulator)
- printed trades: each real trade the particle matched at the same price and size scores +; each real trade it missed and each trade only it produced score −
- hard contradictions get a floor of `log(ε)`, not −∞, so one bad data point can't wipe out every particle

### Output
For each tx: pick the maker with the most total weight across particles, and report `confidence = winning weight / total weight`. Strict scoring stays unchanged. Add diagnostics: strict recall in confidence buckets, and how often the true maker was among the particles at all.

### Resampling
Use systematic resampling when the effective sample size drops below N/2. After resampling, give the copies a small amount of extra variety: re-sample one branch choice from one recent block.

### Performance
- Implement all five layers of the M2 budget rules in §2b. Copy-on-write books, state hashing and run-each-tx-once matching live in the Rust core (`replay_core`). `filter.py` handles the grouping and calls it once per block.
- Report runtime, memory and distinct-state counts for `N_max` in {1, 16, 64, 256} on dev20. Also report each of layers 1–4 switched off one at a time, so its savings are measured. Don't pick `N_max` yet. Report the accuracy/runtime curve.

### Tests
- **Rule 3 regression:** with N=1 and no branching, fills on dev20 are byte-identical to the baseline.
- **Look-ahead guard test** (rule 1).
- **Simulator recovery:** on simulated chunks, for each puzzle type, measure how often the true hidden choice survives in a particle with meaningful weight.
- **P1 oracle:** for the worked example, the set of surviving queues should be a subset of the 12 that `puzzles.py` returns.

**Done when:** all tests pass. Report contested-strict and all-strict versus the baseline on simulated chunks and on dev20, with paired bootstrap confidence intervals, plus the N curve. Don't run the 68-chunk or 240-chunk sets until the user approves.

---

## M3 · Polymorphic — Trader profiles (#3)

**Purpose:** give each account a profile of its habits, and turn the profiles into branch odds and likelihood terms.

```
Order histories (before chunk) ─▶ Feature extractor ─▶ Profile store (per account)
                                                        ├─▶ P1 owner/size odds
                                                        ├─▶ P2/P4 margin-reject odds
                                                        └─▶ particle proposals (#2)
                         ↻ updated online as blocks replay
```

### Features (`features.py`), per account
- **Size habits:** the top-k order sizes and their frequencies (smoothed)
- **Resting time:** how long orders rest before cancel, as quantiles
- **Order mix:** the split between ALO, GTC and IOC, and how far from the touch it quotes, in ticks
- **Rejection history:** counts of margin rejections and the time since the last one
- **Reaction time:** how fast it acts after the bbo changes
- **Last margin snapshot:** `accountValue`, `totalMarginUsed` and positions, if one exists
- **Activity level and account type**, from a simple rule-based classifier: market maker, bot or retail

### Store (`store.py`)
- `build(as_of_ts)` uses only data with `ts < as_of_ts` (rule 2).
- `update(event)` updates the profile online during replay, only with events from blocks whose fills are already recorded.
- Accounts with little history fall back to their account type's profile, then to a global one.

### Priors (`priors.py`) — the interface M2 calls
```python
def owner_size_prob(user, sz) -> float            # P1 split/owner odds
def margin_reject_prob(user, order, ctx) -> float # P2 / P4
def cancel_target_prob(user, candidate) -> float  # P3 tie-break
```
Margin rule for `margin_reject_prob` when a snapshot exists:
- only the part of the order that opens or grows the position needs margin
- needed margin = that size × px ÷ leverage
- leverage: use the account's known setting; if unknown, max leverage (BTC 40, ETH 25)
- turn the gap between free margin and needed margin into a probability with a logistic function tuned on the simulator, then on real data

Without a snapshot, use the rejection-history features.

### Learned classifiers (`profiles/models/`)
The functions in `priors.py` start as the hand-written rules above (Level 1). Each one is then replaced by a small classifier trained on **real** labels from the 1.73M order histories, whose final statuses give the labels for free. Keep the hand-written rule as a fallback and as the baseline to beat.

| Model | Predicts | Label source | Features (all computed as of the order's time) |
|---|---|---|---|
| `ioc_reject` (P2) | was this IOC rejected for margin | `perpMarginRejected` vs filled/`iocCancelRejected` | gap between free margin and needed margin (if a snapshot exists), how old that snapshot is, recent rejection history, account type, order notional, whether it grows the position, how far the price moved since the snapshot |
| `alo_reject` (P4, Ex 5) | was this post-only order rejected for margin | `perpMarginRejected` / `tooManyOpenOrdersRejected` vs resting | same as above, plus open order count and the account's recent post-only rate |
| `cancel_target` (P3) | which candidate order a cancel targets | cancels whose oid resolves through anchors or `orderStatus` | gap between interpolated oid and target, distance to the nearest anchor, price and size match, how old the order is, the account's cancel habits |
| `owner_size` (P1) | who owns a hidden order of size s | snapshot orders later named by trades or cancels | the account's size habits, how much it trades, which levels it quotes, account type |

Rules:
- **Start with logistic regression, then gradient-boosted trees** (LightGBM). No neural networks at this stage.
- **Split by time, never at random.** Train on earlier days, validate on later ones, and keep the evaluation chunks out of training entirely (rules 1 and 2).
- **Calibrate the probabilities** (isotonic or Platt) and report reliability plots. The particle filter uses these numbers as probabilities, so they have to mean what they say.
- Each model gets a size and latency budget: **≤ 50 µs per prediction**, batched where possible, and **≤ 50 MB** on disk.
- Report precision and recall at several cutoffs against the hand-written rule, with confidence intervals. Switch to a model only if it beats the rule on the validation days.

### Tests
- Look-ahead test: building profiles as of time T gives the same result whether or not data after T exists.
- Leakage test for the classifiers: no feature reads data with a timestamp after the order's own.
- Tune `margin_reject_prob` on real data using the 4,022 rejected bot IOCs and the 575 real fills. Report precision and recall at several cutoffs. **Compare with the rough rule's 1,298 of 1,383 caught and 115 of 575 real fills wrongly rejected.**

**Done when:** M2 runs with profile-based priors. Report results with and without profiles on the simulator and dev20, with confidence intervals.

---

## M4 · Self-Embodiment — Probe lab (#1) — TESTNET FIRST

**Purpose:** run small experiments on the chain itself, to confirm rules and get exact oid anchors and queue measurements.

```
Experiment spec ─▶ Guarded sender (testnet) ─▶ Recorder (oid, status, block, fills, feed)
                                                    ├─▶ Oid anchors (P3)
                                                    ├─▶ Queue depth (P1)
                                                    └─▶ Rule tests (Ex 1: 400 ms)
                                                              └─▶ replay config + anchors
```

### Components
- **`spec.py` + `specs/*.yaml`:** hypothesis, accounts used, order pattern, timing (`Δt` values), repetitions, and the result that would confirm or reject the hypothesis.
- **`sender.py`:**
  - use the official `hyperliquid-python-sdk` with its testnet API URL constant; check the current SDK and docs before coding
  - guardrails:
    - refuse to start unless the endpoint is testnet (rule 4)
    - a per-order notional cap
    - an orders-per-minute cap
    - a total notional budget per run
    - a kill switch: SIGINT cancels every open order, then exits
    - dry-run mode that only prints the orders
  - keys come from env vars `HL_TESTNET_KEY_{A,B,C}`, one per account
  - log the local send time with monotonic nanoseconds
- **`recorder.py`:** for every order, store the response (oid, status, reject reason), the block height and time it landed in, its fills, and the `l2Book` and bbo feed from 5 s before to 5 s after. Use an append-only parquet or SQLite store.

### First three experiments
1. **`rule_400ms.yaml`** (Example 1, the top priority):
   1. Account A rests an ALO ask at price X with `p=0`.
   2. Account B places an ALO ask at X with `p>0`, `Δt` later, for `Δt` in {100, 200, 300, 400, 500, 600, 800} ms.
   3. Account C sends an IOC buy for one order's size.
   4. Record whose ask fills.

   Use three accounts so the self-trade rule can't interfere. Run at least 20 repetitions per `Δt`. Analyze by the block time each order landed in, not the send time. Output the chance that B fills first at each `Δt`, with confidence intervals.
2. **`oid_count.yaml`** (P3 follow-up): place several orders in one block, across both coins, including some designed to be rejected. Test whether every order takes the next oid and whether rejected orders use one up.
3. **`queue_depth.yaml`** (P1): rest a small ALO behind a known queue and check that feed size ahead plus time to fill gives the queue ahead of it.

### Analyzers
- `anchors.py` writes probe oids in the format `orderStatus` anchors use, so the replay can use them directly.
- `rules.py` writes a rule-config file, e.g. `priority_window_ms`, `p_priority_mode`, `oid_per_item`, that the replay and the simulator's rules engine both read.

### Caveat (put it in every report)
Testnet results confirm **how the rules work**, not **how often things happen on mainnet**. Testnet has few traders and may have different validators and timing. Anchor and queue-depth probes only help scoring once they run on mainnet, which needs the user's explicit approval and a hand-written config (rule 4).

**Done when:** the sender passes dry-run and guardrail tests (including a test that it refuses a mainnet URL), and `rule_400ms` has run on testnet with a written result. Stop and ask before any mainnet run.

---

## Evaluation protocol (all milestones)

- **Simulator first, then dev20.** Run the 68-chunk confirm set and the 240-chunk final set only with the user's approval.
- **Always paired:** compare against the baseline on the same chunks. Report the mean difference with a 95% bootstrap CI, and use Holm correction when testing several variants, as earlier confirm runs did.
- **Report contested strict, all-trades strict, the BTC/ETH split, and engine-only trades.** Also report the new diagnostics: confidence buckets, whether the true maker survived among the particles, and per-puzzle recovery on the simulator.
- **No target numbers.** Report what you measure, with uncertainty.

## Later, only if triggered: neural network that learns the inverse (Level 3)

Not in scope unless the M2 budget report shows guesses getting cut by the caps often, or runtime hitting the 10× limit. If so, propose (don't build without approval):
- **The approach:** a network trained on simulated (hidden state, observations) pairs that outputs a probability over P1 queue splits. It would only **propose** branches for M2. The filter's weighing step still checks every proposal against the rules and the evidence.
- **Requirements:** the M1 calibration report must show the simulator matching real data first. A network trained on a simulator that doesn't match will be confidently wrong on real chunks.

## Open questions to flag, not guess

- Example 7 / P6: when a position shrinks below a reduce-only order's size but doesn't reach zero, does the chain cancel the order or shrink it? Check the 81 `reduceOnlyCanceled` histories. Until that's settled, the default is to leave the order as is.
- Example 8 / P5: how often do batch boundaries change fills? Measure the effect in the simulator, and count the real blocks where the class sort contradicts the printed trades.
- Does `l2Book fast` (5 levels, ~0.54 s, recorded since Nov 21 22:56 UTC) cover the chunk sets? If not, M2's feed likelihood uses bbo only on older chunks.
