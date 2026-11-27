# Gate 2b: what cancels-first is worth, at scale (pre-registration)

Written 08:29Z Nov 27 (commit 7bf5d9c; first draft said 08:50Z by mistake), BEFORE any gate 2b quantity is computed (no conflict has been scanned or valued). Satya's
overnight brief, with mahito's choices where the brief leaves one open; those are marked **[call]** and logged in
NEEDS_SATYA. Gate 2 (docs/gate2.md, docs/gate2_results.md) stays signed and untouched. Gate 2b reuses none of its
numbers. No engine changes, no new data pulls, no pushes.

## 0. Question and headline

**Worth = USD per day that cancels-first saves the makers who cancel**, estimated as

    W = sum over conflicts i of  P(taker sent first) x saved notional_i x adverse 10 s markout_i   (L21)

Two numbers are reported, both with window-cluster bootstrap CIs and **no significance gate**:
- **UPPER bound** W_up: P = 1, and the takers reach the order without any queue ahead (saved size = min(r, Q), below).
- **ESTIMATE** W_est: P = P_hat from SQD-timed conflicts, and saved size = the queue reach from the B3 book.

Each is reported per day, per block, by 10 s volatility decile, and as the share of W in the top volatility decile
and in the top 1%.

## 1. Population

- **Windows:** every captured explorer window under ~/hl-data/mac3/windows/, which is 2,320 windows and 466,605
  blocks. Every set chunk (dev20, final240, ...) is a sub-range of one of these windows.
- **Exclusions:** windows that touch an ingest gap (`data.catalog.chunk_gap`, data/ingest_gaps.json at the commit
  that adds this file).
- **[call] Held-out sets:** final240, confirm68 and the held sets lie inside these windows. Satya said ALL chunks, and
  gate 2b screens nothing (B3 is fixed and nothing is tuned or chosen on these blocks), so they are included. This is
  not a use for screening.
- **Scored blocks:** from each window's warmup_end_height onward (AGENT_PLAN section 4). The warm-up blocks are
  replayed but never scored.
- **Engine:** the B3 flags (handoff/i27/baseline.txt line 2), ON order only (the chain's listing order), at the
  `unified` commit that adds the scanner. One replay per window.
- **[call] Run order and partial runs:** windows are run in a fixed pseudo-random order (sha256 of the window name).
  If the run isn't finished by 13:15Z, the finished windows are scored as a random subset, labelled "partial: n of N
  windows". Failed replays are listed and excluded. If more than 5% of the windows fail, the headline is labelled
  "partial" as well.

## 2. Batches (P5)

- **Batch rule:** inside a block, batches come from class-order resets on ALL explorer txs of the block, in listing
  order. The class of a tx is the one used for Yuta's SQD batch column (data-streams analysis/gate2_input.py,
  mirrored):
  - C: cancel or cancelByCloid;
  - A: order, modify or batchModify whose items are all Alo;
  - G: order, modify or batchModify with any non-Alo item;
  - O: everything else.
  - Rank C0 < A1 < G2 < O3. A rank drop starts a new batch.
- **Validation:** on every height with an SQD batch id (Yuta's send_times_yuta95.csv for the gate 2 primary, 71
  blocks, and for the A5 extension, 200 blocks), report two things:
  - (a) tx-level agreement: the share of SQD rows whose explorer batch partition matches Yuta's (same-batch relation
    over pairs of txs in the block);
  - (b) the share of conflict pairs (cancel tx, taker tx) that are also in one SQD batch.
  - The P5 cut can split a batch spuriously at an out-of-class tx, but it can't merge two batches. So (b) measures how
    many conflicts survive the SQD cut. This is reported, not gated.

## 3. Conflict

- **Cancel:** a cancel / cancelByCloid / modify / batchModify by user U that, in the B3 replay, removes a resting order
  o of U without a fill.
  - The order has coin C, side s, price P, remaining size r (lots), and queue ahead A (lots).
  - **Queue ahead A** = all resting size on side s at prices better than P, plus the size ahead of o at P (FIFO),
    measured just before the cancel applies.
- **Takers T(o):** order or modify actions with any GTC or IOC item (FrontendMarket = IOC) that meet all of these:
  - in the **same batch** as the cancel;
  - coin C, opposite side;
  - limit crossing P (a buy at or above P against an ask; a sell at or below P against a bid);
  - user different from U;
  - any position in the batch.
- **A conflict** is an order o with T(o) non-empty. Q = the sum of the full sizes of the T(o) items.
- **Not conflicts (counted):** a cancel whose target the engine can't find, and an order resting only as an anonymous
  placeholder. Gate 2b can only see conflicts where the B3 book resolves the cancelled order.

## 4. Valuation

- **Markout, L21 (leads Run 14):** mo = 1e4 x s' x (m(t + 10 s) - m(t-)) / m(t-), with s' = +1 if o is a bid and -1
  if it is an ask.
  - t is the block time.
  - m(t-) is the last recorded bbo mid strictly before t.
  - m(t + 10 s) is the first mid at or after t + 10 s, within the gate 2 wait limit (MID_MAX_WAIT_MS).
  - It is mid-to-mid, so the spread doesn't enter. A conflict with no mid is counted and gets no value.
- **Adverse markout:** max(0, -mo). A maker whose quote would have moved in its favour saves nothing.
- **Value of conflict i, in USD:** saved size_i x P_i x adverse_i / 1e4.
  - UPPER: saved size = min(r, Q), and P = 1.
  - ESTIMATE: saved size = clamp(Q - A, 0, r), and P = P_hat.
- **P_hat:** on the conflicts at SQD-timed heights (above), over pairs (cancel tx, taker tx) where both txs have a
  yuta95 send time:
  - P_hat = the mean of 1[taker send < cancel send], with ties counted as 0.5;
  - pairs with an untimed tx (stale / future nonce) are excluded and counted.
  - **[call]** One P_hat is applied to every conflict.
- **Totals:**
  - W_up = the sum of the UPPER values.
  - W_est = P_hat x the sum of the ESTIMATE values (before P).
  - Per block = W / scored blocks.
  - **[call] Per day** = W / scored seconds x 86,400. Scored seconds = the sum over windows of (last block time - time
    of the first scored block).

## 5. Volatility split

- **vol(t):** for coin C, the trailing realised volatility over the 10 s before t: sqrt(sum of squared log returns of
  consecutive bbo mids with time in [t - 10 s, t)), in bp. No data after t is used.
- **Deciles:** cut per coin over the population's conflicts. Conflicts with fewer than 2 mids in the 10 s window go in
  their own row.
- **Reported:** W_up and W_est per decile, the share of W in the top decile, and the share in the top 1% (vol at or
  above the coin's 99th percentile of conflicts).

## 6. Statistics

- **Cluster:** the window. 10,000 resamples, seed 20261127, percentile 95% CIs.
- **Joint resampling:** each resample draws windows with replacement from the scored population. It recomputes every
  total, and P_hat from the SQD pairs of the drawn windows, so W_est's CI carries both the valuation's and P_hat's
  uncertainty, and their overlap.
- **Reading:** estimates only, no significance gate. The results state the assumptions in section 7 next to the
  numbers.

## 7. Assumptions to state with the result

- **Queue reach:** the takers' full sizes go to this level, and queue ahead is the B3 book's. That book has known
  drift and anonymous placeholder depth. Other cancels ahead in the same batch are not netted out.
- **P:** one share, measured on SQD-timed blocks (the gate 2 primary blocks and the A5-selected blocks), is assumed to
  carry over to all conflicts.
- **Order flow is fixed.** No maker or taker reacts to the rule. This is the mechanical value of the ordering.
- **Markout:** mid-to-mid at 10 s. It is the maker's adverse move, not a realised P&L.
- **Coverage:** only conflicts whose cancelled order the B3 book resolves. Cancels that miss are counted.
- **Capture schedule:** windows were not captured at random times, so per day is a scale-up of the captured windows,
  not a calendar average.

## 8. Code and tests

- **Code:** analysis/gate2b.py (scanner + valuation).
- **Tests:** on hand-checkable synthetic conflicts. Each fixes A, Q, r, the batch, the mids and the expected USD
  value.
- **Outputs:** report/validate/gate2b/, with per-window conflicts written as jsonl.
- **Results:** docs/gate2b_results.md.
