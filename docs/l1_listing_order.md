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

## Results

(filled in after the run)
