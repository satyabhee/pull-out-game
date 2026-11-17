"""
fetch_blocks.py: download COMPLETE Hyperliquid blocks for free from the public explorer.

  python3 fetch_blocks.py START_HEIGHT END_HEIGHT              # inclusive range -> blocks/<START>-<END>/
  python3 fetch_blocks.py START END --out DIR                  # choose the folder
  python3 fetch_blocks.py START END --keepalive --interval 0.8  # experimental: one kept-open connection

Default = a fresh HTTPS connection per request. Measured Nov 17 (200 fresh blocks each): fresh
connection 1.06-1.12 blocks/s; one keep-alive connection 0.84-0.98 blocks/s (more HTTP 429s, at
any pacing). The explorer does not serve gzip (0 gzip responses), so Accept-Encoding: gzip is sent
but has no effect.

One request at a time (the explorer rate-limits per IP; parallel requests are slower).
Every block is checked
(len(txs) == numTxs, error responses refused), written via temp file + rename, and
skipped on rerun. Backs off on HTTP 429. Stops on the daily archived-block cap.
capture.py imports Fetcher from this file.
"""
import argparse, glob, gzip, http.client, json, os, random, sys, time, urllib.request

HOST, PATH = "rpc.hyperliquid.xyz", "/explorer"
DEFAULT_INTERVAL = 0.8                  # pacing for --keepalive only


class ArchivedCap(Exception):
    """'More than 100 archived blocks queried in one day': stop fetching old blocks today."""


class NotYet(Exception):
    """Height not produced yet ('invalid block height')."""


class Fetcher:
    def __init__(self, legacy=False, interval=0.0):
        self.legacy, self.conn, self.interval = legacy, None, interval
        self.gzip_seen = self.plain_seen = self.http429 = 0
        self._last = 0.0

    def _pace(self):
        """Space requests >= interval apart: back-to-back keep-alive requests trip HTTP 429 (+1-2 s back-off)."""
        wait = self._last + self.interval - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _raw(self, h):
        body = json.dumps({"type": "blockDetails", "height": h}).encode()
        self._pace()
        if self.legacy:
            req = urllib.request.Request("https://" + HOST + PATH, data=body,
                                         headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
                    if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                        self.gzip_seen += 1
                        return 200, gzip.decompress(data)
                    self.plain_seen += 1
                    return 200, data
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        if self.conn is None:
            self.conn = http.client.HTTPSConnection(HOST, timeout=60)
        try:
            self.conn.request("POST", PATH, body=body, headers={
                "Content-Type": "application/json", "Accept-Encoding": "gzip", "Connection": "keep-alive"})
            r = self.conn.getresponse()
            data = r.read()
        except Exception:
            self.close()                       # reconnect on the next call
            raise
        if r.getheader("Content-Encoding", "").lower() == "gzip":
            data = gzip.decompress(data)
            self.gzip_seen += 1
        else:
            self.plain_seen += 1
        if (r.getheader("Connection") or "").lower() == "close":
            self.close()
        return r.status, data

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def get(self, h, tries=8):
        """Return (parsed block, raw json bytes). Raises ArchivedCap, NotYet, or RuntimeError after `tries`."""
        delay, last = 1.0, None
        for _ in range(tries):
            try:
                status, data = self._raw(h)
                if status == 429:
                    self.http429 += 1
                    last = "HTTP 429"
                    time.sleep(delay + random.random()); delay = min(delay * 2, 60); continue
                if status != 200:
                    raise ValueError(f"HTTP {status}: {data[:200]!r}")
                blk = json.loads(data)
                b = blk.get("blockDetails", blk) if isinstance(blk, dict) else None
                if not isinstance(b, dict) or blk.get("type") == "error" or not isinstance(b.get("txs"), list):
                    msg = blk.get("message", str(blk)[:200]) if isinstance(blk, dict) else str(blk)[:200]
                    if "archived blocks" in msg:
                        raise ArchivedCap(msg)
                    if "invalid block height" in msg:
                        raise NotYet(msg)
                    raise ValueError(msg)
                if b.get("numTxs") is None or len(b["txs"]) != b["numTxs"]:
                    raise ValueError(f"incomplete: {len(b['txs'])}/{b.get('numTxs')}")
                return b, data
            except (ArchivedCap, NotYet):
                raise
            except Exception as e:
                last = e
            time.sleep(delay); delay = min(delay * 2, 60)
        raise RuntimeError(f"block {h}: {last}")


def save(path, data):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)                      # never leaves a half-written block behind


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", type=int)
    ap.add_argument("end", type=int)
    ap.add_argument("--out")
    ap.add_argument("--max-age-min", type=float, default=120,
                    help="stop before requesting blocks older than this (archived blocks: 100/day/IP cap)")
    ap.add_argument("--keepalive", action="store_true", help="one kept-open connection (measured slower)")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="min seconds between requests (keepalive)")
    args = ap.parse_args()
    out = args.out or os.path.join("blocks", f"{args.start}-{args.end}")
    os.makedirs(out, exist_ok=True)
    todo = [h for h in range(args.start, args.end + 1) if not os.path.exists(os.path.join(out, f"{h}.json.gz"))]
    print(f"{args.end - args.start + 1} blocks in range, {len(todo)} to fetch "
          f"({'keep-alive' if args.keepalive else 'fresh connection per request'})")
    f, t0, failed = Fetcher(not args.keepalive, args.interval if args.keepalive else 0), time.time(), []
    prev_bt = None
    hdrs = sorted(glob.glob("hl_data/*/*/blocks.jsonl"))       # recorder headers, if run next to the recorder
    if todo and hdrs:
        try:
            m = json.loads(open(hdrs[-1]).readlines()[-1])
            age = ((m["msg"]["height"] - todo[0]) / 14.5 + (time.time() * 1000 - m["recv_ms"]) / 1000) / 60
            if age > args.max_age_min:
                print(f"STOPPING: first block is ~{age:.0f} min old > {args.max_age_min:.0f} min; not requesting it")
                sys.exit(2)
        except (ValueError, KeyError, IndexError):
            pass
    for i, h in enumerate(todo, 1):
        if prev_bt and (time.time() * 1000 - prev_bt) / 60000 > args.max_age_min:
            print(f"STOPPING before height {h}: blocks are older than {args.max_age_min:.0f} min "
                  f"(archived blocks are capped at 100/day per IP)"); break
        while True:
            try:
                b, data = f.get(h)
                prev_bt = b["blockTime"]
                save(os.path.join(out, f"{h}.json.gz"), data)
                break
            except NotYet:
                time.sleep(1)                  # caught up with the head
            except ArchivedCap as e:
                print(f"STOPPING: explorer daily cap reached ({e})"); sys.exit(2)
            except RuntimeError as e:
                failed.append(h); print(f"  {e}"); break
        if i % 50 == 0 or i == len(todo):
            rate = i / (time.time() - t0)
            print(f"{i}/{len(todo)} fetched, {rate:.2f} blocks/s, ETA {(len(todo) - i) / rate / 60:.0f} min", flush=True)
    rate = len(todo) / max(time.time() - t0, 1e-9)
    print(f"RESULT blocks={len(todo)} seconds={time.time() - t0:.1f} blocks_per_s={rate:.3f} "
          f"gzip_responses={f.gzip_seen} plain_responses={f.plain_seen} http429={f.http429} failed={len(failed)}")


if __name__ == "__main__":
    main()
