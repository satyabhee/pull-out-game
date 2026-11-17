"""
g0_live.py: fetch the newest N blocks and immediately record order-id anchors while orders are minutes old.

  python3 g0_live.py ~/Downloads/server_data 300

Why: the explorer caps "archived" (older) blocks at 100/day per IP, and the info API's orderStatus
forgets old orders (it answered unknownOid for every 5-hour-old order). Both work for fresh data.
Writes blocks to <root>/blocks/live-<start>-<end>/ (same layout as fetch_blocks.py, so g0_check.py
reads them) and orderStatus answers to <root>/g0_cache.json. One explorer client at a time.
"""
import collections, glob, gzip, json, os, sys, time, urllib.request

ROOT = os.path.expanduser(sys.argv[1])
N = int(sys.argv[2]) if len(sys.argv) > 2 else 300
EXPL, INFO = "https://rpc.hyperliquid.xyz/explorer", "https://api.hyperliquid.xyz/info"
CACHE = os.path.join(ROOT, "g0_cache.json")


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def block(h):
    for attempt in range(6):
        try:
            b = post(EXPL, {"type": "blockDetails", "height": h})
            bb = b.get("blockDetails", b)
            if b.get("type") == "error" or "txs" not in bb:
                raise ValueError(b.get("message", str(b)[:200]))
            if len(bb["txs"]) != bb.get("numTxs"):
                raise ValueError("incomplete")
            return b
        except Exception as e:
            if "archived blocks" in str(e):
                sys.exit(f"STOP: daily archive cap: {e}")
            last = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"block {h}: {last}")


# newest height: probe upward from the newest recorded header (~14.5 blocks/s)
hdrs = sorted(glob.glob(os.path.join(ROOT, "hl_data", "*", "*", "blocks.jsonl*")))
op = gzip.open if hdrs[-1].endswith(".gz") else open
last = json.loads(list(op(hdrs[-1], "rt"))[-1])["msg"]
guess = int(last["height"] + (time.time() * 1000 - last["blockTime"]) / 1000 * 14.5)
lo, hi = guess - 60000, guess + 60000                   # binary search for the newest served height
while hi - lo > 50:
    mid = (lo + hi) // 2
    r = post(EXPL, {"type": "blockDetails", "height": mid})
    ok = "txs" in r.get("blockDetails", r)
    if r.get("type") == "error" and "archived" in r.get("message", ""):
        sys.exit("STOP: daily archive cap")
    lo, hi = (mid, hi) if ok else (lo, mid)
    time.sleep(0.8)
start = lo - 100                                        # stay a little behind the head
end = start + N - 1
out = os.path.join(ROOT, "blocks", f"live-{start}-{end}")
os.makedirs(out, exist_ok=True)
print(f"fetching {start}..{end} into {out}")
cancels = []
for i, h in enumerate(range(start, end + 1)):
    p = os.path.join(out, f"{h}.json.gz")
    if os.path.exists(p):
        continue
    while True:
        try:
            b = block(h)
            break
        except RuntimeError:                            # not produced yet: we caught up with the head
            time.sleep(2)
    with gzip.open(p + ".tmp", "wt") as f:
        json.dump(b, f, separators=(",", ":"))
    os.replace(p + ".tmp", p)
    for tx in b.get("blockDetails", b)["txs"]:
        a = tx["action"]
        if a.get("type") == "cancel" and tx["error"] is None:
            for c in a["cancels"]:
                if c.get("a") in (0, 1):
                    cancels.append((h, tx["user"], c["o"]))
    if i % 50 == 0:
        print(f"  {i}/{N} blocks, {len(cancels)} BTC/ETH cancels so far")
    time.sleep(0.6)

# orderStatus right away; prefer quieter users (busy market makers' old orders drop out first)
per_user = collections.Counter(u for _, u, _ in cancels)
pick, seen = [], set()
for h, u, o in sorted(cancels, key=lambda c: (per_user[c[1]], c[0])):
    if u not in seen or per_user[u] < 50:
        pick.append((u, o)); seen.add(u)
    if len(pick) >= 150:
        break
cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
st = collections.Counter()
for u, o in pick:
    try:
        r = post(INFO, {"type": "orderStatus", "user": u, "oid": o})
    except Exception as e:
        r = {"error": repr(e)}
    cache[f"{u}:{o}"] = r
    st[r.get("status", "err")] += 1
    time.sleep(0.25)
json.dump(cache, open(CACHE, "w"))
print("orderStatus answers:", dict(st))
