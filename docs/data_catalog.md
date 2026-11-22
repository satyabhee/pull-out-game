# Data catalog

Root on this Mac: `~/hl-data/mac3` (`windows/` = captured chunks, `hl_data/<day>/<hour>/` = recorder streams,
other folders = chunk sets made of links into `windows/`). Raw data never goes into git.

## Ingest gaps (no recorder / capture data)

Measured from the recorder files' receive times (bbo, trades, blocks and `book_*_fast` all silent; machine-readable
in `data/ingest_gaps.json`):

| UTC (2026-11-22) | length | cause |
|---|---|---|
| 08:27:34 - 08:27:50 | 16 s | M5 timing script paused the ingest and failed at once; its trap resumed it (reported as "~10 min" that day; the data shows 16 s) |
| 08:38:16 - 09:19:34 | 41.3 min | ingest paused for M5 timing window 1 |
| 09:46:55 - 10:12:08 | 25.2 min | ingest paused for M5 timing window 2 |

Rule since Nov 22 (Satya): the ingest is never paused for timing runs again (more runs + median instead).

**Keeping gaps out of chunk sets.** A chunk touches a gap when [first block time - 15 min, last block time + 5 min]
overlaps one (a replay reads recorder data up to 15 min before a chunk and up to 5 min after it).
`analysis/validate_chunks.find_chunks` skips such chunks with the reason "ingest gap", so no runner can use one, and
`python -m data.catalog ~/hl-data/mac3` checks every set (exit 1 if any set contains one).

Check on Nov 22: all 11 sets clean (dev20, confirm68, final240, held, held66, held67, new3, new34, newcov, post, tgt);
12 captured windows touch a gap and are never to be used: 1230427458, 1230430684, 1230434094, 1230472160,
1230474547, 1230478653, 1230481969, 1230491810, 1230516757, 1230520072, 1230523191, 1230526035 (start heights).

## Chunk sets

| set | chunks | role |
|---|---|---|
| dev20 | 20 | development (= the fix20 / lead20 / ledg20 list); all tuning and benchmarks |
| confirm68 | 68 | confirmation set (runs need Satya's approval) |
| final240 | 240 | final set (runs need Satya's approval) |
| held66, held67 | 66, 67 | hold-outs, never tuned on |
| held, new3, new34, newcov, post, tgt | 133, 3, 34, 50, 74, 13 | earlier working sets |

## Order histories (decision 6, before M3)

See the section added by `analysis/dedupe_histories.py` below once it has run.
