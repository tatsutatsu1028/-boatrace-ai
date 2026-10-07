"""
新しい予想モデル（1着・2着・3着の確率を、情報をまとめて学習する）。

今のモデル（prediction.predict）は、基礎データだけで学習した1着確率に、今節・コース・気象などの
補正を後から人が決めた重みで足している。こちらは ml_features.py の特徴量を全部入れて
1着・2着・3着を直接学習し、後から足す補正は使わない。

  1着: P(艇i が1着)                    … 艇ごとの勝ちやすさをレースの中で割合にする
  2着: P(艇j が2着 | 1着が艇a)         … 1着艇の艇番・強さと、候補艇の関係も特徴量にする
  3着: P(艇k が3着 | 1着が艇a, 2着が艇b)
  3連単 a-b-c = 1着(a) × 2着(b|a) × 3着(c|a,b)  （掛け算だけ。後から足す補正は無い）

確率の調整（温度）:
  各段の「レースの中で割合にする」ときの鋭さ（温度）だけを、学習に使っていない直近の期間で
  最も当てはまるように1つずつ決める（人が重みを決める補正ではなく、データから求める）。

出力は prediction.predict と同じ形（p_first・p_second・p_third・p_second_given_<a>・
p_third_given_<a>_<b>）なので、買い目の作り方（adaptive_ticket_plan・rank_tickets）・
資金配分・画面はそのまま使える。3連単は prediction.trifecta がこのモデルの版を見て
掛け算だけで計算する（trifecta_exact）。
"""

from __future__ import annotations

import itertools
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from sklearn.ensemble import HistGradientBoostingClassifier

import ml_features as mf

MODEL_FAMILY = "ml-chain"
MODEL_FORMAT = 1

# 2着・3着の段で、すでに着順が決まった艇（1着艇・2着艇）から持ち込む項目。
_PLACED_COLS = ["lane", "exp_course", "national_win_rate", "r365_win", "rc_win", "exhibition_time",
                "exhibition_st", "avg_st", "class_ord"]


_CATS = [float(i) for i in range(0, 26)]


def _hgb(seed=42):
    return HistGradientBoostingClassifier(
        max_iter=600,
        learning_rate=0.05,
        max_leaf_nodes=63,
        min_samples_leaf=80,
        l2_regularization=1.0,
        early_stopping=True,
        validation_fraction=0.1,
        n_iter_no_change=30,
        categorical_features="from_dtype",
        random_state=seed,
    )


# ---------------------------------------------------------------
# レース単位の多項ロジット（LightGBM の自作の目的関数）
# ---------------------------------------------------------------
# 「6艇のうちどの艇が1着か」をレースの中の割合（softmax）として直接学習する。
# 1艇ずつ「勝つ/勝たない」で学習してから後で割合にするより、確率の当てはまりが良い。
LGB_PARAMS = {
    "learning_rate": 0.04, "num_leaves": 63, "min_data_in_leaf": 200, "feature_fraction": 0.8,
    "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 2.0, "max_bin": 255,
    "verbosity": -1, "seed": 42,
}


def _group_layout(groups):
    """groups（並び替え済みで同じ値が連続）の各グループの先頭位置と大きさ。"""
    g = np.asarray(groups)
    change = np.r_[True, g[1:] != g[:-1]]
    starts = np.flatnonzero(change)
    sizes = np.diff(np.r_[starts, len(g)])
    return starts, sizes


def _softmax_layout(s, starts, sizes):
    m = np.repeat(np.maximum.reduceat(s, starts), sizes)
    e = np.exp(s - m)
    return e / np.repeat(np.add.reduceat(e, starts), sizes)


def _softmax_objective(starts, sizes):
    def obj(preds, ds):
        y = ds.get_label()
        p = _softmax_layout(preds, starts, sizes)
        return p - y, np.maximum(p * (1.0 - p), 1e-6)
    return obj


def _softmax_eval(starts, sizes):
    def ev(preds, ds):
        y = ds.get_label().astype(bool)
        p = _softmax_layout(preds, starts, sizes)
        return "group_logloss", float(-np.log(np.clip(p[y], 1e-12, None)).mean()), False
    return ev


class _LGBSoftmax:
    """predict_raw でレース内の割合にする前の値（スコア）を返すだけの小さな包み。"""

    def __init__(self, booster):
        self.booster = booster
        self.n_iter_ = booster.best_iteration or booster.current_iteration()

    def predict_raw(self, x):
        return self.booster.predict(x, num_iteration=self.n_iter_, raw_score=True)


def _fit_lgb_softmax(x, y, groups, dates, threads=None, log=print):
    """groups ごとに正解がちょうど1つの行だけで学習する。直近8%の日付で打ち切りを決める。"""
    import lightgbm as lgb
    import os

    df = pd.DataFrame({"g": np.asarray(groups), "y": np.asarray(y), "d": np.asarray(dates)})
    pos = df.groupby("g")["y"].transform("sum")
    keep = (pos == 1).to_numpy()
    x, df = x[keep], df[keep]
    order = np.lexsort((df["g"].to_numpy(), df["d"].to_numpy()))
    x, df = x.iloc[order], df.iloc[order]
    cut = np.quantile(pd.to_numeric(df["d"]).to_numpy(), 0.92)
    va = (pd.to_numeric(df["d"]).to_numpy() > cut)
    tr = ~va
    st_tr, sz_tr = _group_layout(df["g"].to_numpy()[tr])
    st_va, sz_va = _group_layout(df["g"].to_numpy()[va])
    params = dict(LGB_PARAMS, objective=_softmax_objective(st_tr, sz_tr),
                  num_threads=int(threads or os.environ.get("OMP_NUM_THREADS") or 0))
    dtr = lgb.Dataset(x[tr], df["y"].to_numpy()[tr], free_raw_data=False)
    dva = lgb.Dataset(x[va], df["y"].to_numpy()[va], reference=dtr, free_raw_data=False)
    ev_va = _softmax_eval(st_va, sz_va)
    booster = lgb.train(
        params, dtr, num_boost_round=3000, valid_sets=[dva], valid_names=["va"],
        feval=lambda p, d: ev_va(p, d),
        callbacks=[lgb.early_stopping(80, verbose=False)],
    )
    return _LGBSoftmax(booster)


def _prep(x, cols):
    x = x[cols].copy()
    for c in cols:
        if c in mf.CAT_FEATURES:
            v = pd.to_numeric(x[c], errors="coerce")
            x[c] = pd.Categorical(v.where(v >= -1) + 1, categories=_CATS)  # -1（無風）も含めて0以上に
        else:
            x[c] = pd.to_numeric(x[c], errors="coerce").astype(float)
    return x


# ---------------------------------------------------------------
# 2着・3着の段の行（候補艇 × 仮定した上位艇）
# ---------------------------------------------------------------
def _placed_frame(f, lanes_by_race, tag):
    """各レースで「tag 着」と仮定した艇の項目を、そのレースの全艇の行に付ける。"""
    src = f[["race_key"] + _PLACED_COLS].rename(columns={c: f"{tag}_{c}" for c in _PLACED_COLS})
    want = lanes_by_race.rename(f"{tag}_lane").reset_index()
    return want.merge(src, on=["race_key", f"{tag}_lane"], how="left")


def _rel_cols(x, tag):
    x[f"{tag}_lane_gap"] = x["lane"] - x[f"{tag}_lane"]
    x[f"{tag}_course_gap"] = x["exp_course"] - x[f"{tag}_exp_course"]
    x[f"{tag}_inside"] = (x[f"{tag}_lane_gap"] < 0).astype(float)
    return x


def stage_cols(features, stage):
    cols = list(features)
    if stage >= 2:
        cols += [f"w_{c}" for c in _PLACED_COLS] + ["w_lane_gap", "w_course_gap", "w_inside"]
    if stage >= 3:
        cols += [f"s_{c}" for c in _PLACED_COLS] + ["s_lane_gap", "s_course_gap", "s_inside"]
    return cols


def second_rows(f):
    """学習用: 実際の1着艇を条件に、残りの艇（2着の候補）の行を作る。"""
    win = f[f["finish"] == 1].drop_duplicates("race_key").set_index("race_key")["lane"]
    x = f[f["race_key"].isin(win.index)].merge(_placed_frame(f, win, "w"), on="race_key")
    x = x[x["lane"] != x["w_lane"]]
    return _rel_cols(x, "w")


def third_rows(f):
    w = f[f["finish"] == 1].drop_duplicates("race_key").set_index("race_key")["lane"]
    s = f[f["finish"] == 2].drop_duplicates("race_key").set_index("race_key")["lane"]
    keys = w.index.intersection(s.index)
    x = f[f["race_key"].isin(keys)]
    x = x.merge(_placed_frame(f, w[keys], "w"), on="race_key")
    x = x.merge(_placed_frame(f, s[keys], "s"), on="race_key")
    x = x[(x["lane"] != x["w_lane"]) & (x["lane"] != x["s_lane"])]
    return _rel_cols(_rel_cols(x, "w"), "s")


# ---------------------------------------------------------------
# レースの中で割合にする（温度つき）
# ---------------------------------------------------------------
def _logit(q):
    q = np.clip(q, 1e-6, 1 - 1e-6)
    return np.log(q / (1 - q))


def _group_softmax(z, groups, t):
    s = pd.Series(z * t)
    m = s.groupby(groups).transform("max")
    e = np.exp(s - m)
    return (e / e.groupby(groups).transform("sum")).to_numpy()


def _fit_temperature(z, groups, y):
    """y=1 の艇の確率の対数尤度が最大になる温度。"""
    yb = np.asarray(y).astype(bool)

    def nll(t):
        p = _group_softmax(z, groups, t)
        return -np.log(np.clip(p[yb], 1e-12, None)).sum()

    r = minimize_scalar(nll, bounds=(0.3, 3.0), method="bounded")
    return float(r.x)


# ---------------------------------------------------------------
# 学習
# ---------------------------------------------------------------
def _stage_groups(x, stage):
    g = x["race_key"].astype(str)
    if stage >= 2:
        g = g + "_" + x["w_lane"].astype(int).astype(str)
    if stage >= 3:
        g = g + "_" + x["s_lane"].astype(int).astype(str)
    return g.to_numpy()


class ChainModel:
    def __init__(self, features, version, engine="lgb"):
        self.features = list(features)
        self.version = version
        self.engine = engine
        self.models = {}
        self.temps = {1: 1.0, 2: 1.0, 3: 1.0}
        self.info = {}

    # 学習 -------------------------------------------------------
    def fit(self, f, calib=None, seed=42, log=print):
        """f で3段を学習し、calib（学習に使っていない直近の期間）で温度を決める。"""
        t0 = time.time()
        f = f[f["finish"].notna() | f["lane"].notna()]
        rows = {1: f, 2: second_rows(f), 3: third_rows(f)}
        for stage in (1, 2, 3):
            cols = stage_cols(self.features, stage)
            x = rows[stage]
            y = (x["finish"] == stage).astype(int)
            if self.engine == "lgb":
                m = _fit_lgb_softmax(_prep(x, cols), y, _stage_groups(x, stage), x["race_date"], log=log)
            else:
                m = _hgb(seed)
                m.fit(_prep(x, cols), y)
            self.models[stage] = m
            self.info[f"stage{stage}_rows"] = int(len(x))
            self.info[f"stage{stage}_iter"] = int(m.n_iter_)
            log(f"[ML] {stage}着モデル {len(x)}行 {m.n_iter_}回 {time.time() - t0:.0f}秒")
        if calib is not None and len(calib):
            self.calibrate(calib, log=log)
        self.info["train_races"] = int(f["race_key"].nunique())
        self.info["train_from"] = str(f["race_date"].min())
        self.info["train_to"] = str(f["race_date"].max())
        return self

    def calibrate(self, c, log=print):
        rows = {1: c, 2: second_rows(c), 3: third_rows(c)}
        for stage in (1, 2, 3):
            x = rows[stage]
            z = self._z(x, stage)
            self.temps[stage] = _fit_temperature(z, x["race_key"].to_numpy(), x["finish"] == stage)
        self.info["calib_races"] = int(c["race_key"].nunique())
        self.info["calib_from"] = str(c["race_date"].min())
        self.info["calib_to"] = str(c["race_date"].max())
        log(f"[ML] 温度 1着={self.temps[1]:.3f} 2着={self.temps[2]:.3f} 3着={self.temps[3]:.3f}")

    def _z(self, x, stage):
        cols = stage_cols(self.features, stage)
        m = self.models[stage]
        if hasattr(m, "predict_raw"):
            return m.predict_raw(_prep(x, cols))
        return _logit(m.predict_proba(_prep(x, cols))[:, 1])

    # 予想 -------------------------------------------------------
    def predict_tables(self, f, chunk_races=1500):
        """小分けにして _predict_tables を呼ぶ（3着の行はレース数×約120行になるため）。"""
        races = pd.unique(f["race_key"])
        if len(races) <= chunk_races:
            return self._predict_tables(f)
        parts = [self._predict_tables(f[f["race_key"].isin(races[i:i + chunk_races])])
                 for i in range(0, len(races), chunk_races)]
        return tuple(pd.concat([p[j] for p in parts], ignore_index=True) for j in range(3))

    def _predict_tables(self, f):
        """
        レースごとの確率表を返す。
          first:  race_key, lane, p_first
          second: race_key, w_lane, lane, p   （1着が w_lane のときの2着確率）
          third:  race_key, w_lane, s_lane, lane, p
        """
        f = f.reset_index(drop=True)
        rk = f["race_key"].to_numpy()
        p1 = _group_softmax(self._z(f, 1), rk, self.temps[1])
        first = pd.DataFrame({"race_key": rk, "lane": f["lane"].to_numpy(), "p_first": p1})

        lanes = f.groupby("race_key")["lane"].apply(list)
        # 2着: 全ての「1着艇a」の仮定
        pairs = [(r, a) for r, ls in lanes.items() for a in ls]
        w = pd.DataFrame(pairs, columns=["race_key", "w_lane"])
        x2 = f.merge(w, on="race_key")
        src = f[["race_key"] + _PLACED_COLS].rename(columns={c: f"w_{c}" for c in _PLACED_COLS})
        x2 = x2.merge(src, on=["race_key", "w_lane"], how="left")
        x2 = _rel_cols(x2[x2["lane"] != x2["w_lane"]].reset_index(drop=True), "w")
        g2 = x2["race_key"] + "_" + x2["w_lane"].astype(int).astype(str)
        x2["p"] = _group_softmax(self._z(x2, 2), g2.to_numpy(), self.temps[2])
        second = x2[["race_key", "w_lane", "lane", "p"]]

        # 3着: 全ての「1着a・2着b」の仮定
        trip = [(r, a, b) for r, ls in lanes.items() for a in ls for b in ls if a != b]
        ws = pd.DataFrame(trip, columns=["race_key", "w_lane", "s_lane"])
        x3 = f.merge(ws, on="race_key")
        x3 = x3.merge(src, on=["race_key", "w_lane"], how="left")
        src_s = f[["race_key"] + _PLACED_COLS].rename(columns={c: f"s_{c}" for c in _PLACED_COLS})
        x3 = x3.merge(src_s, on=["race_key", "s_lane"], how="left")
        x3 = x3[(x3["lane"] != x3["w_lane"]) & (x3["lane"] != x3["s_lane"])].reset_index(drop=True)
        x3 = _rel_cols(_rel_cols(x3, "w"), "s")
        g3 = (x3["race_key"] + "_" + x3["w_lane"].astype(int).astype(str) + "_"
              + x3["s_lane"].astype(int).astype(str))
        x3["p"] = _group_softmax(self._z(x3, 3), g3.to_numpy(), self.temps[3])
        third = x3[["race_key", "w_lane", "s_lane", "lane", "p"]]
        return first, second, third

    # 重要度 -----------------------------------------------------
    def importance(self, f, stage=1, n_repeats=3, max_races=3000, seed=0):
        """並べ替え重要度（その列をレースの間で入れ替えると LogLoss がどれだけ悪くなるか）。"""
        rng = np.random.default_rng(seed)
        races = f["race_key"].unique()
        if len(races) > max_races:
            races = rng.choice(races, max_races, replace=False)
        x = f[f["race_key"].isin(races)].reset_index(drop=True)
        if stage == 2:
            x = second_rows(x).reset_index(drop=True)
        elif stage == 3:
            x = third_rows(x).reset_index(drop=True)
        rk = x["race_key"].to_numpy()
        y = (x["finish"] == stage).to_numpy()
        cols = stage_cols(self.features, stage)

        def score(xx):
            p = _group_softmax(self._z(xx, stage), rk, self.temps[stage])
            return -np.log(np.clip(p[y], 1e-12, None)).mean()

        base = score(x)
        out = {}
        for c in cols:
            vals = []
            for _ in range(n_repeats):
                xx = x.copy()
                xx[c] = rng.permutation(xx[c].to_numpy())
                vals.append(score(xx) - base)
            out[c] = float(np.mean(vals))
        return pd.Series(out).sort_values(ascending=False)

    # 保存 -------------------------------------------------------
    def save(self, path):
        import joblib

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"format": MODEL_FORMAT, "version": self.version, "features": self.features,
                     "models": self.models, "temps": self.temps, "info": self.info,
                     "engine": self.engine}, path, compress=3)
        meta = {"version": self.version, "temps": self.temps, "info": self.info,
                "features": self.features}
        path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
        return path

    @classmethod
    def load(cls, path):
        import joblib

        d = joblib.load(path)
        m = cls(d["features"], d["version"], d.get("engine", "hgb"))
        m.models, m.temps, m.info = d["models"], d["temps"], d["info"]
        return m


# ---------------------------------------------------------------
# 今の予想の形（prediction.predict の final）に直す
# ---------------------------------------------------------------
def to_final(model, race_features, race=None):
    """1レース分の特徴量（ml_features の行）から、prediction.predict と同じ形の final を作る。"""
    first, second, third = model.predict_tables(race_features)
    lanes = sorted(int(v) for v in first["lane"])
    out = pd.DataFrame({"lane": lanes})
    if race is not None and "racer_name" in race.columns:
        out["racer_name"] = out["lane"].map(dict(zip(pd.to_numeric(race["lane"]).astype(int), race["racer_name"])))
    p1 = dict(zip(first["lane"].astype(int), first["p_first"]))
    out["p_first"] = out["lane"].map(p1).fillna(0.0)

    p2_marg = {ln: 0.0 for ln in lanes}
    for a in lanes:
        s = second[second["w_lane"] == a]
        cond = dict(zip(s["lane"].astype(int), s["p"]))
        out[f"p_second_given_{a}"] = out["lane"].map(cond).fillna(0.0)
        for b, v in cond.items():
            p2_marg[b] += p1.get(a, 0.0) * v
    p3_marg = {ln: 0.0 for ln in lanes}
    for (a, b), s in third.groupby(["w_lane", "s_lane"]):
        a, b = int(a), int(b)
        cond = dict(zip(s["lane"].astype(int), s["p"]))
        out[f"p_third_given_{a}_{b}"] = out["lane"].map(cond).fillna(0.0)
        pab = p1.get(a, 0.0) * float(out.loc[out["lane"] == b, f"p_second_given_{a}"].iloc[0])
        for c, v in cond.items():
            p3_marg[c] += pab * v
    out["p_second"] = out["lane"].map(p2_marg)
    out["p_third"] = out["lane"].map(p3_marg)
    out["model_version"] = model.version
    out["adjustment"] = 0.0
    out["kimarite_adjustment"] = 0.0
    out["kimarite_effect_pct"] = 0.0
    out["kimarite_starts"] = 0
    out["kimarite_wins"] = 0
    out["kimarite_dominant"] = ""
    out["kimarite_available"] = False
    out["reason"] = "学習モデル"
    return out


def trifecta_exact(first):
    """3連単 a-b-c = 1着(a) × 2着(b|a) × 3着(c|a,b)。合計は1（後から足す補正なし）。"""
    p1 = dict(zip(first["lane"].astype(int), pd.to_numeric(first["p_first"], errors="coerce").fillna(0.0)))
    lanes = sorted(p1)
    rows = []
    for a, b, c in itertools.permutations(lanes, 3):
        col2, col3 = f"p_second_given_{a}", f"p_third_given_{a}_{b}"
        if col2 not in first.columns or col3 not in first.columns:
            continue
        pb = float(first.loc[first["lane"] == b, col2].iloc[0])
        pc = float(first.loc[first["lane"] == c, col3].iloc[0])
        rows.append((f"{a}-{b}-{c}", p1[a] * pb * pc))
    out = pd.DataFrame(rows, columns=["combo", "prob"])
    total = out["prob"].sum()
    if total > 0:
        out["prob"] = out["prob"] / total
    return out


def is_ml_final(first):
    try:
        return str(first["model_version"].iloc[0]).startswith(MODEL_FAMILY)
    except Exception:  # noqa: BLE001
        return False


def trifecta_table(first_t, second_t, third_t):
    """predict_tables の結果から、全レースの3連単120通り（race_key, combo, prob）を一度に作る（検証用）。"""
    s = second_t.rename(columns={"lane": "s_lane", "p": "p2"})
    t = third_t.rename(columns={"lane": "t_lane", "p": "p3"})
    f = first_t.rename(columns={"lane": "w_lane"})
    x = f.merge(s, on=["race_key", "w_lane"]).merge(t, on=["race_key", "w_lane", "s_lane"])
    x["prob"] = x["p_first"] * x["p2"] * x["p3"]
    x["combo"] = (x["w_lane"].astype(int).astype(str) + "-" + x["s_lane"].astype(int).astype(str) + "-"
                  + x["t_lane"].astype(int).astype(str))
    return x[["race_key", "combo", "prob"]]
