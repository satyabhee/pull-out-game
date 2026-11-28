# Gate 2 results: what cancels-first is worth to makers on 71 real blocks (ON = B3 vs OFF = M1n)

Pre-registration: docs/gate2.md (sections 0-9, Amendments A1-A5). Code: `unified` 10e8113. Runs: 01:46-02:34Z Nov 27,
0 failed chunks in every run. Results first read at ~02:50Z Nov 27, after Satya's go (written 04:50Z).
Scoring guard: since the commit before this file, `analysis.gate2 score` writes an INVALID first line and exits 1
if any chunk failed (no change to any result). Every number below comes from runs with 0 failed chunks.
Independent recompute: Yuta, docs/gate2_crosscheck.md (data-streams 24cca7b / 6e64aeb), exact match in all 35 chunks.

## 1. Headline (the ONE primary result, docs/gate2.md section 0 + A3)

**V_race = -8.32 USD, 95% CI [-24.27, +0.13]** (window-cluster bootstrap over 35 chunks, 10,000 resamples, seed
20261127). Design: OFF = M1n with calibrated send times (`send_times_yuta95.csv`); one OFF replay per listed block,
with only that block reordered (A3 option (a)); 71 blocks in 35 chunks; 10 s maker markout against the recorded bbo
mid. Run: `report/validate/gate2_one_yuta95` (`analysis.gate2 run ... --one-block`, then `analysis.gate2 score`).

**Reading (frozen rule, section 5): the CI covers 0, so the result is "not distinguishable from 0 at this n".**
That is not a claim of no effect. The n: 23 of the 71 blocks differ between ON and OFF (16 of 35 chunks), and
V_race rests on 14 raced fills (1 under ON, 13 under OFF). The CI is 24.4 USD wide. 16 changed chunks >= 8, so the
"low power" label does not apply.

Sign: V_race < 0 means the makers who were cancelling did better WITHOUT cancels-first on these blocks. Under OFF
their quotes were hit before the cancel landed, and on average those fills were profitable to them at 10 s (OFF
raced fills: +1.11 bps notional-weighted). Under ON they escaped those fills. The point estimate goes against "cancels-first
protects cancelling makers", but the CI reaches +0.13, so the data don't distinguish it from 0.

## 2. Secondary (same run; described, not tested)

| estimand | USD | 95% CI (chunk bootstrap) | fills ON / OFF |
|---|---|---|---|
| **V_race (primary)** | **-8.32** | **[-24.27, +0.13]** | 1 / 13 |
| V_absorb | +17.79 | [-0.49, +53.35] | 227 / 217 |
| V_net = V_race + V_absorb | +9.47 | [-0.74, +29.32] | 228 / 230 |

- Fill notional ON 1,863,602 / OFF 1,863,809 USD; raced orders 10,115; fills dropped for lack of a mid: 0.
- Notional-weighted mean markout (reporting only, A3 item 6): absorb ON +1.469 / OFF +1.432 bps, ON - OFF
  +0.038 bps [-0.012, +0.314]. Race ON +2.531 / OFF +1.112 bps. The race ON mean rests on ONE fill, and its
  bootstrap interval ([+1.27, +1.53]) only covers resamples that contain that fill's chunk, so it is not a usable CI;
  it is shown for completeness only.

## 3. Sensitivity rows (reported, not headline; A3 item 3: first listed block of each chunk, 35 blocks)

| row | run | V_race | V_absorb | V_net | race fills ON / OFF |
|---|---|---|---|---|---|
| S1 stale / reused-nonce excluded (18 fills dropped) | all-listed yuta95 | +0.29 [-0.02, +0.82] | +1.12 [-0.76, +4.37] | +1.41 [-0.18, +4.46] | 0 / 3 |
| S2 raw nonce | all-listed raw | +0.30 [+0.00, +0.82] | -0.33 [-0.86, -0.01] | -0.03 [-0.12, +0.05] | 0 / 2 |
| S3 L44 send times | all-listed L44 | +0.30 [+0.00, +0.82] | -0.33 [-0.86, -0.01] | -0.03 [-0.12, +0.05] | 0 / 2 |
| (first-listed, calibrated, nothing excluded: the A3 fallback design, here a check) | all-listed yuta95 | +0.29 [-0.02, +0.82] | -0.33 [-0.86, -0.01] | -0.04 [-0.13, +0.04] | 0 / 3 |

The first listed blocks hold only 3 raced fills (OFF), and 9 of 35 blocks change, so these rows are small. Their
V_race is the opposite sign from the primary's: the primary's negative V_race comes from LATER listed blocks in
the same chunks. The reading rule does not apply to these rows. A lower bound printed as +0.00 is a rounding, not
a result.

**Appendix (labelled, never headline; A3 item 3):** all 71 listed blocks with carry-over, from the all-listed runs.
- V_race: yuta95 -8.32 [-24.27, +0.13]; raw -8.31 [-24.26, +0.14]; L44 -8.31 [-24.26, +0.14].
- V_net: +9.47 / +9.40 / +9.27.
- Calibrated, stale / reused excluded (59 fills dropped): V_race -3.69 [-10.36, +0.13], V_absorb +17.19 [-4.28, +54.30],
  V_net +13.50 [-4.78, +44.58] (the same as the primary run's exclusion pass).

The send-time file barely matters (V_net within 0.2 USD across the three clocks).

## 4. Why the primary equals the all-listed run (Yuta #69 point 2)

The A3 primary (one OFF replay per block) and the all-listed run (all 71 blocks reordered in one replay) give
identical V to 6 decimals. This is real, not a run that didn't happen: the OFF fills differ between the two designs
in exactly ONE block (1230572957). There, the same 0.30869 BTC @ 60908 is split differently between two makers
whose orders were placed in the same block 1230572946 (same price, side and block time), so the markout sum is the same.
Carry-over exists on this sample, but it is worth 0 USD.

## 5. The one ON race fill (Yuta #69 point 3)

n race fills ON = 1 is correct. Cancels-first holds per consensus BATCH, not per block. In block 1230828780 the
taker (action 1628) is in batch 0 and the owner's cancel (action 1681) is in batch 1, so even under the chain's
rule the taker ran first and the raced order filled.

## 6. L61 mutation row (section 6)

Contested maker-strict accuracy, ON vs OFF, on the 35 chunks (all-listed yuta95 run, whole chunks; contested set
fixed by ON; chunk bootstrap 10,000; `report/validate/gate2_all_yuta95/l61_contested.txt`):

| metric | n txs (chunks) | ON | OFF | OFF - ON, 95% CI |
|---|---|---|---|---|
| **maker-strict, contested** | 169 (35) | 54.6% | 51.0% | **-3.6 pt [-6.65, -1.14]** |
| maker-strict, contested (real makers) | 139 (31) | 61.8% | 57.4% | -4.4 pt [-8.28, -1.36] |
| maker-strict, all scored | 382 (35) | 67.9% | 65.5% | -2.4 pt [-4.45, -0.70] |
| maker-strict, uncontested | 213 (35) | 78.4% | 77.0% | -1.4 pt [-2.96, +0.00] |

**The mutation is KILLED** (the two-sided CI excludes 0): removing the class sort costs 3.6 pt of contested
accuracy, so the accuracy metric does see the in-batch rule on these blocks. (Recall rows: contested -3.5 pt
[-6.59, -0.98].)

## 7. Pre-registered checks

- **ON = B3:** in both the primary and the all-listed yuta95 run, the ON txs rows of all 35 chunks are identical to
  the B3 reference runs (`~/book-replay/report/validate/i21b2_{dev20,devfast,dev_margin60}/txs.csv`, compared per
  chunk; 35 / 35 identical).
- **Inputs:** blocks.csv 09ade47007cc..., send_times_yuta95 976a995cd565..., raw a7d318d888da..., L44 744de38c3798...
  (meta.json of every run; = A2).
- **Untimed / reused:** 60,512 listed-block txs; 16,660 untimed (stale / counter / future nonce: they keep their
  slot), 36,020 share an (account, nonce) with another tx of the block.
- **Synthetic checks:** tests/test_gate2.py (dodged, absorbed, no-op), tests/test_gate2_a3.py,
  tests/test_ordering_m1n.py; make check green on 10e8113 (661 passed).
- **Independent recompute** (Yuta): own bbo reader, markouts and grouping, with the engine's fills as given. Exact in
  all 35 chunks, for the primary and all three all-listed runs.

## 8. Void runs (A4)

The 01:19Z runs (`report/validate/gate2_{primary,s2_raw}_VOID_observer_bug`) are void. The observer looked up the
asset of every action in a listed block, and ScheduleCancel has none. 67 of the 71 listed blocks contain one, so 33 of
35 chunks failed in both arms (Yuta #66). The 2 chunks that survived had 0 fills, so their printed "V_race +0.00"
was an artifact, not a result: **failed run, not reported.** It was fixed in 10e8113 (test: a ScheduleCancel in a
listed block); everything above comes from the reruns on that code. The design is unchanged.

## 9. Limits (Yuta, docs/gate2_limits.md in data-streams 6e64aeb, pasted as written)

Numbers refer to the A3 primary, `report/validate/gate2_one_yuta95`, unless a line says otherwise.

**1. Sample size and selection.**
- 71 listed blocks in 35 chunks from 3 dev sets: dev_margin60 48, devfast 14, both 6, dev20 3. This is every block on
  disk with a full action list and nonces, from the SQD free quota (40 MB used).
- The blocks were fetched because they held contested misses or same-block-ahead placements. They are a selected
  sample, not a random draw, so the results describe these blocks, not Hyperliquid blocks in general.
- Only 23 blocks (16 chunks) differ at all between ON and OFF. Raced fills number 1 under ON and 13 under OFF, so
  V_race rests on 14 fills.
- The CIs resample chunks. The V_race CI [-24.27, +0.13] USD is wide relative to its point (-8.32). "Not
  distinguishable from 0" here is a statement about this n, not evidence of no effect.

**2. Nonces as send times (calibration error).**
- A nonce is the signer's clock at signing, not the moment the action left or arrived.
- The per-account offset is known to 12 ms median, 53 ms p90 (split-half). That is the offset only; per-send jitter
  comes on top.
- Nonce order matches chain order within a class only at 5-100 ms gaps. Under 5 ms it is near a coin flip
  (concordance 0.62-0.70); over 100 ms it is anti-informative (0.45-0.60).
- Many txs have no usable clock (stale / counter / future nonces): 40% of all aligned txs (65,521 of 163,665), and
  27.5% of the run's listed-block BTC/ETH txs (16,660 of 60,512). These keep their stored slot in OFF, so OFF is a
  partial reordering.
- **The comparison M1n actually makes, cancel vs taker across classes, can never be validated:** the chain always
  class-sorts, so that order is never observed. The calibration was chosen on within-class agreement only
  (docs/gate2_data.md).
- Sensitivity:
  - The send-time file barely matters (all-listed runs): V_net +9.47 / +9.40 / +9.27 USD for yuta95 / raw / L44.
  - Excluding stale and reused-nonce actions (primary run, score.txt) moves V_race to -3.69 [-10.36, +0.13] and V_net
    to +13.50 [-4.78, +44.58].

**3. Accounts, not signers.**
- SQD names the account (`user`), not the agent wallet that signed. 36,020 of 60,512 listed-block txs share an
  (account, nonce) pair with another tx in the same block, which means several agents per account with possibly
  different clocks.
- Calibration is per account.

**4. Untracked users and the replay itself.**
- V is the replay's estimate. Its fills come from B3, which names the right maker in 59.4% of contested trades on
  final220.
- The biggest known source of error is untracked users' orders that the chain rejected (margin, open-order cap) and
  we rest in the book. All-user truth would add about 2.9 pt contested on covered misses (ceiling about 5.7 pt
  [1.0, 11.2], RIKA R2).
- So fills on phantom orders can enter V on both arms.
- The 71 SQD blocks do give all-user outcomes for actions INSIDE them, but not for orders placed in earlier blocks.

**5. Definitions that move the split, not the net.**
- "Raced" is order-level: the order itself was removed by its owner's cancel / modify in that block.
- A broader user-level definition (the owner cancelled anything in the block) moves V_race from -8.32 to -23.03 and
  V_absorb from +17.79 to +32.50. V_net is unchanged by construction (docs/gate2_crosscheck.md).
- Cancels-first holds per consensus batch, not per block. A raced order can still fill under ON when its cancel is in
  a later batch (1 case: block 1230828780).
- Batches are inferred from class-rank drops; they are not labelled on chain.

**6. One horizon and one mid source; flow held fixed.**
- Markout is at 10 s, against the recorded bbo mid. It is unchanged when the recorder's receive clock is used instead
  of the exchange clock.
- No trader adapts to the other rule: this is the rule's mechanical effect on fixed order flow, not an equilibrium
  estimate.

**7. Carry-over (A3).**
- The primary reorders one block per replay.
- Versus the all-listed design, OFF fills differ in one block, and only in how the same size at the same price is
  split between two makers. So V is identical in both designs on this sample.

**Independent check.** A separate script with its own mids from the raw bbo files, its own markouts and grouping, and
the engine's fills taken as given reproduces V_race / V_absorb / V_net exactly in all 35 chunks for the primary and
all three all-listed runs (docs/gate2_crosscheck.md). The engine's fills themselves are not independently checked.

## 10. Extension (A5)

Its own result, **never pooled with section 1 and never replacing it** (A5). Design: A5 (9379175), A5.1 (5ad2c65),
A5.2 (de19e47 + 0239ed1), all committed before the rerun was scored.
- **Blocks:** 200 blocks frozen by Yuta before any pull (blocks.csv sha256 2d839a1a...; #74), all 200 pulled
  (#75). They come from 65 physical windows (confirm_margin60 131 blocks, dev20 60, devfast 9).
- **Clusters:** 65. Two windows exist as byte-identical copies in two sets and give 6 blocks each, at different
  heights (A5.2, Satya NS-18).
- **Clock:** send times = yuta95, fitted on the extension blocks (sha256 97d310d1...; 224,500 rows calibrated by
  signer, 2,388 by the pooled offset).
- **Method:** the same as section 1: one OFF replay per listed block, 10 s markout, chunk-cluster bootstrap 10,000,
  seed 20261127.
- **Run:** report/validate/gate2_ext_one_yuta95 on de19e47 (the 0239ed1 commit is doc-only). 65 ON + 200 OFF
  replays, **0 failed chunks**, score exit 0.
- **Void run:** the first run (07:29Z) is void, with the OFF arm failed in 65 / 65 chunks; it was not read (A5.2).

**V_race = -0.07 USD, 95% CI [-0.21, +0.00]** (exact upper bound 0.0).

**Reading (frozen rule, section 5, applied to the extension's own V_race): the CI covers 0, so the result is "not
distinguishable from 0 at this n".** No claim of no effect.
- **The n:** 16 of the 200 blocks differ between ON and OFF, in 13 of 65 chunks. 13 >= 8, so the "low power" label
  does not apply.
- **What V_race rests on:** **2 raced fills (0 under ON, 2 under OFF)**, one in each of 2 chunks (1227941615: -0.00;
  1230350863: -0.07). The CI is 0.21 USD wide. Its upper bound is exactly 0 because every resample that misses those
  2 chunks has V_race = 0.

**Race yield, against the A5 purpose.** A5 was meant to add race opportunities. Yuta's rough forecast (#72) was ~65
raced fills, from 0.33 raced fills per qualifying block measured on the primary blocks. The extension gave 2, from
6,906 raced orders (the primary had 14 raced fills from 10,115 raced orders on 71 blocks). The cause is not examined
here. The extension adds blocks, but not raced fills.

| quantity (extension, A5) | value | 95% CI |
|---|---|---|
| V_race (primary estimand, reading rule applies) | -0.07 USD | [-0.21, +0.00] |
| V_absorb (described, not tested) | -4.22 USD | [-12.70, +0.19] |
| V_net (described, not tested) | -4.29 USD | [-12.76, +0.06] |
| fills: race ON / OFF; absorb ON / OFF | 0 / 2; 310 / 304 | |
| notional ON / OFF | 2,209,715 / 2,199,252 USD | |
| absorb markout, notional-weighted (reporting only) | ON -1.504 / OFF -1.492 bps; ON - OFF -0.012 bps | [-0.015, +0.005] |
| race markout (reporting only) | ON n/a (no fills) / OFF +2.458 bps | |

- **V_absorb is concentrated:** one chunk (1227768025-1227768234) carries -4.13 of the -4.22 USD.
- **Sensitivity (stale / reused-nonce actions excluded, as S1; 100 fills excluded):**
  - V_race -0.00 [-0.00, +0.00] on 1 OFF raced fill.
  - V_absorb +0.07 [-0.43, +0.59].
  - V_net +0.07 [-0.43, +0.59].
- **Scale:** listed-block txs 103,552, of which 28,709 are untimed (stale / counter / future nonce) and 60,095 carry a
  reused nonce.
- **Sign:** V_race is negative in both section 1 and here, as in section 1's sign note. Neither CI excludes 0, and the
  two are not combined.

Sign-off for this section (Yuta): pending; his crosscheck (analysis/gate2_crosscheck.py, grouped by window name).

## 11. Freeze and sign-off

Frozen at the commit that adds this file, apart from section 10 (extension) and the sign-off line. Section 10 added
after the extension rerun was scored (08:15Z Nov 27).
Sign-off (Yuta, 05:40Z Nov 27): sections 1-9 checked against the run files (score.txt, score_first.txt, l61_contested.txt, meta.json) and the independent recompute; numbers correct. Section 10 (A5) is signed separately when it is scored.
(Pasted from Yuta #71. He checked the working-tree file sha256 3d9cd4c924f2...; the committed d2fb954 also has the
scoring-guard line under the header, added at Satya's request. Text only; Yuta to confirm that diff.)

**Erratum (07:46Z Nov 27, Yuta #71 / #74; text only, no result changes).** Section 9 point 1, "from the SQD free
quota (40 MB used)", should read: "from the SQD free quota (these 71 blocks are 42.4 MB uncompressed; all of this
month's SQD queries total 46.7 MB uncompressed, 6.8 MB compressed)". Section 9 is left as frozen; data-streams
docs/gate2_limits.md has the fix.

## 12. Across policies (A6)

These rows are descriptive, with no new headline; M1n remains the primary counterfactual (section 1). The A6 brief
and docs/gate2.md A6 name this "section 11", but section 11 was already the frozen "Freeze and sign-off", so A6 goes
here instead. That is the only change from A6.

**Run.**
- Code: unified 763d46b (`--order cancels_last|random`, `--order-seed`; runner analysis/gate2_policies.py).
- Same A3 primary blocks (71 blocks, 35 chunks), B3 flags, yuta95 clock and one-block mode. The estimator,
  bootstrap (10,000, seed 20261127) and sensitivity row are unchanged.
- 1,597 replays (35 ON + 22 × 71 OFF), 21:53Z-01:22Z, exit 0, 0 failed. All 20 random seeds are complete, so the
  partial rule was not needed.
- Output: report/validate/gate2_a6/{score_a6.txt, summary_a6.json, <policy>/score.txt}.
- Rerun: `python -m analysis.gate2_policies score report/validate/gate2_a6 --primary report/validate/gate2_one_yuta95`.

**M1n reproduction check (A6, before reading anything): IDENTICAL.** The M1n arm rerun in the A6 runner matches
gate2_one_yuta95/score.txt line for line, apart from the path line.

**Comparison** (USD, OFF − ON; 95% CI from the chunk bootstrap; primary rows, stale/reused-nonce actions included).
The contested column is the L61-style maker-strict contested accuracy, OFF − ON, on the **listed blocks only**
(71 txs, 29 chunks). That is a narrower scope than section 6's −3.6 pt, which covers all listed blocks of whole chunks
(gate2_all_yuta95), so the two are not the same row.

| policy | V_race | V_absorb | V_net | raced fills OFF | changed chunks | contested OFF − ON |
|---|---|---|---|---|---|---|
| ON (B3 listing; reference) | 0 | 0 | 0 | 1 (ON) | — | 34.0% (ON level) |
| M1n (primary) | −8.32 [−24.27, +0.13] | +17.79 [−0.49, +53.35] | +9.47 [−0.74, +29.32] | 13 | 16 | −7.8 pt [−14.11, −2.27] |
| cancels-last | −8.32 [−24.27, +0.13] | +17.56 [−0.70, +53.01] | +9.23 [−0.96, +28.98] | 13 | 16 | −6.4 pt [−12.98, −1.72] |
| random, mean of 20 seeds | −10.81 [−32.19, +0.15] | +17.23 [−0.16, +51.38] | +6.41 [−0.04, +19.20] | 11.1 (8-15) | 10-16 | −7.1 pt (−9.9 to −3.4) |

For the random row, each CI is the chunk bootstrap of the per-chunk seed mean. The spread across the 20 seeds
(point estimates) is:
- V_race: min −15.60, max −3.92, SD 3.37.
- V_absorb: min +4.92, max +19.63, SD 2.97.
- V_net: min +0.99, max +10.01, SD 3.42.

Per-seed rows, each with its own CI, are in score_a6.txt.

**Sensitivity row** (stale/reused-nonce actions excluded; same reading as section 3, not a headline), V_net:
- M1n: +13.50 [−4.78, +44.58].
- Cancels-last: +13.27 [−4.99, +44.24].
- Random: mean +14.29 (min +3.44, max +16.59, SD 2.70). V_race mean −2.33 (−3.84 to −0.11).

**What the rows show (described, not tested):**
- **Cancels-last is almost the same as M1n.** V_race and the raced fills are identical, and V_absorb differs by
  0.23 USD. On these blocks the race outcome does not depend on whether the cancel or the GTC/IOC comes first by
  class, given the same batches and movable set.
- **Every policy has the same sign pattern as M1n.** V_race is ≤ 0 and V_absorb is > 0.
  - Like M1n, every V_race CI includes 0 (upper ends +0.01 to +0.52).
  - V_net lower ends run from −0.96 to +0.02. Five random seeds have a lower end of +0.01 or +0.02. That is a single
    chunk at one ON race fill, so it is not read as a separation from 0.
- **The seed spread comes almost entirely from one chunk, 1230828573-1230828782.**
  - That chunk's per-seed SD of V_net is 3.31 USD, against 3.42 for the total. The next largest chunk SD is 0.05.
  - The seeds fall into two groups of 10: V_net +8.4 to +10.0 and +1.0 to +4.2. The group depends on which race that
    chunk's shuffle produces (V_race in that chunk −7.5 to −8.9 vs −13.0 to −15.3).
  - Seed 1 also differs in V_absorb (+3.98 in that chunk vs about +17 elsewhere).
  - Seed 1's score.txt also counts 0 ON raced fills, where every other policy counts 1. ON is the same 35 replays for
    every policy, so the difference comes from the race classification, which is per pair of arms. Not investigated.
  - So the random mean is a mix of two outcomes for one chunk, not a smooth average.
- **The contested accuracy falls under every policy** (−3.4 to −9.9 pt). This is consistent with section 6: reordering
  away from ON makes the replay match real fills less well. Per-seed CIs include 0 for 7 of the 20 seeds.

**Limits.** These are the same as sections 1 and 9 (n = 35 chunks, one ON race fill, wide CIs). Random is a
within-batch shuffle with the M1n batches and movable set, not a model of any real sequencer.
