# Baseline definition (single source; mahito and data-streams both read this before every A run)

Path for other worktrees: `~/book-replay/docs/baseline.md` (branch `mahito`). Any change to the baseline is made
HERE, in the same commit that adopts it, with a row in `docs/scoreboard.md`.

## Current baseline: B0 (since Nov 23; MAHITO_PLAN §00)

Engine: the replay engine on `mahito`, Rust backend (Python engine = reference, byte-identical on B0).

```
--alo-gate touch --reseed --nearest-ratio 3 --keep-known --outside-other-txs --deep-book --p-priority docs \
--evict-arrival --oid-bracket --touch-reconcile --touch-evict ledger --ledger-k 99 \
--learn-evidence tx --trade-learn-at block-end --policy listing --stp cancel_taker
```
= `replay.adapter.options_for()` (switches: order=proposer, stp=taker, correction=blockend). No `--taker-reject`
(learn rejected on final240, FROZEN_CONFIG.md). Lead-in: as captured (150 blocks).

Order "proposer" = the explorer listing, which is the execution order (L1, docs/l1_listing_order.md).

| set | contested strict | uncontested strict | reference outputs |
|---|---|---|---|
| dev20 | 0.587 (n 113) | 0.713 (n 195) | `report/validate/m1_ref_blockend20` |
| dev-fast | 0.442 (n 50) | 0.694 (n 83) | `report/validate/devfast_b0` |

## Differences from data-streams' FROZEN v2 (handoff/FROZEN_CONFIG.md)

FROZEN v2 = the same flags WITHOUT `--learn-evidence tx` and with mid-block corrections (`--trade-learn-at tx`).
Its final240 headline (56.3% contested on its own contested set, 62.7% all-scored) is the record, not B0. B0 has not
been run on final240 or confirm68 (hold-outs need Satya's OK).

## How to compare against B0

```
python -m analysis.validate_chunks ~/hl-data/mac3/dev20 --hl-data ~/hl-data/mac3/hl_data <B0 flags> <switch> \
  --jobs 1 --out report/validate/<name>
python -m analysis.contested_paired <dir with base/ and variant/ links> ~/hl-data/mac3/dev20 --ref base --boot 4000
```
(same for `~/hl-data/mac3/devfast`). Adoption rule: docs/scoreboard.md.
