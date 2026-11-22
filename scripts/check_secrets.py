"""Refuse a commit that looks like it carries a private key, an env file, or a large model file (MAHITO_PLAN §0b).

Checks the STAGED version of every staged file:
  - an assignment of a 64-hex-digit value (optionally 0x-prefixed) to a key-like name
    (private_key, secret, sk, api_key, HL_TESTNET_KEY_*, wallet key, ...)
  - PEM private-key blocks
  - staged .env / .env.* / *.key / *.pem files
  - staged model/data files over 1 MB (.pkl .lgb .onnx .bin .pt .joblib .parquet)
Bare 0x + 64 hex strings are NOT flagged on their own: Hyperliquid tx hashes have exactly that shape and appear all
over tests and docs. So a key pasted with no key-like name next to it is not caught; keys must come from env vars.

usage: python scripts/check_secrets.py            (staged files; exit 1 on a finding)
       python scripts/check_secrets.py FILE...    (check these files on disk, for tests)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

KEY_ASSIGN = re.compile(
    r"(?i)(private[_-]?key|secret[_-]?key|secret|\bsk\b|api[_-]?key|hl_testnet_key_\w*|wallet[_-]?key|signing[_-]?key)"
    r"\s*[\"']?\s*[:=]\s*[\"']?(0x)?[0-9a-f]{64}\b")
PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
BAD_NAMES = re.compile(r"(^|/)(\.env(\..*)?|.*\.key|.*\.pem)$")
BIG_EXT = {".pkl", ".lgb", ".onnx", ".bin", ".pt", ".joblib", ".parquet"}
MAX_MODEL_BYTES = 1_000_000


def findings(name: str, data: bytes) -> list[str]:
    out = []
    if BAD_NAMES.search(name):
        out.append(f"{name}: env/key file")
    if Path(name).suffix in BIG_EXT and len(data) > MAX_MODEL_BYTES:
        out.append(f"{name}: {Path(name).suffix} file over 1 MB ({len(data)} bytes)")
    text = data.decode("utf-8", errors="ignore")
    for i, line in enumerate(text.splitlines(), 1):
        if KEY_ASSIGN.search(line):
            out.append(f"{name}:{i}: 64-hex value assigned to a key-like name")
        if PEM.search(line):
            out.append(f"{name}:{i}: PEM private key")
    return out


def staged() -> list[tuple[str, bytes]]:
    names = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
                           capture_output=True, check=True).stdout.split(b"\0")
    out = []
    for n in filter(None, names):
        name = n.decode()
        out.append((name, subprocess.run(["git", "show", f":{name}"], capture_output=True, check=True).stdout))
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    files = [(p, Path(p).read_bytes()) for p in argv] if argv else staged()
    bad = [f for name, data in files for f in findings(name, data)]
    for b in bad:
        print("REFUSED:", b, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
