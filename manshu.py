"""
「💥 万舟チャンス（荒れやすいレース）」: 3連単の払戻が100円あたり1万円以上（万舟）になりやすいレース。

レースごとに「万舟になる確率」を出す小さなモデル（LightGBM の2値分類）を、新モデル（ml-chain）の
1着確率・3連単確率の散らばりと、級別・勝率の差・場・R など1レース分の特徴量から作る。

  展示前版: 展示前モデル（ml_pre）の出力と朝に分かる情報だけ。毎朝 pre_picks.py がその日の全レースの
            確率を出し、上位10レースに印を付けて Supabase の manshu_chance_picks に保存する
  展示後版: 今の新モデルの出力と展示・直前情報も使う。アプリか自動固定がそのレースを予想したとき
            （ml_live.production_final）に確率を出し、保存する予想（payload の prediction_model.manshu_post）
            に残す。印のレースではトリガーが manshu_chance_picks に写す

検証（tools/manshu_eval.py。結果はデータ用リポジトリの reports/manshu/REPORT.md）:
  テスト 2026-07-07〜10-14 で毎日上位10レースの万舟率 約26%（全体 16%）。展示前版と展示後版の差は小さい。
  印のレースは本命が来にくく、本線の買い目の的中率・回収率は下がる。穴寄りの買い目（1号艇を1着から
  外した上位10点）は印のレースで回収率が上がる傾向だが、100%を超える根拠は無い。
  → 画面では「荒れやすいレース」として見せ、「儲かる」とは書かない。

学習済みファイル（データ用リポジトリ）: models/manshu_latest.json が版と2つのファイル名を指す。
特徴量の作り方は検証と同じ関数（このモジュールの race_features・chain_summary）を使う。
"""

from __future__ import annotations

import json
from functools import lru_cache

import numpy as np
import pandas as pd

from data_paths import data_path

MANSHU_YEN = 10000          # 100円あたりの3連単の払戻がこれ以上なら万舟
PICK_COUNT = 10             # 毎朝、印を付けるレースの数（その日の全レースの中で確率が高い順）
LONGSHOT_N = 10             # 穴寄りの買い目の点数（1号艇を1着から外した3連単確率の上位）
LONGSHOT_STAKE = 100        # 穴寄りの買い目の成績を記録するときの1点あたりの賭け金（表示では資金配分しない）
BASE_RATE = 0.165           # 過去2年の全レースの万舟率（2024-09〜2026-10。表示の目安）
LABEL = "万舟チャンス（荒れやすいレース）"
ICON = "💥"
NOTE = "印のレースは本命が来にくい傾向があります（払戻や回収率が上がることを示すものではありません）。"

LATEST_FILE = "models/manshu_latest.json"

# 1艇ずつの列のうち、艇番ごとに横に並べてレースの特徴量にするもの
LANE_COLS_COMMON = ["class_ord", "national_win_rate", "r365_win", "rc_win", "rc_top3", "avg_st", "r180_st",
                    "motor_2ren", "mot_top2", "f_count", "rv_win"]
LANE_COLS_POST = ["exhibition_time", "exhibition_st", "rk_exh_time"]
RACE_COLS_COMMON = ["jcd", "race_no", "distance_m", "fixed_entry", "race_type_ord", "grade_ord", "day_no",
                    "is_final_day", "deadline_min", "is_night", "n_boats", "st12_diff", "outer_win_sum",
                    "outer_win_max", "outer_rc_win_sum"]
RACE_COLS_POST = ["wind_speed", "wave_cm", "wind_head", "wind_cross", "temperature", "water_temperature",
                  "weather_code", "stabilizer", "st12_ex_diff", "n_front_entry"]


# ---------------------------------------------------------------
# 特徴量（検証 tools/manshu_eval.py と同じ）
# ---------------------------------------------------------------
def chain_summary(first, tri):
    """新モデルの1着確率・3連単確率の散らばり（1レース1行）。first: race_key, lane, p_first / tri: race_key, combo, prob"""
    f = first.copy()
    f["lane"] = f["lane"].astype(int)
    p = f.pivot_table(index="race_key", columns="lane", values="p_first").reindex(columns=range(1, 7))
    p = p.fillna(0.0)
    arr = p.to_numpy()
    srt = -np.sort(-arr, axis=1)
    ent = -(arr * np.log(np.clip(arr, 1e-12, None))).sum(axis=1)
    out = pd.DataFrame(index=p.index)
    for ln in range(1, 7):
        out[f"c_p1_l{ln}"] = arr[:, ln - 1]
    out["c_p1_max"] = srt[:, 0]
    out["c_p1_2nd"] = srt[:, 1]
    out["c_p1_gap"] = srt[:, 0] - srt[:, 1]
    out["c_p1_entropy"] = ent
    out["c_p1_fav_lane"] = arr.argmax(axis=1) + 1
    out["c_p1_out456"] = arr[:, 3:].sum(axis=1)

    t = tri.copy()
    t["a"] = t["combo"].str[0].astype(int)
    t["in3_1"] = t["combo"].str.contains("1")
    t["n_out"] = t["combo"].str.count("[456]")
    g = t.groupby("race_key")
    out["c_tri_max"] = g["prob"].max()
    out["c_tri_entropy"] = g["prob"].apply(lambda s: float(-(s * np.log(np.clip(s, 1e-12, None))).sum()))
    t = t.sort_values(["race_key", "prob"], ascending=[True, False])
    t["rank"] = t.groupby("race_key").cumcount()
    out["c_tri_top5"] = t[t["rank"] < 5].groupby("race_key")["prob"].sum()
    out["c_tri_top10"] = t[t["rank"] < 10].groupby("race_key")["prob"].sum()
    out["c_tri_top20"] = t[t["rank"] < 20].groupby("race_key")["prob"].sum()
    out["c_lane1_out3"] = 1.0 - t[t["in3_1"]].groupby("race_key")["prob"].sum()
    out["c_two_outer"] = t[t["n_out"] >= 2].groupby("race_key")["prob"].sum()
    return out


def race_features(f, post):
    """1艇1行の特徴量の表（ml_features と同じ列）から、1レース1行の特徴量（艇番ごとに横に並べる＋レースの列）。"""
    lane_cols = LANE_COLS_COMMON + (LANE_COLS_POST if post else [])
    race_cols = RACE_COLS_COMMON + (RACE_COLS_POST if post else [])
    x = f[["race_key", "lane"] + lane_cols].copy()
    x["lane"] = x["lane"].astype(int)
    w = x.pivot_table(index="race_key", columns="lane", values=lane_cols, dropna=False)
    w.columns = [f"{c}_l{ln}" for c, ln in w.columns]
    r = f.drop_duplicates("race_key").set_index("race_key")[race_cols + ["race_date"]]
    r = r.join(w)
    # 1号艇の強さ・艇同士の差
    for c in ("national_win_rate", "class_ord", "rc_win", "avg_st", "motor_2ren"):
        cols = [f"{c}_l{ln}" for ln in range(1, 7) if f"{c}_l{ln}" in r.columns]
        v = r[cols].apply(pd.to_numeric, errors="coerce")
        others = v.iloc[:, 1:]
        r[f"{c}_sd"] = v.std(axis=1)
        r[f"{c}_range"] = v.max(axis=1) - v.min(axis=1)
        r[f"{c}_l1_minus_othermax"] = v.iloc[:, 0] - others.max(axis=1)
        r[f"{c}_l1_minus_othermean"] = v.iloc[:, 0] - others.mean(axis=1)
    cls = r[[f"class_ord_l{ln}" for ln in range(1, 7)]].apply(pd.to_numeric, errors="coerce")
    with np.errstate(all="ignore"):
        top = np.nanmax(np.where(np.isnan(cls.to_numpy()), -np.inf, cls.to_numpy()), axis=1)
    r["n_top_class"] = (cls.to_numpy() == top[:, None]).sum(axis=1)
    if post:
        ex = r[[f"exhibition_time_l{ln}" for ln in range(1, 7)]].apply(pd.to_numeric, errors="coerce")
        r["exh_sd"] = ex.std(axis=1)
        r["exh_range"] = ex.max(axis=1) - ex.min(axis=1)
        r["exh_l1_minus_best_other"] = ex.iloc[:, 0] - ex.iloc[:, 1:].min(axis=1)
        # 検証・学習と同じ式（欠けた艇があるとその艇番になる。学習もこの値で行った）
        r["exh_best_lane"] = ex.to_numpy().argmin(axis=1) + 1
        r.loc[ex.isna().all(axis=1), "exh_best_lane"] = np.nan
    r["jcd"] = pd.to_numeric(r["jcd"], errors="coerce")
    return r


def longshot_tickets(tri, n=LONGSHOT_N):
    """穴寄りの買い目: 1号艇を1着から外した3連単確率の上位 n 点。tri: race_key, combo, prob → {race_key: [{combo, prob}]}"""
    t = tri[tri["combo"].astype(str).str[0] != "1"]
    t = t.sort_values(["race_key", "prob"], ascending=[True, False]).groupby("race_key").head(n)
    return {rk: [{"combo": str(c), "prob": round(float(p), 5)} for c, p in zip(g["combo"], g["prob"])]
            for rk, g in t.groupby("race_key", sort=False)}


# ---------------------------------------------------------------
# 万舟狙いの買い目（検証中・参考。オッズを使う）
# ---------------------------------------------------------------
# 作り方は検証（tools/manshu_eval.py odds）の結果で決める。ここを変えれば画面・保存・成績の全部が変わる。
#   "A": オッズ AIM_ODDS_MIN 倍以上の組の中から、モデルの3連単確率が高い順に AIM_N 点
#   "B": 同じ組の中で、4〜6号艇が1着か2着に入る組を優先（足りなければ残りで埋める）
AIM_METHOD = "A"
AIM_N = 10
AIM_ODDS_MIN = 100.0
AIM_STAKE = 100             # 1点あたり（資金配分しない）
AIM_LABEL = "万舟狙いの買い目"
AIM_NOTE = ("検証中・参考: 今の時点の3連単オッズが100倍以上の組から、モデルの確率が高い順に選んでいます。"
            "儲かるとは限りません。")
AIM_MORNING_NOTE = "朝の時点のオッズは売れ始めで当てになりません。締切前に「オッズを更新」で取り直してください。"


def is_outer_top2(combo):
    """4〜6号艇が1着か2着に入る組か。"""
    c = str(combo)
    return len(c) >= 3 and (c[0] in "456" or c[2] in "456")


def aim_tickets(probs, odds, method=None, n=None, odds_min=None):
    """万舟狙いの買い目。probs: {組番: 3連単確率} / odds: {組番: オッズ}
    → [{combo, prob, odds, stake, payout}]（payout は当たったときの払戻。オッズ × 賭け金）"""
    method = method or AIM_METHOD
    n = n or AIM_N
    odds_min = AIM_ODDS_MIN if odds_min is None else odds_min
    rows = []
    for combo, p in (probs or {}).items():
        o = (odds or {}).get(combo)
        try:
            o, p = float(o), float(p)
        except (TypeError, ValueError):
            continue
        if o != o or p != p or o < odds_min:
            continue
        rows.append((combo, p, o))
    if method == "B":
        rows.sort(key=lambda r: (not is_outer_top2(r[0]), -r[1], r[0]))
    else:
        rows.sort(key=lambda r: (-r[1], r[0]))
    return [{"combo": c, "prob": round(p, 5), "odds": o, "stake": AIM_STAKE, "payout": int(round(o * AIM_STAKE))}
            for c, p, o in rows[:n]]


def tri_dict(tri):
    """3連単確率の表（combo, prob）か dict → {組番: 確率}。"""
    if tri is None:
        return {}
    if isinstance(tri, dict):
        return {str(k): float(v) for k, v in tri.items()}
    return {str(c): float(p) for c, p in zip(tri["combo"], tri["prob"])}


def odds_dict(odds):
    """fetch_odds3t の表（combo, odds）か dict → {組番: オッズ}。"""
    if odds is None:
        return {}
    if isinstance(odds, dict):
        return {str(k): float(v) for k, v in odds.items()}
    if len(odds) == 0 or "combo" not in odds.columns:
        return {}
    return {str(c): float(o) for c, o in zip(odds["combo"], pd.to_numeric(odds["odds"], errors="coerce"))}


# ---------------------------------------------------------------
# 学習済みモデル
# ---------------------------------------------------------------
def latest():
    """{"version", "pre", "post", ...}（models/manshu_latest.json）。読めなければ None。"""
    try:
        return json.loads(data_path(LATEST_FILE).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def app_files():
    """アプリが取るファイル（展示後版だけ。展示前版は朝の Actions がデータ用リポジトリから読む）。"""
    info = latest()
    return (f"models/{info['post']}",) if info and info.get("post") else ()


@lru_cache(maxsize=4)
def _booster(name):
    import lightgbm as lgb

    return lgb.Booster(model_file=str(data_path(f"models/{name}")))


def booster(kind):
    """kind: "pre" / "post" → (Booster, 版の名前)。"""
    info = latest()
    if not info or not info.get(kind):
        raise RuntimeError(f"万舟モデル（{kind}）が {LATEST_FILE} にありません")
    return _booster(info[kind]), str(info.get("version") or info[kind])


def predict_frame(bst, feats, first, tri, post):
    """1艇1行の特徴量 feats と新モデルの出力（first・tri）から、レースごとの万舟確率（Series: race_key → 確率）。"""
    x = race_features(feats, post=post).join(chain_summary(first, tri), how="inner")
    cols = bst.feature_name()
    missing = [c for c in cols if c not in x.columns]
    if missing:
        raise RuntimeError(f"万舟モデルの特徴量が足りません: {missing[:5]}")
    p = bst.predict(x[cols].apply(pd.to_numeric, errors="coerce"))
    return pd.Series(p, index=x.index, name="manshu_prob")


def predict_with_chain(kind, chain_model, feats):
    """新モデル chain_model で feats を予想し、万舟確率と穴寄りの買い目を返す。

    戻り値 (probs: Series, longshots: {race_key: [...]}, 版)
    """
    import ml_model as mm

    bst, version = booster(kind)
    first, second, third = chain_model.predict_tables(feats)
    tri = mm.trifecta_table(first, second, third)
    probs = predict_frame(bst, feats, first, tri, post=(kind == "post"))
    probs.attrs["tri"] = tri
    return probs, longshot_tickets(tri), version


def post_info(chain_model, feats):
    """展示後版（アプリ・自動固定の1レース）。保存する予想の prediction_model.manshu_post に入れる値。"""
    probs, longs, version = predict_with_chain("post", chain_model, feats)
    rk = str(probs.index[0])
    return {"prob": round(float(probs.iloc[0]), 4), "version": version, "race_key": rk,
            "longshot": longs.get(rk, [])}


def select_marks(probs, n=PICK_COUNT):
    """その日の全レースの確率から印を付けるレース（確率が高い順に n レース）。"""
    s = probs.sort_values(ascending=False).head(n)
    return list(s.index)


# ---------------------------------------------------------------
# 結果
# ---------------------------------------------------------------
def stake_longshots(longs):
    """穴寄りの買い目に成績記録用の賭け金（1点100円）を付ける。"""
    return [{"combo": t["combo"], "stake": LONGSHOT_STAKE} for t in (longs or [])]


def is_manshu(payout):
    return payout is not None and payout == payout and float(payout) >= MANSHU_YEN


def summarize(rows):
    """結果の出た印のレースの成績（manshu_chance_picks の行・result_status = settled）。"""
    if not rows:
        return None
    df = pd.DataFrame(rows)

    def roi(prefix):
        if f"{prefix}_stake" not in df.columns:
            return None
        st_ = pd.to_numeric(df.get(f"{prefix}_stake"), errors="coerce").fillna(0)
        rt = pd.to_numeric(df.get(f"{prefix}_return"), errors="coerce").fillna(0)
        return float(rt.sum() / st_.sum()) if st_.sum() else None

    def hit(prefix):
        h = df.get(f"{prefix}_hit")
        return float(h.fillna(False).astype(bool).mean()) if h is not None else None

    pay = pd.to_numeric(df["trifecta_payout"], errors="coerce")
    return {
        "日数": int(df["race_date"].nunique()),
        "レース数": int(len(df)),
        "万舟率": float(df["is_manshu"].fillna(False).astype(bool).mean()),
        "3連単の平均配当": float(pay.mean()),
        "3連単の配当の中央値": float(pay.median()),
        "本線 的中率": hit("main"),
        "本線 回収率": roi("main"),
        "穴寄り 的中率": hit("longshot"),
        "穴寄り 回収率": roi("longshot"),
        **aim_summary(df),
    }


def aim_summary(df):
    """表示した万舟狙いの買い目の成績（表示したレースだけ）。"""
    if "aim_stake" not in df.columns:
        return {}
    a = df[pd.to_numeric(df["aim_stake"], errors="coerce").fillna(0) > 0]
    if not len(a):
        return {"万舟狙い レース数": 0}
    return {
        "万舟狙い レース数": int(len(a)),
        "万舟狙い 的中率": float(a["aim_hit"].fillna(False).astype(bool).mean()),
        "万舟狙い 回収率": float(pd.to_numeric(a["aim_return"], errors="coerce").fillna(0).sum()
                             / pd.to_numeric(a["aim_stake"], errors="coerce").fillna(0).sum()),
    }
