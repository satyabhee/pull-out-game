# Gate 2 pre-registration: what cancels-first is worth to makers (ON = B3 vs OFF = M1n)

Written and committed BEFORE any gate-2 run (Satya's plan, Nov 26). Nothing below changes after the first OFF run;
any later change is logged under "Amendments" with its time and a reason, and results under both texts are reported.

## 0. The ONE primary result (A2, Satya)
**V_race under calibrated M1n** (OFF = `send_times_yuta95.csv`), 10 s maker markout, summed over the 71 listed blocks,
with its window (chunk) cluster bootstrap 95% CI (10,000 resamples, seed 20261127). This is the gate-2 headline and
the only number the reading rule in section 5 applies to.
- **Secondary (reported, not headline, not tested):** V_absorb and V_net from the same primary run.
- **Sensitivity rows (reported, not headline):** S1 stale / reused-nonce actions excluded; S2 raw-nonce send times
  (`send_times_raw.csv`); S3 L44 send times as first specified (`send_times.csv`). Each reports V_race, V_absorb,
  V_net with the same bootstrap. None of them replaces or qualifies the primary result's reading.

## 1. Question and arms
**Question.** On real blocks, how much do Hyperliquid's in-block cancels-first rule save the makers who were cancelling
a quote in the same block, and what does it cost the makers who then absorb the flow instead?

- **Key fact (L1, docs/l1_listing_order.md; RIKA R0 on SQD):** the stored block order IS the execution order, already
  class-sorted inside each batch. So "no cancels-first" cannot be built by replaying the stored order without the sort.
- **ON = B3** (FROZEN v5, docs/baseline.md), `--policy listing` (= execution order).
- **OFF = M1n** = B3 + `--order m1n --send-times handoff/gate2/send_times_yuta95.csv` (PRIMARY; see A1): inside each
  batch, txs in calibrated send-time order, no class sort. Batches = the file's `batch` column (Yuta's rank-drop cut on
  all actions; chain in-batch rank cancel < ALO < GTC/IOC < other, Yuta #55); a non-contiguous batch fails loudly.
  Timed txs of a batch are sorted among the slots they occupy; ties keep stored order (`engine_py/ordering.py`).
  Every other block is replayed exactly as ON.
- **Stale / reused-nonce actions (A1):** a tx with a stale, counter or future nonce has no row in the send-time file
  and keeps its listing position; in a class-sorted batch that position lies inside its own class's run, so it keeps
  its listing position within its class. A tx with a reused nonce (same account and nonce as another tx of the
  block: several agent wallets) is placed by its calibrated send time like any timed tx; ties keep stored order.
- Order flow is held fixed (no trader adapts to the other rule): this measures the rule's mechanical effect.

## 2. Blocks and chunks
- **Block list:** Yuta's `handoff/gate2/blocks.csv` (`chunk_dir,height`; Yuta #55): 71 SQD full blocks (every action's
  nonce known) in 35 chunks of dev_margin60 / devfast / dev20. sha256 09ade47007cced64... (full hashes of all four input
  files in A1; `analysis.gate2 run` also writes them to meta.json).
- Each listed block's chunk is replayed whole in both arms (lead-in included), so both arms carry identical state into
  the first listed block. **Check:** ON outputs on those chunks are byte-identical to B3's reference runs; on blocks
  not in the list OFF applies the identical order (policy stats `blocks_untouched`).

## 3. Measured objects (per listed block)
- **Fill:** every engine fill in a listed block, per arm: (chunk, height, taker tx hash, maker identity, maker user,
  maker side, px, sz, asset, block time). **Maker identity** = the placing action (height, tx hash, item index) for
  window orders; ("PH", engine oid) for placeholders (engine oids of pre-block orders are identical in both arms).
- **Raced orders R(block):** maker orders that a cancel / cancelByCloid / modify of their owner in the SAME block
  removed or targeted in either arm (recorded by the gate-2 observer, `analysis/gate2.py`).
- **Maker markout, 10 s (USD):** for a fill at px, size q: maker sold -> (px - mid10) * q; maker bought ->
  (mid10 - px) * q, where mid10 = (bid + ask) / 2 of the first recorded bbo (`bbo_<COIN>`) with time >= block time
  + 10,000 ms. No bbo within 60 s after that: the fill is dropped and counted. Also reported in bps of fill notional.
  Mids come from recorded data only, so they are identical in both arms.
- Sign convention: positive = good for the maker.

## 4. Estimands (sums over listed blocks, per chunk, then pooled)
- **V_race (PRIMARY, section 0)** = sum of 10 s markout of ON fills on raced orders - the same sum for OFF fills on raced orders.
  The value of cancels-first to cancelling makers (mostly: losses on stale quotes they escape under ON).
- **V_absorb (secondary)** = the same difference over fills on non-raced orders (makers who, under ON, absorb the taker flow that
  hits the stale quote under OFF; placeholders included and reported separately).
- **V_net (secondary)** = V_race + V_absorb (all makers together).
- Reported with each: n fills on each side (ON / OFF), n raced orders, n blocks with any difference, n chunks, total
  notional, and the per-fill mean in bps.

## 5. Statistics
- Unit of resampling: the chunk (window). Chunk-cluster bootstrap, 10,000 resamples, seed 20261127, percentile 95% CI,
  for V_race, V_absorb, V_net (and the bps means). Fills, orders and blocks inside one chunk are never treated as
  independent (statistical-analysis skill).
- **Sensitivity rows (reporting only, A1; not headline, section 0):** (S1) V_race / V_absorb / V_net recomputed excluding stale / reused-nonce
  actions: a fill is dropped from BOTH arms when its taker tx is untimed or reused-nonce, or its maker is a raced order
  whose racing cancel tx is untimed or reused-nonce (`analysis.gate2 score`, second block). (S2) OFF with
  `send_times_raw.csv` (raw nonce) and (S3) with `send_times.csv` (L44 as first specified), same blocks.
- Reading rule, fixed now, applied to the PRIMARY V_race only: V_race CI lower > 0 -> "cancels-first measurably protects cancelling makers on these blocks";
  CI covering 0 -> "not distinguishable from 0 at this n" (and we state the n and CI width; no claim of no effect).
  V_absorb and V_net are described, not tested. With fewer than 8 chunks with any difference, every CI is labelled
  "low power" in the results.

## 6. L61 mutation row (reporting)
Contested maker-strict accuracy ON vs OFF on the same chunks (contested tx set fixed by ON; `analysis.contested_paired`,
chunk bootstrap). Exposure = contested txs whose answer differs. **Killed** = the two-sided 95% CI excludes 0.
A survivor (exposure > 0, not killed) means the accuracy metric cannot see the rule on these blocks.

## 7. Synthetic checks (unit tests before any real run)
`tests/test_gate2.py` on hand-checkable blocks: (1) **dodged fill**: maker cancels and a taker hits the quote in the
same block; ON cancel first -> taker fills the next maker; OFF taker first -> stale fill; V_race > 0, V_absorb < 0 by
hand-computed amounts; (2) **absorbed fill**: same, checking the absorbing maker's amount; (3) **no-op**: no race in
the block -> ON fills == OFF fills, all V = 0. Plus `tests/test_ordering_m1n.py` for the ordering itself.

## 8. Known limits (stated before results)
- Coverage: only blocks with nonces (SQD free quota; ~70 blocks), so n is small and the blocks were fetched because
  they held contested misses (selected, not random): results describe those blocks, not all blocks.
- Send time = calibrated nonce. Calibration error (split-half offset) median 12 ms, p90 53 ms; nonce order matches
  chain order reliably only at 5-100 ms gaps, near coin flip under 5 ms, anti-informative over 100 ms (Yuta,
  docs/gate2_data.md). 36% of actions (30,693 of 85,928) have stale nonces (no send time); 21,396 share a reused nonce.
  Keyed by account (SQD has no agent signer).
- Batches are inferred (rank-drop rule), not observed. Cross-class order (what M1n changes) is never observed on chain,
  so the send-time order cannot be validated where it matters; within-class agreement is the only check.
- The replay itself is imperfect (B3 contested accuracy 59.4% on final220); V is the replay's estimate.
- One horizon (10 s), one mid source (bbo). L06 envelope (cancel allocation) only if time allows, reporting only.

## 9. Freeze
Results go to `docs/gate2_results.md`, frozen by Fri Nov 27 6 PM CT (Sat 00:00Z), then Yuta signs off.

## Amendments
**A1 (Nov 26 ~23:20Z, BEFORE any gate-2 run; Satya's instruction + Yuta #55 / docs/gate2_data.md).**
1. Stale / reused-nonce placement and sensitivity row S1, as in sections 1 and 5 (Satya).
2. Batches from Yuta's file instead of my class-descent rule: my rule ranked ALO before cancels, but the chain's
   in-batch order is cancel < ALO < GTC/IOC < other (Yuta #55), so my rule would cut inside real batches.
3. Primary send-time file = `send_times_yuta95.csv` (per-account calibration). Chosen on the only validation
   available, within-class weighted Kendall tau against chain order (no V outcome seen): yuta95 C 0.623 / A 0.481 /
   G 0.251; raw nonce 0.540 / 0.453 / 0.351; L44 as first specified 0.408 / 0.298 / 0.217 (worst in every class:
   it mixes calibrated and raw rows in one batch). yuta95 is best for cancels and ALO, the two sides of the race the
   class sort decides. Raw and L44 run as sensitivity rows S2 / S3.

**A2 (Nov 27 ~00:05Z, BEFORE any gate-2 run; Satya).** Section 0 added: one primary result (V_race, calibrated M1n,
window-cluster bootstrap CI); V_absorb / V_net secondary; S1-S3 sensitivity rows, reported but not headline.
Input sha256: blocks.csv 09ade47007cced643e0fc3f416be0b8b5a5450550562d937e976462e2f4528ee;
send_times_yuta95.csv 976a995cd56564d658d17ba8734b2011a07fb343b319462641d3726b5317c89a;
send_times_raw.csv a7d318d888da08c5247964595851f20d9dcd9091e6140a4a052bfa1cb8607414;
send_times.csv 744de38c37982422044c2853b2f0255a8255bdb4fc7fb9fa56bc5b17c4ed1c25.
