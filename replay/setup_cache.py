"""Per-chunk setup cache (Satya, Q4 and Q9): the built Replay (decoded blocks, snapshots, oid facts, resolver, touch
events, seeded engine) saved to disk right after construction, before any block is replayed, together with the
recorded inputs scoring reads (analysis/validate_replay.scoring_inputs: real trades, ALO truth). Loading only:
scoring logic is unchanged, and a cached run's scores are byte-identical (tests/test_setup_cache.py, slow test).

Key = sha256 of four things; any change gives a new key, so a stale entry is never used:
  - chunk:   the chunk folder name, the sha256 of each of its non-block files (window.json, anchors.json, ...) and
             the names + sizes of its block files;
  - data:    name, size and mtime of every recorder file in the hours a replay of this chunk can read
             (first block - 2 h .. last block + 1 h);
  - code:    sha256 of the source the built state depends on (CODE_PATHS: engine_py/, the data loaders,
             analysis/validate_replay.py, the Rust engine wrappers, this file, replay_core/src/ and Cargo.lock)
             plus the Python version;
  - config:  the Replay arguments (policy by name, every option value) and the engine's settings and backend.
             Only plain values are accepted; anything else raises instead of being hashed by repr.

Files: <cache dir>/<key>.pkl.lz4 (pickle protocol 5, lz4 frame). Cache dir: $BOOK_REPLAY_SETUP_CACHE or
~/hl-data/mac3/cache/setup (data: outside the repo). Disk cap: $BOOK_REPLAY_SETUP_CACHE_GB (default 5): after a
write, least recently used files are deleted until the folder is under the cap. A hit touches the file.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import sys
import tempfile
import time
from pathlib import Path

import lz4.frame

from engine_py.replay import Replay

REPO = Path(__file__).resolve().parent.parent
# Only what a built Replay depends on (not the simulator, the parity runner or tests): editing anything else must
# not invalidate the cache.
CODE_PATHS = ("engine_py", "data/parse.py", "data/oids.py", "data/oid_facts.py", "analysis/validate_replay.py",
              "replay/adapter.py", "replay/rust_engine.py", "replay/rust_book.py", "replay/setup_cache.py",
              "replay_core/src", "replay_core/Cargo.lock")
DEFAULT_DIR = Path.home() / "hl-data" / "mac3" / "cache" / "setup"
DEFAULT_CAP_GB = 5.0
HOUR_MS = 3_600_000

stats = {"hit": 0, "miss": 0, "load_s": 0.0, "build_s": 0.0}


def cache_dir() -> Path:
    return Path(os.environ.get("BOOK_REPLAY_SETUP_CACHE", DEFAULT_DIR)).expanduser()


def cap_bytes() -> int:
    return int(float(os.environ.get("BOOK_REPLAY_SETUP_CACHE_GB", DEFAULT_CAP_GB)) * 1e9)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


_code_version: str | None = None


def code_version() -> str:
    """sha256 of the source the built state depends on (computed once per process)."""
    global _code_version
    if _code_version is None:
        h = hashlib.sha256(sys.version.encode())
        for rel in CODE_PATHS:
            p = REPO / rel
            files = sorted(p.rglob("*")) if p.is_dir() else [p]
            for f in files:
                if f.is_file() and f.suffix in (".py", ".rs", ".lock") and "__pycache__" not in f.parts:
                    h.update(str(f.relative_to(REPO)).encode())
                    h.update(f.read_bytes())
        _code_version = h.hexdigest()
    return _code_version


def chunk_fingerprint(window: Path) -> dict:
    window = Path(window)
    blocks, other = [], {}
    for f in sorted(window.iterdir()):
        if f.name.endswith(".json.gz") and f.name[:-8].isdigit():
            blocks.append((f.name, f.stat().st_size))
        elif f.is_file():
            other[f.name] = _sha(f.read_bytes())
    return {"name": window.resolve().name, "files": other, "blocks": _sha(json.dumps(blocks).encode())}


def data_fingerprint(window: Path, hl_data: Path) -> str:
    meta = json.loads((Path(window) / "window.json").read_text())
    t0 = int(meta.get("first_block_time") or meta["snapshot_time"]) - 2 * HOUR_MS
    t1 = int(meta.get("last_block_time") or meta["snapshot_time"]) + HOUR_MS
    rows = []
    for t in range(t0 - t0 % HOUR_MS, t1 + 1, HOUR_MS):
        hour = Path(hl_data) / time.strftime("%Y-%m-%d/%H", time.gmtime(t / 1000))
        if hour.is_dir():
            for f in sorted(hour.iterdir()):
                st = f.stat()
                rows.append((str(f.relative_to(hl_data)), st.st_size, st.st_mtime_ns))
    return _sha(json.dumps(rows).encode())


def _plain(v):
    """v as JSON-able plain data, or raise (a repr could hide state or change between runs)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    raise TypeError(f"setup cache: cannot key a {type(v).__name__} option value")


def config_fingerprint(args: tuple, kw: dict, backend: str) -> str:
    window, hl_data, policy = args
    eng = kw["engine"]
    cfg = {"policy": f"{policy.__module__}.{policy.__qualname__}", "backend": backend,
           "engine": {"type": type(eng).__name__, "stp": eng.stp, "p_priority": eng.p_priority,
                      "evict_arrival": eng.evict_arrival, "check_every_action": eng.check_every_action,
                      **({"docs_t_ms": eng.docs_t_ms} if getattr(eng, "docs_t_ms", 400) != 400 else {})},
           "options": {k: _plain(v) for k, v in sorted(kw.items()) if k != "engine"}}
    return _sha(json.dumps(cfg, sort_keys=True).encode())


def key_for(args: tuple, kw: dict, backend: str) -> str:
    window, hl_data, _ = args
    parts = {"chunk": chunk_fingerprint(window), "data": data_fingerprint(window, hl_data),
             "code": code_version(), "config": config_fingerprint(args, kw, backend)}
    return _sha(json.dumps(parts, sort_keys=True).encode())


def _evict(folder: Path, keep: Path) -> None:
    files = sorted((f for f in folder.glob("*.pkl.lz4")), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    for f in files:
        if total <= cap_bytes():
            break
        if f == keep:
            continue
        total -= f.stat().st_size
        f.unlink()


def get_replay(args: tuple, kw: dict, backend: str) -> Replay:
    """The built Replay for these arguments: loaded from the cache when an entry with the same key exists,
    otherwise built with Replay(*args, **kw) and stored. `kw["engine"]` must be the new, empty engine."""
    folder = cache_dir()
    key = key_for(args, kw, backend)
    path = folder / f"{key}.pkl.lz4"
    t = time.perf_counter()
    if path.exists():
        entry = pickle.loads(lz4.frame.decompress(path.read_bytes()))
        os.utime(path)                                   # most recently used
        stats["hit"] += 1
        stats["load_s"] += time.perf_counter() - t
        entry["replay"].cached_scoring_inputs = entry["inputs"]
        return entry["replay"]
    rp = Replay(*args, **kw)
    # the recorded inputs scoring reads (validate_replay.scoring_inputs): loading only, cached with the Replay
    from analysis.validate_replay import scoring_inputs
    inputs = scoring_inputs(rp, args[1], kw.get("oid_facts", False))
    stats["miss"] += 1
    stats["build_s"] += time.perf_counter() - t
    folder.mkdir(parents=True, exist_ok=True)
    blob = lz4.frame.compress(pickle.dumps({"replay": rp, "inputs": inputs}, protocol=5))
    with tempfile.NamedTemporaryFile(dir=folder, suffix=".tmp", delete=False) as f:
        f.write(blob)
    os.replace(f.name, path)                             # atomic: a reader never sees half a file
    _evict(folder, keep=path)
    rp.cached_scoring_inputs = inputs
    return rp
