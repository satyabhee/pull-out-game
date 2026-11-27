#!/bin/bash
# run_rust.sh OUT BASELINE: B3 on dev20 driven through replay.adapter with the Rust backend (replay_core).
cd ~/book-replay-detcheck
FLAGS=$(sed -n 2p ~/book-replay-data/handoff/i27/baseline.txt)
date -u
PYTHONHASHSEED=3 ~/book-replay-unified/.venv/bin/python -m replay.parity ~/hl-data/mac3/dev20 --hl-data ~/hl-data/mac3/hl_data \
  --out "$1" --baseline "$2" --backend rust --no-cache --options "$FLAGS"
echo rc=$?
date -u
