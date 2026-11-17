# 01-book — Order Book Specification

## 0. Price and Size Representation

Prices will be represented internally as **integer ticks**, and order quantities as **integer lots**. A tick is the smallest permitted price increment for a market, while a lot is the smallest permitted quantity increment. These are independent units.

Each market has an immutable, versioned configuration containing:

- `tick_size` — smallest permitted price increment.
- `lot_size` — smallest permitted quantity increment.
- `quote_atom_size` — smallest unit used to represent quote-currency notional.
- `min_price_tick` and `max_price_tick` — allowed price range.
- `max_order_lots` — maximum order quantity.
- `max_order_notional` — maximum permitted order notional in quote-currency atoms.
- `config_version` — identifies the applicable configuration.

Tick size, lot size, and quote atom size are stored as exact base-10 decimal values, never floating-point numbers.

For example, with a tick size of $0.01 and a lot size of 0.001 units:

```text
API price: "101.25"  ->  10125 ticks
API size:  "2.345"   ->  2345 lots
```

The API adapter converts decimal strings directly into integer units using exact base-10 arithmetic. It must never parse a price or quantity through a binary floating-point type.

The conversion rules are:

```text
price_ticks = price / tick_size
size_lots   = size / lot_size
```

Both results must be exact integers. If a submitted price or quantity is not an exact multiple of its tick or lot size, the order is rejected rather than rounded. Rounding could change price priority or the quantity being traded.

Size lots always represent increments of the market's `lot_size`, independently of price. They are not price ticks.

### Notional Calculation

The actual quote-currency notional of an order or fill is:

```text
notional = price_ticks * size_lots * tick_size * lot_size
```

The market configuration defines `quote_atom_size` such that one tick multiplied by one lot is an exact integer number of quote-currency atoms.

Therefore:

```text
notional_quote_atoms =
    price_ticks * size_lots *
    (tick_size * lot_size / quote_atom_size)
```

For example:

```text
tick_size:       0.01
lot_size:        0.001
quote_atom_size: 0.00001

1 tick * 1 lot = 1 quote atom
```

The engine uses checked integer arithmetic for notional calculations. Intermediate products use 256-bit or arbitrary-precision integer arithmetic to avoid overflow. The resulting notional must fit the supported quote-amount representation and the market's configured maximum.

### Why Integers Matter

Floating-point representation can make mathematically equal prices compare as different values. For example, a price calculated through multiple floating-point operations may differ slightly from another representation of the same intended price.

This is particularly dangerous for an order book because:
- Equal prices must be recognized as belonging to the same price level.
- Price comparisons determine which orders execute first.
- Equality determines whether two orders cross.
- Different rounding results across machines could produce different fills and state hashes.

Using integer ticks and lots makes these operations exact and deterministic across implementations.

All internal price and quantity values use `uint64_t`. Zero is a valid integer representation, but an order price must fall within its market's configured price range. Quantity must be greater than zero.

If an incoming order exceeds a market's configured price, quantity, or notional limit, it is rejected before admission. These limits are defined explicitly per market, rather than assumed to be universal constants.

Unexpected internal arithmetic overflow, counter exhaustion, or invalid state causes the block transition to fail rather than allowing values to wrap around.

### Configuration Changes and Replay

Market configurations are immutable once activated. Every order and execution records the configuration version used to interpret its integer values.

Historical prices and quantities are never recalculated using a newer configuration during replay.

A configuration change takes effect only at a block boundary. Before activating the new version:

1. Cancel all resting orders using the old configuration.
2. Emit a cancellation event for each order with reason `CONFIG_CHANGE`.
3. Process cancellations in canonical book order: bids from highest to lowest price, then asks from lowest to highest price; orders within each level are processed by arrival sequence.
4. Do not consume arrival or fill sequences for these cancellations.
5. Activate the new configuration version.

This prevents historical integer ticks and lots from being reinterpreted using different market parameters.

---

## 1. Order and Fill Representation

The matching engine supports limit orders in this version. Its canonical Order representation contains exactly the following fields:

### Order

- `market_id` — identifies the market containing the order.
- `config_version` — market configuration used when the order was admitted.
- `order_id` — engine-assigned 128-bit identifier, unique within its market.
- `owner_id` — canonical account identifier used for self-trade prevention.
- `side` — `BUY` or `SELL`.
- `price_tick` — integer limit price in ticks.
- `size_lots` — original order quantity in lots.
- `remaining_lots` — quantity not yet filled.
- `tif` — time-in-force: `GTC`, `IOC`, or `ALO`.
- `arrival_sequence` — sequence number assigned by the matching engine to determine time priority.

The globally unique identity of an order is the pair `(market_id, order_id)`. Order IDs are allocated using a per-market counter and are never reused.

The following validity conditions apply:

- `size_lots` must be greater than zero.
- `price_tick` must be within the configured market price range.
- `remaining_lots` must be greater than zero for an order to remain on the book.
- `remaining_lots` cannot exceed `size_lots`.
- An order whose `remaining_lots` reaches zero is immediately removed from the book.

Order timestamps are not authoritative for queue priority. Arrival sequence determines priority because multiple orders may have identical timestamps or arrive within the same block.

Client-provided IDs, if supported, are optional correlation or idempotency metadata. They do not replace `order_id` and do not determine priority.

`reduce_only` is not part of this version's canonical Order representation. Position and reduce-only validation belong to the risk/admission layer before an order reaches the matching engine.

### Fill

A Fill represents one individual execution between a resting maker and an incoming taker.

The matching engine produces exactly these fields:

- `market_id` — market in which the execution occurred.
- `config_version` — configuration used to interpret the execution units.
- `maker_order_id` — resting order providing liquidity.
- `taker_order_id` — incoming order removing liquidity.
- `maker_owner_id` — account owning the resting order.
- `taker_owner_id` — account owning the incoming order.
- `taker_side` — `BUY` or `SELL`; the maker's side is the opposite.
- `price_tick` — execution price in integer ticks.
- `size_lots` — executed quantity in integer lots.
- `fill_sequence` — monotonically increasing per-market execution sequence.
- `block_height` — height of the block whose execution produced this fill.
- `index_in_block` — position of the fill within that block's execution, starting at 0. `(block_height, index_in_block)` identifies a fill uniquely and orders fills exactly as they were executed.

Owner identifiers are included so downstream settlement can attribute executions without relying on the maker order remaining in the book.

The execution price is always the **resting maker's price**, regardless of whether the maker is a bid or an ask.

Fees, realized PnL, position direction, liquidation information, transaction hashes, TWAP identifiers, and builder fees belong to the settlement, account, or exchange layers rather than the matching engine's core Fill representation.

---

## 2. Block Ordering and Arrival Sequence

Two implementations must process the same block in the same order before assigning arrival sequences. Assigning sequences according to raw transaction order before applying the project's in-block priority rules would produce inconsistent results.

The project uses the following processing order for order-book actions:

1. ALO/post-only order submissions.
2. Cancels.
3. GTC/IOC order submissions.

These are the three supported order-book action categories. Transfers, leverage updates, TWAP controls, and other non-order actions are not implicitly included in any of these categories. Their processing belongs to the enclosing block transition specification.

### Ordering Within a Block

The block coordinator extracts all order-book actions and places them into the three priority categories.

Within each category, actions are ordered by their original transaction index and then their original action index.

If a transaction contains actions belonging to multiple categories, its actions are split across the corresponding processing phases. The transaction's order-book actions are therefore not executed atomically as a single group; the matching engine follows the block's canonical action ordering.

Non-order actions do not enter these three phases or independently modify the book during them.

### Sequence Assignment

`arrival_sequence` is assigned by the matching engine after canonical block ordering has been established and an order passes admission.

It is:
- Assigned by the engine, never supplied by the client.
- Maintained separately for each market.
- Monotonically increasing across blocks and configuration versions.
- Assigned only to admitted orders.
- Persisted in snapshots and restored during replay.
- Never reused, even if an order is immediately filled or cancelled.

The admission and sequence rules are:

| Situation | Order ID / arrival sequence |
|---|---|
| Invalid price or size | Neither assigned |
| Failed risk validation | Neither assigned |
| Invalid ALO that would take liquidity | Neither assigned |
| Valid GTC placed on book | Both assigned |
| Valid GTC immediately fully filled | Both assigned |
| Valid IOC fully filled | Both assigned |
| Valid IOC with no available liquidity | Both assigned |
| Valid taker cancelled by self-trade prevention | Both assigned |

An ALO submission is processed by the matching engine and undergoes its post-only validation as part of the admission decision. If the check fails, the order is rejected without receiving an order ID or arrival sequence.

A GTC or IOC order that passes admission receives its ID and arrival sequence before matching begins. A self-trade discovered during matching does not undo that assignment.

Cancels do not consume arrival or fill sequences.

### Fill Sequence

`fill_sequence` is a separate per-market counter. It increments once for every individual Fill event, in execution order.

For example, if one incoming order matches three different resting orders, three Fill events are generated with three consecutive fill sequences.

A cancellation or rejection consumes no fill sequence.

### Counter Bounds

Arrival and fill sequences use `uint64_t` counters, with the last assigned sequence initially set to zero. Each new assignment increments the corresponding counter by one.

Order IDs use a separate per-market `uint128_t` counter.

If a counter has reached its maximum value and another assignment is required, the block transition fails. Counters never wrap around or reset during configuration changes.

If exhaustion occurs after some candidate fills have been calculated, the entire block transition is discarded atomically. Partially generated state is never published.

---

## 3. Price-Time Priority

The matching engine uses **price-time priority**.

Price determines priority first, followed by arrival sequence among orders at the same price.

For resting bids:
- Higher prices have priority.
- At the same price, the lower `arrival_sequence` has priority.

For resting asks:
- Lower prices have priority.
- At the same price, the lower `arrival_sequence` has priority.

Each price level contains a FIFO queue of individual resting orders.

An incoming buy can match against resting asks whose prices are less than or equal to its limit:

```text
incoming_buy.price_tick >= best_ask.price_tick
```

An incoming sell can match against resting bids whose prices are greater than or equal to its limit:

```text
incoming_sell.price_tick <= best_bid.price_tick
```

The matching engine begins at the best available opposite-side price and processes orders at that price in arrival-sequence order before proceeding to the next price level.

**The fill price is always the resting maker's price.** The maker may be either a resting bid or a resting ask.

### Example: Incoming Buy

Suppose the book contains:

```text
Ask A: 5 lots @ 100, sequence 10
Ask B: 5 lots @ 100, sequence 11
Ask C: 5 lots @ 101, sequence 12
```

An incoming buy arrives:

```text
BUY 12 lots @ 101
```

The resulting fills are:

```text
5 lots @ 100  -> Ask A
5 lots @ 100  -> Ask B
2 lots @ 101  -> Ask C
```

Lower-priced asks receive priority because they offer a better price to the buyer. Ask A precedes Ask B because it has the earlier arrival sequence.

### Example: Incoming Sell

Suppose the book contains:

```text
Bid A: 4 lots @ 102, sequence 20
Bid B: 3 lots @ 101, sequence 21
```

An incoming sell arrives:

```text
SELL 6 lots @ 101
```

The resulting fills are:

```text
4 lots @ 102  -> Bid A
2 lots @ 101  -> Bid B
```

The highest-priced resting bids receive priority. Execution prices are 102 and 101 because those are the respective resting maker prices.

### Partial Fills and Replacements

If a resting order is partially filled, only its `remaining_lots` changes. Its original arrival sequence and queue position are preserved.

If its remaining quantity reaches zero, it is immediately removed from the book.

The matching engine does not support in-place order modifications. A replacement is implemented as a cancellation followed by a new order submission. This applies even to a quantity decrease: the replacement receives a new order ID and arrival sequence and loses its previous queue priority.

---

## 4. Time-in-Force

### GTC — Good Till Cancelled

A GTC order immediately matches against eligible resting liquidity up to its limit price.

If it is not completely filled, its remaining quantity normally rests on the book at its original limit price.

After the matching loop stops, the engine explicitly checks that the remainder cannot still cross the opposite side.

For an incoming buy, it may rest only if:

```text
best_ask > incoming_buy.price_tick
```

For an incoming sell, it may rest only if:

```text
best_bid < incoming_sell.price_tick
```

If the opposite side is empty, there is no crossing constraint.

If this postcondition fails, the engine treats it as an internal matching or state-transition failure. The block transition is aborted atomically, and no partially processed state is published. The engine must not silently cancel or rest the order to conceal the failure.

Self-trade prevention is an explicit exception to normal GTC remainder behavior, as described in Section 5.

### IOC — Immediate Or Cancel

An IOC order immediately matches against eligible resting liquidity up to its limit price.

It fills as much quantity as is immediately available and then cancels any remaining quantity.

Example:

```text
Incoming IOC BUY: 10 lots @ 101

Available asks:
5 lots @ 100
2 lots @ 101

Execution:
5 lots @ 100
2 lots @ 101

Remaining:
3 lots cancelled
```

IOC does not require the entire order to fill. An all-or-nothing execution requirement would be Fill-or-Kill (FOK), which is different.

An IOC order never rests on the book.

### ALO / Post-Only

ALO is the project's post-only time-in-force mode. These terms refer to the same behavior in this specification.

An ALO order is strictly a maker order. It can add liquidity to the book but can never execute against existing liquidity.

During the ALO processing phase, the engine checks whether the submitted order would immediately match against the opposite side:

- A buy ALO is rejected if its limit price is greater than or equal to the best ask.
- A sell ALO is rejected if its limit price is less than or equal to the best bid.

Equality is included because an order at the exact opposite best price would execute immediately.

If either condition is met, the submission is rejected with reason `POST_ONLY_WOULD_TAKE`. It receives no order ID or arrival sequence and does not alter existing orders.

Otherwise, it passes admission, receives its order ID and arrival sequence, and is placed on the book.

Because ALO submissions are processed before cancels in the project's block ordering, the post-only check uses the book as it exists during the ALO phase. It cannot assume that a later cancel in the same block will remove opposing liquidity.

---

## 5. Self-Trade Prevention

The matching engine must prevent an account from trading against its own resting liquidity.

When an incoming taker reaches a resting maker order with the same `owner_id`, the engine applies a deterministic **cancel-taker** policy.

This check occurs only when the resting maker is within the incoming order's executable price range and reaches the front of the matching queue.

The rules are:

1. The engine processes better-priced orders and earlier orders at the same price first.
2. If an eligible resting maker belonging to the taker's owner is encountered, matching stops.
3. No Fill is generated for the self-trade.
4. The resting maker remains on the book, retaining its original arrival sequence and queue position.
5. The incoming taker's entire unfilled remainder is cancelled.
6. The engine does not skip the self-owned maker to execute against orders behind it, even if those orders have different owners.

This applies to both GTC and IOC orders.

For GTC, it is an explicit exception to the usual rule that an unfilled remainder rests on the book. A GTC remainder cancelled by self-trade prevention must never be placed back onto the book.

If the self-owned maker is outside the incoming order's limit price, it is never reached by the matching loop and therefore does not trigger self-trade cancellation.

If other owners' orders are ahead of a self-owned maker at the same price level, those earlier orders can execute normally. Self-trade prevention only fires when the engine reaches the self-owned maker.

Any fills completed before encountering the self-owned maker remain valid.

### Example: Partial GTC Fill Followed by Self-Trade

The resting asks are:

| Order | Owner | Size | Price | Arrival sequence |
|---|---|---:|---:|---:|
| A | Alice | 3 | 100 | 40 |
| B | Bob | 2 | 101 | 41 |
| C | Carol | 4 | 101 | 42 |

Bob submits:

```text
GTC BUY 10 @ 102
```

The incoming order receives arrival sequence 43 after admission.

Matching proceeds:

```text
Fill 1:
3 lots @ 100
Maker: A
Fill sequence: 900

Next maker: B
Same owner as incoming taker
Self-trade detected
```

The engine cancels the incoming remainder:

```text
Incoming quantity: 10
Executed:           3
Cancelled:          7
Rested:             0
```

Ask B remains untouched, as does Ask C. The engine does not skip Ask B to reach Carol's order.

Sequence consumption:

- The incoming order consumes arrival sequence 43.
- The actual Fill consumes fill sequence 900.
- Self-trade cancellation consumes no fill sequence.
- No new arrival sequence is assigned to the cancelled remainder.

The remaining GTC order never rests, so it cannot create a new crossed book.

---

## 6. Crossed Book Invariant

Let:

```text
best_bid = highest resting buy price
best_ask = lowest resting sell price
```

A crossed book exists whenever:

```text
best_bid >= best_ask
```

This definition includes equality. A separate locked-book category is unnecessary for this invariant.

A valid resting order book must therefore satisfy:

```text
best_bid < best_ask
```

whenever both sides contain orders.

### Why a Crossed Book Is Always a Bug

A crossed book means that an executable match exists between two resting orders.

For example:

```text
Best bid: 101
Best ask: 100
```

The buyer is willing to pay 101, while the seller is willing to accept 100. The engine should already have matched these orders.

Equality is equally problematic:

```text
Best bid: 100
Best ask: 100
```

The buyer and seller are willing to trade at exactly the same price. An executable match still exists, but the engine has left both orders resting.

Therefore, after matching is complete, any book satisfying `best_bid >= best_ask` violates the matching engine's fundamental invariant. It means an executable match was left unprocessed or invalid state entered the book through another state transition.

### How Matching Maintains the Invariant

Whenever an incoming GTC or IOC order crosses the opposite side, the matching engine executes against the best available resting orders immediately.

Matching continues until:
- The incoming order is completely filled.
- The next resting price falls outside the incoming order's limit.
- Self-trade prevention cancels the incoming remainder.

Any remaining GTC quantity can rest only after the explicit non-crossing check in Section 4.

An ALO order that would cross or touch the opposite side is rejected rather than executed.

The engine assumes the starting book is valid and must preserve this invariant throughout every state transition.

### Live Invariant Checks

The matching engine checks the invariant:
- After every completed state-changing order action.
- After cancellations and replacements.
- At the end of every block.

If a live check fails, the entire candidate block transition is rejected atomically. All candidate mutations and fills are discarded, and no partially processed state is published.

The market is halted from accepting further orders until the failure has been investigated and the state has been reconstructed from a trusted checkpoint or authoritative event history.

A consensus-based implementation must reject the invalid state transition.

The engine must never silently remove orders or generate unrecorded fills to repair a crossed book.

### Snapshot Validation

A snapshot cannot be trusted merely because it was previously saved.

Before a snapshot becomes available for trading, the engine validates:
- The crossed-book invariant.
- Order ID uniqueness within the market.
- Valid price and size units under the recorded market configuration.
- `remaining_lots` constraints.
- Arrival-sequence uniqueness and ordering.
- The next order ID, arrival sequence, and fill sequence counters.
- The market configuration version.

If a snapshot contains a crossed book, it is rejected as invalid.

The engine attempts to reconstruct the book by replaying authoritative event history from a trusted state. If reconstruction is impossible, the market remains halted until an explicit recovery process succeeds.

The engine must not automatically match, delete, or modify snapshot orders during loading. Doing so could introduce executions or cancellations that never existed in the authoritative event history.

For a book generated through normal matching, a crossed state indicates a matching or state-transition failure. For a loaded snapshot, it means the stored state cannot be trusted and must be reconstructed or recovered.

---

## 7. Canonical Book Structure and State Hashing

The order book is organized into two sides, each containing price levels and FIFO queues.

Conceptually:

```text
Bids:
  highest price
    -> orders sorted by arrival_sequence
  next-highest price
    -> orders sorted by arrival_sequence

Asks:
  lowest price
    -> orders sorted by arrival_sequence
  next-lowest price
    -> orders sorted by arrival_sequence
```

A price level contains individual Order records, not just an aggregated quantity. Aggregated quantities may be calculated for market-data output, but they do not replace the individual orders required for matching.

### Canonical Iteration Order

The order book is serialized for state hashing in this order:

1. Market identifier and active configuration.
2. The next order ID, arrival sequence, and fill sequence counters.
3. Bids from highest to lowest price.
4. Within each bid price level, orders from lowest to highest arrival sequence.
5. Asks from lowest to highest price.
6. Within each ask price level, orders from lowest to highest arrival sequence.

Each order is serialized using this exact field order:

```text
market_id
config_version
order_id
owner_id
side
price_tick
size_lots
remaining_lots
tif
arrival_sequence
```

### Canonical Encoding

To make serialization identical across implementations:

- All fixed-width integers use big-endian byte order.
- Order IDs use fixed-width 16-byte unsigned integer encoding.
- Sequence numbers and tick/lot values use fixed-width 8-byte unsigned integer encoding.
- Identifiers use a 4-byte unsigned big-endian length prefix followed by their canonical byte representation.
- Enums use fixed assigned numeric byte values, not implementation-specific string representations.
- Price-level counts, order counts, and total level counts use fixed-width 8-byte unsigned integers.

The market configuration is included in its canonical immutable representation, or by including a cryptographic digest of that exact representation.

The serialization begins with a fixed domain/version prefix:

```text
01-book-state-v1
```

The state hash is calculated using SHA-256 over the resulting canonical byte sequence.

Memory addresses, hash-map iteration order, internal pointers, cached aggregates, and other implementation-specific details are never included.

The sequence counters and order ID counter are included because two books with identical visible orders but different future ID or priority state could produce different results when new orders arrive.

This ensures that different implementations can serialize the same logical book into identical bytes and independently calculate the same state hash.

---

## 8. End-to-End Matching Example

Suppose the resting asks are:

```text
Ask 1: 4 lots @ 100, sequence 20
Ask 2: 3 lots @ 101, sequence 21
Ask 3: 5 lots @ 101, sequence 22
```

An incoming order arrives:

```text
BUY 10 lots @ 101
```

The matching engine processes the best price first, then arrival sequence:

```text
4 lots @ 100  -> Ask 1
3 lots @ 101  -> Ask 2
3 lots @ 101  -> Ask 3
```

The execution prices are 100, 101, and 101 because each resting maker determines its own execution price.

The engine has explicitly defined:

1. How prices and quantities are represented.
2. How API decimal strings are converted into integer units.
3. How market configuration changes affect historical data and replay.
4. The canonical order in which block actions are processed.
5. When IDs and arrival sequences are assigned.
6. How price-time priority determines execution order.
7. How individual Fill events are generated.
8. How partial fills, replacements, and self-trades affect remaining quantities.
9. How a crossed book is detected and prevented.
10. How snapshots are validated and recovered.
11. How the book is serialized into a deterministic state hash.

Two implementations processing the same canonical block actions, market configurations, and valid initial state must therefore produce identical Fill events, final order-book states, and state hashes.
