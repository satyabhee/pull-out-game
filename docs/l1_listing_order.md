# L1 · Is the explorer listing the execution order? (MAHITO_PLAN §00, Nov 23)

## Decision rule (written before any count was made)

**Class of a tx** (docs' rule, whole action, all assets): 2 = an `order` / `modify` / `batchModify` that places at
least one GTC, IOC or FrontendMarket item (trigger items do not count); 1 = `cancel` / `cancelByCloid`; 0 = anything
else (ALO-only orders, scheduleCancel, noop, transfers, ...). Errored txs are kept (they are listed).

**Statistic.** For each block, the class sequence in listing order. R = number of runs (maximal stretches of equal
class). Under "the chain class-sorts per consensus batch AND the listing shows execution order", every batch is
sorted (0...0 1...1 2...2), so R <= 3 x (batches) and the number of descents (class[i+1] < class[i]) <= batches - 1.
Under "the listing is the proposer's order, not class-sorted", R is close to a random arrangement of the same
classes.
**Null.** 200 seeded shuffles of each block's own class sequence (`random.Random(height)`); ratio per block
rho = R_obs / mean(R_null). Blocks with < 10 txs are skipped. Sets: dev20 (all 4,200 blocks); dev-fast as well once
built.

**Decision** (on the median rho over blocks):
- rho_med <= 0.2: the listing is class-sorted within batches -> listing = execution order under per-batch class
  sort. Batch rate = median descents per block + 1 batches; the simulator truth becomes class_sort with that
  number of batches per block (cut rate set to match).
- rho_med >= 0.8: the listing is NOT class-sorted. Combined with the order audit (from the same book, listing order
  reproduces every scored tx class sort reproduces, never the reverse; 6 contradicting blocks, each needing a cut
  between taker and culprit), the listing order is the execution order as far as the data can tell, and class sort
  of the whole block is not what the chain does -> simulator truth = **proposer**.
- in between: partially sorted; report the distribution, and set the truth to class_sort with the measured batch
  count only if the 6-block check below passes; otherwise proposer.

**6-block check.** For each of the 6 conflicting blocks (`docs/m1_truth_ordering.md` §1), count the descents of the
listing's class sequence inside the block's one-cut range (taker+1 .. culprit] and compare with the shuffled
expectation. "Consistent with batching" for a block = at least one descent in the range AND the count is not above
the null's (batch boundaries should be rare, not everywhere). Passes if all 6 are consistent.

**What it means for A5** (exact oid counting): A5 assumes order items take oids in listing order. If L1 says
proposer, A5 proceeds with listing order; if class sort per batch, oids are assigned in execution (class) order and
A5 must count in that order.

## Results (Nov 23)

`python -m analysis.listing_runs ~/hl-data/mac3/dev20 --out handoff/l1/dev20_runs.csv` (log `handoff/l1/dev20.log`):
3,921 blocks with >= 10 txs (median 2,013 txs per block).

| | observed | shuffled null |
|---|---|---|
| runs of the class sequence per block (median) | **6** | ~1,000 |
| rho = runs / null runs: median (p10, p90) | **0.006** (0.002, 0.021) | 1 |
| class descents per block (median) | **1** | 506 |

Descents per block: 0: 11.3%, 1: 39.1%, 2: 28.9%, 3: 13.0%, 4: 4.8%, 5+: 2.9% (max 10).

**Verdict by the rule: rho_med = 0.006 <= 0.2: the explorer listing is class-sorted within consensus batches, so the
listing shows the execution order under per-batch class sort.** A block holds a median of 2 visible batches (1
descent; a boundary where the next batch starts with a class >= the last one's is invisible, so this is a lower
bound).

**6-block check: 6 / 6 consistent** (`handoff/l1/six_blocks.txt`): every one-cut range from the order audit contains
1-3 real descents (null expectation 72-1,107), e.g. block 1227317687: range (2537, 5056], descent at 2551; block
1227701204: (1911, 6510], descents at 1912 and 1916. The batch boundaries the printed trades need are the ones the
listing shows.

**What this changes.**
- The two hypotheses of `docs/m1_truth_ordering.md` §1 were not alternatives: replaying the LISTING order already IS
  per-batch class sort (with the true batch boundaries). That is why proposer order never lost to whole-block class
  sort, and why whole-block class sort fails exactly where a later batch's cancel gets pulled ahead of an earlier
  batch's taker. The baseline's order=proposer is the correct execution order; class sort of the whole block is not.
- **Simulator truth (set by the rule):** class sort per batch, with the explorer listing written in EXECUTION order
  (`SimConfig.listing = "execution"`, new) and one batch cut in every block (`batch_cut_prob = 1.0`, i.e. 2 batches
  = median descents + 1). Until now the simulator listed txs in their pre-sort order, which the real explorer never
  shows: that is what made last session's truth study pick "proposer" (docs/m1_truth_ordering.md §3, superseded).
- **A5 (exact oid counting):** oids are assigned as orders execute, and the listing is the execution order, so A5
  can count order items in LISTING order (no re-sorting needed). Caveats for A5's test: invisible batch boundaries
  do not matter for counting (the listing is still the execution order across them); rejected items and both coins
  must be checked, as the plan says.

**Simulator under the L1 truth** (sim200 regenerated at `~/hl-data/sim/l1`, fv20 at `~/hl-data/sim/l1_fv`; baseline
replay `report/validate/l1_sim200_base`, log `handoff/l1/sim200.log`): baseline contested strict **0.262** (n 1,884;
pooled per-fill strict 37.4%) vs real dev20 0.587. Was 0.199 with the pre-sort listing. Still too hard: L2.
Simulator tests pass with the new default (35 incl. the full-visibility property test, Rust = Python on all 8 switch
combinations, and a new test that the listing is class-sorted per batch).
