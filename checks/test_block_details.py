"""
Does Hyperliquid's free block-details request return EVERY transaction in a block,
and how far back does it go?

  python3 test_block_details.py                       # newest recorded height and a few older ones
  python3 test_block_details.py 1184000000 1124000000  # specific heights

Hyperliquid makes roughly 1.3 million blocks a day (about 15 per second).
"""
import json, sys, time, urllib.request

URL = "https://rpc.hyperliquid.xyz/explorer"
PER_DAY = 1_300_000


def post(body):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def check(height, newest):
    t0 = time.time()
    try:
        r = post({"type": "blockDetails", "height": height})
    except Exception as e:
        print(f"  height {height}: FAILED ({e})")
        return
    b = r.get("blockDetails", r)
    txs, n = b.get("txs", []), b.get("numTxs")
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(b["blockTime"] / 1000)) if b.get("blockTime") else "?"
    age = f"~{(newest - height) / PER_DAY:.0f} days back" if newest else ""
    errs = sum(1 for t in txs if t.get("error"))
    ok = "COMPLETE" if n is not None and len(txs) == n else "CHECK"
    print(f"  height {height} ({when}, {age}): {len(txs)}/{n} txs {ok}, {errs} with errors, {time.time() - t0:.1f}s")


heights = [int(x) for x in sys.argv[1:]]
newest = 0
try:
    for line in open("hl_data/other.jsonl"):
        for it in json.loads(line)["msg"]:
            if isinstance(it, dict) and "numTxs" in it:
                newest = max(newest, it["height"])
except (FileNotFoundError, TypeError, KeyError):
    pass
if not newest:
    newest = 1_224_000_000          # roughly Nov 16 2026
if not heights:
    heights = [newest - d * PER_DAY for d in (0, 7, 30, 60, 120, 240)]
print(URL)
for h in heights:
    check(h, newest)
    time.sleep(1)
