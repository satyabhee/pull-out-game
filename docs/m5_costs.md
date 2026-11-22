# M5 costs: Python vs Rust on dev20

Protocol (Satya, decision 5): median of 3 runs per backend, data capture paused (`capture.py` and `record_hl.py`
suspended with SIGSTOP 08:38:16 -> resumed 09:19:04 UTC, Nov 22), `--jobs 1`, under `handoff/guard.sh`, runs
interleaved py / rust. Each run is `replay.parity` (dev20 scored with every block driven by the adapter) and
re-checked parity: all 6 runs wrote CSVs byte-identical to the block-end baseline (`report/validate/m0_blockend20`).
Script and logs: `~/book-replay-data/handoff/m5/timings.sh`, `timings.log`, `t_{py,rust}_{1,2,3}.{log,trace}`.

## Stage 1: Rust subset search only (commit 283f077)

| backend | run 1 | run 2 | run 3 | **median** | peak RSS (median) |
|---|---|---|---|---|---|
| py | 452.1 s | 453.4 s | 457.1 s | **453.4 s** (22.7 s / chunk) | 1.30 GB |
| rust (subset search) | 362.4 s | 364.6 s | 358.3 s | **362.4 s** (18.1 s / chunk) | 1.28 GB |

Whole replay 1.25x faster. On the hot function alone (300 real-scale ledger searches, median of 3, capture
running so indicative) Rust is ~7x faster than Python, above the plan's 3x bar. The rest of a chunk is setup
(Python loaders, ~16-20 s) which stays in Python by design.

## Stage 2: whole engine in Rust, copy-on-write (commit 7af9cd2)

Second paused window: 09:46:55 -> 10:11:52 UTC (`timings2.sh`, `timings2.log`). The Python medians above still
apply: no Python engine or replay code changed between the two windows (only `replay/` adapter files and the crate).

| backend | run 1 | run 2 | run 3 | **median** | peak RSS (median) |
|---|---|---|---|---|---|
| py (window 1) | 452.1 s | 453.4 s | 457.1 s | **453.4 s** | 1.30 GB |
| rust: subset only (window 1) | 362.4 s | 364.6 s | 358.3 s | **362.4 s** | 1.28 GB |
| rust: subset + engine (window 2) | 353.5 s | 353.9 s | 353.9 s | **353.9 s** (17.7 s / chunk) | 1.43 GB |

Whole dev20 replay: 453.4 -> 353.9 s = **1.28x**. Peak RSS +10% (two book representations are never held at once;
the extra is the Rust extension plus persistent-map overhead).

### Where the time goes (3 dev20 chunks, median of 3 runs each, summed; `split2.py`, caches cleared before setup)

| part | py | rust | speed-up | note |
|---|---|---|---|---|
| setup (decode, oid facts, recorder reads, seed) | 52.65 s | 52.54 s | 1.0x | Python loaders, stays Python by design |
| block replay total | 21.78 s | 5.27 s | **4.1x** | |
| . ledger subset search | 18.98 s | 2.74 s | **6.9x** | the one-guess hot path |
| . engine calls (`engine.apply`) | 1.18 s | 0.79 s | 1.5x | one Python->Rust crossing per action + Fill objects |
| engine-state copy at block 105 (resolver shared) | 122.7 ms | 0.06 ms | **~2,000x** | copy-on-write; what M2 multiplies |
| `copy_book` (whole Replay, at block 105) | 2.01 s | 1.86 s | 1.08x | dominated by the oid resolver's facts, touch, ledger |

Reading: per-block work is 4x faster and an engine copy is nearly free, but a chunk is ~75% setup, so a single
replay gains 1.28x overall. For M2 the next cost is the non-engine Replay state copied per guess (QUESTIONS Q2).
No `cargo bench` / criterion suite was added: the plan's measurements are end-to-end, and these timers answer them.

## Budget table (M5)

| limit | default | measured | hit? |
|---|---|---|---|
| Rust faster on the hot path (stop rule) | >= 3x | 6.9x (ledger subset search); engine calls 1.5x | no (hot path passes; engine below bar, see Q4) |
| byte-identical fills, Rust vs Python on dev20 | identical | identical: block-end and mid-block, all CSVs | no |
| property tests | identical | 1,500 engine + 400 book + 3,400 subset cases identical | no |
| per-switch parity | identical | 8 of 8 combinations identical, fast set AND full dev20 | no |
| peak RSS per run | (global guard 2.5 GB) | 1.43 GB (py 1.30 GB) | no |
| runtime vs baseline | (no M5 limit) | 0.78x of the Python run (1.28x faster) | no |
| timings protocol | median of >= 3, capture paused | 3 runs per backend, two paused windows (41 + 25 min) | no |
| N_max in {1, 64, 256} | after M2 | not applicable yet (no particle filter) | n/a |
