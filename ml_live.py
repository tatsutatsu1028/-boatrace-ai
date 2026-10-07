"""
新しい予想モデル（ml_model.ChainModel）を本番の1レースで使うための入口。

切り替えるまで本番は今のモデル（prediction.predict）のまま。使うモデルは
selected_model()（app_settings の prediction_model、無ければ環境変数 PREDICTION_MODEL）で決め、
既定は "current"（今のモデル）。"ml" なら models/latest.json が指す最新の版、
"ml-chain-..." のように版を書けばその版に固定する。"current" に戻せばいつでも今のモデルに戻る。

アプリは学習をしない。毎晩・毎週の処理（ml_nightly.py）がデータ用リポジトリに置く
  models/<版>.joblib      学習済みモデル（約1〜3MB）
  models/latest.json      最新の版の名前
  models/ml_state.pkl.gz  前日までの集計表（約1.2MB。選手の調子・コース別・場別・モーター）
を読み、出走表・直前情報（history_pages.collect_race）と当日の番組表（Bファイル）から
学習と同じ特徴量を作って予想する（ml_features.live_table。学習時の値と一致することを確認済み）。
"""

from __future__ import annotations

import gzip
import json
import os
import pickle
from functools import lru_cache

import pandas as pd

import ml_features as mf
import ml_model as mm
from data_paths import data_path

CURRENT = "current"
MODEL_DIR = "models"
STATE_FILE = f"{MODEL_DIR}/ml_state.pkl.gz"
LATEST_FILE = f"{MODEL_DIR}/latest.json"
# アプリが起動時にデータ用リポジトリから取るファイル（data_paths.sync_from_github に足す）
APP_FILES = (LATEST_FILE, STATE_FILE)


def selected_model(settings=None):
    """使う予想モデル: "current"（今のモデル）か、新しいモデルの版の名前。"""
    value = ""
    if settings:
        value = str(settings.get("prediction_model") or "").strip()
    value = value or os.environ.get("PREDICTION_MODEL", "").strip() or CURRENT
    if value == "ml":
        try:
            value = json.loads(data_path(LATEST_FILE).read_text(encoding="utf-8"))["version"]
        except Exception:  # noqa: BLE001  最新の版が読めなければ今のモデルのまま
            return CURRENT
    return value


def model_file(version):
    return f"{MODEL_DIR}/{version}.joblib"


@lru_cache(maxsize=4)
def load_model(version):
    return mm.ChainModel.load(data_path(model_file(version)))


def load_state(path=None):
    with gzip.open(path or data_path(STATE_FILE), "rb") as fh:
        return pickle.load(fh)


@lru_cache(maxsize=4)
def _programs(date_yyyymmdd):
    from official_download import download_text, parse_b

    text = download_text("B", date_yyyymmdd)
    if text is None:
        return pd.DataFrame(columns=["race_key", "lane"])
    b = parse_b(text, date_yyyymmdd)
    b["racer_id"] = b["racer_id"].astype(str)
    return b


def race_features(date_yyyymmdd, jcd, rno, state, current_meet=None, pages=None, programs=None):
    """1レース分の特徴量（ml_features と同じ列）。選手の読めない艇は除く。"""
    from history_pages import collect_race

    jcd = str(jcd).zfill(2)
    if pages is None:
        pages = collect_race(str(date_yyyymmdd), jcd, int(rno), with_racelist=True)
    if programs is None:
        programs = _programs(str(date_yyyymmdd))
    key = f"{date_yyyymmdd}_{jcd}_{int(rno)}"
    programs = programs[programs["race_key"].astype(str) == key]
    pages = pages[pages["racer_id"].notna()]
    if as_of := state.get("as_of"):
        if pd.Timestamp(as_of) != pd.Timestamp(str(date_yyyymmdd)):
            print(f"[ML_LIVE] 集計表の日付 {pd.Timestamp(as_of):%Y-%m-%d} と予想日 {date_yyyymmdd} が違う",
                  flush=True)
    return mf.live_table(pages, programs, state, current_meet), pages


def predict_race(version, date_yyyymmdd, jcd, rno, current_meet=None, state=None, race=None):
    """prediction.predict と同じ形の final を返す（買い目・資金配分・画面はそのまま使える）。"""
    model = load_model(version)
    feats, pages = race_features(date_yyyymmdd, jcd, rno, state or load_state(), current_meet)
    final = mm.to_final(model, feats, race=race if race is not None else pages)
    # 今のモデルと同じく、検証保存用に今節成績などの入力も final に持ち出す
    if race is not None:
        from prediction import LANE_CONTEXT_COLUMNS

        lanes = pd.to_numeric(race["lane"], errors="coerce")
        for c in LANE_CONTEXT_COLUMNS:
            if c in race.columns:
                final[c] = final["lane"].map(dict(zip(lanes, race[c])))
    return final
