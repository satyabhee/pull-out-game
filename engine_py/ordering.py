"""In-block ordering policies as swappable functions.

M1n (gate 2 OFF arm, docs/gate2.md; leads L61 fallback / L44 variant (c)): the stored block order is the execution
order, class-sorted inside each consensus batch (docs/l1_listing_order.md). M1n removes the class sort: inside each
batch, txs run in send-time order (calibrated signed nonce, from handoff/gate2/send_times*.csv). Batches: the file's
batch column (Yuta's rank-drop cut on all actions), else a cut at every rank drop of the stored order. A tx with no
send time (stale / counter / future nonce) keeps its listing slot, which in a class-sorted batch is a position inside
its own class's run; the timed txs of a batch are sorted among the slots they occupy. Ties keep stored order.
Pure: returns a new list, never mutates the input.
"""
from __future__ import annotations

import csv
from collections import Counter
from decimal import Decimal
from pathlib import Path

from engine_py.types import Action, Cancel, CancelByCloid, Modify, Place, Tif


def tx_class(actions_of_tx) -> int:
    """Chain in-batch rank (Yuta #55, docs/gate2_data.md): 0 cancel, 1 ALO-only, 2 sends a GTC/IOC order, 3 other.
    (analysis/validate_replay.tx_class ranks ALO before cancel; that rule is not used to cut batches.)"""
    if any((a.new if isinstance(a, Modify) else a).tif is not Tif.ALO
           for a in actions_of_tx if isinstance(a, (Place, Modify))):
        return 2
    if any(isinstance(a, (Cancel, CancelByCloid)) for a in actions_of_tx):
        return 0
    if any(isinstance(a, Place) for a in actions_of_tx):
        return 1
    return 3


def batches_of(actions: list[Action]) -> dict[int, int]:
    """tx_index -> batch number: a new batch starts at each class descent in stored (tx_index) order."""
    by_tx: dict[int, list] = {}
    for a in actions:
        by_tx.setdefault(a.tx_index, []).append(a)
    out, b, prev = {}, 0, None
    for t in sorted(by_tx):
        c = tx_class(by_tx[t])
        if prev is not None and c < prev:
            b += 1
        out[t], prev = b, c
    return out


def _int_exact(s: str) -> int:
    """An integer from a decimal string; '1795101766784.0' is accepted (pandas writes int columns holding NaN as
    floats), anything with a nonzero fraction fails loudly. Never rounds."""
    d = Decimal(s)
    if not d.is_finite() or d != d.to_integral_value():
        raise ValueError(f"send_times: {s!r} is not an integer")
    return int(d)


def load_send_times(path) -> dict[tuple[int, str], tuple[int, int | None]]:
    """(height, tx hash lowercased) -> (send time ms, batch or None). Empty send_ms falls back to the raw nonce; the
    batch column (Yuta's rank-drop cut on ALL actions, handoff/gate2/*.csv) is used when present. Rows of one tx must
    agree (one signed action = one nonce); a disagreement fails loudly."""
    out: dict[tuple[int, str], tuple[int, int | None]] = {}
    with open(Path(path), newline="") as f:
        for r in csv.DictReader(f):
            k = (int(r["height"]), r["tx_hash"].lower())
            t = _int_exact(r["send_ms"]) if r.get("send_ms") not in (None, "") else _int_exact(r["nonce"])
            b = int(r["batch"]) if r.get("batch") not in (None, "") else None
            if out.setdefault(k, (t, b)) != (t, b):
                raise ValueError(f"send_times: tx {k} has two values ({out[k]}, {(t, b)})")
    return out


def reused_txs(path) -> set[tuple[int, str]]:
    """(height, tx hash) of txs whose (account, nonce) is shared with another tx of the same block (several agent
    wallets of one account; Yuta, docs/gate2_data.md). Used only by the gate 2 sensitivity row."""
    by: dict[tuple[int, str, str], set] = {}
    with open(Path(path), newline="") as f:
        for r in csv.DictReader(f):
            by.setdefault((int(r["height"]), r["user"].lower(), r["nonce"]), set()).add(r["tx_hash"].lower())
    return {(h, x) for (h, _u, _n), hs in by.items() if len(hs) > 1 for x in hs}


def m1n_policy(send: dict):
    """The M1n policy for a send-time table ((height, hash) -> send ms, or -> (send ms, batch)). Batches come from
    the table's batch column when it has one, else from class descents (batches_of). Untimed txs never move, so
    their batch does not matter. The returned function carries `stats` (a Counter)."""
    stats: Counter = Counter()
    send = {k: (v if isinstance(v, tuple) else (v, None)) for k, v in send.items()}

    def policy(actions: list[Action]) -> list[Action]:
        if not actions:
            return list(actions)
        height = actions[0].block
        txs = sorted({a.tx_index for a in actions})
        hash_of = {a.tx_index: a.hash.lower() for a in actions}
        timed = {t: send[(height, hash_of[t])] for t in txs if (height, hash_of[t]) in send}
        if not timed:
            stats["blocks_untouched"] += 1
            return list(actions)
        stats["blocks_reordered"] += 1
        if all(b is not None for _, b in timed.values()):
            batch = {t: timed[t][1] for t in timed}
            seq = [batch[t] for t in txs if t in timed]
            if seq != sorted(seq):
                raise ValueError(f"block {height}: batch ids not contiguous in listing order")
        else:
            cut = batches_of(actions)
            batch = {t: cut[t] for t in timed}
        stats["batches"] += len(set(batch.values()))
        new = list(txs)
        for bno in sorted(set(batch.values())):
            slots = [i for i, t in enumerate(txs) if t in timed and batch[t] == bno]
            moved = sorted((txs[i] for i in slots), key=lambda t: (timed[t][0], t))
            for i, t in zip(slots, moved):
                new[i] = t
        stats["txs_moved"] += sum(1 for x, y in zip(txs, new) if x != y)
        stats["txs_timed"] += len(timed)
        stats["txs_untimed"] += len(txs) - len(timed)
        rank = {t: i for i, t in enumerate(new)}
        return sorted(actions, key=lambda a: (rank[a.tx_index], a.item_index))

    policy.stats = stats
    return policy
