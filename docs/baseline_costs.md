# Baseline costs on dev20 (M0 · Idle, measured Nov 22)

Machine: the 16 GB Mac (recorder + capture ingest running throughout). Python 3 from `~/book-replay-data/.venv`,
`--jobs 1`, every run under `handoff/guard.sh` (polls the process-tree RSS every 0.5 s; kills at 2.5 GB).
Baseline = `replay.adapter.BASELINE_OPTIONS` (frozen config + `--p-priority docs`, listing policy): the engine at
58.8% contested strict on final240.

## Baseline

| quantity | value | how |
|---|---|---|
| chunks / blocks | 20 / 4,200 | `~/hl-data/mac3/dev20` |
| wall time | **459 s = 22.9 s per chunk** | `validate_chunks dev20 <baseline options> --jobs 1 --out report/validate/m0_base20` |
| per-chunk split (one chunk, adapter timings) | setup ~18 s (decode, oid facts, recorder reads, seed), blocks ~30 ms each | `new_book` then 105 `apply_block` calls |
| peak RSS | **1.22 GB** (1,220,672 KB) | guard, whole process tree |
| disk, inputs | 637 MB chunk folders (`windows/`, 20 chunks) + 2.08 GB recorder files in the 14 hours touched | `du -L`; recorder hours = chunk span +-10/5 min |
| disk, outputs | 728 KB (7 CSVs) | `report/validate/m0_base20` |
| scores (identical to the Nov 22 `pdocs20` run, txs.csv byte-equal) | contested strict 0.620 (union) / 0.661 (real makers), uncontested 0.715, all scored 0.680 | maker-strict per-tx, `analysis/contested_paired` |

## Through the adapter (M0 parity)

`python -m replay.parity ~/hl-data/mac3/dev20 --hl-data ~/hl-data/mac3/hl_data --out report/validate/m0_adapter20 --baseline report/validate/m0_base20`

| quantity | baseline | adapter | ratio | M0 budget | hit? |
|---|---|---|---|---|---|
| wall time, 20 chunks | 459 s | 456 s | **0.99x** | <= 1.2x | no |
| peak RSS | 1.22 GB | 1.29 GB | 1.06x | (no M0 limit; global guard 2.5 GB) | no |
| book copies without branching | 0 | 0 (`BookHandle.copies`, asserted in tests) | | none | no |
| output CSVs | 7 | 7, **all byte-identical** | | identical | |

Both runs are single measurements on a shared machine (ingest running); run-to-run noise was not measured, so
treat the 0.99x as "no measurable overhead", not a speed-up.

## copy_book (for M2 planning)

One chunk (1227159597-1227159806), after 105 of 210 blocks: `copy_book` = **0.85 s**, about 28 blocks of replay
time. Peak RSS did not rise (1,269 MB before and after), because setup had already set the peak; the copy's own
footprint is therefore not isolated by this measurement. A deep copy per guess would be far too slow for M2's
N_max = 64 per block; M2's copy-on-write levels are required, as the plan says.
