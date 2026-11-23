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

**NS-1 defaulted 13:14Z (3-hour rule, reversible):** A5 exact oid counting closed; today's resolver stays; next step = trace Yuta's 17 dev_margin60 lost-order cases by removal mechanism (analysis only).
**NS-2 defaulted 13:23Z (Satya's set default):** A8b waits; Stage 1 script ready, Yuta's dev_margin60 queue cases unopened.
**Benchmark check 13:24Z:** `scripts.benchmark_postchange` ranks reproduce Yuta's dev_alo10 (1-10) and confirm_alo2 (11-70) lists exactly; 71 qualifying chunks exist; nothing written.

