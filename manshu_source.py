"""
万舟チャンスの予想3種比較（モデルのみ／Claudeのみ／両方の予想平均）の自動記録。

展示後の自動予想（auto_post_predict.py）が万舟チャンスのレースで中穴〜万舟狙いの買い目を作り直すのと
同じタイミングで、3種類の3連単の確率から同じルールで買い目を作り、manshu_source_compare に1レース3行で残す。

  model  : 「🤖 モデルのみ予想」の3連単の確率（trifecta(final)）
  claude : 「🧠 Claudeのみ予想」と同じ読み（claude_reader.read_race・同じ入力）。Claude が挙げなかった組には、
           残りの確率をモデルの比率で配る（claude_reader.claude_full_tri。「両方の予想平均」と同じ広げ方）
  blend  : 「⚖️ 両方の予想平均」と同じ平均（claude_reader.average_tri）

買い目の選び方は3種とも同じ（compare_tickets）: 1号艇の1着を外し、3連単オッズ30倍以上の組から確率が高い順に
COMPARE_N 点（各100円）。確率だけを各予想のものに差し替える。本番の中穴〜万舟狙いの買い目（manshu.aim_tickets。
中穴5＋万舟5）とは別のルールなので、model の買い目は manshu_chance_picks.aim_tickets と同じとは限らない。

  - 1レース1回だけ（race_key, source が主キー。先に model の行を入れられた実行だけが Claude を呼ぶ）
  - Claude の費用は claude_usage に purpose = manshu_compare で残す。今日（日本時間）の合計が
    app_settings.manshu_compare_claude_daily_usd 以上なら Claude を呼ばず、claude・blend の行の error に理由を残す
    （呼ぶ前に確かめるので、上限を1回分だけ超えることはある）
  - 結果は既存の結果確定（list_results.py・翌朝の pre_picks.py）が万舟チャンスの結果と一緒に書く
  - 集計はビュー manshu_source_summary（saved_at が締切より前の行だけ）

データベース: supabase/migrations/20261028000000_manshu_source_compare.sql
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
TABLE = "manshu_source_compare"
SOURCES = ("model", "claude", "blend")
USAGE_PURPOSE = "manshu_compare"
LIMIT_SETTING = "manshu_compare_claude_daily_usd"
DEFAULT_DAILY_USD = 3.0
COMPARE_N = 10
COMPARE_ODDS_MIN = 30.0
COMPARE_EXCLUDE_FIRST = ("1",)
COMPARE_STAKE = 100


def method_name():
    """保存する買い目の作り方の名前（例: TOP10-odds30-prob-no1）。"""
    ex = "".join(f"-no{x}" for x in COMPARE_EXCLUDE_FIRST)
    return f"TOP{COMPARE_N}-odds{COMPARE_ODDS_MIN:g}-prob{ex}"


def compare_tickets(probs, odds, n=COMPARE_N):
    """3種共通の買い目。probs: {組番: 確率} / odds: {組番: オッズ}
    → [{combo, prob, odds, tier, stake, payout}]（1号艇の1着を外し、オッズ30倍以上から確率の高い順に n 点）"""
    import manshu

    rows = []
    for combo, p in (probs or {}).items():
        combo = str(combo)
        if combo[:1] in COMPARE_EXCLUDE_FIRST:
            continue
        try:
            o, p = float((odds or {}).get(combo)), float(p)
        except (TypeError, ValueError):
            continue
        if p != p or o != o or o < COMPARE_ODDS_MIN:
            continue
        rows.append((combo, p, o))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return [{"combo": c, "prob": round(p, 5), "odds": o, "tier": manshu.odds_tier(o), "stake": COMPARE_STAKE,
             "payout": int(round(o * COMPARE_STAKE))} for c, p, o in rows[:n]]


def source_probs(model_tri, reading):
    """{source: {組番: 確率}}。Claude の読みが無ければ model だけ。model_tri: trifecta(final) の表（combo, prob）。"""
    import claude_reader as cr
    import manshu

    out = {"model": manshu.tri_dict(model_tri[["combo", "prob"]])}
    if reading and reading.get("status") == "ok" and reading.get("tickets"):
        out["claude"] = cr.claude_full_tri(reading["tickets"], model_tri)
        out["blend"] = manshu.tri_dict(cr.average_tri(model_tri, reading["tickets"]))
    return out


def deadline_at(race_date_iso, hhmm):
    """締切の日時（ISO・日本時間）。読めなければ None。"""
    try:
        return datetime.strptime(f"{race_date_iso} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=JST).isoformat()
    except (TypeError, ValueError):
        return None


def build_row(t, source, probs, odds, odds_at, error=None, claude_model=None):
    """manshu_source_compare の1行（結果の列は空）。probs が None なら確率・買い目なしで error だけ残す。"""
    aim = compare_tickets(probs, odds) if probs and odds else []
    if probs and not error:
        if not odds:
            error = "オッズを取れませんでした"
        elif not aim:
            error = "1号艇以外が1着でオッズ30倍以上の組がありません"
    return {
        "race_key": t["race_key"],
        "race_date": t["race_date"],
        "source": source,
        "tri_probs": {c: round(float(p), 6) for c, p in probs.items()} if probs else None,
        "aim_tickets": aim,
        "aim_method": method_name(),
        "odds_at": odds_at,
        "deadline_at": deadline_at(t["race_date"], t.get("deadline")),
        "stake": sum(x["stake"] for x in aim) or None,
        "claude_model": claude_model,
        "error": error,
    }


# ---------------------------------------------------------------
# Supabase（sb は auto_post_predict._sb と同じ形: sb(method, path, **kwargs) → Response）
# ---------------------------------------------------------------
def recorded(sb, race_key):
    """そのレースの比較を記録済みか（model の行があるか）。"""
    rows = sb("GET", f"{TABLE}?race_key=eq.{race_key}&source=eq.model&select=race_key").json() or []
    return bool(rows)


def insert(sb, rows):
    """まだ無い行だけ入れる。入った行を返す（同じレース・同じ種類がもうあれば入らない）。"""
    return sb("POST", f"{TABLE}?on_conflict=race_key,source",
              prefer="resolution=ignore-duplicates,return=representation", json=rows).json() or []


def daily_limit(sb):
    """Claude の1日の上限（ドル）。読めなければ DEFAULT_DAILY_USD。"""
    try:
        rows = sb("GET", f"app_settings?id=eq.1&select={LIMIT_SETTING}").json() or []
        v = rows[0].get(LIMIT_SETTING) if rows else None
        return float(v) if v is not None else DEFAULT_DAILY_USD
    except Exception:  # noqa: BLE001
        return DEFAULT_DAILY_USD


def spent_today(sb, now=None):
    """今日（日本時間）の、この比較で Claude に使った額（ドル）。"""
    now = now or datetime.now(JST)
    start = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = sb("GET", "claude_usage", params=[("select", "usd"), ("purpose", f"eq.{USAGE_PURPOSE}"),
                                             ("called_at", f"gte.{start}")]).json() or []
    return sum(float(r.get("usd") or 0) for r in rows)


def read_claude(sb, t, race, final, api_key, read_model=None, saved_section=None, now=None):
    """「Claudeのみ予想」と同じ読み。戻り値 (reading, error)。上限を超えていれば呼ばない。"""
    import claude_reader as cr
    from official_fetcher import VENUES

    # アプリの「Claudeのみ予想」と同じレース名（日付 会場 R）
    label = f"{t['race_date']} {VENUES.get(t['jcd'], t.get('venue') or '')} {t['race_no']}R"
    reuse = cr.reading_from_section(saved_section)
    # 同じ入力の読みが保存済み（手動で Claude の予想を取った）なら、API を呼ばずにそれを使う
    if reuse:
        r = cr.read_race("", t["race_key"], race, final, model=read_model, race_label=label, reuse=reuse)
        if r.get("status") == "ok":
            return r, None
    if not api_key:
        return None, "ANTHROPIC_API_KEY が未設定です"
    limit, spent = daily_limit(sb), spent_today(sb, now)
    if spent >= limit:
        return None, f"1日の上限 ${limit:.2f} に達したためスキップ（今日 ${spent:.2f}）"
    reading = cr.read_race(api_key, t["race_key"], race, final, model=read_model, race_label=label)
    row = cr.usage_row(reading, t["race_key"], USAGE_PURPOSE)
    if row:
        try:
            sb("POST", "claude_usage", prefer="return=minimal", json=row)
        except Exception as e:  # noqa: BLE001  記録に失敗しても比較は残す
            print("[MANSHU_SOURCE] claude_usage の記録に失敗:", type(e).__name__, flush=True)
    if reading.get("status") != "ok":
        return reading, reading.get("error") or "Claude の予想を取れませんでした"
    return reading, None


def record(sb, t, race, final, odds, api_key="", read_model=None, saved_section=None, now=None):
    """1レースの3種を記録する。戻り値は記録用の短い文（会場名・買い目は入れない）。"""
    import manshu
    from prediction import trifecta

    model_tri = trifecta(final)[["combo", "prob"]]
    odds_d = manshu.odds_dict(odds)
    odds_at = datetime.now(JST).isoformat(timespec="seconds") if odds_d else None
    probs = source_probs(model_tri, None)
    if not insert(sb, [build_row(t, "model", probs["model"], odds_d, odds_at)]):
        return "記録済み"
    reading, err = read_claude(sb, t, race, final, api_key, read_model, saved_section, now)
    probs = source_probs(model_tri, reading)
    cm = (reading or {}).get("served_model") or (reading or {}).get("model")
    rows = [build_row(t, s, probs.get(s), odds_d, odds_at, error=None if s in probs else err, claude_model=cm)
            for s in ("claude", "blend")]
    insert(sb, rows)
    return "3種を記録" if not err else f"モデルだけ記録（Claude: {err}）"


# ---------------------------------------------------------------
# 結果（既存の結果確定から呼ぶ）
# ---------------------------------------------------------------
def result_fields(row, pays):
    """1行の結果の列。pays: 3連単の払戻 [(組番, 払戻)]。無ければ中止・不成立（void）。"""
    from pre_picks import VOID_COMBOS, ticket_result

    out = {"result_at": datetime.now(JST).isoformat(timespec="seconds")}
    real = [(c, po) for c, po in (pays or []) if c not in VOID_COMBOS and po == po and po is not None]
    if not real:
        out["result_status"] = "void"
        return out
    out["result_status"] = "settled"
    out["trifecta_actual"] = " ".join(c for c, _ in real)
    out["trifecta_payout"] = int(max(po for _, po in real))
    out["hit"], out["stake"], out["return"] = ticket_result(row.get("aim_tickets") or [], real)
    return out


def apply_result(sb, race_key, pays):
    """そのレースの結果がまだ無い行に結果を書く。書いた行の数を返す。"""
    rows = sb("GET", f"{TABLE}?race_key=eq.{race_key}&result_at=is.null&select=race_key,source,aim_tickets").json() or []
    for r in rows:
        sb("PATCH", f"{TABLE}?race_key=eq.{race_key}&source=eq.{r['source']}&result_at=is.null",
           prefer="return=minimal", json=result_fields(r, pays))
    return len(rows)


def record_results(sb, today=None):
    """翌朝の保険: 前日までで結果の無い行を、その日の競走成績の払戻で書く（pre_picks.py から呼ぶ）。"""
    from pre_picks import trifecta_payouts

    today = today or datetime.now(JST).strftime("%Y%m%d")
    iso = f"{today[:4]}-{today[4:6]}-{today[6:]}"
    rows = sb("GET", f"{TABLE}?result_at=is.null&race_date=lt.{iso}&select=race_key,race_date").json() or []
    done = 0
    for d in sorted({r["race_date"] for r in rows}):
        pays = trifecta_payouts(d.replace("-", ""))
        if pays is None:
            continue
        for rk in sorted({r["race_key"] for r in rows if r["race_date"] == d}):
            done += apply_result(sb, rk, pays.get(rk))
    return done
