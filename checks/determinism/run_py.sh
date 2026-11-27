#!/bin/bash
# run_py.sh SEED OUT: B3 (handoff/i27/baseline.txt line 2) on dev20, Python engine, given PYTHONHASHSEED.
cd ~/book-replay-detcheck
FLAGS=$(sed -n 2p ~/book-replay-data/handoff/i27/baseline.txt)
date -u
PYTHONHASHSEED=$1 ~/book-replay-unified/.venv/bin/python -m analysis.validate_chunks ~/hl-data/mac3/dev20 \
  --hl-data ~/hl-data/mac3/hl_data --out "$2" --jobs 3 $FLAGS
echo rc=$?
date -u
