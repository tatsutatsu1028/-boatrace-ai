"""
ml_model.ChainModel の1着・2着・3着の各段で、特徴量ごとの寄与（SHAP値）を調べる。本番のモデル・設定は変えない。

  - shap ライブラリは使わず、LightGBM の predict(pred_contrib=True)（TreeSHAP）で寄与を出す
  - 期間は tools/ml_experiment.py と同じ。テスト期間（TEST_START〜TEST_END）のレースが対象
  - 各段の確率はレース（2着・3着は「1着艇・2着艇を仮定した組」）の中の割合なので、
    全艇に同じだけ足される分は確率を変えない。寄与は組の中の平均を引いた値（中心化）で比べ、温度も掛ける
  - 2着・3着は、実際の1着艇（・2着艇）を条件にした行（学習のときと同じ形）
  - 艇番なしモデルの出力から作った列（la_*）は「艇番なしモデル」1つにまとめる
  - 全レース・1号艇が負けたレース・3連単が万舟（払戻10,000円以上）のレースで、平均寄与（絶対値）の順位を出す

結果は公式データを含むので、非公開のデータ用リポジトリ（--out）にだけ書く。
標準出力には件数・時間など中身を含まない進み具合だけを出す。

  python tools/ml_shap.py --out <データ用リポジトリ>/reports/ml_shap \
      --model <データ用リポジトリ>/reports/ml_tune/models/ml-chain-v2-lgb-2y-current.joblib
"""

from __future__ import annotations

import argparse
import json
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
LANE_FREE = "艇番なしモデル"
MANSHU = 10000
# ほとんど効いていない: 全レースでの平均寄与（絶対値）が、その段の合計の0.5%未満
LOW_SHARE = 0.005

# 特徴量の日本語の短い名前（REPORT.md と、第2段階の表示の下敷き）
LABELS = {
    "class_ord": "級別", "national_win_rate": "全国勝率", "national_2ren": "全国2連率", "national_3ren": "全国3連率",
    "age": "年齢", "weight": "体重", "r365_n": "1年の出走数", "r365_win": "1年の1着率", "r365_top2": "1年の2連対率",
    "r365_top3": "1年の3連対率", "r365_fin": "1年の平均着順", "r90_n": "90日の出走数", "r90_win": "90日の1着率",
    "r90_top3": "90日の3連対率", "r90_fin": "90日の平均着順", "last5_fin": "直近5走の平均着順",
    "last10_fin": "直近10走の平均着順", "last1_fin": "前走の着順", "racer_no": "登録番号",
    "exp_course": "想定コース", "front_entry": "前付け", "rc_n": "このコースの出走数", "rc_win": "このコースの1着率",
    "rc_top2": "このコースの2連対率", "rc_top3": "このコースの3連対率", "rc_st": "このコースの平均ST",
    "racer_avg_course_shift": "普段のコースのずれ", "rc_st_rank": "このコースのST順位", "rc_k_nige": "このコースの逃げ",
    "rc_k_makuri": "このコースのまくり", "rc_k_sashi": "このコースの差し", "rc_k_msashi": "このコースのまくり差し",
    "local_win_rate": "当地勝率", "local_2ren": "当地2連率", "local_3ren": "当地3連率", "rv_n": "当地の出走数",
    "rv_win": "当地の1着率", "rv_top3": "当地の3連対率", "rv_n_all": "当地の出走経験",
    "avg_st": "平均ST", "r180_st": "180日の平均ST", "r180_st_sd": "STのばらつき", "r180_st_late": "出遅れの多さ",
    "exhibition_st": "展示ST", "f_count": "F数", "l_count": "L数", "f_period": "F休み明け", "f_flag": "F持ち",
    "meet_n": "今節の出走数", "meet_fin": "今節の平均着順", "meet_fin_adj": "今節の成績（枠補正）", "meet_top2": "今節の2連対率",
    "meet_st": "今節の平均ST", "day_no": "節の何日目", "is_final_day": "最終日", "meet_last_adj": "今節の前走（枠補正）",
    "meet_trend": "今節の調子の上向き",
    "motor_2ren": "モーター2連率", "motor_3ren": "モーター3連率", "boat_2ren": "ボート2連率", "boat_3ren": "ボート3連率",
    "mot_n": "モーターの使用数", "mot_top2": "モーターの最近の2連対率", "mot_fin": "モーターの最近の平均着順",
    "exhibition_time": "展示タイム", "tilt": "チルト", "propeller_new": "新プロペラ", "parts_exchanged": "部品交換",
    "adjust_weight": "調整重量",
    "wind_speed": "風速", "wave_cm": "波高", "temperature": "気温", "water_temperature": "水温", "weather_code": "天候",
    "wind_rel": "風向き", "wind_head": "向かい風・追い風", "wind_cross": "横風",
    "jcd": "場", "race_no": "レース番号", "lane": "艇番", "distance_m": "距離", "fixed_entry": "進入固定",
    "race_type_ord": "レースの種類", "grade_ord": "グレード", "stabilizer": "安定板", "deadline_min": "締切時刻",
    "is_night": "ナイター",
    "rk_win_rate": "全国勝率の順位", "rk_r365_win": "1年の1着率の順位", "rk_exh_time": "展示タイムの順位",
    "rk_exh_st": "展示STの順位", "rk_avg_st": "平均STの順位", "rk_motor": "モーター2連率の順位",
    "rk_rc_win": "コース1着率の順位", "d_win_rate": "全国勝率の平均との差", "d_exh_time": "展示タイムの平均との差",
    "d_exh_st": "展示STの平均との差", "d_avg_st": "平均STの平均との差", "d_rc_win": "コース1着率の平均との差",
    "in_win_rate": "内隣の全国勝率", "in_avg_st": "内隣の平均ST", "in_exh_st": "内隣の展示ST",
    "out_win_rate": "外隣の全国勝率", "out_avg_st": "外隣の平均ST", "lane1_win_rate": "1号艇の全国勝率",
    "lane1_rc_win": "1号艇のコース1着率", "lane1_exh_st": "1号艇の展示ST", "n_boats": "出走艇数",
    "n_front_entry": "前付けする艇の数", "rk_nat2": "全国2連率の順位", "rk_nat3": "全国3連率の順位",
    "rk_class": "級別の順位", "rk_r365_top3": "1年の3連対率の順位", "rk_last5": "直近5走の順位",
    "rk_rc_top3": "コース3連対率の順位", "rk_rc_st": "コースSTの順位", "rk_local": "当地勝率の順位",
    "rk_motor3": "モーター3連率の順位", "rk_boat": "ボートの順位", "rk_meet_adj": "今節成績の順位",
    "rk_weight": "体重の順位", "st12_diff": "1・2号艇のST差", "st12_ex_diff": "1・2号艇の展示ST差",
    "outer_win_sum": "外枠の勝率の合計", "outer_win_max": "外枠の勝率の最大", "outer_rc_win_sum": "外枠のコース1着率の合計",
    LANE_FREE: "艇番を除いた実力",
}
_PLACED_LABELS = {"lane_gap": "艇番の差", "course_gap": "コースの差", "inside": "内側か"}


def label(c):
    if c in LABELS:
        return LABELS[c]
    for tag, who in (("w_", "1着艇の"), ("s_", "2着艇の")):
        if c.startswith(tag):
            rest = c[len(tag):]
            return who + _PLACED_LABELS.get(rest, LABELS.get(rest, rest))
    return c


def group_of(c):
    if c == LANE_FREE:
        return LANE_FREE
    if c.startswith("w_"):
        return "1着艇との関係"
    if c.startswith("s_"):
        return "2着艇との関係"
    for g, cols in mf.FEATURE_GROUPS.items():
        if c in cols:
            return g
    return ""


def stage_contrib(model, x, stage):
    """組の中で中心化し、温度を掛けた寄与（列: 特徴量。la_* は1列にまとめる）。"""
    cols = mm.stage_cols(model.features, stage)
    m = model.models[stage]
    xp = mm._prep(x, cols)
    c = m.booster.predict(xp, num_iteration=m.n_iter_, pred_contrib=True)
    raw = m.predict_raw(xp)
    err = float(np.abs(c.sum(axis=1) - raw).max())
    c = pd.DataFrame(c[:, :-1] * model.temps[stage], columns=cols, index=x.index)
    g = mm._stage_groups(x, stage)
    c = c - c.groupby(g).transform("mean")
    la = [k for k in cols if k in mm.LANE_FREE_DERIVED]
    if la:
        c[LANE_FREE] = c[la].sum(axis=1)
        c = c.drop(columns=la)
    splits = dict(zip(m.booster.feature_name(), m.booster.feature_importance("split")))
    return c, g, err, splits


def ranking(c, mask):
    v = c[mask].abs().mean()
    return v, v / v.sum(), v.rank(ascending=False, method="min").astype(int)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", required=True, help="tools/ml_tune.py などで同じ期間に学習した ChainModel（joblib）")
    ap.add_argument("--features", default="", help="ml_features.build_table の結果（pickle）")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    model = mm.ChainModel.load(args.model)
    if str(model.info.get("train_to", "")) >= ex.CALIB_START or str(model.info.get("calib_to", "")) >= ex.TEST_START:
        raise SystemExit("モデルの学習・調整の期間がテスト期間と重なっている")
    f = pd.read_pickle(args.features) if args.features and Path(args.features).exists() \
        else mf.build_table(*mf.load_sources())
    _, _, test = ex.split(f, "2y")
    t = model.with_lane_free(test)
    log(f"[SHAP] テスト{t['race_key'].nunique()}レース {time.time() - t0:.0f}秒")

    actual = ex.actual_trifecta(test)
    pay = ex.payouts()
    po = {rk: pay.get(rk, {}).get(a, np.nan) for rk, a in actual.items()}
    winner = {rk: int(a[0]) for rk, a in actual.items()}
    seg_races = {
        "all": set(actual),
        "lane1_lost": {rk for rk, w in winner.items() if w != 1},
        "manshu": {rk for rk, p in po.items() if np.isfinite(p) and p >= MANSHU},
    }

    rows = {1: t, 2: mm.second_rows(t), 3: mm.third_rows(t)}
    summary = {"model": model.version, "test": [ex.TEST_START, ex.TEST_END], "low_share": LOW_SHARE,
               "manshu_yen": MANSHU, "races": {k: len(v) for k, v in seg_races.items()}, "stages": {}}
    low_rows, group_rows = [], []
    for s, x in rows.items():
        x = x.reset_index(drop=True)
        y = (x["finish"] == s).to_numpy()
        ok = (pd.Series(y).groupby(mm._stage_groups(x, s)).transform("sum") == 1).to_numpy()
        ok = ok & x["race_key"].isin(seg_races["all"]).to_numpy()
        x, y = x[ok].reset_index(drop=True), y[ok]
        c, g, err, splits = stage_contrib(model, x, s)
        tab = pd.DataFrame({"feature": c.columns})
        tab["label"] = tab["feature"].map(label)
        tab["group"] = tab["feature"].map(group_of)
        for seg, races in seg_races.items():
            mask = x["race_key"].isin(races).to_numpy()
            v, share, rank = ranking(c, mask)
            tab[f"mean_abs_{seg}"] = tab["feature"].map(v)
            tab[f"share_{seg}"] = tab["feature"].map(share)
            tab[f"rank_{seg}"] = tab["feature"].map(rank)
            # 実際にその着になった艇を押し上げた（正）か押し下げた（負）かの平均
            tab[f"actual_mean_{seg}"] = tab["feature"].map(c[mask & y].mean())
            summary["stages"].setdefault(str(s), {})[f"rows_{seg}"] = int(mask.sum())
        for seg in ("lane1_lost", "manshu"):
            tab[f"rank_change_{seg}"] = tab["rank_all"] - tab[f"rank_{seg}"]
            tab[f"ratio_{seg}"] = tab[f"mean_abs_{seg}"] / tab["mean_abs_all"]
        tab["splits"] = tab["feature"].map(lambda k: int(sum(v for n, v in splits.items() if n in mm.LANE_FREE_DERIVED))
                                           if k == LANE_FREE else int(splits.get(k, 0)))
        tab = tab.sort_values("rank_all")
        tab.to_csv(out / f"importance_stage{s}.csv", index=False, encoding="utf-8")
        low = tab[tab["share_all"] < LOW_SHARE].assign(stage=s)
        low_rows.append(low[["stage", "feature", "label", "group", "share_all", "share_lane1_lost", "share_manshu",
                             "rank_all", "splits"]])
        gr = tab.groupby("group")[[f"share_{k}" for k in seg_races]].sum().reset_index().assign(stage=s)
        group_rows.append(gr)
        summary["stages"][str(s)].update({"features": int(len(tab)), "low": int(len(low)),
                                          "max_abs_sum_error": err})
        log(f"[SHAP] {s}着 {len(x)}行 {len(tab)}列 {time.time() - t0:.0f}秒")

    low = pd.concat(low_rows, ignore_index=True)
    low.to_csv(out / "low_importance.csv", index=False, encoding="utf-8")
    # 3段とも効いていない特徴量
    cnt = low.groupby("feature").size()
    summary["low_all_stages"] = sorted(cnt[cnt == 3].index.tolist())
    pd.concat(group_rows, ignore_index=True).to_csv(out / "group_share.csv", index=False, encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float))
    log(f"[SHAP] 終了 {time.time() - t0:.0f}秒")


if __name__ == "__main__":
    main()
