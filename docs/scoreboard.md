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
| Nov 23 | B0 baseline | (definition above) | - | 0.587 | 0.713 | (A1) | baseline | 406e25a |

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
