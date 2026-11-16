# book-replay

Rebuild Hyperliquid's order book from its public transaction log, rerun real history under other ordering rules, and prove every engine version produces identical state, block by block.

Two pillars:

- **Matching**: a price-time priority book (GTC / IOC / ALO, self-trade prevention) replayed against real `replica_cmds` and checked against real fills.
- **State**: a canonical byte encoding and per-block hash chain, so two runs, a restart from snapshot, and the C++ engine all produce the same hashes.

Specs come first and live in [`specs/`](specs/). Code is written from them.

## Fetch

```bash
make install
uv run python -m data.fetch      # replica_cmds + fills for a small window (requester-pays S3)
uv run python -m data.parse
```

## Replay

```bash
uv run python -m engine_py.replay   # fills + one state hash per block
make test
```

## Reproduce figures

```bash
uv run python -m analysis.validate_replay
uv run python -m analysis.counterfactual
uv run python -m analysis.regression
uv run python -m analysis.figures
```

## Layout

| Path | What it holds |
|---|---|
| `specs/` | Data contract, book, matching, ordering, invariants, replication, state encoding, determinism, upgrades, experiment, C++ action format and layout |
| `data/` | S3 fetch, block parsing, binary action file for C++ |
| `engine_py/` | Python reference engine: book, matching, ordering, replay, state hash, snapshots, versions |
| `engine_cpp/` | Flat-memory C++ engine with hash parity to Python |
| `tests/` | Unit, property-based, snapshot, determinism and differential tests |
| `analysis/` | Replay validation, counterfactual experiment, regression, figures |
| `bench/` | Latency harness and tuning log |
| `report/` | IE 498 report outline |

## Gates

| Gate | Pass condition |
|---|---|
| G0 Data works | Link fills to orders and tell rejected from accepted orders in one hour of data; the Python engine replays one real block. |
| G1 Replay matches | Fills for orders placed inside the window match the chain at a stated rate. |
| G2 Deterministic | Identical per-block hashes across two runs, different hash seeds, and a restart from snapshot. |
| G3 Report bar met | Counterfactual results across policies, written into the 6-section report. |
| M1 C++ hash parity | C++ replays the action file with per-block hashes identical to Python, no allocation after startup. |
| M2 Order-ID map | Handle table replaced by an open-addressing map, every hash unchanged. |
