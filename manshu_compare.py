"""
「💥 万舟チャンス」の中穴〜万舟狙いの買い目を、確定オッズで「朝版」と「展示後版」に分けて作り、比べる。

  朝版    : 朝の3連単確率（manshu_chance_picks.tri_probs。展示前モデル）× 確定3連単オッズ
  展示後版: 事後予想の3連単確率（hindcast_predictions.tri_probs。hindcast.py --model ml。展示・直前情報も使う）
            × 確定3連単オッズ
どちらも今と同じ作り方（manshu.aim_tickets: 1号艇1着を外す・中穴5点＋万舟5点・確率順。各100円）。
同じオッズで作るので、違いは3連単確率（展示・直前情報を使ったかどうか）だけになる。

確定オッズはレース後に公式サイトの3連単オッズのページ（fetch_odds3t と同じページ）から1レース1回取り、
manshu_aim_compare.final_odds に残す（取れなかったら理由を odds_error に残し、次回また取る）。
公式サイトへは polite_http（開始間隔3.4秒以上）を通す。1日10レースなので10回。

起動は hindcast.yml の事後予想の後（毎晩）。対象は START 以降の印のレースのうち、結果が出ていて
まだ両方の買い目が作れていないもの（事後予想は1〜2日遅れでそろうので、展示後版は後の回で埋まる）。

ログは誰でも見られるので、件数と理由だけを出す（買い目・成績は出さない）。

  python manshu_compare.py [--start YYYYMMDD]
"""

from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
START = "20261017"          # 万舟チャンスの印を付け始めた日
TABLE = "manshu_aim_compare"
# 事後予想のうち展示後のモデルの版（hindcast.ML_VERSION_FILTER と同じ。展示前モデルは使わない）
HINDCAST_FILTERS = [("model_version", "like.ml-chain*"), ("model_version", "not.like.ml-chain-pre*")]


def log(*a):
    print("[MANSHU_COMPARE]", *a, flush=True)


def _sb(method, path, **kwargs):
    from pre_picks import _sb as sb

    return sb(method, path, **kwargs)


def _iso(ymd):
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"


def side_fields(prefix, probs, odds, pays, missing):
    """片方（pre / post）の買い目と結果の列。probs が無ければ missing を理由に残す。"""
    import manshu
    from pre_picks import ticket_result

    out = {f"{prefix}_tickets": None, f"{prefix}_error": None, f"{prefix}_hit": None,
           f"{prefix}_stake": None, f"{prefix}_return": None}
    if not probs:
        out[f"{prefix}_error"] = missing
        return out
    tickets = manshu.aim_tickets(probs, odds)
    if not tickets:
        out[f"{prefix}_error"] = "オッズ30倍以上の組がありません"
        return out
    out[f"{prefix}_tickets"] = tickets
    out[f"{prefix}_hit"], out[f"{prefix}_stake"], out[f"{prefix}_return"] = ticket_result(tickets, pays)
    return out


def compare_row(pick, odds, post, pays):
    """1レースの比較の行。pick: manshu_chance_picks の行 / odds: 確定オッズ / post: (版, 3連単確率) か None
    / pays: そのレースの3連単の払戻 [(組番, 払戻)]（中止・返還を除いたもの）。"""
    import manshu

    version, post_probs = post or (None, None)
    return {
        "race_key": pick["race_key"],
        "race_date": pick["race_date"],
        "method": manshu.aim_method_name(),
        "hindcast_model_version": version,
        "trifecta_actual": " ".join(c for c, _ in pays),
        "trifecta_payout": int(max(po for _, po in pays)),
        "is_manshu": manshu.is_manshu(max(po for _, po in pays)),
        **side_fields("pre", pick.get("tri_probs"), odds, pays, "朝の3連単の確率がありません"),
        **side_fields("post", post_probs, odds, pays, "事後予想の3連単の確率がまだありません"),
        "updated_at": datetime.now(JST).isoformat(timespec="seconds"),
    }


def done(row):
    """もう作り直さなくてよい行（確定オッズがあり、展示後版の買い目まで作れた行）。"""
    return bool(row and row.get("final_odds") and (row.get("post_tickets") or row.get("post_error")
                                                     not in (None, "事後予想の3連単の確率がまだありません")))


def hindcast_tri(race_keys):
    """事後予想（展示後のモデル）の3連単確率 {race_key: (版, {組番: 確率})}。同じレースに版が複数あれば新しい版。"""
    if not race_keys:
        return {}
    keys = ",".join(race_keys)
    rows = _sb("GET", "hindcast_predictions", params=[
        ("select", "race_key,model_version,tri_probs"), ("race_key", f"in.({keys})"),
        ("tri_probs", "not.is.null"), *HINDCAST_FILTERS]).json() or []
    out = {}
    for r in sorted(rows, key=lambda r: r["model_version"]):
        out[r["race_key"]] = (r["model_version"], r["tri_probs"])
    return out


def fetch_final_odds(race_key, fetch=None):
    """確定3連単オッズ {組番: オッズ}。戻り値 (オッズ, 理由)。"""
    from pre_picks import NoOdds, fetch_odds3t

    try:
        return (fetch or fetch_odds3t)(race_key), None
    except NoOdds as e:
        return None, str(e)[:200]
    except Exception as e:  # noqa: BLE001  1レース読めなくても他は続ける
        return None, f"オッズを読めませんでした（{type(e).__name__}）"


def run(start=START, today=None, fetch=None, payouts=None):
    from pre_picks import VOID_COMBOS, trifecta_payouts

    today = today or datetime.now(JST).strftime("%Y%m%d")
    picks = _sb("GET", "manshu_chance_picks", params=[
        ("select", "race_key,race_date,tri_probs"), ("result_status", "eq.settled"),
        ("race_date", f"gte.{_iso(start)}"), ("race_date", f"lt.{_iso(today)}"),
        ("order", "race_key.asc")]).json() or []
    have = {r["race_key"]: r for r in _sb("GET", TABLE, params=[
        ("select", "race_key,final_odds,odds_at,post_tickets,post_error"),
        ("race_date", f"gte.{_iso(start)}")]).json() or []}
    todo = [p for p in picks if not done(have.get(p["race_key"]))]
    if not todo:
        log("対象なし")
        return 0
    post = hindcast_tri([p["race_key"] for p in todo])
    payouts = payouts or trifecta_payouts
    pays_by_day, made, reasons = {}, 0, {}
    for p in todo:
        rk, ymd = p["race_key"], p["race_date"].replace("-", "")
        old = have.get(rk) or {}
        odds, odds_at = old.get("final_odds"), old.get("odds_at")
        if not odds:
            odds, why = fetch_final_odds(rk, fetch)
            odds_at = datetime.now(JST).isoformat(timespec="seconds")
            if not odds:
                reasons[why] = reasons.get(why, 0) + 1
                _sb("POST", f"{TABLE}?on_conflict=race_key", prefer="resolution=merge-duplicates,return=minimal",
                    json=[{"race_key": rk, "race_date": p["race_date"], "odds_error": why,
                           "updated_at": odds_at}])
                continue
        if ymd not in pays_by_day:
            pays_by_day[ymd] = payouts(ymd)
        day_pays = pays_by_day[ymd]
        real = [(c, po) for c, po in ((day_pays or {}).get(rk) or [])
                if c not in VOID_COMBOS and po == po and po is not None]
        if not real:
            why = "競走成績（払戻）がまだありません"
            reasons[why] = reasons.get(why, 0) + 1
            continue
        row = compare_row(p, odds, post.get(rk), real)
        row.update(final_odds=odds, odds_at=odds_at, odds_error=None)
        _sb("POST", f"{TABLE}?on_conflict=race_key", prefer="resolution=merge-duplicates,return=minimal", json=[row])
        made += 1
        if row["post_error"]:
            reasons[row["post_error"]] = reasons.get(row["post_error"], 0) + 1
    why = "・".join(f"{k} {v}件" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
    log(f"確定オッズで買い目を作成 {made}/{len(todo)}レース" + (f"（{why}）" if why else ""))
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START)
    a = ap.parse_args()
    run(a.start)


if __name__ == "__main__":
    main()
