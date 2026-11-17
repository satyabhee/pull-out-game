# 00 — Data contract

What the replay reads, how its pieces link, what is missing, and whether exact replay is possible.
Measured on real data on Nov 17, 2026. Full evidence and commands: `G0_REPORT.md`; replay rules: `ENGINE_WINDOW_RULES.md`.

**Verdict: GO with caveats.** Blocks decode completely and every non-zero-hash trade links to its taker tx (69 of 69 checked). Exact replay is limited by three known gaps: no per-order responses, about 18% of trades with no tx, and order ids that can't be predicted exactly. Each one has a stated handling below and is reported as its own category in the mismatch table, never hidden.

---

## 1. Sources

| what | from | stored as |
|---|---|---|
| Blocks (every signed action) | `POST https://rpc.hyperliquid.xyz/explorer` `{"type":"blockDetails","height":H}` | `windows/<start>-<end>/<height>.json.gz`, plus `window.json` and `anchors.json` |
| Real fills | recorder, public `trades` websocket stream | `hl_data/YYYY-MM-DD/HH/trades_{BTC,ETH}.jsonl[.gz]` |
| Book snapshots (L2) | recorder, public `l2Book` stream | `hl_data/.../book_{BTC,ETH}.jsonl[.gz]` |
| Block headers (height ↔ time) | recorder | `hl_data/.../blocks.jsonl` |
| Asset ids, decimals | `POST https://api.hyperliquid.xyz/info` `{"type":"meta"}` | — (BTC = 0, ETH = 1) |

There is **no S3, `replica_cmds` or `node_fills`** in this project.

**Access limits.** The explorer allows about 1.1 blocks/s per IP, against about 14.5 blocks/s produced. Blocks older than roughly 3 h count as "archived", and an IP may fetch only 100 archived blocks per day. After that, every request is refused, even for new blocks. So blocks are captured **near-live into windows** (6,100 blocks: about 2 min of warm-up plus 5 min scored) and can't be picked after the fact.

## 2. Block structure (observed)

One file is one block:

```json
{"type": "blockDetails",
 "blockDetails": {"height": 1224258679, "blockTime": 1794894398170,
                  "hash": "0x4f06…9fc3", "proposer": "0x9dd8…71e1",
                  "numTxs": 1167, "txs": [ … ]}}
```

- A block is complete when `len(txs) == numTxs`. An error response has neither field and is never saved as a block.
- Each tx: `{time, user, action, block, hash, error}`. `time` is the block time (ms), and `error` is `null` on success.
- Files are named by height. The parser sorts them **numerically** and asserts heights are strictly increasing.
- **Consensus batches are not visible.** A block is one flat `txs` list. Within it, txs carrying `grouping: {"p": N}` (88% of BTC/ETH order actions) were listed in descending `p`. **Open (G1):** whether listing order is execution order.

Decoded action types, with their effect on the BTC/ETH book (counts from a 12.6-min window):

| `action.type` | count (items) | book effect |
|---|---|---|
| `order` `{orders:[{a,b,p,s,r,t,c?}], grouping}` | ~2.43 M | place each item; trigger items (`t.trigger`) are skipped |
| `cancel` `{cancels:[{a,o}]}` | 175 k | cancel by (user, oid) |
| `cancelByCloid` `{cancels:[{asset,cloid}]}` | 958 k | cancel by (user, cloid); note the key is `asset`, not `a` |
| `modify` / `batchModify` | 1.3 k / 23 k | cancel the target (int oid **or** cloid), place a **new** order |
| `scheduleCancel` `{time?}` | 31.8 k (all assets) | at `time`, cancel all the user's orders; no `time` clears it |
| everything else (`twapOrder`, leverage, transfers, `noop`, `evmRawTx`, …) | — | none; dropped and counted |

Real examples (block 1224258679):

```json
{"type": "cancelByCloid", "cancels": [{"asset": 159, "cloid": "0xfc4e1afffc66a533000208a9451b757e"}], "f": true}
{"type": "cancel", "cancels": [{"a": 110015, "o": 607819318775}], "f": true}
{"type": "scheduleCancel", "time": 1794894427572}
```

**Units.** `p` and `s` are decimal strings in mixed formats (`"64500"` and `"64500.0"`). They are converted exactly to integer ticks and lots (`engine_py/types.py`): BTC tick 0.1 / lot 0.00001, ETH tick 0.01 / lot 0.0001. In 300 blocks (~68 k BTC/ETH items), 3 items had impossible precision: a price of `"0"`, a price of `"63597.59"` on BTC, and a size of `"0.03064524"`. Their txs show no error. These items are rejected by the parser and counted.

## 3. Responses and rejects

- **No per-order responses are recorded.** `error` is action-level only (for example 1,684 "Action already expired" in 12.6 min). An ALO that would cross, or an IOC that finds nothing, is **not** reported.
- `modify` errors are reported ("Post only order would have immediately matched…", "Cannot modify canceled or filled order").
- **Handling:** an action with a non-null `error` is not applied. Otherwise the **engine decides** item-level outcomes from its own book: ALO would-cross, IOC no-fill, unknown cancel target. Margin and reduce-only rejections need account state and are unobservable. They land in the mismatch table as "margin rejection".

## 4. Linking fills to orders

Recorded trade: `{coin, side, px, sz, time, hash, tid, users: [buyer, seller]}`.

- **Taker:** `trade.hash` equals the taker's tx hash. Taker = `users[0]` if `side == "B"`, else `users[1]`. Checked: 69 of 69 non-zero-hash trades linked, the tx sender was the taker every time, and `trade.time == tx.time` exactly.
- **Maker:** only the address. The replay picks the maker order itself (that user's resting order at `px`, by time priority), and that pick is what gets checked.
- **Zero-hash trades** (`hash` all zeros, ~18%, ~45/min): liquidations, TWAP slices, triggered orders. They have no tx. They are applied as outside fills at the end of their block and never scored.

**Worked example** (BTC, block 1224258712, tx index 4287):

```json
tx:    {"time": 1794894400551, "user": "0xf6d9…3e13",
        "action": {"type": "order", "grouping": "na", "orders": [{"a": 0, "b": false,
                   "p": "64764", "s": "0.00906", "r": false,
                   "t": {"limit": {"tif": "Ioc"}}, "c": "0x7754…55dd"}]},
        "hash": "0x0f62fec9…f7a9", "error": null}
trade: {"coin": "BTC", "side": "A", "px": "64893.0", "sz": "0.00906",
        "time": 1794894400551, "hash": "0x0f62fec9…f7a9", "tid": 778960509408088,
        "users": ["0xa3b5…0600", "0xf6d9…3e13"]}
```

The hashes match. `side` is `"A"`, so the taker is `users[1]` = `0xf6d9…3e13`, the tx sender. The taker sent an IOC **sell limited at 64764** and filled the full 0.00906 BTC at **64893.0, the resting buyer's price**. That is price-time priority working as specified: the fill is at the maker's price, never the taker's limit. The maker is `0xa3b5…0600`; which of its orders filled is for the replay to determine.

## 5. Order ids

- One **global** counter across all markets. Each placed item uses the next id, including each `modify` and `batchModify` item. But **1–5% of ids are used by events not visible in blocks**, so ids can't be predicted exactly.
- **85% of BTC/ETH cancels are by cloid**, and 61% of batchModify targets are cloids. These resolve exactly through a (user, cloid) index.
- The other 15% are cancels by int oid. Estimate ids from `anchors.json` (live `orderStatus` answers) plus counted placements, then take the user's nearest resting order on that asset within tolerance. Unresolved cancels are their own metric.
- The engine uses its own internal ids. Hyperliquid oids are only lookup keys.

## 6. Orders resting before the window

They are invisible: placed before the first block, so neither their oid, their owner nor their queue position is known.

**Handling:** seed the book from the L2 snapshot nearest the window's first block (in the test window it was 0.9 s earlier), with one anonymous placeholder order per level for the snapshot size, ahead of all window orders. Then warm up for 1,740 blocks (~2 min) before scoring anything. Score only fills whose taker tx is in the window and whose maker order was placed after the window started.

**Cost:**
- Fills against old orders are never scored, which biases scoring toward short-lived quotes. That's acceptable, since 98% of orders are ALO quotes that refresh quickly.
- Queue position inside each seeded level is unknown.
- Cancels of pre-window orders find nothing ("unknown pre-window order" category).

The per-minute match rate shows whether the warm-up is long enough. It should rise during warm-up and stay flat after.

## 7. Volume and cost

| measure | value |
|---|---|
| one block | ~1,450 txs, ~84 KB gzipped (300 blocks: 434,932 txs, 25.2 MB, 20.8 s of chain time) |
| one window (6,100 blocks) | ~640 MB, ~95 min to fetch from one IP |
| back-to-back capture | ~15 windows/day ≈ 9.6 GB/day (server disk: prune to the Mac every ≤ 3 days) |
| trades | ~250/min for BTC+ETH combined (75 k over 5 h), 18% zero-hash |
| data cost | $0 (public endpoints); the only cost is the EC2 instance |

## 8. Go / no-go

**GO** for exact replay, within windows, with these stated limits:

1. Rejects are separable: action-level errors are recorded, and item-level outcomes come from the engine's own book. Margin rejections are unobservable and are reported, not modeled.
2. Fills link: taker by hash (100% of checked non-zero-hash trades), maker by address, with the order chosen by time priority.
3. Unreproducible activity (zero-hash trades, triggers, TWAP slices, unexplained oids) is applied as outside fills or reported as its own mismatch category.

**To be confirmed by the replay:** G0's one-block BTC replay (fills compared on taker, price and size), and at G1 whether listing order is execution order.
