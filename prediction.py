from __future__ import annotations

import itertools
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

    m = _pipeline()

    y = (
        pd.to_numeric(history["finish"], errors="coerce") == 1
    ).astype(int)

    m.fit(history[BASE_NUM + BASE_CAT], y)

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


def predict(model, race, display_weight=0.32, current_meet_weight=0.18, course_weight=0.16, weather_weight=0.10, venue_course_weight=0.12, class_weight=0.12, original_display_scale=1.0):
    x = race.copy()

    for c in BASE_NUM + BASE_CAT:
        if c not in x:
            x[c] = np.nan

    raw = model.predict_proba(x[BASE_NUM + BASE_CAT])[:, 1]
    raw = np.clip(raw, 1e-6, None)

    adjustment = np.zeros(len(x))
    reasons = [[] for _ in range(len(x))]

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
        if col in x:
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

    out = x[["lane"]].copy()

    if "racer_name" in x:
        out["racer_name"] = x["racer_name"]

    out["p_first"] = p
    out["adjustment"] = adjustment
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

    variants["＋展示"] = predict(
        model, race,
        **{
            **common,
            "current_meet_weight": 0.18,
            "course_weight": 0.16,
            "class_weight": 0.12,
            "venue_course_weight": float(venue_course_weight),
            "display_weight": float(display_weight),
            "original_display_scale": 1.0,
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
        original_display_scale=1.0,
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
COURSE_2ND_WEIGHT = 0.4

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
COURSE_3RD_WEIGHT = 0.3


def trifecta(
    first,
    gamma_b=0.8,
    gamma_c=0.65,
    course_weight=COURSE_2ND_WEIGHT,
    course_weight_3rd=COURSE_3RD_WEIGHT,
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

    さらに2着確率については、実力(p_first)だけのHarville推定に
    COURSE_2ND_PROB（実績ベースのコース別2連対分布）をブレンドする。
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

    rows = []

    for a, b, c in itertools.permutations(range(1, 7), 3):
        pa = s[a] / sum(s.values())

        denom_b = sum(
            (v ** gamma_b) for k, v in s.items()
            if k != a
        )
        pb_ability = (s[b] ** gamma_b) / denom_b
        pb_course = COURSE_2ND_PROB.get(a, {}).get(b)
        if pb_course is not None and course_weight > 0:
            pb = (1 - course_weight) * pb_ability + course_weight * pb_course
        else:
            pb = pb_ability

        denom_c = sum(
            (v ** gamma_c) for k, v in s.items()
            if k not in (a, b)
        )
        pc_ability = (s[c] ** gamma_c) / denom_c

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

    return out


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
):
    x = tri.copy()
    x["prob"] = pd.to_numeric(x["prob"], errors="coerce").fillna(0.0)

    if odds is not None and len(odds):
        od = odds[["combo", "odds"]].copy()
        od["odds"] = pd.to_numeric(od["odds"], errors="coerce")
        x = x.merge(od, on="combo", how="left")
    else:
        x["odds"] = np.nan

    x["expected_return"] = x["prob"] * x["odds"]
    has_odds = x["odds"].notna().sum() > 0

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