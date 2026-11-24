"""ORACLE switches (BLITZ Addendum 3): ORACLE (LOOK-AHEAD BY DESIGN), DIAGNOSTIC ONLY.

They give the replay the true value of one hidden variable wherever chain truth exists, to measure how much each
hidden variable is worth (handoff/ORACLE.md). They read chain truth that is only known AFTER the fact, so they are
never adopted, never used in a screen or a confirm, and never run on final240 / confirm68 / the reserved benchmark /
an unused confirm set.

Truth file (one CSV, Yuta's handoff/truth_audit/status.csv format; extra columns ignored):
    set, chunk, user, tx_hash, item, oid, status, status_time_ms
rows are filtered to the replayed chunk (window folder name). Keys are the placing tx hash + item index.

  O1 status (--oracle-status):  a placement (Place / Modify's new order, any tif but IOC) whose chain status is a
       rejection (status ending in "Rejected") is not applied (a Modify's cancel half still is). A placement whose
       status is a cancel ("...Canceled", except plain "canceled" by the owner's own visible action) with a known
       status_time_ms is removed at the end of the first block at or after that time if it still rests.
  O3 cancel (--oracle-cancel):  an int-oid Cancel whose oid has a truth row removes OUR order created by that row's
       placement (tx_hash, item), instead of the resolver's guess.
  O2 prequeue (--oracle-prequeue FILE): Yuta's prequeue.csv (set, chunk, snapshot_time_ms, coin, side, px, user, oid,
       sz, placed_time_ms, end_status, end_time_ms): orders alive at the chunk's snapshot. At seeding, each level that
       holds a placeholder is rebuilt as: the placeholder's unexplained remainder (front, as the baseline puts
       anonymous liquidity first: an ASSUMPTION), then the true orders in placement order (placed_time, oid), each at
       min(its size, what the placeholder held); a true order whose end_status is canceled is removed at end_time_ms.
  O4 ioc (--oracle-ioc):        an IOC with a truth row: rejected (status ending in "Rejected", except
       "iocCancelRejected" = accepted but nothing to match) -> not applied; otherwise applied and exempt from
       --taker-reject.
"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

ACCEPTED_IOC = {"iocCancelRejected"}


def _rejected(status: str) -> bool:
    return status.endswith("Rejected")


class Oracle:
    def __init__(self, path: str | Path | None, chunk: str, kinds: set[str], prequeue_path: str | Path | None = None):
        bad = kinds - {"status", "cancel", "ioc", "prequeue"}
        if bad:
            raise ValueError(f"unknown oracle kinds {sorted(bad)}")
        self.kinds = kinds
        self.by_key: dict[tuple[str, int], dict] = {}      # (tx hash, item) -> row
        self.by_oid: dict[int, dict] = {}
        self.prequeue: list[dict] = []
        if "prequeue" in kinds:
            if not prequeue_path:
                raise ValueError("oracle prequeue needs its file")
            with open(prequeue_path, newline="") as f:
                self.prequeue = [dict(r) for r in csv.DictReader(f) if r.get("chunk") == chunk]
        rows = []
        if path:
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
        if True:
            for r in rows:
                if r.get("chunk") and r["chunk"] != chunk:
                    continue
                r = dict(r)
                r["item"] = int(r["item"]) if r.get("item") not in (None, "") else None
                r["oid"] = int(r["oid"]) if r.get("oid") not in (None, "") else None
                r["status_time_ms"] = int(r["status_time_ms"]) if r.get("status_time_ms") not in (None, "") else None
                if r.get("tx_hash") and r["item"] is not None:
                    self.by_key[(r["tx_hash"].lower(), r["item"])] = r
                if r["oid"] is not None:
                    self.by_oid[r["oid"]] = r
        self.stats: Counter = Counter()
        self.created: dict[tuple[str, int], int] = {}       # (tx hash, item) -> our engine oid
        self.pending_cancel: list[tuple[int, int]] = []     # (time ms, engine oid)

    def row(self, action) -> dict | None:
        return self.by_key.get((action.hash.lower(), action.item_index))

    # ---- decisions before an action is applied
    def skip_place(self, p, is_ioc: bool) -> str | None:
        """Reason to not apply this placement, or None."""
        r = self.row(p)
        if r is None:
            return None
        st = r["status"]
        if is_ioc and "ioc" in self.kinds:
            if _rejected(st) and st not in ACCEPTED_IOC:
                return "oracle_ioc_reject"
            return None
        if not is_ioc and "status" in self.kinds and _rejected(st):
            return "oracle_status_reject"
        return None

    def ioc_known_accepted(self, p) -> bool:
        r = self.row(p)
        return "ioc" in self.kinds and r is not None and not (_rejected(r["status"]) and r["status"] not in ACCEPTED_IOC)

    def cancel_target(self, oid: int) -> int | None:
        """Our engine oid that this int-oid cancel truly removes (O3), or None if unknown."""
        if "cancel" not in self.kinds:
            return None
        r = self.by_oid.get(oid)
        if r is None or not r.get("tx_hash") or r["item"] is None:
            return None
        return self.created.get((r["tx_hash"].lower(), r["item"]))

    # ---- bookkeeping after an action is applied
    def note_created(self, action, place, engine_oid: int) -> None:
        key = (action.hash.lower(), action.item_index)
        self.created[key] = engine_oid
        r = self.by_key.get(key)
        if ("status" in self.kinds and r is not None and r["status_time_ms"] is not None
                and r["status"].endswith("Canceled") and r["status"] != "canceled"):
            self.pending_cancel.append((r["status_time_ms"], engine_oid))

    def due_cancels(self, block_time_ms: int) -> list[int]:
        due = [o for t, o in self.pending_cancel if t <= block_time_ms]
        self.pending_cancel = [(t, o) for t, o in self.pending_cancel if t > block_time_ms]
        return due
