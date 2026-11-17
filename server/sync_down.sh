#!/bin/bash
# sync_down.sh: copy new server data to the Mac (only new/changed files; safe any time).
#
#   ./sync_down.sh            copy windows/, hl_data/, blocks/  ->  ~/Downloads/server_data/
#   ./sync_down.sh --prune    same, then free server disk: each FINISHED window is deleted on the
#                             server only after its files arrived on the Mac (rsync --remove-source-files).
#                             The window being captured right now is never touched; hl_data and
#                             blocks are never pruned.
# Paths (override with environment variables): HL_ENV (file with SERVER_IP=...), HL_KEY, HL_DATA.
set -euo pipefail
HL_ENV="${HL_ENV:-$HOME/Downloads/server.env}"
HL_KEY="${HL_KEY:-$HOME/Downloads/hype-nodevm-key.pem}"
HL_DATA="${HL_DATA:-$HOME/Downloads/server_data}"
source "$HL_ENV"
SSH="ssh -o ConnectTimeout=15 -i $HL_KEY"
R="ubuntu@$SERVER_IP"
mkdir -p "$HL_DATA/windows"

rsync -az --partial --exclude '*.tmp' -e "$SSH" "$R:~/hl/windows" "$R:~/hl/hl_data" "$R:~/hl/blocks" "$HL_DATA/"
echo "synced to $HL_DATA ($(du -sh "$HL_DATA" | cut -f1))"

if [ "${1:-}" = "--prune" ]; then
  # finished = window.json exists and its status is not "fetching"
  finished=$($SSH "$R" 'cd ~/hl/windows 2>/dev/null || exit 0
    for d in */; do d=${d%/}
      s=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[\"status\"])" "$d/window.json" 2>/dev/null || true)
      [ -n "$s" ] && [ "$s" != fetching ] && echo "$d"
    done')
  n=0
  for w in $finished; do
    rsync -az --remove-source-files --exclude '*.tmp' -e "$SSH" "$R:~/hl/windows/$w" "$HL_DATA/windows/"
    n=$((n + 1))
  done
  $SSH "$R" 'find ~/hl/windows -mindepth 1 -type d -empty -delete; df -h / | tail -1'
  echo "pruned $n finished window(s) from the server (all on the Mac in $HL_DATA/windows)"
fi
