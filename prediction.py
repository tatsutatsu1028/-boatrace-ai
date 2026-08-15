from __future__ import annotations

import itertools
import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from comment_analyzer import total_comment_score


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


def predict(model, race, display_weight=0.32, comment_weight=0.18):
    x = race.copy()

    for c in BASE_NUM + BASE_CAT:
        if c not in x:
            x[c] = np.nan

    raw = model.predict_proba(x[BASE_NUM + BASE_CAT])[:, 1]
    raw = np.clip(raw, 1e-6, None)

    adjustment = np.zeros(len(x))
    reasons = [[] for _ in range(len(x))]

    if "exhibition_time" in x:
        z = _rank_score_lower_better(x["exhibition_time"])
        adjustment += display_weight * z

        for i, v in enumerate(z):
            if v > 0.55:
                reasons[i].append("展示タイム上位")
            elif v < -0.55:
                reasons[i].append("展示タイム下位")

    for col, label, w, lower in [
        ("original_straight", "直線展示", 0.11, True),
        ("original_turn", "まわり足展示", 0.11, True),
        ("original_lap", "1周展示", 0.08, True),
    ]:
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

    if "exhibition_st" in x:
        st = pd.to_numeric(x["exhibition_st"], errors="coerce")

        st_adj = np.where(
            st < 0,
            -0.38,
            np.where(
                st <= 0.08,
                0.22,
                np.where(
                    st <= 0.12,
                    0.12,
                    np.where(st > 0.22, -0.18, 0.0),
                ),
            ),
        )

        st_adj = np.nan_to_num(st_adj)
        adjustment += 0.16 * st_adj

        for i, v in enumerate(st):
            if pd.notna(v) and v < 0:
                reasons[i].append("展示F")
            elif pd.notna(v) and 0 <= v <= 0.08:
                reasons[i].append("展示ST早め")

    if "comment" in x:
        cs = np.array(
            [total_comment_score(v) for v in x["comment"]]
        )

        adjustment += comment_weight * cs

        for i, v in enumerate(cs):
            if v > 0.30:
                reasons[i].append("コメント好感")
            elif v < -0.30:
                reasons[i].append("コメント弱め")

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


def trifecta(first):
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
            v for k, v in s.items()
            if k != a
        )
        pb = s[b] / denom_b

        denom_c = sum(
            v for k, v in s.items()
            if k not in (a, b)
        )
        pc = s[c] / denom_c

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
