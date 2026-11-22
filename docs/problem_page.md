# Replay the Order Book (problem page)

Transcribed from the "Replay the Order Book" artifact (claude.ai, version of Nov 22) on Nov 22, because the
`docs/problem_page.md` mentioned on Nov 22 was not in the repo. One change, as decided: **P4 case 2 uses
`after = (6.5, 4)`** (the page printed `(7.5, 4)`, but its own answer only works for 6.5).

## Problem

You are given a snapshot of an exchange's order book and the blocks of transactions that follow it. The snapshot only
gives each price level's total size and order count. It does not say who owns the orders or in what order they queue.
Replay the blocks through a matching engine. For every transaction that trades, return its fills
`(maker, price, size)`. The answer key is the list of trades the chain actually printed.

**Input.** `snapshot`: `{side: [(px, sz, n), ...]}`, best price first (`n` = orders at the level). `blocks`: ordered,
each with a `time` (ms) and `txs`; a tx has a `user`, an index and one action: `order` (tif `Alo` post-only, `Gtc` or
`Ioc`, and a priority bid `p`), `cancel` or `modify`. `bbo`: best bid / ask `(px, sz, n)` per block from the public
feed. The first 150 blocks are a lead-in (replayed, not scored).

**Output.** `fills[tx] = [(maker, px, sz), ...]` for every scored tx that trades.

**Scoring.** Per-trade strict recall = matched notional / real notional, a fill counting only if the maker is right
too. Headline: the average over contested trades. 58.8% contested strict on 240 chunks (51.3% before the docs p rule);
BTC / ETH 48% / 58% before it; 62.7% all trades strict (was 56.5%).

**Constraints (chain rules).**
- Within a block: actions with no GTC/IOC order first, then cancels, then GTC/IOC orders; proposer order within a class.
- A post-only order that would cross is rejected (`badAloPxRejected`).
- The chain also checks margin, a $10 minimum, open-interest caps and the oracle band (none visible in the blocks).
- At one price, post-only orders placed in the last 400 ms are sorted by priority bid; an order older than that keeps
  its place.
- On a self-trade, the chain cancels the resting order.

## Examples (where the reference solution fails)

**Example 1: fee bid jumps an order that already locked its place (fixed).**
Level 100 asks: A (p=0, placed t=0 ms). New ask at 100: B (p=5, placed t=2000 ms). Taker buys 1 @100 at t=2100.
Chain: A fills (A rested over 400 ms, so B queues behind it). Old "known" mode: B fills. Docs rule: A fills.

**Example 2: IOC from an account with no free margin (failing; fix in test).**
Bot with free margin ~ $0 sends IOC buy 0.01 BTC @ ask. Chain: `perpMarginRejected`, no fill. Ours: fills 0.01
against the ask, eating real liquidity, so the next real taker gets the wrong maker.

**Example 3: snapshot liquidity has no owner (failing).**
Snapshot ask level 100: sz 5, n 3 (owners and queue unknown); taker buys 2 @100. Chain: maker C (2nd in the real
queue). Ours: maker = placeholder (right price and size, wrong maker).

**Example 4: real order lost before it trades (failing).**
A cancel by an oid placed before the lead-in; we guess which resting order it means. Chain removes X; ours removes Y,
so X's later fill is missed and Y's slot is a phantom.

**Example 5: post-only order from an account without margin (small).**
ALO ask 1 @100 from an account with no free margin. Chain: `perpMarginRejected`, never rests. Ours: rests it, then
fills the next taker against it.

**Example 6: self-trade (rare).**
U rests ask 1 @100, then V rests ask 1 @100; U sends IOC buy 1 @100. Chain: U's ask canceled, U's IOC fills against
V. Ours (default stp): U's IOC canceled, U's ask stays (a phantom). Fix: `stp="expire_maker"`.

**Example 7: the chain removes an order with no action (rare).**
Reduce-only ask, then the position closes. Chain: `reduceOnlyCanceled`. Ours: the order stays in the book.

**Example 8: order inside a block we can't see (not measurable).**
An explorer block can merge several consensus batches into one list; ours treats the whole block as one batch.

## Hidden-information puzzles

**P1. Name the anonymous orders (Example 3).** Snapshot ask 100.0 sz 7.0 n 3. t1: U places ALO ask 2.0 @100 (feed
sz 9.0 n 4). t2: no decoded action at 100 (feed sz 6.0 n 3). t3: trade, taker buys 1.5, maker 0xM (feed sz 4.5 n 3).
Return every queue of the snapshot orders that fits. Rules: FIFO, sizes multiples of 0.5, new orders join behind
snapshot orders, trades hit the front first. Answer: a hidden 3.0 left at t2; at t3 the count stays 3, so 0xM owns
the front order and it is bigger than 1.5; the two remaining snapshot orders sum to 4.0, so 0xM's is one of 2.0, 2.5,
3.0, 3.5; the removed 3.0 could have been at any of three positions: 12 queues; only "0xM at the front with 2.0-3.5"
matters for the next fill.

**P2. Will the chain accept this IOC? (Example 2).** Margin needed = notional / leverage (<= 40 BTC, <= 25 ETH);
reduce-only orders and orders that shrink the position skip the check. A: value 25, used 32, long 0.05 BTC.
B: value 10,000, used 2,000, flat. C: no snapshot; last 3 IOCs filled in our engine, the chain printed nothing.
BTC ~ 60,000. Orders: 1) A buy 0.01, 2) A sell 0.02, 3) B buy 1.00, 4) C buy 0.001. Answer: 1) reject (free -7, needs
15); 2) accept (shrinks a long); 3) accept (needs 1,500 of 8,000 free); 4) probably reject.

**P3. Which order does this cancel mean? (Example 4).** Anchors (block 100, tx 3) -> oid 5,000; (block 120, tx 1) ->
oid 5,400. U's resting BTC orders: a (block 90), b (block 105), c (block 118). Cancel U, BTC, oid 5,350. Answer: c
(between the anchors rules out a; ~20 oids per block puts 5,350 near block 117.5).

**P4. Which post-only orders never rested? (Example 5).** Return every set of rejected placements that explains the
after values. Case 1: before (5.0, 2), placements A 1.0, B 2.0, C 0.5, after (6.5, 4): B rejected (unique).
Case 2: before (5.0, 2), placements A 1.0, B 1.0, C 0.5, **after (6.5, 4)**: A or B rejected (ambiguous).

**P5. Where are the batch boundaries? (Example 8).** Book: ask 100 A 1.0. Txs: [0] T IOC buy 1 @100; [1] A cancels
its ask; [2] B ALO ask 1 @100. Printed: T bought 1.0 from A. Answer: one boundary, between [0] and [1].

**P6. When do reduce-only orders vanish? (Example 7).** U short 1.0 BTC (unknown without a snapshot); resting
reduce-only bids r1 0.6 @99, r2 0.6 @98; t1: U buys 1.0 as a taker. Answer: both canceled at t1 (U is flat).
Open: when the position shrinks below a reduce-only order's size without reaching zero, cancel or shrink?

Reference toy solutions: `puzzles.py`.
