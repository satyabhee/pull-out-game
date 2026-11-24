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
  LEAD_IN_BLOCKS=0     chunks: also fetch this many blocks before the chunk (warm-up; ~145 = 10 s)
  WINDOW_BLOCKS=6100   windows mode only (~7 min of chain)
  WARMUP_BLOCKS=1740   windows mode: warmup_end_height = start + WARMUP_BLOCKS
  MAX_ANCHORS=100      orderStatus lookups per window (chunks: CHUNK_ANCHORS=2), max 1 per 2 s
  ANCHOR_MODE=cloid    which lookups: cloid = placements with a cloid, spread over the fetched blocks (the answer
                       is an exact oid at a known position, so the estimator can use it); targets = the legacy
                       int-oid cancel targets (mostly placed before the chunk, so never locatable)
  CHUNK_FILL_USERS=6   chunks: userFillsByTime for the users in the most BTC/ETH trades of the fetched blocks
                       (fills.json; every fill carries the order's oid and the tx hash -> exact anchors)
  CHUNK_OPEN_USERS=5   chunks: frontendOpenOrders of the top recent makers (open_orders.json), asked all at once
                       OPEN_LEAD_MS before the predicted next snapshot, which then becomes the chunk's snapshot, so
                       the answers show what rested just before scoring starts
  OPEN_LEAD_MS=1500    (0 = old behaviour: asked after the chunk is planned, through the writer queue)
  CHUNK_HIST_USERS=1   chunks: historicalOrders of the users with the most int-oid cancels (hist_orders.json), asked
                       after the chunk's blocks are fetched (minutes late: a busy user's last 2000 orders may no
                       longer reach back to the chunk)
  HIST_IOC_USERS=0     chunks: at the chunk's end, historicalOrders (~120 weight each) of the previous chunk's
                       top IOC senders: exact final statuses (perpMarginRejected ...) for replay --taker-reject hist
  MARGIN_USERS=0       chunks: when the chunk is planned, clearinghouseState + spotClearinghouseState (weight 2
                       each) of this many users: the previous chunk's IOC users, then recent takers
  ALO_MARGIN_USERS=0   chunks: clearinghouseState only (weight 2) of this many more users, after the IOC users: half
                       the previous chunk's top post-only (ALO) makers at the touch (within TOUCH_BP of the last
                       trade), half the SUSPECTS_FILE users (makers the replay filled with no real trade,
                       analysis/suspect_makers.py); a short half is filled from the other (replay --alo-reject margin)
  OPEN_MARGIN_USERS=0  chunks: frontendOpenOrders + a fresh clearinghouseState (weight 22 per user) of this many
                       margin-snapshot users, OPEN_MARGIN_DELAY_S after the plan-time snapshot (out of its burst
                       minute): users with resting-order margin (accountValue - withdrawable - max(totalMarginUsed,
                       totalNtlPos / 10) > 0), least free margin for orders first. margin_open.json (not read by the
                       replay); meta open_margin_users
  OPEN_MARGIN_DELAY_S=62
  SUSPECT_LOOKUPS=0    chunks: orderStatus (weight 2) by cloid of this many post-only placements by SUSPECTS_FILE
                       users in the scored blocks, asked as soon as the blocks are fetched, one user at a time in
                       suspect order (round robin): chain truth for phantom/queue cases. suspect_status.json (not
                       anchors.json, so the replay never reads it); meta suspect_lookups / suspect_candidates
  TRUTH_IOC=0          chunks: orderStatus (weight 2) by cloid of up to this many BTC/ETH IOC placements in the
                       scored blocks, asked as each block is fetched (statuses of rejected IOCs are gone within
                       minutes: 15/15 known at 2.5 min, 6/8 at 7 min, 0-1/8 after 4 h), on a low-priority thread
                       capped at TRUTH_WEIGHT_PER_MIN=300 (inside INFO_WEIGHT_PER_MIN); items older than
                       TRUTH_STALE_S=360 s are skipped. truth_status.json (oracle truth, BLITZ Addendum 3: the replay
                       never reads it; written by the thread itself when the chunk's items are done)
  TOUCH_BP=1           post-only placement counts as "at the touch" within this many bp of the coin's last trade
  SUSPECTS_FILE=suspects.json
  HIST_END_USERS=6     chunks: historicalOrders (~120 weight each, reserved up front so they queue under the
                       budget instead of bursting) asked all at once as soon as the chunk's last block exists (its
                       history then always covers the chunk), for the ranker's users (TRACKERS_FILE: contested
                       makers first, then its cancel ranking) other than those the recorder streams live
  TRACKERS_FILE=trackers.json  the ranker's output (analysis/rank_trackers.py)
  INFO_WEIGHT_PER_MIN=600  weight budget for all info lookups (IP limit 1200/min; collect/info.py)
  MAX_AGE_MIN=30       never request a block estimated older than this
  FRESH_MIN=20         on restart, finish a partial window only if its oldest missing block is younger
  ONCE=1               capture one window/chunk and exit (for testing)
Needs the hl-recorder service: block headers and book snapshots come from its files.
"""
import collections, datetime, glob, gzip, json, os, queue, random, shutil, sys, threading, time, urllib.request
from fetch_blocks import DEFAULT_INTERVAL, ArchivedCap, Fetcher, NotYet, TooOld, last_line, recorder_head, save
import info

HOME = os.path.dirname(os.path.abspath(__file__))
WIN_DIR = os.path.join(HOME, "windows")
LOG = os.path.join(HOME, "capture.log")
STATE = os.path.join(HOME, "capture_state.json")
WARMUP = int(os.environ.get("WARMUP_BLOCKS", 1740))
MODE = os.environ.get("CAPTURE_MODE", "chunks")
CHUNK_BLOCKS = int(os.environ.get("CHUNK_BLOCKS", 30))
CHUNK_EVERY_S = float(os.environ.get("CHUNK_EVERY_S", 60))
LEAD_IN_BLOCKS = int(os.environ.get("LEAD_IN_BLOCKS", 0))   # chunks: blocks fetched before the chunk, replayed as warm-up
REQUEST_LOG = os.path.join(HOME, "requests.jsonl")
MAX_ANCHORS = int(os.environ.get("MAX_ANCHORS", 100))
CHUNK_ANCHORS = int(os.environ.get("CHUNK_ANCHORS", 2))
ANCHOR_MODE = os.environ.get("ANCHOR_MODE", "cloid")
CHUNK_FILL_USERS = int(os.environ.get("CHUNK_FILL_USERS", 6))
CHUNK_OPEN_USERS = int(os.environ.get("CHUNK_OPEN_USERS", 5))
OPEN_LEAD_MS = int(os.environ.get("OPEN_LEAD_MS", 1500))
CHUNK_HIST_USERS = int(os.environ.get("CHUNK_HIST_USERS", 1))
HIST_END_USERS = int(os.environ.get("HIST_END_USERS", 6))
TRACKERS_FILE = os.environ.get("TRACKERS_FILE", "trackers.json")
HIST_END_RANDOM = os.environ.get("HIST_END_RANDOM") == "1"
HIST_IOC_USERS = int(os.environ.get("HIST_IOC_USERS", 0))   # chunks: historicalOrders at chunk end for the top IOC senders
CAPTURE_VERSION = "2026-11-24-truth-live"      # written into every chunk's window.json (capture_version)
CAPTURE_SUPERSET_OF = "2026-11-23-open-margin" # same files as that version plus new ones the replay does not read
ALO_MARGIN_USERS = int(os.environ.get("ALO_MARGIN_USERS", 0))
TOUCH_BP = float(os.environ.get("TOUCH_BP", 1))
SUSPECTS_FILE = os.environ.get("SUSPECTS_FILE", "suspects.json")
OPEN_MARGIN_USERS = int(os.environ.get("OPEN_MARGIN_USERS", 0))
OPEN_MARGIN_DELAY_S = float(os.environ.get("OPEN_MARGIN_DELAY_S", 62))
SUSPECT_LOOKUPS = int(os.environ.get("SUSPECT_LOOKUPS", 0))
TRUTH_IOC = int(os.environ.get("TRUTH_IOC", 0))                 # chunks: orderStatus of this many scored-block IOCs
TRUTH_WEIGHT_PER_MIN = int(os.environ.get("TRUTH_WEIGHT_PER_MIN", 300))
TRUTH_STALE_S = float(os.environ.get("TRUTH_STALE_S", 360))
MARGIN_USERS = int(os.environ.get("MARGIN_USERS", 0))   # chunks: clearinghouseState + spot of this many IOC users at plan time
TARGET_LOOKUPS = int(os.environ.get("TARGET_LOOKUPS", 0))    # orderStatus of int-oid cancel targets in the SCORED blocks   # experiment: per-chunk coin flip (seeded by the snapshot)
INFO_FILES = {"userFillsByTime": "fills.json", "frontendOpenOrders": "open_orders.json",
              "historicalOrders": "hist_orders.json"}
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


def ioc_items(block, height):
    """[(height, tx index, item index, user, cloid, block time ms)] of the BTC/ETH IOC placements with a cloid."""
    out = []
    for t, tx in enumerate(block.get("txs", [])):
        if tx.get("error") is not None:
            continue
        a = tx.get("action") or {}
        if a.get("type") != "order":
            continue
        for i, o in enumerate(a.get("orders", [])):
            if isinstance(o, dict) and o.get("a") in (0, 1) and o.get("c") and \
                    ((o.get("t") or {}).get("limit") or {}).get("tif") == "Ioc":
                out.append((height, t, i, tx.get("user", "").lower(), o["c"], block.get("blockTime")))
    return out


_TRUTH_BUDGET = None


def truth_budget():
    global _TRUTH_BUDGET
    if _TRUTH_BUDGET is None:
        _TRUTH_BUDGET = info.Budget(per_min=TRUTH_WEIGHT_PER_MIN)
    return _TRUTH_BUDGET


class TruthAsker:
    """One chunk's live chain-truth lookups (orderStatus by cloid), on its own daemon thread so the fetch loop and
    the writer queue never wait for it. Each call books weight twice: in the low-priority truth budget (its own
    cap) and in the capture's global budget (so the IP total stays inside INFO_WEIGHT_PER_MIN). close() makes the
    thread write `path` once its queue is done; join() waits (tests)."""

    def __init__(self, path, cap, budget, lookup=None, truth_bud=None, clock=time.time, stale_s=None):
        self.path, self.cap, self.budget = path, cap, budget
        self.lookup = lookup or order_status
        self.truth_bud = truth_bud or truth_budget()
        self.clock = clock
        self.stale_s = TRUTH_STALE_S if stale_s is None else stale_s
        self.q = queue.Queue()
        self.rows, self.queued = [], 0
        self.counts = collections.Counter()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def feed(self, items):
        for it in items:
            if self.queued >= self.cap:
                self.counts["over_cap"] += 1
                continue
            self.queued += 1
            self.q.put(it)

    def close(self):
        self.q.put(None)

    def join(self, timeout=None):
        self.thread.join(timeout)

    def _run(self):
        while True:
            it = self.q.get()
            if it is None:
                break
            h, t, i, u, c, bt = it
            if bt is not None and self.clock() * 1000 - bt > self.stale_s * 1000:
                self.counts["skipped_stale"] += 1
                continue
            self.truth_bud.take(2)
            self.budget.take(info.weight_of("orderStatus"))
            asked = self.clock()
            try:
                r = self.lookup(u, c)
            except Exception as e:
                r = {"error": repr(e)}
            st = r.get("order", {}).get("status") if isinstance(r, dict) and r.get("status") == "order" else \
                (r.get("status") if isinstance(r, dict) else None) or "error"
            self.counts[f"status:{st}"] += 1
            self.rows.append({"placed_height": h, "tx": t, "item": i, "user": u, "cloid": c, "block_time": bt,
                              "asked_at": asked, "answered_at": self.clock(), "answer": r})
        try:
            save_json(self.path, {"kind": "truth_status", "queued": self.queued, "counts": dict(self.counts),
                                  "rows": self.rows})
        except Exception:
            pass


class Writer:
    """Background thread for everything that isn't an explorer request: gzip+save blocks, orderStatus anchor
    lookups (paced >= 2 s apart) and the other info lookups (userFillsByTime, frontendOpenOrders,
    historicalOrders; paced by the weight budget). Every info call books its weight in `budget`.
    drain() waits for all of it."""

    def __init__(self, lookup=None, saver=None, info_gap_s=2.0, calls=None, budget=None):
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.anchors = []
        self.info = collections.defaultdict(list)      # kind -> [{"user", "kind", "asked_at", ..., "answer"}]
        self.errors = []
        self.lookup, self.saver, self.info_gap_s = lookup or order_status, saver or save, info_gap_s
        self.calls = calls or info.CALLS
        self.budget = budget or info.Budget()
        self._last_info = 0.0
        self.pending = []                              # background info threads drain() must wait for
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            job = self.q.get()
            try:
                if job[0] == "save":
                    self.saver(job[1], job[2])
                elif job[0] == "info":
                    _, kind, user, args, extra = job
                    self.budget.take(info.weight_of(kind))
                    t = time.time()
                    try:
                        r = self.calls[kind](user, *args)
                    except Exception as e:
                        r = {"error": repr(e)}
                    self.budget.add(info.weight_of(kind, r) - info.weight_of(kind))
                    with self.lock:
                        self.info[kind].append({**extra, "user": user, "kind": kind, "args": list(args),
                                                "asked_at": t, "answered_at": time.time(), "answer": r})
                elif job[0] == "status":                 # orderStatus kept as an info record, not an anchor
                    _, user, oid, extra = job
                    self.budget.take(info.weight_of("orderStatus"))
                    t = time.time()
                    try:
                        r = self.lookup(user, oid)
                    except Exception as e:
                        r = {"error": repr(e)}
                    with self.lock:
                        self.info["suspect_status"].append({**extra, "user": user, "oid": oid,
                                                            "kind": "suspect_status", "asked_at": t,
                                                            "answered_at": time.time(), "answer": r})
                else:
                    _, user, oid, ch, extra, paced = job
                    wait = self._last_info + self.info_gap_s - time.time()
                    if paced and wait > 0:
                        time.sleep(wait)
                    self.budget.take(info.weight_of("orderStatus"))
                    try:
                        r = self.lookup(user, oid)
                    except Exception as e:
                        r = {"error": repr(e)}
                    self._last_info = time.time()
                    with self.lock:
                        self.anchors.append({**extra, "user": user, "oid": oid, "cancel_height": ch,
                                             "asked_at": self._last_info, "answer": r})
            except Exception as e:                       # a failed save must not vanish silently
                with self.lock:
                    self.errors.append(f"{job[0]} {job[1]}: {e!r}")
            finally:
                self.q.task_done()

    def save(self, path, data):
        self.q.put(("save", path, data))

    def ask(self, user, oid, cancel_height, extra=None, paced=True):
        """extra: more fields stored on the anchor record (never a "height" key: data/oids.py reads that
        as its own anchor format). paced=False: only the weight budget paces it (no info_gap_s)."""
        self.q.put(("anchor", user, oid, cancel_height, extra or {}, paced))

    def ask_status(self, user, oid, extra=None):
        """orderStatus by (user, oid or cloid), budget-paced only; stored in info["suspect_status"]."""
        self.q.put(("status", user, oid, extra or {}))

    def ask_info(self, kind, user, args=(), extra=None):
        """One info lookup (collect/info.py CALLS[kind](user, *args)); the answer is kept raw in info[kind]."""
        self.q.put(("info", kind, user, tuple(args), extra or {}))

    def ask_info_now(self, kind, users, extra=None):
        """Ask `kind` for every user at once (one thread each, weight booked as usual) and wait for the answers;
        stored like ask_info's. Returns the latest answer time (s)."""
        def one(u):
            reserved = info.expected_weight(kind)
            self.budget.take(reserved)
            t = time.time()
            try:
                r = self.calls[kind](u)
            except Exception as e:
                r = {"error": repr(e)}
            self.budget.add(info.weight_of(kind, r) - reserved)
            with self.lock:
                self.info[kind].append({**(extra or {}), "user": u, "kind": kind, "args": [], "asked_at": t,
                                        "answered_at": time.time(), "answer": r})
        threads = [threading.Thread(target=one, args=(u,), daemon=True) for u in users]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        with self.lock:
            return max([r["answered_at"] for r in self.info[kind]], default=time.time())

    def drop_info(self, kind):
        with self.lock:
            self.info.pop(kind, None)

    def snapshot(self):
        with self.lock:
            return list(self.anchors)

    def take_info(self):
        """Every info answer so far, by kind; clears them."""
        with self.lock:
            out, self.info = dict(self.info), collections.defaultdict(list)
        return out

    def drain(self):
        with self.lock:
            pending, self.pending = self.pending, []
        for th in pending:
            th.join()
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


def placement_items(tx):
    """BTC/ETH limit placements with a cloid in one successful tx: [(item_index, item)]. Item numbering matches
    data/parse.py and data/oids.py (order list / modify / batchModify list); triggers are skipped."""
    if tx.get("error") is not None:
        return []
    a = tx.get("action") or {}
    kind = a.get("type")
    items = (a.get("orders", []) if kind == "order" else [a.get("order", {})] if kind == "modify"
             else [m.get("order", {}) for m in a.get("modifies", [])] if kind == "batchModify" else [])
    return [(i, o) for i, o in enumerate(items)
            if isinstance(o, dict) and o.get("a") in (0, 1) and o.get("c") and "limit" in (o.get("t") or {})]


def int_oid_targets(block):
    """(user, real oid) of every successful BTC/ETH int-oid cancel and int-oid modify target in a block."""
    out = []
    for tx in block.get("txs", []):
        if tx.get("error"):
            continue
        a, u = tx.get("action", {}), tx.get("user", "")
        k = a.get("type")
        if k == "cancel":
            out += [(u, c["o"]) for c in a.get("cancels", []) if c.get("a") in (0, 1) and isinstance(c.get("o"), int)]
        elif k in ("modify", "batchModify"):
            for m in ([a] if k == "modify" else a.get("modifies", [])):
                if isinstance(m.get("oid"), int) and (m.get("order") or {}).get("a") in (0, 1):
                    out.append((u, m["oid"]))
    return out


class AnchorPicker:
    """Which orderStatus lookups to make while a window/chunk is fetched (one pick per segment of blocks).

    mode "cloid": ask by (user, cloid) for a PLACEMENT inside the fetched blocks. The answer carries the order's real
    oid and timestamp, so data/oids.py can locate it in the blocks and use it as an anchor (an exact oid at a known
    position). Each segment's pick is the placement in the middle of that segment's candidates (deterministic), so
    anchors are spread evenly; a cloid seen twice in the fetched blocks is skipped (it would not locate uniquely).
    mode "targets" (legacy): the int-oid cancel targets, quieter users first; mostly placed before the window."""

    def __init__(self, mode=None, asked=()):
        self.mode = ANCHOR_MODE if mode is None else mode
        if self.mode not in ("cloid", "targets"):
            raise ValueError(f"ANCHOR_MODE must be cloid or targets, not {self.mode!r}")
        self.asked, self.pending, self.seen = set(asked), [], collections.Counter()

    def feed(self, height, block):
        for t, tx in enumerate(block["txs"]):
            if self.mode == "cloid":
                for i, o in placement_items(tx):
                    self.seen[(tx["user"].lower(), o["c"])] += 1
                    self.pending.append((height, t, i, tx["user"], o["c"]))
            else:
                a = tx["action"]
                if a.get("type") == "cancel" and tx["error"] is None:
                    for c in a.get("cancels", []):
                        if c.get("a") in (0, 1) and isinstance(c.get("o"), int):
                            self.pending.append((height, tx["user"], c["o"]))

    def take(self):
        """The lookup for the segment that just ended: (user, oid_or_cloid, extra_fields_for_the_record), or None.
        Clears the segment's candidates either way."""
        pend, self.pending = self.pending, []
        if self.mode == "targets":
            per_user = collections.Counter(u for _, u, _ in pend)
            pend.sort(key=lambda c: per_user[c[1]])
            ch, user, oid = next(((ch, u, o) for ch, u, o in pend if (u, o) not in self.asked), (None, None, None))
            if not user:
                return None
            self.asked.add((user, oid))
            return user, oid, {"cancel_height": ch}
        ok = [c for c in pend if (c[3], c[4]) not in self.asked and self.seen[(c[3].lower(), c[4])] == 1]
        if not ok:
            return None
        h, t, i, user, cloid = ok[len(ok) // 2]
        self.asked.add((user, cloid))
        return user, cloid, {"kind": "cloid", "placed_height": h, "tx": t, "item": i}


def trade_rows(rows, t0_ms=None, t1_ms=None):
    """Recorder trade messages -> [{coin, side, px, sz, time, hash, tid, users}] within [t0, t1]."""
    out = []
    for r in rows:
        m = r.get("msg") if isinstance(r, dict) else None
        for t in (m or {}).get("data") or []:
            if isinstance(t, dict) and "users" in t and (t0_ms is None or t["time"] >= t0_ms) \
                    and (t1_ms is None or t["time"] <= t1_ms):
                out.append(t)
    return out


def recorder_trades(t0_ms, t1_ms, coins=("BTC", "ETH"), nbytes=8 << 20):
    """BTC/ETH trades the recorder wrote with time in [t0, t1] (current hour: file tail; earlier hour: whole .gz)."""
    rows = []
    for coin in coins:
        hours = sorted({time.strftime("%Y-%m-%d/%H", time.gmtime(t / 1000)) for t in (t0_ms, t1_ms)})
        for hour in hours:
            plain = os.path.join(HOME, "hl_data", hour, f"trades_{coin}.jsonl")
            if os.path.exists(plain):
                rows += tail_lines(plain, nbytes)
            elif os.path.exists(plain + ".gz"):
                with gzip.open(plain + ".gz", "rt") as f:
                    for line in f:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
    return trade_rows(rows, t0_ms, t1_ms)


def busiest_users(trades, n, exclude=()):
    """Users in the most trades (either side), most first; ties by address (deterministic)."""
    c = collections.Counter(u.lower() for t in trades for u in t["users"])
    return [u for u, _ in sorted(c.items(), key=lambda x: (-x[1], x[0])) if u not in exclude][:n]


def top_makers(trades, n):
    """Users with the most maker notional (the resting side: seller when the taker bought)."""
    c = collections.Counter()
    for t in trades:
        maker = t["users"][1] if t["side"] == "B" else t["users"][0]
        c[maker.lower()] += float(t["px"]) * float(t["sz"])
    return [u for u, _ in sorted(c.items(), key=lambda x: (-x[1], x[0]))][:n]


def ioc_users(block):
    """user -> number of BTC/ETH IOC order items in one block's txs (rejected ones too: the margin snapshot
    is for exactly the users whose IOCs the chain rejects)."""
    c = collections.Counter()
    for tx in block.get("txs", []):
        a = tx.get("action") or {}
        kind = a.get("type")
        items = (a.get("orders", []) if kind == "order" else [a.get("order", {})] if kind == "modify"
                 else [m.get("order", {}) for m in a.get("modifies", [])] if kind == "batchModify" else [])
        n = sum(1 for o in items if isinstance(o, dict) and o.get("a") in (0, 1)
                and ((o.get("t") or {}).get("limit") or {}).get("tif") == "Ioc")
        if n:
            c[tx.get("user", "").lower()] += n
    return c


def alo_placements(block):
    """[(user, asset, is_buy, px, block time ms)] of the BTC/ETH post-only (ALO) order items in one block's txs
    (rejected ones too: the chain's rejects are what the margin snapshot is for)."""
    out, t = [], block.get("blockTime", 0)
    for tx in block.get("txs", []):
        a = tx.get("action") or {}
        kind = a.get("type")
        items = (a.get("orders", []) if kind == "order" else [a.get("order", {})] if kind == "modify"
                 else [m.get("order", {}) for m in a.get("modifies", [])] if kind == "batchModify" else [])
        for o in items:
            if isinstance(o, dict) and o.get("a") in (0, 1) and ((o.get("t") or {}).get("limit") or {}).get("tif") == "Alo":
                try:
                    out.append((tx.get("user", "").lower(), o["a"], bool(o.get("b")), float(o["p"]), t))
                except (KeyError, TypeError, ValueError):
                    pass
    return out


def alo_touch_users(placements, trades, bp=None, n=100):
    """user -> number of post-only placements within `bp` bp of the coin's last recorder trade before them (most
    first, top n). Placements before the first trade of their coin are not counted."""
    import bisect
    bp = TOUCH_BP if bp is None else bp
    coin = {0: "BTC", 1: "ETH"}
    series = {}
    for t in sorted(trades, key=lambda t: t["time"]):
        ts, px = series.setdefault(t["coin"], ([], []))
        ts.append(t["time"]); px.append(float(t["px"]))
    c = collections.Counter()
    for u, a, _, px, t in placements:
        ts, pxs = series.get(coin[a], ([], []))
        i = bisect.bisect_right(ts, t) - 1
        if i >= 0 and abs(px - pxs[i]) <= pxs[i] * bp / 1e4:
            c[u] += 1
    return dict(sorted(c.items(), key=lambda x: (-x[1], x[0]))[:n])


def load_suspects(path=None):
    """Ranked suspect makers (SUSPECTS_FILE: {"time", "users": [...]}, analysis/suspect_makers.py); [] if missing."""
    try:
        return [u.lower() for u in json.load(open(path or os.path.join(HOME, SUSPECTS_FILE))).get("users", [])]
    except (OSError, ValueError, AttributeError):
        return []


def alo_margin_users(prev_touch, suspects, n, exclude=()):
    """{"touch": [...], "suspect": [...]}: up to n users for the post-only margin snapshot, about half from the
    previous chunk's top touch makers and half from the suspects, none in `exclude` (the IOC margin users, who
    keep priority) and no repeats; a half with too few users is filled from the other list."""
    skip = {u.lower() for u in exclude}
    touch = [u for u, _ in sorted((prev_touch or {}).items(), key=lambda x: (-x[1], x[0])) if u not in skip]
    sus = [u for u in suspects if u not in skip]
    out = {"touch": [], "suspect": []}
    taken = set()

    def take(name, src, k):
        for u in src:
            if len(out[name]) >= k:
                return
            if u not in taken:
                out[name].append(u); taken.add(u)
    take("touch", touch, (n + 1) // 2)
    take("suspect", sus, n - len(out["touch"]))
    take("touch", touch, n - len(out["suspect"]))
    return out


def margin_users(prev_iocs, trades, n):
    """Users for the plan-time margin snapshot: the previous chunk's IOC users (most IOCs first), then the takers
    of the recorder's recent trades (most notional first)."""
    out = [u for u, _ in sorted((prev_iocs or {}).items(), key=lambda x: (-x[1], x[0]))]
    c = collections.Counter()
    for t in trades:
        taker = t["users"][0] if t["side"] == "B" else t["users"][1]
        c[taker.lower()] += float(t["px"]) * float(t["sz"])
    out += [u for u, _ in sorted(c.items(), key=lambda x: (-x[1], x[0])) if u not in out]
    return out[:n]


def order_margin(answer):
    """(free margin for new orders, resting-order margin) from one clearinghouseState answer, as Decimals, or None.
    Hyperliquid's withdrawable = accountValue - max(totalMarginUsed, totalNtlPos / 10) - resting-order margin
    (checked live on 2026-11-23: matches the open orders' position-increasing side / leverage for 6 of 7 users), so
    order margin = accountValue - withdrawable - max(...) and free for orders = accountValue - totalMarginUsed -
    order margin."""
    from decimal import Decimal, InvalidOperation
    try:
        ms = answer["marginSummary"]
        av, mu = Decimal(ms["accountValue"]), Decimal(ms["totalMarginUsed"])
        ntl, wd = Decimal(ms["totalNtlPos"]), Decimal(answer["withdrawable"])
    except (KeyError, TypeError, InvalidOperation):
        return None
    om = av - wd - max(mu, ntl / 10)
    return av - mu - om, om


def open_margin_users(records, n):
    """Users for the open-orders snapshot: those with resting-order margin, least free margin for orders first.
    records: this chunk's clearinghouseState info records. Returns [(user, free, order_margin)] as strings."""
    rows = []
    for r in records:
        fm = order_margin(r.get("answer"))
        if fm and fm[1] > 0:
            rows.append((fm[0], r["user"], fm[1]))
    rows.sort()
    seen, out = set(), []
    for free, u, om in rows:
        if u not in seen:
            seen.add(u); out.append((u, str(free), str(om)))
    return out[:n]


def ask_open_margin_later(writer, users, snap_ms, at_s, now=time.time, sleep=time.sleep):
    """At time at_s, clearinghouseState then frontendOpenOrders for every user (one thread each, weight 22 booked
    first), stored as info["margin_open"]. Runs in the background; writer.drain() waits for it."""
    def one(u, extra):
        writer.budget.take(info.weight_of("clearinghouseState") + info.weight_of("frontendOpenOrders"))
        t = now()
        try:
            r = {"clearinghouseState": writer.calls["clearinghouseState"](u),
                 "frontendOpenOrders": writer.calls["frontendOpenOrders"](u)}
        except Exception as e:
            r = {"error": repr(e)}
        with writer.lock:
            writer.info["margin_open"].append({**extra, "user": u, "kind": "margin_open", "args": [],
                                               "asked_at": t, "answered_at": now(), "answer": r})

    def run():
        wait = at_s - now()
        if wait > 0:
            sleep(wait)
        ths = [threading.Thread(target=one, args=(u, {"snapshot_time": snap_ms, "plan_free": f,
                                                       "plan_order_margin": om}), daemon=True)
               for u, f, om in users]
        for th in ths:
            th.start()
        for th in ths:
            th.join()

    th = threading.Thread(target=run, daemon=True)
    with writer.lock:
        writer.pending.append(th)
    th.start()
    return th


def suspect_candidates(block, height, suspects):
    """[(height, tx, item, user, cloid)] of the post-only placements with a cloid by `suspects` users in one block."""
    out = []
    for t, tx in enumerate(block.get("txs", [])):
        u = tx.get("user", "").lower()
        if u not in suspects:
            continue
        for i, o in placement_items(tx):
            if ((o.get("t") or {}).get("limit") or {}).get("tif") == "Alo":
                out.append((height, t, i, tx["user"], o["c"]))
    return out


def pick_suspect_lookups(cands, rank, n, skip=(), seen=None):
    """Up to n candidates, one user at a time in suspect rank order (round robin), each user's middle remaining
    candidate first; skips (user, cloid) pairs in `skip` and cloids seen more than once (seen: Counter)."""
    by = collections.defaultdict(list)
    for c in cands:
        key = (c[3], c[4])
        if key in skip or (seen is not None and seen[(c[3].lower(), c[4])] > 1):
            continue
        by[c[3].lower()].append(c)
    order = sorted(by, key=lambda u: (rank.get(u, len(rank)), u))
    out = []
    while len(out) < n and any(by[u] for u in order):
        for u in order:
            if by[u] and len(out) < n:
                out.append(by[u].pop(len(by[u]) // 2))
    return out


def ask_margin(writer, snap_ms, users, alo_users=()):
    """clearinghouseState and spotClearinghouseState of `users`, asked at once when the chunk is planned (the
    scored blocks are a few seconds after the snapshot), plus clearinghouseState only of `alo_users` (spot added
    nothing for the IOC gate; keeps the burst minute under the budget). Stored with the chunk."""
    if not users and not alo_users:
        return None
    extra = {"snapshot_time": snap_ms}
    writer.ask_info_now("clearinghouseState", list(users) + list(alo_users), extra)
    return writer.ask_info_now("spotClearinghouseState", users, extra) if users else time.time()


def int_cancel_users(block):
    """user -> number of BTC/ETH int-oid cancel/modify-target items in one block's successful txs."""
    c = collections.Counter()
    for tx in block["txs"]:
        if tx.get("error") is not None:
            continue
        a = tx.get("action") or {}
        kind = a.get("type")
        its = (a.get("cancels", []) if kind == "cancel" else [{"a": (a.get("order") or {}).get("a"), "o": a.get("oid")}]
               if kind == "modify" else [{"a": (m.get("order") or {}).get("a"), "o": m.get("oid")}
                                         for m in a.get("modifies", [])] if kind == "batchModify" else [])
        n = sum(1 for it in its if it.get("a") in (0, 1) and isinstance(it.get("o"), int) and not isinstance(it.get("o"), bool))
        if n:
            c[tx["user"].lower()] += n
    return c


def ask_extras(writer, extras, meta, cancels):
    """After a chunk's blocks are fetched: userFillsByTime for the users in the most trades of the fetched time
    range, historicalOrders for the users with the most int-oid cancels."""
    t0, t1 = meta.get("first_block_time"), meta.get("last_block_time")
    if t0 is None or t1 is None:
        return
    nf = extras.get("fill_users", 0)
    if nf:
        trades = extras["trades"](t0, t1 + 1000) if extras.get("trades") else []
        for u in busiest_users(trades, nf):
            writer.ask_info("userFillsByTime", u, (t0 - 1000, t1 + 5000), {"t0": t0, "t1": t1})
    skip = set(extras["tracked"]()) if extras.get("tracked") else set()   # their orders stream live already
    ranked = [u for u, _ in sorted(cancels.items(), key=lambda x: (-x[1], x[0])) if u not in skip]
    for u in ranked[:extras.get("hist_users", 0)]:
        writer.ask_info("historicalOrders", u, (), {"int_cancels": cancels[u]})


def save_info(d, got, meta):
    """Append this capture's info answers to the window's per-kind files; count them on the meta."""
    for kind, recs in got.items():
        path = os.path.join(d, INFO_FILES.get(kind, kind + ".json"))
        old = json.load(open(path)) if os.path.exists(path) else []
        save_json(path, old + recs)
        meta[f"info_{kind}"] = len(old) + len(recs)
        meta[f"info_{kind}_errors"] = sum(1 for r in old + recs if isinstance(r["answer"], dict) and "error" in r["answer"])


def capture(fetcher, start, end, meta, writer=None, state=None, warmup=None, max_anchors=None, extras=None):
    """Fetch start..end oldest-first into its folder; collect anchors as we go. Returns final status.
    extras (chunks): {"fill_users", "hist_users", "trades": fn(t0_ms, t1_ms) -> recorder trades}; see ask_extras."""
    writer = writer or Writer()
    state = state if state is not None else {}
    warmup = WARMUP if warmup is None else warmup
    max_anchors = MAX_ANCHORS if max_anchors is None else max_anchors
    d = os.path.join(WIN_DIR, f"{start}-{end}")
    os.makedirs(d, exist_ok=True)
    wpath, apath = os.path.join(d, "window.json"), os.path.join(d, "anchors.json")
    old = json.load(open(apath)) if os.path.exists(apath) else []
    picker = AnchorPicker(asked={(a["user"], a["oid"]) for a in old})
    scored_targets = {}                                    # (user, real oid) -> height: int-oid targets, scored blocks
    every = max(1, (end - start + 1) // max(1, max_anchors))   # spread lookups across the window
    n_asked = len(old)
    meta.setdefault("fetch_started", time.time())
    meta.update(start_height=start, end_height=end, warmup_end_height=start + warmup, status="fetching")
    meta.setdefault("failed_heights", [])
    fetched_now, t0, status = 0, time.time(), None
    cancels = collections.Counter()
    suspects = load_suspects() if SUSPECT_LOOKUPS else []
    suspect_set, scands = set(suspects), []
    alos = []
    iocs = collections.Counter()
    truth = TruthAsker(os.path.join(d, "truth_status.json"), TRUTH_IOC, writer.budget) if TRUTH_IOC else None
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
        if b is not None:
            picker.feed(h, b)
            cancels.update(int_cancel_users(b))
            iocs.update(ioc_users(b))
            if ALO_MARGIN_USERS:
                alos += alo_placements(b)
            if h >= start + warmup:
                if truth is not None:
                    try:
                        truth.feed(ioc_items(b, h))
                    except Exception as e:           # never lose a chunk over oracle truth
                        meta["truth_error"] = repr(e)
                if suspect_set:
                    scands += suspect_candidates(b, h, suspect_set)
                for u, oid in int_oid_targets(b):
                    scored_targets.setdefault((u, oid), h)
        # one orderStatus lookup at the end of every `every`-block segment; the writer paces them >= 2 s apart
        if n_asked < max_anchors and (i + 1) % every == 0:
            pick = picker.take()
            if pick:
                user, oid, extra = pick
                n_asked += 1
                writer.ask(user, oid, extra.get("cancel_height"), extra)
        if i % 300 == 0 and i:
            meta["blocks_on_disk"] = len(glob.glob(os.path.join(d, "*.json.gz")))
            save_json(wpath, meta); save_json(apath, old + writer.snapshot())
    if truth is not None:
        meta["truth_ioc_queued"] = truth.queued
        truth.close()
    meta["int_cancel_users"] = dict(sorted(cancels.items(), key=lambda x: (-x[1], x[0]))[:30])
    meta["ioc_users"] = dict(sorted(iocs.items(), key=lambda x: (-x[1], x[0]))[:100])
    if ALO_MARGIN_USERS and alos:
        try:
            meta["alo_touch_users"] = alo_touch_users(alos, recorder_trades(min(a[4] for a in alos) - 60_000,
                                                                            max(a[4] for a in alos)))
        except Exception as e:                       # never lose a chunk over the next chunk's user choice
            meta["alo_touch_error"] = repr(e)
    if SUSPECT_LOOKUPS and status is None:
        picks = pick_suspect_lookups(scands, {u: i for i, u in enumerate(suspects)}, SUSPECT_LOOKUPS,
                                     skip=picker.asked, seen=picker.seen)
        meta["suspect_candidates"] = len(scands)
        meta["suspect_lookups"] = len(picks)
        for h, t, i, u, c in picks:
            writer.ask_status(u, c, {"placed_height": h, "tx": t, "item": i})
    if extras and status is None:
        ask_extras(writer, extras, meta, cancels)
    if TARGET_LOOKUPS and status is None and scored_targets:
        covered = {u.lower() for u in (extras or {}).get("covered", lambda: [])()} | \
            {u.lower() for u in meta.get("hist_end_user_list", [])}
        todo = [(u, o, h) for (u, o), h in scored_targets.items() if u.lower() not in covered and (u, o) not in
                {(a["user"], a["oid"]) for a in old}]
        meta["target_lookups"] = min(len(todo), TARGET_LOOKUPS)
        meta["target_candidates"] = len(scored_targets)
        for u, o, h in todo[:TARGET_LOOKUPS]:
            writer.ask(u, o, h, {"kind": "target"}, paced=False)
    writer.drain()                                         # every block on disk, every lookup answered
    anchors = old + writer.snapshot()
    with writer.lock:
        writer.anchors = []
    save_info(d, writer.take_info(), meta)
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


def with_lead_in(span, lead_in):
    """(fetch start, end, warmup) for a chunk span with `lead_in` blocks before it; scoring starts at the span."""
    start, end = span
    return start - lead_in, end, lead_in


def resume_warmup(meta):
    """Warm-up for a resumed window: a chunk keeps its lead-in (0 if none); full windows use the default."""
    return meta.get("lead_in_blocks", 0) if meta.get("kind") == "chunk" else None


def recent_headers():
    p = os.path.join(HOME, "hl_data", time.strftime("%Y-%m-%d/%H", time.gmtime()), "blocks.jsonl")
    if not os.path.exists(p):
        return []
    return [(r["msg"]["height"], r["msg"]["blockTime"]) for r in tail_lines(p)
            if isinstance(r.get("msg"), dict) and "height" in r["msg"]]


def chunk_extras():
    return {"fill_users": CHUNK_FILL_USERS, "hist_users": CHUNK_HIST_USERS, "trades": recorder_trades,
            "tracked": tracked_now, "covered": tracked_now}


def tracked_now():
    """Users the recorder streams live right now (last line of the current hour's tracked_users.jsonl)."""
    p = os.path.join(HOME, "hl_data", time.strftime("%Y-%m-%d/%H", time.gmtime()), "tracked_users.jsonl")
    if not os.path.exists(p):
        return []
    rows = tail_lines(p, 16384)
    return (rows[-1]["msg"]["users"] + rows[-1]["msg"].get("shared", [])) if rows else []


class CancellerTally:
    """int-oid cancel counts per user over the last `keep` chunks -> cancellers.json, which the recorder reads to
    pick the users it streams (orderUpdates gives the exact oid of every order they place)."""

    def __init__(self, path=None, keep=30):
        self.path = path or os.path.join(HOME, "cancellers.json")
        self.recent = collections.deque(maxlen=keep)

    @property
    def last(self):
        """The previous chunk's int-oid cancel counts ({} before the first chunk)."""
        return dict(self.recent[-1]) if self.recent else {}

    def add(self, counts):
        self.recent.append(collections.Counter(counts or {}))
        tot = sum(self.recent, collections.Counter())
        top = dict(sorted(tot.items(), key=lambda x: (-x[1], x[0]))[:50])
        save_json(self.path, {"time": int(time.time() * 1000), "chunks": len(self.recent), "counts": top})
        return top


def ask_open_orders(writer, snap_ms, trades=None, n=None):
    """frontendOpenOrders of the top makers of the last 10 min, asked as soon as a chunk is planned (the resting
    orders at the snapshot, with oid, timestamp and reduce-only flag). Stored with the chunk (open_orders.json)."""
    n = CHUNK_OPEN_USERS if n is None else n
    if not n:
        return []
    trades = recorder_trades(snap_ms - 600_000, snap_ms) if trades is None else trades
    users = top_makers(trades, n)
    for u in users:
        writer.ask_info("frontendOpenOrders", u, (), {"snapshot_time": snap_ms})
    return users


def snapshot_period_ms(path=None, default=12_400):
    """Median gap between the recorder's recent BTC snapshots (they come on a steady cadence)."""
    p = path or os.path.join(HOME, "hl_data", time.strftime("%Y-%m-%d/%H", time.gmtime()), "book_BTC.jsonl")
    if not os.path.exists(p):
        return default
    t = sorted(r["msg"]["data"]["time"] for r in tail_lines(p, 65536)
               if isinstance(r.get("msg"), dict) and "data" in r["msg"])
    gaps = sorted(b - a for a, b in zip(t, t[1:]) if b > a)
    return gaps[len(gaps) // 2] if gaps else default


def prefetch_open_orders(writer, last_snap, period_ms, lead_ms=None, now=time.time, sleep=time.sleep, trades=None):
    """OPEN_LEAD_MS mode: wait until lead_ms before the predicted next snapshot (last_snap + period), then ask
    frontendOpenOrders of the top recent makers all at once. Returns the latest answer time (ms), or None when
    nothing was asked (no users / mode off)."""
    lead_ms = OPEN_LEAD_MS if lead_ms is None else lead_ms
    if not lead_ms or not CHUNK_OPEN_USERS:
        return None
    target = last_snap + period_ms - lead_ms
    while target < now() * 1000 - period_ms / 2:     # that snapshot is already (nearly) here: aim at the next
        target += period_ms
    wait = target / 1000 - now()
    if wait > 0:
        sleep(wait)
    t = now() * 1000
    trades = recorder_trades(t - 600_000, t) if trades is None else trades
    users = top_makers(trades, CHUNK_OPEN_USERS)
    if not users:
        return None
    return int(writer.ask_info_now("frontendOpenOrders", users, {"prefetch": True}) * 1000)


def hist_end_users(n, path=None, exclude=(), now=None, max_age_s=3600, recent=None, contested_first=3):
    """Users for the end-of-chunk historicalOrders: the ranker's top `contested_first` contested makers, then the
    previous chunk's top int-oid cancellers (`recent`, {user: count}), then the ranker's cancel ranking and the rest
    of its order; minus `exclude` (streamed live already). Without a fresh ranker file: `recent` only."""
    try:
        d = json.load(open(path or os.path.join(HOME, TRACKERS_FILE)))
        if (now if now is not None else time.time()) * 1000 - d.get("time", 0) > max_age_s * 1000:
            d = {}
    except (OSError, ValueError):
        d = {}
    skip = {u.lower() for u in exclude}
    out = []

    def add(users, limit=None):
        k = 0
        for u in users:
            if limit is not None and k >= limit:
                return
            if u.lower() not in skip and u not in out:
                out.append(u)
                k += 1
    add(d.get("contested_scores", {}), contested_first)
    add([u for u, _ in sorted((recent or {}).items(), key=lambda x: (-x[1], x[0]))])
    for group in ("cancel_scores", "counts"):
        add(d.get(group, {}))
    return out[:n]


def ask_hist_at_end(writer, end_height, users, headers=None, now=time.time, sleep=time.sleep, max_wait_s=20.0,
                    settle_s=0.5):
    """Wait until the chunk's last block exists (recorder headers), then ask historicalOrders for every user at
    once. Returns the answer time (s) or None."""
    if not users:
        return None
    headers = headers or recent_headers
    t_end = now() + max_wait_s
    while not any(h >= end_height for h, _ in headers()):
        if now() > t_end:
            log(f"block {end_height} not seen within {max_wait_s:.0f} s; asking historicalOrders anyway")
            break
        sleep(0.2)
    sleep(settle_s)
    return writer.ask_info_now("historicalOrders", users, {"at_chunk_end": True, "end_height": end_height})


def run_chunks(fetcher, state, writer, cancellers=None):
    last_snap, last_start = 0, 0.0
    cancellers = cancellers or CancellerTally()
    answered = None                                  # prefetched open orders: latest answer time (ms)
    prev_iocs = {}                                   # previous chunk's IOC users (margin snapshot)
    prev_touch = {}                                  # previous chunk's post-only makers at the touch
    while True:
        pct = disk_pct()
        if pct > 85:
            log(f"disk {pct:.0f}% full: capture paused (recorder keeps running). Sleeping 30 min.", loud=True)
            time.sleep(1800); continue
        snap = latest_snapshot_ms()
        if snap is None or snap <= last_snap or time.time() - last_start < CHUNK_EVERY_S:
            time.sleep(0.5); continue
        if OPEN_LEAD_MS and CHUNK_OPEN_USERS:
            if answered is None:
                answered = prefetch_open_orders(writer, snap, snapshot_period_ms())
                if answered is None:
                    answered = 0                     # no users: chunk without open orders
                continue
            if answered and snap <= answered:
                time.sleep(0.2); continue            # wait for the snapshot after the answers
            if answered and snap - answered > OPEN_LEAD_MS + 5_000:
                log(f"snapshot {snap} came {snap - answered} ms after the open-orders answers; asking again")
                writer.drop_info("frontendOpenOrders")
                answered = None
                continue
        span = plan_chunk(snap, recent_headers(), CHUNK_BLOCKS)
        if span is None:
            if time.time() * 1000 - snap > 30_000:
                log(f"no block headers around snapshot {snap}; skipping it (recorder lagging?)")
                last_snap = snap
            time.sleep(0.5); continue
        last_snap, last_start = snap, time.time()
        start, end, warmup = with_lead_in(span, LEAD_IN_BLOCKS)
        meta = {"kind": "chunk", "snapshot_time": snap, "lead_in_blocks": warmup, "capture_version": CAPTURE_VERSION,
                "capture_superset_of": CAPTURE_SUPERSET_OF}
        t_plan = time.time()
        if OPEN_LEAD_MS and CHUNK_OPEN_USERS:
            if answered:
                meta["open_orders_lead_ms"] = snap - answered
            answered = None
        else:
            ask_open_orders(writer, snap)
        if MARGIN_USERS:
            mu = margin_users(prev_iocs, recorder_trades(snap - 60_000, snap), MARGIN_USERS)
            am = alo_margin_users(prev_touch, load_suspects(), ALO_MARGIN_USERS, exclude=mu) \
                if ALO_MARGIN_USERS else {"touch": [], "suspect": []}
            if ask_margin(writer, snap, mu, am["touch"] + am["suspect"]):
                meta["margin_users"] = len(mu)
                if ALO_MARGIN_USERS:
                    meta["alo_margin_users"] = am
                meta["margin_answered_at"] = int(time.time() * 1000)
        hist_on = HIST_END_USERS and (not HIST_END_RANDOM or random.Random(snap).random() < 0.5)
        if HIST_END_RANDOM:
            meta["arm_hist_end"] = bool(hist_on)
        if hist_on:
            hu = hist_end_users(HIST_END_USERS, exclude=tracked_now(), recent=cancellers.last)
            ask_hist_at_end(writer, end, hu)
            meta["hist_end_users"] = len(hu)
            meta["hist_end_user_list"] = hu
        if HIST_IOC_USERS:
            iu = [u for u, _ in sorted(prev_iocs.items(), key=lambda x: (-x[1], x[0]))][:HIST_IOC_USERS]
            if iu and ask_hist_at_end(writer, end, iu):
                meta["hist_ioc_users"] = iu
        if OPEN_MARGIN_USERS and meta.get("margin_users"):
            try:                                     # never lose a chunk over the extra snapshot
                with writer.lock:
                    recs = [r for r in writer.info.get("clearinghouseState", []) if r.get("snapshot_time") == snap]
                om = open_margin_users(recs, OPEN_MARGIN_USERS)
                meta["open_margin_users"] = [u for u, _, _ in om]
                if om:
                    ask_open_margin_later(writer, om, snap, t_plan + OPEN_MARGIN_DELAY_S)
            except Exception as e:
                meta["open_margin_error"] = repr(e)
        status = capture(fetcher, start, end, meta, writer=writer, state=state, warmup=warmup, max_anchors=CHUNK_ANCHORS,
                         extras=chunk_extras())
        cancellers.add(meta.get("int_cancel_users"))
        prev_iocs = meta.get("ioc_users") or {}
        prev_touch = meta.get("alo_touch_users") or {}
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
                      head=lambda: recorder_head(os.path.join(HOME, "hl_data")), max_age_min=MAX_AGE_MIN,
                      request_log=REQUEST_LOG)
    writer = Writer()
    log(f"capture started (mode={MODE}, chunk_blocks={CHUNK_BLOCKS}, lead_in_blocks={LEAD_IN_BLOCKS}, chunk_every_s={CHUNK_EVERY_S}, "
        f"window_blocks={window_blocks}, max_age_min={MAX_AGE_MIN}, once={ONCE})")
    resume = unfinished_window()
    if resume:
        start, end, meta = resume
        capture(fetcher, start, end, meta, writer=writer, state=state,
                warmup=resume_warmup(meta),
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
