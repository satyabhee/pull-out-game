# G2 determinism check (Yuta, Nov 27, 21:24-22:05Z)

**Gate (README):** "G2 Deterministic: identical per-block hashes across two runs, different hash seeds, and a restart
from snapshot."

**Setup:**
- Code: `unified` 2b16768, in its own worktree, `git worktree add --detach ~/book-replay-detcheck 2b16768`. The capture
  was not touched.
- Python: `~/book-replay-unified/.venv` (the Python files import from the worktree; checked).
- Rust: `replay_core`, the build installed in that venv on Nov 26 16:42. Its source last changed in 4c542ae (Nov 23).
- Data: dev20 (20 chunks, 4,200 blocks), B3 = `handoff/i27/baseline.txt` line 2.
- Scripts and logs: data-streams `checks/determinism/`. Outputs: `~/book-replay-detcheck/report/determinism/`.

## Update after Satya's decision (~22:10Z Nov 27)

**Decision:** the SHA-256 book digest below is the **official per-block state hash**.
- **Scope = the books:** price, size, queue position and identity of every resting order.
- **Stated exclusion:** ledgers, learners, oid maps and counters are not in the hash.
- **Snapshot save / restore is not built today.**

| condition | status now |
|---|---|
| per-block state hash | **built:** `engine_py/state.py` (`encode_books`, `block_hash`). It is the same code as `checks/determinism/book_digest.py`, so the 4,200-block results below are results for this hash. Pass `hasher=state.block_hash` to Replay. The default stays None. |
| two runs, different PYTHONHASHSEED | **PASS:** `tests/test_determinism.py::test_two_runs_different_hash_seeds_identical` (a seeded 60-block simulated chunk, two subprocesses with PYTHONHASHSEED 1 and 2), plus dev20 4,200 / 4,200 below |
| Rust vs Python | **PASS:** `test_rust_equals_python_per_block_hashes` (simulated chunk, baseline and class-sort orders), plus dev20 4,200 / 4,200 below |
| the hash can fail | **PASS:** `test_hash_sees_an_ordering_change`, plus the dev20 sensitivity run below |
| **restart from snapshot** | **NOT BUILT.** `tests/test_snapshot.py` is an explicit skip, so the gap shows in every run. |

- **Tests run:** on unified 2b16768 + these files (~/book-replay-detcheck), `nice -n 19`: 4 passed, 1 skipped (the
  snapshot test), 28 s under load.
- On data-streams, which has no `replay.sim` or `replay_core`, the 3 simulated-chunk tests skip and the encoding test
  passes.
- **G2 status:** met for "two runs, different hash seeds" and for Rust vs Python, with the book-scope hash. "Restart
  from snapshot" is not built.
- The verdict below is the 22:05Z check, from before the decision.

## Verdict (22:05Z, before the decision)

**G2 is NOT met as written.**
- The per-block state hash and snapshot restart the gate names don't exist: `engine_py/state.py`,
  `engine_py/snapshot.py`, `tests/test_determinism.py` and `tests/test_snapshot.py` are one-line TODO stubs.
- What can be tested, passes:
  - two runs with different hash seeds give byte-identical outputs, and identical per-block BOOK digests (an external
    proxy, below) on all 4,200 blocks;
  - the Rust backend reproduces the Python outputs byte for byte on all 20 chunks, and the same per-block book
    digests (all 20 chunks, 4,200 / 4,200 blocks).

| # | condition | result | evidence |
|---|---|---|---|
| 1 | `tests/test_determinism.py`, `tests/test_snapshot.py` | **FAIL (no tests exist)** | `pytest` collects 0 tests, exit 5 ("no tests ran"). Both files are docstring + `TODO`. |
| 2a | B3 on dev20, PYTHONHASHSEED=1 vs 2: outputs | **PASS** | All 7 output CSVs are byte-identical (e.g. txs.csv sha256 8769055dcc7bea38..., summary.csv 8db18e53d2f9f96a...). 20 / 20 chunks ok, 0 failed. |
| 2b | the same, per-block state hash (spec) | **NOT TESTABLE** | `engine_py/state.py` (canonical encoding + FNV-1a chain) is a TODO stub. Replay yields `state_hash = None` unless a hasher is passed. |
| 2c | the same, per-block BOOK digest (proxy, mine) | **PASS** | 4,200 / 4,200 blocks identical (20 chunks; digests.csv sha256, seed 1 = seed 2: acb41881..., 7a7f0d03..., 66367485...). |
| 3 | restart from a mid-window snapshot | **FAIL (not implemented)** | `engine_py/snapshot.py` ("save and restore state at a height; resume replay from it") is a TODO stub. There is no save / restore / resume API, so no restart can be run. |
| 4a | Rust backend vs Python, same run: outputs | **PASS** | `replay.parity --backend rust` on dev20 with the B3 options: "PARITY OK: every baseline CSV identical" vs the PYTHONHASHSEED=1 Python run (20 chunks, 1,058 s, setup cache off). |
| 4b | Rust vs Python: per-block book digest (proxy) | **PASS** (all 20 chunks, 4,200 / 4,200 blocks) | The adapter path runs Replay.step_block, which calls the hasher with the `replay.rust_engine.RustEngine` (books = `replay.rust_book` wrappers over replay_core). Its digests.csv equals the Python one byte for byte. `checks/determinism/rust_digest.py`. |
| - | reproducibility over time (extra) | PASS | My dev20 txs.csv equals the earlier `report/validate/unified_tip_dev20` and `unified_parity_dev20` runs (same sha256 8769055d...). |

## The book digest (condition 2c), and the check that it can fail

- **Code:** `checks/determinism/book_digest.py`. It is not engine code. It passes Replay's existing hook
  `hasher(engine, prev_hash, height)` (engine_py/replay.py, called at every block end).
- **What it computes:** sha256(prev || dump). The dump covers each asset in sorted order, bids then asks, each best
  first. Each level gives its px, then its nodes in queue order as (oid, user, remaining, seq). Placeholders are
  included as '-'.
- **What it covers:** the books (prices, sizes, queue order, order identity). It does NOT cover ledgers, learners,
  oid maps or counters. So it is a proxy for the spec's state hash, not a replacement.
- **The books are non-empty:** dumps were 6,190 to 160,902 bytes per block on the first chunk.
- **The digest catches a real change.** On chunk 1227159597, an ordering change (`--policy class` added) changes the
  digest from block 4 on, in 207 / 210 blocks, and txs.csv changes with it.
- Three other switch changes there left both the digest and txs.csv unchanged: `--stp cancel_taker`, no
  `--deep-book`, and `--nearest-ratio 1`. They are inert on that chunk, so they say nothing either way. Removing
  `--reseed` is invalid with `--keep-known` ("keep_known needs reseed=True").

## Commands (from ~/book-replay-detcheck; scripts in checks/determinism/)

    PY=~/book-replay-unified/.venv/bin/python
    # 1
    $PY -m pytest -q -p no:cacheprovider tests/test_determinism.py tests/test_snapshot.py       # -> no tests ran (exit 5)
    # 2a: run_py.sh SEED OUT
    PYTHONHASHSEED=1 $PY -m analysis.validate_chunks ~/hl-data/mac3/dev20 --hl-data ~/hl-data/mac3/hl_data \
        --out report/determinism/py_seed1 --jobs 3 <B3 flags>
    PYTHONHASHSEED=2 ...                                   --out report/determinism/py_seed2 ...
    for f in report/determinism/py_seed1/*.csv; do cmp $f report/determinism/py_seed2/$(basename $f); done
    # 2c: run_digest.sh / run_digest2.sh SEED OUT CHUNKLIST (4 + 8 + 8 chunks = all 20)
    PYTHONPATH=. PYTHONHASHSEED=1 $PY report/determinism/book_digest.py report/determinism/digest_seed1 <chunk dirs>
    cmp report/determinism/digest_seed1/digests.csv report/determinism/digest_seed2/digests.csv
    # sensitivity: run_digest2.sh 1 digest_sens5 chunks_one.txt "--gate-spare-after=>--gate-spare-after --policy class"
    # 4b: PYTHONPATH=. $PY report/determinism/rust_digest.py report/determinism/digest_rust <chunk dirs>; cmp with digest_seed1
    # 4a: run_rust.sh OUT BASELINE
    PYTHONHASHSEED=3 $PY -m replay.parity ~/hl-data/mac3/dev20 --hl-data ~/hl-data/mac3/hl_data \
        --out report/determinism/rust --baseline report/determinism/py_seed1 --backend rust --no-cache --options "<B3 flags>"

## What would make G2 pass

- **engine_py/state.py:** the canonical encoding and hash. Its hash function is marked as Satya's call (replay.py
  docstring, section 5.3).
- **engine_py/snapshot.py:** save / restore / resume.
- **The two test files.**
- **The real state hash on the Rust side.** The book proxy already compares the two (4b).

I didn't implement any of these: tonight's brief was to verify, and the hash function is a design decision.
