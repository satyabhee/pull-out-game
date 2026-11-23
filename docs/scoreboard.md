# Scoreboard (MAHITO_PLAN §00)

One row per attempt, failed ones included. Every rule below was written BEFORE its item's run (git history of this
file shows the order). Metric: per-tx maker-strict (matched notional with the right maker / real notional), mean
over txs, split by contested (one of the tx's makers, real or engine, sent a cancel / modify / ALO on that asset in
that block; `analysis/contested.py`). CIs: paired, chunk-cluster bootstrap 4000 (`analysis/contested_paired.py`).

**Current baseline (B0)**: real engine on `mahito`, Rust backend, block-end correction, `--p-priority docs`,
`--learn-evidence tx`, order=proposer, stp=taker, lead-in 150 (as captured). Reference outputs
`report/validate/m1_ref_blockend20` (Python engine; Rust identical).

**Default adoption rule (§00 rule 2)**: adopt iff on dev20 (paired vs the current baseline) the contested diff's 95% CI
lower bound is >= 0 AND the uncontested diff's 95% CI upper bound is >= -1.0 point (i.e. a drop of more than 1 point
is not established). dev-fast is reported next to dev20 but does not decide (until Satya says otherwise).

## Attempts

| date | item | switch | rule | dev20 contested (n 113) | dev20 uncontested (n 195) | dev-fast contested / uncontested | adopted | commit |
|---|---|---|---|---|---|---|---|---|
| Nov 23 | B0 baseline | (definition above) | - | 0.587 | 0.713 | **0.442 (n 50) / 0.694 (n 83)** | baseline | 406e25a |
| Nov 23 | A0 re-triage | - | analysis only | 59 contested misses classified (below) | | | n/a | this session |
| Nov 23 | A1 dev-fast | - | analysis only | | | B0 scored (row above); 20 chunks, `docs/devfast_chunks.txt` | n/a | this session |
| Nov 23 | A2 lead-in 300 / 600 | - | default rule | **not run: no blocks before the 150-block lead-in (QUESTIONS Q10)** | | | n | - |

## Rules written before each item (Nov 23 session)

### A0 · Re-triage of contested misses (analysis only: nothing can be adopted)
Scope: dev20, baseline B0, scored contested txs with maker-strict < 1. Missed notional of a tx = real notional -
strict-matched notional. Each miss gets ONE cause, from the first point where its real fills and the baseline's
fills diverge (real fill r = maker M at price p; engine fill e at the same position, if any), checked in this order:
1. **ordering inside a block**: from the same pre-block book, whole-block class sort reproduces the tx strictly
   (`analysis/order_audit.py`, `report/validate/order_audit_dev20`).
2. **placeholder maker**: e was filled against an anonymous snapshot placeholder.
3. **margin-rejected IOC / ALO we filled**: e's maker order is a window ALO whose chain status is a rejection
   (stream / probe truth), OR M's order at p was consumed earlier by one of our fills for a taker tx that printed no
   trades (the chain rejected or no-filled that IOC).
4. **phantom order**: e's maker order is a window order the chain had already removed (truth says canceled / filled
   / rejected before this tx) and M has no order at p in our book.
5. **lost order / wrong cancel target**: M placed an order at p in the window that is no longer in our book at this
   tx and was not consumed by our fills (removed by a cancel we resolved, a correction, a reconcile or an eviction).
6. **queue order at the right price**: M has an order at p in our book, but our queue at p fills others first (or
   the same makers with different sizes).
7. **other**: everything else (incl. M never placed at p in the window and no placeholder there: pre-window liquidity
   we do not hold).
Report per cause: miss count, share of missed contested notional, 3 examples. A3-A7 are then ordered by the
share of missed contested notional each one targets: A3 = cause 3 (IOC part), A4 = 2 (+ the pre-window part of 7),
A5 = 5, A6 = 3 (ALO part) and 4, A7 = reduce-only (inside 4 / 7 if identifiable). L5 (M2) gate: share of 2 + 6
(several hypotheses at once: ambiguous queue splits / cancel targets) >= 30%.

### A1 · dev-fast (a new development set; analysis only: scoring the baseline adopts nothing)
Selection, fixed before looking at any score: captured chunks with first block time >= Nov 21 23:11 UTC (the fast
5-level feed starts 22:56; +15 min so the replay's 15-min recorder look-back is inside it), status complete,
lead-in 150, not touching an ingest gap (`find_chunks` guard), not in any existing set (dev20, confirm68, final240,
held, held66, held67, new3, new34, newcov, post, tgt), and last block time <= newest recorder data - 10 min. From
the eligible list sorted by start height, take 20 evenly spaced: index round(i * (N - 1) / 19), i = 0..19. Folder
`~/hl-data/mac3/devfast`. Loader: `data.parse.iter_fast` (5 levels: px, sz and order count n per level). Report the
baseline's contested / uncontested on it as a B0 row.

### A2 · Lead-in 300 and 600 blocks
Rule: the default rule, for each length vs B0 (lead-in 150) on dev20. If both pass, adopt the larger point estimate,
unless the two are within 0.5 points: then the shorter one (cheaper). Runtime cost reported (median of runs,
ingest running). Status before running: see QUESTIONS.md Q10 (dev20 has no blocks before its 150-block lead-in).

## Results

### A0 (dev20, baseline B0; `python -m analysis.retriage ~/hl-data/mac3/dev20 --hl-data ~/hl-data/mac3/hl_data --audit report/validate/order_audit_dev20 --out report/validate/a0_retriage_dev20`)
59 contested misses (= the reference run's 59), 2.28 M USD missed notional. Log and examples:
`handoff/a0/dev20.log`, `handoff/a0/breakdown.txt`, rows `report/validate/a0_retriage_dev20/misses.csv`.

| cause | misses | share of misses | share of missed notional | excl. the one 1.37 M USD tx |
|---|---|---|---|---|
| ordering inside a block | 0 | 0% | 0.0% | 0.0% |
| placeholder maker | 8 | 14% | 16.1% | 40.3% |
| margin-rejected IOC / ALO we filled | 1 | 2% | **60.1%** | 0.0% |
| phantom order | 0 | 0% | 0.0% | 0.0% |
| lost order / wrong cancel target | 14 | 24% | 1.1% | 2.8% |
| queue order at the right price | 26 | **44%** | 19.3% | **48.4%** |
| other (M never placed at p in the window, no placeholder fill) | 10 | 17% | 3.4% | 8.5% |

3 examples per cause (block, tx, missed USD; more in breakdown.txt):
- placeholder: 1227286735 0xe7e56b9b 314k (real maker 0x78dce962 at 62795.0; we filled a placeholder at 62796.0);
  1227941786 0x6b7b42c9 30k; 1227801425 0x8c4a8801 7k.
- margin-rejected IOC: 1227973673 0xc82ce3dd 1,370k (a 30-BTC sweep; the real maker 0x721ed8f9's order at 61616.0 was
  already consumed in our replay by an IOC that printed no trade on chain).
- lost order: 1227701231 0x10525d7e 9k (2 window orders of 0x2cb56238 at 61519.0, both gone from our book, none
  filled by us); 1227701227 0x47c42565 6k; 1227701198 0x92827dd8 2k.
- queue order: 1227701244 0x5a9998a0 291k (real maker 0x0e8836d1 rests at 61507.0 in our book; we filled 0x50d59f4c
  at 61509.0 first); 1227159780 0x3766f096 50k; 1227159780 0xe609d6c1 34k.
- other: 1227836325 0xbe3a5439 35k; 1227768212 0x14102769 25k; 1227429869 0x6d11b010 16k.

**Order of A3-A7 by the written rule (share of missed contested notional):** A3 IOC margin (60.1%) > A4 placeholders
(16.1% + "other" 3.4% = 19.5%) > A5 lost orders (1.1%) > A6 (0%) > A7 (0%, none identified). This ranking rests on
ONE tx (60% of the notional). Without it: A4 (48.8%) > A5 (2.8%) > A3 (0%). The largest cause by count and by
notional without the outlier, **queue order at the right price (44% of misses), is not targeted by any A item** in the
plan (QUESTIONS Q11). L5 gate quantity (placeholder + queue): 35.4% of missed notional (>= 30%), but the plan
evaluates the gate only after A2-A4 have landed.
