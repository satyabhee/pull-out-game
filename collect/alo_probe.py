"""
alo_probe.py: live ground truth for ALO (post-only) orders, run by the hl-alo-probe service.

Why live: orderStatus forgets a busy user's orders within minutes (confirmed orders answered
unknownOid at ~80 min), so capture, which runs minutes behind the chain, can't ask. Instead this
reads the live explorer tx feed (a ~1-in-2000 sample of all txs, ~10 tx/s), picks BTC/ETH ALO
orders with a cloid, and asks orderStatus by (user, cloid) ~2 s after seeing each one.
Answers are e.g. "badAloPxRejected" (rejected: would have crossed), "open", "canceled", "filled".

Output: hl_data/<date>/<hour>/alo_status.jsonl (hourly, gzipped when the hour closes, like the
recorder), one line per sample with the tx's block, hash, position and the answer, so samples
that fall inside a captured window give per-order truth for it.

Pace: at most one info request per INTERVAL_S (1 s: orderStatus weighs 2 of the info API's
1200/min per IP). After an error, pause 60 s. Newest candidates win; ones older than 30 s drop.
No explorer block requests. Usage (in ~/hl):  .venv/bin/python -u alo_probe.py
"""
import asyncio, collections, json, os, time, urllib.request

INFO = "https://api.hyperliquid.xyz/info"
RPC_WS = "wss://rpc.hyperliquid.xyz/ws"
ASSETS = (0, 1)                     # BTC, ETH
DELAY_S = 2.0                       # ask this long after seeing the tx
MAX_AGE_S = 30.0                    # drop candidates older than this
INTERVAL_S = float(os.environ.get("ALO_PROBE_INTERVAL", 1.0))
PAUSE_S = 60.0


def alo_items(tx):
    """BTC/ETH ALO items with a cloid in one successful tx: [(item_index, item)].
    Item numbering matches data/parse.py and data/oids.py (order list / modify / batchModify list)."""
    if not isinstance(tx, dict) or tx.get("error") is not None:
        return []
    a = tx.get("action") or {}
    kind = a.get("type")
    items = (a.get("orders", []) if kind == "order" else [a.get("order", {})] if kind == "modify"
             else [m.get("order", {}) for m in a.get("modifies", [])] if kind == "batchModify" else [])
    return [(i, o) for i, o in enumerate(items)
            if isinstance(o, dict) and o.get("a") in ASSETS and o.get("c")
            and o.get("t", {}).get("limit", {}).get("tif") == "Alo"]


def order_status(user, cloid):
    req = urllib.request.Request(INFO, data=json.dumps({"type": "orderStatus", "user": user, "oid": cloid}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


async def main():
    from record_hl import Sink, stream   # needs websockets; imported here so tests can import alo_items
    sink = Sink("alo_status")
    pending = collections.deque(maxlen=200)
    stats = collections.Counter()

    def route(m):
        if not isinstance(m, list):
            return
        now = time.time()
        for tx in m:
            items = alo_items(tx)
            if items:
                i, o = items[0]               # one per tx, so a big batch doesn't dominate
                pending.append((now, tx, i, o, len(items)))
                stats["seen"] += 1

    async def worker():
        last_log = time.time()
        while True:
            await asyncio.sleep(INTERVAL_S)
            now = time.time()
            while pending and now - pending[0][0] > MAX_AGE_S:
                pending.popleft(); stats["dropped_old"] += 1
            ready = [p for p in pending if now - p[0] >= DELAY_S]
            if not ready:
                continue
            seen, tx, i, o, n = ready[-1]     # newest candidate at least DELAY_S old
            pending.remove(ready[-1])
            rec = {"seen_ms": int(seen * 1000), "tx_time": tx.get("time"), "block": tx.get("block"),
                   "hash": tx.get("hash"), "user": tx["user"].lower(), "kind": tx["action"]["type"],
                   "item": i, "alo_items_in_tx": n, "cloid": o["c"], "a": o["a"], "b": o["b"],
                   "p": o["p"], "s": o["s"], "r": o.get("r")}
            try:
                rec["answer"] = await asyncio.to_thread(order_status, rec["user"], o["c"])
            except Exception as e:
                rec["answer"] = {"error": repr(e)}
                stats["errors"] += 1
                await asyncio.sleep(PAUSE_S)
            rec["asked_ms"] = int(time.time() * 1000)
            sink.write(rec)
            ans = rec["answer"]
            stats[(ans.get("order") or {}).get("status") or ans.get("status") or "error"] += 1
            if time.time() - last_log > 600:
                print(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), dict(stats), flush=True)
                last_log = time.time()

    try:
        await asyncio.gather(stream(RPC_WS, [{"type": "explorerTxs"}], route), worker())
    finally:
        sink.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped")
