"""rust_digest.py: book_digest.py's per-block digest with the Rust backend (replay.parity's adapter path).
usage (worktree root): PYTHONPATH=. python report/determinism/rust_digest.py OUT CHUNK..."""
import csv, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import book_digest as bd
from analysis import validate_chunks as vc
from analysis import validate_replay as vr
from replay import parity as P
from replay.adapter import run_kwargs_from_options, split_policy

out, chunks = Path(sys.argv[1]), sys.argv[2:]
flags = open(Path.home() / "book-replay-data/handoff/i27/baseline.txt").read().splitlines()[1].split()
policy, opts = split_policy(flags)
hl = str(Path.home() / "hl-data/mac3/hl_data")
kw = run_kwargs_from_options(hl, opts)
P.BACKEND, P.USE_CACHE = "rust", False
def make(*a, **k):
    k["hasher"] = bd.hasher
    return P.make_replay(*a, **k)
vr.Replay = make
found, _ = vc.find_chunks(chunks)
out.mkdir(parents=True, exist_ok=True)
with open(out / "digests.csv", "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["chunk_start", "height", "digest"])
    for c in found:
        bd.ROWS.clear(); bd.SIZES.clear()
        vc.run_batch([c], hl, policy, out_dir=str(out / "csv" / Path(c).name), jobs=1, skipped=[], **kw)
        for h, d in bd.ROWS:
            w.writerow([Path(c).name.split("-")[0], h, d])
        print(Path(c).name, len(bd.ROWS), "engine", type(bd.ROWS and None), flush=True)
