"""
Does Hyperliquid's free block-details request return EVERY transaction in a block,
and how far back does it go?

  python3 test_block_details.py                       # newest recorded height and a few older ones
  python3 test_block_details.py 1184000000 1124000000  # specific heights

WARNING: blocks older than ~3 h are "archived" (100/day per IP; going over locks the IP out of every
block for the day, the capture service included). Heights > 2 h old are refused unless --allow-archived.

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


ALLOW = "--allow-archived" in sys.argv
heights = [int(x) for x in sys.argv[1:] if x != "--allow-archived"]
newest = 0
try:
    for line in open("hl_data/other.jsonl"):
        for it in json.loads(line)["msg"]:
            if isinstance(it, dict) and "numTxs" in it:
                newest = max(newest, it["height"])
except (FileNotFoundError, TypeError, KeyError):
    pass
if not newest:                      # extrapolate from a known header (height 1224219680 at 2026-11-17 05:00 UTC)
    newest = int(1_224_219_680 + (time.time() * 1000 - 1_794_891_599_767) / 1000 * 14.5)
if not heights:
    heights = [newest - d * PER_DAY for d in (0, 7, 30, 60, 120, 240)]
old = [h for h in heights if (newest - h) / 14.5 / 60 > 120]
if old and not ALLOW:
    sys.exit(f"REFUSING: {len(old)} of these heights are > 2 h old, i.e. archived. Each one spends the IP's "
             f"100/day archived budget, and going over locks the IP out of ALL blocks for the day "
             f"(the capture service too). Rerun with --allow-archived if that's really intended.")
print(URL)
for h in heights:
    check(h, newest)
    time.sleep(1)
