# L3 · Captured data with per-tx arrival times or mempool data (report only; nothing built)

Why it matters: L1 showed the explorer listing IS the execution order (class sort per batch). The IE 498
counterfactual ("what if first-come ordering") needs each tx's ARRIVAL order, which execution order does not give.

**Finding: none of the captured data has a per-tx arrival time, nonce or mempool view.** Checked Nov 23:

| source (on this Mac) | time fields | arrival information? |
|---|---|---|
| explorer blocks (`windows/*/<height>.json.gz`) | tx `time` | no: equals the block time for every tx (198 / 198 blocks checked: one time per block, = `blockTime`); no nonce, no signature, no receive time per tx. Order inside a block = execution order (L1) |
| recorder `blocks.jsonl` | `blockTime`, `proposer`, `numTxs`; our `recv_ms` | no (per block; `recv_ms` = when our websocket got the header) |
| recorder `trades_*` | trade `time`, our `recv_ms` | no: trade time = block time; `recv_ms` is post-execution |
| recorder `user_orders` (tracked users) | `order.timestamp`, `statusTimestamp` | no: `order.timestamp` equals a block time for 20,012 / 20,012 orders checked (the placement block) |
| recorder `user_fills` | fill `time` | no (block time) |
| recorder `bbo_*`, `book_*`, `book_*_sf4`, `book_*_fast` | feed `time`, `recv_ms` | no (book state after blocks) |
| `tracked_users` | selection times | no |
| per-window `hist_orders.json`, `clearinghouseState.json`, `spotClearinghouseState.json`, `anchors.json`, `window.json` | order timestamps (block times), capture times | no |
| `capture.py` ("pending") | internal list of orderStatus lookups to make | no (capture bookkeeping) |

There is no mempool data: Hyperliquid has no public mempool feed, and nothing in the capture or recorder asks a node
for pending txs.

**What could provide arrival order** (not captured; for Satya to decide, nothing built):
- the signed action **nonce** (a client millisecond timestamp in every Hyperliquid action) if an API exposes it per
  tx (the explorer `blockDetails` answer does not);
- running our own non-validating node, which sees txs as they are gossiped (arrival at that node);
- L4's testnet probe: we know our own send times exactly, so a controlled experiment gives arrival vs execution for
  our own txs only.
Until one of these exists, the counterfactual can only use proxies (e.g. random or class-ignoring orders within a
batch, as specs/03-ordering.md's sensitivity check), and the report must say so.
