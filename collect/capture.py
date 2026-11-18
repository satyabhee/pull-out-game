"""
capture.py: capture replay windows forever (run by the hl-capture systemd service).

Each window = the newest WINDOW_BLOCKS blocks at the moment it starts, fetched oldest-first into
  ~/hl/windows/<start>-<end>/<height>.json.gz   + window.json + anchors.json
then the next window starts right away from the then-newest height (fixed rule -> unbiased sample).

Settings (environment variables):
  WINDOW_BLOCKS=6100   ~2 min warm-up + 5 min scored at ~14.5 blocks/s (halved on archived cap, min 1800)
  WARMUP_BLOCKS=1740   warmup_end_height = start + WARMUP_BLOCKS
  MAX_ANCHORS=100      orderStatus lookups per window (int-oid cancels on BTC/ETH), max 1 per 2 s
  FRESH_MIN=90         on restart, finish a partial window only if its oldest missing block is younger
  ONCE=1               capture one window and exit (for testing)
Needs the hl-recorder service: the newest block height comes from its blocks.jsonl.
"""
import collections, glob, gzip, json, os, shutil, sys, time, urllib.request
from fetch_blocks import DEFAULT_INTERVAL, ArchivedCap, Fetcher, NotYet, TooOld, last_line, recorder_head, save

HOME = os.path.dirname(os.path.abspath(__file__))
WIN_DIR = os.path.join(HOME, "windows")
LOG = os.path.join(HOME, "capture.log")
STATE = os.path.join(HOME, "capture_state.json")
WARMUP = int(os.environ.get("WARMUP_BLOCKS", 1740))
MAX_ANCHORS = int(os.environ.get("MAX_ANCHORS", 100))
FRESH_MIN = float(os.environ.get("FRESH_MIN", 90))
# Never request a block older than this. Blocks older than ~3 h count as "archived" (100/day/IP, and going
# over locks the IP out of ALL blocks for a day). 120 min keeps ~1 h of margin; a normal window peaks ~95 min.
MAX_AGE_MIN = float(os.environ.get("MAX_AGE_MIN", 120))
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


def capture(fetcher, start, end, meta):
    """Fetch start..end oldest-first into its folder; collect anchors as we go. Returns final status."""
    d = os.path.join(WIN_DIR, f"{start}-{end}")
    os.makedirs(d, exist_ok=True)
    wpath, apath = os.path.join(d, "window.json"), os.path.join(d, "anchors.json")
    anchors = json.load(open(apath)) if os.path.exists(apath) else []
    asked = {(a["user"], a["oid"]) for a in anchors}
    every = max(1, (end - start + 1) // MAX_ANCHORS)       # spread lookups across the window
    pending, last_info = [], 0.0
    meta.setdefault("fetch_started", time.time())
    meta.update(start_height=start, end_height=end, warmup_end_height=start + WARMUP, status="fetching")
    meta.setdefault("failed_heights", [])
    fetched_now, t0, status = 0, time.time(), None
    for i, h in enumerate(range(start, end + 1)):
        p = os.path.join(d, f"{h}.json.gz")
        b = None
        if not os.path.exists(p):
            while True:
                try:
                    b, data = fetcher.get(h)
                    save(p, data)
                    fetched_now += 1
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
                    log(f"archived cap at height {h}: {e}", loud=True)
                    break
                except RuntimeError as e:
                    meta["failed_heights"].append(h)
                    log(f"gave up on block {h}: {e}")
                    break
            if status:
                break
        if h in (start, end) and os.path.exists(p):
            bt = b["blockTime"] if b is not None else (lambda x: x.get("blockDetails", x))(json.load(gzip.open(p, "rt")))["blockTime"]
            meta["first_block_time" if h == start else "last_block_time"] = bt
        if b is not None:                                     # remember BTC/ETH int-oid cancels for anchors
            for tx in b["txs"]:
                a = tx["action"]
                if a.get("type") == "cancel" and tx["error"] is None:
                    for c in a.get("cancels", []):
                        if c.get("a") in (0, 1) and isinstance(c.get("o"), int):
                            pending.append((h, tx["user"], c["o"]))
        # one orderStatus lookup per `every` blocks, at most one per 2 s, quieter users first
        if pending and len(anchors) < MAX_ANCHORS and i % every == 0 and time.time() - last_info >= 2:
            per_user = collections.Counter(u for _, u, _ in pending)
            pending.sort(key=lambda c: per_user[c[1]])
            ch, user, oid = next(((ch, u, o) for ch, u, o in pending if (u, o) not in asked), (None, None, None))
            if user:
                try:
                    r = order_status(user, oid)
                except Exception as e:
                    r = {"error": repr(e)}
                last_info = time.time()
                asked.add((user, oid))
                anchors.append({"user": user, "oid": oid, "cancel_height": ch, "asked_at": last_info, "answer": r})
            pending = []
        if i % 300 == 0:
            meta["blocks_on_disk"] = len(glob.glob(os.path.join(d, "*.json.gz")))
            save_json(wpath, meta); save_json(apath, anchors)
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


def wait_for_cap_reset(fetcher):
    """While the IP is over the daily cap, EVERY block is refused (even the newest). Probe once per 30 min."""
    while True:
        time.sleep(1800)
        newest, _ = newest_block()
        if newest is None:
            log("cap probe skipped: recorder header unreadable; next probe in 30 min")
            continue
        try:
            fetcher.get(newest - 50, tries=2)
            log("explorer accepts requests again (daily cap reset)", loud=True)
            return
        except ArchivedCap:
            log("still over the daily archived-block cap; next probe in 30 min")
        except TooOld as e:
            log(f"cap probe refused by the age guard ({e}); next probe in 30 min")
        except Exception as e:
            log(f"cap probe failed ({e}); next probe in 30 min")


def main():
    os.makedirs(WIN_DIR, exist_ok=True)
    state = load_state()
    # an explicit WINDOW_BLOCKS wins; otherwise the size remembered after an archived-cap halving, else 6100
    window_blocks = int(os.environ["WINDOW_BLOCKS"]) if "WINDOW_BLOCKS" in os.environ else state.get("window_blocks", 6100)
    keep = os.environ.get("KEEPALIVE") == "1"           # default: fresh connection per request (measured faster)
    fetcher = Fetcher(legacy=not keep, interval=float(os.environ.get("FETCH_INTERVAL", DEFAULT_INTERVAL)) if keep else 0,
                      head=lambda: recorder_head(os.path.join(HOME, "hl_data")), max_age_min=MAX_AGE_MIN)
    log(f"capture started (window_blocks={window_blocks}, once={ONCE})")
    resume = unfinished_window()
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
        status = capture(fetcher, start, end, meta)
        if status == "partial" and not glob.glob(os.path.join(WIN_DIR, f"{start}-{end}", "*.json.gz")):
            time.sleep(60)                                 # guard refused the first block: don't spin
        if status == "archived_cap":
            # cap on the very first block = the IP is already locked out for the day, not a too-long window
            if glob.glob(os.path.join(WIN_DIR, f"{start}-{end}", "*.json.gz")):
                window_blocks = max(MIN_WINDOW, window_blocks // 2)
                state.update(window_blocks=window_blocks, halved=True)
                save_json(STATE, state)
                log(f"next windows use WINDOW_BLOCKS={window_blocks}", loud=True)
            log("waiting for the daily cap to reset (probe every 30 min)", loud=True)
            if ONCE:
                break
            wait_for_cap_reset(fetcher)
        if ONCE:
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("capture stopped by hand")
