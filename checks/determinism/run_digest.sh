#!/bin/bash
# run_digest.sh SEED: external book digest (book_digest.py) on the 4 chunks in digest_chunks.txt.
cd ~/book-replay-detcheck
date -u
PYTHONPATH=. PYTHONHASHSEED=$1 ~/book-replay-unified/.venv/bin/python report/determinism/book_digest.py \
  report/determinism/digest_seed$1 $(cat report/determinism/digest_chunks.txt)
echo rc=$?
date -u
