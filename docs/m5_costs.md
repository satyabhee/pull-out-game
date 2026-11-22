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

## Stage 2: + Rust copy-on-write book (this branch, after 283f077)

Not timed with capture paused (the pause needs Satya's go-ahead per run window; see docs/overnight_report.md).
