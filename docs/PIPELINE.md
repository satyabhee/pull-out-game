# The pipeline, end to end (branch `unified`)

For a reader who knows basic Python but not this repo. Four stages, run in order:

```
 ingest  ->  chunks  ->  replay  ->  scoring
 (server +   (folders   (engine     (compare our fills with
  recorder)   of blocks)  per block)   the chain's real fills)
```

Vocabulary used below:
- **Block**: one Hyperliquid block, a list of signed transactions (txs) in execution order (docs/l1_listing_order.md).
- **Maker / taker**: the maker's order was resting in the book; the taker's order arrived and traded against it.
- **Contested trade**: a real trade where more than one maker could have been filled at that price, so getting the
  maker right needs the queue order right. Our headline metric is **contested maker-strict recall**: the share of
  contested real fill notional that our replay gives to the same maker at the same price.
- **Placeholder**: anonymous liquidity we put in the book from an L2 snapshot because we cannot see who placed it.
- **B0..B3**: baseline configurations (flag sets), defined in `docs/baseline.md`. B3 is frozen (FROZEN v5).

All raw data lives outside git under `~/hl-data/mac3/` (`windows/` = captured chunks, `hl_data/<day>/<hour>/` =
recorder streams, other folders = chunk sets made of symlinks into `windows/`).

---

## Stage 1. Ingest: capture blocks and market data while they are fresh

**Why it exists.** Hyperliquid's explorer gives full blocks only while they are new (archived blocks are capped at
100 per day per IP), and there is no free bulk history with every user's actions. So we must record live:
- the blocks themselves;
- the public market data we score against (trades, book snapshots);
- per-user facts that help us guess what we can't see (order statuses, margin).

| what | code | output files | runs as |
|---|---|---|---|
| block chunks + per-chunk lookups | `collect/capture.py` (uses `collect/fetch_blocks.py`, `collect/info.py`, `collect/backoff.py`) | `windows/<start>-<end>/<height>.json.gz` + `window.json` (incl. `capture_version`) + `anchors.json`; since Nov 22 also `clearinghouseState.json`, `spotClearinghouseState.json` (margin), `margin_open.json`, `hist_orders.json`, `suspect_status.json` (suspect makers' `orderStatus`), since Nov 24 `truth_status.json` (live IOC `orderStatus`, TRUTH_IOC), `open_orders_truth.json`, `open_book_users.json` | Mac: `caffeinate -i python3 -u capture.py` in `~/hl-data/mac3` -> `capture.out` (see below); server: `hl-capture` service |
| market data streams | `collect/record_hl.py` | `hl_data/<day>/<hour>/` (gzipped when the hour closes): `trades_{BTC,ETH}.jsonl`, `book_{COIN}.jsonl` (20-level L2, ~12 s), `book_{COIN}_sf4.jsonl` (L2 at 4 significant figures, deeper), `book_{COIN}_fast.jsonl` (5 levels with order counts n, ~0.5 s, since Nov 21 22:56 UTC), `bbo_{COIN}.jsonl`, `blocks.jsonl`, `user_orders.jsonl` / `user_fills.jsonl` (tracked users) + `tracked_users.jsonl` (who was tracked when: a user's stream is complete only while they're in the set; first pick 600 s after start) | Mac: `caffeinate -i python3 -u record_hl.py BTC ETH` in `~/hl-data/mac3` -> `recorder.log`; server: `hl-recorder` service |
| request / throttle logs | `collect/fetch_blocks.py`, `collect/backoff.py` | `~/hl-data/mac3/requests.jsonl` (one row per explorer request: height, ms, status, outcome), `~/hl-data/mac3/http429_hourly.json` (HTTP 429 count per UTC hour and source, since capture 2026-11-26-backoff429) | written by the capture |
| copy server -> Mac | `server/sync_down.sh` | `~/hl-data/mac3/...` | by hand |
| free all-user truth (small) | `collect/sqd_pull.sh` | `~/hl-data/sqd/blocks/<height>.jsonl` (SQD replica_cmds: every action with user, nonce, per-item outcome); every query is logged in `handoff/sqd_queries.log` | by hand, quota ~50 MB/month |

**How the Mac ingest runs.** The capture and recorder run from COPIES of the `collect/` files in `~/hl-data/mac3`
(`capture.py`, `fetch_blocks.py`, `info.py`, `backoff.py`, `record_hl.py`), under the system `python3`, not the
repo's venv.
- **Settings** are environment variables. The current command line is the latest `~/hl-data/mac3/restart_*.sh`
  (now `restart_backoff429.sh`).
- **Deploying a capture change** takes three steps:
  1. Copy the file from `collect/`, keeping the old copy as `<file>.pre-<tag>`.
  2. Bump `CAPTURE_VERSION` in `capture.py`; it is written into every new `window.json`.
  3. Restart with a new `restart_<tag>.sh`.
- **Rules:**
  - Never pause the ingest (`never-pause-ingest` rule; a pause or outage becomes an ingest gap).
  - HTTP 429s are retried with exponential back-off (equal jitter, cap 60 s) for both explorer and info lookups.
- **If the ingest stops,** log the gap in `data/ingest_gaps.json`. Use the first silent and the first resumed
  `recv_ms` of the recorder files; `end_ms: null` while it is still open. `data/catalog.py` and
  `analysis/validate_chunks.find_chunks` then keep every chunk that touches it out of all sets.
- **Do not touch it from this worktree.**

| derived truth (offline, diagnostic; never an input to a baseline rule) | code | output |
|---|---|---|
| tracked users' order truth O1-O4 (status, oids, pre-window queue, IOC status) | `analysis/oracle_truth.py` (from `user_orders`, `hist_orders.json`, `anchors.json`, `suspect_status.json`) | `handoff/oracle_truth/<set>/` |
| all-user outcomes in SQD-fetched blocks | `analysis/sqd_truth.py` (explorer tx <-> SQD (actionIndex, item) alignment) | `handoff/truth_audit/sqd/<set>/{status,ioc}.csv` |
| gate 2 send times (nonce, batch, class, calibrated send time) | `analysis/gate2_input.py`, `analysis/gate2_sendtimes.py` (docs/gate2_data.md) | `handoff/gate2_input/`, `handoff/gate2/{blocks,send_times*}.csv` |

**B0 -> B3 at this stage:** B1's margin rule needs `clearinghouseState.json`, which the capture writes only for chunks
captured since Nov 22 04:20 UTC (dev20 and final240 are older, so B1 is inert there). B2 / B3 need nothing new.

## Stage 2. Chunks: choose which captured blocks we replay and score

**Why it exists.** A chunk is a short run of blocks (a 150-block lead-in to warm up the book, then the scored part)
that starts right after a book snapshot, so the replay can seed from the snapshot. Grouping chunks into fixed **sets**
keeps tuning and testing honest: we tune on dev sets, confirm on fresh sets used once, and report on a hold-out.

| what | code | output |
|---|---|---|
| list / validate chunks, skip bad ones (ingest gaps, incomplete) | `analysis/validate_chunks.find_chunks`, `data/catalog.py` (`data/ingest_gaps.json`) | the list of chunk folders a run will use |
| freeze fresh confirm sets | `collect/confirm_sets.py` | `handoff/confirm_sets/*` (start heights, frozen before any replay) |
| decode a chunk | `data/parse.py` (`load_window`, `decode_tx`) | typed actions (`engine_py/types.py`: `Place`, `Cancel`, `CancelByCloid`, `Modify`, `ScheduleCancel`, `OutsideFill`), prices / sizes as integer ticks / lots |

Sets (folders of symlinks under `~/hl-data/mac3/`): `dev20`, `devfast`, `dev_margin60` (dev, pooled for screens),
`final240` (hold-out; the B3 headline is on **final220 = final240 minus dev20**), plus retired confirm sets.

One command (check every set is clean): `uv run python -m data.catalog ~/hl-data/mac3`

**B0 -> B3 at this stage:** none (sets are fixed). The contested tx set of each dev set is fixed by B1's reference runs,
so later baselines are scored on the same trades.

## Stage 3. Replay: rebuild the book block by block

**Why it exists.** To ask "who would have been filled", we need the book as it stood at every tx. The replay seeds the
book from the nearest snapshot, applies every action of every block in execution order through a matching engine, and
corrects drift from later snapshots. Most of the code is about what we can't see: pre-window orders (placeholders),
orders the chain silently rejected (margin, post-only), cancels whose target we must guess (oid resolver).

| piece | code |
|---|---|
| per-block loop | `engine_py/replay.py` `Replay.step_block`: touch estimate -> touch reconcile / ledger -> order the block's actions (`--policy`, `_ordered`) -> apply each (`_apply`: ALO gate, taker-reject rules) -> trade corrections -> `end_block` (scheduleCancel) -> snapshot reconcile -> deep book |
| matching engine | `engine_py/matching.py` `Engine` (price-time priority, fills at the maker's price, 400 ms ALO priority window under `--p-priority docs`), `engine_py/book.py` |
| Rust backend (same answers, faster) | `replay_core/` (built with `make rust`), wired in by `replay/adapter.py`; checked byte-identical against Python by `replay/parity.py` and `tests/test_replay_core.py` |
| cancel-by-oid resolver | `data/oids.py` |

Inputs: a chunk folder + `hl_data`. Output: our fills per block (taker tx hash, maker order, px, sz), consumed directly
by stage 4 (the replay is not run on its own in normal use).

**B0 -> B3 at this stage** (flags in `docs/baseline.md`):
- B0: the base flag set (`--alo-gate touch --reseed --keep-known --p-priority docs ... --learn-evidence tx --trade-learn-at block-end --policy listing`).
- B1 = B0 + `--taker-reject margin`: skip an IOC that grows a user's position when their captured free margin can't cover it (chain would reject it).
- B2 = B1 + `--touch-trades-first` (trades and bbo of one block share a time; apply the trades first so they never overwrite the post-block bbo) + `--stp expire_maker` (a self-trade cancels the RESTING order, as the chain does).
- B3 = B2 + `--gate-spare-after` (an admitted post-only order evicts nothing placed after the touch evidence).

## Stage 4. Scoring: compare with the chain's real fills

**Why it exists.** It is the only check that the replay is right. Each real trade carries the taker's tx hash and the
maker's address, so for every scored tx we compare our fills with the real ones. Paired tests compare two configs on
the same trades, with chunk-clustered uncertainty (trades in one chunk share causes, so they are not independent).

| what | code | output |
|---|---|---|
| replay + score one or many chunks | `analysis/validate_chunks.py` (calls `analysis/validate_replay.run`) | `report/validate/<name>/{txs.csv, chunks.csv, summary.csv, per_asset.csv, mismatches.csv, horizon.csv, alo_truth.csv}` |
| contested scope + paired test (one set) | `analysis/contested.py`, `analysis/contested_paired.py` (chunk-cluster bootstrap, Holm) | printed table |
| pooled paired test (several sets) | `analysis/pooled_paired.py` | printed table (e.g. `handoff/headline_b3/paired_v2_vs_b3.txt`) |

One command (B3 on dev20):
```
B3="$(sed -n 2p ~/book-replay-data/handoff/i27/baseline.txt)"
uv run python -m analysis.validate_chunks ~/hl-data/mac3/dev20 --hl-data ~/hl-data/mac3/hl_data $B3 --jobs 1 --out report/validate/b3_dev20
```
`txs.csv` has one row per scored tx (chunk, hash, real vs replay notional, maker-strict match, category); the headline
is the contested subset of it, paired against the reference run.

**B0 -> B3 at this stage:** no scoring change. Adoption rules changed over time (`docs/scoreboard.md`; bug-fix track,
BLITZ Addendum 1). Results per baseline: `handoff/ACCURACY.md`. B3 headline on final220: contested 56.2% -> 59.4%
(+3.2 [+1.5, +5.0], n 1,932, 201 chunks) vs FROZEN v2.

---

## Checks that guard the pipeline

- `make check` (before every commit): secret scan, `pytest` (includes the look-ahead rule test and Rust-vs-Python
  parity on a fast test set), `cargo test` + `cargo clippy -D warnings` for `replay_core`.
- `make checkpoint`: per-switch parity and full dev20 Rust/Python parity.
- Merge gate for `unified` (Nov 26): B3 on final220 byte-identical to the frozen headline run (`docs/simplification.md`).
