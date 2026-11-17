#!/bin/bash
# status.sh: one-shot health check of the Hyperliquid server.   Usage:  ./status.sh
# Paths (override with environment variables): HL_ENV (file with SERVER_IP=...), HL_KEY.
HL_ENV="${HL_ENV:-$HOME/Downloads/server.env}"
HL_KEY="${HL_KEY:-$HOME/Downloads/hype-nodevm-key.pem}"
source "$HL_ENV"
ssh -o ConnectTimeout=15 -i "$HL_KEY" "ubuntu@$SERVER_IP" 'cd ~/hl
echo "== services";      for s in hl-recorder hl-capture; do printf "%-12s %s\n" $s "$(systemctl is-active $s)"; done
echo "== capture.log (last 5)"; tail -5 capture.log 2>/dev/null || echo "(no log yet)"
echo "== windows on server"; echo "complete: $(grep -l "\"status\": \"complete\"" windows/*/window.json 2>/dev/null | wc -l)   all: $(ls -d windows/*/ 2>/dev/null | wc -l)   (pruned windows live on the Mac)"
echo "== disk";          df -h / | tail -1
echo "== newest recorded trade"
.venv/bin/python - <<"EOF"
import glob, json, time
fs = sorted(glob.glob("hl_data/*/*/trades_*.jsonl"))[-2:]
t = 0
for f in fs:
    for line in open(f).readlines()[-50:]:
        try: t = max(t, max(x["time"] for x in json.loads(line)["msg"]["data"]))
        except Exception: pass
print(f"{(time.time()*1000 - t)/1000:.0f} s ago" if t else "none in the open hour (recorder down?)")
EOF'
