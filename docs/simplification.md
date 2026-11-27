# Simplification log (branch `unified`, Nov 26)

Rule (Satya, Nov 26): only removals that leave dev20, dev-fast and dev_margin60 output byte-identical under B3. Each
change is listed with its check. Anything needing judgment goes to "Saturday candidates" and is NOT done.

## Done

| # | change | why safe | check |
|---|---|---|---|
| S1 | removed 6 unused imports (`data.parse.Stats` in engine_py/replay.py; `engine_py.types.ASSETS` in engine_py/multi_lite.py; `pathlib.Path` in replay/adapter.py; `chunk_name` in replay/sim/calibration_rounds.py; `argparse`, `sys` in data/tid_solve.py) | `ruff --select F401`: names never used; the modules have no import side effects | step 4 (Nov 27): B3 on dev20 / dev-fast / dev_margin60 from the unified tip, every CSV vs the B3 reference runs `i21b2_*` (result in docs/gate2_results.md and status) |

## Merge notes (mahito + data-streams, not simplifications)

- `Replay._taker_skip` margin check: data-streams' `_margin_short` (Decimal arithmetic, `MARGIN_RULES`) replaces
  mahito's float version; mahito's `ioc_model` fallback kept after it. B3 uses `--taker-reject margin`, so this path is
  in the headline: the final220 gate (byte-identical txs.csv) is the check.
- `suspect_status_truth` (data-streams) moved into `analysis/validate_replay.scoring_inputs` so the setup cache and
  the plain path load the same ALO truth.
- `--deep-ledger` (data-streams) hooked into `Replay.step_block` after the touch reconcile, as in its own loop.

## Saturday candidates (need judgment; not done)

1. **Experimental flags not in B3** (each is cited by scoreboard / prereg rows, so removing one makes those rows
   unreproducible from `unified`): `--multi-lite`, `--named-cancel`, `--placeholder-split`, `--name-strict`,
   `--ioc-model`, `--phantom-ledger`/`--phantom-mode`, `--cap-windows`/`--cap-free`, `--px-sigfig`, `--gate-insweep`,
   `--learn-tid`, `--oracle-*`, `--deep-ledger`, `--alo-distrust`, `--alo-reject`, `--placeholder-*`, `--touch-n-cap`,
   `--touch-refill`, `--snapshot-shift`, `--oid-guard`, `--evict-known`, `--trade-learn`. Option: keep them but move
   the switch table to one place, or remove the failed ones and point the rows at their archive tags
   (`archive/mahito-6568f02`, `archive/data-streams-bcda726`).
2. `--trade-learn-at tx` (mid-block corrections; rule-1 deviation D4): only FROZEN v2 (the headline reference) uses it.
3. `tx_class` exists twice (`analysis/validate_replay.py`, `engine_py/ordering.py`); have validate_replay import the
   engine copy (behavior-identical, but touches the policy path of every run).
4. `analysis/counterfactual.py` is a TODO stub superseded by `analysis/gate2.py`.
5. Two branches of the 400 ms priority code (`Engine._rest` jumps closure, defined twice in matching.py) could share
   one helper.
