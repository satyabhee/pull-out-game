"""
record_hl.py v2: free Hyperliquid recorder (no AWS).

Records, for the coins you pass:
  trades_<COIN>.jsonl  every public trade (px, sz, time, hash, users)  -> real fills to score your replay
  book_<COIN>.jsonl    L2 book snapshots                                -> sanity checks
  bbo_<COIN>.jsonl     best bid/offer on every block where it changes   -> the real touch (replay ALO gate)
and network-wide:
  blocks.jsonl         every block header (height, blockTime, numTxs)   -> maps clock time to block height
optional extra streams (environment variables):
  book_<COIN>_sf<N>.jsonl  L2_SIGFIGS=4 (default; "" = off): l2Book aggregated to N significant figures, so the
                       20 levels per side reach much deeper than the full-precision book (own connection: the
                       messages do not say which aggregation they are)
  user_orders.jsonl    TRACK_USERS=6 (default; 0 = off): orderUpdates (every placement / cancel / fill / reject of
  user_fills.jsonl     that user, with oid) and userFills. TRACK_BY=cancels (default): the users with the most int-oid
                       cancels in capture.py's last 30 chunks (cancellers.json, used if < 1 h old; their cancels are
                       what the replay cannot resolve without exact oids); else / TRACK_BY=makers: the top makers by
                       BTC/ETH maker notional. Re-picked
  tracked_users.jsonl  every TRACK_EVERY_S=1800 s (first pick after TRACK_WARMUP_S=600 s, or TRACK_USER_LIST=a,b at
                       start). One connection per user, because orderUpdates messages carry no user; the IP allows
                       10 connections, so api + rpc + book_sf + 6 users = 9. tracked_users.jsonl logs each set with
                       its start time: a user's stream is complete only while they are in the set.

It does NOT record transactions: the live explorer tx feed is only a ~1-in-2000 sample.
Fetch complete blocks later with fetch_blocks.py for the time windows you need.

Files roll over every hour into hl_data/YYYY-MM-DD/HH/ and are gzipped when closed.
Usage:  python3 record_hl.py BTC ETH      (Mac: caffeinate -i python3 record_hl.py BTC ETH)
"""
import asyncio, gzip, json, os, random, shutil, sys, time

COINS = sys.argv[1:] or ["BTC"]
L2_SIGFIGS = os.environ.get("L2_SIGFIGS", "4")
TRACK_USERS = int(os.environ.get("TRACK_USERS", 6))
TRACK_EVERY_S = float(os.environ.get("TRACK_EVERY_S", 1800))
TRACK_WARMUP_S = float(os.environ.get("TRACK_WARMUP_S", 600))
TRACK_BY = os.environ.get("TRACK_BY", "cancels")          # cancels (capture's cancellers.json) | makers
CANCELLERS_FILE = os.environ.get("CANCELLERS_FILE", "cancellers.json")
# Randomized tracking (an experiment): at each re-pick a seeded coin flip picks the arm; "contested" = the file's
# "counts" order (the ranker's contested makers first), "cancels" = its "cancel_scores" order. Logged per pick.
TRACK_ARMS = [a for a in os.environ.get("TRACK_ARMS", "").split(",") if a]
LAST_ARM = {"arm": None}
TRACK_USER_LIST = [u.strip().lower() for u in os.environ.get("TRACK_USER_LIST", "").split(",") if u.strip()]
ROOT = "hl_data"
API_WS = "wss://api.hyperliquid.xyz/ws"
RPC_WS = "wss://rpc.hyperliquid.xyz/ws"


class Sink:
    """Appends JSON lines to hl_data/<date>/<hour>/<name>.jsonl and gzips each hour's file when the hour changes."""
    def __init__(self, name):
        self.name, self.hour, self.f, self.path = name, None, None, None

    def write(self, obj):
        hour = time.strftime("%Y-%m-%d/%H", time.gmtime())
        if hour != self.hour:
            self.close()
            d = os.path.join(ROOT, hour)
            os.makedirs(d, exist_ok=True)
            self.path = os.path.join(d, self.name + ".jsonl")
            self.f, self.hour = open(self.path, "a", buffering=1), hour
        self.f.write(json.dumps({"recv_ms": int(time.time() * 1000), "msg": obj}) + "\n")

    def close(self):
        if self.f:
            self.f.close()
            with open(self.path, "rb") as src, gzip.open(self.path + ".gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(self.path)
            self.f = None


async def stream(url, subs, route):
    import websockets   # imported here so tests can import this module without it
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, max_size=None) as ws:
                for s in subs:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": s}))
                print(f"connected {url}")
                async for raw in ws:
                    route(json.loads(raw))
        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')} {url} dropped ({e!r}); reconnecting in 3s  <- note this gap")
            await asyncio.sleep(3)


def api_subscriptions(coins):
    return [s for c in coins for s in ({"type": "trades", "coin": c}, {"type": "l2Book", "coin": c},
                                       {"type": "bbo", "coin": c})]


def sigfig_subscriptions(coins, n):
    return [{"type": "l2Book", "coin": c, "nSigFigs": int(n)} for c in coins]


def user_subscriptions(user):
    return [{"type": "orderUpdates", "user": user}, {"type": "userFills", "user": user}]


class MakerTally:
    """Maker notional per user from the trades feed; pick(n) = top n since the last pick (ties by address)."""

    def __init__(self):
        self.c = {}

    def feed(self, trades):
        for t in trades:
            try:
                maker = (t["users"][1] if t["side"] == "B" else t["users"][0]).lower()
                self.c[maker] = self.c.get(maker, 0.0) + float(t["px"]) * float(t["sz"])
            except Exception:
                continue

    def pick(self, n):
        top = [u for u, _ in sorted(self.c.items(), key=lambda x: (-x[1], x[0]))][:n]
        self.c = {}
        return top


def route_user(m, user, orders, fills, other):
    """One message of a user's connection -> user_orders / user_fills, tagged with the user."""
    if not isinstance(m, dict) or m.get("channel") in ("subscriptionResponse", "pong"):
        return
    ch = m.get("channel")
    if ch == "orderUpdates":
        orders.write({"user": user, "data": m.get("data")})
    elif ch == "userFills":
        fills.write({"user": user, "data": m.get("data")})
    else:
        other.write({"user": user, "msg": m})


def route_api(m, trades, books, bbos, other, tally=None):
    """Send one api-ws message to its sink (dicts keyed by coin); anything unexpected goes to `other`."""
    if not isinstance(m, dict) or m.get("channel") == "subscriptionResponse":
        return
    d = m.get("data")
    ch = m.get("channel")
    if ch == "trades" and d:
        if tally is not None:
            tally.feed(d)
        trades.get(d[0].get("coin"), other).write(m)
    elif ch == "l2Book" and isinstance(d, dict):
        books.get(d.get("coin"), other).write(m)
    elif ch == "bbo" and isinstance(d, dict):
        bbos.get(d.get("coin"), other).write(m)
    else:
        other.write(m)


def pick_users(tally, n, by=None, path=None, now=None, max_age_s=3600):
    """Users to stream: top int-oid cancellers from cancellers.json when fresh (TRACK_BY=cancels), else top makers.
    The maker tally is reset either way, so it always covers the last period."""
    by = TRACK_BY if by is None else by
    makers = tally.pick(n)
    if by == "cancels":
        try:
            d = json.load(open(path or CANCELLERS_FILE))
            t = now if now is not None else time.time()
            if t * 1000 - d["time"] <= max_age_s * 1000:
                key = "counts"
                if TRACK_ARMS:
                    arm = random.Random(int(t // 60)).choice(TRACK_ARMS)
                    LAST_ARM["arm"] = arm
                    key = "cancel_scores" if arm == "cancels" and d.get("cancel_scores") else "counts"
                top = [u for u, _ in sorted(d[key].items(), key=lambda x: (-x[1], x[0]))][:n]
                if top:
                    return top
        except Exception:
            pass
    return makers


async def track_users(tally, orders, fills, tracked, other, n=None, every_s=None, warmup_s=None, initial=None,
                      start=None, sleep=None, picker=None):
    """Keep one orderUpdates+userFills connection per tracked user; re-pick the set every `every_s`."""
    n = TRACK_USERS if n is None else n
    every_s = TRACK_EVERY_S if every_s is None else every_s
    sleep = sleep or asyncio.sleep
    start = start or (lambda u: asyncio.ensure_future(
        stream(API_WS, user_subscriptions(u), lambda m: route_user(m, u, orders, fills, other))))
    picker = picker or (lambda k: pick_users(tally, k))
    tasks = {}
    users = list(initial if initial is not None else TRACK_USER_LIST)[:n]
    if not users and TRACK_BY == "cancels" and picker is not None and initial is None:
        users = picker(n) if os.path.exists(CANCELLERS_FILE) else []
    if not users:
        await sleep(TRACK_WARMUP_S if warmup_s is None else warmup_s)
        users = picker(n)
    while True:
        for u in [u for u in tasks if u not in users]:
            tasks.pop(u).cancel()
        for u in users:
            if u not in tasks:
                tasks[u] = start(u)
        rec = {"time": int(time.time() * 1000), "users": sorted(tasks)}
        if LAST_ARM["arm"] is not None:
            rec["arm"] = LAST_ARM["arm"]
        tracked.write(rec)
        await sleep(every_s)
        users = picker(n) or users


async def main():
    trades = {c: Sink(f"trades_{c}") for c in COINS}
    books = {c: Sink(f"book_{c}") for c in COINS}
    bbos = {c: Sink(f"bbo_{c}") for c in COINS}
    blocks, other = Sink("blocks"), Sink("other")
    sf_books = {c: Sink(f"book_{c}_sf{L2_SIGFIGS}") for c in COINS} if L2_SIGFIGS else {}
    orders, fills, tracked = Sink("user_orders"), Sink("user_fills"), Sink("tracked_users")
    tally = MakerTally()

    def route_rpc(m):
        # explorer messages arrive as bare lists; keep only block headers
        if isinstance(m, list):
            for it in m:
                if isinstance(it, dict) and "numTxs" in it:
                    blocks.write(it)

    jobs = [stream(API_WS, api_subscriptions(COINS), lambda m: route_api(m, trades, books, bbos, other, tally)),
            stream(RPC_WS, [{"type": "explorerBlock"}], route_rpc)]
    if sf_books:
        jobs.append(stream(API_WS, sigfig_subscriptions(COINS, L2_SIGFIGS),
                           lambda m: route_api(m, {}, sf_books, {}, other)))
    if TRACK_USERS:
        jobs.append(track_users(tally, orders, fills, tracked, other))
    try:
        await asyncio.gather(*jobs)
    finally:
        for s in [*trades.values(), *books.values(), *bbos.values(), *sf_books.values(), blocks, other,
                  orders, fills, tracked]:
            s.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped; current hour's files gzipped")
