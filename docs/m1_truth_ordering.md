# Class sort vs proposer order on dev20, and the simulator's truth ordering (Satya, Nov 23)

## Decision rule (written 2026-11-23 03:17 UTC, before any run of this study)

Candidate simulator truth settings: `proposer` (txs execute in listing order), `class_sort` per consensus batch with
batch cuts at rate r = 0.1 (the current default), and `class_sort` with r = 0.5. Each candidate: regenerate the 200
seeds (sim200) with the r3 calibration otherwise unchanged, replay with the current baseline engine (proposer order,
stp=taker, block-end corrections with tx evidence), and measure baseline contested strict (per-tx maker-strict,
union scope) plus the 9 calibration statistics and the new maker-concentration statistic.

**We pick the setting whose baseline contested strict is closest to real dev20** (0.587 per-tx union on the
current baseline, report/validate/m1_ref_blockend20), **keeping the 9 statistics within +-25% where possible**
(a candidate that breaks a statistic that is currently in band loses to one that does not, unless no candidate keeps
it). Ties (within 0.01): prefer the one with more statistics in band. Report it, then stop.

New calibration targets added to calibrate.py: "baseline contested strict" and "maker concentration (share of
printed fills whose maker is one of the 5 makers with the most fills, per chunk, mean over chunks)".

## 1. Batch hypothesis (P5) on real dev20

`python -m analysis.order_audit ~/hl-data/mac3/dev20 --hl-data ~/hl-data/mac3/hl_data --ref
report/validate/m1_ref_blockend20 --out report/validate/order_audit_dev20` (log: `handoff/order_audit/dev20.log`).
At each of the 222 scored blocks (308 scored txs), the baseline replay is forked and only that block is re-run
under proposer order (P), class sort over the whole block (C) and class sort per batch with cuts (P5), all from the
same book. So the comparison is the block's ordering only, not history. Forked P reproduces the baseline's own
per-tx strict scores exactly (smoke check: 35 / 43 = baseline).

| | blocks | scored txs |
|---|---|---|
| scored | 222 | 308 |
| a real maker of the tx sends a cancel / modify / ALO on that asset LATER in the listing (class sort moves it ahead) | 15 | 18 (5.8%) |
| **class contradicts the printed trades** (P matches a tx, C does not) | **6** | 8 |
| class matches a tx that P does not | **0** | 0 |
| contradicting blocks fixed by batch cuts (<= 3 cuts searched) | **6 of 6, each by exactly 1 cut** | 8 |

Matched txs: P 180, C 172 of 308. When a cut is needed it is needed in 6 of 222 blocks (2.7%). The fixing cut is not
pinned to one position: any cut after the taker and at or before the culprit tx works (both ends replayed:
`handoff/order_audit/cut_upper_check.txt`; one past the culprit fails).

| block | txs | taker idx | culprit (idx, kind, class) | cut range k | share of block before the cut |
|---|---|---|---|---|---|
| 1227317687 | 6022 | 2537 | 5056 cancelByCloid, 1 | 2538..5056 | 0.42..0.84 |
| 1227398804 | 5272 | 3230 | 4840 cancelByCloid, 1 | 3231..4840 | 0.61..0.92 |
| 1227429874 | 4294 | 1960 | 2245 ALO order, 0 | 1961..2245 | 0.46..0.52 |
| 1227701204 | 6840 | 1911 | 6510 cancelByCloid, 1 | 1912..6510 | 0.28..0.95 |
| 1227768212 | 5567 | 3059, 3063 | 4756 cancel, 1 | 3064..4756 | 0.55..0.85 |
| 1227836353 | 6750 | 3076, 3090 | 4089 ALO order, 0 | 3091..4089 | 0.46..0.61 |

**Median batch size when a cut is needed:** the first batch holds between the taker and the culprit, a median
range of **2,801..4,798 txs** (median of lower ends .. median of upper ends), i.e. **about 46%..85% of the block**;
the second batch is the rest (median block 5,795 txs in these 6; 3,164 over all scored blocks). The minimum cut the
search reports (just after the taker) gives batches of median 3,064 txs (12 batches).

Reading: proposer order explains every scored tx that class sort explains (0 blocks the other way), and the 6 blocks
where class sort fails are each repaired by one consensus-batch boundary somewhere between the taker and the maker
action it would have overtaken. The data cannot tell "one cut per block at an unknown place" from "listing order is
the execution order": every repair is a cut that keeps the taker before the culprit, which proposer order does by
construction. Only 18 scored txs (5.8%) are exposed at all, so there is little power to separate the two.

Full-run view (whole-window class_sort run vs proposer run, same corrections; `handoff/order_audit/fullrun_flips.txt`):
7 contested txs class wrong / proposer right, 0 the other way (this is the -6.6 points of D34). 3 of the 7 are in
the first block where the two runs diverge (local ordering), 4 come after an earlier divergence (history: the books
differ already).

## 2. Classification audit (the txs in the 8 local flips; `report/validate/order_audit_dev20/flips.csv`)

Takers: all `order` actions with an IOC item, class 2 (6 plain IOC; 2 IOC + trigger (TP/SL) items, which the
decoder skips; class 2 is right either way because the IOC alone puts the tx in class 2).
Txs class sort moved ahead of a flipped taker (same asset, by one of its real or engine makers): 44 (36 harmless
when moved alone, 8 culprits; 6 distinct culprit txs).

| culprit kind | class given | count | real maker of the taker? |
|---|---|---|---|
| cancelByCloid | 1 | 3 | yes (3) |
| cancel (by oid) | 1 | 1 (breaks 2 takers) | yes |
| order, ALO only | 0 | 2 (one breaks 2 takers) | no: an engine maker |

**Nothing looks misclassified.** Each culprit's class is the docs' class (a pure cancel is class 1; an ALO-only
order sends no GTC/IOC, class 0). In every case the real maker's cancel (or a later ALO) is listed after the
taker in the block, and the printed trade shows the taker hit the maker's order, i.e. in reality that cancel ran
AFTER the taker, which whole-block class sort forbids. Flags raised by the audit and checked:
- IOC + trigger items in one order action (2 takers): class 2 regardless; no effect.
- modify / batchModify (an implicit cancel, class 0 under tx_class unless a GTC/IOC is placed): none among the
  culprits or the moved txs of flipped takers.
- GTC/IOC items on other assets, action errors: none.
The ALO culprits are not the real makers: class sort lets an ALO listed after the taker rest first, and the taker
then hits it in our book, while in reality the taker hit someone else. That is the same contradiction (the ALO
ran after the taker).

## 3. Simulator truth ordering

**Superseded by L1 (docs/l1_listing_order.md, Nov 23):** the real explorer lists txs in execution order (class-sorted
per batch); the candidates below all listed txs in their pre-sort order, so the comparison was between two wrong
maskings. Kept for the record.

`SimConfig.truth_order` = `class_sort` (default, unchanged) | `proposer`; `batch_cut_prob` = r, the probability
that a block holds one batch cut (`python -m replay.sim.generate ROOT --truth-order proposer`, `--batch-cut-prob
0.5`). Each candidate: 200 seeds, r3 calibration otherwise, replayed with the current baseline (log
`handoff/order_audit/truth.progress`, `rep_*.log`); statistics by `python -m replay.sim.truth_study ...`
(`handoff/order_audit/truth_study.log`, every number in `truth_study.json`).

| statistic | real (dev20) | class sort, r = 0.1 (default) | class sort, r = 0.5 | proposer |
|---|---|---|---|---|
| **baseline contested strict** | **0.587** (n 113) | 0.199 (0.34x), n 1,883 | 0.206 (0.35x), n 1,877 | **0.286 (0.49x)**, n 1,905 |
| trades per block | 0.557 | 0.572 ok | 0.567 ok | 0.571 ok |
| orders at the touch (median) | 13 | 12 ok | 12 ok | 12 ok |
| orders per level, 20 levels | 11.95 | 12.22 ok | 12.22 ok | 12.22 ok |
| touch \|dn\| median | 2 | 2 ok | 2 ok | 2 ok |
| touch \|dn\| p90 | 10 | 6 out | 6 out | 6 out |
| touch \|dsz\|/sz median | 0.144 | 0.239 out | 0.242 out | 0.241 out |
| touch \|dsz\|/sz p90 | 1.057 | 0.839 ok | 0.841 ok | 0.852 ok |
| post-only margin rejections | 0.0167 | 0.0171 ok | 0.0172 ok | 0.0171 ok |
| open-order cap rejections | 0.00143 | 0.00156 ok | 0.00157 ok | 0.00157 ok |
| maker concentration (top 5) | 0.374 | 0.552 (1.48x) | 0.559 (1.50x) | 0.558 (1.49x) |
| statistics in band (of 9) | | 7 | 7 | 7 |

Pooled per-fill strict: class r = 0.1 34.5%, class r = 0.5 34.7%, proposer 38.7%.

**By the rule written above: proposer** (closest to real, 0.286 vs 0.587; every candidate keeps the same 7 of 9
statistics in band, so none is dropped). The SimConfig default is NOT changed (report, then stop): switching it is
`truth_order = "proposer"` in `replay/sim/config.py`, for Satya to decide.

Reading:
- Truth ordering moves simulated difficulty only part of the way: 0.20 -> 0.29 contested, against 0.59 real.
  Part of the proposer gain is mechanical: the baseline replays in proposer order, so a proposer chain is the one
  whose ordering the engine assumes. The remaining gap (0.30) is not ordering.
- More batch cuts (r 0.1 -> 0.5) barely change difficulty (+0.007): one cut per block moves few contested pairs.
- The new maker-concentration target is out for every candidate (1.5x real: fills concentrate on a few simulated
  market makers), and ordering does not touch it. It is a candidate cause of the remaining difficulty gap (a
  contested tx's maker is more often a busy quoter with many cancels and ALOs in the same block), untested.
