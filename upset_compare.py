"""
「1号艇が負けた場合の着順」専用モデル（ml_upset.py）の毎晩の検証。本番の予想・買い目・画面は変えない。

「💥 万舟チャンス」の印のレース（START 以降・結果が出たもの）について、
  展示後版: 事後予想の3連単確率（hindcast_predictions.tri_probs。今の新モデル）の1号艇1着の組はそのまま、
            それ以外を（1 − 1号艇の1着確率）× 専用モデル（展示後の版）に置き換えた3連単確率
  朝版    : 朝の3連単確率（manshu_chance_picks.tri_probs。展示前モデル）を同じように専用モデル（展示前の版）で置き換えたもの
を作り、manshu_upset_compare に保存する（荒れたときの本命・2番手・1号艇が負ける確率も）。
専用モデルの版は、その日より前のデータだけで学習・調整した版のうちいちばん新しいもの（ml_upset.version_for）。

あわせて、manshu_compare.py がレース後に取った確定3連単オッズ（manshu_aim_compare.final_odds）で、
今のモデルと専用モデルの両方の確率から
  A: 1号艇1着を外す・中穴5点＋万舟5点・確率順（今の作り方。manshu.aim_tickets）
  E: 1着 = 1号艇以外で1着確率が高い上位2艇。その2艇が1着の組から3連単の確率順に10点（オッズの条件なし）
を作り、同じレース・同じオッズで並べる（各100円。variants・variant_tickets）。

起動は hindcast.yml の manshu_compare.py の後（毎晩）。事後予想は1〜2日遅れでそろうので、展示後版は後の回で埋まる。
ログは誰でも見られるので、件数と理由だけを出す（確率・買い目・成績は出さない）。

  python upset_compare.py [--start YYYYMMDD]
"""

from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

JST = ZoneInfo("Asia/Tokyo")
START = "20261017"          # 万舟チャンスの印を付け始めた日
TABLE = "manshu_upset_compare"
E_N = 10                    # 作り方E の点数
MISSING_BASE = {"pre": "朝の3連単の確率がありません", "post": "事後予想の3連単の確率がまだありません"}
NO_VERSION = "その日より前に学習した専用モデルの版がありません"
NO_FEATURES = "特徴量を作れません（出走表・直前情報が未取得）"
RETRY_DAYS = 14             # 作れなかったレースを取り直すのは、この日数より新しいレースだけ


def log(*a):
    print("[UPSET_COMPARE]", *a, flush=True)


def _sb(method, path, **kwargs):
    from pre_picks import _sb as sb

    return sb(method, path, **kwargs)


def _iso(ymd):
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"


# ---------------------------------------------------------------
# 買い目（A・E）
# ---------------------------------------------------------------
def top2_lanes(probs):
    """1号艇以外で、1着確率が高い上位2艇の艇番 [a, b]（同じ確率なら内側）。"""
    import manshu

    first = {k: v for k, v in manshu.first_probs(probs).items() if k != 1}
    return sorted(first, key=lambda k: (-first[k], k))[:2]


def e_tickets(probs, odds, n=E_N):
    """作り方E: 1着を top2_lanes に固定し、その組の中から3連単の確率が高い順に n 点（各100円）。
    確定オッズが無い組（欠場など）は買えないので外す。"""
    import manshu

    lanes = {str(x) for x in top2_lanes(probs)}
    rows = []
    for combo, p in (probs or {}).items():
        if str(combo)[:1] not in lanes:
            continue
        try:
            o, p = float((odds or {}).get(combo)), float(p)
        except (TypeError, ValueError):
            continue
        if p != p or o != o:
            continue
        rows.append((combo, p, o))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return [{"combo": c, "prob": round(p, 5), "odds": o, "tier": manshu.odds_tier(o) or "30倍未満",
             "stake": manshu.AIM_STAKE, "payout": int(round(o * manshu.AIM_STAKE))} for c, p, o in rows[:n]]


def a_tickets(probs, odds):
    """作り方A（今の作り方）: 1号艇1着を外す・中穴5点＋万舟5点・確率順。"""
    import manshu

    return manshu.aim_tickets(probs, odds, method="MIX", mix={"中穴": 5, "万舟": 5}, order="prob", exclude=("1",))


def model_stats(probs, odds, pays, winner):
    """1つの3連単確率（今のモデル / 専用モデル）での A・E の成績と、1着の当たり具合。"""
    from pre_picks import ticket_result

    top2 = top2_lanes(probs)
    ranked = sorted(probs, key=lambda c: -float(probs[c]))
    actual = pays[0][0] if pays else None
    stats = {"top2": top2, "top1_hit": bool(top2) and top2[0] == winner, "top2_hit": winner in top2,
             "rank_actual": ranked.index(actual) + 1 if actual in probs else None}
    tickets = {}
    if odds:
        for name, t in (("A", a_tickets(probs, odds)), ("E", e_tickets(probs, odds))):
            hit, stake, ret = ticket_result(t, pays)
            stats[name] = {"hit": bool(hit), "stake": int(stake or 0), "return": int(ret or 0),
                           "prob_sum": round(sum(x["prob"] for x in t), 5)}
            tickets[name] = t
    return stats, tickets


# ---------------------------------------------------------------
# 専用モデルの確率
# ---------------------------------------------------------------
def side_probs(kind, base, feats, version, model):
    """片方（pre / post）の列。base: 今のモデルの3連単 / feats: そのレースの特徴量。"""
    import ml_upset

    out = {f"{kind}_version": version, f"{kind}_tri_probs": None, f"{kind}_summary": None, f"{kind}_error": None,
           f"{kind}_base_tri_probs": base or None}
    if not base:
        out[f"{kind}_error"] = MISSING_BASE[kind]
    elif version is None:
        out[f"{kind}_error"] = NO_VERSION
    elif feats is None or feats.empty:
        out[f"{kind}_error"] = NO_FEATURES
    else:
        cond = next(iter(ml_upset.upset_tri(model, feats).values()), None)
        out[f"{kind}_tri_probs"] = ml_upset.combine(base, cond)
        out[f"{kind}_summary"] = ml_upset.upset_summary(cond, base)
    return out


def features(race_keys, days):
    """対象レースの特徴量 (展示後, 展示前)。学習と同じ関数（ml_features.build_table・ml_pre.build_pre_table）。"""
    import ml_features as mf
    import ml_pre

    start = (pd.Timestamp(min(days)) - pd.Timedelta(days=400)).strftime("%Y%m%d")
    k, b, p = mf.load_sources(start, max(days))
    post = mf.build_table(k, b, p, min(days), max(days))
    full = post.groupby("race_key")["has_pages"].transform("all")
    post = post[full & post["race_key"].isin(race_keys)]
    pre = ml_pre.build_pre_table(k, b, p)
    pre = pre[pre["race_key"].isin(race_keys)]
    return dict(tuple(post.groupby("race_key"))), dict(tuple(pre.groupby("race_key")))


def done(row):
    return bool(row and row.get("post_tri_probs") and row.get("pre_tri_probs") and row.get("variants"))


def retry(row, race_date, today):
    """作れていないレースを今回作るか（古いレースは作れなかった理由を残したまま諦める）。"""
    if done(row):
        return False
    return not row or (pd.Timestamp(today) - pd.Timestamp(race_date)).days <= RETRY_DAYS


def run(start=START, today=None, payouts=None, feature_fn=None, load_model=None):
    import ml_upset
    from manshu_compare import hindcast_tri
    from pre_picks import VOID_COMBOS, trifecta_payouts

    today = today or datetime.now(JST).strftime("%Y%m%d")
    picks = _sb("GET", "manshu_chance_picks", params=[
        ("select", "race_key,race_date,tri_probs"), ("result_status", "eq.settled"),
        ("race_date", f"gte.{_iso(start)}"), ("race_date", f"lt.{_iso(today)}"),
        ("order", "race_key.asc")]).json() or []
    have = {r["race_key"]: r for r in _sb("GET", TABLE, params=[
        ("select", "race_key,post_tri_probs,pre_tri_probs,variants"),
        ("race_date", f"gte.{_iso(start)}")]).json() or []}
    todo = [p for p in picks if retry(have.get(p["race_key"]), p["race_date"].replace("-", ""), today)]
    if not todo:
        log("対象なし")
        return 0
    keys = [p["race_key"] for p in todo]
    days = sorted({p["race_date"].replace("-", "") for p in todo})
    post_base = hindcast_tri(keys)
    odds_rows = _sb("GET", "manshu_aim_compare", params=[
        ("select", "race_key,final_odds"), ("race_key", f"in.({','.join(keys)})")]).json() or []
    odds_by = {r["race_key"]: r.get("final_odds") for r in odds_rows}
    try:
        post_f, pre_f = (feature_fn or features)(set(keys), days)
    except Exception as e:  # noqa: BLE001
        log(f"特徴量を作れません（{type(e).__name__}）")
        post_f, pre_f = {}, {}
    vers = {"post": ml_upset.versions(), "pre": ml_upset.versions(pre=True)}
    load_model = load_model or ml_upset.load
    models = {}
    payouts = payouts or trifecta_payouts
    pays_by_day, made, reasons = {}, 0, {}
    for p in todo:
        rk, ymd = p["race_key"], p["race_date"].replace("-", "")
        if ymd not in pays_by_day:
            pays_by_day[ymd] = payouts(ymd)
        pays = [(c, po) for c, po in (((pays_by_day[ymd] or {}).get(rk)) or [])
                if c not in VOID_COMBOS and po == po and po is not None]
        if not pays:
            reasons["競走成績（払戻）がまだありません"] = reasons.get("競走成績（払戻）がまだありません", 0) + 1
            continue
        row = {"race_key": rk, "race_date": p["race_date"], "trifecta_actual": " ".join(c for c, _ in pays),
               "trifecta_payout": int(max(po for _, po in pays)), "winner_lane": int(pays[0][0][0])}
        row["is_manshu"] = row["trifecta_payout"] >= 10000
        for kind, base, fts in (("post", (post_base.get(rk) or (None, None))[1], post_f.get(rk)),
                                ("pre", p.get("tri_probs"), pre_f.get(rk))):
            version = ml_upset.version_for(ymd, vers[kind])
            if version and version not in models:
                models[version] = load_model(version)
            row.update(side_probs(kind, base, fts, version, models.get(version)))
            if row[f"{kind}_error"]:
                reasons[row[f"{kind}_error"]] = reasons.get(row[f"{kind}_error"], 0) + 1
        row["post_base_version"] = (post_base.get(rk) or (None, None))[0]
        odds = odds_by.get(rk)
        variants, tickets = {}, {}
        for kind in ("pre", "post"):
            if not row.get(f"{kind}_tri_probs"):
                continue
            variants[kind], tickets[kind] = {}, {}
            for name, probs in (("current", row[f"{kind}_base_tri_probs"]), ("upset", row[f"{kind}_tri_probs"])):
                variants[kind][name], tickets[kind][name] = model_stats(probs, odds, pays, row["winner_lane"])
        # 確定オッズがまだ無ければ買い目は作らない（次回 manshu_compare.py が取った後に作る）
        row["variants"] = variants if odds and variants else None
        row["variant_tickets"] = tickets if odds and tickets else None
        row["odds_ready"] = bool(odds)
        if not odds:
            reasons["確定オッズがまだありません"] = reasons.get("確定オッズがまだありません", 0) + 1
        row.pop("post_base_tri_probs", None)
        row.pop("pre_base_tri_probs", None)
        row["updated_at"] = datetime.now(JST).isoformat(timespec="seconds")
        _sb("POST", f"{TABLE}?on_conflict=race_key", prefer="resolution=merge-duplicates,return=minimal", json=[row])
        made += 1
    why = "・".join(f"{k} {v}件" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
    log(f"専用モデルの確率を保存 {made}/{len(todo)}レース" + (f"（{why}）" if why else ""))
    return made


# ---------------------------------------------------------------
# 集計（検証タブ）
# ---------------------------------------------------------------
def summary(rows):
    """今のモデルと専用モデルを同じレースで並べる（朝版・展示後版ごと）。
    1着の当たり（1号艇が負けたレースだけ）・実際の組の順位・A/E の成績。"""
    out = []
    for kind, label in (("pre", "朝版"), ("post", "展示後版")):
        races = [r for r in rows or [] if ((r.get("variants") or {}).get(kind) or {}).get("upset")]
        if not races:
            continue
        for name, mlabel in (("current", "今のモデル"), ("upset", "専用モデル")):
            ss = [r["variants"][kind][name] for r in races]
            lost = [s for s, r in zip(ss, races) if r.get("winner_lane") != 1]
            man = [s for s, r in zip(ss, races) if r.get("winner_lane") != 1 and r.get("is_manshu")]
            ranks = [s["rank_actual"] for s in lost if s.get("rank_actual")]
            mranks = [s["rank_actual"] for s in man if s.get("rank_actual")]
            row = {"版": label, "確率": mlabel, "レース数": len(races), "1号艇が負けた": len(lost),
                   "1番手が1着": _rate([s["top1_hit"] for s in lost]),
                   "上位2艇が1着": _rate([s["top2_hit"] for s in lost]),
                   "実際の組の順位（平均）": sum(ranks) / len(ranks) if ranks else None,
                   "万舟の順位（平均）": sum(mranks) / len(mranks) if mranks else None}
            for v in ("A", "E"):
                vs = [s[v] for s in ss if v in s]
                stake = sum(x["stake"] for x in vs)
                row[f"{v} 的中率"] = _rate([x["hit"] for x in vs])
                row[f"{v} 回収率"] = sum(x["return"] for x in vs) / stake if stake else None
            out.append(row)
    return out


def _rate(xs):
    return sum(bool(x) for x in xs) / len(xs) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START)
    a = ap.parse_args()
    run(a.start)


if __name__ == "__main__":
    main()
