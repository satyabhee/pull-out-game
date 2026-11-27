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

**A3 (Nov 27 01:35Z; Satya's "Amendment A2" of Nov 27, numbered A3 because A2 above already holds section 0;
Yuta #59 / #63 / #64). Written BEFORE any gate-2 result was viewed:** the runs started 01:19Z; since then I have
read only their progress trace (elapsed seconds, memory) and a stage list. No results.pkl, score, out/ CSV or
result line has been opened or printed.
1. **State carry-over (#59 item 1), PRIMARY = option (a).** 18 of the 35 chunks hold more than one listed block, and
   after the first listed block OFF's book differs from ON's, so a later block's ON - OFF mixes its own reorder with
   carry-over. New PRIMARY run (`analysis.gate2 run --one-block`): ON is replayed once per chunk (it does not depend
   on which block OFF reorders); OFF is replayed once per listed block, with ONLY that block reordered (the send-time
   file filtered to that height, so every other block keeps the ON order), and only that block is scored. Both arms
   therefore enter every scored block with identical state. 71 blocks, 35 chunks, OFF = `send_times_yuta95.csv`,
   window-cluster bootstrap and everything else as in sections 0-5. Launched as soon as the current runs finish.
2. **FALLBACK (pre-registered): if the (a) run is not finished by Fri Nov 27 10:00 CT (16:00Z),** the PRIMARY is the
   FIRST listed block of each chunk (35 blocks) from the current yuta95 run (`analysis.gate2 score DIR
   --first-listed`). Before its first listed block OFF applies the identical order, so the state into that block is
   identical in both arms. If (a) finishes in time, this row is reported as a check of (a), not as a headline.
3. **Sensitivity rows S1-S3 = first-listed-block results (35 blocks) from the current runs:** S1 = the yuta95 run
   with stale / reused-nonce actions excluded; S2 = the raw-nonce run; S3 = the L44 run. All-listed-block results
   (71 blocks, carry-over included) from the current runs are reported only as a labelled appendix row, never as a
   headline or sensitivity row.
4. **L61 mutation row (section 6)** stays as committed: whole-chunk contested accuracy ON vs OFF from the current
   yuta95 run (all listed blocks reordered; carry-over is part of what the accuracy metric sees there).
5. **#59 item 2, raced set = union over arms, kept on purpose.** Under OFF a raced order that was already filled
   before its owner's cancel is no longer in the owner's book when the cancel runs, so only ON records it as raced.
   The union is what makes its OFF fill count as a race fill. Do not "fix" it to OFF-only or ON-only.
6. **#59 item 3, reporting added to `analysis.gate2 score`:** per category (race / absorb) and arm, the
   notional-weighted mean markout in bps (1e4 x sum markout / sum fill notional) and the ON - OFF difference in bps,
   with the same chunk bootstrap (numerator and denominator resampled together); and n chunks with any difference
   (already printed as "chunks changed"). Reporting only: the reading rule (section 5) still applies to V_race in USD.

**A4 (01:42Z Nov 27; still BEFORE any result was viewed). Observer bug; the 01:19Z runs are void.** A smoke test of
`--one-block` on one chunk failed in both arms: the gate-2 observer looked up the asset of every action in a listed
block, and `ScheduleCancel` has none, so any chunk with a scheduleCancel in a listed block failed in BOTH arms. The
01:19Z runs used that code (primary finished 01:38Z, S2 was stopped mid-run): their folders are renamed
`report/validate/gate2_{primary,s2_raw}_VOID_observer_bug` and never read. Fix: the lookup happens only for actions
with fills (`tests/test_gate2_a3.py`). Everything in A3 now refers to new runs on the fixed code: the all-listed
runs (yuta95, raw, L44), from which come the A3 fallback and S1-S3, then the A3 primary `--one-block` run. The
pre-registered design is unchanged.

**A5 (04:46Z Nov 27; EXTENSION; written BEFORE Yuta pulls any new block and before any extension run; Satya).**
Purpose: more race opportunities. **The A3 primary is unchanged.** A5 is reported in its own section of
docs/gate2_results.md, never pooled into, averaged with, or replacing the primary.
1. **Selection rule (outcome-blind: chain data only, no replay output, no V).** A block qualifies when, inside ONE
   batch (Yuta's rank-drop cut on the stored order, as in the `batch` column), there is (i) a cancel /
   cancelByCloid / modify whose target order rests at coin C, price P, side s, and (ii) a GTC / IOC order (place or
   modify) on coin C, opposite side, whose limit reaches P (buy px >= P against an ask, sell px <= P against a bid).
   The target's (C, P, s) comes from its placing action in the same chunk or from the capture's order-status files.
   A target that can't be resolved this way doesn't qualify the block. BTC / ETH only.
2. **Pool of chunks:** captured chunks of the dev sets and retired confirm sets only: dev20, devfast, dev_margin60,
   dev_alo10, confirm_margin60, confirm2_margin60, confirm_alo2_60, confirm_fast5_60. Excluded: final240 (and
   final220), confirm68, held / held66 / held67, the reserved post-change benchmark, new3 / new34 / newcov / post / tgt
   (status not checked tonight; can be added only by a later amendment before their blocks are pulled), every chunk
   that touches a gap in data/ingest_gaps.json, and the 71 primary blocks. No use of final240 for screening.
3. **Order and cap, fixed before any pull:** qualifying blocks are listed by ascending height (across all pool
   chunks). The first N are taken, where N is the cap from Yuta's SQD quota estimate, written with the full list
   to handoff/gate2_ext/blocks.csv (`chunk_dir,height`; sha256 logged in his mail) BEFORE any block is pulled.
   A listed block that SQD can't return in full (an action without nonce) is dropped and counted, never replaced.
4. **Same method:** code 10e8113, plus only fixes that are needed and logged here; send times by the same yuta95
   calibration (analysis/gate2_sendtimes.py), file sha256 logged before the run; `analysis.gate2 run
   --one-block` (one OFF replay per listed block); same estimator, 10 s markout, chunk-cluster bootstrap (10,000,
   seed 20261127), and the section 5 reading rule applied to the extension's own V_race.
5. **Cutoff: Fri Nov 27 9:00 AM CT (15:00Z)** (Satya's "Change to A5", which supersedes the 2 PM CT in his earlier
   message). If the extension isn't scored by then, docs/gate2_results.md reports it as "pending", with no partial
   numbers.

**A5.1 (06:06Z Nov 27; BEFORE any extension pull or outcome; Satya, answering Yuta #70).** Replaces A5 item 3's
"first N by height", which would put the first 200 qualifying blocks in ~4 chunks (all dev20, one day). The CIs are
window-cluster bootstrapped, so power comes from the number of chunks. Selection, in this order:
1. Pool chunks (A5 item 2) in ascending start height. From each chunk, take its first k qualifying blocks by height
   (A5 item 1; the 71 primary blocks excluded). k = 3.
2. Concatenate the per-chunk picks in chunk order, and stop when the list reaches the quota cap N (Yuta #70: N = 200).
   If the whole pool gives fewer than 200 blocks with k = 3, use k = 5 instead (same rule).
3. The list (handoff/gate2_ext/blocks.csv) is frozen, and its sha256 mailed, before any pull. Blocks are pulled in
   list order. Blocks that SQD refuses or can't return in full are reported as "not pulled" and never replaced, so the
   pulled set is a prefix of the frozen list.
Everything else in A5 is unchanged: same code, the yuta95 clock, one run per block, the same estimator, CIs and
reading rule, its own results section, and the cutoff of Fri 9:00 AM CT.
