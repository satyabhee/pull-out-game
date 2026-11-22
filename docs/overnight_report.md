# Overnight report, Nov 22 (M5 · Isomer), branch `mahito`

Stopped at the M5 checkpoint. M1-M4 not started. No pushes beyond the two backups you approved, no tags beyond
`m0-idle`, nothing touched on `main`, no confirm68 / final240, no change to Python engine behaviour, scoring or
defaults. The ingest was paused only inside the two timing windows (see 4) and resumed automatically both times.

## 1. Needs you (full text in docs/QUESTIONS.md)

1. **Q1. Default baseline: block-end (as committed in `283f077`) or mid-block?** Your decision 1 said block-end if
   the contested CI includes ~0 (it did: +0.0 [0.0, 0.0]); tonight's rules said "don't switch it". I changed
   nothing further. Rust parity is proven under BOTH settings on full dev20, so either answer is covered; reverting
   is one line (`BASELINE_SWITCHES` in `replay/adapter.py`). Recommendation: keep block-end.
2. **Q2. How M2 should use the Rust core.** The engine is now a per-action drop-in (identical fills). An engine copy
   costs 0.02 ms in Rust vs 43 ms in Python, but copying a whole replay is still ~0.8 s in both, dominated by
   non-engine state (oid resolver facts, touch, ledger). Recommendation: before M2, split the Replay into shared
   window inputs vs per-guess state, then profile N=16/64 and port only what that shows.
3. **Q4. Plan's 3x stop rule.** The hot path (ledger subset search) is 6.9x faster: passes. Engine calls are only
   1.5x (Python wrapper per action), but they are ~2% of a chunk. Recommendation: stop porting for speed here.
4. **Q5. The "problem page" (Examples 1, 2, 5, 6, 7) is not in the repo.** The Example-based unit tests are not
   written; please add the page or paste the examples.
5. **Q3. Switch the adapter's default backend to Rust?** Evidence supports it; recommendation is to keep "py" until
   M2 needs the speed. Left at "py".

## 2. What's done

| commit | what |
|---|---|
| `3e9493d` (capture-guard) | MAHITO_PLAN.md + puzzles.py |
| `7a36828` | merge data-streams into the new branch `mahito` |
| `3b85b1c` = tag `m0-idle` | M0: adapter, engine map, dev20 parity byte-identical, commit guards |
| `283f077` | M5 step 1: ledger subset search in Rust; block-end baseline (decision 1); stp/order/correction switches |
| `7af9cd2` | M5 step 2: Rust copy-on-write book + full matching engine; parity on dev20 and all 8 switch combinations |
| (this commit) | docs: costs, questions, walkthrough, this report; full-dev20 parity for every switch combination |

Also: backups `origin/backup/data-streams` (a322eec) and `origin/backup/mahito` (283f077); pre-commit hook enabled
for this worktree only; permissions allowlist in `.claude/settings.local.json` (not committed).

## 3. Parity, Rust vs Python, per switch combination

Fast set = first 40 blocks of dev20 chunk 1227159597 (`tests/test_switch_parity.py`). Full dev20 = all 20 chunks
scored through `replay/parity.py`, every output CSV byte-compared with the Python run.

| stp | order | correction | fast set | full dev20 |
|---|---|---|---|---|
| taker | proposer | blockend (**baseline**) | identical | **identical** (vs `m0_blockend20`; 4 runs) |
| taker | proposer | midblock | identical | **identical** (vs `m0_base20`, the 58.8% config) |
| taker | class_sort | blockend | identical | **identical** |
| taker | class_sort | midblock | identical | **identical** |
| maker | proposer | blockend | identical | **identical** |
| maker | proposer | midblock | identical | **identical** |
| maker | class_sort | blockend | identical | **identical** |
| maker | class_sort | midblock | identical | **identical** |

Rows 3-8: the Python reference was the same dev20 run with `--backend py` and that setting
(`report/validate/m5c_py_<setting>`), Rust compared against it (`m5c_rs_<setting>`); log
`~/book-replay-data/handoff/m5/combos.log`. No first differing fill to report: none differed.

Differential tests (Rust engine vs Python engine after every step): 1,500 random action sequences across every
engine setting (stp x 7 priority modes x evict-arrival) and 400 random book sequences: no difference. One degenerate
input differs and is excluded (an outside fill whose maker is literally named "placeholder"; never occurs in data;
deviations D22). No differing fill was found anywhere on real data.

## 4. M5 budget table (dev20, median of 3 runs, capture paused)

| | Python | Rust | ratio |
|---|---|---|---|
| whole dev20 run (20 chunks) | 453.4 s | 353.9 s | 1.28x faster |
| per chunk | 22.7 s | 17.7 s | |
| peak RSS | 1.30 GB | 1.43 GB | +10% |
| block replay (3 chunks) | 21.78 s | 5.27 s | 4.1x |
| . ledger subset search (hot path) | 18.98 s | 2.74 s | 6.9x (bar: 3x) |
| . engine calls | 1.18 s | 0.79 s | 1.5x |
| setup (Python loaders, by design) | 52.65 s | 52.54 s | 1.0x |
| engine-state copy | 122.7 ms | 0.06 ms | ~2,000x |
| whole-Replay copy (`copy_book`) | 2.01 s | 1.86 s | 1.08x |

Capture paused 08:38:16-09:19:04 UTC (window 1: Python + subset stage) and 09:46:55-10:11:52 UTC (window 2: full
engine), both resumed automatically. Details: `docs/m5_costs.md`.

## 5. Half-done, and where it stops

- **Example-based tests (Q5):** not started; blocked on the problem page.
- **N_max in {1, 64, 256} timings:** need M2 (out of scope tonight).
- **Default backend switch (Q3):** waits for your sign-off.
- **Tag `m5-isomer`:** not made; tags wait for your approval of the checkpoint.
- Nothing is mid-edit: the working tree has only your own uncommitted `.gitignore` line and untracked `viz/` files.

## 6. Next, by answer to the top question (Q1)

- **Q1 = keep block-end (recommended):** nothing to change. Next: answer Q2/Q4 (my recommendation: stop porting,
  plan M2's state split), then M1 (simulator) per the plan order, starting from the order-history dedupe you asked
  for before M3 (decision 6) only when M3 is next.
- **Q1 = revert to mid-block:** one-line change of `BASELINE_SWITCHES["correction"]` in `replay/adapter.py`,
  re-mark `tests/test_lookahead.py::test_baseline_reads_no_trades_before_fills` as `xfail(strict)` (and keep the
  mid-block detection test), update docs/deviations.md D4. Parity needs no re-run: the mid-block full-dev20 Rust run
  is already identical (`report/validate/m5_engine20_mid`).
