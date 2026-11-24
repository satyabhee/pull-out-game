# Scoreboard (MAHITO_PLAN §00)

One row per attempt, failed ones included. Every rule below was written BEFORE its item's run (git history of this
file shows the order). Metric: per-tx maker-strict (matched notional with the right maker / real notional), mean
over txs, split by contested (one of the tx's makers, real or engine, sent a cancel / modify / ALO on that asset in
that block; `analysis/contested.py`). CIs: paired, chunk-cluster bootstrap 4000 (`analysis/contested_paired.py`).

**Current baseline: B1 = B0 + `--taker-reject margin` (Nov 23; docs/baseline.md).** B0 was: real engine on `mahito`, Rust backend, block-end correction, `--p-priority docs`,
`--learn-evidence tx`, order=proposer, stp=taker, lead-in 150 (as captured). Reference outputs
`report/validate/m1_ref_blockend20` (Python engine; Rust identical).

**Default adoption rule (§00 rule 2)**: adopt iff on dev20 (paired vs the current baseline) the contested diff's 95% CI
lower bound is >= 0 AND the uncontested diff's 95% CI upper bound is >= -1.0 point (i.e. a drop of more than 1 point
is not established). dev-fast is reported next to dev20 but does not decide (until Satya says otherwise).

**Adoption rule since Nov 23 (Satya; written before any A8 / A4 run): pass the dev20 rule above AND dev-fast
contested must not drop (paired 95% CI upper bound >= 0).** The baseline itself is defined in `docs/baseline.md`
(checked before every A run; both sessions read it).

**Adoption rule since Nov 23, session 5 (Satya, Q15; written before the next run, replaces the two above): adopt iff
dev20 contested point estimate > 0 AND its 95% CI lower bound >= 0 AND dev-fast contested 95% CI upper bound >= 0
(the dev20 uncontested clause stays: upper bound >= -1.0). For owner-naming switches, ALSO naming precision >= 70% on
both dev20 and dev-fast (engine fills on named pieces whose named owner is the real maker at that price / all engine
fills on named pieces).** Reason (Satya): a wrong owner name biases per-maker results in the IE 498 study, and
maker-strict does not penalise it.

**Data-gated switches (Satya, Nov 23; tightened in the overnight instructions):** for a switch that needs data only some chunks have (margin snapshots, the post-only margin snapshots, the 5-level feed, ...), the dev20 "point > 0" requirement is replaced by: (1) on the sets where the switch cannot act, byte-identical output, OR every differing tx traced to a case where its data exists, plus contested not worse; AND (2) a passing pre-registered confirm on chunks where it can act (contested point estimate > 0, Holm p < .05). All other switches keep the default rule. Where a set CAN act (e.g. dev-fast for `--taker-reject margin`), it is not part of (i); it is
reported, and the dev-fast clause (upper bound >= 0) still applies.

## Attempts

| date | item | switch | rule | dev20 contested (n 113) | dev20 uncontested (n 195) | dev-fast contested / uncontested | adopted | commit |
|---|---|---|---|---|---|---|---|---|
| Nov 23 | B0 baseline | (definition above) | - | 0.587 | 0.713 | **0.442 (n 50) / 0.694 (n 83)** | baseline | 406e25a |
| Nov 23 | A0 re-triage | - | analysis only | 59 contested misses classified (below) | | | n/a | this session |
| Nov 23 | A1 dev-fast | - | analysis only | | | B0 scored (row above); 20 chunks, `docs/devfast_chunks.txt` | n/a | this session |
| Nov 23 | A2 lead-in 300 / 600 | - | default rule | **not run: no blocks before the 150-block lead-in (QUESTIONS Q10)** | | | n | - |
| Nov 23 | A8 queue order (26 misses split) | none run | sub-cause rule | no rule-fixable sub-cause supported by the chain's fills (below) | | | n/a | this session |
| Nov 23 | A4(a) placeholder split | `--placeholder-split` | rule since Nov 23 | +0.0 [0.0, 0.0] (all txs byte-identical) | +0.0 [0.0, 0.0] | +0.0 [0.0, 0.0] / +0.0 [0.0, 0.0] | **n (formally passes; held, QUESTIONS Q13)** | this session |
| Nov 23 | A4(b), A4(c) | - | - | not built in session 3 (Q13); (b) built in session 4 (rows below); (c) not built | | | n | - |
| Nov 23 | B0 + taker margin gate (data-streams) | `--taker-reject margin` | rule since Nov 23 | +0.0 [0.0, 0.0] (byte-identical: no margin snapshots) | +0.0 [0.0, 0.0] | **+2.9 [+0.0, +8.4] / +3.4 [-1.5, +8.3]** (0.442 -> 0.471 / 0.694 -> 0.728; all scored +3.2 [-0.4, +7.4]; engine-only txs 1633 -> 738) | **y -> B1** (Rust parity: byte-identical, dev20 + dev-fast) | this session |
| Nov 23 | A4(b1) name pieces from trades (vs B1) | `--placeholder-split --trade-learn sweep,queue,name` | rule since Nov 23 | +0.5 [+0.0, +1.9] | +3.0 [+0.0, +6.3] | +0.0 [0.0, 0.0] / -1.1 [-7.2, +3.4] (B1 set: n 49 / 84) | **n: formally passes; held for Satya (QUESTIONS Q15)** | this session |
| Nov 23 | A4(b2) + name from cancels (vs B1) | (b1) + `--named-cancel` | rule since Nov 23 | identical to (b1) on every tx | identical | identical | n (same as b1; Q15) | this session |
| Nov 23 | A5 Part 1 oid counting test | - | A5 rule (ce533a8) | **no scope holds**: best = V0 same block, 89.1% of 312,162 pairs r = 0 (bar 99%) | | | n/a: Part 2 (resolver) not built, per the rule | this session |
| Nov 23 | L5-lite Multiplicity-lite (vs B1) | `--multi-lite` | ML rule (c9c595c) + 70% naming bar | +0.5 [+0.0, +1.9] | +3.0 [+0.0, +6.3] | +0.0 [0, 0] / -1.1 [-7.2, +3.4]; naming precision 54% (dev20) / 35% (dev-fast) | **n: naming precision < 70% on both** | this session |
| Nov 23 | Fallback 3a naming strictness (vs B1) | `--placeholder-split --trade-learn sweep,queue,name --name-strict N1/N2/N3` | 3a rule (d6ca88c) | N1 +0.5 [+0.0, +1.9] (54%); N2 +0.5 [+0.0, +1.9] (53%); N3 +0.0 (names nothing) | N1/N2 +3.0 [+0.0, +6.3]; N3 +0.0 | not run (no level reaches 70% on dev20) | **n: rule picks N3, which has no named fills and no contested gain** | this session |
| Nov 23 | Fallback 3b A3 ioc_reject classifier (vs B1) | `--taker-reject margin,ioc_model --ioc-model report/validate/a3/model.json` | 3b rule (a751296) | **-54.1 [-66.6, -41.9]** | -67.1 [-77.5, -57.2] | -25.6 [-45.5, -4.7] / -50.0 [-63.4, -37.4]; engine-only 738 -> 73, real-only -> 396 | **n** (dev_margin60 not run) | this session |
| Nov 23 | A3 / I12 cost-based cutoff R = 130 (vs B1) | `--taker-reject margin,ioc_model --ioc-model report/validate/a3c/model_R130.json` | I12 rule (56f74cc) | **-16.0 [-22.2, -8.0]** | -21.3 [-30.7, -12.9] | -0.7 [-2.1, +0.0] / -22.6 [-31.4, -12.6]; dev_margin60 contested -15.5 [-23.8, -7.9] (n 159); pooled -13.4 [-18.2, -8.5] | **n** | this session |
| Nov 23 | A8b queue order among real orders (Stage 1 + 2) | none | A8b rule (536b57a) + H1-H3 (ad3bf32) | no hypothesis supported on dm60 chain fills (H1 16%, H2 58%, H3 78.8%; bar 80%) | | | n (nothing built) | this session |
| Nov 23 | I01 V1 docs window 300 ms (vs B1, tournament) | `--p-priority docs:300` | I01 rule (ac1d885) | -2.3 [-6.7, +0.0] | | -3.3 [-8.8, +0.0] / -; dm60 -1.1 [-4.0, +1.4]; pooled -1.8 [-4.0, -0.0], uncontested -3.3 [-5.3, -1.4] | **n** (bar) | this session |
| Nov 23 | I01 V2 docs window 500 ms | `--p-priority docs:500` | I01 rule (ac1d885) | -2.4 [-4.7, -0.4] | | -1.0 [-3.4, +0.0] / -; dm60 -0.6 [-2.3, +0.0]; pooled -1.3 [-2.5, -0.3], uncontested -1.4 [-2.3, -0.6] | **n** (bar) | this session |
| Nov 23 | I01 V3 docs for any resting tif | `--p-priority docsall` | I01 rule (ac1d885) | +0.0 [0, 0] | | +0.0 / -; dm60 +0.0; pooled +0.0 (changes only warm-up txs: dev20 2, dev-fast none) | **n** (bar: point < +1.0) | this session |
| Nov 23 | I21 gate spares orders placed after its touch evidence (vs B1, tournament) | `--gate-spare-after` | I21 rule (0272e36) | +2.7 [+0.0, +5.9] | | +4.1 [+0.0, +9.8] / -; dm60 +2.7 [+0.2, +6.4]; **pooled +2.9 [+1.0, +5.1]** (0.527 -> 0.556), uncontested +0.3 [-0.0, +1.0]; engine-only 133 / 208 / 347 -> 133 / 208 / 346 | **promoted** -> fresh confirm (not adopted yet) | this session |

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

### B0 + `--taker-reject margin` (Satya, Nov 23; written before the run)
Switch from data-streams (passed twice on its margin-era confirm sets vs FROZEN v2: +4.7 and +3.8 contested). Rule:
the rule since Nov 23 (dev20 rule AND dev-fast contested paired CI upper bound >= 0), B0 vs B0 + the switch.
Expected: dev20 a no-op (0 of 20 chunks have clearinghouseState.json; sanity check); dev-fast is the real test (20 of
20 have it). If it passes: into docs/baseline.md (B1), Rust parity checked (the rule is in the Python Replay layer,
which drives both engines), row here.

### Overnight protocol (Satya, Nov 23; `~/book-replay-data/handoff/OVERNIGHT_PROTOCOL.md`, recorded before the next run)
- Ownership: mahito = A4 (placeholders / pre-window naming) and A5 (cancel matching / exact oid counting); Yuta = A6
  (post-only margin gate + phantom orders) and the data-side diagnostics; Yuta sends unmatched-cancel examples via
  `handoff/`.
- Mailboxes `handoff/mail/to_<name>.md` (append-only, numbered); read pointer `handoff/mail/<name>.read`; inbox checked
  after every finished step.
- One big run at a time: a run expected to use > 1 GB takes `mkdir handoff/RUN_LOCK` (name + start time inside;
  `rmdir` when done); held lock: non-run work or wait <= 20 min; a lock older than 3 h is broken only after asking.
- Adoption without Satya only by the rules above (default; data-gated). Yuta's switches: Yuta hands over, mahito
  runs the B0 check, adopts into docs/baseline.md, checks parity; Yuta mirrors FROZEN_CONFIG.md. Both re-read
  docs/baseline.md before every run.
- Never: final240, confirm68, the reserved post-change benchmark set, pushing, touching main, changing a rule after
  its results are seen, pausing the ingest. 3 failed attempts at one thing -> QUESTIONS + next item.
- mahito queue: 1 A5 · 2 B0 checks of handed-over switches · 3 A8b (queue order among real orders) if Yuta reports
  queue priority as the main cause · 4 reserve >= 120 post-change chunks after Yuta's confirm set in
  `handoff/benchmark_postchange.txt` (never replayed).

### A5 · Exact oid counting (Satya, Nov 23, session 5; written before any A5 run)
Owner: mahito (Yuta sizes unresolved-cancel misses on dev_margin60 and sends cases). Baseline: B1.

**Part 1 · counting test (analysis; adopts nothing).** Sets dev20 + dev-fast (40 chunks), raw explorer blocks, every
order item of every tx on every asset (errored txs included, flagged). Truth points = placements whose real oid is
known: capture's orderStatus answers (anchors.json), the oid facts (data/oid_facts.load), recorder user_orders on every
coin (stream_orders_all), each located at EXACTLY one order item by (user, cloid, time) or (user, px, size, time);
points located ambiguously are dropped, then the longest chain increasing in both position and oid is kept
(OidEstimator.consistent). For consecutive truth points a < b (listing order across the chunk), residual
r = (oid_b - oid_a) - (items in (a, b] that the variant says take an id). Variants, fixed now:
- V0 = rule B (G0): every item of a NON-errored order / modify / batchModify tx, any asset, trigger items included;
- V1 = V0 + the items of errored txs;
- V2 = V0 without trigger items.
Reported per variant: pairs with r = 0 / r < 0 / r > 0, split into same-block pairs and pairs crossing d = 1, 2-5,
> 5 block boundaries; for r != 0 pairs what lies between (tx types incl. twapOrder, block boundaries, zero-hash trades
on BTC/ETH).
**Verdict.** Counting "holds" in a scope (same block; or crossing d boundaries) for a variant iff >= 99% of that
scope's pairs have r = 0 AND the scope has >= 200 pairs over the 40 chunks. Best variant = the most same-block
pairs with r = 0 (tie: V0). If counting holds in no scope: report, build nothing, stop.

**Part 2 · resolver behind `--oid-count` (only if Part 1 finds a scope).** Inside a scope where counting holds,
the oid of every placement is exact from a truth point in that scope (best variant's count); an int-oid cancel /
modify target that falls in such a scope maps to that one placement, used iff it rests, belongs to the canceller and
is on that asset; everything else keeps today's resolver (interpolation + oid bracket). Held-out check before any
scoring run: truth points in 3 folds (by chunk order), each fold's points removed from the counting anchors; every
exact mapping of a held-out point compared with its real placement. **Wrong exact mappings must be <= 1%** (a wrong
cancel target removes a real maker's order: same bias as a wrong owner name), else the switch is not scored.
Then the scoreboard run vs B1 under the session-5 rule above (no naming clause; the held-out precision >= 99% plays
that role).

### A4(b) · Name placeholder pieces from later trades and cancels (Satya, Nov 23; written before building)
Builds on A4(a) (`--placeholder-split`: each snapshot level is split into its uncovered order count, so one
placeholder piece ~ one real order). Q13: (a) is adopted only together with (b).
- **(b1) name from trades** = `--placeholder-split` + trade-learn rule `name` (B0's `sweep,queue` become
  `sweep,queue,name`). After a tx's real trades (block end, as B0), at the tx's worst price w on a maker side, the
  last real maker M at w (highest seq) may still rest there with a partial remainder. If our queue at w has no order
  of M and its FRONT order is a placeholder piece, that one piece becomes M's order (same size, same place). With the
  split, a piece is one order, not the whole level (the whole-level version, `name`, was dropped Nov 19).
- **(b2) name from cancels** = (b1) + `--named-cancel`: a cancel / cancelByCloid of user U on asset a that our book
  cannot resolve (UNKNOWN_ORDER) removes U's trade-named piece on a if U has exactly ONE such piece resting there
  (none or several: nothing happens). A named piece has no oid / cloid, so this is the only way U's own cancels reach it.
- Rule: the rule since Nov 23, each of (b1) and (b2) vs the current baseline in docs/baseline.md at run time. If both
  pass, adopt the larger dev20 contested point estimate, unless within 0.5 points: then (b1) (simpler).
  Counters reported: pieces named, named pieces later filled in the engine (and whether by the real maker),
  named pieces removed by (b2).
- Parity: the naming lives in the Python Replay layer (drives both engines); an adopted part gets a Rust parity run.

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

## Rules written before each item (Nov 23, second session)

### A8 · Queue order among known orders: sub-causes (written before the A8 run)
Population: A0's 26 "queue order at the right price" misses (dev20; `report/validate/a0_retriage_dev20/misses.csv`).
For each, at its first divergence (real fill: maker M at price p; ours: maker E at price p'), the orders in our book
just before the tx are read with their origin (placed in the window by a Place or a Modify: block, listing position,
time, priority p; or not placed in the window: placeholder, named-from-trades, kept pre-window). One sub-cause each,
first match wins:
1. **partial fill / size**: E = M (same maker; our size differs from the chain's).
2. **price**: p' != p (we filled a different price level first).
3. **named order placement**: E's order was created from trades (named post-snapshot liquidity, put at the FRONT of
   its level) or is a pre-window order we keep, and sits ahead of M.
4. **modify semantics**: M's order or E's order was created by a modify / batchModify (our engine: modify = cancel +
   new order at the back).
5. **priority window**: E and M are both ALOs placed within 400 ms of each other and our docs priority rule changed
   their order (E has the higher p and was placed after M).
6. **placement order within a block**: E and M were placed in the same block (listing order decides).
7. **older order the chain did not fill**: E was placed before M in an earlier block, yet the chain filled M first:
   on chain E was not resting there (cancelled / rejected / moved): a phantom we cannot see.
8. **other**.
A hypothesis is "supported by the chain's fills" when the real fills of the miss (and of other txs at that level)
order the makers the way the hypothesis predicts; each supported sub-cause gets a fix behind a switch, scored with
the adoption rule above.

### A4 (a)-(c) · Placeholder owners (written before the A4 run)
Switch `--placeholder-split` (default off). (a) split each placeholder level into its snapshot count n of orders
(sizes from the feed's count changes where available, else equal parts), keeping one queue; (b) name owners from
later trades and cancels (a real maker filled at that level in a later tx takes the placeholder order of the matching
size); (c) ties broken by the account's habitual order size (median size of its window orders at that asset).
Adoption: the rule above (dev20 rule AND dev-fast contested CI upper bound >= 0).

### A8 (dev20; `python -m analysis.a8_queue ...`, `handoff/a8/`)

| sub-cause (first match) | misses | share of the 26's missed notional (441k USD) | checked against the chain | rule fix? |
|---|---|---|---|---|
| price: we filled E at a level the chain skipped | 7 | 67.7% | E's order older, placed once (cloid), never cancelled, never filled on chain; the chain swept past its level | no: E's order was not on chain (phantom, cause unseen) |
| placement order within a block | 6 | 23.1% | chain-wide test, 169 same-block pairs: listing order agrees 89%, higher priority bid p filled first 61:3 (our docs priority already does this); in all 6 misses E was listed first AND had the higher p, yet was not filled | no: E not on chain. 0x6e8bc7cd's order: perpMarginRejected (order histories) |
| older order the chain did not fill | 7 | 1.3% | same as "price": E placed once, never cancelled/filled; in the clearest case our level equals the chain's (4 orders, 83,604 lots) but the chain filled the newer M first | no: phantom / unknown |
| partial fill / size | 4 | 0.7% | 4/4: the chain filled E at that price earlier, fills our engine gave to other makers | no own fix: downstream of earlier misses |
| other | 2 | 7.3% | E placed in the tx's own block; 0x6e8bc7cd's order perpMarginRejected | no |
| named order placement, modify semantics, priority window | 0 | 0% | - | - |

Hypotheses rejected on the chain's data: cloid reuse (each E cloid placed exactly once), modify (none), named
orders (none), the 400 ms priority window (none). **Reading: 21 of the 26 "queue" misses are not queue rules at all:
our book holds an order of E that the chain does not (2 / 2 checkable in the order histories: perpMarginRejected).**
That is A6's target (margin-rejected post-only orders), not A8's. No A8 switch was run because no sub-cause gave a
rule supported by the fills.

### A4(a) (`--placeholder-split`)
Works as built (one dev20 chunk: 78 -> 781 placeholder orders after 3 blocks) but changes NO scored fill on dev20 or
dev-fast (txs.csv byte-identical): nothing in B0 acts on individual placeholder orders (takers eat a level's
front placeholders in order whatever the piece boundaries; scoring treats placeholder fills as anonymous; the touch
ledger skips placeholders). It can only matter together with (b) naming or (d) count-based eviction of pieces.

### A4(b) (vs B1; outputs `report/validate/a4b{1,2}_{dev20,devfast}`, pairs `a4b_cmp_*`, logs `handoff/a4b/`)
Maker-strict (the scoreboard metric) in the row above. The pre-registered PRIMARY metric of contested_paired, per-tx
recall, moves the other way: dev20 contested -0.5 [-1.9, 0.0], uncontested -3.4 [-6.9, 0.0]; dev-fast uncontested
-3.5 [-9.9, 0.0]. Cause (checked on dev20 tx 0x291196be9e): recall credits a fill against an anonymous placeholder as
a wildcard; once the piece is named, the fill carries a maker, and recall scores only makers placed in the window, so the
same fill flips from recall-credit to strict-credit. Same fills, re-labelled.

Counters (`python -m analysis.a4b_counters <set> --hl-data ... <B1 flags> --placeholder-split --trade-learn sweep,queue,name --named-cancel`):

| set | pieces named | engine fills on named pieces | of which real maker at that price | named pieces removed by owner's cancel (b2) |
|---|---|---|---|---|
| dev20 | 91 (BTC 48, ETH 43) | 107 | 72 (67%) | 40 (no scored tx changed) |
| dev-fast | 60 (BTC 38, ETH 22) | 110 | 51 (46%) | 30 (no scored tx changed) |

Both parts formally pass the rule (dev20 contested lower bound 0.0; dev-fast contested unchanged). Held, not adopted:
QUESTIONS Q15. Rust parity: not available (replay_core's refill has no per-level counts; every chunk fails with a
TypeError), so adoption would need that port first.

### A5 Part 1 (dev20 + dev-fast, 40 chunks; `python -m analysis.a5_oid_count ~/hl-data/mac3/dev20 ~/hl-data/mac3/devfast --hl-data ~/hl-data/mac3/hl_data --out report/validate/a5_count/count.json`, log `handoff/a5/count.log`)
6,968,481 order items (28,108 in errored txs, 7,559 trigger items). 319,105 truth points located (anchors.json 6,140,
oid facts 193,627, recorder user_orders on every coin 294,730; 6 dropped as not monotone).

| variant | same block (312,162 pairs) | 1 boundary (5,475) | 2-5 boundaries (1,428) |
|---|---|---|---|
| V0 rule B | **89.1%** r = 0 (r > 0: 28,153; r < 0: 5,912) | 25.1% | 22.6% |
| V1 + errored items | 88.6% (r < 0 rises to 8,046: errored items take no id) | 24.5% | 21.8% |
| V2 - trigger items | 88.9% | 21.6% | 19.1% |

V0 same-block residuals: -1: 4,428 · +1: 13,984 · +2: 3,620 · >= +10: 4,878. Pairs with r = 0 span on average 9 order
txs; pairs with r > 0 span 105: ids the blocks do not show appear INSIDE blocks at a steady rate (roughly one per
few hundred order items), plus more at block boundaries (25% r = 0 across one boundary). **Verdict (rule): counting
holds in no scope; no resolver built.** Today's interpolation + oid-bracket resolver stays.

Exploratory only (looked at after the verdict; not pre-registered, decides nothing): on one dev-fast chunk all 118
pairs inside ONE tx had r = 0 (items of a tx take consecutive ids; data/oids.py joint_tx already uses this); a
least-squares fit of r on item classes (3 dev-fast chunks, gaps <= 40 items) gives about +0.15 extra ids per
non-filling BTC/ETH IOC item and -0.06 to -0.16 per modify item (a modify of a missing target takes no id). See
QUESTIONS Q16.

### Queue item 4 · reserved post-change benchmark (rule fixed Nov 23 ~10:13Z, before the chunks exist)
`python -m scripts.benchmark_postchange`: qualifying chunks (Yuta's Amendment 1/2 filter) ranked by start height;
1-10 dev_alo10, 11-70 Yuta's confirm_alo2, **71-190 = the benchmark**, written once to
`~/book-replay-data/handoff/benchmark_postchange.txt` when all 190 exist (never rewritten, never replayed; Satya
decides when). At 10:13Z: 21 qualifying chunks (about one per 5 min, so ~14 h to go). Nothing written yet.

### A8b · Queue order among REAL orders: pre-registration (Nov 23 ~10:20Z; activates only if Yuta's phantom-vs-queue
### result says queue priority is the main cause, protocol queue item 3; nothing run yet)
Two stages, so no hypothesis is tested on the data that suggested it.
- **Stage 1 (form, dev20 + dev-fast under B1).** Cases: contested misses whose A0 cause is "queue order at the right
  price" (A0 retriage rerun on B1) PLUS any A8 "price" / "older order" case that Yuta's feed-count bucket puts in
  "ours == n" (our order count at that level equals the feed's n just before the tx). For each case record, for E (our
  first filled order) and M (the chain's): placement block / listing position / time, priority p, tif, whether placed
  by modify / batchModify, size, same user as the taker, reduce-only, and how many of our orders at that level the
  chain's fills skipped. Hypotheses = candidate queue rules that reproduce M-before-E in at least 2/3 of the cases they
  apply to; at most 3, written into this section before Stage 2.
- **Stage 2 (test, chain fills, no engine).** Each hypothesis is checked on the chain's own consecutive fills at one
  price (as analysis/same_block_queue.py) on Yuta's dev_margin60 cases and chunks, which mahito has not looked at:
  supported iff it predicts the chain's order in >= 80% of the pairs it applies to, with >= 30 pairs.
- **Stage 3.** Each supported rule goes behind a switch; default adoption rule (dev20 contested point > 0, lower
  >= 0, dev-fast upper >= 0; uncontested upper >= -1.0). If no hypothesis survives Stage 1 or 2: report, build nothing.

**A8b status (10:23Z):** not triggered. Yuta's phantom-vs-queue result on dev_margin60 (mail 2): ours > n 39.0%,
ours == n 12.6%, ours < n 16.3%, level not in the 5-level feed 31.9%; with chain truth, 19 of 20 filled orders were live.
By the protocol branch Yuta builds A6-feed; whether A8b starts anyway is NS-2 (handoff/NEEDS_SATYA.md; QUESTIONS
Q17). Stage 1 script: `~/book-replay-data/handoff/a8b/stage1.sh` (not run). The dev_margin60 queue cases stay unopened
(Stage 2 test set).

**A8b Stage 1 result (Nov 23 ~19:40Z; NS-2 answered 17:24Z: start).** Inputs on B1: order_audit, A0 retriage, and
`analysis.a8b_cases` (our queue at p just before each "queue order at the right price" miss), dev20 + dev-fast
(`report/validate/a8b_cases_b1_{dev20,devfast}.json`; retriage dev20 hit the 2.5 GB guard after writing its output,
which is byte-identical to B0's, as B0 = B1 on dev20). Yuta's feed-count bucket exists only for dev_margin60 (the
Stage 2 set), so Stage 1 uses the retriage queue cases only. 37 cases: 6 where E and M are the same user / order (miss
lies later in the tx), 7 without both E and M at p, **24 with M behind E**. In those 24: M has a LOWER priority bid than
E in 21 (p-always predicts M first in 2), M was placed later than E in 20 (FIFO by placement predicts M first in 4), E is
larger than M in 17, all are ALO; E's owners are a few heavy-quoting bots (0xd7d271, 0xfeee94, 0x6e8bc7, 0x50d59f,
0x1bde1b, ...). "Lower p first" is not a plausible exchange rule; the likelier reading is that those E orders were not
resting on the chain (phantoms), which Stage 2 can separate because the chain's fills contain only live orders.
Disclosure: while locating the Stage 2 tool I printed the first 80 characters of the header and two rows of Yuta's
queue-cases file (column names, chunk, hash prefix); nothing about makers, positions or outcomes.
**Hypotheses for Stage 2 (written before it runs; at most 3):**
- **H1 lower p first**: of two live orders at one price, the one with the lower priority bid fills first.
- **H2 smaller first**: of two live orders at one price, the one with the smaller original size fills first.
- **H3 FIFO among live orders (phantom reading)**: the earlier-placed order (block, tx, item) fills first. If H3 holds and
  H1 / H2 do not, the Stage 1 misses are phantom orders ahead of M (A6 territory), not a queue rule.
Stage 2 test (raw chain data, no engine): on dev_margin60, every pair of consecutive real fills at one price within one
taker tx by two different makers whose latest placement at that price/side before the tx is in the window (and unique
in its block); a hypothesis applies to a pair when its key differs between the two orders. Supported iff it predicts the
chain's order in >= 80% of the pairs it applies to, with >= 30 pairs (as pre-registered). Supported H1 / H2 -> Stage 3
switch + default rule; H3 alone -> report, no queue switch (hand the phantom evidence to A6 / Yuta).

**A8b Stage 2 result (19:37Z; `python -m analysis.a8b_stage2 ~/hl-data/mac3/dev_margin60 --hl-data ~/hl-data/mac3/hl_data`,
`report/validate/a8b_stage2_dm60.json`; rules committed 19:34Z as ad3bf32):** 915 usable pairs of consecutive chain fills
(1,160 skipped: placement unknown or ambiguous). H1 lower p first: right in 71 of 443 (16.0%); H2 smaller first: 521 of
900 (57.9%); H3 FIFO: 721 of 915 (78.8%). **None reaches 80%: A8b builds nothing (Stage 3 not entered).** Exploratory, not
a result: on live orders the HIGHER-p order fills first in 84% of the H1 pairs, the reverse of the Stage 1 misses, which
fits the phantom reading (the high-p bot orders we keep ahead of M were not resting on the chain); handed to Yuta (phantom
ideas). Slip: this raw-data scan peaked at 1.2 GB without RUN_LOCK, overlapping mahito's own I15 hold.

### L5-lite · Multiplicity-lite `--multi-lite` (Satya, Nov 23 ~10:40Z: scoped L5; full M2 stays parked). Rule written ~10:55Z, before any build or run
**What.** Alternatives per price level only (no forking anywhere else), at most 4 per level, at most one open fork per
level (a second trigger at a level with an open fork is skipped and counted). The engine book always holds the
highest-weight alternative ("primary"; ties: the B1 choice, then the newest candidate); fills are recorded from the
primary BEFORE any evidence (rule 1). `--multi-lite` implies `--placeholder-split` and trade-learn `name` (owner hook).
Budget (M2 rules): alternatives giving the same queue (user, size sequence) are merged (weights added); weight < 1e-4
is dropped; weights renormalised after every update.
- **Phantom-or-real hook.** At every re-seed snapshot (the l2Book snapshots B1 already re-seeds from; they carry the
  per-level order count n), after B1's reconcile + refill, for each level of the snapshot: c = orders we hold there
  (placeholder pieces count one each; with the split they stand for the snapshot's uncovered orders). If c > n and
  the level holds >= 2 known window orders: candidates = those known orders, newest first (by engine seq), top 3.
  Alternatives: keep all (= B1) and "drop candidate i" for each candidate; uniform prior weights.
- **Owner-of-piece hook.** Where trade-learn `name` would name the front placeholder piece at w: if the tx's real
  trades at w have exactly ONE distinct maker, name it (A4(b1)); if 2+ distinct makers, fork the piece's owner over
  the up-to-3 most recent of them (uniform; primary = the most recent maker, = A4(b1)'s choice).
- **Evidence and collapse.** (E1) the next tx with real trades at that level (at block end, as B1's corrections): from
  the level's queue just before that tx, each alternative's FIFO prefix covering the real size traded there scores
  one point per real fill whose maker owns an order in that prefix (any placeholder in the prefix matches one fill);
  alternatives below the best score are dropped; for an owner fork, a candidate appearing as a real maker at w while
  the piece is in the prefix collapses to that owner. (E2) owner's cancel: a cancel / modify that removes candidate i
  drops "drop i" (owners don't cancel phantoms); an unresolvable cancel of an owner-fork candidate collapses to that
  owner and removes the piece (A4(b2) logic). (E3) feed counts: at each later snapshot, alternatives whose count is
  not the closest to n are halved; after 3 consecutive snapshots with the same best set the fork collapses to its
  highest-weight alternative. Collapse = the chosen alternative's queue is written into the level (orders removed /
  re-added in engine seq order / relabelled); nothing scored is changed.
- **Tests (before the screen).** Switch off -> byte-identical to B1 (unit tests + a B1 rerun on dev-fast compared with
  `report/validate/margin_devfast`). Full-visibility sim (`~/hl-data/sim/m1_fv`) -> strict 1.0 with the switch on.
  Calibrated sim (`~/hl-data/sim/m1`): share of forks whose collapse picks the truth (phantom = the sim's rejected
  order; owner = the sim's seeded owner), per hook and per evidence type. Reported, decides nothing.
- **Screen.** dev20 + dev-fast vs B1, default rule (dev20 contested point > 0 AND lower >= 0; dev-fast contested upper
  >= 0; dev20 uncontested upper >= -1.0) PLUS naming precision >= 70% on both sets for owner-labelled fills (fills on
  owner-forked or named pieces whose label at fill time is the real maker / all such fills). No confirm runs. The
  phantom hook overlaps Yuta's A6-feed: whichever passes first becomes the baseline the other is tested on (mailbox).

### Fallback 3a · A4(b) naming precision (Satya, Nov 23; rule written ~10:56Z, before any build or run)
Runs only while Multiplicity-lite waits (RUN_LOCK) or is blocked. Step 0: port the A4(a) split (`Engine.refill(counts=)`)
to the Rust engine with parity (Rust = Python on the sim parity tests and on one real chunk with `--placeholder-split`),
since every A4 adoption needs it.
Naming = trade-learn `name` on split pieces (A4(b1)), allowed only at a strictness level, using only what is known
when the name is given (no look-ahead):
- **N1**: the tx's real trades at w have exactly one distinct maker M.
- **N2**: N1 AND M's real filled size at w in this tx <= the piece's size when it was seeded (one order of M fits it).
- **N3**: N2 AND size habit: the piece's seeded size, or M's filled size + the piece's remaining, equals the size of
  one of M's own placements on that asset in the window so far (exact lots).
- "Same user later cancels / modifies a matching order" cannot be known when naming, so it is used only as later
  confirmation: `--named-cancel` (A4(b2): M's unresolvable cancel removes M's one named piece) is added to the chosen
  level as a second run if that level passes.
**Choice (dev20 only):** the least strict level whose dev20 naming precision (engine fills on named pieces whose name
is the real maker at that price / all fills on named pieces) is >= 70%; if none reaches 70%, the strictest (N3).
**Adopt iff** for the chosen level: dev20 contested point > 0 AND lower >= 0; dev-fast contested upper >= 0; dev20
uncontested upper >= -1.0; naming precision >= 70% on dev20 AND dev-fast. Baseline: whatever docs/baseline.md says at
run time (B1 now).

### Fallback 3b · A3 `ioc_reject` classifier for users WITHOUT margin snapshots (Satya, Nov 23; rule written ~11:30Z, before any training)
Facts seen before writing this (data inventory only, no model fit): the de-duplicated order histories
(`windows/*/hist_orders.json`, 466 chunks, 46 users) hold 257,550 BTC/ETH IOC / FrontendMarket orders, Jun 5 - Nov 23;
final statuses: iocCancelRejected 255,830, filled 1,714, minTradeNtlRejected 4, reduceOnlyRejected 2 (no margin
rejections). Every dev / hold-out set starts at or after Nov 19 15:22 UTC (dev20 and final240 first block).
- **Label**: 1 = the chain filled nothing (any status but filled), 0 = filled.
- **Time split**: train = orders with timestamp < Nov 19 15:22 UTC; within it, fit on the first 80% by time and choose
  calibration + cutoff on the last 20% (validation); test (report only) = orders after Nov 19 15:22 UTC.
- **Features** (all known at replay time, nothing from the order's future): the user's no-fill rate in the FIT part
  (Laplace (k+1)/(n+2); users without history get the global rate), coin, side, log10 notional (limit px x size).
- **Model**: logistic regression (statsmodels), calibration checked by 10 reliability bins on validation.
- **Cutoff (validation only)**: the lowest predicted p whose skipped set has precision (no-fill among skipped) >= 0.99;
  if none, the classifier is not used.
- **Leakage tests**: (i) every fit / validation timestamp < the split time; (ii) permuted-label refit: validation AUC
  within 0.5 +- 0.05; (iii) user rates computed from the fit part only.
- **Replay switch** `--taker-reject ioc_model --ioc-model FILE`: an IOC / FrontendMarket of a user WITHOUT a margin
  snapshot in the chunk is skipped iff its predicted p >= cutoff; evaluated as an exact per-(user, coin, side)
  notional threshold, so no floats in the replay. On top of B1 (margin gate stays first).
- **Adopt iff** the default rule (dev20 contested point > 0 AND lower >= 0; dev-fast contested upper >= 0; dev20
  uncontested upper >= -1.0) AND not worse on dev_margin60 (contested paired CI upper >= 0). Engine-only txs reported
  (dev-fast B1: 738).

### Multiplicity-lite results (vs B1; `python -m analysis.named_screen <set> --hl-data ... <B1 flags> --multi-lite --out report/validate/ml_on_<set>`, logs `handoff/ml/`)
Tests before the screen: unit 8/8; full-visibility sim property test exact with the switch on (reseed on, no fork
fires: nothing is hidden); Rust = Python with the switch on, 3 calibrated-sim chunks; switch off = B1 byte-identical
on dev-fast (5 CSVs, today's code). Calibrated sim, 40 chunks (`analysis.ml_sim_truth`, `report/validate/ml_sim/truth40.json`):
phantom collapses pick the truth 149 / 156 (by owner's cancel 131 / 132, by the next trade 6 / 10, gone 12 / 14;
only 7 of 333 forks had a truly rejected candidate; 177 still open at chunk end); owner collapses 184 / 184, but that
check cannot tell candidates apart (every candidate also had a seeded order there), so it means little.

| set | contested | uncontested | phantom forks (rewrites) | owner forks | fills on named pieces (right maker) |
|---|---|---|---|---|---|
| dev20 | +0.5 [+0.0, +1.9] | +3.0 [+0.0, +6.3] | 397 (56) | 9 | 136 (74 = 54%) |
| dev-fast | +0.0 [0, 0] | -1.1 [-7.2, +3.4] | 259 (12) | 11 | 148 (52 = 35%) |

The scored change equals A4(b1)'s to the third decimal: the phantom forks close mostly on owners' cancels (E2) and
rewrite few levels, so the effect is the naming, and the naming is wrong about half the time. Not adopted.

### Fallback 3a results (dev20 vs B1; `python -m analysis.named_screen ~/hl-data/mac3/dev20 ... --name-strict N<k> --out report/validate/a3a_N<k>_dev20`, logs `handoff/a3a/`)
| level | pieces named | fills on named pieces | right maker | contested | uncontested |
|---|---|---|---|---|---|
| N1 one distinct maker | 79 | 135 | 73 (54%) | +0.5 [+0.0, +1.9] | +3.0 [+0.0, +6.3] |
| N2 + fits the seeded piece | 78 | 132 | 70 (53%) | +0.5 [+0.0, +1.9] | +3.0 [+0.0, +6.3] |
| N3 + size habit | 0 | 0 | - | +0.0 | +0.0 |
No level reaches 70%, so the rule picks N3; N3 names nothing (a split piece's size practically never equals one of
the maker's own order sizes) and has no contested gain. Not adopted; dev-fast not run (it cannot change the outcome).
The single-maker and size-fit checks barely filter (9 and 3 namings skipped): the wrong names are not ambiguous
cases, the trade-learn `name` premise (the last real maker at w still rests at the front) is wrong about half the time.

### Fallback 3b results (`python -m analysis.a3_ioc_model ~/hl-data/mac3/windows --out report/validate/a3/model.json`; screens `report/validate/a3b_{dev20,devfast}`, logs `handoff/a3b/`)
Training (orders before Nov 19 15:22:47 UTC): fit 22,271 / validation 5,568 / test 232,353 orders; 21 users in the
fit part; no-fill base rate 98.8% (fit), 99.4% (validation, test). Validation AUC 0.960; leakage tests pass (all
fit / validation timestamps before the split; permuted-label refit, user rates from the permuted labels too, AUC 0.540;
user rates from the fit part only). Test AUC 0.954. The registered cutoff (lowest p whose skipped set is >= 99% no-fill)
is p = 0.280, which skips 232,306 of 232,353 test orders, i.e. almost every one that really filled.
Screen: dev20 contested 0.587 -> 0.046, txs we no longer fill (real-only) 18 -> 853, engine-only 518 -> 10; dev-fast contested
0.460 -> 0.204, engine-only 738 -> 73, real-only 396. Fails the default rule by a wide margin; dev_margin60 not run.
The model ranks well; the cutoff rule (precision on a 99%-negative population) was the wrong target: a skipped
IOC that really fills costs a scored tx, so the cutoff has to be set on the cost of skipped real fills (NS-3 (b)).

### A3 / I12 · cost-based cutoff for the `ioc_reject` classifier (NS-3 answer, Satya 17:24Z; rule committed 17:40Z as 56f74cc, before any new computation)
Facts known before writing this: only the 3b results above (model, validation AUC, the degenerate 0.28 cutoff and its screens).
- **Model: unchanged** (fit, features, coefficients of `report/validate/a3/model.json`, commit 179cac8). Only the cutoff rule changes.
- **Cost ratio, written first: R = 130** = cost of a false skip / cost of a false keep, in scored-tx units.
  False skip (skip an IOC that really filled): loses that tx's score if it is scored, ≈ B1's mean strict 0.65 (taken
  as the full cost: conservative, i.e. toward fewer skips). False keep (fill an IOC the chain rejected): from the
  recorded B0 -> B1 adoption (a different switch, numbers fixed before this item): removing 895 such engine-only txs
  on dev-fast gained ≈ 0.029 x 50 + 0.034 x 83 ≈ 4.3 tx-units, ≈ 0.0048 per tx. 0.65 / 0.0048 ≈ 135, rounded to 130.
- **Cutoff (training days only)**: Bayes decision under R: skip iff p >= c0 = R / (R + 1) = 0.99237 (model p as is).
  **False-skip cap** on validation (last 20% of the pre-split period): skipped filled IOCs <= 5% of validation's filled
  IOCs; if c0 breaks the cap, raise the cutoff to the lowest p that meets it. **Net check** on validation:
  (no-fill IOCs skipped) - R x (filled IOCs skipped) must be > 0 at the chosen cutoff, else the classifier is not
  used and the item fails without a screen. Test period (after the split) is reported only.
- **Variants (tournament, chosen now, max 3):** primary R = 130 decides; R = 40 and R = 400 (x 1/3, x 3) are
  reported as sensitivity only and cannot be adopted from this screen.
- **Replay switch**: the existing `--taker-reject margin,ioc_model --ioc-model FILE` on top of B1 (users with a
  margin snapshot are never touched by the model).
- **Adopt iff** (primary only) the default rule (dev20 contested point > 0 AND lower >= 0; dev-fast contested
  upper >= 0; dev20 uncontested upper >= -1.0) AND not worse on dev_margin60 (contested paired 95% CI upper >= 0).
  Engine-only and real-only tx counts reported. Pooled screen (dev20 + dev-fast + dev_margin60) reported for the
  tournament promotion bar as well.

### A3 / I12 results (`python -m analysis.a3_ioc_model ~/hl-data/mac3/windows --cost-ratio R --out report/validate/a3c/model_R<R>.json`; screens `report/validate/a3c_*`, logs `handoff/a3c/`)
Model identical to 179cac8 (fit 22,271 / validation 5,568, same coefficients; only the report-only test period grew
with new history files). Training side (R = 130): cutoff 0.99237 (c0, cap not binding); validation skips 4,776 of
5,537 no-fill IOCs and 0 of 31 filled (net 4,776 > 0, so used); test period skips 62 of 3,841 filled (1.6%).
R = 40 (sensitivity): cap binds, cutoff 0.98968, test 248 / 3,863 filled skipped. R = 400: cutoff 0.99751, 3 / 3,863.
| set | contested | uncontested |
|---|---|---|
| dev20 | -16.0 [-22.2, -8.0] (0.587 -> 0.427) | -21.3 [-30.7, -12.9] |
| dev-fast | -0.7 [-2.1, +0.0] | -22.6 [-31.4, -12.6] |
| dev_margin60 (vs B1 run today, `a3c_base_dev_margin60`) | -15.5 [-23.8, -7.9] (0.505 -> 0.350) | -24.9 [-31.7, -18.6] |
| pooled | -13.4 [-18.2, -8.5] (n 321) | -23.5 [-28.7, -18.9] |
**Fails** the rule (and the tournament bar). Diagnosis (after the verdict, exploratory): every scored tx we newly lose
(dev20 90 of 90, dev-fast 28 of 28) has a taker WITHOUT order history, skipped by the global `*` threshold (users
without history get the global no-fill rate, a 3b design choice); the 21 history users are bots whose IOCs nearly
never fill, so their rate does not transfer. History users cause only 104 of 518 (dev20) / 44 of 738 (dev-fast) B1
engine-only txs, worth ~+0.2 pt at B0 -> B1's rate, so a history-users-only variant cannot reach the +1.0 bar (NS-5).
R = 40 / 400 sensitivity screens: queued after the I01 runs (reported only).

### I01 · docs priority window T (tournament; rule committed 18:05Z as ac1d885, before any run)
Switch: `--p-priority docs:<ms>` / `docsall:<ms>` (engine_py/matching.py; `docs` = `docs:400` byte-for-byte, test).
Variants (max 3, fixed now), each replacing B1's `--p-priority docs`: **V1 docs:300**, **V2 docs:500**, **V3 docsall**
(T 400, every resting tif may be jumped). Screen: each variant vs B1 on dev20 + dev-fast + dev_margin60, pooled
(`analysis.pooled_paired`, chunk-cluster bootstrap 4000). Every set can act (no data gate).
**Promote** (DAYTIME_PROTOCOL tournament) iff pooled maker-strict contested point >= +1.0 pt AND lower >= -1.0 pt AND no
set's contested point < 0; if two or more pass, the best pooled point goes first. Promoted variants go to a fresh
confirm (list + Holm fixed before that run); only a passing confirm leads to a B1 check and adoption. Python engine for
the screen (the Rust port of `docs:<ms>` is built only if a variant is promoted).

### I21 · ALO gate spares orders placed after its touch evidence (tournament; rule written before any screen)
Origin: I15 trace (dev_margin60): in all 6 gate-eviction lost-order cases, the touch (bbo) the gate used is 66-133 ms
older than the evicted order's placement. Switch `--gate-spare-after` (engine_py/replay.py, default off): when an
admitted ALO would cross at least one resting order whose placement block time is after the touch evidence time, the
gate evicts nothing and the engine applies the ALO (it is rejected as would-cross). One variant only.
Smoke (dev_margin60 chunk 1230886851, the case where it was found): the evicted order now rests at the case blocks.
**Screen** vs B1 on dev20 + dev-fast + dev_margin60, pooled (`analysis.pooled_paired`). **Promote** iff the tournament
bar (pooled contested point >= +1.0 pt, lower >= -1.0, no set < 0). dev_margin60 is where the idea came from, so a
promotion is only a ticket to the fresh confirm (list + Holm fixed before it); adoption only after a passing confirm,
then the default rule on B1. Gate counters `spared_after_touch` / `spared_orders` reported.

### Confirm round 1 (tournament): I21 on confirm_alo2_60 (list frozen 22:15Z Nov 23, before any replay of these chunks)
- **Set:** `~/hl-data/mac3/confirm_alo2_60`, the 60 chunks of `handoff/confirm_alo2_chunks.txt` (qualifying post-change
  ranks 11-70; frozen by Yuta at 13:18Z for the post-only margin confirm, which was skipped (NS-4), never replayed by
  either session). The protocol's first fresh set. Used for this one round, then retired to dev.
- **Ideas in the round (Holm over them):** I21 `--gate-spare-after` only (Yuta's I23 goes to confirm_fast5_60, mail #21),
  so Holm over 1 = the raw p.
- **Runs:** B1 (docs/baseline.md flags) and B1 + `--gate-spare-after`, `analysis.validate_chunks`, Python engine, `--jobs 1`,
  same 60 chunks; one RUN_LOCK hold (< 45 min) or two.
- **Test:** `analysis.contested_paired` (variant vs B1 base, tx set fixed by the base run), per-tx maker-strict contested,
  chunk-cluster bootstrap 4000, seed as coded; two-sided p.
- **Pass** iff contested point estimate > 0 AND Holm p < .05. Uncontested and all-scored reported.
- **If it passes:** I21 goes to adoption: B1 check by the default rule (screen numbers on dev20 / dev-fast already meet it:
  dev20 contested +2.7 [+0.0, +5.9], uncontested +1.0 [+0.0, +3.6]; dev-fast contested upper +9.8), Rust parity
  (byte-identical dev20 + dev-fast with the switch), then B2 = B1 + `--gate-spare-after` in docs/baseline.md.
- **If it fails:** I21 is not adopted and is not re-tried on this set with a tweak.
- **Known property of the test, stated before the run:** the coded p is 2 x min(share of bootstrap means <= 0, share >= 0),
  so resamples whose mean is exactly 0 (chunks where the switch changes no scored tx) count against the effect. With few
  changed txs this makes p conservative (dev20 screen: lower bound +0.0 but p 0.23). The test is used as coded; no
  alternative p is computed for the decision.

**NS-1 defaulted 13:14Z (3-hour rule, reversible):** A5 exact oid counting closed; today's resolver stays; next step = trace Yuta's 17 dev_margin60 lost-order cases by removal mechanism (analysis only).
**NS-2 defaulted 13:23Z (Satya's set default):** A8b waits; Stage 1 script ready, Yuta's dev_margin60 queue cases unopened.
**Benchmark check 13:24Z:** `scripts.benchmark_postchange` ranks reproduce Yuta's dev_alo10 (1-10) and confirm_alo2 (11-70) lists exactly; 71 qualifying chunks exist; nothing written.

**NS-5 defaulted 21:01Z (3-hour rule, reversible):** A3 closed: the cost-cutoff screen failed and a history-users-only variant is worth ~+0.2 pt at most (below the +1.0 bar); I12b not pre-registered.

**Confirm round 1 result (23:22Z Nov 23): FAIL on p.** confirm_alo2_60, B1 vs B1 + `--gate-spare-after`, per-tx maker-strict
contested **+4.1 [-0.0, +11.4], p 0.251** (n 125, 41 chunks; 0.562 -> 0.603). Uncontested +1.1 [+0.0, +3.6] p 0.71; all
scored +2.0 [-0.0, +5.0] p 0.095. Changed txs: 3 contested chunks (+1.0 x4; +1.0, +0.16; one -0.001) and 1 uncontested
chunk (+1.0 x3). As stated before the run, p is limited by how few chunks the switch touches. Not adopted on round 1;
no tweak. confirm_alo2_60 retired to dev. Output report/validate/confirm1_{base,i21}, handoff/i21/confirm1_result.txt.
**Backup confirm triggered** (Yuta's handoff/prereg_i21_second_line_2026-11-23.md, written 22:48Z before this result:
round 1 failed on p with point > 0): same runs and test on confirm_fast5_60, pass iff point > 0 and p < .025; started 23:22Z.

**A3 sensitivity (reported only, decides nothing; A3 closed by NS-5):** maker-strict vs B1.
| cost ratio | dev20 contested | dev20 uncontested | dev-fast contested | dev-fast uncontested |
|---|---|---|---|---|
| R40  | -20.4 [-27.7, -11.5] | -25.0 [-33.6, -17.1] | -6.8 [-18.9, +0.0] | -23.8 [-32.3, -14.0] |
| R400 | -4.6 [-9.2, +0.0]    | -8.4 [-15.6, -2.7]   | +0.0 [+0.0, +0.0]  | -6.0 [-11.5, +0.0]   |
No cost ratio helps; agrees with R130 and the closure. handoff/a3c/sensitivity.txt.

### I21 bug-fix-track evaluation (BLITZ Addendum 1.4), written 23:40Z Nov 23, BEFORE the backup confirm result exists
(confirm2 base finished 23:33Z; variant still running; no confirm_fast5_60 comparison computed). Used only if the
backup confirm (frozen rule above: point > 0 and p < .025) fails. All five checks must hold:
1. **Chain behaviour cited.** Mechanism: the ALO gate judged an order with touch evidence older than the order (an
   order placed in a block later than the touch time cannot have been in the book the touch shows). Chain truth: in
   the I15 trace (handoff/i15/), ALO-gate evictions were 6 of the 17 dev_margin60 lost-order cases, and in every one the
   chain later filled the real maker M at p (case definition). Causality argument + that chain truth are the citation.
2. **Full-visibility sim stays 1.0.** `replay.sim.generate` full-visibility chunks, seeds 0-9 (120 blocks, lead-in 40,
   as tests/test_sim_generate.py), replayed with (a) the test suite's full-visibility flags, (b) (a) + B1's gate flags
   (`--alo-gate touch --touch-reconcile --touch-evict ledger --ledger-k 99 --evict-arrival` where the sim supplies the
   inputs they need), (c) (b) + `--gate-spare-after`: every scored tx strict-matched in (c) and (c) not below (b).
   If the sim provides no touch feed, (b)/(c) cannot exercise the gate: that is reported and check 2 counts as
   "not exercised" (= not met) rather than passed.
3. **Point > 0 on >= 3 sets, none negative:** per-tx maker-strict contested point, B1 vs B1 + switch, on every set with a
   paired run: dev20, dev-fast, dev_margin60, confirm_alo2_60, confirm_fast5_60 (5 sets). Screen numbers were already
   seen when this was written (dev20 +2.7, dev-fast +4.1, dev_margin60 +2.7, alo2 +4.1); only fast5 is unseen.
4. **Uncontested not worse:** pooled per-tx maker-strict uncontested point >= 0 over the same 5 sets
   (`analysis.pooled_paired`), and no set's uncontested point below -0.5 pt. (dev-fast screen showed -0.0, seen.)
5. **Flipped txs majority positive:** over the 5 sets' contested txs, count txs whose per-tx maker-strict value rose vs
   fell; rose > fell.
Reported next to it (Addendum 1.1): changed chunks and changed contested txs per set.
If all hold: NEEDS_SATYA entry (reversible, 3-hour default) "bug-fix adoption of I21 as B2", then Rust parity before
docs/baseline.md changes. If any fails: I21 closed.

**Backup confirm result (23:44Z Nov 23): FAIL.** confirm_fast5_60, B1 vs B1 + `--gate-spare-after`: contested **+1.7
[+0.0, +5.7], p 0.735** (n 115, 40 chunks; 0.268 -> 0.285); uncontested +0.3, all scored +0.6. Changed: 1 contested
chunk (2 txs up, 0 down). confirm_fast5_60 retired to dev.

**I21 bug-fix-track evaluation (checks written at 23:39Z, above), 23:44Z:**
| check | result | met |
|---|---|---|
| 1 chain behaviour cited | causality + I15 chain truth (6 gate evictions of orders the chain later filled) | yes |
| 2 full-visibility sim 1.0 | (a) doc flags 394/394; (b) + B1 gate flags **391/394**; (c) + switch 391/394, identical to (b): `spared_after_touch` never fires in the sim (switch not exercised) | **no** |
| 3 point > 0 on >= 3 of 5 sets, none < 0 | dev20 +2.7, dev-fast +4.1, dev_margin60 +2.7, alo2 +4.1, fast5 +1.7 | yes |
| 4 uncontested not worse | pooled +0.5 [-0.0, +1.1] (n 1290); worst set dev-fast -0.01 | yes |
| 5 flipped contested txs majority up | 19 up vs 2 down (12 changed chunks over 5 sets) | yes |
**I21 is CLOSED** under the rule as written (check 2 fails). Pooled over the 5 sets: contested +2.9 [+1.1, +5.0]
(n 561, 162 chunks). Re-opening it (e.g. a sim scenario that exercises the switch) would change a rule after its
results were seen: asked as NS-6, not defaultable. Files: handoff/i21/confirm2_result.txt, bugfix_pooled.txt,
bugfix_flips.txt, sim_check.log (`analysis.sim_switch_check`).

**Sim finding (lane item 3), 23:44Z:** with nothing hidden, the bare `--alo-gate touch` loses 3 of 394 scored txs (seed 4) and
rejects 92 ALOs the exact engine admits (`reject_engine_would_admit`); B1 loses one more (seed 2) that
`--stp expire_maker` recovers (the chain's documented self-trade rule, docs/deviations.md D5; B1 uses cancel_taker).
Both become IDEAS rows (I25, I26).

### I26: self-trade cancels the resting order (`--stp expire_maker`), pre-registered 23:46Z Nov 23 (before any I26 run on real data)
- **Mechanism / citation:** docs/deviations.md D5 (plan §1: the chain cancels the RESTING order and keeps matching;
  Example 6 passes only with stp=maker). B1 uses cancel_taker. Full-visibility sim: B1 390 / 394, B1 + switch 391 / 394.
- **Screen (BLITZ / tournament):** B1 vs B1 + `--stp expire_maker`, dev20 + dev-fast + dev_margin60 (B1 references
  margin_dev20, margin_devfast, a3c_base_dev_margin60), `analysis.pooled_paired`. Promote iff pooled contested point
  >= +1.0, lower >= -1.0, no acting set < 0 (a set where nothing changes is inert). One variant.
- **Reported (Addendum 1.1):** changed chunks, changed contested txs (up / down) per set; uncontested; engine-only.
- **After a promotion:** confirm per Addendum 1 (sign test on flipped contested txs, min evidence 10 flips or 8 changed
  chunks, written before the first look), or the bug-fix track (its checks written before computing them). Check 2
  (sim 1.0 under all combos) cannot pass until the touch gate's sim shortfall (I25) is fixed.

### I25: touch evidence in time order (`--touch-trades-first`), pre-registered 23:52Z Nov 23 (before any I25 run on real data)
- **Mechanism / citation:** RealTouch ranked equal-time events SNAP < BBO < TRADE; a block's trades (intra-block) and its
  bbo / l2 snapshot (post-block) share the block time (real data: all bbo and trade times equal block times), so trade
  prices of consumed levels overwrote the post-block touch. Full-visibility sim (`analysis.sim_switch_check`, 10
  seeds): `--alo-gate touch` 391 / 394 with 92 wrong rejects -> 394 / 394, 0 wrong rejects; B1 390 -> 393; B1 + switch
  + `--stp expire_maker` 394 / 394 (handoff/i21/sim_check_tf.json). Test: tests/test_p_priority.py.
- **Screen:** B1 vs B1 + `--touch-trades-first`, dev20 + dev-fast + dev_margin60 (same B1 references as I26),
  `analysis.pooled_paired`; promote iff pooled contested point >= +1.0, lower >= -1.0, no acting set < 0. One variant.
  Reported: changed chunks, contested txs up / down per set, uncontested, gate counters.
- **After a promotion:** bug-fix track (Addendum 1.4) is the intended route (documented-data fact + sim); its checks will
  be written before they are computed. I25 and I26 are screened separately; their combination only after both.

**Set overlap found (23:58Z Nov 24; fixed in the tool before the I25 / I26 results exist).** dev_margin60 and dev-fast share
4 chunks (1230748426, 1230805870, 1230864798, 1230924707), so every pooled screen so far counted them twice.
`analysis.pooled_paired` now pools each distinct chunk once (first set listed wins; shared chunks printed; per-set
lines unchanged). Corrected I21 screen: pooled contested **+2.7 [+0.8, +4.9]** (n 314, 78 chunks; was +2.9 [+1.0, +5.1]),
still above the bar; no past promotion decision changes (I01 V1-V3, I23 and A3 failed on per-set or point criteria that
the duplicates do not flip; to re-check I23: Yuta). Also: **dev20 is a subset of final240** (20 of 240; dev20 ⊂ held67
⊂ held ⊂ final240), and confirm_margin60 / confirm2_margin60 each share 3 chunks with dev-fast: NS-8.

**Full-visibility sim sweep (00:01Z Nov 24, `analysis.sim_switch_check`, 10 seeds, 394 scored txs; base = listing,
stp expire_maker, docs priority, `--alo-gate touch --touch-trades-first`):** each of 18 switches added singly stays at
394 / 394 (trade corrections block-end / tx, reseed, keep-known, nearest-ratio, outside-other-txs, deep-book,
evict-arrival, oid-bracket, touch-reconcile, ledger, taker-reject margin, gate-spare-after, multi-lite, named-cancel,
touch-refill, touch-n-cap). Only `--p-priority always` falls short (218 / 395), as expected: it ignores the documented
400 ms window (control, not in B1). So under full visibility the only B1 deviations are I25 and I26.
(handoff/i21/sim_sweep.json)

**I26 screen result (00:19Z Nov 24): FAIL (inert).** B1 vs B1 + `--stp expire_maker`: pooled contested +0.0 [+0.0, +0.0]
(n 314, 78 chunks), uncontested +0.0; **0 changed scored txs, 0 changed chunks on all three sets** (self-trades never
reach a scored tx on dev). Not promoted. It matches the documented chain rule and costs nothing; it stays default-off
and is re-checked only inside a later bug-fix bundle (if any), not on its own. handoff/i21/i26_screen.txt.

**I25 screen result (00:48Z Nov 24): FAIL the tournament bar.** B1 vs B1 + `--touch-trades-first`: pooled contested **+0.8
[-0.5, +2.2]** (n 314, 78 chunks; bar +1.0); dev20 +0.0 [-2.8, +2.1] (1 up, 1 down), dev-fast +2.0 (1 up), dev_margin60
+0.9 (4 up, 3 down); uncontested +0.2 [-0.5, +1.2] (dm60 4 up, 2 down). Contested flips 6 up / 4 down over 8 changed
chunks. Near-miss (>= +0.5): re-screen on the next baseline (STACKING). handoff/i21/i25_screen.txt.

### Bug-fix bundle BF1 = B1 + `--touch-trades-first` + `--stp expire_maker` (I25 + I26), bug-fix track (Addendum 1.4)
Written 00:48Z Nov 24, after the I25 / I26 dev screens above were seen, BEFORE any BF1 run and before any I25 / I26 run on
confirm_alo2_60 / confirm_fast5_60 (both retired to dev). Why a bundle: both make a rule match a documented chain or
data fact; I26 alone is inert on real data; the sim is exact only with both.
- **Runs:** BF1 on dev20, dev-fast, dev_margin60, confirm_alo2_60, confirm_fast5_60 (Python engine, `--jobs 1`);
  references: B1 runs already on disk (margin_dev20, margin_devfast, a3c_base_dev_margin60, confirm1_base, confirm2_base).
- **Checks (all must hold):**
  1. Citation: real bbo / trade times equal block times (bbo = post-block state, trades = intra-block), RealTouch
     ranked trades after the bbo; docs/deviations.md D5 for stp.
  2. Sim: BF1 = 394 / 394 on the 10 full-visibility seeds, and each of the 18 single switches of the sweep stays 394 / 394
     on BF1's base (already seen: sim_check_tf.json, sim_sweep.json; re-run on the committed code before the decision).
  3. Contested point > 0 on >= 3 of the 5 sets and no set < 0.
  4. Pooled uncontested point >= 0 (`analysis.pooled_paired`, chunks once) and no set's uncontested point < -0.5.
  5. Contested flips over the 5 sets: up > down.
  Reported: changed chunks / txs per set, pooled contested + all-scored, gate counters, engine-only.
- **If all hold:** NEEDS_SATYA "bug-fix adoption of BF1 as B2" (reversible, 3-hour default), Rust parity first, then
  docs/baseline.md, ACCURACY.md ("bug-fix adoption"), ANATOMY redo, final240 headline per the pre-approval (reporting
  only; NS-8 decides whether dev20 is excluded from it). **If any fails:** BF1 not adopted; I25 waits for the next
  baseline's re-screen.

**BF1 bug-fix-track evaluation (01:48Z Nov 24; checks written 0833309 before any BF1 run): ALL 5 MET.**
| check | result | met |
|---|---|---|
| 1 citation | bbo / trade times = block times (bbo post-block, trades intra-block); D5 stp | yes |
| 2 sim | BF1 394 / 394; each of 18 single switches on BF1's base 394 / 394 (re-run on committed code dcad22e; handoff/i21/bf1_sim.log) | yes |
| 3 contested point > 0 on >= 3 of 5, none < 0 | dev20 0.00 (one tx +1, one -1), dev-fast +2.0, dm60 +0.9, alo2 +6.4, fast5 +4.3 | yes |
| 4 uncontested not worse | pooled +0.1 [-0.3, +0.6] (n 1267); every set >= 0 | yes |
| 5 flips | 20 up / 4 down (shared chunks once) | yes |
Pooled over the 5 sets (chunks once): contested **+2.8 [+1.0, +5.0]** (0.488 -> 0.516, n 554, 159 chunks). Changed
chunks: dev20 2, dev-fast 1, dm60 5 (+3 uncontested), alo2 5, fast5 3 (+3 uncontested). Next: Rust parity (dev20 +
dev-fast), then NS-9 default -> B2. handoff/i21/bf1_eval.txt.
**BF1 Rust parity (02:18Z):** `replay.parity` (Rust backend, options_for(stp=maker) + `--touch-trades-first`) vs the
Python BF1 outputs: **PARITY OK, every CSV identical** on dev20 and dev-fast. Peak 2.0 GB (dev20). Adoption now waits only
on NS-9 (default 04:48Z).

**NS-6 (answered (c)) - I21 check 2 re-run exactly as written, with the I25 fix in the code (02:19Z):** (a) not re-run
(394 / 394 before); (b) = (a) + B1 gate flags + `--touch-trades-first`: 394 / 394; (c) = (b) + `--gate-spare-after`:
394 / 394, not below (b). The sim has a touch feed and the gate acts (2,188 rejects), so the "no touch feed" clause does
not apply: **check 2 is MET as written.** Stated next to it: `spared_after_touch` = 0 in the sim, so the sim neither
supports nor contradicts I21's own branch. I21's checks 3-5 were measured on B1; the baseline is about to move (NS-10),
so I21 is NOT adopted on the B1 evaluation. Instead, pre-registered now: **I21 on B2** = the same 5 checks (1 and 2 as
above; 3-5 on B2 vs B2 + `--gate-spare-after` over the same 5 sets, chunks once), run after B2 is adopted; if all hold,
NEEDS_SATYA bug-fix adoption as for BF1.

**NS-7 done (02:19Z):** ~/book-replay-g2's 11 files committed as-is to g2-determinism (9a3b82e), tagged archive/g2-determinism,
worktree removed (no --force), local branch deleted (tag keeps it).

### I27 information check (02:21Z Nov 24): **the trade id encodes both order ids.**
`tid = SipHash-1-3(keys 0, LE u64 buyer_oid || LE u64 seller_oid) >> 14` (Rust DefaultHasher on the (u64, u64) tuple,
top 50 bits). Verified on **1,996 of 1,996** recorded trades whose buyer and seller oids are both known (userFills
captures in 186 chunks' fills.json; no exception). Code: data/tid_hash.py, tests/test_tid_hash.py. Meaning: every
recorded trade (public trades stream: tid, users, taker tx hash) pins the exact (buyer order, seller order) pair once the
candidates are narrowed (taker oid near its placement's estimated oid; maker oid among the maker's orders at p). It is
post-tx evidence printed with the trade, like the evidence `--learn-evidence tx` already uses (rule 1 holds if it is
used at block end, as corrections are). Next (measurement only, no replay change): how many real fills of the dev sets
can be solved (taker oid and maker oid identified uniquely) with the oid estimator's brackets.
**I27 solve coverage (02:34Z, `analysis.tid_solve`, measurement only):** real fills (taker tx in window, after warm-up)
pinned to a unique (taker oid, maker placement): **dev20 333 / 757 (44%)**, **dev-fast 160 / 204 (78%)**, dev_margin60
760 / 1,077 (71%; partial: stopped at ~40 of 60 chunks, see slip). Unsolved: maker order not among the maker's in-window
placements at p (pre-window) 120 / 10, no match 278 / 24, ambiguous 8 / 2. Slip: the solver ran outside a lock hold
(dev20 peak 1.8 GB) - logged in handoff/i27/NOTES.md; dev_margin60 to be re-run inside a hold.

### I27a: tid-pinned queue correction (`--learn-tid`), pre-registered 02:37Z Nov 24 (before any I27a run on real data)
- **Mechanism:** B1's `queue` correction (block end, tx evidence) removes what rests ahead of the filled maker's FIRST
  order in our queue at p. The trade id (data/tid_hash.py) pins which order the chain actually filled; `--learn-tid` cuts
  at that exact order (data/tid_solve.py, solved per chunk with the replay's own oid estimator), removing everything
  ahead of it, the maker's own older orders included; unsolved trades fall back to the old rule. Evidence = the tx's
  own printed trades at block end, as before (rule 1).
- **Sim:** full visibility exact with it on (tests/test_sim_generate.py); hidden-state sim (6 seeds, real tids):
  BF1 59 -> 62 of 231 strict, notional 0.335 -> 0.348 (one seed -1 tx).
- **Screen:** current baseline (B2 = BF1 if NS-10 adopts it at / before 04:48Z, else B1) vs baseline + `--learn-tid`,
  dev20 + dev-fast + dev_margin60, `analysis.pooled_paired` (chunks once); promote iff pooled contested point >= +1.0,
  lower >= -1.0, no acting set < 0. One variant. Reported: changed chunks, contested up / down, uncontested,
  tid_exact_cuts and tid_queue_* counters. Runs after the first ORACLE queue frees the lock (shares gaps with run2).
- **After a promotion:** confirm under Addendum 1 (sign test on flipped contested txs; min 10 flips or 8 changed chunks;
  pooling plan written before the first look) on fresh sets frozen by Yuta after the benchmark list.

**B2 adopted (04:49Z Nov 24): bug-fix adoption of BF1** (NS-10 default (a), no answer by 04:48Z; Rust parity OK).
B2 = B1 + `--touch-trades-first` + `--stp expire_maker`; docs/baseline.md updated; ACCURACY.md row added. Reversible.

**I27a screen result (05:35Z Nov 24; B2 = bf1_* vs B2 + `--learn-tid`; dev20, dev-fast, dm60): FAILED (inert).**
Pooled contested +0.0 [+0.0, +0.0] (n 314, 78 chunks), uncontested +0.0 (n 632); every set +0.0; 0 txs changed, 0
changed chunks. The switch acts (one dev20 chunk probe: 4 tid_exact_cuts, 3 placeholder orders cut) but the tid-pinned
cut lands where the old rule cuts (the maker's first order at p), so no fill changes. Reading: B2's queue correction
already picks the order the chain filled whenever it fires; fill identity adds nothing THROUGH THIS CORRECTION. Not
promoted. Fill identity may still help where no correction fires (e.g. as phantom evidence for orders never filled while
later ones are): not pre-registered, not run.

**ORACLE (Addendum 3) complete (05:00Z; handoff/ORACLE.md):** O1+O3+O4 0.0; O2 contested -0.15 [-0.93, +0.65], uncontested
+1.08; O-all contested +0.43 [+0.00, +1.03], uncontested +1.20 [+0.46, +2.01]; residual 343 / 344 B1 contested misses.
Full-visibility sim: all 66 switch pairs on the B2 base 394 / 394 (handoff/i21/sim_pairs_b2.json).

**I21 on B2, bug-fix-track evaluation (07:20Z Nov 24; checks pre-registered 23:39Z Nov 23, re-run on B2 as registered
after NS-6): ALL 5 MET.** B2 (`bf1_*`) vs B2 + `--gate-spare-after` (`i21b2_*`), 5 sets, chunks once:
| check | result | met |
|---|---|---|
| 1 chain behaviour cited | causality (touch older than the order cannot disprove it) + I15 chain truth (6 of 17 dm60 lost-order cases) | yes |
| 2 full-visibility sim | met as written (NS-6 re-run; spare branch not exercised by the sim, stated) | yes |
| 3 point > 0 on >= 3 sets, none negative | dev20 +1.8, dev-fast +4.1, dm60 +2.7, alo2 +0.9, fast5 +1.7 (5 of 5 > 0) | yes |
| 4 uncontested not worse | pooled +0.6 [+0.1, +1.3]; per set +1.0 / -0.0 / +0.4 / +1.1 / +0.3 (none < -0.5) | yes |
| 5 flips majority positive | contested 13 up / 2 down (10 changed chunks); uncontested 8 up / 1 down | yes |
Pooled contested **+1.9 [+0.8, +3.3]** (n 552, 159 chunks; 0.514 -> 0.533). Next as registered: Rust parity (queued),
then NEEDS_SATYA "bug-fix adoption of I21 as B3" (reversible, 3-hour default).
**I21-on-B2 Rust parity (07:25Z):** replay.parity, options_for(stp=maker) + `--touch-trades-first --gate-spare-after`
vs i21b2_dev20 / i21b2_devfast: PARITY OK, every CSV identical. NS-11 (a) unblocked (default 10:22Z).
**I27 tid solve, dev_margin60 (07:16Z, inside a hold):** 917 of 1,332 trades solved (69%); dev20 44%, dev-fast 78%.

### I27c: phantom ledger from trade ids, pre-registered 08:10Z Nov 24 (info check seen; NO I27c variant built or run)
- **Info check (seen):** analysis.tid_phantoms over all 96 dev chunks (B2 + `--learn-tid`): 279 phantom proofs, 128 real
  proofs. Flag "phantom proofs >= 1 and > real proofs", counting only proofs from EARLIER blocks (earlier chunks in time
  order + earlier blocks of the same chunk): recall 0.69, precision 0.94 on later proofs. Within-chunk only: recall 0.13.
- **Ledger:** handoff/i27/tp_chunks events compiled to handoff/i27/phantom_ledger.json (user -> [height, kind]). When
  replaying chunk C, a proof counts only if its height < C's first block height; proofs from C's own earlier blocks
  are added online from the replay's tid events (block end, rule 1). Flag fixed: phantom >= 1 and phantom > real (k = 1,
  chosen from the info check; no other k is screened).
- **V1 sink (`--phantom-mode sink`):** a resting named order of a flagged user is jumped by every later non-flagged
  named order at its level (walk from the tail, as p-priority insertion), and a flagged user's own order never jumps
  anyone by p. Placeholders untouched; nothing is removed.
- **V2 reject (`--phantom-mode reject`):** a flagged user's window ALO placement is not applied (a Modify's cancel half
  still is).
- **Screen:** B2 vs B2 + `--learn-tid --phantom-ledger FILE --phantom-mode {sink,reject}` on dev20 + dev-fast +
  dev_margin60, analysis.pooled_paired; promote iff pooled contested >= +1.0, lower >= -1.0, no acting set < 0. Reported:
  changed chunks, contested up / down, uncontested, flagged users, sunk / rejected counts. Holm across the 2 variants at
  the confirm. Caveat: the screen sets' ledger was built from runs on these same dev sets (earlier chunks only), so the
  confirm on fresh sets is the real test; its ledger = proofs from any chunk earlier in time than each confirm chunk.
- **Full-visibility sim:** both variants must stay 394 / 394 (no phantoms exist there, so nothing should be flagged).
- **If B3 (NS-11) is adopted before the screen runs, the screen uses B3 as its baseline** (stated now).

**I27c screen result (08:58Z Nov 24; B2 vs B2 + `--learn-tid --phantom-ledger --phantom-mode`): BOTH FAIL.**
| variant | pooled contested | pooled uncontested | per set contested (dev20 / dev-fast / dm60) | contested up / down | uncontested up / down |
|---|---|---|---|---|---|
| V1 sink | -2.9 [-7.1, +1.7] | **-6.9 [-9.9, -4.3]** | -0.4 / +1.8 / -4.0 | 24 / 36 (34 chunks) | 2 / 54 |
| V2 reject | **-9.9 [-17.4, -2.8]** | **-7.0 [-11.6, -3.2]** | -4.3 / -17.1 / -10.5 | 33 / 71 (46 chunks) | 17 / 75 |
Reading: the info check measured precision on PROOF events only, and proofs only arise where a queue cut fires at a
contested level. The flagged users are big ALO makers whose OTHER orders are real (they are the real maker in many
uncontested fills): flagging the USER instead of the ORDER removes / demotes real liquidity. A phantom is a property
of an order (or of a user at a moment), not of a user. Not promoted; I27c closed (no other k / variant screened).

**Chain queue order vs the docs p-priority rule (09:22Z Nov 24; analysis.queue_truth; 216 chunks of dev20, dev-fast,
dm60, confirm_alo2_60, confirm_fast5_60; measurement):** when one taker tx fills two tid-pinned maker orders A then B
at one price, the chain's queue had A ahead of B. Over **1,003 pairs**: FIFO-consistent 881 (309 same block), docs jump
predicted and seen 120 (**23 same block**), jump seen but not predicted **0**, docs jump predicted but not seen **2**
(cross block). **The docs rule (400 ms, higher p, ALO over ALO) matches the chain in 1,001 of 1,003 pairs, same block
included.** Consequence for tid_triage: the 371 same-block, higher-p ALOs listed AFTER the chain's filled order X but
queued ahead of it by our docs rule cannot have existed on chain (a real one would have jumped X, as the 23 same-block
pairs show): they are PHANTOMS (H-reject; H-jump refuted). They cannot have been rejected for crossing (X rested at the
same price on the same side), so margin-type item rejections or invisible removals are the candidates (Yuta's status
truth: perpMarginRejected seen among the few known).

**B3 adopted (10:24Z Nov 24): bug-fix adoption of I21 on B2** (NS-11 default (a), no answer by 10:22Z; Rust parity OK).
B3 = B2 + `--gate-spare-after`; docs/baseline.md updated; ACCURACY.md row added. Reversible.

### I33: price significant-figure rule, pre-registered 10:41Z Nov 24 (no I33 code or run exists yet)
- **Chain behaviour:** HL docs (tick and lot size): a price may have at most 5 significant figures unless it is an
  integer, and at most 6 - szDecimals decimals. SQD truth (RIKA R1, devfast block 1230572957, user 0xf36c562b, action
  691): 7 BTC ALOs incl. "60967.2" -> ONE status for the whole action, "Price must be divisible by tick size", no item
  placed (the valid "60908" item included). Our engine rests every such item (touch audit: 6 / 6 rested).
- **Info check (seen):** 383 window placements with a non-integer price of > 5 significant figures in the first 6
  dev_margin60 chunks (BTC 382, ETH 1) of 240,674 placements.
- **V1 item rule (`--px-sigfig item`; bug-fix track, Addendum 1.4):** a window Place (or a Modify's new order) whose
  price is non-integer with > 5 significant figures is not applied (a Modify's cancel half still is). All tifs.
- **V2 action rule (`--px-sigfig action`; normal screen):** V1, plus every other order tx of the same user in the same
  block at adjacent listing positions with the same time (the explorer's split of one action) is not applied either.
- **Bug-fix-track checks for V1 (written now, before any run):** (1) chain behaviour cited (above); (2) full-visibility
  sim 394 / 394 for B3 + V1 and each of the 12 single switch groups on that base (the sim's prices never violate, stated:
  the rule cannot fire there, so check 2 shows only that it changes nothing else); (3) per-set contested point > 0 on
  >= 3 of the 5 sets (dev20, dev-fast, dm60, alo2, fast5; B3 refs i21b2_*), none negative; (4) pooled uncontested >= 0
  and no set below -0.5; (5) contested flips up > down. Reported: changed chunks, changed contested txs, rejected counts.
- **V2:** screen bar (pooled contested >= +1.0, lower >= -1.0, no acting set < 0) on dev20 + dev-fast + dm60 vs B3; a
  pass goes to a fresh-set confirm (Addendum 1 sign test). V1 and V2 Holm-paired at the confirm.
- I33 note (written before any I33 run): decoded actions carry no explorer tx time, so V2 groups by same user +
  adjacent listing positions in the block only (no time equality); the explorer's split txs of one action are adjacent.

### I34: gate defers after an in-block sweep, pre-registered 11:24Z Nov 24 (no I34 code or run exists yet)
- **Info check (touch audit, RIKA.md, seen):** 33 SQD chunks, 7,033 ALOs the chain ACCEPTED: we rejected 170 (2.4%),
  all as would-cross; 130 of them by the ALO gate while our in-block engine book did NOT cross; in 120 of those 130 an
  EARLIER tx of the same block traded on the crossing side at / through the gate's touch price (recorded trades, taker
  tx listed before the ALO). The gate reads the previous block's post-block bbo, so it cannot see the in-block sweep.
- **Rule (`--gate-insweep`, default off):** when a recorded trade of an earlier tx in the same block (execution order)
  hit the ALO's crossing side at or through the gate's touch price, the gate abstains for that ALO and the engine's own
  crossing check decides. Causal: only trades of txs executed before the ALO are used (rule 1).
- **Normal screen (one variant):** B3 vs B3 + `--gate-insweep` on dev20 + dev-fast + dm60, analysis.pooled_paired;
  promote iff pooled contested >= +1.0, lower >= -1.0, no acting set < 0; then a fresh-set confirm (Addendum 1 sign test).
  Reported: changed chunks / contested flips, uncontested, gate abstains added. Full-visibility sim must stay 394 / 394.

**O5 ALL-USER ORACLE (RIKA R2; 12:00Z Nov 24; look-ahead by design, diagnostic only; B3; dev-fast + dm60):** all-user SQD
truth on 56 covered contested misses: +2.86 pt [+0.48, +5.66] (6 fixed, 7 up / 1 down); tracked-only truth +0.00 on
identical runs; ceiling @100% coverage 5.7 pt [1.0, 11.2]. Branch ">= ~2 pt" (CI lower end 1.0). NS-13 (AWS plan).

**I33 V1 (`--px-sigfig item`) bug-fix-track result (12:28Z Nov 24): NOT adopted (check 3 fails).** B3 vs B3 + V1:
dev20 / dev-fast / dm60 contested +0.0 / +0.0 / +0.0, uncontested +0.0 each: 0 scored txs change (the rule acts on
unscored txs only; 383 violating placements per 6 dm60 chunks sit away from scored fills). Check 3 (point > 0 on >= 3 of 5
sets) can no longer be met, so the 2 confirm-set runs were stopped. The rule stays documented chain behaviour (kept as a
switch, default off). V2 (action rule) screen running as pre-registered.
**I33 V2 (`--px-sigfig action`) screen (13:35Z Nov 24): FAIL (inert).** Pooled contested +0.0 [+0.0, +0.0]
(n 311), uncontested +0.0; 0 txs changed on dev20 / dev-fast / dm60. I33 closed (both variants inert on scored txs).

### dev_cap20 dev designation + I37 plan, pre-registered 14:25Z Nov 24 (no replay of these chunks has happened)
- **dev_cap20** (handoff/dev_sets/dev_cap20.txt, read-only, sha256 e1d86d1508937be8...): the first 20 chunks qualifying
  under scripts.benchmark_postchange that start at or after 1233034937 and carry open_book_users.json (truth-live3),
  1233034937-1233035146 .. 1233109679-1233109888. DEV ONLY (screens / info checks for I37); never a confirm set (Yuta
  excludes them; confirm sets C3+ start after it). Chosen by height only, before any replay.
- **I37 info check (first, no rule chosen from it alone):** on dev_cap20, for users whose plan-time open count is flat
  across the previous snapshots (open_book_users.json, past only), count their window placements that our B3 engine
  rests; with Yuta's help (SQD only if quota remains: not required), how many of those placements sit ahead of a
  scored contested fill. Then the screen rule below is built.
- **I37 V1 rule (`--cap-pinned`, default off; written now, built after the info check):** a user is PINNED at chunk
  start if its open_book_users total_open is identical (+-1) in the last 2 snapshots before the chunk; a pinned user's
  window placements are not applied, except after that user's own cancels / fills in the window free a slot (one
  placement per freed slot). Screen: B3 vs B3 + V1 on dev_cap20 (pooled_paired; promotion bar), confirm on the next
  fresh set that carries open_book_users.json (Addendum 1 sign test).
