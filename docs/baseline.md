# Baseline definition (single source; mahito and data-streams both read this before every A run)

Path for other worktrees: `~/book-replay/docs/baseline.md` (branch `mahito`). Any change to the baseline is made
HERE, in the same commit that adopts it, with a row in `docs/scoreboard.md`.

## Current baseline: B1 = B0 + `--taker-reject margin` (since Nov 23; MAHITO_PLAN §00)

Engine: the replay engine on `mahito`, Rust backend (Python engine = reference, byte-identical on B1: `replay.parity --backend rust`, dev20 and dev-fast).

```
--alo-gate touch --reseed --nearest-ratio 3 --keep-known --outside-other-txs --deep-book --p-priority docs \
--evict-arrival --oid-bracket --touch-reconcile --touch-evict ledger --ledger-k 99 \
--learn-evidence tx --taker-reject margin --trade-learn-at block-end --policy listing --stp cancel_taker
```
= `replay.adapter.options_for()` (switches: order=proposer, stp=taker, correction=blockend). `--taker-reject margin`
only (data-streams' margin gate; `learn` stays out: rejected on final240, FROZEN_CONFIG.md). It skips an IOC that grows
the user's position when their plan-time free margin (clearinghouseState.json) is under notional / max leverage; it
acts only on chunks captured since Nov 22 04:20 UTC (dev20: none, dev-fast: all). Lead-in: as captured (150 blocks).

Order "proposer" = the explorer listing, which is the execution order (L1, docs/l1_listing_order.md).

| set | contested strict | uncontested strict | reference outputs |
|---|---|---|---|
| dev20 | 0.587 (n 113) | 0.713 (n 195) | `report/validate/margin_dev20` (= `m1_ref_blockend20`, byte-identical) |
| dev-fast | 0.460 (n 49) | 0.731 (n 84) | `report/validate/margin_devfast` |

B0 (before Nov 23, same flags without `--taker-reject margin`): dev20 as above; dev-fast 0.442 / 0.694
(`report/validate/devfast_b0`). The contested set is fixed by the reference run's own engine makers, so B1 defines
a slightly different dev-fast set (49 / 84) than B0 (50 / 83); on B0's set B1 scores 0.471 / 0.728. Adoption: docs/scoreboard.md row "B0 + --taker-reject margin".

## Differences from data-streams' FROZEN v2 (handoff/FROZEN_CONFIG.md)

FROZEN v2 = the B0 flags WITHOUT `--learn-evidence tx` and with mid-block corrections (`--trade-learn-at tx`); data-streams'
frozen v3 adds `--taker-reject margin` (Satya, Nov 23: "if it passes there, it goes into frozen config v3").
Its final240 headline (56.3% contested on its own contested set, 62.7% all-scored) is the record, not B1. B0 / B1 have not
been run on final240 or confirm68 (hold-outs need Satya's OK).

## How to compare against B0

```
python -m analysis.validate_chunks ~/hl-data/mac3/dev20 --hl-data ~/hl-data/mac3/hl_data <B0 flags> <switch> \
  --jobs 1 --out report/validate/<name>
python -m analysis.contested_paired <dir with base/ and variant/ links> ~/hl-data/mac3/dev20 --ref base --boot 4000
```
(same for `~/hl-data/mac3/devfast`). Adoption rule: docs/scoreboard.md.
