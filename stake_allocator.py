
import math
import numpy as np
import pandas as pd


GROUP_WEIGHT = {
    "本線": 1.00,
    "抑え": 0.62,
    "穴": 0.30,
}

# value_bias=1.0（妙味を最重視）のときの重み。
# 本線/抑え/穴という「カテゴリの序列」そのものを弱め、
# 期待値（＝市場オッズに対するAIの優位性＝妙味）で勝負できるようにする。
GROUP_WEIGHT_VALUE = {
    "本線": 1.00,
    "抑え": 0.88,
    "穴": 0.78,
}


def _safe_num(v, default=np.nan):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _round_to_unit(value, unit):
    unit = max(int(unit), 1)
    return int(round(float(value) / unit) * unit)


def allocate_stakes_smart(
    tickets,
    budget=2000,
    unit=100,
    min_bet=100,
    max_longshot_share=0.15,
    max_ticket_share=0.35,
    ev_floor=0.85,
    hard_ev_cut=0.55,
    very_low_prob=0.003,
    value_bias=0.0,
):
    """
    実戦寄りの資金配分。

    方針
    ----
    - 本線 > 抑え > 穴 の優先度（value_biasを上げるほどこの序列は弱まる）
    - 的中確率が極端に低い買い目は強く減点
    - EVが1未満でも本線/抑えは完全排除しないが減額
    - EVが著しく低い買い目は見送り候補
    - 穴全体の購入額を予算の一定割合以内に制限
    - 1点への集中も制限
    - 最低購入額・100円単位などに丸める

    value_bias : float (0.0〜1.0)
        「妙味重視度」。0なら従来通り（本線を厚めに、確率重視）。
        1に近づくほど、カテゴリの序列よりも期待値（市場オッズに対する
        優位性）を重視した配分になる。回収率を狙うなら高め、
        的中率の安定を狙うなら低め。

    ev_floor / hard_ev_cut : float
        経緯: 一度 ev_floor=0.65, hard_ev_cut=0.40 まで引き下げたことが
        ある。検証57レースで「的中した9点のうち7点がEV0.78〜0.98で、
        EV1未満というだけで減額されていた」ことが理由だった。

        しかしその後111レースまで検証を広げたところ、判断の前提が
        崩れた。prediction.trifecta() が出す確率が実際の的中率より
        系統的に約2倍高く、期待値そのものが水増しされていた
        （AI予測2〜5%の帯で実際の的中率は1.8%）。
        つまり「EV0.78の買い目」の真のEVは0.4程度であり、
        引き下げは悪い買い目を通しただけだった。実測でもEV0.75〜1.0の
        帯の回収率は41.8%と最も悪い部類だった。

        確率側を _calibrate_prob() で較正したうえで、閾値は元の
        0.85 / 0.55 に戻している。較正後の期待値に対して適用されるので、
        以前と同じ数値でも意味が変わっている点に注意。

        なお、この111レースのデータで閾値自体を最適化してはいけない。
        「EV2.0以上を除外」のような条件は通算では回収率121%に見えるが、
        前半152.6% / 後半48.5% と期間で全く再現せず、ノイズを拾って
        いるだけだった。
    """
    if tickets is None or len(tickets) == 0:
        out = pd.DataFrame(columns=["combo", "group", "prob", "odds", "expected_return", "stake"])
        return out

    df = tickets.copy()
    budget = int(budget)
    unit = int(unit)
    min_bet = int(min_bet)
    value_bias = float(np.clip(value_bias, 0.0, 1.0))

    if budget <= 0:
        df["stake"] = 0
        return df

    for col in ["prob", "odds", "expected_return"]:
        if col not in df.columns:
            df[col] = np.nan

    df["prob"] = pd.to_numeric(df["prob"], errors="coerce").fillna(0).clip(lower=0)
    df["odds"] = pd.to_numeric(df["odds"], errors="coerce")
    df["expected_return"] = pd.to_numeric(df["expected_return"], errors="coerce")

    # EVが無い場合は odds * prob で補完
    missing_ev = df["expected_return"].isna()
    df.loc[missing_ev, "expected_return"] = (
        df.loc[missing_ev, "prob"] * df.loc[missing_ev, "odds"]
    )

    # 確率スコアの指数。sqrt(=0.5)は本線偏重寄り。
    # value_biasを上げるほど指数を下げ、確率差による支配力を弱める
    # （＝期待値の差が相対的に効きやすくする）。
    prob_exponent = 0.5 - 0.15 * value_bias

    scores = []

    for _, row in df.iterrows():
        group = str(row.get("group", "抑え"))
        prob = _safe_num(row.get("prob"), 0.0)
        ev = _safe_num(row.get("expected_return"), np.nan)

        gw_base = GROUP_WEIGHT.get(group, 0.50)
        gw_value = GROUP_WEIGHT_VALUE.get(group, 0.80)
        group_w = (1 - value_bias) * gw_base + value_bias * gw_value

        # 基礎: 的中確率を評価（value_biasが高いほど指数を下げて影響を弱める）
        prob_score = max(prob, 0.0) ** prob_exponent

        # EV補正
        if math.isnan(ev):
            ev_mult = 0.80
        elif ev < hard_ev_cut:
            ev_mult = 0.18
        elif ev < ev_floor:
            ev_mult = 0.45
        elif ev < 1.00:
            ev_mult = 0.72
        elif ev < 1.30:
            ev_mult = 1.00
        elif ev < 1.80:
            ev_mult = 1.10
        elif ev < 2.50:
            ev_mult = 1.16
        else:
            # 高EVを無制限に持ち上げない（ベース）
            ev_mult = 1.20

        # value_biasが高いほど、高EVの妙味をさらに積み増す
        # （EV1.0を上回った分だけ、最大で+0.9倍まで加算）
        edge = max(0.0, ev - 1.0) if not math.isnan(ev) else 0.0
        ev_mult *= 1.0 + value_bias * min(edge, 1.5) * 0.6

        # 超低確率ペナルティ（value_biasが高いほど、妙味があれば少し緩める）
        if prob < 0.001:
            prob_mult = 0.22 + 0.10 * value_bias
        elif prob < very_low_prob:
            prob_mult = 0.42 + 0.15 * value_bias
        elif prob < 0.01:
            prob_mult = 0.70 + 0.10 * value_bias
        else:
            prob_mult = 1.00

        score = group_w * prob_score * ev_mult * prob_mult
        scores.append(max(score, 0.0))

    df["_score"] = scores

    # EVが極端に低い穴は原則見送り
    bad_longshot = (
        (df["group"].astype(str) == "穴")
        & df["expected_return"].notna()
        & (df["expected_return"] < ev_floor)
    )
    df.loc[bad_longshot, "_score"] *= 0.20

    # 全スコア0なら確率ベースにフォールバック
    if df["_score"].sum() <= 0:
        df["_score"] = df["prob"].clip(lower=0)
    if df["_score"].sum() <= 0:
        df["_score"] = 1.0

    # まず連続値で配分
    df["_raw_stake"] = budget * df["_score"] / df["_score"].sum()

    # 1点上限
    ticket_cap = max(min_bet, _round_to_unit(budget * max_ticket_share, unit))
    df["_raw_stake"] = df["_raw_stake"].clip(upper=ticket_cap)

    # 穴の総額上限
    long_mask = df["group"].astype(str) == "穴"
    long_cap = max(0, _round_to_unit(budget * max_longshot_share, unit))
    long_total = df.loc[long_mask, "_raw_stake"].sum()
    if long_total > long_cap and long_total > 0:
        df.loc[long_mask, "_raw_stake"] *= long_cap / long_total

    # 一旦丸め
    df["stake"] = df["_raw_stake"].apply(lambda x: _round_to_unit(x, unit))

    # 小額は0か最低額に
    for i in df.index:
        s = int(df.at[i, "stake"])
        if s <= 0:
            df.at[i, "stake"] = 0
        elif s < min_bet:
            df.at[i, "stake"] = min_bet

    # 予算超過時は優先度の低いものから100円ずつ削る
    def remove_priority(idx):
        row = df.loc[idx]
        group_rank = {"穴": 0, "抑え": 1, "本線": 2}.get(str(row["group"]), 1)
        return (
            group_rank,
            float(row["_score"]),
            float(row["prob"]),
        )

    while int(df["stake"].sum()) > budget:
        candidates = [i for i in df.index if int(df.at[i, "stake"]) > 0]
        if not candidates:
            break
        candidates.sort(key=remove_priority)
        changed = False
        for i in candidates:
            s = int(df.at[i, "stake"])
            new_s = s - unit
            if new_s != 0 and new_s < min_bet:
                new_s = 0
            if new_s < 0:
                new_s = 0
            if new_s < s:
                df.at[i, "stake"] = new_s
                changed = True
                break
        if not changed:
            break

    # 余りはスコア上位に100円ずつ追加
    # ただし穴全体上限・1点上限を守る
    def can_add(i):
        current = int(df.at[i, "stake"])
        if current + unit > ticket_cap:
            return False
        if str(df.at[i, "group"]) == "穴":
            current_long = int(df.loc[long_mask, "stake"].sum())
            if current_long + unit > long_cap:
                return False
        return True

    order = list(df.sort_values(["_score", "prob"], ascending=False).index)

    guard = 0
    while int(df["stake"].sum()) + unit <= budget and guard < 10000:
        guard += 1
        added = False
        for i in order:
            if can_add(i):
                df.at[i, "stake"] = int(df.at[i, "stake"]) + unit
                added = True
                break
        if not added:
            break

    # 最終チェック
    df["stake"] = pd.to_numeric(df["stake"], errors="coerce").fillna(0).astype(int)

    # 表示用の理由
    reasons = []
    for _, row in df.iterrows():
        group = str(row.get("group", ""))
        prob = _safe_num(row.get("prob"), 0.0)
        ev = _safe_num(row.get("expected_return"), np.nan)
        stake = int(row.get("stake", 0))

        if stake <= 0:
            reason = "見送り"
        elif group == "穴" and prob < very_low_prob:
            reason = "超低確率のため少額"
        elif not math.isnan(ev) and ev < 1.0:
            reason = "期待値1未満のため抑制"
        elif value_bias >= 0.3 and not math.isnan(ev) and ev >= 1.5:
            reason = "妙味大きく厚め"
        elif group == "本線":
            reason = "本線を厚め"
        elif group == "穴":
            reason = "穴は上限管理"
        else:
            reason = "確率・期待値を加味"
        reasons.append(reason)

    df["stake_reason"] = reasons

    return df.drop(columns=["_score", "_raw_stake"], errors="ignore")
