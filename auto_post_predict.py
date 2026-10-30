"""
展示後の自動予想: その日の「🌅 展示前厳選10」と「💥 万舟チャンス」のレース（重複は1回）を、
締切の20〜5分前に、予想タブの「🤖 モデルのみ予想」と同じ予想（本番の新モデル）で予想して保存する。

  python auto_post_predict.py run                      5分おき（pg_cron → auto_post_predict.yml）
  python auto_post_predict.py plan   [--date YYYYMMDD] [--delay 分]   どのレースが何時に動くか（保存はしない）
  python auto_post_predict.py report [--date YYYYMMDD]              その日の対象・予想できた数・見送った数（理由つき）
  python auto_post_predict.py memo   --race-key YYYYMMDD_JJ_R        メモ入りの Claude 予想を作り直す（メモの保存で起動）
  python auto_post_predict.py notify-test                            メモ入り予想の通知先（Discord）にテスト通知を送る

  - 締切は朝に保存している当日の締切時刻（daily_schedule.deadlines）。無ければ一覧に保存した締切
  - 中止の会場は見送り（理由「中止」）。途中から中止になった会場は、締切から45分たっても一覧に結果が無い
    見送りのレースの理由を「中止（会場が途中から中止）」に書き直す（relabel_cancelled）
  - 締切20〜5分前のレースで、展示タイムが6艇そろっていれば予想する。そろっていなければ次の回（5分後）に
    もう一度。5分前を過ぎてもそろわなければ見送り、理由を auto_post_log に残す
  - 1レース1回だけ。展示後の予想が保存済み（手動・自動固定）ならスキップ。展示前に保存された予想は上書きする
  - 保存は手動と同じ save_latest_prediction_snapshot()。collector_name = auto_post（自動固定 auto_random とは別。
    自動固定の1日の件数・結果の自動記録には数えない）。保存するとトリガーが一覧の展示後の欄（post_*）を書く
  - 万舟チャンスのレースは、展示後の3連単の確率とその時点のオッズで中穴〜万舟狙いの買い目を作り直して
    保存する（予想タブの「展示後に作り直す」と同じ。aim_source = auto）
  - 保存する予想に Claude の予想は使わない（手動のまま）。ただし万舟チャンスのレースだけ、買い目を作り直すのと
    同じタイミングで「モデルのみ／Claudeのみ／両方の予想平均」の3種の比較を記録する（manshu_source.py。
    展示後の予想が保存済みでスキップしたレースも記録する。Claude の費用は1日の上限あり）
  - 比較の「メモ入りの Claude（claude_memo）」と「メモ入りの平均（blend_memo）」は、記録のときにオーナーのメモ
    （manshu_memos）があれば作り、無ければ「記録なし」。記録の後・締切前にメモが保存・変更されたら作り直す（メモの保存が task=memo を起動。
    取りこぼしは task=run が毎回確かめる）。メモは比較にだけ使い、保存する予想・買い目・ログには入れない。
    作ったとき・作り直したときは、オーナー専用の Discord（DISCORD_MEMO_WEBHOOK_URL）にだけメモ入り予想を通知する（memo_notify.py）

ログは誰でも見られるので、会場名・確率・買い目は出さない（件数・締切時刻・理由・合否だけ）。
レースごとの記録は Supabase の auto_post_log にある。
"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

JST = ZoneInfo("Asia/Tokyo")
COLLECTOR = "auto_post"
LOG_TABLE = "auto_post_log"
# 締切の何分前から何分前までに予想するか
WINDOW_FROM = 20
WINDOW_TO = 5
# pg_cron の起動間隔（分）と、plan の既定の起動時間帯（JST）
CRON_STEP = 5
CRON_START = "08:00"
CRON_END = "22:55"
# plan で見込む、pg_cron の起動から予想を始めるまでの時間（GitHub Actions の起動・準備）
DEFAULT_DELAY = 3
FINAL = ("done", "skipped", "already")
SOURCE_LABELS = {"pre": "展示前厳選10", "manshu": "万舟チャンス"}
NEVER_RAN = "締切20〜5分前に一度も確認できませんでした（起動の遅れ・欠落など）"


def log(*a):
    print("[AUTO_POST]", *a, flush=True)


def _today():
    return datetime.now(JST).strftime("%Y%m%d")


def _iso(date8):
    return f"{date8[:4]}-{date8[4:6]}-{date8[6:]}"


# ---------------------------------------------------------------
# 対象レースと締切（Supabase から読むものを組み合わせる。ここは通信しない）
# ---------------------------------------------------------------
def merge_targets(pre_rows, manshu_rows):
    """展示前厳選10と万舟チャンスの行を1レース1件にまとめる（sources に両方入る）。締切順。"""
    out = {}
    for src, rows in (("pre", pre_rows), ("manshu", manshu_rows)):
        for r in rows or []:
            rk = str(r["race_key"])
            t = out.setdefault(rk, {
                "race_key": rk,
                "race_date": str(r.get("race_date") or _iso(rk[:8])),
                "jcd": str(r.get("jcd") or rk.split("_")[1]).zfill(2),
                "venue": r.get("venue"),
                "race_no": int(r.get("race_no") or rk.split("_")[2]),
                "deadline": r.get("deadline"),
                "sources": [],
            })
            t["sources"].append(src)
            if not t.get("deadline") and r.get("deadline"):
                t["deadline"] = r["deadline"]
    return sorted(out.values(), key=lambda t: (str(t.get("deadline") or "99:99"), t["race_key"]))


def apply_schedule(targets, schedule_rows):
    """朝に保存した当日の締切時刻（daily_schedule.deadlines）で締切を上書きし、中止の会場に印を付ける。"""
    by_jcd = {str(s.get("jcd")).zfill(2): s for s in schedule_rows or []}
    for t in targets:
        s = by_jcd.get(t["jcd"])
        if not s:
            continue
        hhmm = (s.get("deadlines") or {}).get(str(t["race_no"]))
        if hhmm:
            t["deadline"] = str(hhmm)
        if str(s.get("status") or "").strip() == "中止":
            t["cancelled"] = True
    return targets


def minutes_left(race_date_iso, hhmm, now):
    """締切までの分。締切が読めなければ None。"""
    try:
        dl = datetime.strptime(f"{race_date_iso} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except (TypeError, ValueError):
        return None
    return (dl - now).total_seconds() / 60


def phase(left):
    """early（まだ）/ window（締切20〜5分前。予想する）/ late（5分前を過ぎた）/ unknown（締切不明）。"""
    if left is None:
        return "unknown"
    if left > WINDOW_FROM:
        return "early"
    if left >= WINDOW_TO:
        return "window"
    return "late"


def plan(targets, date8, delay=DEFAULT_DELAY, start=CRON_START, end=CRON_END, step=CRON_STEP):
    """pg_cron が step 分おきに start〜end に起動し、delay 分後に確認するとして、各レースを確認する時刻。
    戻り値 [(target, [確認する時刻 datetime, ...])]（最初の時刻で展示がそろえば予想、そろわなければ次の時刻）。"""
    d = datetime.strptime(date8, "%Y%m%d").replace(tzinfo=JST)
    t0 = d.replace(hour=int(start[:2]), minute=int(start[3:]))
    t1 = d.replace(hour=int(end[:2]), minute=int(end[3:]))
    ticks = []
    t = t0
    while t <= t1:
        ticks.append(t + timedelta(minutes=delay))
        t += timedelta(minutes=step)
    out = []
    for tg in targets:
        runs = [x for x in ticks
                if not tg.get("cancelled")
                and phase(minutes_left(tg["race_date"], tg.get("deadline"), x)) == "window"]
        out.append((tg, runs))
    return out


def format_plan(planned, detail=True):
    """plan の結果を文に。detail=False（Actions のログ）では会場名を出さない。"""
    lines = []
    for tg, runs in planned:
        name = f"{tg.get('venue') or tg['jcd']} {tg['race_no']}R" if detail else f"締切 {tg.get('deadline')}"
        src = "・".join(SOURCE_LABELS[s] for s in tg["sources"])
        if tg.get("cancelled"):
            when = "中止のため動かない"
        elif not runs:
            when = "動かない（締切20〜5分前が起動の時間帯の外）"
        else:
            when = " → ".join(x.strftime("%H:%M") for x in runs) + f"（{len(runs)}回まで）"
        head = f"{name} 締切 {tg.get('deadline') or '--:--'}" if detail else name
        lines.append(f"{head}［{src}］ {when}")
    return lines


# ---------------------------------------------------------------
# Supabase（service_role。自動固定と同じ接続）
# ---------------------------------------------------------------
def _sb(method, path, **kwargs):
    from auto_random_fix import _cfg, _headers, _request

    url, _ = _cfg()
    prefer = kwargs.pop("prefer", None)
    return _request(method, f"{url}/rest/v1/{path}", headers=_headers(prefer), timeout=30, **kwargs)


def load_targets(date8):
    iso = _iso(date8)
    cols = "race_key,race_date,jcd,venue,race_no,deadline"
    pre = _sb("GET", f"pre_exhibition_picks?race_date=eq.{iso}&select={cols}").json() or []
    try:
        man = _sb("GET", f"manshu_chance_picks?race_date=eq.{iso}&select={cols}").json() or []
    except Exception as e:  # noqa: BLE001  万舟チャンスが読めなくても厳選10は続ける
        log(f"万舟チャンスを読めませんでした（{type(e).__name__}）")
        man = []
    sched = _sb("GET", f"daily_schedule?race_date=eq.{date8}&select=jcd,deadlines,status").json() or []
    return apply_schedule(merge_targets(pre, man), sched)


def load_log(date8):
    rows = _sb("GET", f"{LOG_TABLE}?race_date=eq.{_iso(date8)}&select=*").json() or []
    return {r["race_key"]: r for r in rows}


def write_log(t, prev, status, reason=None, tried=False, **extra):
    now = datetime.now(JST).isoformat(timespec="seconds")
    row = {
        "race_key": t["race_key"],
        "race_date": t["race_date"],
        "jcd": t["jcd"],
        "venue": t.get("venue"),
        "race_no": t["race_no"],
        "deadline": t.get("deadline"),
        "sources": t["sources"],
        "status": status,
        "reason": reason,
        "attempts": int((prev or {}).get("attempts") or 0) + (1 if tried else 0),
        "last_tried_at": now if tried else (prev or {}).get("last_tried_at"),
        "done_at": now if status in FINAL else None,
        "snapshot_status": extra.get("snapshot_status", (prev or {}).get("snapshot_status")),
        "aim_status": extra.get("aim_status", (prev or {}).get("aim_status")),
        "updated_at": now,
    }
    _sb("POST", f"{LOG_TABLE}?on_conflict=race_key", prefer="resolution=merge-duplicates,return=minimal", json=row)
    return row


def snapshot_meta(race_key):
    rows = _sb("GET", f"prediction_snapshots?race_key=eq.{race_key}"
                      "&select=collector_name,pre_exhibition,snapshot_kind&limit=1").json() or []
    return rows[0] if rows else None


def already_reason(meta):
    """展示後の予想が保存済みなら (status, 理由)。展示前の予想だけ・未保存なら None（予想する）。"""
    if not meta:
        return None
    who = str(meta.get("collector_name") or "")
    if who == COLLECTOR:
        return "done", "展示後の自動予想で保存済み"
    if who == "auto_random" or meta.get("snapshot_kind") == "auto_random":
        return "already", "自動固定で保存済み"
    if meta.get("pre_exhibition"):
        return None
    return "already", "展示後の予想が保存済み（手動）"


# ---------------------------------------------------------------
# 予想して保存する（1レース）
# ---------------------------------------------------------------
class Context:
    """1回の実行で使い回すもの（今までのモデルの学習は、予想するレースがあるときだけ1回）。"""

    def __init__(self):
        from auto_random_fix import _load_settings, _runtime_settings

        self.settings = _load_settings()
        self.runtime = _runtime_settings(self.settings)
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from data_paths import data_path
            from prediction import train

            self._model = train(pd.read_csv(data_path("sample_history.csv")))
        return self._model


def exhibition_count(race):
    if race is None or "exhibition_time" not in race.columns:
        return 0
    t = pd.to_numeric(race["exhibition_time"], errors="coerce")
    return int(t.between(6.0, 8.5).sum())


def predict_race(ctx, t, race, odds):
    """「モデルのみ予想」と同じ予想・買い目を出し、保存する予想（payload_json）を作る。"""
    import hit_calibration
    import result_tracker
    from model_only import model_only_prediction
    from prediction import confidence

    date8 = t["race_key"][:8]
    work = race.copy()
    work["date"] = t["race_date"]
    work["venue"] = t.get("venue")
    work["race_no"] = t["race_no"]
    r = ctx.runtime
    params = {k: r[k] for k in ("display_weight", "weather_weight", "venue_course_weight", "hedge_enabled",
                                "longshot_min_prob_pct", "total_budget", "min_bet", "value_bias")}
    res = model_only_prediction(ctx.model, work, odds, params, ctx.settings, date8, t["jcd"], t["race_no"])
    res["work"] = work
    # AI総合信頼度(A/B/C)は confidence() が final に記録する（アプリと同じく保存の前に1回）
    try:
        confidence(res["final"], work)
    except Exception:  # noqa: BLE001
        pass
    # アプリは Streamlit の Secrets で的中確率の補正を読む。ここでは自動固定と同じく環境変数の接続で読む
    # （新しいモデルの予想には補正を使わないので、今までのモデルに戻ったときだけ効く）
    result_tracker.load_hit_calibration = hit_calibration.latest_from_env
    payload_json, payload_hash = result_tracker.build_snapshot_payload_json(
        res["final"],
        res["tickets"],
        research_variants=res.get("research_variants") or {},
        race_features=work,
        pre_exhibition=False,
        extra={
            **({"prediction_model": res["model_info"]} if res.get("model_info") else {}),
            # 展示後の自動予想の印（自動固定 auto_random とは別）。同じ予想なら同じ内容になるよう時刻は入れない
            "auto_post": {"sources": t["sources"]},
        },
    )
    return res, payload_json, payload_hash


def save_snapshot(t, payload_json, payload_hash):
    """手動の予想と同じ関数で保存する（締切後・自動固定済みは書かない。展示前の予想は上書きする）。"""
    r = _sb("POST", "rpc/save_latest_prediction_snapshot", json={
        "p_race_key": t["race_key"],
        "p_venue": t.get("venue") or "",
        "p_collector_name": COLLECTOR,
        "p_saved_at": datetime.now(JST).isoformat(timespec="seconds"),
        "p_payload_json": payload_json,
        "p_payload_hash": payload_hash,
        "p_pre_exhibition": False,
    })
    data = r.json()
    if not isinstance(data, dict) or not data.get("status"):
        raise RuntimeError("保存の結果を読めませんでした")
    return data["status"]


def start_odds_tracking(t, odds):
    """手動の予想と同じく、オッズ追跡に登録する（締切後に結果を prediction_results に記録するため）。"""
    from auto_random_fix import _add_to_odds_watchlist, _save_odds_snapshot_now

    try:
        _add_to_odds_watchlist(t["race_key"][:8], t["jcd"], t["race_no"])
        _save_odds_snapshot_now(t["race_key"][:8], t["jcd"], t["race_no"], odds)
    except Exception as e:  # noqa: BLE001  予想の保存は取り消さない
        log(f"締切 {t.get('deadline')}: オッズ追跡の登録に失敗（{type(e).__name__}）")


def rebuild_aim(t, final, odds):
    """万舟チャンス: 展示後の3連単の確率とその時点のオッズで中穴〜万舟狙いの買い目を作り直して保存する。
    戻り値は記録用の短い文。"""
    import manshu
    from prediction import trifecta

    probs = manshu.tri_dict(trifecta(final)[["combo", "prob"]])
    odds_d = manshu.odds_dict(odds)
    if not odds_d:
        return "オッズを取れず作り直せませんでした"
    aim = manshu.aim_tickets(probs, odds_d)
    if not aim:
        return "オッズ30倍以上の組が無く作り直せませんでした"
    ok = _sb("POST", "rpc/manshu_save_aim", json={
        "p_race_key": t["race_key"], "p_tickets": aim, "p_method": manshu.aim_method_name(),
        "p_source": "auto", "p_odds_at": datetime.now(JST).isoformat(timespec="seconds"),
    }).json()
    return f"作り直して保存（{len(aim)}点）" if ok else "保存されませんでした（締切後・結果記録済み）"


def compare_sources(ctx, t, final=None, work=None, odds=None):
    """万舟チャンス: 予想3種（モデルのみ／Claudeのみ／両方の予想平均）の比較を記録する（manshu_source.py）。
    final が無い（展示後の予想が保存済みでスキップした）ときは、ここで展示とオッズを読んで予想し直す。
    失敗しても自動予想は止めない。戻り値は記録用の短い文。"""
    import manshu_source

    try:
        if manshu_source.recorded(_sb, t["race_key"]):
            return "記録済み"
        if final is None:
            from official_fetcher import fetch_odds3t, fetch_official_race

            date8 = t["race_key"][:8]
            race = fetch_official_race(date8, t["jcd"], t["race_no"])
            n = exhibition_count(race)
            if race is None or len(race) != 6 or n != 6:
                return f"展示タイムがそろっていないため記録しませんでした（{n}/6）"
            try:
                odds = fetch_odds3t(date8, t["jcd"], t["race_no"])
            except Exception:  # noqa: BLE001
                odds = None
            res, _, _ = predict_race(ctx, t, race, odds)
            final, work = res["final"], res["work"]
        return manshu_source.record(
            _sb, t, work, final, odds,
            api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            read_model=os.environ.get("CLAUDE_READ_MODEL", "").strip() or None,
        )
    except Exception as e:  # noqa: BLE001
        return f"エラー（{type(e).__name__}）"


def pending_memos(targets, now, race_key=None):
    """メモ入りの Claude 予想を作り直すレース [(t, メモ)]: 万舟チャンスで締切前、自動記録（model の行）が済み、
    今のメモと記録（claude_memo の memo_saved_at）が食い違うレース。記録の前のレースは記録のときにメモを使う。"""
    import manshu_source as ms

    keys = {}
    for t in targets:
        left = minutes_left(t["race_date"], t.get("deadline"), now)
        if ("manshu" in t["sources"] and not t.get("cancelled") and left is not None and left > 0
                and race_key in (None, t["race_key"])):
            keys[t["race_key"]] = t
    if not keys:
        return []
    inn = f"in.({','.join(keys)})"
    rows = _sb("GET", f"{ms.TABLE}?race_key={inn}&source=in.(model,{ms.MEMO_SOURCE})"
                      "&select=race_key,source,memo_saved_at,result_at").json() or []
    memos = {m["race_key"]: m for m in _sb("GET", f"{ms.MEMO_TABLE}?race_key={inn}&select=race_key,memo,saved_at").json()
             or [] if str(m.get("memo") or "").strip()}
    out = []
    for k, t in keys.items():
        mine = {r["source"]: r for r in rows if r["race_key"] == k}
        if "model" in mine and ms.needs_memo_update(memos.get(k), mine.get(ms.MEMO_SOURCE)):
            out.append((t, memos.get(k)))
    return out


def rebuild_memo(ctx, t, memo):
    """メモ入りの Claude 予想（claude_memo・blend_memo）を、その時点の展示・オッズで予想し直して作り直す。戻り値は記録用の短い文。"""
    import manshu_source
    from official_fetcher import fetch_odds3t, fetch_official_race

    try:
        date8 = t["race_key"][:8]
        race = fetch_official_race(date8, t["jcd"], t["race_no"])
        n = exhibition_count(race)
        if race is None or len(race) != 6 or n != 6:
            return f"展示タイムがそろっていないため作り直しませんでした（{n}/6）"
        try:
            odds = fetch_odds3t(date8, t["jcd"], t["race_no"])
        except Exception:  # noqa: BLE001
            odds = None
        res, _, _ = predict_race(ctx, t, race, odds)
        return manshu_source.sync_memo(
            _sb, t, res["work"], res["final"], odds, api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            read_model=os.environ.get("CLAUDE_READ_MODEL", "").strip() or None, memo=memo)
    except Exception as e:  # noqa: BLE001
        return f"エラー（{type(e).__name__}）"


def sync_memos(ctx, targets, race_key=None):
    """メモが自動記録の後に保存・変更されたレースの claude_memo・blend_memo を作り直す。戻り値は使った Context（無ければ None）。"""
    try:
        todo = pending_memos(targets, datetime.now(JST), race_key)
    except Exception as e:  # noqa: BLE001  メモの表がまだ無いデータベースなど。自動予想は止めない
        log(f"メモの確認に失敗（{type(e).__name__}）")
        return ctx
    for t, memo in todo:
        ctx = ctx or Context()
        log(f"締切 {t.get('deadline')}: メモ入りの予想 {rebuild_memo(ctx, t, memo)}")
    if race_key and not todo:
        log("メモ入りの予想: 作り直すものはありません（締切後・自動記録の前・作り直し済み）")
    return ctx


def handle_window(ctx, t, prev):
    """締切20〜5分前の1レース。戻り値は書いた状態。"""
    from official_fetcher import fetch_odds3t, fetch_official_race

    meta = snapshot_meta(t["race_key"])
    done = already_reason(meta)
    if done:
        status = write_log(t, prev, done[0], done[1])["status"]
        if "manshu" in t["sources"]:
            log(f"締切 {t.get('deadline')}: 予想3種の比較 {compare_sources(ctx, t)}")
        return status
    date8 = t["race_key"][:8]
    try:
        race = fetch_official_race(date8, t["jcd"], t["race_no"])
    except Exception as e:  # noqa: BLE001
        return write_log(t, prev, "waiting", f"公式サイトを読めませんでした（{type(e).__name__}）", tried=True)["status"]
    n = exhibition_count(race)
    if race is None or len(race) != 6 or n != 6:
        return write_log(t, prev, "waiting", f"展示タイムがそろっていません（{n}/6）", tried=True)["status"]
    try:
        odds = fetch_odds3t(date8, t["jcd"], t["race_no"])
    except Exception:  # noqa: BLE001  オッズが無くても予想はできる（アプリと同じ）
        odds = None
    try:
        res, payload_json, payload_hash = predict_race(ctx, t, race, odds)
        status = save_snapshot(t, payload_json, payload_hash)
    except Exception as e:  # noqa: BLE001  次の回（締切5分前まで）にもう一度
        log(f"締切 {t.get('deadline')}: 予想・保存でエラー {type(e).__name__}: {str(e)[:200]}")
        return write_log(t, prev, "waiting", f"予想・保存でエラー（{type(e).__name__}）", tried=True)["status"]
    if status == "auto_fixed":
        return write_log(t, prev, "already", "自動固定で保存済み", tried=True, snapshot_status=status)["status"]
    if status == "closed":
        return write_log(t, prev, "skipped", "締切後のため保存されませんでした", tried=True,
                         snapshot_status=status)["status"]
    start_odds_tracking(t, odds)
    aim_status = None
    if "manshu" in t["sources"]:
        try:
            aim_status = rebuild_aim(t, res["final"], odds)
        except Exception as e:  # noqa: BLE001
            aim_status = f"作り直しでエラー（{type(e).__name__}）"
        log(f"締切 {t.get('deadline')}: 予想3種の比較 "
            f"{compare_sources(ctx, t, res['final'], res.get('work'), odds)}")
    model = (res.get("model_info") or {}).get("model_used")
    return write_log(t, prev, "done", f"予想して保存（{status}・{model}）", tried=True,
                     snapshot_status=status, aim_status=aim_status)["status"]


def handle_late(t, prev):
    """締切5分前を過ぎても予想していないレース: 見送りとして記録する（展示後の予想が保存済みならそれを記録）。"""
    if t.get("cancelled"):
        return write_log(t, prev, "skipped", "中止")["status"]
    try:
        done = already_reason(snapshot_meta(t["race_key"]))
    except Exception:  # noqa: BLE001
        done = None
    if done:
        return write_log(t, prev, done[0], done[1])["status"]
    why = (prev or {}).get("reason") if (prev or {}).get("attempts") else None
    return write_log(t, prev, "skipped", f"見送り: {why}" if why else NEVER_RAN)["status"]


# 途中から中止になった会場: 中止の印（daily_schedule.status）は30分おきにしか更新されないので、締切の時点では
# まだ分からず「展示タイムがそろっていません」などで見送りにしたレースがある。会場が中止になり、締切から
# CANCEL_RELABEL_AFTER 分たっても結果が無いレースは、理由を「中止」に書き直す（走ったレースは一覧の結果確定
# list_results.py が締切20分後から結果を書くので、結果があれば書き直さない）
CANCEL_RELABEL_AFTER = 45
CANCELLED_MIDDAY = "中止（会場が途中から中止）"
RESULT_TABLES = ("pre_exhibition_picks", "manshu_chance_picks")


def relabel_candidates(targets, logged, now):
    """理由を「中止」に書き直す候補: 中止の会場で、展示待ちなどで見送りにし、締切から時間がたったレース。"""
    out = []
    for t in targets:
        r = logged.get(t["race_key"]) or {}
        why = str(r.get("reason") or "")
        if not t.get("cancelled") or r.get("status") != "skipped" or why.startswith("中止"):
            continue
        left = minutes_left(t["race_date"], t.get("deadline"), now)
        if left is not None and left <= -CANCEL_RELABEL_AFTER:
            out.append(t)
    return out


def settled_races(date8):
    """一覧（展示前厳選10・万舟チャンス）で結果（払戻）が出たレース {race_key}。中止・不成立（void）は入れない
    （中止のレースは翌朝の記録で void になるので、結果が書かれたかどうかでは走ったか分からない）。"""
    out = set()
    for table in RESULT_TABLES:
        rows = _sb("GET", f"{table}?race_date=eq.{_iso(date8)}&result_status=eq.settled&select=race_key").json() or []
        out |= {str(r["race_key"]) for r in rows}
    return out


def relabel_cancelled(date8, targets, logged, now):
    """途中から中止になった会場の、結果の無い見送りのレースの理由を「中止」に書き直す。書き直した数を返す。"""
    cands = relabel_candidates(targets, logged, now)
    if not cands:
        return 0
    try:
        settled = settled_races(date8)
    except Exception as e:  # noqa: BLE001  結果が読めなければ書き直さない（次の回にもう一度）
        log(f"中止の書き直し: 結果を読めませんでした（{type(e).__name__}）")
        return 0
    n = 0
    for t in cands:
        if t["race_key"] in settled:
            continue
        prev = logged[t["race_key"]]
        logged[t["race_key"]] = write_log(t, prev, "skipped", CANCELLED_MIDDAY)
        n += 1
    if n:
        log(f"{date8}: 途中から中止の会場のレースを中止として記録 {n}")
    return n


def run(date8=None, now=None):
    date8 = date8 or _today()
    now = now or datetime.now(JST)
    targets = load_targets(date8)
    if not targets:
        log(f"{date8}: 対象レースがありません（展示前厳選10・万舟チャンスが未選定）")
        return Counter()
    logged = load_log(date8)
    try:
        relabel_cancelled(date8, targets, logged, now)
    except Exception as e:  # noqa: BLE001  書き直しに失敗しても予想は続ける
        log(f"中止の書き直しに失敗（{type(e).__name__}）")
    ctx = None
    counts = Counter()
    for t in targets:
        prev = logged.get(t["race_key"])
        if prev and prev.get("status") in FINAL:
            continue
        left = minutes_left(t["race_date"], t.get("deadline"), now)
        p = "late" if t.get("cancelled") else phase(left)
        if p in ("early", "unknown"):
            continue
        if p == "late":
            st = handle_late(t, prev)
        else:
            ctx = ctx or Context()
            began = datetime.now(JST)
            st = handle_window(ctx, t, prev)
            now += datetime.now(JST) - began  # 予想に時間がかかった分だけ進める
        counts[st] += 1
        log(f"締切 {t.get('deadline')}（あと{left:.0f}分）: {st}" if left is not None else f"{st}")
    if not counts:
        log(f"{date8}: 締切20〜5分前のレースはありません（対象 {len(targets)}レース）")
    else:
        log(f"{date8}: " + "・".join(f"{k} {v}" for k, v in counts.items()))
    sync_memos(ctx, targets)
    return counts


def run_memo(race_key):
    """メモの保存で起動: そのレースのメモ入りの Claude 予想を作り直す（必要なときだけ）。"""
    targets = load_targets(race_key[:8])
    sync_memos(None, targets, race_key)


# ---------------------------------------------------------------
# 夜の集計・事前の確認
# ---------------------------------------------------------------
STATUS_LABELS = {"done": "予想できた", "already": "予想済みでスキップ", "skipped": "見送り", "waiting": "未確定"}


def summarize(targets, logged, now):
    """その日の対象・予想できた数・見送った数（理由ごと）。締切を過ぎて記録の無いレースは見送り扱い。"""
    out = {"targets": len(targets), "pre": 0, "manshu": 0, "both": 0, "status": Counter(),
           "reasons": Counter(), "aim": Counter(), "skipped": []}
    for t in targets:
        out["pre"] += "pre" in t["sources"]
        out["manshu"] += "manshu" in t["sources"]
        out["both"] += len(t["sources"]) == 2
        r = logged.get(t["race_key"]) or {}
        st, why = r.get("status"), r.get("reason")
        left = minutes_left(t["race_date"], t.get("deadline"), now)
        if st not in FINAL and (t.get("cancelled") or (left is not None and left < WINDOW_TO)):
            st = "skipped"
            why = "中止" if t.get("cancelled") else (f"見送り: {why}" if why and r.get("attempts") else NEVER_RAN)
        st = st or "waiting"
        out["status"][st] += 1
        if st in ("skipped", "already"):
            out["reasons"][why or "理由の記録なし"] += 1
        if st == "skipped":
            out["skipped"].append((t.get("deadline"), why))
        if r.get("aim_status"):
            out["aim"][r["aim_status"].split("（")[0]] += 1
    return out


def report(date8=None, now=None, write=True):
    date8 = date8 or _today()
    now = now or datetime.now(JST)
    targets = load_targets(date8)
    logged = load_log(date8)
    if write:
        # 締切を過ぎて記録が確定していないレースを見送りとして書く（実行が止まっていた日も残るように）
        for t in targets:
            prev = logged.get(t["race_key"])
            left = minutes_left(t["race_date"], t.get("deadline"), now)
            if (not prev or prev.get("status") not in FINAL) and (
                    t.get("cancelled") or (left is not None and left < WINDOW_TO)):
                try:
                    handle_late(t, prev)
                except Exception as e:  # noqa: BLE001
                    log(f"記録に失敗（{type(e).__name__}）")
        logged = load_log(date8)
        try:
            relabel_cancelled(date8, targets, logged, now)
        except Exception as e:  # noqa: BLE001
            log(f"中止の書き直しに失敗（{type(e).__name__}）")
    s = summarize(targets, logged, now)
    log(f"===== {date8} 展示後の自動予想 =====")
    log(f"対象 {s['targets']}レース（展示前厳選10 {s['pre']}・万舟チャンス {s['manshu']}・両方に入る {s['both']}）")
    for k in ("done", "already", "skipped", "waiting"):
        log(f"  {STATUS_LABELS[k]}: {s['status'].get(k, 0)}")
    for why, n in s["reasons"].most_common():
        log(f"    理由: {why} … {n}")
    for dl, why in sorted(s["skipped"], key=lambda x: str(x[0])):
        log(f"    見送り 締切 {dl}: {why}")
    if s["aim"]:
        log("  万舟狙いの買い目: " + "・".join(f"{k} {v}" for k, v in s["aim"].items()))
    return s


def main():
    ap = argparse.ArgumentParser(description="展示後の自動予想（展示前厳選10・万舟チャンス）")
    ap.add_argument("task", choices=["run", "plan", "report", "memo", "notify-test"])
    ap.add_argument("--date", default=None, help="YYYYMMDD（既定は今日 JST）")
    ap.add_argument("--delay", type=int, default=DEFAULT_DELAY, help="plan: 起動から確認までの見込み（分）")
    ap.add_argument("--race-key", default=None, help="memo: 作り直すレース（YYYYMMDD_JJ_R）")
    args = ap.parse_args()
    if args.task == "run":
        run(args.date)
    elif args.task == "memo":
        if not args.race_key:
            ap.error("memo には --race-key が必要です")
        run_memo(args.race_key)
    elif args.task == "report":
        report(args.date)
    elif args.task == "notify-test":
        import memo_notify

        raise SystemExit(0 if memo_notify.send_test() else 1)
    else:
        date8 = args.date or _today()
        targets = load_targets(date8)
        detail = not os.environ.get("GITHUB_ACTIONS")
        log(f"{date8}: 対象 {len(targets)}レース（起動 {CRON_START}〜{CRON_END} の{CRON_STEP}分おき・"
            f"起動から{args.delay}分後に確認すると見込む）")
        for line in format_plan(plan(targets, date8, delay=args.delay), detail=detail):
            log("  " + line)


if __name__ == "__main__":
    main()
