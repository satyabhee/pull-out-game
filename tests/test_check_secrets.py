"""scripts/check_secrets.py: key-like assignments and env/model files are refused; tx hashes are not."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_secrets", Path(__file__).parents[1] / "scripts" / "check_secrets.py")
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

HEX = "ab" * 32


def test_refuses_key_assignments():
    for line in (f"PRIVATE_KEY=0x{HEX}", f"HL_TESTNET_KEY_A = '{HEX}'", f'"secret_key": "0x{HEX}"', f"sk={HEX}"):
        assert cs.findings("x.py", line.encode()), line


def test_refuses_pem_env_and_big_models():
    pem = ("-----BEGIN EC " + "PRIVATE KEY-----\n").encode()     # built at runtime so this file passes the check
    assert cs.findings("a.txt", pem)
    assert cs.findings(".env", b"X=1")
    assert cs.findings("conf/.env.testnet", b"")
    assert cs.findings("models/m.lgb", b"0" * 1_000_001)
    assert not cs.findings("models/m.lgb", b"0" * 1000)


def test_tx_hashes_pass():
    assert not cs.findings("t.py", f"hash = '0x{HEX}'\nfill.taker_hash == '0x{HEX}'".encode())
