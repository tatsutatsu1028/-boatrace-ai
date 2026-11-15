"""
今までのモデルを「アプリの中で学習」から「Actions で学習・保存したものを読む」に変えても、
予想・買い目・資金配分が1つも変わらないことを確かめる（legacy_model.py）。

  - 合成データ: いつも実行する（データ用リポジトリが無くても動く）
  - 実データ: LEGACY_REAL_DATA_DIR にデータ用リポジトリ（sample_history.csv・history_full.csv）の
    場所を入れたときだけ実行する。LEGACY_REAL_RACES（カンマ区切りの race_key）で対象のレースを
    変えられる（既定は厳選・万舟チャンス・通常を混ぜた8レース）。データは公開しないので、
    テストはデータの中身を出さず、一致したかどうかだけを見る。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

import legacy_model
import manshu
import ml_live
import prediction
from model_only import model_only_prediction

PARAMS = {
    "display_weight": 0.32,
    "weather_weight": 0.10,
    "venue_course_weight": 0.12,
    "hedge_enabled": True,
    "longshot_min_prob_pct": 0.30,
    "total_budget": 2000,
    "min_bet": 100,
    "value_bias": 0.0,
}
# 今までのモデルで予想する設定（新しいモデルを使わない）
LEGACY_SETTING = {"prediction_model": ml_live.CURRENT}
# 万舟チャンス（20261113_24_4, 20261113_16_9, 20261113_16_8）・展示前厳選
# （20261113_24_11, 20261113_23_1, 20261113_20_8）・通常（20261113_02_1, 20261113_02_12）
REAL_RACES = (
    "20261113_24_4,20261113_16_9,20261113_16_8,"
    "20261113_24_11,20261113_23_1,20261113_20_8,"
    "20261113_02_1,20261113_02_12"
)


def _synthetic(dir_: Path, races=260, seed=7):
    rng = np.random.default_rng(seed)
    venues = ["桐生", "戸田", "江戸川", "平和島"]
    kimarite = ["逃げ", "差し", "まくり", "まくり差し"]
    rows = []
    for i in range(races):
        d = f"202609{(i % 28) + 1:02d}"
        jcd = f"{(i % 4) + 1:02d}"
        rno = (i // 4) % 12 + 1
        order = rng.permutation(6) + 1
        for lane in range(1, 7):
            rows.append({
                "lane": lane,
                "racer_id": str(4000 + int(rng.integers(0, 300))),
                "racer_name": f"選手{lane}",
                "racer_class": rng.choice(["A1", "A2", "B1"]),
                "avg_st": round(float(rng.uniform(0.12, 0.22)), 2),
                "racer_win_rate": round(float(rng.uniform(3, 8)), 2),
                "local_win_rate": round(float(rng.uniform(3, 8)), 2),
                "motor_2ren": round(float(rng.uniform(20, 50)), 2),
                "boat_2ren": round(float(rng.uniform(20, 50)), 2),
                "exhibition_time": round(float(rng.uniform(6.5, 6.9)), 2),
                "exhibition_st": round(float(rng.uniform(0.0, 0.25)), 2),
                "tilt": -0.5,
                "temperature": 20.0, "wind_speed": 2.0, "water_temperature": 18.0, "wave_height": 1.0,
                "finish": int(order[lane - 1]),
                "race_date": d, "jcd": jcd, "venue": venues[i % 4], "race_no": rno,
                "race_key": f"{d}_{jcd}_{rno}_{i}",
                "kimarite": kimarite[int(rng.integers(0, 4))],
            })
    full = pd.DataFrame(rows)
    full.to_csv(dir_ / "history_full.csv", index=False)
    sample = full.drop(columns=["race_key", "kimarite"]).copy()
    sample["race_date"] = pd.to_datetime(sample["race_date"]).dt.strftime("%Y-%m-%d")
    sample.to_csv(dir_ / "sample_history.csv", index=False)
    return full


def _race(full, race_key):
    r = full[full["race_key"].astype(str) == race_key].sort_values("lane").reset_index(drop=True)
    return r.drop(columns=[c for c in ("finish", "kimarite", "trifecta", "trifecta_payout_per_100",
                                       "finish_full") if c in r.columns])


def _compare_outputs(a, b):
    """model_only_prediction の戻り値2つが（買い目・資金配分・比較用の予想まで）同じか。"""
    pdt.assert_frame_equal(a["final"], b["final"], check_exact=True)
    pdt.assert_frame_equal(a["tickets"], b["tickets"], check_exact=True)
    pdt.assert_frame_equal(a["before"], b["before"], check_exact=True)
    assert a["hedge_lane"] == b["hedge_lane"]
    assert a["ticket_plan"] == b["ticket_plan"]
    assert a["model_info"] == b["model_info"]
    assert list(a["research_variants"]) == list(b["research_variants"])
    for k in a["research_variants"]:
        pdt.assert_frame_equal(a["research_variants"][k], b["research_variants"][k], check_exact=True)
    # 万舟チャンスの穴寄りの買い目（表示している予想の3連単確率から作る）
    assert _longshot(a["final"]) == _longshot(b["final"])


def _longshot(final):
    tri = prediction.trifecta(final)[["combo", "prob"]].assign(race_key="r")
    return manshu.longshot_tickets(tri, manshu.LONGSHOT_N).get("r", [])


def _check_dir(monkeypatch, data_dir: Path, races, full):
    monkeypatch.setenv("BOATRACE_DATA_DIR", str(data_dir))
    history = pd.read_csv(data_dir / "sample_history.csv")
    # 変更前: アプリの中で学習していたのと同じ呼び方
    trained = prediction.train(history)
    trained_la = prediction.train_lane_agnostic(history)
    # 変更後: Actions と同じ build() で保存し、アプリと同じ load() で読む
    info = legacy_model.build(log=lambda *_: None)
    loaded, loaded_la, loaded_info = legacy_model.load()
    assert loaded_info == info
    assert info["sample_history_rows"] == len(history)

    checked = 0
    for rk in races:
        work = _race(full, rk)
        assert len(work) == 6, rk
        date_key, jcd, rno = rk.split("_")[:3]
        a = model_only_prediction(trained, work, None, PARAMS, LEGACY_SETTING, date_key, jcd, int(rno))
        b = model_only_prediction(loaded, work, None, PARAMS, LEGACY_SETTING, date_key, jcd, int(rno))
        _compare_outputs(a, b)
        assert prediction.lane_agnostic_strongest(trained_la, work) == \
            prediction.lane_agnostic_strongest(loaded_la, work)
        checked += 1
    return checked


def test_saved_model_gives_identical_predictions_synthetic(tmp_path, monkeypatch):
    full = _synthetic(tmp_path)
    races = list(full["race_key"].drop_duplicates().iloc[[0, 7, 50, 120, 200, 259]])
    assert _check_dir(monkeypatch, tmp_path, races, full) == 6


@pytest.mark.skipif(not os.environ.get("LEGACY_REAL_DATA_DIR"), reason="実データは LEGACY_REAL_DATA_DIR があるときだけ")
def test_saved_model_gives_identical_predictions_real(tmp_path, monkeypatch):
    src = Path(os.environ["LEGACY_REAL_DATA_DIR"])
    for name in legacy_model.SOURCE_FILES:
        shutil.copy(src / name, tmp_path / name)
    full = pd.read_csv(tmp_path / "history_full.csv", dtype={"jcd": str, "race_key": str}, low_memory=False)
    races = [r for r in os.environ.get("LEGACY_REAL_RACES", REAL_RACES).split(",") if r]
    assert _check_dir(monkeypatch, tmp_path, races, full) == len(races) >= 5


def test_build_skips_when_sources_unchanged(tmp_path, monkeypatch):
    _synthetic(tmp_path, races=150)
    monkeypatch.setenv("BOATRACE_DATA_DIR", str(tmp_path))
    first = legacy_model.build(log=lambda *_: None)
    mtime = (tmp_path / legacy_model.MODEL_FILE).stat().st_mtime_ns
    again = legacy_model.build(log=lambda *_: None)
    assert again == first
    assert (tmp_path / legacy_model.MODEL_FILE).stat().st_mtime_ns == mtime
    # 学習データが変われば作り直す
    with open(tmp_path / "history_full.csv", "a", encoding="utf-8") as fh:
        fh.write("\n")
    assert legacy_model.build(log=lambda *_: None)["sources"]["history_full.csv"]["blob_sha"] != \
        first["sources"]["history_full.csv"]["blob_sha"]


def test_load_refuses_missing_or_other_sklearn(tmp_path, monkeypatch):
    monkeypatch.setenv("BOATRACE_DATA_DIR", str(tmp_path))
    with pytest.raises(legacy_model.LegacyModelUnavailable):
        legacy_model.load()
    _synthetic(tmp_path, races=150)
    legacy_model.build(log=lambda *_: None)
    info_path = tmp_path / legacy_model.INFO_FILE
    import json

    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["versions"]["sklearn"] = "0.0.1"
    info_path.write_text(json.dumps(info), encoding="utf-8")
    with pytest.raises(legacy_model.LegacyModelUnavailable, match="scikit-learn"):
        legacy_model.load()


def test_stale_sources():
    info = {"sources": {"history_full.csv": {"blob_sha": "a"}, "sample_history.csv": {"blob_sha": "b"}}}
    assert legacy_model.stale_sources(info, {"history_full.csv": "c", "sample_history.csv": "b"}) == ["history_full.csv"]
    assert legacy_model.stale_sources(info, {}) == []


def test_git_blob_sha_matches_git(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes(b"a,b\n1,2\n")
    import subprocess

    out = subprocess.run(["git", "hash-object", str(p)], capture_output=True, text=True, check=True).stdout.strip()
    assert legacy_model.git_blob_sha(p) == out


def test_new_model_only_prediction_matches_model_only(tmp_path, monkeypatch):
    """今までのモデルが読めないときの予想は、新しいモデルが使えるレースでは
    model_only_prediction の final・買い目と同じ（今までのモデルとの比較だけが無い）。"""
    full = _synthetic(tmp_path)
    monkeypatch.setenv("BOATRACE_DATA_DIR", str(tmp_path))
    model = prediction.train(pd.read_csv(tmp_path / "sample_history.csv"))
    for rk in list(full["race_key"].drop_duplicates().iloc[[3, 90, 180, 240, 255]]):
        work = _race(full, rk)
        # 新しいモデルの予想の代わり（今までのモデルの予想に印を付けたもの）
        fake = prediction.predict(model, work, display_weight=0.5).copy()
        fake["model_version"] = "ml-test"
        info = {"prediction_model": "ml-test", "model_used": "ml-test"}
        monkeypatch.setattr(ml_live, "production_final", lambda *a, _f=fake, _i=info: (_f.copy(), dict(_i)))
        a = model_only_prediction(model, work, None, PARAMS, {"prediction_model": "ml-test"}, "20260901", "01", 1)
        b = legacy_model.new_model_only_prediction(work, None, PARAMS, {"prediction_model": "ml-test"},
                                                   "20260901", "01", 1)
        pdt.assert_frame_equal(a["final"], b["final"], check_exact=True)
        pdt.assert_frame_equal(a["tickets"], b["tickets"], check_exact=True)
        assert a["hedge_lane"] == b["hedge_lane"]
        assert a["ticket_plan"] == b["ticket_plan"]
        assert a["risk_reasons"] == b["risk_reasons"]
        assert b["research_variants"] == {} and b["before"]["p_first"].isna().all()


def test_new_model_only_prediction_raises_when_new_model_unusable(tmp_path, monkeypatch):
    full = _synthetic(tmp_path, races=150)
    work = _race(full, full["race_key"].iloc[0])
    monkeypatch.setattr(ml_live, "production_final",
                        lambda *a: (None, {"fallback_reason": "集計表が古い"}))
    with pytest.raises(legacy_model.LegacyModelUnavailable, match="集計表が古い"):
        legacy_model.new_model_only_prediction(work, None, PARAMS, {}, "20260901", "01", 1)
