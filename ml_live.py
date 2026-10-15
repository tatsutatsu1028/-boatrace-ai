"""
新しい予想モデル（ml_model.ChainModel）を本番の1レースで使うための入口。

使うモデルは selected_model()（app_settings の prediction_model、無ければ環境変数 PREDICTION_MODEL）で決める。
"ml" なら models/latest.json が指す最新の版、"ml-chain-..." のように版を書けばその版に固定する。
"current" に戻せばいつでも今のモデル（prediction.predict）に戻る（今のモデルのコードは消していない）。
アプリ（app_core）と自動固定（auto_random_fix）は production_final() を通して予想する。
新しいモデルで予想できなかったレース（集計表が古い・出走表が読めない など）は今のモデルで予想し、
保存する予想に「どちらのモデルで出したか」と理由を残す。

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
# アプリが起動時にデータ用リポジトリから取るファイル（data_paths.sync_from_github に足す）。
# 学習済みモデル本体は latest.json を読んでから app_model_files() で取る。
APP_FILES = (LATEST_FILE, STATE_FILE)
# 集計表（前日まで）がこれより古ければ新しいモデルを使わない（毎朝作り直すので普段は0〜1日）
MAX_STATE_AGE_DAYS = 3


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


def app_model_files(setting_value=None):
    """アプリが取る学習済みモデルのファイル（最新の版と、版を固定していればその版）。"""
    names = []
    try:
        names.append(json.loads(data_path(LATEST_FILE).read_text(encoding="utf-8"))["version"])
    except Exception:  # noqa: BLE001
        pass
    value = str(setting_value or "").strip()
    if value.startswith(mm.MODEL_FAMILY):
        names.append(value)
    return tuple(dict.fromkeys(model_file(v) for v in names))


@lru_cache(maxsize=4)
def load_model(version):
    return mm.ChainModel.load(data_path(model_file(version)))


def load_state(path=None):
    """集計表を読み、文字列の列をこの環境の型にそろえる（Actions で作った pickle を本番で読むため）。"""
    with gzip.open(path or data_path(STATE_FILE), "rb") as fh:
        return mf.unify_state(pickle.load(fh))


_STATE_CACHE = {}


def _cached_state():
    """集計表（約1.2MB）。ファイルが入れ替わったら読み直す。"""
    path = data_path(STATE_FILE)
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _STATE_CACHE.get("key") != key:
        _STATE_CACHE.update(key=key, state=load_state(path))
    return _STATE_CACHE["state"]


_PROGRAMS = {}


def _programs(date_yyyymmdd):
    """その日の番組表（Bファイル）。取れた日だけ覚える（取れなかった日は次の予想で取り直す）。"""
    from official_download import download_text, parse_b

    d = str(date_yyyymmdd)
    if d in _PROGRAMS:
        return _PROGRAMS[d]
    text = download_text("B", d)
    if text is None:
        return pd.DataFrame(columns=["race_key", "jcd", "lane"])
    b = parse_b(text, d)
    b["racer_id"] = b["racer_id"].astype(str)
    if len(_PROGRAMS) >= 4:
        _PROGRAMS.pop(next(iter(_PROGRAMS)))
    _PROGRAMS[d] = b
    return b


def race_features(date_yyyymmdd, jcd, rno, state, current_meet=None, pages=None, programs=None):
    """1レース分の特徴量（ml_features と同じ列）。選手の読めない艇は除く。"""
    from history_pages import collect_race

    jcd = str(jcd).zfill(2)
    if pages is None:
        pages = collect_race(str(date_yyyymmdd), jcd, int(rno), with_racelist=True)
    if programs is None:
        programs = _programs(str(date_yyyymmdd))
    # ナイターの判定にその場のその日の全レースの締切が要るので、場で絞るだけにする
    programs = programs[programs["jcd"].astype(str).str.zfill(2) == jcd]
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


def _check_state_date(state, date_yyyymmdd):
    """集計表がそのレースの予想に使えるか。使えなければ理由を返す。

    集計表は「基準日の前日まで」の結果。予想日より後の基準日（= 過去のレース）は
    そのレース自身の結果が混ざるので使わない。古すぎる集計表も使わない。
    """
    as_of = state.get("as_of")
    if not as_of:
        return "集計表の基準日が読めない"
    gap = (pd.Timestamp(str(date_yyyymmdd)) - pd.Timestamp(as_of)).days
    if gap < 0:
        return f"過去のレース（集計表 {pd.Timestamp(as_of):%Y-%m-%d} 時点の結果を含むため）"
    if gap > MAX_STATE_AGE_DAYS:
        return f"集計表が古い（{pd.Timestamp(as_of):%Y-%m-%d} 用）"
    return None


def production_final(settings, race, date_yyyymmdd, jcd, rno):
    """本番の予想（アプリ・自動固定）で使う新しいモデルの final と、使ったモデルの記録。

    戻り値 (final, info)。final が None なら今のモデル（prediction.predict）で予想する。
    info は保存する予想に残す: prediction_model（設定値）・model_used（使ったモデル）・
    fallback_reason（新しいモデルを選んでいたのに使えなかった理由）。
    """
    setting = str((settings or {}).get("prediction_model") or os.environ.get("PREDICTION_MODEL", "")
                  or CURRENT).strip()
    info = {"prediction_model": setting, "model_used": CURRENT}
    version = selected_model(settings)
    if version == CURRENT:
        if setting != CURRENT:
            info["fallback_reason"] = "新しいモデルの最新の版（models/latest.json）が読めない"
            print(f"[ML_LIVE] 今のモデルで予想: {info['fallback_reason']}", flush=True)
        return None, info
    try:
        state = _cached_state()
        reason = _check_state_date(state, date_yyyymmdd)
        if reason:
            raise RuntimeError(reason)
        race_key = f"{date_yyyymmdd}_{str(jcd).zfill(2)}_{int(rno)}"
        if not (_programs(str(date_yyyymmdd))["race_key"].astype(str) == race_key).any():
            raise RuntimeError("番組表（Bファイル）にこのレースが無い")
        meet_cols = ["lane"] + [c for c in mf._LIVE_MEET if c in race.columns]
        current_meet = race[meet_cols] if len(meet_cols) > 1 else None
        final = predict_race(version, date_yyyymmdd, jcd, rno, current_meet=current_meet,
                             state=state, race=race)
        p1 = pd.to_numeric(final["p_first"], errors="coerce")
        if len(final) < 2 or p1.isna().any() or abs(float(p1.sum()) - 1.0) > 1e-3:
            raise RuntimeError(f"予想の形が不正（{len(final)}艇・1着確率の合計 {float(p1.sum()):.3f}）")
    except Exception as e:  # noqa: BLE001  新しいモデルで出せなければ今のモデルで予想する
        info["fallback_reason"] = f"{type(e).__name__}: {e}"[:300]
        print(f"[ML_LIVE] {date_yyyymmdd}_{str(jcd).zfill(2)}_{int(rno)} 今のモデルで予想: "
            f"{info['fallback_reason']}", flush=True)
        return None, info
    info["model_used"] = version
    print(f"[ML_LIVE] {date_yyyymmdd}_{str(jcd).zfill(2)}_{int(rno)} 新しいモデル {version} で予想", flush=True)
    return final, info
