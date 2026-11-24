"""sqd_truth: all-user chain truth from SQD replica-cmds blocks (BLITZ Addendum 5, RIKA R1; diagnostic / oracle use only).

For every explorer block we have an SQD copy of (~/hl-data/sqd/blocks/<height>.jsonl), each explorer tx (one item: the
explorer splits multi-item actions) is aligned to its SQD (actionIndex, item) by (user, exact item payload), in order;
the item's response gives the chain outcome:
  resting {oid}  -> status "open" (rested at placement), oid
  filled {...}   -> "filled" (oid when given)
  error text     -> "badAloPxRejected" ("Post only order would have immediately matched"), "iocCancelRejected"
                    ("could not immediately match"), "perpMarginRejected" ("Insufficient margin"), "minTradeNtlRejected"
                    ("minimum value"), "reduceOnlyRejected" ("Reduce only"), "tickRejected" ("tick"), else "rejected:<text>"
  cancels        -> "cancel:success" / "cancel:<error text>"
Writes OUT/<set>/{status.csv, ioc.csv} in the format of analysis/oracle_truth (status_time_ms = block time: the outcome is
known at placement) plus columns tracked (user in the recorder's tracked set that hour) and the SQD text, and prints
alignment counts and agreement with the existing truth files (TRUTH_ROOT/<set>/status.csv) on shared placements.
usage: python -m analysis.sqd_truth PLAN.csv --sqd ~/hl-data/sqd/blocks --chunks-root C --hl-data H --truth T --out O
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

ERR = (("Post only order would have immediately matched", "badAloPxRejected"),
       ("could not immediately match", "iocCancelRejected"),
       ("Insufficient margin", "perpMarginRejected"),
       ("minimum value", "minTradeNtlRejected"),
       ("Reduce only", "reduceOnlyRejected"),
       ("tick", "tickRejected"))


def item_list(action):
    t = action.get("type")
    if t == "order":
        return action.get("orders", [])
    if t in ("cancel", "cancelByCloid"):
        return action.get("cancels", [])
    if t == "batchModify":
        return action.get("modifies", [])
    if t == "modify":
        return [{"oid": action.get("oid"), "order": action.get("order")}]
    return [action]


def outcome(resp, j):
    """(status, oid, text) of item j from an SQD response."""
    if not isinstance(resp, dict):
        return "unknown", None, str(resp)[:120]
    data = resp.get("data") or {}
    sts = data.get("statuses") if isinstance(data, dict) else None
    if resp.get("type") in ("order", "batchModify", "modify") and isinstance(sts, list):
        if j >= len(sts):
            return "unknown", None, "no status for item"
        s = sts[j]
        if isinstance(s, dict):
            if "resting" in s:
                return "open", s["resting"].get("oid"), "resting"
            if "filled" in s:
                return "filled", (s["filled"] or {}).get("oid"), "filled"
            if "error" in s:
                txt = s["error"]
                for k, name in ERR:
                    if k in txt:
                        return name, None, txt
                return "rejected:" + txt[:60], None, txt
        return "ok:" + str(s)[:30], None, str(s)
    if resp.get("type") == "cancel" and isinstance(sts, list):
        s = sts[j] if j < len(sts) else None
        if s == "success":
            return "cancel:success", None, "success"
        return "cancel:" + (s.get("error", str(s)) if isinstance(s, dict) else str(s))[:60], None, str(s)
    return "unknown", None, json.dumps(resp)[:120]


def align(explorer_txs, sqd_actions):
    """explorer tx index -> (actionIndex, item index, status, oid, text) or None."""
    pool = defaultdict(list)
    for a in sqd_actions:
        for j, it in enumerate(item_list(a["action"])):
            pool[(a["user"].lower(), json.dumps(it, sort_keys=True))].append((a, j))
    out = {}
    for t, tx in enumerate(explorer_txs):
        its = item_list(tx.get("action") or {})
        if not its:
            out[t] = None
            continue
        k = (tx["user"].lower(), json.dumps(its[0], sort_keys=True))
        if not pool[k]:
            out[t] = None
            continue
        a, j = pool[k].pop(0)
        st, oid, txt = outcome(a.get("response"), j)
        out[t] = (a["actionIndex"], j, st, oid, txt)
    return out


def tracked_users(hl_data, t_ms):
    """Users (dedicated + shared) the recorder streamed at t_ms (its last tracked_users record before t_ms)."""
    g = time.gmtime(t_ms / 1000)
    users = set()
    for back in (0, 1):
        gg = time.gmtime(t_ms / 1000 - 3600 * back)
        p = Path(hl_data) / time.strftime("%Y-%m-%d", gg) / time.strftime("%H", gg) / "tracked_users.jsonl.gz"
        if not p.exists():
            continue
        last = None
        with gzip.open(p, "rt") as fh:
            for line in fh:
                m = json.loads(line).get("msg") or {}
                if m.get("time", 0) <= t_ms:
                    last = m
        if last:
            return {u.lower() for u in last.get("users", []) + last.get("shared", [])}
    del g
    return users


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("plan")
    ap.add_argument("--sqd", required=True)
    ap.add_argument("--chunks-root", required=True)
    ap.add_argument("--hl-data", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    plan = list(csv.DictReader(open(a.plan)))
    blocks = {}
    for r in plan:
        blocks.setdefault((r["set"], int(r["block"])), r["chunk"])
    out = Path(a.out)
    writers, files = {}, []
    cnt = Counter()
    agree = Counter()
    existing = {}
    for (s, h), ch in sorted(blocks.items()):
        f = Path(a.sqd) / f"{h}.jsonl"
        if not f.exists():
            cnt["blocks_missing_sqd"] += 1
            continue
        line = f.read_text().splitlines()[0]
        sq = json.loads(line)
        if sq.get("header", {}).get("number") != h:
            cnt["blocks_bad_sqd"] += 1
            continue
        with gzip.open(Path(a.chunks_root) / s / ch / f"{h}.json.gz", "rt") as fh:
            eb = json.load(fh)
        eb = eb.get("blockDetails", eb)
        if sq["header"].get("timestamp") != eb["blockTime"]:
            cnt["blocks_time_mismatch"] += 1
        al = align(eb["txs"], sq.get("actions", []))
        tr = tracked_users(a.hl_data, eb["blockTime"])
        if s not in writers:
            (out / s).mkdir(parents=True, exist_ok=True)
            fs = open(out / s / "status.csv", "w", newline=""), open(out / s / "ioc.csv", "w", newline="")
            files += fs
            w1, w2 = csv.writer(fs[0]), csv.writer(fs[1])
            w1.writerow(["set", "chunk", "user", "tx_hash", "item", "oid", "status", "status_time_ms", "src",
                         "tracked", "block", "tx_index", "action_index", "sqd_item", "text"])
            w2.writerow(["set", "chunk", "user", "tx_hash", "item", "oid", "status", "src", "tracked"])
            writers[s] = (w1, w2)
            p = Path(a.truth) / s / "status.csv"
            existing[s] = {(x["chunk"], x["tx_hash"], x["item"]): x["status"] for x in csv.DictReader(open(p))} \
                if p.exists() else {}
        w1, w2 = writers[s]
        cnt["blocks"] += 1
        for t, tx in enumerate(eb["txs"]):
            res = al.get(t)
            kind = (tx.get("action") or {}).get("type")
            cnt["txs"] += 1
            if res is None:
                cnt["txs_unaligned"] += 1
                continue
            ai, j, st, oid, txt = res
            u = tx["user"].lower()
            trk = int(u in tr)
            cnt[f"status:{st.split(':')[0] if st.startswith(('rejected', 'cancel', 'ok')) else st}"] += 1
            if kind == "order":
                o = (tx["action"].get("orders") or [{}])[0]
                w1.writerow([s, ch, u, tx["hash"], 0, oid or "", st, eb["blockTime"], "sqd", trk, h, t, ai, j, txt[:120]])
                cnt[f"orders_{'tracked' if trk else 'untracked'}"] += 1
                if ((o.get("t") or {}).get("limit") or {}).get("tif") == "Ioc":
                    w2.writerow([s, ch, u, tx["hash"], 0, oid or "", st, "sqd", trk])
                old = existing[s].get((ch, tx["hash"], "0"))
                if old is not None:
                    same = (old == st) or (old in ("canceled", "filled", "open") and st in ("open", "filled"))
                    agree["same" if same else f"differ:{old}->{st}"] += 1
    for f in files:
        f.close()
    print(dict(sorted(cnt.items())))
    print("agreement with existing truth on shared placements:", dict(agree.most_common()))


if __name__ == "__main__":
    main()
