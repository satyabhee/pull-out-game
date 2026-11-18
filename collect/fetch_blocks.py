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
Only one explorer client per machine: Fetcher takes an exclusive lock (explorer.lock next to this file,
or $HL_EXPLORER_LOCK), so a hand-run fetch can't share the IP's budget with the capture service.

Archived-block guard (fail closed). The explorer caps "archived" blocks per IP per day, and going over
refuses every block except the newest until 00:00 UTC. The rule is NOT what we assumed (">~3 h old,
100/day"): on Nov 18 the cap hit while fetching ~84-min-old blocks, after ~10.5k blocks that day aged
7-110 min, so "archived" likely starts within minutes and the budget is in the thousands; capture.py
now logs every block's age and the day's count to pin it down. The cap error comes too late to
protect anything, so before EVERY request Fetcher estimates the block's age from the nearest known
(height, blockTime) reference (the recorder's newest header, or the last block it fetched) and raises
TooOld, with no request sent, if the age is over max_age_min (30) or can't be estimated.
Every block is checked
(len(txs) == numTxs, error responses refused), written via temp file + rename, and
skipped on rerun. Backs off on HTTP 429. Stops on the daily archived-block cap.
capture.py imports Fetcher from this file.
"""
import argparse, fcntl, glob, gzip, http.client, json, os, random, sys, time, urllib.request

HOST, PATH = "rpc.hyperliquid.xyz", "/explorer"
DEFAULT_INTERVAL = 0.8                  # pacing for --keepalive only
BPS = 14.5                              # Hyperliquid blocks per second (approx.)
MAX_AGE_MIN = 30.0                      # never request a block older than this (see the guard note above)
MAX_REF_GAP_MIN = 240.0                 # a reference further than this (in blocks / BPS) is too imprecise
LOCK = os.environ.get("HL_EXPLORER_LOCK", os.path.join(os.path.dirname(os.path.abspath(__file__)), "explorer.lock"))


class ArchivedCap(Exception):
    """'More than 100 archived blocks queried in one day': stop fetching old blocks today."""


class NotYet(Exception):
    """Height not produced yet ('invalid block height')."""


class TooOld(Exception):
    """Refused BEFORE requesting: the block may be archived, or its age can't be estimated (fail closed)."""


class Busy(Exception):
    """Another explorer client on this machine holds the lock."""


def last_line(path):
    """Last complete line of a (possibly .gz, possibly still growing) jsonl file, or None."""
    if path.endswith(".gz"):
        last = None
        with gzip.open(path, "rt") as f:
            for last in f:
                pass
        return last
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 8192))
        lines = [l for l in f.read().splitlines() if l.strip()]
    return lines[-1].decode() if lines else None


def recorder_head(hl_data):
    """(height, blockTime_ms) of the newest block header the recorder wrote under hl_data, or None."""
    for p in reversed(sorted(glob.glob(os.path.join(hl_data, "*", "*", "blocks.jsonl*")))[-2:]):
        for _ in range(3):                     # the last line may be half-written
            try:
                m = json.loads(last_line(p))["msg"]
                return int(m["height"]), int(m["blockTime"])
            except Exception:
                time.sleep(0.2)
    return None


def acquire_lock(path):
    f = open(path, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise Busy(f"another explorer client holds {path} (one client per IP; stop it first, "
                   f"e.g. sudo systemctl stop hl-capture)")
    return f


class Fetcher:
    def __init__(self, legacy=False, interval=0.0, head=None, max_age_min=MAX_AGE_MIN, lock=LOCK):
        """head: callable -> (height, blockTime_ms) of a recent block, or None. lock: path or None (tests)."""
        self.legacy, self.conn, self.interval = legacy, None, interval
        self.gzip_seen = self.plain_seen = self.http429 = 0
        self._last = 0.0
        self.head, self.max_age_min = head, max_age_min
        self.ref = None                        # (height, blockTime_ms) of the last block fetched
        self.last_age_min = None               # estimated age of the last block requested
        self._lock = acquire_lock(lock) if lock else None

    def age_min(self, h):
        """Estimated age of height h in minutes, from the nearest reference; None if no usable reference."""
        refs = [r for r in (self.ref, self.head() if self.head else None) if r]
        if not refs:
            return None
        rh, rt = min(refs, key=lambda r: abs(r[0] - h))
        if abs(rh - h) / BPS / 60 > MAX_REF_GAP_MIN:
            return None
        return ((rh - h) / BPS * 1000 + time.time() * 1000 - rt) / 60000

    def check_age(self, h):
        age = self.age_min(h)
        if age is None:
            raise TooOld(f"block {h}: age unknown (no recorder header or fetched block near it); not requesting")
        if age > self.max_age_min:
            raise TooOld(f"block {h} is ~{age:.0f} min old > {self.max_age_min:.0f} min; not requesting "
                         f"(archived blocks: 100/day per IP)")
        return age

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
        """Return (parsed block, raw json bytes). Raises TooOld (nothing sent), ArchivedCap, NotYet,
        or RuntimeError after `tries`."""
        delay, last = 1.0, None
        for _ in range(tries):
            self.last_age_min = self.check_age(h)   # before EVERY request, retries included
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
                if b.get("blockTime"):
                    self.ref = (h, int(b["blockTime"]))
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
    ap.add_argument("--hl-data", default="hl_data",
                    help="recorder data dir; its newest block header dates the blocks (default: ./hl_data)")
    ap.add_argument("--max-age-min", type=float, default=MAX_AGE_MIN,
                    help="never request blocks older than this (daily archived-block cap per IP)")
    ap.add_argument("--keepalive", action="store_true", help="one kept-open connection (measured slower)")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="min seconds between requests (keepalive)")
    args = ap.parse_args()
    out = args.out or os.path.join("blocks", f"{args.start}-{args.end}")
    os.makedirs(out, exist_ok=True)
    todo = [h for h in range(args.start, args.end + 1) if not os.path.exists(os.path.join(out, f"{h}.json.gz"))]
    print(f"{args.end - args.start + 1} blocks in range, {len(todo)} to fetch "
          f"({'keep-alive' if args.keepalive else 'fresh connection per request'})")
    try:
        f = Fetcher(not args.keepalive, args.interval if args.keepalive else 0,
                    head=lambda: recorder_head(args.hl_data), max_age_min=args.max_age_min)
    except Busy as e:
        print(f"STOPPING: {e}"); sys.exit(2)
    t0, failed = time.time(), []
    for i, h in enumerate(todo, 1):
        while True:
            try:
                b, data = f.get(h)
                save(os.path.join(out, f"{h}.json.gz"), data)
                break
            except NotYet:
                time.sleep(1)                  # caught up with the head
            except TooOld as e:
                print(f"STOPPING: {e}"); sys.exit(2)
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
