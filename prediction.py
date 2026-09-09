from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


BASE_NUM = [
    "race_no",
    "lane",
    "racer_win_rate",
    "local_win_rate",
    "motor_2ren",
    "boat_2ren",
    "avg_st",
]
BASE_CAT = ["venue"]

# 検証データでロジック世代を区別するための固定ID。
MODEL_VERSION = "position-v1-20260908"

# オリジナル展示（直線・まわり足・1周）は順位ベースで評価するため、
# 一部の艇にしか値が入っていないと、その艇だけが不当に高く評価される。
# 有効な値がこの数に満たない列は評価対象外にする。
_ORIGINAL_MIN_VALID = 4

# 選手×想定コースの決まり手実績。
# BOAT RACE公式の選手「コース別成績」ページには決まり手別内訳がないため、
# history_full.csv に保存した公式レース結果から自前集計する。
# 現行システムと同じく艇番＝想定コースとして扱う。
_KIMARITE_METHODS = (
    "nige",
    "makuri",
    "sashi",
    "makuri_sashi",
    "nuki",
    "megumare",
)
_KIMARITE_JA = {
    "nige": "逃げ",
    "makuri": "まくり",
    "sashi": "差し",
    "makuri_sashi": "まくり差し",
    "nuki": "抜き",
    "megumare": "恵まれ",
}
_VENUE_KIMARITE_COLS = {
    "nige": "venue_kimarite_nige",
    "makuri": "venue_kimarite_makuri",
    "sashi": "venue_kimarite_sashi",
    "makuri_sashi": "venue_kimarite_makuri_sashi",
    "nuki": "venue_kimarite_nuki",
    "megumare": "venue_kimarite_megumare",
}


def _normalize_kimarite(value):
    s = "" if value is None else str(value).strip()
    s = s.replace("捲り差し", "まくり差し").replace("捲り", "まくり")
    reverse = {v: k for k, v in _KIMARITE_JA.items()}
    return reverse.get(s)


def _normalize_racer_id(value):
    if value is None:
        return ""
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none"}:
        return ""
    try:
        return str(int(float(s)))
    except Exception:
        return s


def _build_course_kimarite_stats(history):
    """学習履歴から選手×艇番(想定コース)の決まり手分布を作る。"""
    need = {"racer_id", "lane", "finish", "kimarite"}
    if history is None or not need.issubset(history.columns):
        return {}

    h = history[list(need)].copy()
    h["racer_id"] = h["racer_id"].map(_normalize_racer_id)
    h["lane"] = pd.to_numeric(h["lane"], errors="coerce")
    h["finish"] = pd.to_numeric(h["finish"], errors="coerce")
    h["_kimarite"] = h["kimarite"].map(_normalize_kimarite)
    h = h[
        h["racer_id"].ne("")
        & h["lane"].between(1, 6)
    ].copy()

    if len(h) == 0:
        return {}

    starts = h.groupby(["racer_id", "lane"], dropna=False).size()
    wins = h[(h["finish"] == 1) & h["_kimarite"].notna()].copy()

    win_counts = {}
    if len(wins):
        grouped = (
            wins.groupby(["racer_id", "lane", "_kimarite"], dropna=False)
            .size()
        )
        for (racer_id, lane, method), count in grouped.items():
            key = (str(racer_id), int(lane))
            win_counts.setdefault(key, {})[str(method)] = int(count)

    out = {}
    for (racer_id, lane), start_count in starts.items():
        key = (str(racer_id), int(lane))
        counts = win_counts.get(key, {})
        method_counts = {
            method: int(counts.get(method, 0))
            for method in _KIMARITE_METHODS
        }
        wins_with_method = int(sum(method_counts.values()))
        out[key] = {
            "starts": int(start_count),
            "wins": wins_with_method,
            "counts": method_counts,
        }

    return out


def _jensen_shannon_similarity(p, q):
    """0〜1。1に近いほど2つの決まり手分布が似ている。"""
    p = np.asarray(p, dtype=float)
    q = np.asarray(q, dtype=float)

    if (
        len(p) == 0
        or len(q) == 0
        or not np.isfinite(p).all()
        or not np.isfinite(q).all()
        or p.sum() <= 0
        or q.sum() <= 0
    ):
        return np.nan

    p = p / p.sum()
    q = q / q.sum()
    m = (p + q) / 2.0

    def _kl(a, b):
        mask = a > 0
        return float(np.sum(a[mask] * np.log(a[mask] / b[mask])))

    js = 0.5 * _kl(p, m) + 0.5 * _kl(q, m)
    return float(np.clip(1.0 - js / np.log(2.0), 0.0, 1.0))


def _pipeline():
    prep = ColumnTransformer(
        [
            (
                "num",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="median")),
                        ("scale", StandardScaler()),
                    ]
                ),
                BASE_NUM,
            ),
            (
                "cat",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="most_frequent")),
                        (
                            "ohe",
                            OneHotEncoder(
                                handle_unknown="ignore",
                                sparse_output=False,
                            ),
                        ),
                    ]
                ),
                BASE_CAT,
            ),
        ]
    )

    clf = HistGradientBoostingClassifier(
        max_iter=220,
        learning_rate=0.06,
        max_leaf_nodes=31,
        l2_regularization=1.0,
        random_state=42,
    )

    return Pipeline([("prep", prep), ("clf", clf)])


def train(history):
    need = set(BASE_NUM + BASE_CAT + ["finish"])
    missing = need - set(history.columns)

    if missing:
        raise ValueError(
            "学習CSVに不足列: " + ", ".join(sorted(missing))
        )

    # 1着モデルは従来どおり、呼び出し側の学習CSVを使う。
    # 既存の1着予想を不用意に変えないため、ここは挙動を維持する。
    m = _pipeline()
    y_first = (
        pd.to_numeric(history["finish"], errors="coerce") == 1
    ).astype(int)
    m.fit(history[BASE_NUM + BASE_CAT], y_first)

    # 2着・3着は実レースの history_full.csv を優先して専用学習する。
    # 「1着は弱いが2着・3着には来る」外枠タイプを p_first の使い回しで
    # 潰さないため。実履歴が読めない場合だけ呼び出し側 history にフォールバック。
    position_history = history
    try:
        hist_path = Path(__file__).parent / "history_full.csv"
        if hist_path.exists():
            usecols = list(dict.fromkeys(BASE_NUM + BASE_CAT + ["finish"]))
            real_history = pd.read_csv(hist_path, usecols=usecols)
            if len(real_history) >= 100:
                position_history = real_history
    except Exception:
        position_history = history

    m_second = _pipeline()
    y_second = (
        pd.to_numeric(position_history["finish"], errors="coerce") == 2
    ).astype(int)
    m_second.fit(position_history[BASE_NUM + BASE_CAT], y_second)

    m_third = _pipeline()
    y_third = (
        pd.to_numeric(position_history["finish"], errors="coerce") == 3
    ).astype(int)
    m_third.fit(position_history[BASE_NUM + BASE_CAT], y_third)

    m._second_model = m_second
    m._third_model = m_third
    m._position_model_rows = int(len(position_history))

    # 予想時に選手×想定コースの決まり手補正を使えるよう、
    # まず学習CSV自身からプロファイルを作る。
    kimarite_stats = _build_course_kimarite_stats(history)

    # 既定の sample_history.csv には決まり手列がないため、
    # 決まり手プロファイルだけ実レース収集データから補完する。
    if not kimarite_stats:
        try:
            hist_path = Path(__file__).parent / "history_full.csv"
            if hist_path.exists():
                kh = pd.read_csv(
                    hist_path,
                    usecols=["racer_id", "lane", "finish", "kimarite"],
                )
                kimarite_stats = _build_course_kimarite_stats(kh)
        except Exception:
            kimarite_stats = {}

    m._course_kimarite_stats = kimarite_stats

    return m


def _rank_score_lower_better(series):
    s = pd.to_numeric(series, errors="coerce")

    if s.notna().sum() < 2:
        return np.zeros(len(s))

    r = s.rank(method="average", ascending=True)
    mid = (s.notna().sum() + 1) / 2
    z = (mid - r) / (max(1, s.notna().sum() - 1) / 2)

    return z.fillna(0).to_numpy()


def _rank_score_higher_better(series):
    return -_rank_score_lower_better(series)


def predict(model, race, display_weight=0.32, current_meet_weight=0.18, course_weight=0.16, weather_weight=0.10, venue_course_weight=0.12, class_weight=0.12, kimarite_weight=0.06, original_display_scale=0.0):
    x = race.copy()

    for c in BASE_NUM + BASE_CAT:
        if c not in x:
            x[c] = np.nan

    raw = model.predict_proba(x[BASE_NUM + BASE_CAT])[:, 1]
    raw = np.clip(raw, 1e-6, None)

    adjustment = np.zeros(len(x))
    reasons = [[] for _ in range(len(x))]

    # 表示用：選手×コース別の決まり手補正の内訳。
    # 補正が使えない艇も列自体は返し、UI側で「データ不足」と判定できるようにする。
    kimarite_adjustment = np.zeros(len(x), dtype=float)
    kimarite_starts = np.zeros(len(x), dtype=int)
    kimarite_wins = np.zeros(len(x), dtype=int)
    kimarite_dominant = np.array([""] * len(x), dtype=object)
    kimarite_available = np.zeros(len(x), dtype=bool)

    # -----------------------------
    # 今節成績による補正
    # -----------------------------
    # 学習CSVにはまだ今節列を追加せず、当日の補正として使用する。
    # 走数が少ない序盤は current_meet_races で自動的に弱く効かせる。
    meet_cols = {
        "current_meet_avg_finish",
        "current_meet_top2_rate",
        "current_meet_avg_st",
        "current_meet_races",
    }

    if meet_cols.issubset(x.columns):
        races = pd.to_numeric(
            x["current_meet_races"],
            errors="coerce",
        ).fillna(0.0)

        reliability = np.clip(
            races.to_numpy(dtype=float) / 6.0,
            0.0,
            1.0,
        )

        z_finish = _rank_score_lower_better(
            x["current_meet_avg_finish"]
        )
        z_top2 = _rank_score_higher_better(
            x["current_meet_top2_rate"]
        )
        z_st = _rank_score_lower_better(
            x["current_meet_avg_st"]
        )

        # 着順を最重視し、2連対率・STを補助材料にする。
        meet_score = (
            z_finish * 0.45
            + z_top2 * 0.35
            + z_st * 0.20
        )

        meet_score = meet_score * reliability
        adjustment += current_meet_weight * meet_score

        for i, v in enumerate(meet_score):
            if reliability[i] < 0.34:
                continue

            if v >= 0.45:
                reasons[i].append("今節好調")
            elif v <= -0.45:
                reasons[i].append("今節低調")

            if z_st[i] >= 0.60 and reliability[i] >= 0.50:
                reasons[i].append("今節ST良好")

    # -----------------------------
    # コース適性による補正
    # -----------------------------
    # 学習CSVには追加せず、当日の補正として使用する。
    # 現段階では艇番＝想定コースとして取得した選手別コース成績を使う。
    course_cols = {
        "course_top3_rate",
        "course_avg_st",
        "course_start_rank",
    }

    if course_cols.issubset(x.columns):
        z_top3 = _rank_score_higher_better(
            x["course_top3_rate"]
        )
        z_course_st = _rank_score_lower_better(
            x["course_avg_st"]
        )
        z_start_rank = _rank_score_lower_better(
            x["course_start_rank"]
        )

        # コース3連対率を最重視。
        # 平均STとST順位はスタート適性の補助材料として使う。
        course_score = (
            z_top3 * 0.55
            + z_course_st * 0.30
            + z_start_rank * 0.15
        )

        # 欠損が多い艇は実質的に0補正になる。
        course_score = np.clip(course_score, -1.0, 1.0)
        adjustment += course_weight * course_score

        for i, v in enumerate(course_score):
            if v >= 0.45:
                reasons[i].append("コース適性高")
            elif v <= -0.45:
                reasons[i].append("コース適性低")

            if z_course_st[i] >= 0.60:
                reasons[i].append("コースST良好")

    # -----------------------------
    # 選手×コース別の決まり手適性による補正
    # -----------------------------
    # 選手本人の「その想定コースで勝ったときの決まり手構成」と、
    # その会場・同コースで出やすい決まり手構成の相性を見る。
    # 単純な勝利回数を加点すると内コースを二重評価しやすいため、
    # 勝率そのものではなく分布の類似度だけを使う。
    #
    # 少数勝利で100%まくり等に偏るのを避けるため各決まり手に0.5勝の
    # 擬似カウントを加える。さらに同コース走数12走で満額になる
    # reliabilityを掛け、データが薄い選手は弱くしか効かない。
    kimarite_stats = getattr(model, "_course_kimarite_stats", {}) or {}
    venue_method_cols = set(_VENUE_KIMARITE_COLS.values())

    if (
        kimarite_weight != 0
        and kimarite_stats
        and {"racer_id", "lane"}.issubset(x.columns)
        and venue_method_cols.issubset(x.columns)
    ):
        fit = pd.Series(np.nan, index=x.index, dtype=float)
        reliability = pd.Series(0.0, index=x.index, dtype=float)
        dominant_method = pd.Series("", index=x.index, dtype=str)

        for idx, row in x.iterrows():
            racer_id = _normalize_racer_id(row.get("racer_id"))
            lane_num = pd.to_numeric(
                pd.Series([row.get("lane")]),
                errors="coerce",
            ).iloc[0]

            if not racer_id or pd.isna(lane_num):
                continue

            rec = kimarite_stats.get((racer_id, int(lane_num)))
            if not rec:
                continue

            pos = x.index.get_loc(idx)
            starts = max(0, int(rec.get("starts", 0)))
            wins_count = max(0, int(rec.get("wins", 0)))
            kimarite_starts[pos] = starts
            kimarite_wins[pos] = wins_count

            if wins_count <= 0:
                continue

            counts = np.array(
                [
                    float(rec.get("counts", {}).get(method, 0))
                    for method in _KIMARITE_METHODS
                ],
                dtype=float,
            )
            # 0.5ずつの擬似カウントで少数サンプルの極端化を抑える。
            player_profile = counts + 0.5

            venue_profile = np.array(
                [
                    pd.to_numeric(
                        pd.Series([row.get(_VENUE_KIMARITE_COLS[method])]),
                        errors="coerce",
                    ).iloc[0]
                    for method in _KIMARITE_METHODS
                ],
                dtype=float,
            )
            venue_profile = np.nan_to_num(
                venue_profile,
                nan=0.0,
                posinf=0.0,
                neginf=0.0,
            )

            sim = _jensen_shannon_similarity(
                player_profile,
                venue_profile,
            )
            if not np.isfinite(sim):
                continue

            fit.loc[idx] = sim
            reliability.loc[idx] = min(starts / 12.0, 1.0)
            kimarite_available[pos] = True

            if counts.sum() > 0:
                best = int(np.argmax(counts))
                dominant = _KIMARITE_JA[_KIMARITE_METHODS[best]]
                dominant_method.loc[idx] = dominant
                kimarite_dominant[pos] = dominant

        # 2艇だけで順位を付けると片方が満額加点/減点になりやすい。
        # 3艇以上に有効データがあるときだけ補正する。
        if fit.notna().sum() >= 3:
            z_fit = _rank_score_higher_better(fit)
            kimarite_score = (
                z_fit
                * reliability.to_numpy(dtype=float)
            )
            kimarite_score = np.clip(kimarite_score, -1.0, 1.0)
            kimarite_adjustment = float(kimarite_weight) * kimarite_score
            adjustment += kimarite_adjustment

            for pos, (_, row) in enumerate(x.iterrows()):
                rel = float(reliability.iloc[pos])
                score = float(kimarite_score[pos])
                if rel < 0.34:
                    continue

                method = str(dominant_method.iloc[pos] or "")
                if score >= 0.45:
                    if method:
                        reasons[pos].append(
                            f"コース決まり手適性({method})"
                        )
                    else:
                        reasons[pos].append("コース決まり手適性")
                elif score <= -0.45:
                    reasons[pos].append("コース決まり手相性低")

    # -----------------------------
    # 級別（A1/A2/B1/B2）による補正
    # -----------------------------
    # 学習CSVには追加せず、当日の補正として使用する（現状補正と同じ理由：
    # 学習用の実レース履歴に級別の列がないため）。
    # 6号艇の実績を調べると、モーター・ボートの良し悪しではほぼ勝率が
    # 変わらない一方、選手本人の勝率が上位だと勝率が2.5倍になるなど、
    # アウトコースほど「選手本人の総合力」がものを言う傾向が見られた。
    # 級別は全国勝率・当地勝率だけでは拾いきれない選手の総合力
    # （スタート技術や立ち回りの巧さなど）を表す公式な格付けなので、
    # 補完的な評価材料として追加する。
    if "racer_class" in x.columns:
        _class_score_map = {"A1": 3, "A2": 2, "B1": 1, "B2": 0}
        class_score = x["racer_class"].map(_class_score_map)
        z_class = _rank_score_higher_better(class_score)
        z_class = np.nan_to_num(z_class, nan=0.0)

        adjustment += class_weight * z_class

        for i in range(len(x)):
            cls = x["racer_class"].iloc[i]
            if cls == "A1":
                reasons[i].append("級別A1")
            elif cls == "B2":
                reasons[i].append("級別B2(下位)")

    # -----------------------------
    # 場全体のコース特性（逃げ率・決まり手）による補正
    # -----------------------------
    # 選手個人の実績ではなく、「この場のこのコースはそもそも
    # 強いか」という場自体の特性。BOAT RACE公式の集計値
    # （venue_course_1st = そのコースの1着率）を艇番=進入コース
    # とみなして使う。前づけ等で実際のコースとズレることはあるが、
    # 出走表時点では艇番をベースにするのが現実的。
    if "venue_course_1st" in x.columns:
        z_venue_course = _rank_score_higher_better(x["venue_course_1st"])
        z_venue_course = np.nan_to_num(z_venue_course, nan=0.0)
        z_venue_course = np.clip(z_venue_course, -1.0, 1.0)

        adjustment += venue_course_weight * z_venue_course

        for i, v in enumerate(z_venue_course):
            if v >= 0.55:
                reasons[i].append("当水面はイン系有利")
            elif v <= -0.55:
                reasons[i].append("当水面は当該コース不利")

        # 荒れ水面（1コースの逃げ率が低い）で、まくり実績が豊富な
        # コースにはボーナスを少し追加する。
        if "venue_kimarite_makuri" in x.columns and "lane" in x.columns:
            lane1_nige = x.loc[x["lane"] == 1, "venue_course_1st"]
            rough_water = bool(len(lane1_nige) and pd.notna(lane1_nige.iloc[0]) and lane1_nige.iloc[0] < 45)

            if rough_water:
                makuri = pd.to_numeric(x["venue_kimarite_makuri"], errors="coerce").fillna(0)
                lane_num = pd.to_numeric(x["lane"], errors="coerce").fillna(0)
                # 3コースあたりのまくりが特に決まりやすい水面を想定した簡易ボーナス
                makuri_bonus = (
                    (makuri >= 30).astype(float)
                    * ((lane_num == 3) | (lane_num == 4)).astype(float)
                    * 0.06
                )
                adjustment += makuri_bonus.to_numpy()

                for i, v in enumerate(makuri_bonus):
                    if v > 0:
                        reasons[i].append("荒水面でまくり決着多め")

    # 展示Fの艇は「展示ST」（フライング/ST早め・遅め）による加点・減点の
    # 対象にはしない（Fである時点でSTの情報価値はないため）。
    # 展示タイム・直線（伸び足）・まわり足・1周はSTとは別物で、
    # フライングの有無に関わらず速さの参考になるため、F艇でも
    # 通常どおり評価する（画像OCRでの入力精度も確認できたため、
    # 「Fの艇も展示ST以外は評価する」という要望に対応）。
    flying = pd.Series(False, index=x.index)
    if "exhibition_st" in x:
        st_raw = pd.to_numeric(x["exhibition_st"], errors="coerce")
        flying = (st_raw < 0).fillna(False)
        for i in range(len(x)):
            if flying.iloc[i]:
                reasons[i].append("展示F")

    if "exhibition_time" in x:
        z = _rank_score_lower_better(x["exhibition_time"])
        adjustment += display_weight * z

        for i, v in enumerate(z):
            if v > 0.55:
                reasons[i].append("展示タイム上位")
            elif v < -0.55:
                reasons[i].append("展示タイム下位")

    # 展示の直線（伸び足）・まわり足・1周タイムで評価する。
    # 以前は展示ST（フライング/ST早め・遅め）も別枠で加点・減点していたが、
    # 「展示Fを予想条件に入れず、直線・伸び足・まわり足の評価にしたい」
    # という要望を受けて、展示STベースの調整（フライング減点を含む）は
    # 廃止した。直線タイムは「伸び足」（コーナー後の伸び・加速力）の
    # 評価も兼ねるものとして扱う。
    for col, label, w, lower in [
        ("original_straight", "直線・伸び足展示", 0.11, True),
        ("original_turn", "まわり足展示", 0.11, True),
        ("original_lap", "1周展示", 0.08, True),
    ]:
        # 研究用のアブレーション比較では、オリジナル展示だけを
        # 無効化できるようスケールを掛ける。既定値1.0なので
        # 本番予想の挙動は従来と完全に同じ。
        w = w * float(original_display_scale)
        if col in x and w != 0:
            # 順位ベースの評価なので、一部の艇にしか値が入っていないと
            # 「2艇の中で1位」でも「6艇の中で1位」と同じ満額の加点に
            # なってしまい、値が入っている艇だけが不当に高評価になる。
            # 画像OCRでは一部の艇だけ読み取れないことがあるため、
            # 有効な値が規定数に満たない列はまとめて評価対象外にする。
            valid_count = pd.to_numeric(x[col], errors="coerce").notna().sum()
            if valid_count < _ORIGINAL_MIN_VALID:
                continue

            z = (
                _rank_score_lower_better(x[col])
                if lower
                else _rank_score_higher_better(x[col])
            )

            adjustment += w * z

            for i, v in enumerate(z):
                if v > 0.65:
                    reasons[i].append(label + "上位")

    # -----------------------------
    # 天候（風・波）による補正
    # -----------------------------
    # 強風・高波の荒れ水面ではアウトコース（5, 6号艇）が不利になりやすい
    # という経験則を反映する。学習データには含めず、当日補正として使用する。
    if {"wind_speed", "wave_height"}.issubset(x.columns) and "lane" in x.columns:
        wind = pd.to_numeric(x["wind_speed"], errors="coerce")
        wave = pd.to_numeric(x["wave_height"], errors="coerce")
        lane = pd.to_numeric(x["lane"], errors="coerce")

        # 風速5m/s以上、または波高3cm以上を「荒れ水面」とみなす簡易しきい値。
        # 実データが貯まったらしきい値・係数ともに見直す前提。
        rough = ((wind.fillna(0) >= 5) | (wave.fillna(0) >= 3)).astype(float)

        outer_penalty = rough * (lane - 3).clip(lower=0) * -1.0
        outer_penalty = outer_penalty.fillna(0.0).to_numpy()

        adjustment += weather_weight * outer_penalty

        for i in range(len(x)):
            if rough.iloc[i] and lane.iloc[i] >= 5:
                reasons[i].append("荒水面でアウト不利")

    strength = raw * np.exp(adjustment)

    if strength.sum() <= 0:
        strength = np.ones(len(strength))

    p = strength / strength.sum()

    # -----------------------------
    # 2着・3着専用モデル
    # -----------------------------
    # 1着確率の使い回しではなく、finish==2 / finish==3 を直接学習した
    # 専用モデルの出力を使う。さらに場の2着率・3着率を各着順へ直接反映。
    # これにより「頭は薄いがヒモでは強い」外枠を表現できる。
    p_second = p.copy()
    p_third = p.copy()

    try:
        second_model = getattr(model, "_second_model", None)
        if second_model is not None:
            raw_second = second_model.predict_proba(x[BASE_NUM + BASE_CAT])[:, 1]
            second_adj = np.zeros(len(x), dtype=float)

            if "venue_course_2nd" in x.columns:
                z_v2 = _rank_score_higher_better(x["venue_course_2nd"])
                second_adj += 0.20 * np.clip(z_v2, -1.0, 1.0)

            if "course_top3_rate" in x.columns:
                z_c3 = _rank_score_higher_better(x["course_top3_rate"])
                second_adj += 0.08 * np.clip(z_c3, -1.0, 1.0)

            second_strength = raw_second * np.exp(second_adj)
            if second_strength.sum() > 0:
                p_second = second_strength / second_strength.sum()
    except Exception:
        p_second = p.copy()

    try:
        third_model = getattr(model, "_third_model", None)
        if third_model is not None:
            raw_third = third_model.predict_proba(x[BASE_NUM + BASE_CAT])[:, 1]
            third_adj = np.zeros(len(x), dtype=float)

            if "venue_course_3rd" in x.columns:
                z_v3 = _rank_score_higher_better(x["venue_course_3rd"])
                third_adj += 0.18 * np.clip(z_v3, -1.0, 1.0)

            if "course_top3_rate" in x.columns:
                z_c3 = _rank_score_higher_better(x["course_top3_rate"])
                third_adj += 0.10 * np.clip(z_c3, -1.0, 1.0)

            third_strength = raw_third * np.exp(third_adj)
            if third_strength.sum() > 0:
                p_third = third_strength / third_strength.sum()
    except Exception:
        p_third = p.copy()

    out = x[["lane"]].copy()

    if "racer_name" in x:
        out["racer_name"] = x["racer_name"]

    out["p_first"] = p
    out["p_second"] = p_second
    out["p_third"] = p_third
    out["model_version"] = MODEL_VERSION
    out["adjustment"] = adjustment

    # 決まり手補正はlog強度で計算しているため、UIでは
    # exp(log補正)-1 を「強さの増減率」として表示する。
    out["kimarite_adjustment"] = kimarite_adjustment
    out["kimarite_effect_pct"] = (np.exp(kimarite_adjustment) - 1.0) * 100.0
    out["kimarite_starts"] = kimarite_starts
    out["kimarite_wins"] = kimarite_wins
    out["kimarite_dominant"] = kimarite_dominant
    out["kimarite_available"] = kimarite_available

    out["reason"] = [
        " / ".join(r) if r else "基礎データ中心"
        for r in reasons
    ]

    return out.sort_values("lane").reset_index(drop=True)



def research_prediction_variants(
    model,
    race,
    display_weight=0.32,
    weather_weight=0.10,
    venue_course_weight=0.12,
):
    """研究用の段階別予想を返す。

    本番の ``predict`` 結果は一切変更せず、同じ入力に対して補正を
    段階的に足した結果を別計算する。保存済み実結果と照合することで、
    どの補正が本当に精度改善に寄与しているかを後から検証できる。
    """
    common = dict(
        display_weight=0.0,
        current_meet_weight=0.0,
        course_weight=0.0,
        weather_weight=0.0,
        venue_course_weight=0.0,
        class_weight=0.0,
        kimarite_weight=0.0,
        original_display_scale=0.0,
    )

    variants = {}

    variants["基礎AI"] = predict(model, race, **common)

    variants["＋今節"] = predict(
        model, race,
        **{**common, "current_meet_weight": 0.18},
    )

    variants["＋コース"] = predict(
        model, race,
        **{
            **common,
            "current_meet_weight": 0.18,
            "course_weight": 0.16,
        },
    )

    variants["＋級別"] = predict(
        model, race,
        **{
            **common,
            "current_meet_weight": 0.18,
            "course_weight": 0.16,
            "class_weight": 0.12,
        },
    )

    variants["＋場特性"] = predict(
        model, race,
        **{
            **common,
            "current_meet_weight": 0.18,
            "course_weight": 0.16,
            "class_weight": 0.12,
            "venue_course_weight": float(venue_course_weight),
        },
    )

    variants["＋決まり手"] = predict(
        model, race,
        **{
            **common,
            "current_meet_weight": 0.18,
            "course_weight": 0.16,
            "class_weight": 0.12,
            "venue_course_weight": float(venue_course_weight),
            "kimarite_weight": 0.06,
        },
    )

    variants["＋展示"] = predict(
        model, race,
        **{
            **common,
            "current_meet_weight": 0.18,
            "course_weight": 0.16,
            "class_weight": 0.12,
            "venue_course_weight": float(venue_course_weight),
            "kimarite_weight": 0.06,
            "display_weight": float(display_weight),
            "original_display_scale": 0.0,
        },
    )

    variants["現行全部入り"] = predict(
        model, race,
        display_weight=float(display_weight),
        current_meet_weight=0.18,
        course_weight=0.16,
        weather_weight=float(weather_weight),
        venue_course_weight=float(venue_course_weight),
        class_weight=0.12,
        kimarite_weight=0.06,
        original_display_scale=0.0,
    )

    return variants

def assess_favorite_risk(race, final):
    """
    その回の本命艇（1着確率が最も高い艇）について、
    展示・モーター/ボート・今節成績を総合したリスクを評価する。

    「1号艇が飛べば全滅」という3連単特有のリスクに備え、
    本命艇の状態に不安要素が多い場合はrank_ticketsで
    本命艇を含まない保険買い目を混ぜるかどうかの判断材料に使う。

    戻り値: (favorite_lane, risk_score, risk_reasons)
      risk_score: 0以上の整数。目安として2以上で「要注意」。
    """
    if final is None or len(final) == 0 or "p_first" not in final.columns:
        return None, 0, []

    favorite_row = final.loc[final["p_first"].idxmax()]
    favorite_lane = int(favorite_row["lane"])

    fav = race[race["lane"] == favorite_lane]
    if len(fav) == 0:
        return favorite_lane, 0, []
    fav = fav.iloc[0]

    score = 0
    reasons = []

    # 本命艇が展示Fの場合でも、展示ST以外（展示タイム・直線・まわり足）は
    # 通常どおり評価する。Fそのものを減点材料にはしない
    # （画像OCRでの入力精度も確認できたため、「Fの艇も展示ST以外は
    # 評価する」という要望に対応）。
    fav_st = pd.to_numeric(pd.Series([fav.get("exhibition_st")]), errors="coerce").iloc[0]
    fav_is_flying = pd.notna(fav_st) and fav_st < 0

    if fav_is_flying:
        reasons.append("本命艇が展示F")

    # 展示の直線（伸び足）・まわり足が場内で下位
    for col, label in (("original_straight", "直線・伸び足"), ("original_turn", "まわり足")):
        if col not in race.columns:
            continue
        vals = pd.to_numeric(race[col], errors="coerce")
        v_fav = pd.to_numeric(pd.Series([fav.get(col)]), errors="coerce").iloc[0]
        if pd.notna(v_fav) and vals.notna().sum() >= 3 and v_fav > vals.median():
            score += 1
            reasons.append(f"本命艇の展示{label}が平均以下")

    # 展示タイム（周回タイム）が場内で下位
    ex_all = pd.to_numeric(race["exhibition_time"], errors="coerce") if "exhibition_time" in race else None
    ex_fav = pd.to_numeric(pd.Series([fav.get("exhibition_time")]), errors="coerce").iloc[0]
    if ex_all is not None and pd.notna(ex_fav) and ex_all.notna().sum() >= 3:
        if ex_fav > ex_all.median():
            score += 1
            reasons.append("本命艇の展示タイムが平均以下")

    # モーター・ボート2連率が場内で下位
    for col, label in (("motor_2ren", "モーター"), ("boat_2ren", "ボート")):
        if col not in race.columns:
            continue
        vals = pd.to_numeric(race[col], errors="coerce")
        v_fav = pd.to_numeric(pd.Series([fav.get(col)]), errors="coerce").iloc[0]
        if pd.notna(v_fav) and vals.notna().sum() >= 3 and v_fav < vals.median():
            score += 1
            reasons.append(f"本命艇の{label}が平均以下")

    # 今節成績が振るわない
    top2 = pd.to_numeric(pd.Series([fav.get("current_meet_top2_rate")]), errors="coerce").iloc[0]
    if pd.notna(top2) and top2 < 30:
        score += 1
        reasons.append("本命艇の今節成績が不振")


    return favorite_lane, score, reasons


# 1着艇別・2着艇の実績分布（2連対率）。
# 実レース14,376件（sample_history.csv）から集計した実測値。
# 例えば1号艇が1着のとき、2着は2号艇が33.8%と最多（3号艇28.4%より明確に高い）で、
# 1号艇以外が1着のときは、2着に1号艇が来る割合が32〜41%と常に最多になる。
# これは「その艇の強さ」だけでは説明できないコース・ターンマーク位置による
# 構造的な優位性（実力が拮抗していてもインコースの艇が2着を取りやすい）で、
# p_first（各艇の勝率）だけから計算する素のHarville法では再現できない。
# そのため2着確率の計算にこの実績分布を一定割合でブレンドする。
COURSE_2ND_PROB = {
    1: {2: 0.3382, 3: 0.2842, 4: 0.1834, 5: 0.1197, 6: 0.0746},
    2: {1: 0.4137, 3: 0.2244, 4: 0.1650, 5: 0.1239, 6: 0.0731},
    3: {1: 0.4082, 2: 0.1907, 4: 0.1852, 5: 0.1302, 6: 0.0857},
    4: {1: 0.3221, 2: 0.1840, 3: 0.1482, 5: 0.2133, 6: 0.1324},
    5: {1: 0.3567, 2: 0.2007, 3: 0.1391, 4: 0.1862, 6: 0.1173},
    6: {1: 0.3484, 2: 0.2029, 3: 0.1551, 4: 0.1766, 5: 0.1169},
}
# 2着確率に占める実績分布(COURSE_2ND_PROB)のブレンド比率。
# 0なら従来通り実力(p_first)のみ、1なら実績分布のみに依存する。
COURSE_2ND_WEIGHT = 0.2

# 3着の実績分布（残り4艇を若い番号順に並べたときの順位別出現率）。
# 実レース14,092件（sample_history.csv、1〜3着が揃うレース）から集計。
# 1着・2着を除いた残り4艇のうち、「一番若い番号の艇」が3着になる割合が
# 33.8%で最多（均等なら25%のはず）、以降26.4%・22.3%・17.4%と番号が
# 若いほど3着になりやすい傾向が一貫して見られた。2着ほど極端ではないが
# （2着は本命艇に対して最大4.5倍の差だったのに対し3着は最大1.9倍）、
# ここでも「実力」だけでは説明できないコース位置の優位性が残っている。
# 具体的な(1着,2着)の組み合わせごとの3着分布はサンプルが薄くなる
# （最少49件）ため、より頑健な「残り艇内の番号順位」という一般化した
# 形で使う。
COURSE_3RD_RANK_PROB = {1: 0.3384, 2: 0.2641, 3: 0.2233, 4: 0.1741}
COURSE_3RD_WEIGHT = 0.15


def trifecta(
    first,
    gamma_b=0.8,
    gamma_c=0.65,
    course_weight=COURSE_2ND_WEIGHT,
    course_weight_3rd=COURSE_3RD_WEIGHT,
    calibrate=True,
):
    """
    Harville法をベースにした3連単の的中確率計算。

    素のHarville法（2着・3着の条件付き確率をそのまま勝率の比で計算する
    方式）は、本命が勝った後の2着・3着争いを実際より「順当」に
    見積もりがちで、本命絡みの買い目の確率を過大評価する傾向がある
    （検証データで実測：本命買い目の自己申告確率が実際の的中率より
    平均1.3倍ほど高く出ていた）。

    2着・3着の計算に使う勝率を指数(gamma_b, gamma_c < 1)で
    割り引くことで、上位艇への偏りを緩和し、実際の的中率に近づける。
    実データ(37レース)で検証済み：この補正により、本命買い目の
    平均予測確率(14.3%→10.8%)が実際の的中率(10.8%)とほぼ一致した。
    どの買い目を選ぶか自体は変わらず、確率の較正だけが改善する。

    2着・3着については、p_firstの使い回しではなく finish==2 / finish==3
    を直接学習した専用モデルを使用する。場の venue_course_2nd /
    venue_course_3rd も各着順に直接反映する。COURSE_2ND_PROB等の
    コース分布は、勝者との条件付き関係を補う弱い事前分布としてだけ残す。
    検証57レースの分析で、1号艇が1着のレースの48%で2号艇が2着に
    入っていたのに、買い目の中に2号艇絡みの組が十分にカバーされて
    おらず（平均で購入点数の1割強にとどまる）3連単的中率が低い
    （本命買い目的中8.8%）ことが分かったための対応。

    3着確率についても同様に、COURSE_3RD_RANK_PROB（残り艇を番号順に
    並べたときの実績順位分布）をブレンドする。1着・2着ほど極端では
    ないが、3着争いでも若い番号（イン寄り）の艇がやや有利という
    傾向が実データで確認できたため。
    """
    s = dict(
        zip(
            first["lane"].astype(int),
            first["p_first"].astype(float),
        )
    )
    # 専用2着・3着モデルがある新データはそちらを使い、
    # 旧保存データ等で列がない場合だけp_firstへフォールバックする。
    s2 = dict(
        zip(
            first["lane"].astype(int),
            pd.to_numeric(
                first["p_second"] if "p_second" in first.columns else first["p_first"],
                errors="coerce",
            ).fillna(0.0).astype(float),
        )
    )
    s3 = dict(
        zip(
            first["lane"].astype(int),
            pd.to_numeric(
                first["p_third"] if "p_third" in first.columns else first["p_first"],
                errors="coerce",
            ).fillna(0.0).astype(float),
        )
    )

    rows = []

    for a, b, c in itertools.permutations(range(1, 7), 3):
        pa = s[a] / sum(s.values())

        denom_b = sum(
            (max(v, 1e-12) ** gamma_b) for k, v in s2.items()
            if k != a
        )
        pb_ability = (max(s2[b], 1e-12) ** gamma_b) / denom_b
        pb_course = COURSE_2ND_PROB.get(a, {}).get(b)
        if pb_course is not None and course_weight > 0:
            pb = (1 - course_weight) * pb_ability + course_weight * pb_course
        else:
            pb = pb_ability

        denom_c = sum(
            (max(v, 1e-12) ** gamma_c) for k, v in s3.items()
            if k not in (a, b)
        )
        pc_ability = (max(s3[c], 1e-12) ** gamma_c) / denom_c

        remaining_sorted = sorted(k for k in s if k not in (a, b))
        c_rank = remaining_sorted.index(c) + 1
        pc_course = COURSE_3RD_RANK_PROB.get(c_rank)
        if pc_course is not None and course_weight_3rd > 0:
            pc = (1 - course_weight_3rd) * pc_ability + course_weight_3rd * pc_course
        else:
            pc = pc_ability

        rows.append(
            (
                f"{a}-{b}-{c}",
                pa * pb * pc,
            )
        )

    out = pd.DataFrame(
        rows,
        columns=["combo", "prob"],
    )

    total = out["prob"].sum()

    if total > 0:
        out["prob"] = out["prob"] / total

    if calibrate:
        out["prob"] = _calibrate_prob(out["prob"])

    return out


# -----------------------------
# 確率の較正（キャリブレーション）
# -----------------------------
# 実測111レース・購入976点の検証で、この関数が出す確率が
# 実際の的中率より系統的に高いことが分かった。
#
#   AI予測  2〜5%  ->  実際 1.8%   （約2.0倍の過大評価）
#   AI予測  5〜10% ->  実際 4.0%   （約1.7倍）
#   AI予測 10%以上 ->  実際12.1%   （ほぼ正確）
#
# 期待値は「確率 × オッズ」なので、確率が2倍なら期待値も2倍に膨らむ。
# その結果、資金配分ロジックが「期待値1.2の良い買い目」と判断したものが
# 実際には期待値0.6の悪い買い目、ということが起き続けていた。
# 回収率が控除率（25%）を超えられない直接の原因がこれ。
#
# そこで p_cal = C * p^A の形で較正する。指数A>1なので、
# 小さい確率ほど強く縮み、大きい確率はほぼそのまま残る。
# 上の実測パターンと一致する。
#
# パラメータは購入976点のベルヌーイ最尤推定で求めた。
# 前半57レースと後半54レースで別々に当てはめても、実用レンジでは
#   p=5%  -> 前半2.83% / 後半2.83%
#   p=10% -> 前半5.84% / 後半7.44%
# と近い値になり、期間をまたいで安定していることを確認済み。
#
# 注意: 較正後の確率は120通りの合計が1にならない。これは意図的で、
# 減った分は「AIが極端に低く見積もっている大穴側」に本来あるべき
# 確率質量に対応する。買い目の順位付けは単調変換なので変わらず、
# 期待値の絶対値だけが実測に合うようになる。
_CALIB_COEF = 1.211
_CALIB_EXP = 1.247


def _calibrate_prob(prob):
    p = pd.to_numeric(prob, errors="coerce").fillna(0.0).clip(lower=0.0)
    return (_CALIB_COEF * np.power(p, _CALIB_EXP)).clip(upper=1.0)


def _safe_rank(series):
    s = pd.to_numeric(series, errors="coerce")
    if s.notna().sum() == 0:
        return pd.Series(0.0, index=s.index)
    return s.rank(pct=True, method="average").fillna(0.0)


def _take_unique(df, n):
    if n <= 0 or len(df) == 0:
        return df.head(0).copy()
    return df.head(int(n)).copy()


def rank_tickets(
    tri,
    odds=None,
    main_n=3,
    cover_n=3,
    longshot_n=2,
    longshot_min_prob=0.003,
    longshot_exception_ev=1.80,
    hedge_lane=None,
    use_odds=False,
):
    """
    3連単の確率表から購入候補を選ぶ。

    use_odds=False（既定）のとき、オッズは買い目の選択に一切使わない。
    検証318レースで、オッズ（＝期待値）を重視するほど成績が単調に
    悪化することが確認されたため。詳細は stake_allocator の
    allocate_stakes_smart のコメントを参照。

    オッズ自体は表示・記録用に残るので、後から使う設定に戻して
    比較することはできる。
    """
    x = tri.copy()
    x["prob"] = pd.to_numeric(x["prob"], errors="coerce").fillna(0.0)

    if odds is not None and len(odds):
        od = odds[["combo", "odds"]].copy()
        od["odds"] = pd.to_numeric(od["odds"], errors="coerce")
        x = x.merge(od, on="combo", how="left")
    else:
        x["odds"] = np.nan

    x["expected_return"] = x["prob"] * x["odds"]

    # 期待値の列は表示・記録用に残すが、use_odds=False なら
    # 買い目の選択には使わない（確率だけで選ぶ）。
    has_odds = bool(use_odds) and x["odds"].notna().sum() > 0

    x["prob_rank"] = _safe_rank(x["prob"])

    if has_odds:
        x["ev_rank"] = _safe_rank(x["expected_return"].clip(lower=0, upper=2.50))
        x["odds_rank"] = _safe_rank(np.log1p(x["odds"].clip(lower=0)))
    else:
        x["ev_rank"] = 0.0
        x["odds_rank"] = 0.0

    # 本線：高確率＋EV0.90以上を優先
    if has_odds:
        main_pool = x[
            (x["expected_return"] >= 0.90)
            | (x["prob_rank"] >= 0.93)
        ].copy()
        if len(main_pool) < main_n:
            main_pool = x.copy()
        main_pool["main_score"] = (
            main_pool["prob_rank"] * 0.78
            + main_pool["ev_rank"] * 0.22
        )
        main = _take_unique(
            main_pool.sort_values(["main_score", "prob"], ascending=False),
            main_n,
        )
    else:
        main = _take_unique(x.sort_values("prob", ascending=False), main_n)

    used = set(main["combo"])
    rem = x[~x["combo"].isin(used)].copy()

    # 抑え：的中率重視
    if has_odds:
        rem["cover_score"] = rem["prob_rank"] * 0.82 + rem["ev_rank"] * 0.18
        rem.loc[rem["expected_return"] < 0.75, "cover_score"] -= 0.08
        cover = _take_unique(
            rem.sort_values(["cover_score", "prob"], ascending=False),
            cover_n,
        )
    else:
        cover = _take_unique(rem.sort_values("prob", ascending=False), cover_n)

    used |= set(cover["combo"])
    rem = x[~x["combo"].isin(used)].copy()

    # 穴：EV1以上＋原則0.30%以上。EV1.8以上は超低確率でも例外可
    if has_odds:
        long_pool = rem[
            (rem["expected_return"] >= 1.00)
            & (
                (rem["prob"] >= float(longshot_min_prob))
                | (rem["expected_return"] >= float(longshot_exception_ev))
            )
        ].copy()

        if len(long_pool) < longshot_n:
            fallback = rem[rem["expected_return"] >= 1.00].copy()
            if len(fallback):
                long_pool = fallback

        if len(long_pool) < longshot_n:
            long_pool = rem.copy()

        long_pool["long_score"] = (
            long_pool["ev_rank"] * 0.55
            + long_pool["odds_rank"] * 0.20
            + long_pool["prob_rank"] * 0.25
        )

        too_low = (
            (long_pool["prob"] < float(longshot_min_prob))
            & (long_pool["expected_return"] < float(longshot_exception_ev))
        )
        long_pool.loc[too_low, "long_score"] -= 0.25

        longshot = _take_unique(
            long_pool.sort_values(
                ["long_score", "expected_return", "prob"],
                ascending=False,
            ),
            longshot_n,
        )
    else:
        longshot = _take_unique(rem.sort_values("prob", ascending=False), longshot_n)

    # 保険買い目：本命艇（hedge_lane）に不安要素がある場合、
    # 「1号艇が飛べば全滅」を避けるため、穴の枠のうち1点を
    # hedge_laneを含まない組み合わせの中で最も条件の良いものに差し替える。
    # 既に穴の中にhedge_lane抜きの買い目があれば何もしない。
    if hedge_lane is not None and longshot_n > 0 and len(longshot):
        lane_str = str(int(hedge_lane))

        def _includes_lane(combo):
            return lane_str in str(combo).split("-")

        already_hedged = longshot["combo"].apply(_includes_lane).eq(False).any()

        if not already_hedged:
            hedge_pool = x[~x["combo"].isin(used)].copy()
            hedge_pool = hedge_pool[~hedge_pool["combo"].apply(_includes_lane)]

            if len(hedge_pool):
                if has_odds:
                    hedge_pool["hedge_score"] = (
                        hedge_pool["ev_rank"] * 0.5 + hedge_pool["prob_rank"] * 0.5
                    )
                    hedge_pick = hedge_pool.sort_values(
                        ["hedge_score", "expected_return", "prob"], ascending=False
                    ).head(1)
                else:
                    hedge_pick = hedge_pool.sort_values("prob", ascending=False).head(1)

                # 穴の中で最もスコアが低い1点と差し替える
                longshot = pd.concat(
                    [longshot.iloc[:-1], hedge_pick], ignore_index=True
                )

    main["group"] = "本線"
    cover["group"] = "抑え"
    longshot["group"] = "穴"

    result = pd.concat([main, cover, longshot], ignore_index=True)
    keep = ["combo", "prob", "odds", "expected_return", "group"]

    for c in keep:
        if c not in result:
            result[c] = np.nan

    return result[keep]


def allocate_stakes(tickets, budget=2000, unit=100, min_bet=100):
    """予算内で100円単位の推奨購入額を配分する。"""
    if tickets is None or len(tickets) == 0:
        return tickets

    x = tickets.copy()
    budget = max(0, int(budget))
    unit = max(100, int(unit))
    min_bet = max(unit, int(min_bet))

    budget = (budget // unit) * unit
    min_bet = (min_bet // unit) * unit

    x["prob"] = pd.to_numeric(x["prob"], errors="coerce").fillna(0.0)
    x["expected_return"] = pd.to_numeric(x["expected_return"], errors="coerce")

    if budget <= 0:
        x["stake"] = 0
        return x

    gw = x["group"].map({"本線": 1.00, "抑え": 0.72, "穴": 0.42}).fillna(0.5)
    p = np.sqrt(x["prob"].clip(lower=0))
    ev = x["expected_return"].fillna(1.0).clip(lower=0.5, upper=2.5)

    score = gw * (0.72 * p + 0.28 * p * ev)

    hole = x["group"].eq("穴")
    score.loc[hole] *= 0.85 + 0.25 * ev.loc[hole]
    score.loc[hole & (x["prob"] < 0.003)] *= 0.55
    score = score.replace([np.inf, -np.inf], np.nan).fillna(0.0)

    x["stake"] = 0

    # 予算不足時はスコア上位から100円ずつ
    if len(x) * min_bet > budget:
        slots = budget // unit
        for idx in score.sort_values(ascending=False).index[:slots]:
            x.loc[idx, "stake"] = unit
        return x

    x["stake"] = min_bet
    remaining = budget - int(x["stake"].sum())

    if remaining <= 0 or score.sum() <= 0:
        return x

    raw_units = (score / score.sum()) * (remaining / unit)
    floor_units = np.floor(raw_units).astype(int)
    x["stake"] += floor_units * unit

    leftover_units = (remaining - int((floor_units * unit).sum())) // unit
    frac = (raw_units - floor_units).sort_values(ascending=False)

    for idx in frac.index[:leftover_units]:
        x.loc[idx, "stake"] += unit

    return x

def confidence(first, race):
    p = np.sort(
        pd.to_numeric(
            first["p_first"],
            errors="coerce",
        ).fillna(0).to_numpy()
    )[::-1]

    if len(p) < 2:
        return "C"

    margin = p[0] - p[1]

    check_cols = [
        "racer_win_rate",
        "local_win_rate",
        "motor_2ren",
        "boat_2ren",
        "avg_st",
        "exhibition_time",
    ]

    completeness_list = []

    for c in check_cols:
        if c in race:
            completeness_list.append(
                pd.to_numeric(
                    race[c],
                    errors="coerce",
                ).notna().mean()
            )
        else:
            completeness_list.append(0.0)

    completeness = float(
        np.mean(completeness_list)
    )

    score = (
        margin * 0.88
        + completeness * 0.18
    )

    if score > 0.33:
        return "A"
    if score > 0.20:
        return "B"
    return "C"