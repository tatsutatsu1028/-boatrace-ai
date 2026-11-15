"""
remote_predict.py（GitHub Actions で計算する予想）が、アプリの中で計算したときと同じ予想・買い目・保存内容に
なること。入力（アプリの表）も結果も JSON（frame_codec）を通してやり取りする。
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pandas.testing as pdt
import pytest

import frame_codec
import legacy_model
import predict_flow
import predict_runtime
import remote_predict
import result_tracker
from test_legacy_model import PARAMS, _race, _synthetic


@pytest.fixture
def env(tmp_path, monkeypatch):
    full = _synthetic(tmp_path)
    monkeypatch.setenv("BOATRACE_DATA_DIR", str(tmp_path))
    legacy_model.build(log=lambda *_: None)
    saves = []

    def fake_save(**kw):
        saves.append(kw)
        return {"status": "inserted", "snapshot_kind": "backtest", "payload_hash": kw["payload_hash"]}

    monkeypatch.setattr(result_tracker, "save_latest_prediction_snapshot", fake_save)
    monkeypatch.setattr(result_tracker, "load_prediction_snapshot_meta", lambda rk: None)
    monkeypatch.setattr(result_tracker, "load_prediction_model_setting", lambda: {})
    monkeypatch.setattr(result_tracker, "load_settings", lambda: dict(result_tracker.DEFAULT_SETTINGS))
    remote_predict._LEGACY.clear()
    predict_runtime.clear_settings_memo()
    yield full, saves
    remote_predict._LEGACY.clear()
    predict_runtime.uninstall()


def _app_side(full, rk):
    """アプリの中で計算したとき（predict_runtime を入れて predict_flow.compute → 保存内容の hash）。"""
    bundle = {}

    def getter():
        if not bundle:
            m, la, info = legacy_model.load()
            bundle.update(model=m, lane_agnostic=la, info=info)
        return bundle

    predict_runtime.install(getter)
    import prediction

    work = _race(full, rk)
    result = predict_flow.compute(prediction.train(None), work, None, PARAMS, {}, "20260901", "01", 1, "20260901_01_1")
    predict_flow.build_payload(result)
    return work, result


def _request(work, mode="model", role="staff"):
    payload = {
        "date": "2026-09-01", "jcd": "01", "rno": 1, "venue": "桐生", "deadlines": {}, "work": work, "odds": None,
        "params": PARAMS, "claude_params": {k: PARAMS[k] for k in ("hedge_enabled", "longshot_min_prob_pct",
                                                                   "total_budget", "min_bet", "value_bias")},
        "label": "2026-09-01 桐生 1R", "mode": mode,
    }
    return {"race_key": "20260901_01_1", "mode": mode, "role": role, "collector_name": "スタッフA",
            "input": json.loads(json.dumps(frame_codec.encode(payload), allow_nan=False))}


def test_actions_result_equals_in_app(env):
    full, saves = env
    for rk in list(full["race_key"].drop_duplicates().iloc[[2, 77, 150, 230, 258]]):
        work, app = _app_side(full, rk)
        out, save_status, line = remote_predict.predict(_request(work))
        back = frame_codec.decode(json.loads(json.dumps(frame_codec.encode(out), allow_nan=False)))
        res = back["result"]
        for k in ("final", "tickets", "before", "work"):
            pdt.assert_frame_equal(app[k], res[k], check_exact=True)
        for name in app["research_variants"]:
            pdt.assert_frame_equal(app["research_variants"][name], res["research_variants"][name], check_exact=True)
        assert res["payload_hash"] == app["payload_hash"]
        assert res["hedge_lane"] == app["hedge_lane"] and res["ticket_plan"] == app["ticket_plan"]
        assert res["final"].attrs == app["final"].attrs
        assert save_status == "inserted" and line.startswith("[TIMING] Actions予想")
        # 過去日・未保存なので保存した（スタッフの名前で）
        assert saves[-1]["collector_name"] == "スタッフA" and saves[-1]["payload_hash"] == app["payload_hash"]


def test_run_marks_done_and_error(env, monkeypatch):
    full, _ = env
    work, _ = _app_side(full, full["race_key"].iloc[0])
    rows = {"r1": {"id": "r1", "status": "queued", **_request(work)}, "r2": {"id": "r2", "status": "queued",
                                                                           **_request(work)}}
    rows["r2"]["input"] = {"$dict": {}}  # 壊れた依頼

    monkeypatch.setattr(remote_predict, "load_request", lambda rid: dict(rows[rid]) if rid in rows else None)
    monkeypatch.setattr(remote_predict, "update_request", lambda rid, **f: rows[rid].update(f))
    assert remote_predict.run("r1") == 0
    assert rows["r1"]["status"] == "done" and rows["r1"]["save_status"] == "inserted"
    assert "final" in frame_codec.decode(rows["r1"]["result"])["result"]
    assert remote_predict.run("r2") == 1
    assert rows["r2"]["status"] == "error" and rows["r2"]["error"]
    # 終わった依頼はもう一度動かしても書き換えない
    assert remote_predict.run("r1") == 0 and rows["r1"]["status"] == "done"
    assert remote_predict.run("missing") == 1


def test_secrets_file_is_valid_toml(tmp_path):
    import tomllib

    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "wss", pathlib.Path(__file__).resolve().parent.parent / "tools" / "write_streamlit_secrets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = mod.build({"SUPABASE_URL": "https://x.supabase.co/", "SUPABASE_KEY": 'k"\\y',
                      "ANTHROPIC_API_KEY": "sk-1", "CLAUDE_READ_MODEL": ""})
    data = tomllib.loads(text)
    assert data["supabase"] == {"url": "https://x.supabase.co", "key": 'k"\\y'}
    assert data["ANTHROPIC_API_KEY"] == "sk-1" and "CLAUDE_READ_MODEL" not in data


def test_work_from_race_matches_screen_table():
    race = pd.DataFrame({"lane": [2, 1, 3, 4, 5, 6], "racer_name": list("bacdef"), "exhibition_time": [6.7] * 6,
                         "original_straight": [7.1, 7.0, None, None, None, None]})
    work = predict_flow.work_from_race(race, date(2026, 11, 15), "徳山", 12)
    assert list(work["lane"]) == [1, 2, 3, 4, 5, 6]
    assert work.loc[0, "racer_name"] == "a" and work["race_no"].eq(12).all() and work["date"].eq("2026-11-15").all()
    assert "original_straight_x" in work.columns  # 画面と同じく、取得した列とオリジナル展示の表の両方を持つ
