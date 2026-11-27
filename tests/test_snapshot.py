"""Snapshot at N, restore, continue: hashes must match a straight run.

NOT BUILT (Satya, Nov 27): engine_py/snapshot.py has no save / restore / resume yet. This test is an explicit skip
so the gap shows in every pytest run; docs/determinism_check.md marks "restart from snapshot: not built".
"""
import pytest


def test_restart_from_snapshot_matches_straight_run():
    pytest.skip("restart from snapshot: not built (engine_py/snapshot.py is a stub; docs/determinism_check.md)")
