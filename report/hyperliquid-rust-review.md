# hyperliquid-rust (HLX Atlas) review against our open replay problems

Date: 2026-11-19. Reviewer: research pass, read-only on the external material.

## What was actually available

- `github.com/lastdotnet/hyperliquid-rust` **does not exist publicly** (`Repository not found`). The public repo is
  `lastdotnet/hyperliquid-rust-docs`, described as *"Public docs export for the private hyperliquid-rust research
  repo"*. Its README says: *"This public bundle contains documentation and normalized evidence, not the private Rust
  source."* Cloned (depth 1, commit `506f1e6`, 2026-10-11) into `~/hyperliquid-rust`.
- The `lastdotnet.github.io/hyperliquid-rust-docs/` URL returns 404; the same site ships in the repo (`index.html`,
  `reference.html`, `data.json`).
- **So the hl-engine crate (matching, order handling, state) could not be read.** What the bundle has: 20 prose
  chapters, an inventory of the 92 actions with the private source's function names and `file:line` locations
  (call graph, no bodies), 18 recovered block phases, and a gap ledger.
- **License:** no license file covers the docs content. The only license is MIT for vendored Three.js
  (`vendor/THREE-LICENSE.txt`, `data.json` → `thirdParty`). Default copyright applies: we can read and cite it, not
  copy it. Nothing was copied into our repo.
- Their evidence model is "official Mainnet 113" (snapshot 2026-10-09), bound to private source head `35d72fb`.

Citations below use `data.json:<path>` (field path in the bundle) and `src:<file>:<line>` (a location in their
private source, as the bundle states it; we cannot open it).

## Summary

| # | Question | Their answer | Matches us? | Action |
|---|---|---|---|---|
| 1 | When does the oid counter advance; what burns ids invisibly? | **Not documented.** One lead: every `cancel` dispatch calls `consume_cancel_oid_burns_for_round` after `apply_cancel_batch`. The name says cancels burn oids per round. | Unknown. Our counting rule ignores cancels (`data/oids.py:4-7`). | **Test** whether cancel items explain our 1–5% drift (below). |
| 2 | In-block ordering | **Not recovered.** Phase "incoming batch preparation/storage": *"preparation, ordering and execution callbacks remain unclosed"*. | No contradiction. Our class sort (`analysis/validate_replay.py:65-90`) stays as is. | None from them. |
| 3 | Cancel by oid / cloid; modify | Cancel and modify semantics are labelled *"binding_only_handler_semantics_unreviewed"* / *"unreviewed"*. Modify and batchModify have explicit "unmodeled" guards. Docs describe an oid index and a cloid tracker (`OidNamespace`, `CloidTracker`). | Nothing to compare. Our modify = cancel + new order, new oid, back of queue (AGENT_PLAN line 85; `engine_py/matching.py:78-84`). | None from them; test modify priority ourselves. |
| 4 | ALO acceptance | `order` dispatch goes through `with_action_order_conversion` in **`post_only_window.rs`** before `process_order_action`. No semantics given. | Unknown. Our gate reads the real touch from the previous block (`engine_py/replay.py:817`). | Low-priority lead: is there a time window where orders are forced post-only? |
| 5 | Queue priority details | Book = levels + slot arena + oid index; *"price-time priority, with additional native priority metadata carried where the model requires it"* (`src:crates/hl-engine/src/book_priority.rs:1`). Trigger and reduce-only chronology is a listed gap. | Partly. We use pure price-time. Our parser keeps `grouping.p` and uses it only to order GTC/IOC takers inside a block (`priority_policy`, `analysis/validate_replay.py:92`). | **Test** whether `p` affects queue position among resting orders (below). |
| 6 | Verified vs inferred | Explicit 4-axis ledger. Only 3 "verified scopes" (outcome reference price, book publication and trigger activation, portfolio repair), none about core matching. All 92 actions: *"exact serde bindings but unreviewed complete semantics"*. | n/a | Treat everything above as inferred. |

**Bottom line:** the bundle doesn't answer our questions. Its value is two leads: cancels may consume oids, and the
book carries priority metadata beyond price-time. Both can be tested with our own real data at no cost.

## Details

### 1. Order-id assignment
- `data.json:actions[name=cancel].dispatch.direct_resolved_helpers`: `apply_cancel_batch`
  (`src:crates/hl-engine/src/exchange.rs:22066-22097`), then `consume_cancel_oid_burns_for_round`
  (`src:exchange.rs:22186-22235`), called from `src:actions.rs:2259`. **Only the name is evidence.** It suggests
  some per-round oid consumption tied to cancels. It may also mean "cancel requests targeting burned oids" rather
  than "cancels consume ids". Confidence: low.
- `src:crates/hl-engine/src/oid_arity.rs` exists (`oid_arity::enabled`, line 45, called from account actions). Name
  only. `data.json` typed-state audit lists `OidNamespace` and `CloidTracker` stored as rmpv values.
- No statement on TWAP slices, liquidations, rejected or failed ALO, spot/HIP-3, or vault actions consuming ids.
- Ours: `data/oids.py:3-7`, every successful order item, modify and batchModify item consumes one id, with 1–5%
  unexplained. Measured today: between consecutive reference points, counting is exact in 86.5% of gaps
  (`analysis/oid_bracket.py`). Also: rejected orders (`badAloPxRejected`, `perpMarginRejected`) **do** carry oids
  in orderUpdates, so they consume ids too.
- **Deterministic oids from blocks alone:** nothing in the bundle enables it.

### 2. In-block ordering
- `data.json:phases[top_level-3]` "incoming batch preparation/storage": *"structural call/order known ...
  preparation, ordering and execution callbacks remain unclosed"*.
- `data.json:phases[top_level-0]`: *"counter reset and delayed ordering pinned"*. The execution chapter: begin-block
  work, *"transaction-index reset"*, matured delayed rows, then incoming batches.
- Begin-block spans run **before** user actions (`data.json:phases[begin_block_spans-*]`): funding,
  `cancel_aggressive_orders_at_oi_cap`, `prune_book_empty_user_states`, oracle/mark refresh, BOLE liquidations.
  These can remove orders at block start, which we don't model. Probably rare for BTC/ETH.
- Ours: `class_policy` (class, tx, item) and `priority_policy` (`analysis/validate_replay.py:86-98`). No conflict.

### 3. Cancels and modify
- `cancel` and `cancelByCloid`: `nativeStatus = binding_only_handler_semantics_unreviewed`, implementation, tests
  and runtime all "unreviewed" (`data.json:actions[index 3, 4]`).
- `modify` (`index 12`, 7 guards) and `batchModify` (`index 2`, 3 guards) poison on unmodeled cases, e.g. *"a parent
  with pending children was not canceled"* and `ModifyTarget::UnmodeledTriggerToLimit`. These are TP/SL parent-child
  cases, not queue position.
- A doc excerpt on OID resolution: *"All three resolve the OID through the current physical book index first ... a
  missing owner, missing OID, Free resting slot or owner mismatch returns without"* effect. That's consistent with
  our "cancel of an unknown order is a no-op".
- **Queue position on modify: not stated.**

### 4. ALO acceptance
- `order` path: `with_action_order_conversion` (`src:post_only_window.rs:242-247`), then `process_order_action` /
  `process_order_with_collateral` (`src:exchange.rs:15934`). A "post-only window" converting orders is suggestive
  (for example, a period where all orders are forced post-only), but unverified.
- When the book is read for the would-cross check: not stated.

### 5. Queue priority details
- `book_priority.rs`: *"Native priority metadata"* (`data.json:chapters[matching].sources`). The prose: *"the main
  matching surface uses price-time priority, with additional native priority metadata carried where the model
  requires it."*
- `gossipPriorityBid` action (`src:priority_auction.rs:318-334`, unmodeled guard).
- Reduce-only and trigger chronology is a known gap: *"trigger child reserved before reduce-only capacity cleanup"*
  (`data.json:scopes[tracker-ordered-state]`, runtime "divergent").
- Self-trade prevention: not mentioned.

### 6. Verified vs inferred (their labels)
- `data.json:counts`: 92 actions, 18 block phases, 6 known gaps, 3 verified scopes.
- Gaps chapter: of 27 semantic rows, 3 verified scopes; runtime: 21 unreviewed, 3 divergent, 3 scoped passes.
- **Everything relevant to us is "unreviewed" or name-only.** Nothing here was verified against the binary or
  traffic for matching, cancel, modify or ALO.

## A lead from our own data while checking #5

In the published viz case "Phantom at 63749" (chunk 1227073313-1227073522), the chain filled three resting bids at
63749 in this order: E, then F, then Y. Their placements: Y in block 1227073394 (`grouping.p = 130`), E and F in block
1227073395 (`p = 300` and `p = 171`). Placement-time priority predicts Y first; descending `p` predicts E, F, Y, which
is what happened. Makers D (`p = 66`) and X (`p = 3`), also resting there, never filled first.

**This is one example of 3 orders: descending p by chance is 1 in 6.** A wider test on the 34 newest chunks was
inconclusive. For pairs of makers filled consecutively at one price, using each maker's latest (or only) in-window
placement:

| proxy | time order right | p-descending right | n pairs |
|---|---|---|---|
| latest placement | 75 (46%) | 78 (48%) | 162 |
| only placement in window | 25 (42%) | 26 (43%) | 60 |

Both rules sit near 50%, even with unambiguous orders. That means the filled orders were mostly placed **before**
the window, so in-window placements can't test either rule. The two blocks are also exactly 71 ms apart, which
matches the 70 ms priority-fee bucket noted in our own `priority_policy` docstring. If the fee sort applies across
the bucket and not per explorer block, it could reorder resting post-only orders across adjacent blocks.

## Update (Nov 19, later): the p-priority test was run and the lead holds

Two corrections to the section above. First, trade ids (`tid`) are random, not sequential: only 319 of 1,117
multi-fill transactions had increasing tids in message order. The table above sorted fills by tid, so it measured
nothing. Real fill order is the order of fills in the trades message, and our pipeline now uses it (`Trade.seq`).
Second, the test was redone on fresh levels only: prices empty at the end of an earlier block, so every resting order
was placed in the chunk. `analysis/p_priority.py`, all 218 Mac chunks, chunk-cluster bootstrap:

| | pairs | result | 95% CI |
|---|---|---|---|
| sanity: time and p predict the same order | 272 | correct 100% | [100, 100] |
| equal p: time order right | 175 | 98.9% | [97.2, 100] |
| **primary: discordant pairs, p order right** | 179 (87 chunks) | **95.5%** | **[92.2, 98.2]** |

Exploratory: by placement gap, 0-100 ms 100% (n 70), 100-500 ms 93.7% (n 111), over 500 ms 0 of 2. Whether p
still wins against much older orders is open. Conclusion: at one price, the chain serves the highest `grouping.p`
first, with arrival as the tie-break. Our engine is pure price-time. The engine change (two variants, "p always" and
"p within 500 ms") is the next paired test. Viz tabs: "p priority: rule & fix" and "p priority at 63922".

## Proposed tests (using our real fills; no engine change)

1. **Does `p` set queue position among resting orders? (highest value.)** Use only orders whose real oid we know:
   tracked users' orderUpdates on BTC/ETH. Take pairs of known orders at the same price and side that both rest when
   a real taker fills one of them. Compare "filled first" against (a) lower oid (time) and (b) higher `p`. Repeat
   for pairs placed in the same block, in adjacent blocks within 70 ms, and farther apart. If (b) wins clearly
   within 70 ms and (a) wins beyond it, the bucket hypothesis holds. Data is already on disk. If p matters, it
   directly attacks the ~10–15% "right orders, wrong sequence" share of contested misses, and it would explain the
   viz case's open question. Expected effect: unknown until the test runs. I won't guess a number.
2. **Do cancels consume oids?** Between consecutive exact reference points (`analysis/oid_bracket.py`), regress the
   drift (oid gap − counted items) on the number of cancel items, rejected orders and other action types in the
   gap. If cancel items explain the drift, add them to the counting rule. That shrinks the bracket and should
   resolve more cancels. But resolving more cancels has not moved contested accuracy so far (bracket test: +1,147
   cancels resolved, 0 transactions changed), so the expected effect on contested maker accuracy is small.
3. **Modify priority.** For tracked users' modifies (oid stream gives old and new oids): does the new order keep the
   old queue position? Real fills tell us whether it's served before orders placed between the old order and the
   modify. Expected effect: small unless modifies are common among contested makers. Count them first.

## Ranked changes to our engine (not made; for decision)

1. **Fix the two position-guessing corrections**, from the viz case, independent of this review:
   `trade_learn` "queue" (`engine_py/replay.py:699-726`) deletes everything ahead of the filled maker, and the
   keep-known re-seed trims oldest-first (`engine_py/replay.py:184-187`). In the case, both removed live orders that
   the chain's real front consisted of. Candidate change: when the chain fills a maker that sits behind orders we
   hold, **move that maker's order forward** instead of deleting what's ahead, and trim **newest** unconfirmed
   orders at re-seed. Effect unknown. Needs one paired run on the 34 chunks, contested metric, CI.
2. **If test 1 shows `p` priority:** add a queue-priority rule and run it as a pre-registered variant.
3. **If test 2 shows cancel burns:** update the counting rule. Expected small effect on contested accuracy.
4. Begin-block cancellations (`cancel_aggressive_orders_at_oi_cap`): ignore unless a test shows BTC/ETH hits.

## Contradictions with our specs

None established. The bundle never states semantics that conflict with `specs/ENGINE_WINDOW_RULES.md` or AGENT_PLAN.
The `p`-priority question is ours (AGENT_PLAN line 90 already flags `grouping.p` as "looks like a priority bid"),
not theirs.
