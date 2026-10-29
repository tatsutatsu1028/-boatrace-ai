"""
Optuna で ml_model.LGB_PARAMS（1着・2着・3着の段ごと）を調整する実験。本番のモデル・設定は変えない。

  - 期間は tools/ml_experiment.py の「2年分」と同じに日付で分ける
      学習: 〜CALIB_START の前日 / 較正: CALIB_START〜TEST_START の前日 / テスト: TEST_START〜TEST_END
  - 調整に使うのは学習期間と較正期間だけ。各試行で学習期間だけで1つの段を学習し、
    較正期間で温度を決めたときの、その段の LogLoss（レースの中の割合）が小さい値を選ぶ
    （打ち切りの回数は今と同じく学習期間の直近8%の日付で決まる）。テスト期間は最後の比較にだけ使う
  - 艇番なしモデル（la_* の列）は今の LGB_PARAMS のままにし、3段の学習に使う列を全試行で同じにする
  - 1回目の試行は今の LGB_PARAMS（＝今の本番の設定）。テストでは、同じ期間で学習した
    今の設定のモデルと、調整後のモデルを、1着的中率・3連単的中率・回収率・LogLoss で比べる
    （本番の latest は学習期間にテスト期間を含むので比較には使わない）
  - 買い目・資金配分は本番と同じ手順（tools/ml_compare_old.py の bet）

結果は公式データを含むので、非公開のデータ用リポジトリ（--out）にだけ書く。
標準出力には件数・時間など中身を含まない進み具合だけを出す。

  python tools/ml_tune.py --out <データ用リポジトリ>/reports/ml_tune --trials 40
"""

from __future__ import annotations

import argparse
import contextlib
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ml_experiment as ex  # noqa: E402
import ml_features as mf  # noqa: E402
import ml_model as mm  # noqa: E402

log = ex.log
STAGE_NAMES = {1: "1着", 2: "2着", 3: "3着"}


@contextlib.contextmanager
def lgb_params(params):
    """_fit_lgb_softmax が読む mm.LGB_PARAMS を一時的に差し替える（ml_model.py は書き換えない）。"""
    old = mm.LGB_PARAMS
    mm.LGB_PARAMS = params
    try:
        yield
    finally:
        mm.LGB_PARAMS = old


def suggest(trial):
    p = dict(mm.LGB_PARAMS)
    p.update({
        "learning_rate": trial.suggest_float("learning_rate", 0.02, 0.12, log=True),
        "num_leaves": trial.suggest_int("num_leaves", 15, 255, log=True),
        "min_data_in_leaf": trial.suggest_int("min_data_in_leaf", 20, 2000, log=True),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.3, 1.0),
        "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 1.0),
        "lambda_l1": trial.suggest_float("lambda_l1", 1e-3, 10.0, log=True),
        "lambda_l2": trial.suggest_float("lambda_l2", 1e-2, 100.0, log=True),
        "min_gain_to_split": trial.suggest_float("min_gain_to_split", 1e-4, 1.0, log=True),
    })
    return p


# 1回目の試行（今の LGB_PARAMS。今は無い lambda_l1・min_gain_to_split は LightGBM の既定 0 に近い値）
def current_trial():
    p = mm.LGB_PARAMS
    return {k: p[k] for k in ("learning_rate", "num_leaves", "min_data_in_leaf", "feature_fraction",
                              "bagging_fraction", "lambda_l2")} | {"lambda_l1": 1e-3, "min_gain_to_split": 1e-4}


def stage_logloss(z, groups, y, t):
    p = mm._group_softmax(z, groups, t)
    return float(-np.log(np.clip(p[np.asarray(y).astype(bool)], 1e-12, None)).mean())


class StageData:
    """1つの段の学習・較正の行（全試行で使い回す）。"""

    def __init__(self, model, train_rows, calib_rows, stage):
        self.stage = stage
        cols = mm.stage_cols(model.features, stage)
        self.x = mm._prep(train_rows, cols)
        self.y = (train_rows["finish"] == stage).astype(int).to_numpy()
        self.g = mm._stage_groups(train_rows, stage)
        self.d = train_rows["race_date"].to_numpy()
        self.xc = mm._prep(calib_rows, cols)
        self.gc = mm._stage_groups(calib_rows, stage)
        self.yc = (calib_rows["finish"] == stage).to_numpy()

    def fit(self, params):
        with lgb_params(params):
            m = mm._fit_lgb_softmax(self.x, self.y, self.g, self.d, log=lambda *a: None)
        z = m.predict_raw(self.xc)
        t = mm._fit_temperature(z, self.gc, self.yc)
        return m, t, stage_logloss(z, self.gc, self.yc, t)


def tune_stage(sd, n_trials, timeout, seed, out):
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="minimize", sampler=optuna.samplers.TPESampler(seed=seed + sd.stage))
    study.enqueue_trial(current_trial(), user_attrs={"current": True})
    keep = {}
    t0 = time.time()

    def objective(trial):
        p = suggest(trial)
        params = mm.LGB_PARAMS if trial.number == 0 else p
        t1 = time.time()
        m, temp, ll = sd.fit(params)
        trial.set_user_attr("iter", int(m.n_iter_))
        trial.set_user_attr("temp", temp)
        trial.set_user_attr("seconds", time.time() - t1)
        if trial.number == 0:
            keep["current"] = (m, params)
        best = keep.get("best")
        if best is None or ll < best[2]:
            keep["best"] = (m, params, ll)
        log(f"[TUNE] {STAGE_NAMES[sd.stage]} 試行{trial.number + 1}/{n_trials} {m.n_iter_}回 "
            f"{time.time() - t1:.0f}秒（計{time.time() - t0:.0f}秒）")
        return ll

    study.optimize(objective, n_trials=n_trials, timeout=timeout)
    study.trials_dataframe().to_csv(out / f"trials_stage{sd.stage}.csv", index=False, encoding="utf-8")
    return keep["current"], keep["best"][:2], study


# ---------------------------------------------------------------
# 買い目の判定に使う race（hindcast.build_race。tools/ml_compare_old.py と同じ作り方、予想はしない）
# ---------------------------------------------------------------
def build_races(start, end, cache=None):
    import hindcast as hc
    from data_paths import data_path

    if cache and Path(cache).exists():
        with open(cache, "rb") as fh:
            return pickle.load(fh)
    t0 = time.time()
    hf = pd.read_csv(data_path("history_full.csv"), dtype=str, keep_default_na=False).replace({"": np.nan})
    hf["race_date"] = hf["race_date"].astype(str)
    days = [d.strftime("%Y%m%d") for d in pd.date_range(start, end)]
    k, _ = hc.load_k_results(days[0], days[-1], download_missing=False)
    races = {}
    for d in days:
        hf_day = hf[hf["race_date"] == d]
        if hf_day.empty:
            continue
        day = pd.Timestamp(d)
        cstats, vprof = hc.course_stats(k, day), hc.venue_profile(k, day)
        k_days = {
            jcd: g.groupby("d").agg(day_no=("day_no", "first"), meet_title=("meet_title", "first")).sort_index()
            for jcd, g in k[(k["d"] <= day) & (k["d"] >= day - pd.Timedelta(days=14))].groupby("jcd")
        }
        baseline = hc.course_baseline(hf[hf["race_date"] < d])
        for race_key, rows in hf_day.groupby("race_key", sort=True):
            rows = rows.drop_duplicates("lane")
            if len(rows) < 3:
                continue
            jcd = str(rows["jcd"].iloc[0]).zfill(2)
            k_meet = k[(k["jcd"] == jcd) & k["d"].isin(hc.meet_days(k_days, jcd, day))]
            races[race_key] = hc.build_race(rows, day, cstats, vprof, k_meet, baseline)
        log(f"[TUNE] race {d} 計{len(races)}レース {time.time() - t0:.0f}秒")
    if cache:
        Path(cache).parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "wb") as fh:
            pickle.dump(races, fh)
    return races


# ---------------------------------------------------------------
# テスト期間での比較
# ---------------------------------------------------------------
def stage_test_logloss(model, test):
    """2着・3着は実際の上位艇を条件にした LogLoss（学習のときと同じ形）。"""
    t = model.with_lane_free(test)
    rows = {1: t, 2: mm.second_rows(t), 3: mm.third_rows(t)}
    out = {}
    for s, x in rows.items():
        y = (x["finish"] == s).to_numpy()
        g = mm._stage_groups(x, s)
        ok = pd.Series(y).groupby(g).transform("sum").to_numpy() == 1
        out[f"stage{s}_logloss"] = stage_logloss(model._z(x[ok], s), g[ok], y[ok], model.temps[s])
    return out


def trifecta_rows(name, model, test, actual):
    """1レース1行: 1着の本命・3連単の最も確率の高い組・実際の3連単の確率。"""
    first, second, third = model.predict_tables(test)
    tri = mm.trifecta_table(first, second, third)
    top = tri.loc[tri.groupby("race_key")["prob"].idxmax()].set_index("race_key")
    tri = tri.assign(actual=tri["race_key"].map(actual))
    p_act = tri[tri["combo"] == tri["actual"]].set_index("race_key")["prob"]
    fav = first.loc[first.groupby("race_key")["p_first"].idxmax()].set_index("race_key")["lane"]
    d = pd.DataFrame({"race_key": list(actual)})
    d = d[d["race_key"].isin(top.index)]
    d["model"] = name
    d["winner"] = d["race_key"].map(lambda r: int(actual[r][0]))
    d["favorite"] = d["race_key"].map(fav).astype(int)
    d["top_combo"] = d["race_key"].map(top["combo"])
    d["top_combo_hit"] = d["top_combo"] == d["race_key"].map(actual)
    d["p_actual_trifecta"] = d["race_key"].map(p_act)
    return d


def bootstrap_diff(a, b, stat, n=1000, seed=0):
    """同じレースを選び直して stat(b) - stat(a) の95%区間を返す（a, b は race_key で並べた表）。"""
    rng = np.random.default_rng(seed)
    idx = np.arange(len(a))
    vals = []
    for _ in range(n):
        i = rng.choice(idx, len(idx), replace=True)
        vals.append(stat(b.iloc[i]) - stat(a.iloc[i]))
    return float(np.mean(vals)), float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975))


def make_model(version, base, stage_models, calib):
    m = mm.ChainModel(base.features, version, engine="lgb", lane_free=base.lane_free)
    m.lane_free_model = base.lane_free_model
    m.models = dict(stage_models)
    for s, mdl in stage_models.items():
        m.info[f"stage{s}_iter"] = int(mdl.n_iter_)
    m.calibrate(calib, log=lambda *a: None)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--features", default="", help="ml_features.build_table の結果（pickle）")
    ap.add_argument("--trials", type=int, default=40, help="段ごとの試行回数（1回目は今の設定）")
    ap.add_argument("--timeout", type=float, default=None, help="段ごとの上限秒数")
    ap.add_argument("--stages", default="1,2,3")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--races-cache", default="", help="買い目の判定に使う race の保存先（公式データを含む）")
    ap.add_argument("--no-bets", action="store_true", help="買い目・回収率を計算しない")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    if args.features and Path(args.features).exists():
        f = pd.read_pickle(args.features)
    else:
        f = mf.build_table(*mf.load_sources())
    train, calib, test = ex.split(f, "2y")
    log(f"[TUNE] 特徴量 {f.shape} 学習{train['race_key'].nunique()} 較正{calib['race_key'].nunique()} "
        f"テスト{test['race_key'].nunique()}レース {time.time() - t0:.0f}秒")

    # 艇番なしモデル（今の設定）と、それを足した学習・較正の行
    base = mm.ChainModel(mf.FEATURES + mm.LANE_FREE_DERIVED, "base", engine="lgb", lane_free=mf.LANE_FREE_FEATURES)
    tr = base._fit_lane_free(train[train["finish"].notna() | train["lane"].notna()], log=lambda *a: None)
    cb = base.with_lane_free(calib)
    log(f"[TUNE] 艇番なしモデル {time.time() - t0:.0f}秒")

    rows_fn = {1: lambda x: x, 2: mm.second_rows, 3: mm.third_rows}
    stages = [int(s) for s in args.stages.split(",")]
    cur_models, best_models, best_params, tune_info = {}, {}, {}, {}
    for s in (1, 2, 3):
        sd = StageData(base, rows_fn[s](tr), rows_fn[s](cb), s)
        if s in stages:
            (cm, _), (bm, bp), study = tune_stage(sd, args.trials, args.timeout, args.seed, out)
            cur_models[s], best_models[s], best_params[s] = cm, bm, bp
            tune_info[s] = {"trials": len(study.trials), "calib_logloss_current": study.trials[0].value,
                            "calib_logloss_best": study.best_value, "best_trial": study.best_trial.number,
                            "best_iter": study.best_trial.user_attrs["iter"]}
        else:
            m, _, _ = sd.fit(mm.LGB_PARAMS)
            cur_models[s] = best_models[s] = m
            best_params[s] = dict(mm.LGB_PARAMS)
        del sd
        log(f"[TUNE] {STAGE_NAMES[s]} 完了 {time.time() - t0:.0f}秒")
    (out / "best_params.json").write_text(json.dumps(
        {STAGE_NAMES[s]: {k: v for k, v in p.items() if k not in ("verbosity", "seed")}
         for s, p in best_params.items()}, ensure_ascii=False, indent=1, default=float))

    fam = f"{mm.MODEL_FAMILY}-v2-lgb-2y"
    models = {"current": make_model(f"{fam}-current", base, cur_models, calib),
              "tuned": make_model(f"{fam}-tuned", base, best_models, calib)}
    for name, m in models.items():
        m.save(out / "models" / f"{m.version}.joblib")
    log(f"[TUNE] 調整済みモデル {time.time() - t0:.0f}秒")

    # ---- テスト期間（ここで初めて使う）----
    actual = ex.actual_trifecta(test)
    summary = {"periods": {"train": [str(train["race_date"].min()), ex.CALIB_START],
                           "calib": [ex.CALIB_START, ex.TEST_START], "test": [ex.TEST_START, ex.TEST_END]},
               "tuning": {STAGE_NAMES[s]: v for s, v in tune_info.items()}, "test": {}}
    tri_rows, calib_rows = [], []
    for name, m in models.items():
        fm, firsts = ex.first_metrics(m.predict_tables(test)[0], test)
        summary["test"][name] = dict(fm, **stage_test_logloss(m, test), temps=m.temps,
                                     iters={s: int(m.models[s].n_iter_) for s in (1, 2, 3)})
        calib_rows.append(ex.calib_table(firsts["p_first"].to_numpy(), firsts["y"].to_numpy())
                          .assign(model=name, kind="1着確率"))
        tri_rows.append(trifecta_rows(name, m, test, actual))
        log(f"[TUNE] テスト {name} {time.time() - t0:.0f}秒")
    tri = pd.concat(tri_rows, ignore_index=True)
    common = set.intersection(*[set(d["race_key"]) for _, d in tri.groupby("model")])
    tri = tri[tri["race_key"].isin(common)]
    for name, d in tri.groupby("model"):
        summary["test"][name].update({
            "trifecta_races": int(len(d)),
            "trifecta_top_hit": float(d["top_combo_hit"].mean()),
            "trifecta_logloss": float(-np.log(np.clip(d["p_actual_trifecta"].fillna(1e-6), 1e-6, None)).mean()),
        })

    tickets = None
    if not args.no_bets:
        races = build_races(ex.TEST_START, ex.TEST_END, args.races_cache or None)
        pay = ex.payouts()
        tk = []
        for name, m in models.items():
            tsub = test[test["race_key"].isin(set(races))]
            finals, _ = ex.finals_by_race(m, tsub)
            preds = ex.bet_all(finals, races)
            tk.append(ex.ticket_rows(name, preds, actual, pay))
            log(f"[TUNE] 買い目 {name} {len(preds)}レース {time.time() - t0:.0f}秒")
        tickets = pd.concat(tk, ignore_index=True)
        common_t = set.intersection(*[set(d["race_key"]) for _, d in tickets.groupby("model")])
        tickets = tickets[tickets["race_key"].isin(common_t)]
        tickets.to_csv(out / "race_level_tickets.csv.gz", index=False, encoding="utf-8")
        ex.summarize(tickets).to_csv(out / "summary_tickets.csv", index=False, encoding="utf-8")
        for name, d in tickets.groupby("model"):
            calib_rows.append(ex.calib_table(d["hit_probability"].to_numpy(), d["candidate_hit"].astype(float)
                                             .to_numpy(), ex.HIT_BINS).assign(model=name, kind="買い目全体の的中確率"))

    # ---- 差の95%区間（同じレースを選び直す）----
    a = tri[tri["model"] == "current"].set_index("race_key").sort_index()
    b = tri[tri["model"] == "tuned"].set_index("race_key").sort_index()
    diffs = {
        "first_hit": bootstrap_diff(a, b, lambda d: (d["favorite"] == d["winner"]).mean()),
        "trifecta_top_hit": bootstrap_diff(a, b, lambda d: d["top_combo_hit"].mean()),
        "trifecta_logloss": bootstrap_diff(
            a, b, lambda d: -np.log(np.clip(d["p_actual_trifecta"].fillna(1e-6), 1e-6, None)).mean()),
    }
    if tickets is not None:
        ta = tickets[tickets["model"] == "current"].set_index("race_key").sort_index()
        tb = tickets[tickets["model"] == "tuned"].set_index("race_key").sort_index()
        diffs["candidate_hit"] = bootstrap_diff(ta, tb, lambda d: d["candidate_hit"].mean())
        diffs["bet_hit"] = bootstrap_diff(ta, tb, lambda d: d["bet_hit"].mean())
        diffs["roi"] = bootstrap_diff(ta, tb, lambda d: d["return"].sum() / d["stake"].sum())
    summary["test_diff_tuned_minus_current"] = {k: dict(zip(("mean", "lo95", "hi95"), v)) for k, v in diffs.items()}
    tri.to_csv(out / "race_level_trifecta.csv.gz", index=False, encoding="utf-8")
    pd.concat(calib_rows, ignore_index=True).to_csv(out / "calibration.csv", index=False, encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float))
    log(f"[TUNE] 終了 {time.time() - t0:.0f}秒")


if __name__ == "__main__":
    main()
