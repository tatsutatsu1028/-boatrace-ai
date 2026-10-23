"""
「1号艇が負けた場合の着順」を予想する専用モデル（検証中。本番の予想・買い目・画面には使っていない）。

今の新モデル（ml_model.ChainModel。全レースで学習）は、1号艇が負けるレースで「誰が勝つか」の読みが弱い。
そこで、過去2年分のうち1号艇が1着でなかったレースだけで、同じ作りの3段のモデルを学習する。

  1着: P(艇a が1着 | 1号艇が負け)            … 2〜6号艇の中の割合（1号艇の行は使わない）
  2着: P(艇b が2着 | 1着が艇a)               … 1号艇も候補に入れる（1着が1号艇以外のときだけ）
  3着: P(艇c が3着 | 1着が艇a・2着が艇b)

特徴量は今の新モデルと同じ（展示・直前情報を含む。ml_features.FEATURES と艇番を除いた実力の列）。
展示前の版は ml_pre と同じ列（展示・直前情報を使わない）。艇番を除いた実力のモデルは全レースで学習する
（「艇番に関係ない強さ」なので、1号艇が負けたレースに絞らない）。

3連単の確率（combine）: 1号艇1着の組は今のモデルのまま、1号艇以外が1着の組だけ
  P(a-b-c) = （今のモデルの1号艇が負ける確率）× 専用モデルの P(a) × P(b|a) × P(c|a,b)
に置き換える。120通りの合計は1のまま。

学習済みファイル（データ用リポジトリ。本番のファイルとは別の名前）:
  models/ml-upset-v1-lgb-2y-<学習日>.joblib  / models/upset_latest.json      （展示後の版）
  models/ml-upset-pre-v1-lgb-2y-<学習日>.joblib / models/upset_pre_latest.json （展示前の版）
本番の models/latest.json・pre_latest.json・ml_state.pkl.gz・ml_nightly.py には触れない。

  python ml_upset.py train [--as-of YYYYMMDD]   展示後・展示前の2つを学習（学習は直近28日の前まで、温度は直近28日）
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import ml_features as mf
import ml_model as mm
import ml_pre
from data_paths import data_path

JST = ZoneInfo("Asia/Tokyo")
UPSET_FAMILY = "ml-upset"
UPSET_PRE_FAMILY = "ml-upset-pre"
MODEL_SET = "v1"
LATEST_FILE = "models/upset_latest.json"
PRE_LATEST_FILE = "models/upset_pre_latest.json"
HISTORY_START = "20240927"   # 今の新モデルと同じ（2年分）
CALIB_DAYS = 28


def upset_races(f):
    """1号艇が出走していて1着でなかったレース（1着がちょうど1艇）の行だけ。"""
    fin = pd.to_numeric(f["finish"], errors="coerce")
    g = f.assign(_w=(fin == 1), _l1=(f["lane"] == 1), _l1w=(fin == 1) & (f["lane"] == 1)).groupby("race_key")
    ok = (g["_w"].transform("sum") == 1) & g["_l1"].transform("any") & ~g["_l1w"].transform("any")
    return f[ok.to_numpy()]


class UpsetModel(mm.ChainModel):
    """ml_model.ChainModel と同じ3段。1着の段は2〜6号艇だけ、学習は1号艇が負けたレースだけ。"""

    def fit(self, f, calib=None, seed=42, log=print):
        t0 = time.time()
        f = f[f["finish"].notna() | f["lane"].notna()]
        if self.lane_free:
            f = self._fit_lane_free(f, log=log)   # 全レースで学習（艇番に関係ない強さ）
            log(f"[UPSET] 艇番なしモデル {time.time() - t0:.0f}秒")
        u = upset_races(f)
        rows = {1: u[u["lane"] != 1], 2: mm.second_rows(u), 3: mm.third_rows(u)}
        for stage in (1, 2, 3):
            cols = mm.stage_cols(self.features, stage)
            x = rows[stage]
            y = (x["finish"] == stage).astype(int)
            m = mm._fit_lgb_softmax(mm._prep(x, cols), y, mm._stage_groups(x, stage), x["race_date"], log=log)
            self.models[stage] = m
            self.info[f"stage{stage}_rows"] = int(len(x))
            self.info[f"stage{stage}_iter"] = int(m.n_iter_)
            log(f"[UPSET] {stage}着モデル {len(x)}行 {m.n_iter_}回 {time.time() - t0:.0f}秒")
        if calib is not None and len(calib):
            self.calibrate(calib, log=log)
        self.info["train_races"] = int(u["race_key"].nunique())
        self.info["train_all_races"] = int(f["race_key"].nunique())
        self.info["train_from"] = str(f["race_date"].min())
        self.info["train_to"] = str(f["race_date"].max())
        return self

    def calibrate(self, c, log=print):
        c = upset_races(self.with_lane_free(c))
        rows = {1: c[c["lane"] != 1], 2: mm.second_rows(c), 3: mm.third_rows(c)}
        for stage in (1, 2, 3):
            x = rows[stage]
            z = self._z(x, stage)
            self.temps[stage] = mm._fit_temperature(z, mm._stage_groups(x, stage), x["finish"] == stage)
        self.info["calib_races"] = int(c["race_key"].nunique())
        self.info["calib_from"] = str(c["race_date"].min())
        self.info["calib_to"] = str(c["race_date"].max())
        log(f"[UPSET] 温度 1着={self.temps[1]:.3f} 2着={self.temps[2]:.3f} 3着={self.temps[3]:.3f}")

    def _predict_tables_core(self, f):
        """ChainModel と同じ形の表。first は2〜6号艇の中の割合、second/third は1着が1号艇以外の仮定だけ。"""
        f = f.reset_index(drop=True)
        _, second, third = super()._predict_tables_core(f)
        x = f[f["lane"] != 1].reset_index(drop=True)
        rk = x["race_key"].to_numpy()
        first = pd.DataFrame({"race_key": rk, "lane": x["lane"].to_numpy(),
                              "p_first": mm._group_softmax(self._z(x, 1), rk, self.temps[1])})
        return (first, second[second["w_lane"] != 1].reset_index(drop=True),
                third[third["w_lane"] != 1].reset_index(drop=True))


def new_model(version):
    return UpsetModel(mf.FEATURES + mm.LANE_FREE_DERIVED, version, engine="lgb", lane_free=mf.LANE_FREE_FEATURES)


def new_pre_model(version):
    return UpsetModel(ml_pre.PRE_FEATURES, version, engine="lgb", lane_free=ml_pre.PRE_LANE_FREE)


# ---------------------------------------------------------------
# 3連単の確率（今のモデルと合わせる）
# ---------------------------------------------------------------
def upset_tri(model, feats):
    """専用モデルの3連単 {race_key: {組番: P(組 | 1号艇が負け)}}（1着が1号艇以外の組だけ。合計1）。"""
    tri = mm.trifecta_table(*model.predict_tables(feats))
    return {rk: dict(zip(g["combo"].astype(str), g["prob"].astype(float))) for rk, g in tri.groupby("race_key")}


def lane1_win(base_tri):
    """今のモデルの3連単から、1号艇が1着になる確率。"""
    return float(sum(float(p) for c, p in (base_tri or {}).items() if str(c)[:1] == "1"))


def combine(base_tri, cond_tri, digits=6):
    """1号艇1着の組は今のモデルのまま、それ以外を（1 − 1号艇の1着確率）× 専用モデルの確率に置き換える。"""
    base = {str(c): float(p) for c, p in (base_tri or {}).items()}
    total = sum(base.values())
    if total <= 0 or not cond_tri:
        return None
    base = {c: p / total for c, p in base.items()}
    p1 = lane1_win(base)
    s = sum(float(p) for p in cond_tri.values())
    out = {c: p for c, p in base.items() if c[:1] == "1"}
    for c, p in cond_tri.items():
        out[str(c)] = (1.0 - p1) * float(p) / s
    return {c: round(p, digits) for c, p in out.items()}


def upset_first(cond_tri):
    """専用モデルの P(艇a が1着 | 1号艇が負け) {艇番: 確率}（確率の高い順）。"""
    out = {}
    for c, p in (cond_tri or {}).items():
        a = int(str(c)[0])
        out[a] = out.get(a, 0.0) + float(p)
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def upset_summary(cond_tri, base_tri=None):
    """画面・保存用の要約: 荒れたときの本命と2番手（1号艇が負けたときの1着確率）、1号艇が負ける確率。"""
    first = upset_first(cond_tri)
    top = list(first.items())[:2]
    out = {"first": {str(k): round(v, 5) for k, v in first.items()},
           "top1": top[0][0] if top else None, "top1_prob": round(top[0][1], 5) if top else None,
           "top2": top[1][0] if len(top) > 1 else None, "top2_prob": round(top[1][1], 5) if len(top) > 1 else None}
    if base_tri:
        out["p_lane1_lose"] = round(1.0 - lane1_win(base_tri), 5)
    return out


# ---------------------------------------------------------------
# 版
# ---------------------------------------------------------------
def model_file(version):
    return f"models/{version}.joblib"


def versions(pre=False):
    """データ用リポジトリ models/ にある専用モデルの版 {版名: 確率の調整に使った最後の日}。"""
    prefix = UPSET_PRE_FAMILY if pre else UPSET_FAMILY
    out = {}
    for p in sorted(data_path("models").glob(f"{prefix}-*.json")):
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
            v = str(info["version"])
            if not pre and v.startswith(UPSET_PRE_FAMILY):
                continue
            out[v] = str(info["info"].get("calib_to") or info["info"].get("train_to"))
        except Exception:  # noqa: BLE001
            continue
    return out


def version_for(day, vers):
    """その日より前のデータだけで学習・調整した版のうち、いちばん新しいもの（無ければ None）。"""
    ok = [(last, v) for v, last in vers.items() if last < str(day)]
    return max(ok)[1] if ok else None


def load(version):
    return UpsetModel.load(data_path(model_file(version)))


# ---------------------------------------------------------------
# 学習
# ---------------------------------------------------------------
def _split(f, as_of):
    f = f[f["race_date"] < str(as_of)]
    calib_start = (pd.Timestamp(as_of) - pd.Timedelta(days=CALIB_DAYS)).strftime("%Y%m%d")
    return f[f["race_date"] < calib_start], f[f["race_date"] >= calib_start]


def _fit_and_save(model, train_f, calib_f, latest_name, t0, label):
    model.fit(train_f, calib_f, log=lambda *a: None)
    print(f"[UPSET] {label}学習 1号艇が負けた {model.info['train_races']}レース"
          f"（{model.info['train_from']}〜{model.info['train_to']}）調整 {model.info.get('calib_races')}レース"
          f" {time.time() - t0:.0f}秒", flush=True)
    model.save(data_path(model_file(model.version)))
    data_path(latest_name).write_text(json.dumps({"version": model.version,
                                                  "trained_at": datetime.now(JST).isoformat()}, ensure_ascii=False))
    print(f"[UPSET] {label}版 {model.version}", flush=True)


def train(as_of=None, post=True, pre=True):
    as_of = as_of or datetime.now(JST).strftime("%Y%m%d")
    t0 = time.time()
    k, b, p = mf.load_sources(HISTORY_START)
    if post:
        tr, ca = _split(mf.build_table(k, b, p), as_of)
        _fit_and_save(new_model(f"{UPSET_FAMILY}-{MODEL_SET}-lgb-2y-{as_of}"), tr, ca, LATEST_FILE, t0, "")
    if pre:
        tr, ca = _split(ml_pre.build_pre_table(k, b, p), as_of)
        _fit_and_save(new_pre_model(f"{UPSET_PRE_FAMILY}-{MODEL_SET}-lgb-2y-{as_of}"), tr, ca, PRE_LATEST_FILE, t0,
                      "展示前 ")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["train"])
    ap.add_argument("--as-of", default="")
    ap.add_argument("--only", choices=["post", "pre", ""], default="")
    a = ap.parse_args()
    train(a.as_of or None, post=a.only != "pre", pre=a.only != "post")
