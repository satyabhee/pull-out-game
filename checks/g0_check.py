"""
g0_check.py: G0 data checks for the Hyperliquid BTC/ETH replay (stdlib only).

  python3 g0_check.py ~/Downloads/server_data                 # offline checks (uses cached API answers if present)
  python3 g0_check.py ~/Downloads/server_data --query 120     # also ask the info API about 120 cancelled orders
                                                              #   (order-id anchors), cached in <root>/g0_cache.json

Sections:
  1 inventory     blocks on disk, heights, times, gaps
  2 decoder       every action shape touching BTC (a=0) / ETH (a=1): tif, trigger, reduce-only, cloid, grouping, errors
  3 fill links    recorded trades vs block tx hashes; is the tx sender the taker?
  4 order ids     anchors (oid known from orderStatus, placement found in blocks) -> which counting rule fits
  5 unreproducible  trigger / TWAP / scheduleCancel / unlinked trades per minute
Streams one block at a time; safe on a 2 GB machine.
"""
import collections, glob, gzip, json, os, random, sys, time, urllib.request
from decimal import Decimal

ROOT = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "~/Downloads/server_data")
QUERY = int(sys.argv[sys.argv.index("--query") + 1]) if "--query" in sys.argv else 0
ASSETS = {0: "BTC", 1: "ETH"}
INFO = "https://api.hyperliquid.xyz/info"
CACHE = os.path.join(ROOT, "g0_cache.json")


def block_files():
    fs = glob.glob(os.path.join(ROOT, "blocks", "*", "*.json.gz"))
    return sorted(fs, key=lambda f: int(os.path.basename(f).split(".")[0]))


BAD = []                                      # block files without the normal shape (counted, skipped)


def blocks():
    for f in block_files():
        with gzip.open(f, "rt") as fh:
            b = json.load(fh)
        b = b.get("blockDetails", b) if isinstance(b, dict) else b
        if not isinstance(b, dict) or "blockTime" not in b or "txs" not in b:
            if f not in [x[0] for x in BAD]:
                BAD.append((f, json.dumps(b)[:200]))
            continue
        yield b


def jsonl(pattern):
    for f in sorted(glob.glob(os.path.join(ROOT, "hl_data", "*", "*", pattern))):
        op = gzip.open if f.endswith(".gz") else open
        with op(f, "rt") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass                      # last line of a file still being written


def tif(o):
    t = o.get("t", {})
    if "limit" in t:
        return t["limit"].get("tif")
    if "trigger" in t:
        tr = t["trigger"]
        return f"trigger/{tr.get('tpsl')}/{'mkt' if tr.get('isMarket') else 'lmt'}"
    return json.dumps(t)


def order_items(a):
    """(kind, order dict, modify-target) for every new-order-like item in an action."""
    ty = a.get("type")
    if ty == "order":
        for o in a.get("orders", []):
            yield "order", o, None
    elif ty == "modify":
        yield "modify", a.get("order", {}), a.get("oid")
    elif ty == "batchModify":
        for m in a.get("modifies", []):
            yield "batchModify", m.get("order", {}), m.get("oid")


def H(title):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


# ---------------------------------------------------------------- 1 inventory
H("1. INVENTORY")
files = block_files()
heights = [int(os.path.basename(f).split(".")[0]) for f in files]
print(f"block files: {len(files)}  heights {heights[0]}..{heights[-1]}  "
      f"missing inside range: {heights[-1] - heights[0] + 1 - len(heights)}")

trades = []
for m in jsonl("trades_*.jsonl*"):
    for t in m["msg"].get("data", []):
        trades.append(t)
seen_tid = set()
trades = [t for t in trades if not (t["tid"] in seen_tid or seen_tid.add(t["tid"]))]
print(f"recorded trades (dedup by tid): {len(trades)}  " + str(collections.Counter(t["coin"] for t in trades)))
if trades:
    print("trades span", time.strftime("%m-%d %H:%M:%S", time.gmtime(min(t["time"] for t in trades) / 1000)), "to",
          time.strftime("%m-%d %H:%M:%S", time.gmtime(max(t["time"] for t in trades) / 1000)), "UTC")

# ---------------------------------------------------------------- one streaming pass for 2, 3, 5 and anchor candidates
trade_by_hash = collections.defaultdict(list)
for t in trades:
    trade_by_hash[t["hash"]].append(t)

shape = collections.Counter()                 # (action type, detail) for BTC/ETH items
errs = collections.Counter()
err_examples = collections.defaultdict(list)
modify_oid_kind = collections.Counter()
grouping = collections.Counter()
builder = 0
other_types = collections.Counter()           # non-order action types seen anywhere (ignored by the book)
linked = {}                                   # trade hash -> tx
t_first = t_last = None
cancel_cands = []                             # (height, user, asset, oid)
twap_btc_eth = sched = trig = 0
nblk = 0
for b in blocks():
    nblk += 1
    bt = b["blockTime"]
    t_first = t_first or bt
    t_last = bt
    for tx in b["txs"]:
        a = tx["action"]
        ty = a.get("type")
        if tx["hash"] in trade_by_hash:
            linked[tx["hash"]] = {"user": tx["user"], "type": ty, "err": tx["error"], "block": b["height"],
                                  "time": tx["time"], "action": a}
        touches = False
        for kind, o, tgt in order_items(a):
            if o.get("a") not in ASSETS:
                continue
            touches = True
            shape[(kind, tif(o), "reduceOnly" if o.get("r") else "-", "cloid" if o.get("c") else "no-cloid")] += 1
            if "trigger" in o.get("t", {}):
                trig += 1
            if tgt is not None:
                modify_oid_kind[(kind, "int" if isinstance(tgt, int) else "cloid-str")] += 1
        if ty == "order" and touches:
            g = a.get("grouping")
            grouping["na" if g == "na" else ("p" if isinstance(g, dict) and "p" in g else json.dumps(g))] += 1
            builder += "builder" in a
        if ty in ("cancel", "cancelByCloid"):
            for c in a.get("cancels", []):
                asset = c.get("a", c.get("asset"))
                if asset in ASSETS:
                    touches = True
                    shape[(ty, "f" if a.get("f") else "-")] += 1
                    if ty == "cancel" and tx["error"] is None:
                        cancel_cands.append((b["height"], tx["user"], asset, c["o"]))
        if ty == "twapOrder" and a.get("twap", {}).get("a") in ASSETS:
            touches = True
            twap_btc_eth += 1
            shape[("twapOrder", json.dumps({k: v for k, v in a["twap"].items() if k not in ("s",)}))] += 1
        if ty == "scheduleCancel":
            sched += 1
        if ty not in ("order", "modify", "batchModify", "cancel", "cancelByCloid", "twapOrder"):
            other_types[ty] += 1
        if touches and tx["error"]:
            errs[ty] += 1
            if len(err_examples[ty]) < 4:
                err_examples[ty].append(str(tx["error"])[:160])
minutes = (t_last - t_first) / 60000
print(f"odd block files skipped: {len(BAD)}", BAD[:3])

H("2. DECODER CENSUS (items on BTC/ETH)")
print(f"{nblk} blocks, {time.strftime('%H:%M:%S', time.gmtime(t_first / 1000))}-"
      f"{time.strftime('%H:%M:%S', time.gmtime(t_last / 1000))} UTC = {minutes:.1f} min")
for k, v in sorted(shape.items(), key=lambda kv: -kv[1]):
    print(f"  {v:>9}  {k}")
print("modify/batchModify target oid kind:", dict(modify_oid_kind))
print("grouping on BTC/ETH order actions:", dict(grouping), " with builder field:", builder)
print("errored BTC/ETH txs by type:", dict(errs))
for ty, ex in err_examples.items():
    for e in ex:
        print(f"    {ty}: {e}")
print("other action types (no BTC/ETH book effect expected):", dict(other_types.most_common(25)))

# ---------------------------------------------------------------- 3 fill links
H("3. FILL -> TX LINKS (trades inside the block window)")
win = [t for t in trades if t_first <= t["time"] <= t_last]
zero = [t for t in win if int(t["hash"], 16) == 0]
nz = [t for t in win if int(t["hash"], 16) != 0]
lk = [t for t in nz if t["hash"] in linked]
print(f"trades in window: {len(win)}  all-zero hash: {len(zero)}  non-zero: {len(nz)}  linked by hash: {len(lk)} "
      f"({100 * len(lk) / max(len(nz), 1):.2f}% of non-zero)")
taker_is_sender = collections.Counter()
link_types = collections.Counter()
dt = []
for t in lk:
    tx = linked[t["hash"]]
    taker = t["users"][0] if t["side"] == "B" else t["users"][1]
    maker = t["users"][1] if t["side"] == "B" else t["users"][0]
    taker_is_sender["taker" if tx["user"] == taker else ("maker" if tx["user"] == maker else "neither")] += 1
    link_types[(tx["type"], "err" if tx["err"] else "ok")] += 1
    dt.append(t["time"] - tx["time"])
print("tx sender is:", dict(taker_is_sender), "  (side B = taker bought, users=[buyer, seller])")
print("linked tx types:", dict(link_types))
if dt:
    print("trade.time - tx.time (ms): min", min(dt), "max", max(dt))
unl = [t for t in nz if t["hash"] not in linked]
print(f"non-zero unlinked: {len(unl)}", [(t['coin'], t['time'], t['hash'][:12]) for t in unl[:5]])
zu = collections.Counter(tuple(t["users"]) for t in zero)
print("all-zero-hash trades: distinct (buyer,seller) pairs:", len(zu), " most common:", zu.most_common(3))
print("hashes -> trades per hash (one taker tx sweeping several makers):",
      dict(collections.Counter(len(v) for h, v in trade_by_hash.items() if h in linked)))

# ---------------------------------------------------------------- 5 unreproducible
H("5. UNREPRODUCIBLE ACTIVITY (per minute of block window)")
for name, n in [("trigger orders BTC/ETH (placed)", trig), ("twapOrder BTC/ETH", twap_btc_eth),
                ("scheduleCancel (all assets, asset unknown)", sched),
                ("all-zero-hash trades BTC/ETH", len(zero)), ("non-zero unlinked trades", len(unl))]:
    print(f"  {name:<45} {n:>8}  {n / max(minutes, 1e-9):8.2f}/min")

# ---------------------------------------------------------------- 4 order ids
H("4. ORDER-ID RULE (anchors)")
cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}


def info(body):
    req = urllib.request.Request(INFO, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


if QUERY:
    lo = heights[0] + (heights[-1] - heights[0]) // 20          # skip first 5%: placement must be in window
    pool = [c for c in cancel_cands if c[0] >= lo]
    random.seed(0)
    pick = sorted(random.sample(pool, min(QUERY, len(pool))))
    asked = 0
    for h, user, asset, oid in pick:
        key = f"{user}:{oid}"
        if key in cache:
            continue
        try:
            cache[key] = info({"type": "orderStatus", "user": user, "oid": oid})
        except Exception as e:
            cache[key] = {"error": repr(e)}
        asked += 1
        time.sleep(0.3)
    json.dump(cache, open(CACHE, "w"))
    print(f"asked info API about {asked} orders (cache now {len(cache)})")

want = {}                                    # (user, asset, isBuy, px, origSz, ts) -> [oid, cloid]
for key, r in cache.items():
    o = (r.get("order") or {}).get("order") if isinstance(r, dict) else None
    if not o:
        continue
    asset = {"BTC": 0, "ETH": 1}.get(o["coin"])
    if asset is None:
        continue
    k = (key.split(":")[0], asset, o["side"] == "B", Decimal(o["limitPx"]), Decimal(o["origSz"]), o["timestamp"])
    want[k] = [o["oid"], o.get("cloid")]
print(f"orderStatus answers usable as anchors: {len(want)} (of {len(cache)} cached)")

if True:
    # second pass: find each anchor's placement and snapshot running counters of candidate oid-consuming events
    CATS = ["order_ok", "order_err", "modify_ok", "modify_err", "bmod_ok", "bmod_err", "twap_ok", "twap_err",
            "cancel_tx", "other_tx"]
    run = dict.fromkeys(CATS, 0)
    anchors, ambiguous = [], collections.Counter()
    found = collections.defaultdict(list)
    seg, prev, bound = 0, None, []             # seg = unbroken run of heights; bound = per-block max cancelled oid
    for b in blocks():
        if prev is not None and b["height"] != prev + 1:
            seg += 1
        prev, mx = b["height"], None
        for i, tx in enumerate(b["txs"]):
            a, ok = tx["action"], tx["error"] is None
            ty = a.get("type")
            items = list(order_items(a))
            for j, (kind, o, tgt) in enumerate(items):
                if o.get("a") in ASSETS:
                    try:
                        k = (tx["user"], o["a"], o["b"], Decimal(o["p"]), Decimal(o["s"]), tx["time"])
                    except Exception:
                        k = None
                    if k in want and (want[k][1] is None or want[k][1] == o.get("c")):
                        snap = dict(run)
                        cat = {"order": "order", "modify": "modify", "batchModify": "bmod"}[kind]
                        snap[cat + ("_ok" if ok else "_err")] += j       # items earlier in the same action
                        found[k].append((want[k][0], b["height"], i, j, kind, ok, snap, seg))
            if ty == "order":
                run["order_ok" if ok else "order_err"] += len(items)
            elif ty == "modify":
                run["modify_ok" if ok else "modify_err"] += 1
            elif ty == "batchModify":
                run["bmod_ok" if ok else "bmod_err"] += len(items)
            elif ty == "twapOrder":
                run["twap_ok" if ok else "twap_err"] += 1
            elif ty in ("cancel", "cancelByCloid"):
                run["cancel_tx"] += 1
                if ty == "cancel" and ok:
                    for c in a.get("cancels", []):
                        if isinstance(c.get("o"), int) and (mx is None or c["o"] > mx):
                            mx = c["o"]
            else:
                run["other_tx"] += 1
        bound.append((seg, b["height"], mx, dict(run)))
    for k, v in found.items():
        if len(v) == 1:
            anchors.append(v[0])
        else:
            ambiguous[len(v)] += 1
    anchors.sort(key=lambda x: (x[1], x[2], x[3]))
    print(f"anchors placed in blocks: {len(anchors)}  ambiguous: {dict(ambiguous)}  "
          f"not found: {len(want) - len(found)}")
    print("anchor kinds:", dict(collections.Counter((x[4], x[5]) for x in anchors)))
    rules = {
        "A all order-items (order+modify+bmod, ok+err)": ["order_ok", "order_err", "modify_ok", "modify_err", "bmod_ok", "bmod_err"],
        "B ok order-items only": ["order_ok", "modify_ok", "bmod_ok"],
        "C order actions only (ok+err)": ["order_ok", "order_err"],
        "D ok order actions only": ["order_ok"],
        "E A + twap": ["order_ok", "order_err", "modify_ok", "modify_err", "bmod_ok", "bmod_err", "twap_ok", "twap_err"],
        "F B + ok twap": ["order_ok", "modify_ok", "bmod_ok", "twap_ok"],
    }
    pairs = [(x, y) for x, y in zip(anchors, anchors[1:]) if x[7] == y[7]]
    print(f"\n{len(pairs)} consecutive anchor pairs; residual = (oid gap) - (counted events)")
    for name, cats in rules.items():
        res = [(y[0] - x[0]) - sum(y[6][c] - x[6][c] for c in cats) for x, y in pairs]
        ratio = [(y[0] - x[0]) / max(sum(y[6][c] - x[6][c] for c in cats), 1) for x, y in pairs]
        if res:
            print(f"  {name:<50} exact={sum(r == 0 for r in res):>3}/{len(res)}  "
                  f"residual min {min(res)} median {sorted(res)[len(res) // 2]} max {max(res)}  "
                  f"oid/event median {sorted(ratio)[len(ratio) // 2]:.3f}")
    print("\nfirst anchor pairs (oid gap vs per-category counts):")
    for x, y in pairs[:12]:
        d = {c: y[6][c] - x[6][c] for c in CATS if y[6][c] - x[6][c]}
        print(f"  h{x[1]}->h{y[1]} oid {x[0]}->{y[0]} gap {y[0] - x[0]}  {d}")
    print("\nAPI-free check: highest oid cancelled per block (all markets) vs counted placements, 300-block steps")
    for name, cats in rules.items():
        ratios = []
        for sg in sorted({x[0] for x in bound}):
            pts = [x for x in bound if x[0] == sg and x[2] is not None]
            for p0, p1 in zip(pts[::300], pts[300::300]):
                ev = sum(p1[3][c] - p0[3][c] for c in cats)
                if ev:
                    ratios.append((p1[2] - p0[2]) / ev)
        if ratios:
            ratios.sort()
            print(f"  {name:<50} oid growth / events: median {ratios[len(ratios)//2]:.4f}  "
                  f"p10 {ratios[len(ratios)//10]:.4f}  p90 {ratios[9*len(ratios)//10]:.4f}  (n={len(ratios)})")
    json.dump([{"oid": x[0], "height": x[1], "tx": x[2], "item": x[3], "kind": x[4], "ok": x[5], "counts": x[6]}
               for x in anchors], open(os.path.join(ROOT, "g0_anchors.json"), "w"), indent=0)
    print(f"anchors saved to {os.path.join(ROOT, 'g0_anchors.json')}")
else:
    print("no anchors yet: rerun with --query 120")
