# L55 audit: raw price / size STRING joins ("X.0" vs "X")

Yuta, 2026-11-26 (data-streams). Scope: every place in analysis/, collect/, data/ and engine_py/ that compares or keys
on a price or size string from two different sources.

## The formats differ by source (measured)
| source | BTC px | ETH px | sizes |
|---|---|---|---|
| explorer action `p` / `s` (signed by the trader) | integer text, "60914" (8,060 of 8,061 items) | "1692.2"; 68 of 1,788 are integer text, "1692" | "0.19897"; integer text "1" for whole sizes (BTC 304 / 8,061, ETH 20 / 1,788) |
| API feeds: trades `px`/`sz`, user_orders `limitPx`/`origSz`/`sz`, historicalOrders | "60975.0" (always ".0") | "621.1"; whole prices "1692.0" | whole sizes "355523.0" |
| SQD replica_cmds action | the signed payload, same text as the explorer | same | same |

Sample: 60 devfast blocks (explorer) and 1 recorder hour (user_orders, 2026-11-26 05Z).

So any raw-string join across explorer/SQD and the API misses:
- **every BTC price**;
- **whole-number ETH prices and whole sizes**.

## Join sites checked
| file:line | join | verdict |
|---|---|---|
| data/parse.py:97, 488, 504 | px_to_ticks / sz_to_lots | safe (integer ticks / lots) |
| data/oids.py:100, 110, 196, 205, 246; 210-213 (`_put_px` -> ticks) | Decimal / ticks | safe |
| data/oid_facts.py:89, 106 | Decimal | safe |
| data/oid_facts.py:156 | `_norm` = Decimal.normalize | safe |
| analysis/oid_bracket.py:59, 90 | `norm` = Decimal.normalize | safe |
| analysis/anchor_density.py:56/73, oid_tx_eval.py:86/97 | Decimal keys (Decimal("1") == Decimal("1.0"), equal hashes) | safe |
| analysis/outside_feed.py:49, 108, 136 | px_to_ticks | safe |
| analysis/p_priority.py, collect/capture.py:564-658 | float | safe for these magnitudes |
| analysis/validate_replay.py `fmt_fills` -> suspect_makers.py:31 | both columns formatted from ticks by the same function | safe |
| analysis/cancel_trace.py:269-274, g1_queue.py:348 | ticks vs ticks | safe |
| analysis/sqd_truth.align | json.dumps of the explorer item vs the SQD item (both are the signed payload) | safe: 163,665 / 163,674 aligned. The 9 misses are perpDeploy, SystemSpotSendAction and approveAgent, which SQD doesn't carry. |
| **analysis/oracle_truth.py:118-120 (`Truth.find`, shape path)** | **`x["orig_sz"] == sz`: API origSz text vs explorer `s` text, raw** | **DEFECT** (px is normalized by `norm`, size is not) |
| analysis/oracle_truth.py:110 (cloid path) | px normalized; size not compared | safe |

## The one defect: `Truth.find` shape path
For a placement with no cloid, the truth record is found by (user, coin, side, norm(px), block time) and then
`orig_sz == sz` as raw strings. A whole size ("1" in the action vs "1.0" from the API) or a trailing-zero size never
matches, so that placement gets no O1 / O3 truth row.

Exposure: no-cloid placements are 583 of 278,310 BTC/ETH limit placements in 6 devfast chunks (0.2%). Of those 583,
just 7 have a whole or trailing-zero size.

Measured impact: I rebuilt the devfast truth twice, once as-is and once with the size compared as Decimal
(scratch runs, today's data). The O1 rows are **42,587 vs 42,587**, O2 94,209 vs 94,209 and O4 2,666 vs 2,666:
**zero difference**. The rare no-cloid whole-size placements had no truth record to find anyway.

**Verdict: L55 is closed.** No join in the pipeline loses data to "X.0" vs "X". The one raw compare is a latent
defect with zero measured effect; the fix is proposed for unified, not applied here.

Fix (one line, `Decimal(str(x["orig_sz"])) == Decimal(str(sz))`): NOT applied. Accuracy is frozen at B3 and the
truth files are oracle/diagnostic inputs. Applying it would change files that mahito's frozen results were built on,
so I'm proposing it instead (mail to mahito).
