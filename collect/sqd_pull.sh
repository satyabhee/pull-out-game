#!/bin/bash
# sqd_pull.sh FROM TO OUT [FILTER_JSON]: one SQD public-portal replica-cmds query (BLITZ Addendum 5, free plan ~50 MB/month).
# FILTER examples: '"actions":[{}]' (all) or '"orderActions":[{"containsAsset":[0]}],"cancelActions":[{"containsAsset":[0]}]'.
# Logs blocks, wire bytes (compressed), body bytes and seconds to handoff/sqd_queries.log. FILTER_JSON default: all actions.
FROM=$1; TO=$2; OUT=$3; FILTER=${4:-'"actions":[{}]'}     # a JSON fragment: one or more request arrays
LOG=~/book-replay-data/handoff/sqd_queries.log
BODY="{\"type\":\"hyperliquidReplicaCmds\",\"fromBlock\":$FROM,\"toBlock\":$TO,\"fields\":{\"block\":{\"number\":true,\"timestamp\":true},\"action\":{\"actionIndex\":true,\"user\":true,\"action\":true,\"status\":true,\"response\":true,\"nonce\":true}},$FILTER}"
for TRY in 1 2 3 4; do
T0=$(date +%s.%N)
W=$(curl -s --compressed -X POST https://portal.sqd.dev/datasets/hyperliquid-replica-cmds/stream -H 'Content-Type: application/json' -d "$BODY" -D "$OUT.headers" -o "$OUT" -w '%{http_code} %{size_download}')
T1=$(date +%s.%N)
echo "$(date -u +%FT%TZ) blocks $FROM-$TO filter $FILTER http/wire_bytes $W body_bytes $(wc -c < "$OUT" | tr -d ' ') secs $(echo "$T1 - $T0" | bc) try $TRY usage $(grep -i '^x-sqd-usage' "$OUT.headers" | tr -d '\r' | sed 's/x-sqd-usage-//I' | tr '\n' ' ')" >> $LOG
case "$W" in 429*|529*|503*) [ $TRY -lt 4 ] && { /bin/sleep $((20 * TRY)); continue; };; esac
break
done
tail -1 $LOG
