"""A3 additions (docs/gate2.md): one-block mode, first-listed filter, bps reporting."""
from decimal import Decimal

from analysis.gate2 import (GFill, _assemble_one_block, _one_block_send_times, block_values, bps_diff,
                            cluster_boot_ratio)


def f(maker, side, px, h=7, taker="t"):
    return GFill(chunk="c", height=h, taker_hash=taker, maker=maker, maker_user="m", maker_side=side,
                 px=Decimal(px), sz=Decimal(1), coin="BTC", time_ms=0)


def test_block_values_splits_markout_and_notional_by_group():
    v = block_values([f("b", "S", 99)], [f("a", "S", 100)], {"a"}, lambda c, t: Decimal(102))
    assert v["m_race_off"] == -2 and v["ntl_race_off"] == 100 and v["m_race_on"] == 0
    assert v["m_absorb_on"] == -3 and v["ntl_absorb_on"] == 99


def test_bps_diff_and_ratio_bootstrap():
    assert bps_diff(1, 100, 2, 100) == -100.0
    assert bps_diff(1, 0, 2, 100) is None
    per = {"a": (1.0, 100.0, 2.0, 100.0), "b": (1.0, 100.0, 2.0, 100.0)}
    assert cluster_boot_ratio(per, reps=50) == (-100.0, -100.0, -100.0)
    assert cluster_boot_ratio({}, reps=5) == (None, None, None)


def test_one_block_send_times_keeps_only_that_height(tmp_path):
    src = tmp_path / "s.csv"
    src.write_text("height,tx_hash,item_index,user,nonce,send_ms,calibrated,batch\n"
                   "7,0xa,0,u,1,1,1,0\n8,0xb,0,u,2,2,1,0\n7,0xc,0,u,3,3,1,0\n")
    p = _one_block_send_times(src, tmp_path / "by", 7)
    assert p.read_text().splitlines()[1:] == ["7,0xa,0,u,1,1,1,0", "7,0xc,0,u,3,3,1,0"]


def test_assemble_one_block_takes_each_off_replay_for_its_own_block():
    on = {"status": "ok", "fills": [{"height": 7}, {"height": 9}], "raced": [], "txs": [(7, "x"), (9, "y")]}
    off7 = {"status": "ok", "fills": [{"height": 7}], "raced": [{"height": 7}], "txs": [(7, "x")]}
    off9 = {"status": "ok", "fills": [{"height": 9}], "raced": [], "txs": [(9, "y")]}
    parts = [{"tag": "off:9", "chunk": "c", "arm": off9}, {"tag": "on", "chunk": "c", "arm": on},
             {"tag": "off:7", "chunk": "c", "arm": off7}]
    (r,) = _assemble_one_block(parts)
    assert r["arms"]["on"] is on
    assert r["arms"]["off"]["fills"] == [{"height": 7}, {"height": 9}] and r["arms"]["off"]["raced"] == [{"height": 7}]
    bad = _assemble_one_block([{"tag": "on", "chunk": "c", "arm": on},
                               {"tag": "off:7", "chunk": "c", "arm": {"status": "failed", "error": "E"}}])
    assert bad[0]["arms"]["off"]["status"] == "failed"


def test_observer_ignores_schedule_cancel_in_a_listed_block():
    # a ScheduleCancel has no asset; the gate-2 hook must not look one up when there are no fills (smoke run, A3)
    from types import SimpleNamespace

    from analysis.gate2 import observer_for
    from engine_py.types import ScheduleCancel
    obs = observer_for(frozenset({7}))({}, {})
    rp = SimpleNamespace(minute=lambda t: 0, engine=SimpleNamespace(by_user={}))
    block = SimpleNamespace(height=7, time_ms=0)
    a = ScheduleCancel(user="u", block=7, tx_index=0, item_index=0, hash="0xS", priority=None, at_ms=None)
    obs.before_action(rp, block, a)
    obs.after_action(rp, block, a, [], [], None)
    assert obs.g_fills == [] and obs.g_txs == {(7, "0xs")}


def test_score_marks_a_run_with_a_failed_chunk_invalid_and_exits_1(tmp_path):
    import json
    import pickle

    import pytest

    from analysis.gate2 import score_main
    st = tmp_path / "s.csv"
    st.write_text("height,tx_hash,item_index,user,nonce,send_ms,calibrated,batch\n7,0xa,0,u,1,1,1,0\n")
    (tmp_path / "meta.json").write_text(json.dumps({"argv": ["--hl-data", str(tmp_path)], "send_times": str(st),
                                                    "blocks_sha256": "0" * 64, "send_times_sha256": "0" * 64}))
    failed = {"status": "failed", "error": "E"}
    pickle.dump([{"chunk": "c", "arms": {"on": failed, "off": failed}}], open(tmp_path / "results.pkl", "wb"))
    with pytest.raises(SystemExit) as e:
        score_main([str(tmp_path)])
    assert e.value.code == 1
    assert (tmp_path / "score.txt").read_text().startswith("INVALID: 1 chunks failed")
