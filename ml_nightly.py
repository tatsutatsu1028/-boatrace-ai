"""
新しい予想モデルの定期処理（GitHub Actions の ml_model.yml から動かす）。

  python ml_nightly.py state   毎晩: 前日までの集計表 models/ml_state.pkl.gz を作り直す（数十秒）
  python ml_nightly.py train   毎週: 2年分（出走表・直前情報の無い期間は欠損）で学習し直し、
                               models/<版>.joblib と models/latest.json を書く（数分）。
                               続けて展示前モデル（ml_pre.py）も同じ期間で学習し直し、
                               models/<展示前の版>.joblib と models/pre_latest.json を書く
  python ml_nightly.py train_pre  展示前モデルだけ学習し直す（手動用）

学習は直近28日を確率の調整（温度）に使い、その前の日までで学習する。
学習を終えたら直近28日で、新しいモデルの LogLoss が前の版より大きく悪くなっていないかを確かめ、
悪くなっていれば latest.json を書き換えない（前の版のまま）。

アプリは学習をせず、これらのファイルを読むだけ（ml_live.py）。
ログは誰でも見られるので、件数・日付・所要時間・合否だけを出す（確率や成績の数字は出さない）。
"""

from __future__ import annotations

import argparse
import gzip
import json
import pickle
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import ml_features as mf
import ml_model as mm
import ml_pre
from data_paths import data_path
from ml_live import LATEST_FILE, STATE_FILE, model_file

JST = ZoneInfo("Asia/Tokyo")
CALIB_DAYS = 28
# 特徴量の版（v2: 特徴量の表と照合して足した列と、艇番を除いた実力のモデルの列を含む）
MODEL_SET = "v2"
HISTORY_START = "20240927"
# 新しい版の LogLoss が前の版よりこれ以上悪ければ入れ替えない
MAX_LOGLOSS_WORSE = 0.01


def _today():
    return datetime.now(JST).strftime("%Y%m%d")


def _ymd(ts):
    return pd.Timestamp(ts).strftime("%Y%m%d")


def _k_with_recent(start, as_of):
    """データ用リポジトリの競走成績に、まだ入っていない直近の日を公式ファイルから足す（保存はしない）。"""
    k = mf.store.read_kind("k_results", start)
    have = set(k["race_date"].astype(str))
    extra = []
    day = pd.Timestamp(as_of) - pd.Timedelta(days=10)
    while day < pd.Timestamp(as_of):
        d = _ymd(day)
        if d not in have:
            try:
                from official_download import download_text, parse_k

                text = download_text("K", d)
                if text is not None:
                    boats, _ = parse_k(text, d)
                    extra.append(boats.astype(str))
                    print(f"[ML] K {d}: 公式から補う", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[ML] K {d}: 取得失敗 {type(e).__name__}", flush=True)
        day += pd.Timedelta(days=1)
    if extra:
        k = pd.concat([k] + extra, ignore_index=True)
    k["race_key"] = k["race_key"].astype(str)
    k["lane"] = pd.to_numeric(k["lane"], errors="coerce").astype("Int64")
    k["racer_id"] = k["racer_id"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    return k


def build_state(as_of=None):
    as_of = as_of or _today()
    t0 = time.time()
    # 学習と同じ期間から集計する（当地の出走経験数 rv_n_all は「集計できる全期間」の数なので、
    # 期間を短くすると学習時の値と合わなくなる）
    k = _k_with_recent(HISTORY_START, as_of)
    k = k[k["race_date"].astype(str) < str(as_of)]
    state = mf.compact_state(mf.build_state(mf._events(k)), as_of)
    path = data_path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as fh:
        pickle.dump(state, fh)
    print(f"[ML] 集計表 {as_of} 用（{k['race_date'].min()}〜{k['race_date'].max()}）"
          f" {time.time() - t0:.0f}秒", flush=True)


def _group_logloss(model, f):
    first = model.predict_tables(f)[0].merge(f[["race_key", "lane", "finish"]], on=["race_key", "lane"])
    ok = first.groupby("race_key")["finish"].transform(lambda s: (s == 1).sum() == 1)
    first = first[ok]
    return float(-np.log(np.clip(first.loc[first["finish"] == 1, "p_first"], 1e-12, None)).mean())


def _fit_and_publish(model, train_f, calib_f, latest_name, t0, label):
    """学習し、前の版と同じ直近28日で比べて、大きく悪くなっていなければ latest を書き換える。"""
    model.fit(train_f, calib_f, log=lambda *a: None)
    version = model.version
    print(f"[ML] {label}学習 {model.info['train_races']}レース（{model.info['train_from']}〜{model.info['train_to']}）"
          f" 調整 {model.info.get('calib_races')}レース {time.time() - t0:.0f}秒", flush=True)
    latest = data_path(latest_name)
    replace = True
    if latest.exists():
        prev = json.loads(latest.read_text(encoding="utf-8"))["version"]
        try:
            prev_model = mm.ChainModel.load(data_path(model_file(prev)))
            new_ll, old_ll = _group_logloss(model, calib_f), _group_logloss(prev_model, calib_f)
            replace = new_ll <= old_ll + MAX_LOGLOSS_WORSE
            model.info["check_vs"] = prev
            model.info["check_logloss_new"] = new_ll
            model.info["check_logloss_prev"] = old_ll
        except Exception as e:  # noqa: BLE001
            print(f"[ML] {label}前の版を読めないため比較なし: {type(e).__name__}", flush=True)
    model.save(data_path(model_file(version)))
    if replace:
        latest.write_text(json.dumps({"version": version, "trained_at": datetime.now(JST).isoformat()},
                                     ensure_ascii=False))
    print(f"[ML] {label}版 {version} {'を最新にした' if replace else 'は前の版より悪いため最新にしない'}", flush=True)


def _split(f, as_of):
    f = f[f["race_date"] < str(as_of)]
    calib_start = _ymd(pd.Timestamp(as_of) - pd.Timedelta(days=CALIB_DAYS))
    return f[f["race_date"] < calib_start], f[f["race_date"] >= calib_start]


def train(as_of=None, main=True, pre=True):
    as_of = as_of or _today()
    t0 = time.time()
    k, b, p = mf.load_sources(HISTORY_START)
    if main:
        train_f, calib_f = _split(mf.build_table(k, b, p), as_of)
        version = f"{mm.MODEL_FAMILY}-{MODEL_SET}-lgb-2y-{as_of}"
        model = mm.ChainModel(mf.FEATURES + mm.LANE_FREE_DERIVED, version, engine="lgb",
                              lane_free=mf.LANE_FREE_FEATURES)
        _fit_and_publish(model, train_f, calib_f, LATEST_FILE, t0, "")
    if pre:
        # 展示前モデル（展示・直前情報を使わない）。「🌅 展示前厳選10」（pre_picks.py）が使う
        train_f, calib_f = _split(ml_pre.build_pre_table(k, b, p), as_of)
        version = f"{ml_pre.PRE_FAMILY}-{MODEL_SET}-lgb-2y-{as_of}"
        _fit_and_publish(ml_pre.new_model(version), train_f, calib_f, ml_pre.PRE_LATEST_FILE, t0, "展示前モデル ")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["state", "train", "train_pre"])
    ap.add_argument("--as-of", default="")
    a = ap.parse_args()
    if a.task == "state":
        build_state(a.as_of or None)
    elif a.task == "train_pre":
        train(a.as_of or None, main=False)
    else:
        train(a.as_of or None)
