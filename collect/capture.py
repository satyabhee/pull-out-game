"""
capture.py: capture replay data forever (run by the hl-capture systemd service).

CAPTURE_MODE=chunks (default, G1 #3): snapshot-aligned chunks. The replay re-seeds its book from
every L2 snapshot, so each snapshot interval stands alone and the first seconds after a snapshot
are the most accurate. For a new recorder snapshot (BTC and ETH arrive together, ~every 12 s) at
time t, the chunk is the CHUNK_BLOCKS blocks after the last block with blockTime <= t, fetched while
they are seconds old (so they are never "archived"). At most one chunk per CHUNK_EVERY_S, spread
over the day. Each chunk is a window folder (window.json kind="chunk", warmup_end_height = start).

CAPTURE_MODE=windows (legacy): the newest WINDOW_BLOCKS blocks, oldest-first.

Both write  ~/hl/windows/<start>-<end>/<height>.json.gz  + window.json + anchors.json.
Explorer requests run one at a time in the main thread (one client per IP); saving to disk and
orderStatus anchor lookups run in a background thread, so the next request is never held up.
Every block's estimated age at request time is logged per window (max_age_s) and counted per UTC
day (capture_state.json), so the real archived-block rule can be read off the first cap hit.
After an archived-block cap: wait until 00:00 UTC (the observed reset), no probing, no shrinking.

Settings (environment variables):
  CAPTURE_MODE=chunks  chunks | windows
  CHUNK_BLOCKS=30      ~2 s of chain per chunk
  CHUNK_EVERY_S=60     at most one chunk per this many seconds
  WINDOW_BLOCKS=6100   windows mode only (~7 min of chain)
  WARMUP_BLOCKS=1740   windows mode: warmup_end_height = start + WARMUP_BLOCKS
  MAX_ANCHORS=100      orderStatus lookups per window (chunks: CHUNK_ANCHORS=2), max 1 per 2 s
  MAX_AGE_MIN=30       never request a block estimated older than this
  FRESH_MIN=20         on restart, finish a partial window only if its oldest missing block is younger
  ONCE=1               capture one window/chunk and exit (for testing)
Needs the hl-recorder service: block headers and book snapshots come from its files.
"""
import collections, datetime, glob, gzip, json, os, queue, shutil, sys, threading, time, urllib.request
from fetch_blocks import DEFAULT_INTERVAL, ArchivedCap, Fetcher, NotYet, TooOld, last_line, recorder_head, save

HOME = os.path.dirname(os.path.abspath(__file__))
WIN_DIR = os.path.join(HOME, "windows")
LOG = os.path.join(HOME, "capture.log")
STATE = os.path.join(HOME, "capture_state.json")
WARMUP = int(os.environ.get("WARMUP_BLOCKS", 1740))
MODE = os.environ.get("CAPTURE_MODE", "chunks")
CHUNK_BLOCKS = int(os.environ.get("CHUNK_BLOCKS", 30))
CHUNK_EVERY_S = float(os.environ.get("CHUNK_EVERY_S", 60))
MAX_ANCHORS = int(os.environ.get("MAX_ANCHORS", 100))
CHUNK_ANCHORS = int(os.environ.get("CHUNK_ANCHORS", 2))
FRESH_MIN = float(os.environ.get("FRESH_MIN", 20))
# Never request a block older than this. Nov 18 log: the cap hit while fetching ~84-min-old blocks after
# ~10.5k blocks that day, so "archived" starts far earlier than the ~3 h assumed before. Chunks fetch
# blocks seconds old; 30 min only matters for the legacy windows mode and restarts.
MAX_AGE_MIN = float(os.environ.get("MAX_AGE_MIN", 30))
ONCE = os.environ.get("ONCE") == "1"
BPS = 14.5                                     # Hyperliquid blocks per second (approx.)
MIN_WINDOW = 1800


def log(msg, loud=False):
    line = time.strftime("%Y-%m-%d %H:%M:%S UTC ", time.gmtime()) + ("!!! " if loud else "") + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_json(path, obj):
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(path + ".tmp", path)


def newest_block():
    """(height, recv_ms) of the newest block header the recorder wrote."""
    hour = time.strftime("%Y-%m-%d/%H", time.gmtime())
    cur = os.path.join(HOME, "hl_data", hour, "blocks.jsonl")
    cands = [cur] if os.path.exists(cur) else sorted(glob.glob(os.path.join(HOME, "hl_data", "*", "*", "blocks.jsonl*")))[-1:]
    for p in cands:
        for _ in range(3):                        # last line may be half-written
            try:
                m = json.loads(last_line(p))
                return m["msg"]["height"], m["recv_ms"]
            except Exception:
                time.sleep(0.5)
    return None, None


def disk_pct():
    u = shutil.disk_usage(HOME)
    return 100 * u.used / u.total


def order_status(user, oid):
    req = urllib.request.Request("https://api.hyperliquid.xyz/info",
                                 data=json.dumps({"type": "orderStatus", "user": user, "oid": oid}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


class Writer:
    """Background thread for everything that isn't an explorer request: gzip+save blocks, and
    orderStatus anchor lookups (info API, paced >= 2 s apart). drain() waits for all of it."""

    def __init__(self, lookup=None, saver=None, info_gap_s=2.0):
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.anchors = []
        self.errors = []
        self.lookup, self.saver, self.info_gap_s = lookup or order_status, saver or save, info_gap_s
        self._last_info = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            job = self.q.get()
            try:
                if job[0] == "save":
                    self.saver(job[1], job[2])
                else:
                    _, user, oid, ch = job
                    wait = self._last_info + self.info_gap_s - time.time()
                    if wait > 0:
                        time.sleep(wait)
                    try:
                        r = self.lookup(user, oid)
                    except Exception as e:
                        r = {"error": repr(e)}
                    self._last_info = time.time()
                    with self.lock:
                        self.anchors.append({"user": user, "oid": oid, "cancel_height": ch,
                                             "asked_at": self._last_info, "answer": r})
            except Exception as e:                       # a failed save must not vanish silently
                with self.lock:
                    self.errors.append(f"{job[0]} {job[1]}: {e!r}")
            finally:
                self.q.task_done()

    def save(self, path, data):
        self.q.put(("save", path, data))

    def ask(self, user, oid, cancel_height):
        self.q.put(("anchor", user, oid, cancel_height))

    def snapshot(self):
        with self.lock:
            return list(self.anchors)

    def drain(self):
        self.q.join()
        with self.lock:
            errs, self.errors = self.errors, []
        if errs:
            raise RuntimeError("background writer failed: " + "; ".join(errs[:3]))


def utc_day(ts=None):
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() if ts is None else ts))


def seconds_until_utc_midnight(ts=None, margin_s=120):
    now = datetime.datetime.fromtimestamp(time.time() if ts is None else ts, datetime.timezone.utc)
    nxt = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (nxt - now).total_seconds() + margin_s


def count_fetch(state, age_min):
    """Per UTC day: blocks fetched and their max age (what the explorer's daily budget sees)."""
    day = utc_day()
    if state.get("day") != day:
        state.update(day=day, fetched_today=0, max_age_today_s=0)
    state["fetched_today"] += 1
    if age_min is not None:
        state["max_age_today_s"] = max(state["max_age_today_s"], round(age_min * 60, 1))


def capture(fetcher, start, end, meta, writer=None, state=None, warmup=None, max_anchors=None):
    """Fetch start..end oldest-first into its folder; collect anchors as we go. Returns final status."""
    writer = writer or Writer()
    state = state if state is not None else {}
    warmup = WARMUP if warmup is None else warmup
    max_anchors = MAX_ANCHORS if max_anchors is None else max_anchors
    d = os.path.join(WIN_DIR, f"{start}-{end}")
    os.makedirs(d, exist_ok=True)
    wpath, apath = os.path.join(d, "window.json"), os.path.join(d, "anchors.json")
    old = json.load(open(apath)) if os.path.exists(apath) else []
    asked = {(a["user"], a["oid"]) for a in old}
    every = max(1, (end - start + 1) // max(1, max_anchors))   # spread lookups across the window
    pending, n_asked = [], len(old)
    meta.setdefault("fetch_started", time.time())
    meta.update(start_height=start, end_height=end, warmup_end_height=start + warmup, status="fetching")
    meta.setdefault("failed_heights", [])
    fetched_now, t0, status = 0, time.time(), None
    for i, h in enumerate(range(start, end + 1)):
        p = os.path.join(d, f"{h}.json.gz")
        b = None
        if not os.path.exists(p):
            while True:
                try:
                    b, data = fetcher.get(h)
                    writer.save(p, data)                   # off the request path
                    fetched_now += 1
                    age = getattr(fetcher, "last_age_min", None)
                    count_fetch(state, age)
                    if age is not None:
                        meta["max_age_s"] = max(meta.get("max_age_s", 0), round(age * 60, 1))
                    break
                except NotYet:
                    time.sleep(1)
                except TooOld as e:                        # nothing was requested (fail closed)
                    meta["note"] = f"stopped at height {h}: {e}"
                    log(f"window {start}-{end}: {meta['note']}", loud=True)
                    status = "partial"
                    break
                except ArchivedCap as e:
                    status = "archived_cap"
                    age = fetcher.age_min(h) if hasattr(fetcher, "age_min") else None
                    meta["cap_at"] = {"height": h, "age_s": None if age is None else round(age * 60, 1),
                                      "fetched_today": state.get("fetched_today"),
                                      "max_age_today_s": state.get("max_age_today_s")}
                    log(f"archived cap at height {h} (~{meta['cap_at']['age_s']} s old; fetched today "
                        f"{state.get('fetched_today')}, max age today {state.get('max_age_today_s')} s): {e}",
                        loud=True)
                    break
                except RuntimeError as e:
                    meta["failed_heights"].append(h)
                    log(f"gave up on block {h}: {e}")
                    break
            if status:
                break
        if h in (start, end):
            if b is not None:
                meta["first_block_time" if h == start else "last_block_time"] = b["blockTime"]
            elif os.path.exists(p):
                bt = (lambda x: x.get("blockDetails", x))(json.load(gzip.open(p, "rt")))["blockTime"]
                meta["first_block_time" if h == start else "last_block_time"] = bt
        if b is not None:                                     # remember BTC/ETH int-oid cancels for anchors
            for tx in b["txs"]:
                a = tx["action"]
                if a.get("type") == "cancel" and tx["error"] is None:
                    for c in a.get("cancels", []):
                        if c.get("a") in (0, 1) and isinstance(c.get("o"), int):
                            pending.append((h, tx["user"], c["o"]))
        # one orderStatus lookup per `every` blocks, quieter users first; the writer paces them >= 2 s apart
        if pending and n_asked < max_anchors and i % every == 0:
            per_user = collections.Counter(u for _, u, _ in pending)
            pending.sort(key=lambda c: per_user[c[1]])
            ch, user, oid = next(((ch, u, o) for ch, u, o in pending if (u, o) not in asked), (None, None, None))
            if user:
                asked.add((user, oid))
                n_asked += 1
                writer.ask(user, oid, ch)
            pending = []
        if i % 300 == 0 and i:
            meta["blocks_on_disk"] = len(glob.glob(os.path.join(d, "*.json.gz")))
            save_json(wpath, meta); save_json(apath, old + writer.snapshot())
    writer.drain()                                         # every block on disk, every lookup answered
    anchors = old + writer.snapshot()
    with writer.lock:
        writer.anchors = []
    secs = time.time() - t0
    meta["fetch_finished"] = time.time()
    meta["blocks_per_s"] = round(fetched_now / secs, 3) if secs > 0 else None
    meta["blocks_on_disk"] = len(glob.glob(os.path.join(d, "*.json.gz")))
    meta["anchors_found"] = sum(1 for a in anchors if a["answer"].get("status") == "order")
    meta["anchors_asked"] = len(anchors)
    meta["status"] = status or ("complete" if meta["blocks_on_disk"] == end - start + 1 else "partial")
    save_json(wpath, meta); save_json(apath, anchors)
    log(f"window {start}-{end} status={meta['status']} blocks={meta['blocks_on_disk']}/{end - start + 1} "
        f"blocks/s={meta['blocks_per_s']} anchors={meta['anchors_found']}/{meta['anchors_asked']} "
        f"failed={len(meta['failed_heights'])}")
    return meta["status"]


def unfinished_window():
    """A window left 'fetching' by a crash/reboot: resume it if its missing blocks are still fresh."""
    for wp in sorted(glob.glob(os.path.join(WIN_DIR, "*", "window.json"))):
        try:
            m = json.load(open(wp))
        except Exception:
            continue
        if m.get("status") != "fetching":
            continue
        s, e = m["start_height"], m["end_height"]
        have = {int(os.path.basename(f).split(".")[0]) for f in glob.glob(os.path.join(os.path.dirname(wp), "*.json.gz"))}
        missing = [h for h in range(s, e + 1) if h not in have]
        newest, _ = newest_block()
        if missing and newest is None:                     # age unknown: never resume blind (fail closed)
            age_min = float("inf")
        else:
            age_min = (newest - missing[0]) / BPS / 60 if missing else 0
        if missing and age_min < FRESH_MIN:
            log(f"resuming window {s}-{e}: {len(missing)} blocks missing, oldest ~{age_min:.0f} min old")
            return s, e, m
        m["status"] = "partial" if missing else "complete"
        m["note"] = f"interrupted; not resumed (oldest missing block ~{age_min:.0f} min old)"
        save_json(wp, m)
        log(f"window {s}-{e} interrupted, marked {m['status']} ({len(missing)} missing, ~{age_min:.0f} min old)")
    return None


def wait_for_cap_reset(fetcher=None):
    """Over the daily cap the newest block is still served, so probing says nothing (Nov 18: three
    false "reset" probes). Sleep until 00:00 UTC + 2 min, the observed reset."""
    secs = seconds_until_utc_midnight()
    log(f"waiting {secs / 3600:.1f} h for the 00:00 UTC cap reset", loud=True)
    time.sleep(secs)


def tail_lines(path, nbytes=65536):
    """Complete lines from the end of a growing (plain) jsonl file."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        data = f.read()
    lines = data.splitlines()
    if size > nbytes:
        lines = lines[1:]                                  # first line is likely cut
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except Exception:
            continue                                       # the last line may be half-written
    return out


def latest_snapshot_ms():
    """Time of the newest BTC L2 snapshot the recorder wrote (BTC and ETH arrive together)."""
    p = os.path.join(HOME, "hl_data", time.strftime("%Y-%m-%d/%H", time.gmtime()), "book_BTC.jsonl")
    if not os.path.exists(p):
        return None
    rows = tail_lines(p, 16384)
    times = [r["msg"]["data"]["time"] for r in rows if isinstance(r.get("msg"), dict) and "data" in r["msg"]]
    return max(times) if times else None


def plan_chunk(snap_ms, headers, n):
    """(start, end) of the chunk after a snapshot: the n blocks after the last block whose blockTime
    <= snap_ms (rule 8 applies a snapshot at the end of that block). None until a header newer than
    the snapshot is known (so 'last block <= snap_ms' is settled). headers: [(height, blockTime)]."""
    before = [h for h, t in headers if t <= snap_ms]
    if not before or not any(t > snap_ms for _, t in headers):
        return None
    last = max(before)
    return last + 1, last + n


def recent_headers():
    p = os.path.join(HOME, "hl_data", time.strftime("%Y-%m-%d/%H", time.gmtime()), "blocks.jsonl")
    if not os.path.exists(p):
        return []
    return [(r["msg"]["height"], r["msg"]["blockTime"]) for r in tail_lines(p)
            if isinstance(r.get("msg"), dict) and "height" in r["msg"]]


def run_chunks(fetcher, state, writer):
    last_snap, last_start = 0, 0.0
    while True:
        pct = disk_pct()
        if pct > 85:
            log(f"disk {pct:.0f}% full: capture paused (recorder keeps running). Sleeping 30 min.", loud=True)
            time.sleep(1800); continue
        snap = latest_snapshot_ms()
        if snap is None or snap <= last_snap or time.time() - last_start < CHUNK_EVERY_S:
            time.sleep(0.5); continue
        span = plan_chunk(snap, recent_headers(), CHUNK_BLOCKS)
        if span is None:
            if time.time() * 1000 - snap > 30_000:
                log(f"no block headers around snapshot {snap}; skipping it (recorder lagging?)")
                last_snap = snap
            time.sleep(0.5); continue
        last_snap, last_start = snap, time.time()
        start, end = span
        meta = {"kind": "chunk", "snapshot_time": snap}
        status = capture(fetcher, start, end, meta, writer=writer, state=state, warmup=0, max_anchors=CHUNK_ANCHORS)
        save_json(STATE, state)
        if status == "archived_cap":
            if ONCE:
                return
            wait_for_cap_reset()
        if ONCE:
            return


def main():
    os.makedirs(WIN_DIR, exist_ok=True)
    state = load_state()
    # an explicit WINDOW_BLOCKS wins; otherwise the size remembered after an archived-cap halving, else 6100
    window_blocks = int(os.environ["WINDOW_BLOCKS"]) if "WINDOW_BLOCKS" in os.environ else state.get("window_blocks", 6100)
    keep = os.environ.get("KEEPALIVE") == "1"           # default: fresh connection per request (measured faster)
    fetcher = Fetcher(legacy=not keep, interval=float(os.environ.get("FETCH_INTERVAL", DEFAULT_INTERVAL)) if keep else 0,
                      head=lambda: recorder_head(os.path.join(HOME, "hl_data")), max_age_min=MAX_AGE_MIN)
    writer = Writer()
    log(f"capture started (mode={MODE}, chunk_blocks={CHUNK_BLOCKS}, chunk_every_s={CHUNK_EVERY_S}, "
        f"window_blocks={window_blocks}, max_age_min={MAX_AGE_MIN}, once={ONCE})")
    resume = unfinished_window()
    if resume:
        start, end, meta = resume
        capture(fetcher, start, end, meta, writer=writer, state=state,
                warmup=0 if meta.get("kind") == "chunk" else None,
                max_anchors=CHUNK_ANCHORS if meta.get("kind") == "chunk" else None)
        save_json(STATE, state)
        resume = None
    if MODE == "chunks":
        run_chunks(fetcher, state, writer)
        return
    while True:
        pct = disk_pct()
        if pct > 85:
            log(f"disk {pct:.0f}% full: capture paused (recorder keeps running). Sleeping 30 min.", loud=True)
            time.sleep(1800); continue
        if resume:
            start, end, meta = resume; resume = None
        else:
            newest, recv_ms = newest_block()
            if newest is None or time.time() * 1000 - recv_ms > 60_000:
                age = "never" if newest is None else f"{(time.time() * 1000 - recv_ms) / 1000:.0f}s ago"
                log(f"RECORDER LOOKS DOWN: newest block header {age}. Not capturing; retry in 60 s.", loud=True)
                time.sleep(60); continue
            start, end, meta = newest - window_blocks + 1, newest, {}
            log(f"window {start}-{end} starting ({window_blocks} blocks)")
        status = capture(fetcher, start, end, meta, writer=writer, state=state)
        save_json(STATE, state)
        if status == "partial" and not glob.glob(os.path.join(WIN_DIR, f"{start}-{end}", "*.json.gz")):
            time.sleep(60)                                 # guard refused the first block: don't spin
        if status == "archived_cap":
            # cap on the very first block = the IP is already locked out for the day, not a too-long window
            if glob.glob(os.path.join(WIN_DIR, f"{start}-{end}", "*.json.gz")):
                window_blocks = max(MIN_WINDOW, window_blocks // 2)
                state.update(window_blocks=window_blocks, halved=True)
                save_json(STATE, state)
                log(f"next windows use WINDOW_BLOCKS={window_blocks}", loud=True)
            if ONCE:
                break
            wait_for_cap_reset()
        if ONCE:
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("capture stopped by hand")
