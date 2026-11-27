"""oracle_truth: chain-truth files for the ORACLE ceiling test (BLITZ Addendum 3; look-ahead by design, DIAGNOSTIC ONLY).

From data already on disk (no API calls):
  - anchors.json            orderStatus answers (by cloid / oid) the capture asked for int-oid anchoring
  - hist_orders.json        historicalOrders (last 2000 orders) of a few users per chunk
  - suspect_status.json     orderStatus of suspect post-only placements (open-margin capture only)
  - hl_data/<day>/<hour>/user_orders.jsonl.gz   live orderUpdates of the recorder's tracked users
For every chunk of a set it writes, under OUT/<set>/:
  o1_order_status.jsonl  one row per in-window BTC/ETH placement (height, tx, item) with a known chain status:
                         status, status_ms (statusTimestamp), oid, tif, src
  o2_prewindow.jsonl     orders ALIVE at the chunk's first block (placed before it, final status after it) on BTC/ETH:
                         user, coin, side, px, orig_sz, sz_at_status, oid, placed_ms, end_status, end_ms, src
  o3_oids.jsonl          one row per in-window placement with a known chain oid (the true cancel target id)
  o4_ioc_status.jsonl    the IOC subset of o1 (accepted / rejected as on chain)
Rows are keyed so the replay can join them: (chunk, height, tx, item) for placements; (chunk, user, oid) for o2.
Join: by (user, cloid) when the placement has a cloid, else by (user, coin, side, px, orig sz, timestamp == blockTime).
usage: python -m analysis.oracle_truth SET_DIR [...] --hl-data H --out handoff/oracle_truth
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import time
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

COINS = {0: "BTC", 1: "ETH"}
CNUM = {v: k for k, v in COINS.items()}
TERMINAL_OPEN = {"open", "triggered"}
PREFIX = 10          # "0x" + 8 hex: a cloid prefix names one user in 8,856 of 8,863 prefixes (devfast chunk 1)


def blocks(chunk_dir):
    for f in sorted(Path(chunk_dir).glob("*.json.gz")):
        n = f.name[: -len(".json.gz")]
        if n.isdigit():
            with gzip.open(f, "rt") as fh:
                b = json.load(fh)
            yield int(n), b.get("blockDetails", b)


def placements(chunk_dir):
    """[(height, block_ms, tx, item, user, coin, side 'B'/'A', px str, sz str, tif, cloid)] of BTC/ETH limit orders."""
    out = []
    for h, b in blocks(chunk_dir):
        for t, tx in enumerate(b.get("txs", [])):
            if tx.get("error") is not None:
                continue
            a = tx.get("action") or {}
            kind = a.get("type")
            items = (a.get("orders", []) if kind == "order" else [a.get("order", {})] if kind == "modify"
                     else [m.get("order", {}) for m in a.get("modifies", [])] if kind == "batchModify" else [])
            for i, o in enumerate(items):
                if not isinstance(o, dict) or o.get("a") not in COINS or "limit" not in (o.get("t") or {}):
                    continue
                out.append((h, b["blockTime"], t, i, tx.get("hash"), tx["user"].lower(), COINS[o["a"]], "B" if o.get("b") else "A",
                            o.get("p"), o.get("s"), o["t"]["limit"].get("tif"), (o.get("c") or "").lower() or None))
    return out


def norm(px):
    try:
        return format(float(px), ".10g")
    except (TypeError, ValueError):
        return str(px)


class Truth:
    """Order records {(user, oid): rec}; rec = dict(order fields + status, status_ms, src). Latest status wins."""

    def __init__(self):
        self.by_oid = {}

    def add(self, user, entry, src, shared=()):
        o = entry.get("order") or {}
        if "oid" not in o:
            return
        key = ((user or "").lower(), int(o["oid"]))
        rec = {"user": key[0], "oid": key[1], "coin": o.get("coin"), "side": o.get("side"), "px": norm(o.get("limitPx")),
               "orig_sz": o.get("origSz"), "sz": o.get("sz"), "placed_ms": o.get("timestamp"),
               "cloid": (o.get("cloid") or "").lower() or None, "tif": o.get("tif"),
               "status": entry.get("status"), "status_ms": entry.get("statusTimestamp"), "src": src,
               "shared": tuple(x.lower() for x in shared)}
        old = self.by_oid.get(key)
        if old is None or (rec["status_ms"] or 0) >= (old["status_ms"] or 0):
            if old is not None and rec["status"] in TERMINAL_OPEN and old["status"] not in TERMINAL_OPEN:
                return                       # never let an older-looking "open" hide a terminal status
            if old is not None:
                rec["src"] = old["src"] if old["src"] == src else f"{old['src']}+{src}"
            self.by_oid[key] = rec

    def index(self):
        self.by_cloid = defaultdict(list)          # cloids can be reused: keep every record
        self.by_shape = defaultdict(list)
        for (u, oid), r in self.by_oid.items():
            if r["cloid"]:
                self.by_cloid[(u, r["cloid"])].append(r)
                if not u:
                    self.by_cloid[("*", r["cloid"])].append(r)
            self.by_shape[(u, r["coin"], r["side"], r["px"], r["placed_ms"])].append(r)

    def find(self, u, cloid, coin, side, px, bt, sz, used):
        """The record of a placement by user u: own key first, then an unattributed shared-stream record whose
        shared list holds u."""
        if cloid:
            # the record must be THIS placement: same coin / side / px and placed at this block's time (a reused
            # cloid otherwise names a later order; Nov 24 truth audit)
            same = lambda x: x["coin"] == coin and x["side"] == side and x["px"] == px and x["placed_ms"] == bt \
                and (u, x["oid"]) not in used
            rs = [x for x in self.by_cloid.get((u, cloid), []) if same(x)] or \
                [x for x in self.by_cloid.get(("*", cloid), []) if same(x) and u in x["shared"]]
            if len(rs) == 1:
                return rs[0]
            if rs:
                return None
        cands = [x for x in self.by_shape.get((u, coin, side, px, bt), []) + [
                 x for x in self.by_shape.get(("", coin, side, px, bt), []) if u in x["shared"]]
                 if Decimal(str(x["orig_sz"])) == Decimal(str(sz)) and (u, x["oid"]) not in used]   # L55: "1" == "1.0"
        return cands[0] if len(cands) == 1 else None


def chunk_truth_files(chunk_dir, truth):
    """Feed the chunk's own captured answers into `truth`."""
    p = Path(chunk_dir)
    for name, src in (("anchors.json", "anchor"), ("suspect_status.json", "suspect")):
        f = p / name
        if not f.exists():
            continue
        try:
            rows = json.load(open(f))
        except ValueError:
            continue
        for r in rows if isinstance(rows, list) else rows.values():
            ans = r.get("answer") or {}
            if isinstance(ans, dict) and ans.get("status") == "order" and r.get("user"):
                truth.add(r["user"], ans["order"], src)
    f = p / "hist_orders.json"
    if f.exists():
        for e in json.load(open(f)):
            if isinstance(e.get("answer"), list):
                for x in e["answer"]:
                    truth.add(e["user"], x, "hist")


def stream_hours(hl_data, t0_ms, t1_ms):
    t = (t0_ms // 3_600_000) * 3_600_000
    while t <= t1_ms:
        g = time.gmtime(t / 1000)
        yield Path(hl_data) / time.strftime("%Y-%m-%d", g) / time.strftime("%H", g) / "user_orders.jsonl.gz"
        t += 3_600_000


def load_stream(hl_data, spans, truth):
    """Add the tracked users' orderUpdates whose statusTimestamp falls in any chunk span [t0, t1] (ms)."""
    hours = sorted({f for t0, t1 in spans for f in stream_hours(hl_data, t0, t1)})
    spans = sorted(spans)
    for f in hours:
        if not f.exists():
            continue
        with gzip.open(f, "rt") as fh:
            for line in fh:
                m = json.loads(line).get("msg") or {}
                u = m.get("user") or ""
                shared = m.get("shared") or ()
                for x in m.get("data") or []:
                    if (x.get("order") or {}).get("coin") not in CNUM:
                        continue
                    st = x.get("statusTimestamp") or 0
                    if any(a <= st <= b for a, b in spans):
                        truth.add(u, x, "stream", shared)


def build(set_dir, hl_data, out_dir):
    set_dir = Path(set_dir)
    chunks = sorted(d for d in set_dir.iterdir() if d.is_dir() and (d / "window.json").exists())
    spans, meta = [], {}
    for d in chunks:
        w = json.load(open(d / "window.json"))
        t0, t1 = w.get("first_block_time"), w.get("last_block_time")
        if t0 is None or t1 is None:
            hs = sorted(int(f.name.split(".")[0]) for f in d.glob("*.json.gz") if f.name.split(".")[0].isdigit())
            t0 = next(blocks_one(d, hs[0]))["blockTime"]
            t1 = next(blocks_one(d, hs[-1]))["blockTime"]
        meta[d.name] = (t0, t1, w.get("snapshot_time") or t0)
        spans.append((t0, t1 + 120_000))          # final statuses up to 2 min after the chunk
    stream_truth = Truth()
    load_stream(hl_data, spans, stream_truth)
    out = Path(out_dir) / set_dir.name
    out.mkdir(parents=True, exist_ok=True)
    files = {k: open(out / f"{k}.jsonl", "w") for k in ("o1_order_status", "o2_prewindow", "o3_oids", "o4_ioc_status")}
    csvs = {k: csv.writer(open(out / f"{k}.csv", "w", newline="")) for k in ("status", "ioc", "prequeue")}
    csvs["status"].writerow(["set", "chunk", "user", "tx_hash", "item", "oid", "status", "status_time_ms", "src"])
    csvs["ioc"].writerow(["set", "chunk", "user", "tx_hash", "item", "oid", "status", "src"])
    csvs["prequeue"].writerow(["set", "chunk", "snapshot_time_ms", "coin", "side", "px", "user", "oid", "sz",
                               "placed_time_ms", "end_status", "end_time_ms", "src"])
    cov = Counter()
    for d in chunks:
        t0, t1, snap = meta[d.name]
        truth = Truth()
        truth.by_oid.update({k: dict(v) for k, v in stream_truth.by_oid.items()
                             if (v["status_ms"] or 0) <= t1 + 120_000 and (v["placed_ms"] or 0) <= t1})
        chunk_truth_files(d, truth)
        truth.index()
        used = set()
        chunk_places = placements(d)
        for h, bt, t, i, txh, u, coin, side, px, sz, tif, cloid in chunk_places:
            cov["placements"] += 1
            cov[f"placements_{tif}"] += 1
            r = truth.find(u, cloid, coin, side, norm(px), bt, sz, used)
            if r is None:
                continue
            used.add((u, r["oid"]))
            row = {"chunk": d.name, "height": h, "tx": t, "tx_hash": txh, "item": i, "user": u, "coin": coin, "side": side,
                   "px": norm(px), "tif": tif, "cloid": cloid, "oid": r["oid"], "status": r["status"],
                   "status_ms": r["status_ms"], "block_ms": bt, "placed_ms": r["placed_ms"], "src": r["src"]}
            files["o1_order_status"].write(json.dumps(row) + "\n")
            files["o3_oids"].write(json.dumps({k: row[k] for k in ("chunk", "height", "tx", "item", "user", "cloid",
                                                                   "oid", "src")}) + "\n")
            csvs["status"].writerow([set_dir.name, d.name, u, txh, i, r["oid"], r["status"], r["status_ms"], r["src"]])
            cov["o1_rows"] += 1
            cov[f"o1_{tif}"] += 1
            cov[f"o1_status:{r['status']}"] += 1
            if tif == "Ioc":
                files["o4_ioc_status"].write(json.dumps(row) + "\n")
                csvs["ioc"].writerow([set_dir.name, d.name, u, txh, i, r["oid"], r["status"], r["src"]])
                cov["o4_rows"] += 1
        oid_owner, cloid_owner = cancel_owners(d)
        pref = defaultdict(set)                  # cloid prefix -> users, learned from this chunk's placements
        for x in chunk_places:
            if x[-1]:
                pref[x[-1][:PREFIX]].add(x[5])
        for (u, oid), r in truth.by_oid.items():
            if not u:
                u = oid_owner.get(oid) or cloid_owner.get(r["cloid"])
                if u is None and r["cloid"]:
                    cand = pref.get(r["cloid"][:PREFIX], set()) & set(r["shared"])
                    if len(cand) == 1:
                        u = next(iter(cand))
                        cov["o2_by_cloid_prefix"] += 1
                if u is None or (r["shared"] and u not in r["shared"]):
                    cov["o2_unattributed"] += (r["coin"] in CNUM)
                    continue
            if r["coin"] not in CNUM or r["placed_ms"] is None or r["placed_ms"] >= snap:
                continue
            alive = (r["status"] in TERMINAL_OPEN) or (r["status_ms"] or 0) > snap
            if not alive or r["tif"] == "Ioc":
                continue
            files["o2_prewindow"].write(json.dumps({"chunk": d.name, "user": u, "coin": r["coin"], "side": r["side"],
                                                    "px": r["px"], "orig_sz": r["orig_sz"], "sz_at_status": r["sz"],
                                                    "oid": oid, "placed_ms": r["placed_ms"],
                                                    "end_status": r["status"], "end_ms": r["status_ms"],
                                                    "src": r["src"]}) + "\n")
            # size at the snapshot: the size reported with the final status (remaining when canceled; a later
            # partial fill would make it smaller than at the snapshot), the original size for a fill / open
            szs = r["sz"] if r["status"] not in ("filled",) else r["orig_sz"]
            csvs["prequeue"].writerow([set_dir.name, d.name, snap, r["coin"], r["side"], r["px"], u, oid, szs,
                                       r["placed_ms"], r["status"], r["status_ms"], r["src"]])
            cov["o2_rows"] += 1
    for f in files.values():
        f.close()
    json.dump(dict(cov), open(out / "coverage.json", "w"), indent=1, sort_keys=True)
    return cov


def cancel_owners(chunk_dir):
    """oid -> user and cloid -> user from the chunk's cancel / modify actions (the canceller owns the order)."""
    by_oid, by_cloid = {}, {}
    for h, b in blocks(chunk_dir):
        for tx in b.get("txs", []):
            a = tx.get("action") or {}
            u = tx.get("user", "").lower()
            k = a.get("type")
            if k == "cancel":
                for c in a.get("cancels", []):
                    if isinstance(c.get("o"), int):
                        by_oid[c["o"]] = u
            elif k == "cancelByCloid":
                for c in a.get("cancels", []):
                    if c.get("cloid"):
                        by_cloid[c["cloid"].lower()] = u
            elif k == "modify" and isinstance(a.get("oid"), int):
                by_oid[a["oid"]] = u
            elif k == "batchModify":
                for m in a.get("modifies", []):
                    if isinstance(m.get("oid"), int):
                        by_oid[m["oid"]] = u
    return by_oid, by_cloid


def blocks_one(d, h):
    with gzip.open(Path(d) / f"{h}.json.gz", "rt") as fh:
        b = json.load(fh)
    yield b.get("blockDetails", b)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sets", nargs="+")
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    for s in a.sets:
        cov = build(s, a.hl_data, a.out)
        print(Path(s).name, {k: v for k, v in sorted(cov.items()) if not k.startswith("o1_status")})
        print("  statuses", {k[10:]: v for k, v in sorted(cov.items(), key=lambda x: -x[1]) if k.startswith("o1_status")})


if __name__ == "__main__":
    main()
