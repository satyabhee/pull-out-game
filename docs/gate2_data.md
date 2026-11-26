# Gate 2 input: SQD blocks with full action lists and nonces

Written by Yuta (data-streams), 2026-11-26 ~22:55Z. Script: `analysis/gate2_input.py`. Output: `handoff/gate2_input/`.
Rebuild: `uv run python -m analysis.gate2_input --out handoff/gate2_input` (about 15 s; reads only files on disk).
No new SQD pulls were made for this. All numbers below are from `handoff/gate2_input/summary.json`, `inventory.csv`,
`nonce_order.csv`, `nonce_resolution.json` and `signer_skew.csv`.

## Decision: GO (71 usable blocks, at least 30 needed)
- **71 full blocks** on disk from Rika R0/R1 (`~/hl-data/sqd/blocks/<height>.jsonl`, every one pulled with the
  all-actions filter, logged in `handoff/sqd_queries.log`). 85,928 actions, 1,623 signers (accounts).
- **Every action has a nonce** (0 missing). Explorer alignment: 163,665 of 163,674 explorer txs map to an SQD
  (actionIndex, item) (99.995%), with 2-5 adjacent inversions per block, all between identical payloads, as in R0.
- **Every block is in a dev set:** dev_margin60 48, devfast 14, both 6, dev20 3. (The 3 dev20 blocks are also in
  final240, because dev20 is part of final240 by design. 5 devfast blocks are also in the retired confirm2_margin60.)
  No block is only in a hold-out set (`holdout_only` = 0 for all 71).
- BTC/ETH takers: 808 GTC/IOC actions on BTC or ETH in 69 blocks (47 blocks have at least 5). 77.5% of them have a
  fresh nonce, so they get a calibrated send time.
- Do nonces reflect send order? **Yes, at the 5-100 ms scale, inside a class (and a fee tier for takers).** They don't
  at a gap under 5 ms or over 100 ms (see (a)). That is enough to pass the rule, so no extra pull was made. If gate 2
  needs races decided by under 5 ms, nonces can't settle them; see Limits.
- Under the rule's 10-30 band this is not a pilot. Still, 71 blocks come from 35 chunks, so the blocks are not
  independent (see Limits).

## Definitions
- **Listing position** = SQD `actionIndex`. Explorer listing order = (actionIndex, item) order, re-checked on all 71
  blocks (above).
- **Class.** The empirical order inside a batch is C, then A, then G, then O:
  - C: cancel or cancelByCloid.
  - A: order, modify or batchModify whose items are all post-only (Alo).
  - G: order, modify or batchModify with at least one Gtc, Ioc or trigger item.
  - O: everything else (noop, scheduleCancel, updateLeverage, evmRawTx, validator actions, ...).

  I measured this order before using it. Of the non-cancel, non-taker actions in runs right after a cancel run, 34,969
  of 34,970 are ALO orders or modifies (1 noop). The actions after G are
  noop 850, scheduleCancel 302, oracle updates 51, leverage 39, ... (only 7 ALO orders). Note: the docs paraphrase in
  `analysis/validate_replay.py` puts "no GTC/IOC" before cancels. The data shows cancels first.
- **Batch.** Consensus batches are not labelled. A drop in class rank in listing order starts a new batch. Batches per
  block: 1 (23 blocks), 2 (37), 3 (8), 4 (3). 26 batches have fewer than 10 actions. These are mostly a short leading
  batch whose nonces are about 220 s old, or a split caused by an action out of class order.
- **Tier** (G only) = `grouping.p` (the priority fee; 0 when absent). Inside a G batch, listing order follows -p (median
  Kendall tau 0.50 over 59 batches with more than one p). So nonce order is checked within (batch, class, tier).
- **lag** = nonce - block time (ms). **fresh**: -10 s <= lag <= +2 s.
- **Calibrated send time** `send_cal_ms` = nonce - offset_u, with offset_u = the 95th percentile of signer u's fresh
  lags. The bound comes from this: an action reaches the proposer before the block time, so the true clock offset is
  at least max(lag). The 95th percentile is a robust max, which treats u's fastest sends as having about 0 latency.
  - Signers with fewer than 5 fresh actions get the pooled median offset, -162 ms (`calib = pooled`, 1,411 actions).
  - Non-fresh nonces get no send time (`calib = stale` 30,693 / `future` 2). Their nonce is not a send clock.

## Nonce checks
**(a) Nonce order vs listing order within (block, batch, class[, tier])**, Kendall tau per group:

| class | raw nonce: weighted tau (groups +/-) | fresh nonce | calibrated send | pairwise concordance at a 5-100 ms gap |
|---|---|---|---|---|
| C cancels | 0.255 (109 / 6) | **0.539** (100 / 0) | **0.623** (100 / 0) | 0.83-0.93 |
| A post-only | 0.268 (116 / 6) | **0.453** (100 / 0) | **0.481** (99 / 1) | 0.73-0.84 |
| G GTC/IOC (per tier) | 0.226 (95 / 27) | **0.351** (99 / 20) | 0.251 (90 / 31) | 0.76-0.80 (fresh nonce) |
| O other | 0.139 | 0.244 | 0.221 | - |

Resolution (`nonce_resolution.json`): pairwise concordance by |nonce gap|:
- C, fresh nonce: 0.70 under 5 ms, 0.83 at 5-20 ms, 0.93 at 20-50 ms, 0.90 at 50-100 ms, then **0.71 at 100-250 ms and
  0.45 over 250 ms**.
- G, fresh nonce: 0.62 under 5 ms, 0.76 at 5-20 ms, 0.80 at 20-50 ms, 0.57-0.60 over 50 ms.

Large gaps are anti-informative. A nonce signed long before its action arrives (queued, retried or pre-signed) sorts
early but lists late.

Calibration helps C and A but not G: 0.351 drops to 0.251. **For takers, use the fresh raw nonce inside a tier**
(both columns are in the rows).

**(b) Per-signer clock skew (`signer_skew.csv`)**:
- 517 signers are calibrated. Offset quantiles: p05 -1,176 ms, median -162 ms, p95 -84 ms.
- 10 signers have offset > 0 (their clock runs ahead of block time). One example is 0x7b7f72a2, with median lag
  +247 ms over 3,166 actions.
- 224 signers have no fresh nonce at all (19,719 actions; their nonce is not a clock). This includes 0xf5d81a13, the
  #1 open-order-cap user.
- Odd nonces:
  - future (lag > +2 s): 2 actions (0x7cd96839 +52.7 s, 0x12e53c4d +20.8 s).
  - non-ms (< 1e12): 0.
  - **reused**: 21,396 actions share (account, nonce) with another action, almost all in the same block (6,452 of 6,600
    keys). SQD gives the account (`user`), not the signing agent wallet, and HL rejects a reused nonce per signer. So
    these are several agent wallets of one account signing in the same ms. Their clocks may differ. This is flagged
    per row; calibration is per account.

**(c) Calibration error.** Split-half test: each signer's offset from even blocks minus its offset from odd blocks, over
330 signers with at least 5 fresh actions in each half. The median |difference| is 12 ms and p90 is 53 ms. This is the
error of the offset estimate only. A single send's latency jitter is on top of it (see the under-5 ms concordance).

## Files (`handoff/gate2_input/`)
- `blocks/<height>.csv` has one row per action, in listing order, with these columns:
  - block, block_time_ms, batch, class, priority_p, tier, listing_pos, pos_in_batch_class_tier
  - signer, nonce, lag_ms, type, n_items, assets, btc_eth, status
  - offset_ms, calib (signer / pooled / stale / future), send_cal_ms, send_minus_block_ms, flags (stale, future,
    reused, non_ms_nonce, no_nonce)
- `inventory.csv` has one row per block: n_actions, nonce_missing, n_batches, sets, dev_sets, holdout_only,
  explorer_txs, aligned, align_inversions, btc_eth_actions.
- `nonce_order.csv` holds the per-group taus (a). `signer_skew.csv` holds (b). `summary.json` holds the totals and (c).
  `nonce_resolution.json` holds concordance by gap.

## Limits (for the gate 2 write-up)
1. **Sample size:** 71 blocks in 35 chunks of 3 dev sets (dev_margin60 dominates: 54 of 71). They were chosen as
   contested-miss blocks and same-block-ahead placement blocks, not at random. Cluster by chunk.
2. **Nonce = signer clock, not send time.** Calibration error is 12 ms median and 53 ms p90 (offset only). Ordering is
   reliable only for gaps of 5-100 ms. 36% of actions (30,693) have no usable send time.
3. **Account, not signer:** agent wallets are not visible. Reused nonces show at least 2 agents per account for the
   top makers.
4. **Batches are inferred**, not observed (rank-drop rule).
5. Untracked users are included here (SQD covers all users), but only for actions inside these 71 blocks.

Free-quota note: R0 + R1 used 40.1 MB uncompressed (the conservative reading of the ~50 MB/month public quota). No pull
was needed tonight.
