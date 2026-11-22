# Engine map (M0 · Idle)

Where each piece of MAHITO_PLAN's reference sketch lives in the real engine (branch `mahito` = `capture-guard`
+ merge of `data-streams` a322eec, the engine at 58.8% contested strict on final240). Paths are repo-relative.

## Engine pieces

| Sketch piece | Real location | Notes |
|---|---|---|
| `Order` | `engine_py/types.py` `Order` | frozen dataclass; `oid` = engine id (never the HL oid), `seq` = arrival order, `px` ticks, `sz` lots (ints only) |
| `Book` | `engine_py/book.py` `Book` | one per asset; `bids` / `asks` are `SortedDict` px -> `Level` |
| `Book.levels` | `Book.side(side)` -> `SortedDict[px, Level]` | `Level` = doubly linked FIFO of `Node(order, remaining)`, cached `total` (lots) and `count` (`len`) |
| `Book.by_oid` | `Book.index: dict[engine oid, Node]` | HL oid / cloid lookups live on the engine: `Engine.by_cloid`, `Engine.by_user`, resolver |
| `Book.best` | `Book.best_bid()`, `best_ask()`, `best_level(side)` | O(1) via `SortedDict.peekitem` |
| `Book.rest` | `Engine._rest` -> `Book.add(order, remaining, jumps=...)` | the priority insertion rule is the `jumps` closure built inside `Engine._rest` |
| `Book.remove` | `Book.remove(oid)`; engine-level `Engine._remove`, `Engine.evict`, `Engine._forget` | `_forget` drops the engine's user/cloid indexes |
| `match` | `Engine._place` (match loop) | re-reads the best level every iteration; fill at the maker's px; `Book.reduce` |
| `place` | `Engine.apply(Place)` -> `Engine._place` | ALO: reject if it would cross OUR book (`ALO_WOULD_CROSS`); IOC remainder dropped; GTC rests |
| cancel / modify | `Engine._cancel_cloid`, `Engine._cancel_hl_oid` (resolver `data/oids.py` `OidResolver`), `Engine.apply(Modify)` | modify = cancel + new order (new id, back of queue) |
| self-trade rule | `Engine(stp=...)`, default **`cancel_taker`** | the plan's §1 says the chain cancels the RESTING order (`expire_maker`); baseline uses `cancel_taker` (deviation D5) |
| 400 ms priority | `Engine._rest` `jumps` closure, mode `"docs"` (`DOCS_T_MS = 400`) | ALO only; placeholders / outside-evidence orders never jumped; baseline config `--p-priority docs` |
| `reconcile` | `Engine.reconcile(asset, bids, asks, known=...)` + `Engine.refill`; driven in `Replay.step_block` after `end_block` at each snapshot (rule 8) | `--reseed --keep-known`: trim to snapshot keeping oldest known orders, refill placeholders |
| `tx_class` | `analysis/validate_replay.py` `tx_class`, `class_policy` (also `priority_policy`, `anti_policy`, `shuffle_policy`) | baseline policy is `listing` (`engine_py/replay.py` `listing`): proposer order, no class sort |
| `Solution.replayBook` | `analysis/validate_replay.py` `run()` builds `engine_py/replay.py` `Replay` and iterates it | `validate_chunks.run_variants/run_batch` loop chunks |
| per-block loop | `Replay.step_block()` (was the body of `Replay.__iter__`; `__iter__` now calls it) | order: touch advance -> touch reconcile/ledger -> ordered actions (`_ordered`, `_sequence`) -> `_apply` (ALO gate, taker reject) -> trade_learn -> taker learn -> `end_block` (scheduleCancel expiries) -> snapshot reconcile -> deep book -> hash |
| placeholders | `Engine.seed`, `Replay._seed`, `_seed_levels`; user `PLACEHOLDER` | anonymous snapshot liquidity, queued ahead of window orders |
| outside fills | `engine_py/types.py` `OutsideFill`; `Engine._outside_fill`; `data/parse.load_window(outside_other_txs=...)` | zero-hash trades (liquidations, TWAP, triggers) applied as forced fills at block end |
| ALO gate on the real touch | `Replay._gate`, `Replay._apply_gated`, `RealTouch` | pre-block real touch only (events strictly before the block's time) |
| ledger / touch reconcile | `Replay._reconcile_touch`, `Replay._ledger_touch`, `unique_subset` | evicts a unique subset of known orders matching bbo (sz, n) |
| trade corrections | `Replay._learn`, `_learn_queue`, `parse_trade_learn` | **reads a tx's real trades mid-block** under the default `--trade-learn-at tx` (rule-1 deviation D4) |
| IOC margin rules | `Replay._taker_skip`, `_taker_learn`, `load_margin`, `load_hist_rejects` | `--taker-reject` rules; off in the baseline config |
| state hash | `Replay(hasher=...)` | None in validation runs |

## Data loaders

| Data | Files | Loader |
|---|---|---|
| L2 snapshot (20 levels, ~12 s) | `hl_data/<day>/<hour>/book_{BTC,ETH}.jsonl` | `data/parse.py` `iter_books`, `nearest_book`, `BookSnapshot`; seeded in `Replay._seed` |
| coarse deep book | `book_{COIN}_sf4.jsonl` | `iter_books(..., suffix="_sf4")` in `Replay._build_deep` (`--deep-book`) |
| blocks | `windows/<start>-<end>/<height>.json.gz` (+ `window.json`) | `data/parse.py` `read_block`, `decode_tx`, `iter_window`, `load_window` (cached, `_WINDOW_CACHE_MAX`) |
| bbo | `bbo_{COIN}.jsonl` | `engine_py/replay.py` `iter_bbo` -> `RealTouch` |
| `l2Book fast` (5 levels, ~0.5 s) | `book_{COIN}_fast.jsonl` (recorded since Nov 21 22:56 UTC) | **no loader exists**; dev20 / confirm68 / final240 predate it (plan's open question: answer is "not covered") |
| public trades | `trades_{COIN}.jsonl` | `data/parse.py` `iter_trades` -> `Trade` (maker = `users[1]` if taker buys, etc.) |
| account snapshots | `windows/<chunk>/clearinghouseState.json` (capture `MARGIN_USERS`, chunks after Nov 22 04:20 UTC) | `engine_py/replay.py` `load_margin` |
| `orderStatus` anchors | `windows/<chunk>/anchors.json`; `hl_data/.../alo_status.jsonl` | `data/oids.py` `OidEstimator` / `OidResolver`; `analysis/validate_replay.py` `load_alo_status` |
| order histories (`historicalOrders`) | `windows/<chunk>/hist_orders.json` (382 windows, 4.39M records, 66 users, overlapping between chunks; the plan's "1.73M" is presumably the de-duplicated count, not verified) | `data/oid_facts.py` `load` (source "hist"), `engine_py/replay.py` `load_hist_rejects` |
| tracked-user streams | `user_orders.jsonl`, `user_fills.jsonl`, `tracked_users.jsonl` | `data/oid_facts.py` `stream_orders_all`; `analysis/validate_replay.py` `stream_order_truth` |
| open orders / user fills at chunk time | `windows/<chunk>/open_orders.json`, `fills.json` | `data/oid_facts.py` `load` (sources "open", "fills"); 0 of dev20/confirm68/final240 have `open_orders.json` |

Root: `~/hl-data/mac3` (`windows/`, `hl_data/`); chunk sets are folders of symlinks into `windows/`.

## Scoring and chunk sets

| Piece | Location |
|---|---|
| per-tx rows, match / strict match, notional recall | `analysis/validate_replay.py` `run`, `is_match`, `is_match_px_sz`, `matched_notional` (strict = `matched_notional_full`, key (px, maker)), `categorize`, `CATEGORIES`, `summarize`, `write_csvs` |
| wildcard (placeholder) effective fills | `ValidateObserver.effective` (used when `--reseed`) |
| pooling over chunks, variants | `analysis/validate_chunks.py` `run_variants`, `run_batch`, `write_compare` |
| contested scope | `analysis/contested.py` `block_actors`, `classify`; `analysis/contested_paired.py` `scope_of` (union / real makers) |
| paired test vs a reference | `analysis/contested_paired.py` (chunk-cluster bootstrap, Holm) |
| dev20 (= fix20 / lead20 / ledg20 list) | `~/hl-data/mac3/dev20` (20 chunks) |
| 68-chunk confirm | `~/hl-data/mac3/confirm68` |
| 240-chunk final | `~/hl-data/mac3/final240` (list: `handoff/fresh_chunks_2026-11-21.txt` + held-out) |
| hold-outs (never tuned on) | `~/hl-data/mac3/held66`, `held67` |

## M0 additions (this branch)

| File | Role |
|---|---|
| `replay/adapter.py` | `EngineAdapter`, `BranchContext`, `BookHandle`; builds Replays through `validate_replay.run`'s own argument mapping (`replay_args`) |
| `replay/parity.py` | scores chunks with every block driven by `apply_block`, compares CSVs byte-for-byte with a baseline run |
| `engine_py/replay.py` | `__iter__` split into `_begin_blocks()` + `step_block()` (body unchanged) |
| `tests/test_adapter.py`, `tests/test_lookahead.py`, `tests/test_check_secrets.py` | adapter, rule 1, secret check |
| `scripts/check_secrets.py`, `scripts/git-hooks/pre-commit`, `make check` | §0b commit guards |
