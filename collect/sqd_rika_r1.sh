#!/bin/bash
# RIKA R1: pull full contested-miss blocks from SQD (plan order) until BUDGET uncompressed bytes are used (R0 rule).
cd ~/book-replay-data
BUDGET=${BUDGET:-40000000}
USED=$(awk '/http\/wire_bytes 200/{for(i=1;i<=NF;i++) if($i=="body_bytes") s+=$(i+1)} END{print s+0}' handoff/sqd_queries.log)
for B in $(tail -n +2 handoff/truth_audit/sqd/pull_order.csv | cut -d, -f3 | awk '!seen[$0]++'); do
  OUT=~/hl-data/sqd/blocks/$B.jsonl
  [ -s "$OUT" ] && grep -q '"header"' "$OUT" && continue
  [ "$USED" -ge "$BUDGET" ] && { echo "budget reached: $USED bytes"; break; }
  collect/sqd_pull.sh $B $B $OUT > /dev/null
  if grep -q '"header"' "$OUT"; then USED=$((USED + $(wc -c < "$OUT"))); else rm -f "$OUT"; fi
  /bin/sleep 6                                  # ~0.1 MB/s public rate
done
echo "done: used $USED bytes (uncompressed, incl. R0), blocks on disk $(ls ~/hl-data/sqd/blocks/*.jsonl 2>/dev/null | wc -l)"
