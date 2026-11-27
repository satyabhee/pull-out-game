#!/bin/bash
# run_digest2.sh SEED OUTNAME CHUNKFILE [FLAG-SED]: book_digest.py on the chunks listed in CHUNKFILE.
# FLAG-SED (optional) edits the B3 flag line, for the sensitivity check only.
cd ~/book-replay-detcheck
date -u
if [ -n "$4" ]; then export DIGEST_FLAG_SED="$4"; fi
PYTHONPATH=. PYTHONHASHSEED=$1 ~/book-replay-unified/.venv/bin/python report/determinism/book_digest.py \
  report/determinism/$2 $(cat "$3")
echo rc=$?
date -u
