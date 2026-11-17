# Server setup report: unattended window capture

**Status (Nov 17, 09:52 UTC):** both services run on their own, start on boot, and survived a reboot test. **Capture hasn't produced a server window yet:** the server's IP is still locked out by the explorer's daily cap (from the old one-off fetch). The service waits and retries every 30 min, and starts capturing by itself once the cap resets. The whole capture loop was tested end to end on the Mac (below).

## Everyday commands (Mac)

Run them from the repo (`server/status.sh`) or from `~/Downloads` (`./status.sh`). Both read `~/Downloads/server.env` (the IP), `~/Downloads/hype-nodevm-key.pem` and `~/Downloads/server_data/`. Override with `HL_ENV`, `HL_KEY`, `HL_DATA`.

| what | command |
|---|---|
| check on the server | `./status.sh` |
| download new data | `./sync_down.sh` → `~/Downloads/server_data/{windows,hl_data,blocks}` |
| download **and free server disk** | `./sync_down.sh --prune`: each finished window is deleted on the server only after its files reached the Mac (`rsync --remove-source-files`); the window being captured is never touched. Run it at least every ~3 days |
| log in | `source server.env && ssh -i hype-nodevm-key.pem ubuntu@$SERVER_IP` |
| stop capture (on the server) | `sudo systemctl stop hl-capture` (stays off until start; still starts on reboot unless disabled) |
| start capture | `sudo systemctl start hl-capture` |
| turn capture off for good | `sudo systemctl disable --now hl-capture` |
| live log | `tail -f ~/hl/capture.log` or `journalctl -u hl-capture -f` |

Both scripts read the IP from `server.env`. That file and `*.pem` are in `~/Downloads/.gitignore`, so git won't pick them up. Nothing needs to run on the Mac between checks.

## What changed on the server (`~/hl/`)

| file | what |
|---|---|
| `fetch_blocks.py` | Replaced. Has a `Fetcher` class: refuses error responses, checks `len(txs) == numTxs`, writes via temp file + rename, resumes, backs off on HTTP 429, raises a clear error on the archived cap. Default = fresh HTTPS connection per request (measured fastest, below); `--keepalive` is kept for experiments |
| `capture.py` | New. The capture loop |
| `hl-capture.service` → `/etc/systemd/system/` | New unit: `Restart=always`, `RestartSec=10`, `User=ubuntu`, unbuffered, `WantedBy=multi-user.target` (starts on boot), after `hl-recorder` |
| `windows/<start>-<end>/` | Output: `<height>.json.gz`, `window.json`, `anchors.json` |
| `capture.log` | One line per window, plus loud `!!!` lines for problems |
| `capture_state.json` | Window size remembered after a cap-halving (delete it to return to 6,100) |
| `hl-recorder` | **Not touched** (only read its files and rebooted) |
| tmux session `fetch` | Killed. It had finished ("all blocks complete") and was idle |

## 1. Bad files deleted

Checked every file's content in `blocks/1223994849-1224012849/` (block = has a `txs` list):

```
before: good 10431 bad 7570 {'More than 100 archived blocks queried in one day': 7570}
deleted 7570
after: files left 10431
```

That's more bad files than the expected 4,607, because the old fetch kept saving errors until it ended. All 7,570 had the same cap message. 10,431 is the real good count (the handoff said 10,353).

Your **Mac copy** (`server_data/blocks/1223994849-1224012849/`) still has the 4,607 error files from the earlier copy; `sync_down.sh` never deletes. `g0_check.py` skips them. Delete them locally if you like (ask me).

## 2. Fetch speed (same limit)

The server's IP is capped, so all benchmarks ran **from the Mac**: 200 fresh blocks each (~1 min old), one run at a time.

| method | blocks/s | HTTP 429s |
|---|---|---|
| old: new connection per block | **1.118**, rerun **1.058** | (not counted), 24 |
| keep-alive, no pacing | 0.862 | ~25 |
| keep-alive, 0.8 s pacing | 0.976 | 12 |
| keep-alive, 0.9 s pacing | 0.842 | 22 |

- **gzip is not supported:** 0 of 225+ responses were gzip-encoded. The header is still sent and handled if that changes.
- **Keep-alive is slower** at every pacing tried. The explorer seems to throttle a long-lived connection harder. So the default stays one fresh connection per request: about **1.06–1.12 blocks/s**. A 6,100-block window takes ~95 min to fetch.
- Rerun on the server once the cap resets: `cd ~/hl && .venv/bin/python fetch_blocks.py S S+199 --out /tmp/b` (S = newest height − 500). capture.log also reports blocks/s for every window.

## 3. How capture works

Loop forever:
1. **Newest height** = last line of the recorder's `hl_data/<UTC date>/<hour>/blocks.jsonl`. If that header is >60 s old, it logs `!!! RECORDER LOOKS DOWN` and retries every 60 s; nothing is fetched.
2. **Window** = newest − `WINDOW_BLOCKS` + 1 … newest (default 6,100 ≈ 2 min warm-up + 5 min scored). Fetched oldest-first, one request at a time.
3. **Anchors:** spread through the fetch (one lookup per ~61 blocks, ≥2 s apart, up to 100), it picks a BTC/ETH int-oid cancel from the blocks just fetched (quieter users first) and asks `orderStatus` while the order is minutes old.
4. Writes `window.json` (every 300 blocks and at the end) and `anchors.json`, logs one line, and **immediately starts the next window from the then-newest height** (fixed rule → unbiased sample).

Safety rules:
- **Never request old blocks (`MAX_AGE_MIN`, default 120).** Before every request, capture checks the block's age: the previous block's time, or for a window's first block the recorder's newest height. If the next block is over 120 min old (e.g. after an outage), it stops that window (`partial`, with a note) and starts fresh at the newest block. The archived threshold is ~3 h (the old fetch ran fine until ~3 h behind the head, then hit the cap within ~100 blocks), so this keeps ~1 h of margin. A normal window's last block is ~95 min old when fetched. `fetch_blocks.py` has the same guard (`--max-age-min`, default 120; it also checks the first block against the recorder's headers when run in `~/hl`). Tested: asked for 32-min-old blocks with a 1-min limit, it refused with zero requests.
- **Archived cap:** the window is marked `archived_cap`. If blocks had already been fetched, `WINDOW_BLOCKS` is halved (min 1,800) and remembered. If the cap hit on the very first block, the IP is simply locked out, so the size is kept. Then it **waits, probing once every 30 min** (one request), until the explorer answers again. This changed from the spec: discovered today, **once an IP is over the cap, every block is refused, even the newest one**, so retrying a smaller window right away would just hammer the explorer.
- **Disk > 85%:** capture pauses with a `!!!` log line, rechecks every 30 min; the recorder keeps running.
- **Restart:** a window left `fetching` is resumed if its oldest missing block is < 90 min old (`FRESH_MIN`); otherwise it's marked `partial` with a note, and a new window starts.
- Settings (environment variables in the unit file): `WINDOW_BLOCKS`, `WARMUP_BLOCKS` (1740), `MAX_ANCHORS` (100), `FRESH_MIN` (90), `ONCE=1` (one window, for tests).

## 4. Tests

**One window by hand** (Mac, `WINDOW_BLOCKS=300`, a temporary recorder feeding `blocks.jsonl`, the same `capture.py`). Log line:
```
2026-11-17 09:38:08 UTC window 1224448463-1224448762 status=complete blocks=300/300 blocks/s=0.858 anchors=46/62 failed=0
```
Its `window.json` (first window captured):
```json
{
 "fetch_started": 1794907938.8214011,
 "start_height": 1224448463,
 "end_height": 1224448762,
 "warmup_end_height": 1224450203,
 "status": "complete",
 "failed_heights": [],
 "first_block_time": 1794907917449,
 "blocks_on_disk": 300,
 "last_block_time": 1794907938483,
 "fetch_finished": 1794908288.596172,
 "blocks_per_s": 0.858,
 "anchors_found": 46,
 "anchors_asked": 62
}
```
(`warmup_end_height` lies past `end_height` only because this test window was shorter than the 1,740-block warm-up.) `anchors.json`: 62 lookups, 46 full answers (`"status": "order"`, with coin, side, limitPx, sz, oid, timestamp, cloid), 16 `unknownOid`. That run used the keep-alive fetcher, since switched to the faster default.

**Service on the server:** enabled, started, immediately hit the IP lockout, logged it, went into 30-min probing.

**Reboot test:** after `sudo reboot`, about 75 s later:
```
 09:51:41 up 1 min,  1 user,  load average: 0.23, 0.08, 0.03
hl-recorder: active
hl-capture:  active
2026-11-17 09:50:42 UTC capture started (window_blocks=6100, once=False)
```
`./status.sh` right after: both active, disk 8%, newest recorded trade 1 s ago.

## ⚠ Two things you need to decide or watch

1. **Disk fills in ~4 days unless you prune. Decision: option (a), `./sync_down.sh --prune`** (tested Nov 17: 4 windows moved to the Mac, 0 left on the server). Background: A block is ~105 KB gzipped, so a 6,100-block window is ~640 MB. Back to back that's ~15 windows/day ≈ **9.6 GB/day**. With 44 GB free, the 85% stop arrives after ~4 days. Then capture pauses safely, but nothing new is captured. Options, pick one:
   - (a) After each `./sync_down.sh`, delete synced windows on the server (I can add a `--prune` option that deletes only windows whose files match the Mac copy).
   - (b) Store only what the replay needs: full BTC/ETH txs + cancels/scheduleCancel, and for other markets only per-tx counts of order items (needed for oid counting). Probably ~10× smaller, but it's a format decision.
   - (c) Pause between windows (e.g. one window every 3 h ≈ 5 GB/day).
2. **When the cap resets is unknown.** It's either midnight UTC or 24 h after the lockout (~03:40 UTC Nov 18). Run `./status.sh` tomorrow morning: the log should show `explorer accepts requests again`, then a window starting. Normal capture shouldn't hit the cap again: the old fetch only hit it after ~3 h behind the head, and a window's blocks are at most ~95 min old when fetched.

## If something breaks

| symptom in `./status.sh` | do this |
|---|---|
| `hl-capture failed` / `inactive` | log in, `journalctl -u hl-capture -n 50` to see why, then `sudo systemctl restart hl-capture` |
| `!!! RECORDER LOOKS DOWN` | `sudo systemctl status hl-recorder`; `sudo systemctl restart hl-recorder` |
| newest trade is minutes old | same as above (recorder) |
| `!!! disk … full` | free space (see the disk options above), capture resumes within 30 min |
| `still over the daily archived-block cap` for > 24 h | tell me; don't run any other explorer client from the server meanwhile |
| `ssh` times out | the instance may be stopped: start it in the AWS console (EC2 → Instances); both services come back on boot. A stop/start can change the public IP: update `server.env` |

Engine-side rules for these windows: `ENGINE_WINDOW_RULES.md`.
