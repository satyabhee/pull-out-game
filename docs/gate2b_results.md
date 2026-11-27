# Gate 2b results: what cancels-first is worth, at scale

The pre-registration is docs/gate2b.md (commit 7bf5d9c, 08:29Z). The run was on unified 19dd4f3: it started at 08:37Z
and was scored at 12:08Z. Every number below is an **estimate**: the pre-registration sets no significance gate. The
assumptions in section 5 belong next to every headline number. Gate 2 (docs/gate2.md, docs/gate2_results.md) is
untouched.

Reproduce:

    uv run python -m analysis.gate2b run --out report/validate/gate2b --jobs 6 --stop-at 13:15 \
        --hl-data ~/hl-data/mac3/hl_data <B3 flags, handoff/i27/baseline.txt line 2>
    uv run python -m analysis.gate2b score report/validate/gate2b        # -> score.txt, conflicts.jsonl
    uv run python -m analysis.gate2b_diag report/validate/gate2b         # section 4 (post-hoc, descriptive)

## 0. Headline

| per the pre-reg | total over scored blocks | per day (scale-up, section 5) | per block |
|---|---|---|---|
| **UPPER** W_up (P = 1, takers reach the order with no queue ahead) | 392,380 USD [296,345, 501,838] | 3.66 M USD [2.76 M, 4.68 M] | 3.45 USD [2.60, 4.40] |
| **ESTIMATE** W_est (P = P_hat, queue reach from the B3 book) | 19,515 USD [9,634, 33,687] | 182,130 USD [89,962, 314,191] | 0.17 USD [0.08, 0.30] |

These are 95% window-cluster bootstrap CIs (10,000 resamples, seed 20261127). The resampling is joint, so W_est's CI
includes the uncertainty in P_hat.

- **P_hat** = the share of conflict pairs where the taker was sent first (ties count 0.5) = **0.85 [0.76, 0.99]**.
  It comes from 1,788 timed (cancel, taker) pairs in 2,096 conflicts at SQD-timed heights. 650 pairs were untimed and
  excluded.
- **The estimate is about 5% of the upper bound.** Queue reach is what cuts it: only 2,587 of 474,805 conflicts have
  takers whose size gets past the queue ahead of the cancelled order.
- **Volatility:** the top trailing-10 s volatility decile holds 0.34 [0.20, 0.47] of W_up and 0.26 [0.11, 0.51] of
  W_est. The top 1% holds 0.02 [0.00, 0.06] of W_up and 0.01 [0.00, 0.03] of W_est.

## 1. Population and coverage

- **Windows:**
  - 2,323 captured.
  - 16 excluded because they touch an ingest gap (listed in meta.json).
  - 2,307 run, 2,294 ok.
  - **13 failed** (0.56%, under the 5% "partial" line), so the headline is **not partial**.
    - 12 have no window.json: 1226403166, 1226646869, 1226766950, 1226882277, 1226905715, 1226923808, 1226967082,
      1227007939, 1229675191, 1229814022, 1230221044, 1235715152 (start heights).
    - 1 has no blocks: 1227467988-1227468197.
  - The run finished before the 13:15Z stop, so it is the full population, not a random subset.
- **Scored:** 113,883 blocks, which is 2.57 h of chain time (0.107 days). Warm-up blocks are replayed but not scored.
- **Raw txs:**
  - No raw block files were missing.
  - Every parsed hash was found in the raw txs.
- **Conflicts:**
  - 474,805 in total.
  - 469 have no mid, so they get no value.
  - By coin: BTC 397,630, ETH 77,175.
- **Cancel-type actions with crossing same-batch takers from another user:** 15,810,145 in total.
  - 15,335,340 of them removed no crossing order of that user: the cancel hit a different order, or missed.
  - Section 3 of the pre-reg counts only cancels that the B3 book resolves to a crossing resting order.

## 2. Batches (P5) against SQD batch ids

- **SQD-timed heights that are scored here: 67 of 271.** The other 204 lie in the warm-up of their window. Gate 2b
  replays whole windows and scores from warmup_end_height (pre-reg section 1). Gate 2 replayed set chunks with their
  own lead-in, so its blocks were scored there.
- **This means P_hat comes from those 67 heights,** not from all 271 that the pre-reg had in view. Section 4 of the
  pre-reg didn't foresee the warm-up overlap. I didn't change the population to get the other heights back.
- **(a) Tx-pair same-batch agreement:** 66,952,758 / 100,086,265 = **0.669**.
  - As the pre-reg expected, the P5 cut splits batches that SQD keeps together. It can't merge two batches.
  - So the explorer batches are finer than the SQD batches. Conflicts are found only inside the finer batches, which
    makes the scan conservative in how many conflicts it finds.
- **(b) Conflict pairs also in one SQD batch:** 1,788 / 1,788 = 1.000.
  - The denominator is the timed pairs, the ones with an SQD row for both txs.
  - Every conflict the P5 cut finds is also a same-batch conflict under SQD.

## 3. By trailing 10 s volatility decile

Deciles are cut per coin over the conflicts. Decile 10 holds conflicts with fewer than 2 mids in the trailing 10 s.

| decile | n | W_up USD [95% CI] | W_est USD [95% CI] |
|---|---|---|---|
| 0 | 47,419 | 11,321 [5,312, 19,923] | 232 [94, 416] |
| 1 | 47,462 | 14,352 [6,138, 28,531] | 180 [62, 368] |
| 2 | 47,382 | 12,457 [8,475, 17,291] | 93 [47, 154] |
| 3 | 47,276 | 14,725 [8,392, 22,536] | 400 [121, 834] |
| 4 | 47,565 | 19,663 [11,056, 30,642] | 340 [123, 638] |
| 5 | 47,535 | 24,905 [14,592, 36,781] | 1,184 [423, 2,266] |
| 6 | 47,335 | 40,990 [22,815, 64,091] | 3,877 [242, 12,329] |
| 7 | 47,384 | 31,428 [20,639, 44,321] | 1,100 [361, 2,236] |
| 8 | 47,578 | 89,083 [44,527, 142,637] | 6,953 [1,376, 16,706] |
| 9 | 47,541 | 133,367 [65,621, 217,461] | 5,156 [2,153, 9,409] |
| < 2 mids | 328 | 90 [0, 271] | 0 [0, 0] |

**Decile cuts (bp):**
- BTC: 0.94, 1.34, 1.69, 2.07, 2.52, 2.97, 3.47, 4.15, 5.36; top 1% ≥ 14.91.
- ETH: 1.1, 1.59, 2.05, 2.49, 2.87, 3.54, 4.29, 5.25, 7.29; top 1% ≥ 17.44.

W rises with volatility, mostly in deciles 8–9 (H3's direction). The top 1% is small because it holds about 1% of
conflicts and its value is not outsized.

## 4. Concentration (post-hoc, descriptive; not pre-registered)

These come from analysis/gate2b_diag.py. They read the CIs above; they don't change them.

- **W_est is concentrated:**
  - The top 10 conflicts hold 37% of it, the top 100 hold 70%, and the top 1,000 hold 98.8%.
  - The top 10 windows hold 71%, and the top 50 hold 93%.
  - 720 of the 2,264 windows with a conflict have any W_est.
  - The top 10 of 561 maker addresses hold 50%.
- **W_up is spread out:** the top 10 windows hold 34%, and 219,965 conflicts have a value.
- **Reading:** W_est rests on a few hundred large conflicts in a few dozen windows. With about 10 windows carrying most
  of it, a percentile bootstrap may understate the upper tail of W_est. Treat its CI as indicative.

## 5. Assumptions (pre-reg section 7, stated with the result)

- **Queue reach (ESTIMATE):**
  - The takers' full sizes go to this level.
  - Queue ahead is taken from the B3 book, which has known drift and anonymous placeholder depth.
  - Other cancels ahead in the same batch aren't netted out, so queue ahead is overstated and W_est leans low on this
    count.
- **Upper bound:**
  - Each conflict gets min(r, Q) independently.
  - The same taker size is counted against every crossing maker order it could reach (for example, two cancelled bids
    at 9 and 51 lots of queue ahead each get the full taker size).
  - W_up is a bound by construction, not a plausible value.
- **P:**
  - One P_hat (0.85) is applied to every conflict.
  - It is measured on 67 SQD-timed heights (section 2), using the yuta95 clock, and is assumed to carry over to all
    windows.
- **Order flow is fixed.** No maker or taker reacts to the rule, so this is the mechanical value of the ordering.
- **Markout:** L21, mid-to-mid at 10 s, adverse side only (max(0, −mo)). It is the maker's adverse move, not a
  realised P&L.
- **Coverage:** only conflicts whose cancelled order the B3 book resolves (section 1).
- **Per day:** W / 2.57 scored hours × 24. The windows were not captured at random times, so this is a scale-up of the
  captured windows, not a calendar average. Per block is the more direct number.

## 6. Files

- report/validate/gate2b/:
  - score.txt;
  - meta.json (argv, exclusions, send_times sha256);
  - conflicts.jsonl (one row per conflict);
  - windows/<w>.json (per-window rows, stats, SQD batch maps).
- These outputs are not committed.
