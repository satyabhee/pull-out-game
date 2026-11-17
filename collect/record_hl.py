"""
record_hl.py v2: free Hyperliquid recorder (no AWS).

Records, for the coins you pass:
  trades_<COIN>.jsonl  every public trade (px, sz, time, hash, users)  -> real fills to score your replay
  book_<COIN>.jsonl    L2 book snapshots                                -> sanity checks
and network-wide:
  blocks.jsonl         every block header (height, blockTime, numTxs)   -> maps clock time to block height

It does NOT record transactions: the live explorer tx feed is only a ~1-in-2000 sample.
Fetch complete blocks later with fetch_blocks.py for the time windows you need.

Files roll over every hour into hl_data/YYYY-MM-DD/HH/ and are gzipped when closed.
Usage:  python3 record_hl.py BTC ETH      (Mac: caffeinate -i python3 record_hl.py BTC ETH)
"""
import asyncio, gzip, json, os, shutil, sys, time
import websockets

COINS = sys.argv[1:] or ["BTC"]
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


async def main():
    trades = {c: Sink(f"trades_{c}") for c in COINS}
    books = {c: Sink(f"book_{c}") for c in COINS}
    blocks, other = Sink("blocks"), Sink("other")

    def route_api(m):
        if not isinstance(m, dict) or m.get("channel") == "subscriptionResponse":
            return
        d = m.get("data")
        if m.get("channel") == "trades" and d:
            trades.get(d[0].get("coin"), other).write(m)
        elif m.get("channel") == "l2Book" and isinstance(d, dict):
            books.get(d.get("coin"), other).write(m)
        else:
            other.write(m)

    def route_rpc(m):
        # explorer messages arrive as bare lists; keep only block headers
        if isinstance(m, list):
            for it in m:
                if isinstance(it, dict) and "numTxs" in it:
                    blocks.write(it)

    api_subs = [s for c in COINS for s in ({"type": "trades", "coin": c}, {"type": "l2Book", "coin": c})]
    try:
        await asyncio.gather(stream(API_WS, api_subs, route_api),
                             stream(RPC_WS, [{"type": "explorerBlock"}], route_rpc))
    finally:
        for s in [*trades.values(), *books.values(), blocks, other]:
            s.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped; current hour's files gzipped")
