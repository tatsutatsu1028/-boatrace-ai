"""
万舟チャンスの予想比較（モデルのみ／Claudeのみ／両方の予想平均／メモ入りのClaude）の自動記録。

展示後の自動予想（auto_post_predict.py）が万舟チャンスのレースで中穴〜万舟狙いの買い目を作り直すのと
同じタイミングで、3種類の3連単の確率から同じルールで買い目を作り、manshu_source_compare に1レース3行で残す。

  model  : 「🤖 モデルのみ予想」の3連単の確率（trifecta(final)）
  claude : この比較専用の Claude の呼び出し（claude_reader.read_longshots。入力は read_race と同じ build_brief）で、
           1号艇以外が1着で中穴〜万舟（オッズ30倍以上）になりそうな組と確率を直接挙げてもらう。
           Claude が挙げた組だけを使い、モデルの確率では埋めない
  blend  : 組ごとに (モデルの確率 + Claude の確率) / 2。Claude が挙げなかった組の Claude の確率は 0
  claude_memo : claude と同じ入力・同じ呼び出しに、オーナーが自分で書いたメモ（manshu_memos）を
           「オーナーが自分で見た気配」として足したもの。メモの無いレースはモデルで埋めず「記録なし」
           （確率・買い目なし。集計に入れない）。memo_saved_at に使ったメモの保存日時を残す。
           自動記録の後・締切前にメモが保存されたら、その時点で作り直す（sync_memo。メモの保存で
           auto_post_predict.yml task=memo が起動する。取りこぼしは5分おきの task=run）。
           メモはこの比較にだけ使い、本番の予想・買い目・表示・通知・投稿には使わない

買い目の選び方は3種とも本番と同じ（manshu.aim_tickets。中穴5＋万舟5・確率順・1号艇1着を外す）。確率だけを
各予想のものに差し替える。claude は挙げた組だけから作るので、足りなければ10点より少ないまま（aim_count に点数）。
claude・blend の行の claude_listed_count は、Claude 自身が挙げた組の点数。

  - 1レース1回だけ（race_key, source が主キー。先に model の行を入れられた実行だけが Claude を呼ぶ）
  - Claude の費用は claude_usage に purpose = manshu_compare（メモ入りは manshu_compare_memo）で残す。今日（日本時間）の
    2つの合計が app_settings.manshu_compare_claude_daily_usd 以上なら Claude を呼ばず、行の error に理由を残す
    （呼ぶ前に確かめるので、上限を1回分だけ超えることはある）
  - 結果は既存の結果確定（list_results.py・翌朝の pre_picks.py）が万舟チャンスの結果と一緒に書く
  - 集計はビュー manshu_source_summary（saved_at が締切より前の行だけ）

データベース: supabase/migrations/20261028000000_manshu_source_compare.sql・20261030000000_manshu_memos.sql
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
TABLE = "manshu_source_compare"
SOURCES = ("model", "claude", "blend", "claude_memo")
MEMO_SOURCE = "claude_memo"
MEMO_TABLE = "manshu_memos"
NO_MEMO = "記録なし（メモなし）"
USAGE_PURPOSE = "manshu_compare"
MEMO_PURPOSE = "manshu_compare_memo"
LIMIT_SETTING = "manshu_compare_claude_daily_usd"
DEFAULT_DAILY_USD = 3.0


def method_name():
    """保存する買い目の作り方の名前（本番と同じ。例: MIX5+5-prob-no1）。"""
    import manshu

    return manshu.aim_method_name()


def compare_tickets(probs, odds):
    """3種共通の買い目（本番の中穴〜万舟狙いの買い目と同じ manshu.aim_tickets）。probs: {組番: 確率} / odds: {組番: オッズ}
    → [{combo, prob, odds, tier, stake, payout}]"""
    import manshu

    return manshu.aim_tickets(probs, odds)


def source_probs(model_tri, reading):
    """{source: {組番: 確率}}。Claude の答えが無ければ model だけ。model_tri: trifecta(final) の表（combo, prob）。
    reading: claude_reader.read_longshots の戻り値。claude は挙げた組だけ、blend は120通りの (モデル + Claude) / 2。"""
    import manshu

    out = {"model": manshu.tri_dict(model_tri[["combo", "prob"]])}
    if reading and reading.get("status") == "ok" and reading.get("tickets"):
        c = {str(t["combo"]): float(t["prob"]) for t in reading["tickets"]}
        out["claude"] = c
        out["blend"] = {k: (v + c.get(k, 0.0)) / 2.0 for k, v in out["model"].items()}
    return out


def deadline_at(race_date_iso, hhmm):
    """締切の日時（ISO・日本時間）。読めなければ None。"""
    try:
        return datetime.strptime(f"{race_date_iso} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=JST).isoformat()
    except (TypeError, ValueError):
        return None


def build_row(t, source, probs, odds, odds_at, error=None, claude_model=None, claude_listed_count=None,
              memo_saved_at=None):
    """manshu_source_compare の1行（結果の列は空）。probs が None なら確率・買い目なしで error だけ残す。
    claude_memo の行だけ memo_saved_at を持つ（ほかの行は列を送らない）。"""
    aim = compare_tickets(probs, odds) if probs and odds else []
    if probs and not error:
        if not odds:
            error = "オッズを取れませんでした"
        elif not aim:
            error = ("Claude が挙げた組にオッズ30倍以上の組がありません" if source in ("claude", MEMO_SOURCE)
                     else "1号艇以外が1着でオッズ30倍以上の組がありません")
    row = {
        "race_key": t["race_key"],
        "race_date": t["race_date"],
        "source": source,
        "tri_probs": {c: round(float(p), 6) for c, p in probs.items()} if probs else None,
        "aim_tickets": aim,
        "aim_method": method_name(),
        "odds_at": odds_at,
        "deadline_at": deadline_at(t["race_date"], t.get("deadline")),
        "aim_count": len(aim),
        "stake": sum(x["stake"] for x in aim) or None,
        "claude_model": claude_model,
        "claude_listed_count": claude_listed_count,
        "error": error,
    }
    if source == MEMO_SOURCE:
        row["memo_saved_at"] = memo_saved_at
    return row


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
    """今日（日本時間）の、この比較（メモ入りも含む）で Claude に使った額（ドル）。"""
    now = now or datetime.now(JST)
    start = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = sb("GET", "claude_usage", params=[("select", "usd"), ("purpose", f"in.({USAGE_PURPOSE},{MEMO_PURPOSE})"),
                                             ("called_at", f"gte.{start}")]).json() or []
    return sum(float(r.get("usd") or 0) for r in rows)


def read_claude(sb, t, race, final, api_key, read_model=None, now=None, memo=None):
    """この比較専用の Claude の呼び出し（read_longshots）。戻り値 (reading, error)。上限を超えていれば呼ばない。
    memo があればメモ入り（claude_memo）として呼ぶ。"""
    import claude_reader as cr
    from official_fetcher import VENUES

    if not api_key:
        return None, "ANTHROPIC_API_KEY が未設定です"
    limit, spent = daily_limit(sb), spent_today(sb, now)
    if spent >= limit:
        return None, f"1日の上限 ${limit:.2f} に達したためスキップ（今日 ${spent:.2f}）"
    # アプリの「Claudeのみ予想」と同じレース名（日付 会場 R）
    label = f"{t['race_date']} {VENUES.get(t['jcd'], t.get('venue') or '')} {t['race_no']}R"
    reading = cr.read_longshots(api_key, t["race_key"], race, final, model=read_model, race_label=label, memo=memo)
    row = cr.usage_row(reading, t["race_key"], MEMO_PURPOSE if memo else USAGE_PURPOSE)
    if row:
        try:
            sb("POST", "claude_usage", prefer="return=minimal", json=row)
        except Exception as e:  # noqa: BLE001  記録に失敗しても比較は残す
            print("[MANSHU_SOURCE] claude_usage の記録に失敗:", type(e).__name__, flush=True)
    if reading.get("status") != "ok":
        return reading, reading.get("error") or "Claude の答えを取れませんでした"
    return reading, None


def record(sb, t, race, final, odds, api_key="", read_model=None, now=None):
    """1レースの4種を記録する。戻り値は記録用の短い文（会場名・買い目・メモは入れない）。"""
    import manshu
    from prediction import trifecta

    model_tri = trifecta(final)[["combo", "prob"]]
    odds_d = manshu.odds_dict(odds)
    odds_at = datetime.now(JST).isoformat(timespec="seconds") if odds_d else None
    probs = source_probs(model_tri, None)
    if not insert(sb, [build_row(t, "model", probs["model"], odds_d, odds_at)]):
        return "記録済み"
    reading, err = read_claude(sb, t, race, final, api_key, read_model, now)
    probs = source_probs(model_tri, reading)
    cm = (reading or {}).get("served_model") or (reading or {}).get("model")
    listed = len(probs["claude"]) if "claude" in probs else None
    rows = [build_row(t, s, probs.get(s), odds_d, odds_at, error=None if s in probs else err, claude_model=cm,
                      claude_listed_count=listed)
            for s in ("claude", "blend")]
    insert(sb, rows)
    memo_text = record_memo(sb, t, race, final, model_tri, odds_d, odds_at, api_key, read_model, now)
    if err:
        return f"モデルだけ記録（Claude: {err}）・メモ入り: {memo_text}"
    return f"3種を記録（Claude が挙げた組 {listed}点・買い目 {len(rows[0]['aim_tickets'])}点）・メモ入り: {memo_text}"


# ---------------------------------------------------------------
# メモ入りの Claude（claude_memo）
# ---------------------------------------------------------------
def _ts(v):
    """データベースの時刻（文字列）を比べられる形に。読めなければ None。"""
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_memo(sb, race_key):
    """そのレースのメモ {memo, saved_at}。無ければ None。"""
    rows = sb("GET", f"{MEMO_TABLE}?race_key=eq.{race_key}&select=memo,saved_at").json() or []
    return rows[0] if rows and str(rows[0].get("memo") or "").strip() else None


def memo_row(sb, t, race, final, model_tri, odds_d, odds_at, api_key="", read_model=None, now=None, memo=None):
    """claude_memo の1行。メモが無ければ「記録なし」（Claude を呼ばない・モデルで埋めない）。"""
    if not memo:
        return build_row(t, MEMO_SOURCE, None, odds_d, odds_at, error=NO_MEMO)
    reading, err = read_claude(sb, t, race, final, api_key, read_model, now, memo=memo["memo"])
    probs = source_probs(model_tri, reading)
    cm = (reading or {}).get("served_model") or (reading or {}).get("model")
    c = probs.get("claude")
    return build_row(t, MEMO_SOURCE, c, odds_d, odds_at, error=None if c else err, claude_model=cm,
                     claude_listed_count=len(c) if c else None, memo_saved_at=memo["saved_at"])


def _memo_text(row):
    if row.get("memo_saved_at") is None:
        return "記録なし"
    if row.get("error"):
        return f"作れませんでした（{row['error']}）"
    return f"記録（Claude が挙げた組 {row['claude_listed_count']}点・買い目 {row['aim_count']}点）"


def record_memo(sb, t, race, final, model_tri, odds_d, odds_at, api_key="", read_model=None, now=None):
    """自動記録のときの claude_memo（まだ無いときだけ入れる）。失敗しても3種の記録は残す。戻り値は短い文。"""
    try:
        row = memo_row(sb, t, race, final, model_tri, odds_d, odds_at, api_key, read_model, now,
                       load_memo(sb, t["race_key"]))
        insert(sb, [row])
        return _memo_text(row)
    except Exception as e:  # noqa: BLE001  メモの表・列がまだ無いデータベースなど
        return f"エラー（{type(e).__name__}）"


def memo_state(sb, race_key):
    """(メモ, claude_memo の行)。どちらも無ければ None。"""
    rows = sb("GET", f"{TABLE}?race_key=eq.{race_key}&source=eq.{MEMO_SOURCE}"
                     "&select=memo_saved_at,saved_at,result_at").json() or []
    return load_memo(sb, race_key), (rows[0] if rows else None)


def needs_memo_update(memo, row):
    """claude_memo を作り直すか。今のメモ（無ければ None）と、記録済みの行を比べる。結果の出た行は触らない。"""
    if row and row.get("result_at"):
        return False
    used = _ts((row or {}).get("memo_saved_at"))
    if memo is None:
        return used is not None  # メモを消した: 「記録なし」に戻す
    return used is None or _ts(memo.get("saved_at")) != used


def upsert(sb, rows):
    """同じレース・同じ種類の行を書き換える（結果の列は送らないので残る）。"""
    return sb("POST", f"{TABLE}?on_conflict=race_key,source",
              prefer="resolution=merge-duplicates,return=minimal", json=rows)


def sync_memo(sb, t, race, final, odds, api_key="", read_model=None, now=None, memo=None):
    """自動記録の後に保存・変更・削除されたメモで claude_memo を作り直す。memo は load_memo の戻り値。
    戻り値は記録用の短い文（メモ・会場名・買い目は入れない）。"""
    import manshu
    from prediction import trifecta

    model_tri = trifecta(final)[["combo", "prob"]]
    odds_d = manshu.odds_dict(odds)
    odds_at = datetime.now(JST).isoformat(timespec="seconds") if odds_d else None
    row = memo_row(sb, t, race, final, model_tri, odds_d, odds_at, api_key, read_model, now, memo)
    # 作り直すときは前の行の内容を全部置き換える（記録なしに戻すときは古い確率・買い目を残さない）
    row.setdefault("claude_model", None)
    row["saved_at"] = datetime.now(JST).isoformat(timespec="seconds")
    upsert(sb, [row])
    return "作り直し: " + _memo_text(row)


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
