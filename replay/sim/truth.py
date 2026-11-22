"""M1 truth log: every hidden fact of a simulated chunk (MAHITO_PLAN M1 truth.py).

Records (one JSON object per line, `kind` first): seeded (pre-window orders with owner and queue position), placed
(every order item: oid, owner, block, tx, item, p), rested (queue position at placement), order_status (accepted /
rejected with the chain's reason, cancels, silent reduce-only cancels, self-trade cancels), fill, exec (batch
membership and execution order), burned_oids (invisible events), and block (time, batches as tx index lists).

Format: lz4-framed JSON lines, <root>/truth/<chunk>.jsonl.lz4. The plan asks for compressed parquet; that needs
`pyarrow`, a new dependency (docs/QUESTIONS.md). Only write_truth / read_truth know the format.
"""
from __future__ import annotations

import json
from pathlib import Path

import lz4.frame


def write_truth(path: Path, records: list[dict]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records).encode()
    blob = lz4.frame.compress(data)
    path.write_bytes(blob)
    return len(blob)


def read_truth(path: Path) -> list[dict]:
    return [json.loads(line) for line in lz4.frame.decompress(Path(path).read_bytes()).decode().splitlines()]
