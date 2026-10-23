"""
「💥 万舟チャンス」の中穴〜万舟狙いの買い目を、確定オッズで「朝版」と「展示後版」に分けて作り、比べる。

  朝版    : 朝の3連単確率（manshu_chance_picks.tri_probs。展示前モデル）× 確定3連単オッズ
  展示後版: 事後予想の3連単確率（hindcast_predictions.tri_probs。hindcast.py --model ml。展示・直前情報も使う）
            × 確定3連単オッズ
どちらも今と同じ作り方（manshu.aim_tickets: 1号艇1着を外す・中穴5点＋万舟5点・確率順。各100円）。
同じオッズで作るので、違いは3連単確率（展示・直前情報を使ったかどうか）だけになる。

作り方の比較（検証だけ。本番・画面の買い目は変えない）: 朝版・展示後版それぞれで、どれも1号艇1着を外す・
オッズ30倍以上・計10点・各100円で
  A: 確率順・中穴5点＋万舟5点（今の作り方） / B: 期待値順（確率×オッズ）・中穴5点＋万舟5点
  C: 確率順・中穴3点＋万舟7点            / D: 期待値順・中穴3点＋万舟7点
を作る。あわせて1着を絞る作り方（1着 = 1号艇以外で1着確率が高い上位2艇。その2艇が1着の組の中から
3連単の確率順・各100円）として
  E: 10点 / F: 15点 / G: 20点（オッズの条件なし） / H: 10点 / I: 20点（オッズ30倍以上だけ）
を作る。どれも variants（成績・1号艇の1着確率・1着に選んだ2艇と、そのどちらかが1着になったか）と
variant_tickets（買い目）に残す。
「1号艇の1着確率が50%以上なら見送る」版は、variants の p1（その版の確率で見た1号艇の1着確率）で集計時に絞る
（manshu.variant_summary）。

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
MISSING_PRE = "朝の3連単の確率がありません"
MISSING_POST = "事後予想の3連単の確率がまだありません"
# 比べる作り方 {名前: (並べ方, 中穴の点数, 万舟の点数)}。どれも1号艇1着を外す・30倍以上・計10点・各100円
VARIANTS = {"A": ("prob", 5, 5), "B": ("ev", 5, 5), "C": ("prob", 3, 7), "D": ("ev", 3, 7)}
# 1着を絞る作り方 E〜I は manshu.TOP2_VARIANTS


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


def variant_side(probs, odds, pays, missing):
    """片方（朝版 / 展示後版）の作り方ごとの成績と買い目。
    → ({"p1", "error", "top2"（1着に選んだ2艇）, "top2_hit"（そのどちらかが1着か）,
         作り方: {hit, stake, return, prob_sum}}, {作り方: 買い目})"""
    import manshu
    from pre_picks import ticket_result

    if not probs:
        return {"error": missing}, {}
    top2 = manshu.top2_lanes(probs)
    stats = {"p1": round(manshu.first_probs(probs).get(1, 0.0), 5), "error": None, "top2": top2,
             "top2_hit": any(str(c)[:1] in {str(x) for x in top2} for c, _ in pays)}
    made = {}
    for name, (order, mid, high) in VARIANTS.items():
        made[name] = manshu.aim_tickets(probs, odds, method="MIX", mix={"中穴": mid, "万舟": high}, order=order,
                                        exclude=("1",))
    for name, (n, odds_min) in manshu.TOP2_VARIANTS.items():
        made[name] = manshu.top2_tickets(probs, odds, n, odds_min, lanes=top2)
    tickets = {}
    for name, t in made.items():
        hit, stake, ret = ticket_result(t, pays)
        stats[name] = {"hit": bool(hit), "stake": int(stake or 0), "return": int(ret or 0),
                       "prob_sum": round(sum(x["prob"] for x in t), 5)}
        tickets[name] = t
    return stats, tickets


def variant_fields(pre_probs, post_probs, odds, pays):
    """作り方ごとの比較の列（variants・variant_tickets）。"""
    pre, pre_t = variant_side(pre_probs, odds, pays, MISSING_PRE)
    post, post_t = variant_side(post_probs, odds, pays, MISSING_POST)
    return {"variants": {"pre": pre, "post": post}, "variant_tickets": {"pre": pre_t, "post": post_t}}


def compare_row(pick, odds, post, pays, variants=True):
    """1レースの比較の行。pick: manshu_chance_picks の行 / odds: 確定オッズ / post: (版, 3連単確率) か None
    / pays: そのレースの3連単の払戻 [(組番, 払戻)]（中止・返還を除いたもの）
    / variants: 作り方ごとの比較の列も作るか（列がまだ無いデータベースでは False）。"""
    import manshu

    version, post_probs = post or (None, None)
    row = {
        "race_key": pick["race_key"],
        "race_date": pick["race_date"],
        "method": manshu.aim_method_name(),
        "hindcast_model_version": version,
        "trifecta_actual": " ".join(c for c, _ in pays),
        "trifecta_payout": int(max(po for _, po in pays)),
        "is_manshu": manshu.is_manshu(max(po for _, po in pays)),
        **side_fields("pre", pick.get("tri_probs"), odds, pays, MISSING_PRE),
        **side_fields("post", post_probs, odds, pays, MISSING_POST),
        "updated_at": datetime.now(JST).isoformat(timespec="seconds"),
    }
    if variants:
        row.update(variant_fields(pick.get("tri_probs"), post_probs, odds, pays))
    return row


def done(row, variants=True):
    """もう作り直さなくてよい行（確定オッズがあり、展示後版の買い目まで作れた行）。
    variants: 作り方ごとの比較もそろっていることを求める（無ければ確定オッズは取り直さずに作り直す）。"""
    import manshu

    ok = bool(row and row.get("final_odds") and (row.get("post_tickets") or row.get("post_error")
                                                   not in (None, MISSING_POST)))
    if ok and variants:
        post = (row.get("variants") or {}).get("post") or {}
        ok = bool(post) and post.get("error") != MISSING_POST
        if ok and not post.get("error"):  # 後から足した作り方（E〜I）が無ければ作り直す
            ok = all(k in post for k in [*VARIANTS, *manshu.TOP2_VARIANTS, "top2_hit"])
    return ok


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


def saved_rows(start):
    """保存済みの行 {race_key: 行} と、作り方ごとの比較の列があるか。
    列がまだ無い（SQL の適用前）なら、今までの朝版・展示後版だけを作る。"""
    cols = "race_key,final_odds,odds_at,post_tickets,post_error"
    try:
        rows = _sb("GET", TABLE, params=[("select", cols + ",variants"),
                                         ("race_date", f"gte.{_iso(start)}")]).json() or []
        variants = True
    except Exception as e:  # noqa: BLE001
        log(f"作り方ごとの比較の列を読めません（{type(e).__name__}）。"
            "supabase/migrations/20261023000000_manshu_aim_variants.sql の適用が必要かもしれません。今までの分だけ作ります")
        rows = _sb("GET", TABLE, params=[("select", cols), ("race_date", f"gte.{_iso(start)}")]).json() or []
        variants = False
    return {r["race_key"]: r for r in rows}, variants


def run(start=START, today=None, fetch=None, payouts=None):
    from pre_picks import VOID_COMBOS, trifecta_payouts

    today = today or datetime.now(JST).strftime("%Y%m%d")
    picks = _sb("GET", "manshu_chance_picks", params=[
        ("select", "race_key,race_date,tri_probs"), ("result_status", "eq.settled"),
        ("race_date", f"gte.{_iso(start)}"), ("race_date", f"lt.{_iso(today)}"),
        ("order", "race_key.asc")]).json() or []
    have, variants = saved_rows(start)
    todo = [p for p in picks if not done(have.get(p["race_key"]), variants)]
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
        row = compare_row(p, odds, post.get(rk), real, variants)
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
