"""M1 truth log: every hidden fact of a simulated chunk (MAHITO_PLAN M1 truth.py), as compressed parquet.

Records (`kind` first): seeded (pre-window orders with owner and queue position), placed (every order item: oid,
owner, block, tx, item, p), rested (queue position at placement), order_status (accepted / rejected with the chain's
reason, cancels, silent reduce-only cancels, self-trade cancels), fill, exec (batch membership and execution order),
burned_oids (invisible events), and block (time, batches as tx index lists).

File: <root>/truth/<chunk>.parquet (zstd). One row per record in execution order; the columns are the union of every
record's keys, and `_absent` lists the keys a row did not have, so read_truth returns exactly the dicts written.
The run's SimConfig (a "config" record) is kept as JSON in the file metadata.
"""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def write_truth(path: Path, records: list[dict]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    config = [r for r in records if r.get("kind") == "config"]
    rows = [r for r in records if r.get("kind") != "config"]
    keys: list[str] = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    cols = {k: pa.array([r.get(k) for r in rows]) for k in keys}
    cols["_absent"] = pa.array([",".join(k for k in keys if k not in r) for r in rows], pa.string())
    meta = {b"config": json.dumps(config[0] if config else {}, default=list).encode()}
    table = pa.table(cols).replace_schema_metadata(meta)
    pq.write_table(table, path, compression="zstd")
    return path.stat().st_size


def read_truth(path: Path) -> list[dict]:
    table = pq.read_table(path)
    out = []
    for r in table.to_pylist():
        absent = set(filter(None, r.pop("_absent").split(",")))
        out.append({k: v for k, v in r.items() if k not in absent})
    return out


def read_config(path: Path) -> dict:
    return json.loads(pq.read_schema(path).metadata[b"config"])
