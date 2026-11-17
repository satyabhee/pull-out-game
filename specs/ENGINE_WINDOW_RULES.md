# Engine rules for captured windows (spec notes, not implemented)

Each window lives in `windows/<start>-<end>/` with `window.json` (start/end height, block times, `warmup_end_height`, status) and `anchors.json` (orderStatus answers). Facts behind these rules: `G0_REPORT.md`.

1. **Warm-up.** Replay each window from its first block, but score nothing before `warmup_end_height` (start + 1,740 blocks ≈ 2 min).
2. **Placeholder liquidity.** At window start, take the BTC/ETH L2 snapshot (`book_<COIN>.jsonl`) nearest the first block time. Put one anonymous placeholder order per price level for the snapshot size, **ahead of** every order placed in the window. Fills against unknown makers use placeholders up first.
3. **Outside fills.** Recorded trades with an all-zero hash (~18% of trades) have no transaction. Apply them as forced fills against the named maker at that price, at a fixed position: **end of the block whose time equals the trade time** (one rule; log every application). Don't count them in the match rate.
4. **Scoring.** Score only fills whose taker tx is in the window (link `trade.hash` == tx hash; taker = `users[0]` if side B else `users[1]`) and whose maker order was placed after window start. Report the match rate per minute of window time; it should rise during warm-up and stay flat after it.
5. **Order ids.** Resolve cancels/modifies by (user, cloid) first (~85% of BTC/ETH cancels). For int-oid cancels, estimate ids from `anchors.json` plus counted placements (global counter, +1 per order / modify / batchModify item; ~1–5% unexplained drift, so match the nearest resting order of that user and asset within tolerance). Report unresolved cancels as their own metric.
6. **Statistics.** Cluster standard errors by window (windows are the independent unit; blocks within a window are not).
7. **Window status.** Use `complete` windows; `partial` ones only if the missing heights are reported; skip `archived_cap`.
