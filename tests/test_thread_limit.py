"""
アプリは計算ライブラリのスレッドを1本にする（app.py の先頭）。スレッドの数で予想・買い目が
変わらないことを確かめる（今までのモデルの予想と、新しいモデルが使う LightGBM の予想）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pandas.testing as pdt
from threadpoolctl import threadpool_limits

import prediction
from model_only import model_only_prediction
from test_legacy_model import LEGACY_SETTING, PARAMS, _race, _synthetic


def _run_all(model, full, races):
    out = []
    for rk in races:
        out.append(model_only_prediction(model, _race(full, rk), None, PARAMS, LEGACY_SETTING, "20260901", "01", 1))
    return out


def test_legacy_prediction_same_with_one_thread(tmp_path, monkeypatch):
    full = _synthetic(tmp_path)
    monkeypatch.setenv("BOATRACE_DATA_DIR", str(tmp_path))
    model = prediction.train(pd.read_csv(tmp_path / "sample_history.csv"))
    races = list(full["race_key"].drop_duplicates().iloc[[1, 40, 99, 180, 250]])
    many = _run_all(model, full, races)
    with threadpool_limits(limits=1):
        one = _run_all(model, full, races)
    for a, b in zip(many, one):
        for k in ("final", "tickets", "before"):
            pdt.assert_frame_equal(a[k], b[k], check_exact=True)
        for name in a["research_variants"]:
            pdt.assert_frame_equal(a["research_variants"][name], b["research_variants"][name], check_exact=True)


def test_lightgbm_predict_same_with_one_thread():
    import lightgbm as lgb

    rng = np.random.default_rng(3)
    x = rng.normal(size=(3000, 12))
    y = (x[:, 0] + rng.normal(size=3000) > 0).astype(int)
    booster = lgb.train({"objective": "binary", "verbose": -1, "seed": 1, "deterministic": True},
                        lgb.Dataset(x, y), num_boost_round=60)
    q = rng.normal(size=(6, 12))
    many = [booster.predict(q[i:i + 1], raw_score=True) for i in range(6)]
    with threadpool_limits(limits=1):
        one = [booster.predict(q[i:i + 1], raw_score=True) for i in range(6)]
    for a, b in zip(many, one):
        np.testing.assert_array_equal(a, b)
