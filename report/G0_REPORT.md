# G0 report: does the Hyperliquid data work for a BTC/ETH replay?

**Verdict: PASS WITH CAVEATS.** The data can be decoded and fills link to their taker tx 100% of the time. Two caveats change the plan: (1) the explorer caps *archived* blocks at 100/day per IP, so blocks must be fetched while they are fresh, and (2) order ids can't be reproduced exactly by counting: about 1–5% of ids are used by events not visible in the blocks.

Data used (all on the Mac, `~/Downloads/server_data/`):
- `blocks/1223994849-1224012849/`: 10,431 real blocks, 00:31:23–00:44:00 UTC Nov 17 (12.6 min). **4,607 more files there are not blocks**; see Gotcha 1.
- `blocks/live-1224258679-1224258978/`: 300 blocks fetched fresh from the Mac (20.8 s, ~05:55 UTC Nov 17).
- `hl_data/`: recorder output 00:47–05:55 UTC Nov 17, 76,528 BTC/ETH trades.

---

## 1. Asset map

| coin | `a` |
|---|---|
| BTC | **0** |
| ETH | **1** |

Any other perp: POST `https://api.hyperliquid.xyz/info` `{"type":"meta"}`; the asset number is the coin's index in `universe` (235 entries on Nov 17). Assets ≥ 10000 (e.g. 110014) are spot or builder-deployed markets. Ignore them for the BTC/ETH book.

## 2. Decoder table (what the BTC/ETH replay must handle)

Every tx: `{time, user, action, block, hash, error}`. `time` = block time (ms). `error` is `null` on success.

Counts below are BTC/ETH items in the 12.6-min old window.

| action `type` | fields | count | effect on the book |
|---|---|---|---|
| `order` | `orders:[{a,b,p,s,r,t,c?}]`, `grouping`, `builder?` | ~2.43 M items | **Place** each item (needs a new oid). `b`=is buy, `p`/`s`=price/size strings (compare as `Decimal`), `r`=reduce-only, `c`=cloid (hex string, optional) |
| `cancel` | `cancels:[{a,o}]`, `f?` | 175 k | **Cancel** by (user, oid `o`) |
| `cancelByCloid` | `cancels:[{asset,cloid}]`, `f?` | 958 k | **Cancel** by (user, cloid). Note the key is `asset` here, not `a` |
| `modify` | `oid` (**int or cloid string**), `order:{...}` | 1.3 k | Cancel the target, then place `order` as a **new order with a new oid** (id evidence below) |
| `batchModify` | `modifies:[{oid, order}]` | 23 k items | Same as `modify`, per item. `oid` is a cloid string in 61% of items |
| `twapOrder` | `twap:{a,b,s,r,m (minutes),t (randomize)}` | 1 | Not placed directly. The exchange later creates slice orders that are **not** in the blocks → unreproducible |
| `scheduleCancel` | `time?` | 31.8 k (all assets) | Dead-man switch: cancels all the user's open orders at `time`; no `time` = clear the schedule. Reproducible only if the engine tracks it. The asset isn't in the action |
| `updateLeverage`, `updateIsolatedMargin` | `asset, ...` | — | No book effect (margin only; matters only if you model margin rejections) |
| `noop`, `evmRawTx`, oracle/validator/transfer actions, etc. | — | — | **Ignore** |

**Order type `t`:**
- `{"limit":{"tif":"Alo"}}`: post-only (~98% of BTC/ETH orders). Reject if it would cross.
- `{"limit":{"tif":"Ioc"}}`: take what's available, then cancel the rest.
- `{"limit":{"tif":"Gtc"}}`: take what's available, then rest.
- `{"limit":{"tif":"FrontendMarket"}}`: a UI market order. Treat as IOC at price `p` (the UI sets a wide slippage price).
- `{"trigger":{"isMarket","triggerPx","tpsl":"tp"|"sl"}}`: stop or take-profit. **Doesn't enter the book** until the mark/oracle price crosses `triggerPx`, then becomes a market or limit order. The trigger moment depends on the oracle, not the trades → unreproducible.

**`grouping`:**
- `"na"`: normal.
- `"normalTpsl"` / `"positionTpsl"`: TP/SL attached to an entry or position (306 actions).
- `{"p": N}`: on **88%** of BTC/ETH order actions; N ranges 2…302+. This isn't in the public API docs. It looks like a priority bid: the first block's txs were listed in descending `p`. **Open question:** whether listing order = execution order.

**`builder`:** a builder-fee field (907 actions). No book effect.

## 3. Order-id rule

**Rule (medium confidence):** one **global counter across all markets**. Every new-order item uses the next id(s): each `order` item, each `modify`, and each `batchModify` item, so **modify creates a new oid**. Ids are monotonic in block-listing order. **But about 1–5% extra ids are used by events not visible as block txs**, so ids can't be predicted exactly by counting.

Evidence (`g0_live.py` → `g0_check.py` section 4):
- 136 of 150 fresh `orderStatus` lookups answered (orders a few minutes old). Old orders all return `unknownOid` (0/120 at ~5 h old), so anchors must be taken live.
- 48 anchors placed unambiguously in the 300 live blocks; 47 consecutive pairs.

| counting rule | exact pairs | residual (ids − counted) | ids per counted event |
|---|---|---|---|
| A: order + modify + batchModify items (ok + errored) | 13/47 | min 0, median 20, max 719 | 1.012 |
| B: same, successful txs only | 13/47 | min 0, median 20, max 724 | 1.012 |
| C: `order` items only | 6/47 | median 101 | 1.112 |
| + TWAP actions | no change | | |

- An API-free cross-check (highest oid cancelled per block vs. counted placements, 31 steps of 300 blocks) gives the same: rule A median 1.031 (p10 1.004, p90 1.046). Without modifies/batchModifies the ratio jumps to 1.12, which supports "modify uses an id".
- **Residuals are never negative**, and they occur **inside a single block with no cancels** (e.g. 380-id gap, 85 unexplained). So it isn't cancels or non-order txs. Candidates, not yet tested: TWAP slices, triggered TP/SL orders getting a new id, liquidation/ADL/system orders, or execution order ≠ listing order within a block.
- Errored order txs: too rare (1,684 "Action already expired" in 12.6 min) to tell whether they use an id; rules A and B are indistinguishable.

**What this means for the engine:** don't depend on predicting oids.
- **85% of BTC/ETH cancels are `cancelByCloid`**, and 61% of batchModify targets are cloids. Index resting orders by (user, cloid) and these resolve exactly.
- For the 15% of cancels by int oid: keep each user's resting orders per asset in placement order and estimate each order's oid as (last anchor + counted events). Resolve a cancel to the user's order on that asset whose estimated oid is nearest and ≤ the target, within the residual tolerance. Report the ambiguous cases as a metric.
- Better long-term: add an anchor collector to the live pipeline (fetch fresh blocks + `orderStatus` within minutes) so every replay window has anchors every few blocks.

## 4. Fill linking rule

Recorded trade: `{coin, side, px, sz, time, hash, tid, users:[buyer, seller]}`.

- **Taker:** `trade.hash` == the hash of the **taker's** tx. In the live window, **69/69 non-zero-hash trades linked (100%)**, the tx sender was the taker in 69/69, and `trade.time == tx.time` exactly. Taker = `users[0]` if `side == "B"`, else `users[1]`. Linked taker orders: 22 IOC, 6 FrontendMarket, 1 GTC. One taker tx can sweep several makers (up to 18 trades share one hash).
- **Maker:** trades carry **no maker oid**, only the maker's address. The replay must pick the maker order itself (that user's resting order at `px` with time priority); this becomes the fill check.
- **Zero-hash trades:** **16 of 85 (19%)** in the live window; 13,910 of ~75k (18%, ~45/min) across the recorder period. Small sizes (e.g. 0.00018 BTC) and many different address pairs. Most likely TWAP slices, triggered orders and liquidations: exchange-generated orders with no user tx. **Not yet classified.**
- The old 12.6-min block window doesn't overlap the trades (trades start 00:46:59, blocks end 00:44:00). That's why the first run showed 0.11%; the real rate is 100%.

## 5. Unreproducible activity (BTC/ETH)

| category | count | rate | source window |
|---|---|---|---|
| trigger orders placed | 3,362 | 267/min | old 12.6 min (most never trigger; only triggered ones matter) |
| twapOrder actions | 1 | 0.08/min | old 12.6 min (each one creates many hidden slices) |
| scheduleCancel (all assets) | 31,818 | 2,523/min | old 12.6 min (reproducible if tracked) |
| zero-hash trades (no taker tx) | 13,910 | ~45/min, 18–19% of trades | recorder 00:47–05:55; live window 16/85 |
| unexplained oid use | ~1–5% of ids | — | live anchors |
| errored order txs ("Action already expired") | 1,684 | 134/min | old 12.6 min (no book effect) |

## 6. Gotchas for the engine spec

1. **The explorer caps archived blocks: "More than 100 archived blocks queried in one day"** (per IP). The server's fetch got 10,353 blocks and then **saved 4,607 error messages as `.json.gz` "blocks"** (heights 1224005202 onward). The old `fetch_blocks.py` only compared `len(txs)` to `numTxs`, and an error has neither. It's fixed now: it refuses error responses and stops on the cap. **Delete those 4,607 files on the server before refetching**, or reruns will skip them as already on disk. The age at which a block becomes "archived" isn't known yet; 2-hour-old blocks still loaded from a fresh IP. In practice, fetch blocks near-live. At ~1.2 blocks/s against ~14.5 produced/s, one IP covers only ~8% of wall time, so plan short windows.
2. The 240-day history found on Nov 16 is real but **limited to 100 blocks/day/IP**, which is useless for whole windows. Volatile days must be captured live, not picked after the fact.
3. `orderStatus` forgets orders within hours (busy users first). Anchors must be collected within minutes.
4. `modify`/`batchModify` `oid` can be an **int or a cloid hex string**.
5. `cancel` uses `a`/`o`; `cancelByCloid` uses `asset`/`cloid`. Most cancels carry `"f": true` (meaning unknown; no book effect assumed).
6. Price/size are strings with varying formats (`"64500"` vs `"64500.0"`). Always use `Decimal`.
7. Block tx listing may be sorted by `grouping.p` (priority). Confirm listing order = execution order before relying on it for the "first come" vs "cancels first" comparison.
8. Errors are action-level only. ALO crosses and IOC no-fills in `order` actions are **not** reported; infer them from the book. `modify` does report them ("Post only order would have immediately matched, bbo was …", "Cannot modify canceled or filled order").

## 7. Open questions (with evidence above)

1. What uses the extra 1–5% of oids? Test: in a fresh window, record zero-hash trades and trigger hits per block and regress the per-pair residuals on them.
2. What's the "archived" age threshold? Test from a fresh IP: blocks at 1, 2, 4, 8 h old.
3. Is block listing order the execution order? Same anchor method: a negative residual would prove reordering.
4. What are zero-hash trades? Match them against `twapOrder` activity and the liquidation users.

## 8. Commands used (rerunnable, Mac, from `~/Downloads`)

```bash
# asset ids
curl -s -X POST https://api.hyperliquid.xyz/info -H 'Content-Type: application/json' -d '{"type":"meta"}'

# copy server data down (replace <SERVER_IP>)
rsync -az -e "ssh -i ~/Downloads/hype-nodevm-key.pem" ubuntu@<SERVER_IP>:~/hl/blocks ubuntu@<SERVER_IP>:~/hl/hl_data ~/Downloads/server_data/

# fresh blocks + live order-id anchors (Mac IP; one explorer client at a time)
python3 g0_live.py ~/Downloads/server_data 300

# newest trades, then the full check (~8 min, offline; reads cached orderStatus answers)
rsync -az -e "ssh -i ~/Downloads/hype-nodevm-key.pem" ubuntu@<SERVER_IP>:~/hl/hl_data ~/Downloads/server_data/
python3 g0_check.py ~/Downloads/server_data > ~/Downloads/server_data/g0_run2.txt
```

Outputs: `server_data/g0_run1.txt` (first run), `g0_run2.txt` (final), `g0_live_run.txt`, `g0_cache.json` (orderStatus answers), `g0_anchors.json`.
Scripts: `g0_check.py` (all four questions, stdlib only), `g0_live.py` (fresh blocks + anchors), `fetch_blocks.py` (now rejects error responses).

---

## 9. Engine replays real blocks (G0, second half)

The Python reference engine (`engine_py/`, implementing `specs/01-book.md`) replays real explorer blocks end to end:
`uv run python -m engine_py.replay ~/Downloads/server_data/blocks/live-1224258679-1224258978 --quiet`

- **One block** (height 1224258679): 26 accepted, 104 rejected (`POST_ONLY_WOULD_TAKE`), 4 IOC remainders cancelled, 20 unresolved cancel targets, state hash `d8e14c57291998e6…`.
- **300 consecutive blocks** (20.8 s), project ordering, empty starting book: no engine errors; 38,814 orders accepted, 29,251 rejected, 68 fills, final book uncrossed (BTC 64904.0 / 64905.0). Takes 110 s (~0.37 s/block, mostly the per-block copy that makes blocks all-or-nothing). `first_come` ordering: also 68 fills, 29,614 vs 29,419 resting BTC orders.
- **Accepted vs rejected:** the engine says which orders it accepted and rejects the rest with a reason. The only non-post-only rejections were 13 orders Hyperliquid would also refuse (size 0, price 0, or `63597.59` with too many decimals for BTC), so the unit config (BTC tick 0.1 / lot 0.00001, ETH 0.01 / 0.0001) fits every real order.
- **Why so many post-only rejections:** replaying from an **empty** book. Example: a market maker's sells at 64890–64893 rest on our empty book, so every real bid at 64894–64906 looks like it crosses. On-chain, the real resting bids (~64900) made those sells the ones rejected. This is the cold-start effect that warm-up + placeholder liquidity (`specs/ENGINE_WINDOW_RULES.md`) address. It's G1 work, not an engine bug.
- **Unresolved targets** (15,622 by cloid, 3,683 by oid in 300 blocks) are mostly cancels of orders placed before the window or wrongly rejected at cold start. Oid cancels can't resolve until `exchange_oid` estimates from `anchors.json` are wired in (G1).

**G0 status:**

| condition | status |
|---|---|
| Link fills to orders | Real fills link to their taker tx by hash (69/69). The engine records each taker's tx hash. |
| Tell rejected from accepted orders | The chain doesn't record per-order outcomes; the engine decides them from the book. |
| Python engine replays a real block | Done. |

Caveat on "one hour of data": the explorer's rate limit (~1.1 blocks/s against ~14.5 produced) makes a full contiguous hour (~52,000 blocks, ~13 h of fetching) impractical. G0 was shown on a 20.8-s window, and the capture service now collects ~7-minute windows around the clock.
