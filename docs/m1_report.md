# M1 · Transfigure: checkpoint report (Nov 22)

## What was built

| file | role |
|---|---|
| `replay/sim/config.py` | `SimConfig`: every sampler and mask parameter; `SimConfig.full_visibility()` for the property test |
| `replay/sim/rules_engine.py` | the simulated chain, written from the rules (class sort per batch, ALO cross reject, 400 ms priority, margin, self-trade cancels resting, reduce-only silent cancels, $10 minimum, global oids incl. burns) |
| `replay/sim/sampler.py` | agents (market makers, bots incl. broke ones, retail incl. reduce-only and broke) + hidden events (pre-window orders, batch cuts, oid burns); one seeded RNG |
| `replay/sim/mask.py` | writes a chunk in the REAL formats (explorer blocks with batches merged, window.json, anchors.json, clearinghouseState.json, recorder trades / bbo / l2Book / sf4 / fast / blocks) |
| `replay/sim/truth.py` | truth log (lz4 JSON lines; parquet needs pyarrow: Q6) |
| `replay/sim/generate.py` | `python -m replay.sim.generate <root> --seeds 0-199 --set sim200` |
| `replay/sim/calibrate.py` | real vs simulated statistics with gaps -> `docs/m1_calibration.md` |
| `tests/chain_examples.py` | problem-page Examples 1, 2, 5, 6, 7 as chain data (shared with the engine tests) |

Suites (data, outside the repo): `~/hl-data/sim/m1/sim200` (200 seeds, 427 MB) and `~/hl-data/sim/m1_fv/fv20`
(20 seeds, full visibility, 53 MB).

## Tests (all pass)

- `tests/test_sim_rules.py` (11): every problem-page Example reproduced exactly by the simulated chain (incl. 2, 5, 7
  that need hidden account state); P5 class sort per batch; 400 ms window; self-trade; oids incl. rejected items and
  burns; IOC no-fill and $10 minimum; cloid cancels and action errors.
- `tests/test_sim_puzzles.py` (7): P1-P6 worked examples built in the simulator, masked, read back with the real
  loaders; visible output equals the puzzle's "you see", and the hidden truth is among `puzzles.py`'s answers
  (P1: one of the 12 queues; P2: chain decisions = puzzle answers and the oracle's; P3: resolves to c; P4 both cases
  incl. the corrected after=(6.5, 4); P5: cut at 1; P6: both silent cancels).
- `tests/test_sim_generate.py` (3): same seed -> same bytes (windows, truth, recorder); masked files consistent with
  the truth (every printed trade, anchors point to the true oids, oids never visible in blocks); **property test:
  full visibility -> the replay engine's strict recall is exactly 1.0**.
- `tests/test_examples_engine.py` (14): the Examples through the replay engine on both backends (D26).

## Property test and disagreements (full visibility, 3 chunks, 403 scored txs)

| replay configuration (chain rules: class order, stp=maker, docs priority) | strict |
|---|---|
| plain engine, no trade corrections | **100.0%** (books identical after every block) |
| + baseline heuristics (ledger, touch reconcile, oid bracket, evict-arrival, ...), no corrections | 98.3% |
| + block-end trade corrections (= the full baseline) | 92.0% |

Disagreements written down: int-oid cancels need anchors (D30); the trade-correction "queue" rule deletes real orders
when the real maker also has a newer quote at that price (D31, Q7); baseline heuristics misfire on perfect data (D31).

## Strict-recall baseline of the current engine on simulated chunks (sim200, 200 chunks, 42,000 blocks)

Paired per-tx maker-strict, chunk-cluster bootstrap 4000 (`report/validate/m1_sim200_cmp`):

| scope | n txs | baseline (listing, stp=taker) | chain rules (class, stp=maker) | diff [95% CI] |
|---|---|---|---|---|
| contested (union) | 11,011 | **0.376** | 0.619 | +24.4 [+23.5, +25.3] |
| contested (real makers) | 7,625 | 0.499 | 0.771 | +27.2 [+26.0, +28.4] |
| uncontested | 13,873 | 0.565 | 0.587 | +2.3 [+1.7, +2.8] |
| all scored | 24,884 | 0.481 | 0.602 | +12.1 [+11.5, +12.6] |

Pooled per-fill strict: 48.4% (baseline) vs 60.4% (chain rules). Caveat: the simulated chain USES class order and
stp=maker by construction, so this shows what those rules are worth IF the chain works that way; it does not say
which is real (that is still your decision, AGENT_PLAN §5.3). The simulated baseline (37.6% contested) is well below
the real one (~59%) because the simulated market differs (calibration below).

## Calibration (`docs/m1_calibration.md`, `python -m replay.sim.calibrate ...`)

| statistic | real (dev20) | simulated | gap |
|---|---|---|---|
| touch order-count change, median / p90 | 2 / 10 | 1 / 2 | levels hold far fewer orders in the sim |
| touch size change / size, median / p90 | 0.14 / 1.06 | 0.20 / 0.73 | |
| trades per block | 0.56 | 2.88 | ~5x too many takers |
| post-only margin rejection rate | 1.67% | 0.16% | too few broke quoters |
| tooManyOpenOrders rate | 0.14% | 0% | not modelled |
| engine-only trade share | 30.2% | 14.6% | |

Not tuned yet (Q8): proposed first pass = taker rates x0.2, more and smaller market-maker orders per level, a broke
quoter agent for the post-only margin rate, a per-user open-order cap.

## Budget table (M1)

| limit | default | measured | hit? |
|---|---|---|---|
| generating one chunk | <= baseline replay time of one chunk (17.7 s) | 0.29-0.48 s | no |
| simulated data on disk | <= 5 GB | 480 MB (sim200 427 MB + fv20 53 MB) | no |
| default suite | 200 chunks | 200 | no |
| configurations searched on dev20 | <= 20 | 0 (no tuning) | no |

## Not done / open

- Calibration tuning (Q8); parquet truth logs (Q6); the trade-correction finding (Q7).
- Not modelled: modify / batchModify, scheduleCancel, liquidations / TWAP / triggers, open-interest caps, oracle band
  (D29).
