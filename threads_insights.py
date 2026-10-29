"""
スレッズのインサイト（閲覧数・いいね等）を毎日1回 Supabase にためる。

オッズ追跡（track_odds.yml・5分おき）の最後のステップで動く。JST COLLECT_AFTER 以降の最初の回に、
その日の分を1回だけ取る（threads_account_insights のその日の行が done なら何もしない）。

  python threads_insights.py run [--force] [--backfill]

  1. 自分の投稿（/me/threads と /me/replies）を読み、threads_posts に1件1行で記録する。
     種類（朝の自動投稿・結果の返信・アプリからの1レース投稿・本日のまとめ・手動）とどのレースの投稿かは、
     それぞれの投稿の記録（threads_auto_posts / threads_result_posts / threads_race_posts / threads_posts）の
     投稿IDで紐付ける。記録に無い投稿は本文の型から見分け、どれでもなければ手動（manual）
  2. 投稿から POST_DAYS 日以内の投稿の数値（views・likes・replies・reposts・quotes・shares）を
     /{投稿ID}/insights から取り、threads_post_insights に1投稿1日1行で書く（日ごとの推移が残る）
  3. アカウント全体の数値（前日の views・今の followers_count）を /me/threads_insights から取り、
     threads_account_insights に1日1行で書く

  --force    : 時刻・その日に取り終えたかに関係なく取る（手動の確かめ用）
  --backfill : /me/threads・/me/replies を最後まで読み、過去の投稿もすべて threads_posts に記録する
               （threads_posts が空の最初の回は、付けなくても自動でこうする）

失敗したら次の回にやり直す（MAX_ATTEMPTS 回まで）。ログは誰でも見られるので、件数・合否だけを出す。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import threads_morning as tm
import threads_poster as tp

JST = ZoneInfo("Asia/Tokyo")
POSTS = "threads_posts"
POST_INSIGHTS = "threads_post_insights"
ACCOUNT = "threads_account_insights"
# 投稿からこの日数以内の投稿の数値を取る
POST_DAYS = 14
# この時刻（JST）以降の最初の回に取る。朝の投稿・結果の返信・夜のまとめが出たあと
COLLECT_AFTER = (21, 30)
MAX_ATTEMPTS = 3
# 取っている途中の回（running）がこの分数より新しければ、別の回が動いているとみなして待つ
RUNNING_STALE_MINUTES = 20
POST_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
POST_FIELDS = "id,text,timestamp,media_type,permalink,is_reply"
PAGE_LIMIT = 100
API_INTERVAL_SEC = 0.3
TIMEOUT = 30

# 本文の型から種類を見分ける（記録の表に無い古い投稿・記録に失敗した投稿のため）
_RACE_TITLE = re.compile(r"^(?:🚤|🔒|💥)\s*【(.+?)\s*(\d{1,2})R｜(展示後予想|万舟チャンス)】")


def log(*a):
    print("[THREADS_INSIGHTS]", *a, flush=True)


def _sb(*a, **kw):
    return tm._sb(*a, **kw)


def _json(r):
    return tm._json(r)


# ---------------------------------------------------------------
# Threads API
# ---------------------------------------------------------------
def _api(path, token, params=None):
    r = requests.get(f"{tp.API_BASE}/{path}", params={**(params or {}), "access_token": token}, timeout=TIMEOUT)
    if r.status_code >= 400:
        # 本文にトークンは含まれないが、念のため長さを切る
        raise RuntimeError(f"Threads API {path.split('?')[0]}: HTTP {r.status_code} {r.text[:200]}")
    return r.json() or {}


def parse_ts(value):
    """Threads の timestamp（例: 2026-10-29T00:00:12+0000）を datetime に。読めなければ None。"""
    s = str(value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z",):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def list_own_posts(token, since=None, edge="threads", sleep=time.sleep):
    """自分の投稿（edge = threads / replies）を新しい順に読む。since より古い投稿が出たら止める（None なら最後まで）。"""
    out = []
    params = {"fields": POST_FIELDS, "limit": PAGE_LIMIT}
    path = f"me/{edge}"
    while True:
        data = _api(path, token, params)
        page = data.get("data") or []
        stop = False
        for p in page:
            ts = parse_ts(p.get("timestamp"))
            if since is not None and ts is not None and ts < since:
                stop = True
                continue
            out.append(p)
        after = ((data.get("paging") or {}).get("cursors") or {}).get("after")
        if stop or not page or not after or not (data.get("paging") or {}).get("next"):
            return out
        params = {**params, "after": after}
        sleep(API_INTERVAL_SEC)


def metric_value(item):
    """insights の1項目（values か total_value）を数にする。values が複数なら足す。"""
    total = (item.get("total_value") or {}).get("value")
    if total is not None:
        return int(total)
    values = item.get("values") or []
    nums = [v.get("value") for v in values if isinstance(v.get("value"), (int, float))]
    return int(sum(nums)) if nums else None


def parse_metrics(data):
    return {item.get("name"): metric_value(item) for item in (data.get("data") or []) if item.get("name")}


def fetch_post_metrics(post_id, token):
    """1投稿の数値。shares が使えない投稿ではそれを外して取り直す。"""
    try:
        got = parse_metrics(_api(f"{post_id}/insights", token, {"metric": ",".join(POST_METRICS)}))
    except RuntimeError:
        metrics = [m for m in POST_METRICS if m != "shares"]
        got = parse_metrics(_api(f"{post_id}/insights", token, {"metric": ",".join(metrics)}))
    return {m: got.get(m) for m in POST_METRICS}


def day_range_jst(day):
    """JST のその日の 0:00〜翌日 0:00（unix 秒）。"""
    start = datetime(day.year, day.month, day.day, tzinfo=JST)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def fetch_account_metrics(token, views_day):
    since, until = day_range_jst(views_day)
    views = parse_metrics(_api("me/threads_insights", token,
                               {"metric": "views", "since": since, "until": until})).get("views")
    followers = parse_metrics(_api("me/threads_insights", token, {"metric": "followers_count"})).get("followers_count")
    return {"views": views, "followers_count": followers}


# ---------------------------------------------------------------
# 種類とレースの紐付け（Supabase を使わない部分。tests で確かめる）
# ---------------------------------------------------------------
def _venue_codes():
    try:
        from official_fetcher import VENUES
    except Exception:  # noqa: BLE001
        return {}
    return {name: code for code, name in VENUES.items()}


def _race_fields(race_key, venue=None, race_no=None):
    k = str(race_key or "")
    out = {"race_key": k or None, "venue": venue, "race_no": race_no}
    if len(k) >= 8 and k[:8].isdigit():
        out["race_date"] = f"{k[:4]}-{k[4:6]}-{k[6:8]}"
    return {key: v for key, v in out.items() if v is not None}


def known_posts(auto_rows, result_rows, race_rows, recorded_rows):
    """投稿ID → 種類・レースの情報（記録の表から）。"""
    known = {}
    for row in recorded_rows or []:
        if row.get("post_id") and row.get("source") == "app":
            known[str(row["post_id"])] = {k: row.get(k) for k in
                                          ("kind", "source", "race_key", "race_date", "venue", "race_no", "style")
                                          if row.get(k) is not None}
    for row in race_rows or []:
        if row.get("post_id"):
            known[str(row["post_id"])] = {"kind": "race", "source": "race_posts", "style": row.get("style"),
                                          **_race_fields(row.get("race_key"))}
    for row in result_rows or []:
        if not row.get("post_id"):
            continue
        if row.get("item") == "summary":
            known[str(row["post_id"])] = {"kind": "result_summary", "source": "result_posts",
                                          "race_date": row.get("race_date")}
        else:
            known[str(row["post_id"])] = {"kind": "result", "source": "result_posts",
                                          "post_index": row.get("post_index"),
                                          **_race_fields(row.get("race_key"), row.get("venue"), row.get("race_no"))}
    for row in auto_rows or []:
        for i, p in enumerate(row.get("posts") or [], 1):
            if p.get("post_id"):
                known[str(p["post_id"])] = {"kind": "morning", "source": "auto_posts", "post_index": i,
                                            **_race_fields(p.get("race_key"), p.get("venue"), p.get("race_no"))}
    return {pid: {k: v for k, v in info.items() if v is not None} for pid, info in known.items()}


def guess_from_text(text, posted_at, venue_codes=None):
    """記録の表に無い投稿の種類を本文の型から。分からなければ None。"""
    head = str(text or "").lstrip()
    if head.startswith("🌅【展示前予想】"):
        return {"kind": "morning", "source": "text"}
    if head.startswith("🏁【結果】"):
        return {"kind": "result", "source": "text"}
    if head.startswith("📋 ") and "結果まとめ" in head.split("\n", 1)[0]:
        return {"kind": "result_summary", "source": "text"}
    if head.startswith("📊 ") and "本日の予想まとめ" in head.split("\n", 1)[0]:
        return {"kind": "summary", "source": "text"}
    m = _RACE_TITLE.match(head)
    if m:
        venue, rno = m.group(1).strip(), int(m.group(2))
        info = {"kind": "race", "source": "text", "venue": venue, "race_no": rno,
                "style": "aim" if m.group(3) == "万舟チャンス" else "normal"}
        code = (venue_codes if venue_codes is not None else _venue_codes()).get(venue)
        if code and posted_at is not None:
            info.update(_race_fields(f"{posted_at.astimezone(JST):%Y%m%d}_{code}_{rno}", venue, rno))
        return info
    return None


def build_post_row(api_post, known, venue_codes=None, now=None):
    """/me/threads・/me/replies の1件 → threads_posts の1行。"""
    pid = str(api_post.get("id"))
    text = str(api_post.get("text") or "")
    posted_at = parse_ts(api_post.get("timestamp"))
    is_reply = bool(api_post.get("is_reply"))
    info = known.get(pid) or guess_from_text(text, posted_at, venue_codes) \
        or {"kind": "manual_reply" if is_reply else "manual", "source": "none"}
    row = {
        "post_id": pid,
        "posted_at": posted_at.isoformat() if posted_at else None,
        "posted_date": posted_at.astimezone(JST).date().isoformat() if posted_at else None,
        "kind": None, "source": None, "post_index": None, "race_key": None, "race_date": None,
        "venue": None, "race_no": None, "style": None,
        "text_head": text[:tp.TEXT_HEAD_LEN],
        "text_length": len(text),
        "media_type": api_post.get("media_type"),
        "is_reply": is_reply,
        "permalink": api_post.get("permalink"),
        "updated_at": (now or datetime.now(timezone.utc)).isoformat(),
    }
    row.update(info)
    return row


def recent_post_ids(rows, now, days=POST_DAYS):
    """投稿から days 日以内の投稿ID（数値を取る対象）。"""
    since = now - timedelta(days=days)
    out = []
    for row in rows:
        ts = parse_ts(row.get("posted_at"))
        if ts is not None and ts >= since:
            out.append((row["post_id"], ts))
    return out


def should_collect(now, account_row, force=False):
    """(取るか, 理由)。"""
    if force:
        return True, "force"
    if (now.hour, now.minute) < COLLECT_AFTER:
        return False, "まだ取る時刻ではない"
    if not account_row:
        return True, "その日の最初の回"
    if account_row.get("status") == "done":
        return False, "その日の分は取得済み"
    if int(account_row.get("attempts") or 0) >= MAX_ATTEMPTS:
        return False, f"{MAX_ATTEMPTS}回失敗したので止めている"
    attempted = parse_ts(account_row.get("attempted_at"))
    if account_row.get("status") == "running" and attempted is not None \
            and now - attempted < timedelta(minutes=RUNNING_STALE_MINUTES):
        return False, "別の回が取得中"
    return True, "やり直し"


# ---------------------------------------------------------------
# 実行
# ---------------------------------------------------------------
def _in(values):
    return ",".join(str(v) for v in values)


def _load_known(post_ids, since_date):
    ids = sorted(set(post_ids))
    date_filter = f"race_date=gte.{since_date}" if since_date else "race_date=not.is.null"
    auto = _json(_sb("GET", f"threads_auto_posts?{date_filter}&select=race_date,posts"))
    results = _json(_sb("GET", f"threads_result_posts?{date_filter}&post_id=not.is.null"
                               "&select=race_date,item,post_index,race_key,venue,race_no,post_id"))
    races = _json(_sb("GET", f"threads_race_posts?{date_filter}&post_id=not.is.null"
                             "&select=race_key,post_id,style"))
    recorded = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        recorded += _json(_sb("GET", f"{POSTS}?post_id=in.({_in(chunk)})"
                                     "&select=post_id,kind,source,race_key,race_date,venue,race_no,style"))
    return known_posts(auto, results, races, recorded)


def _upsert(table, rows, conflict):
    for i in range(0, len(rows), 200):
        _sb("POST", f"{table}?on_conflict={conflict}", prefer="resolution=merge-duplicates,return=minimal",
            json=rows[i:i + 200])


def _account_row(day_iso):
    rows = _json(_sb("GET", f"{ACCOUNT}?snapshot_date=eq.{day_iso}&select=*"))
    return rows[0] if rows else None


def _save_account(day_iso, fields):
    _upsert(ACCOUNT, [{"snapshot_date": day_iso, **fields}], "snapshot_date")


def collect(cfg, now, backfill=False, sleep=time.sleep):
    """投稿の記録・投稿ごとの数値・アカウントの数値を取って書く。(数値を取った投稿の数, 失敗した数, アカウントの数値)。"""
    token = cfg["access_token"]
    since = None if backfill else now - timedelta(days=POST_DAYS + 1)
    api_posts = {}
    for edge in ("threads", "replies"):
        for p in list_own_posts(token, since=since, edge=edge, sleep=sleep):
            if p.get("id"):
                api_posts.setdefault(str(p["id"]), p)
    log(f"投稿 {len(api_posts)}件を読んだ")

    since_date = None if backfill else (now - timedelta(days=POST_DAYS + 2)).date().isoformat()
    known = _load_known(api_posts.keys(), since_date)
    venue_codes = _venue_codes()
    rows = [build_post_row(p, known, venue_codes, now) for p in api_posts.values()]
    _upsert(POSTS, rows, "post_id")
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    log("種類ごとの件数: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))

    day_iso = now.date().isoformat()
    insights, failed = [], 0
    for pid, ts in recent_post_ids(rows, now):
        try:
            metrics = fetch_post_metrics(pid, token)
        except (RuntimeError, requests.RequestException):
            failed += 1
            continue
        insights.append({"post_id": pid, "snapshot_date": day_iso, "fetched_at": now.isoformat(),
                         "age_hours": round((now - ts).total_seconds() / 3600, 1), **metrics})
        sleep(API_INTERVAL_SEC)
    _upsert(POST_INSIGHTS, insights, "post_id,snapshot_date")
    log(f"投稿ごとの数値: {len(insights)}件（失敗 {failed}件）")

    account = fetch_account_metrics(token, (now - timedelta(days=1)).date())
    return len(insights), failed, account


def run(force=False, backfill=False, now=None):
    now = (now or datetime.now(JST)).astimezone(JST)
    day_iso = now.date().isoformat()
    row = _account_row(day_iso)
    ok, why = should_collect(now, row, force=force or backfill)
    if not ok:
        log(f"取らない: {why}")
        return 0
    attempts = int((row or {}).get("attempts") or 0) + 1
    _save_account(day_iso, {"status": "running", "attempts": attempts, "attempted_at": now.isoformat(),
                            "error": None})

    cfg = tm._load_threads_config()
    if not cfg:
        _save_account(day_iso, {"status": "failed", "error": "スレッズ連携が未設定"})
        log("スレッズ連携が未設定")
        return 1
    if not backfill and not _json(_sb("GET", f"{POSTS}?select=post_id&limit=1")):
        # まだ1件も記録が無い（最初の回）なら、過去の投稿もすべて記録する
        backfill = True
        log("最初の回なので過去の投稿もすべて記録する")
    try:
        count, failed, account = collect(cfg, now, backfill=backfill)
    except Exception as e:  # noqa: BLE001
        # API のエラー本文に権限不足（threads_manage_insights）などの理由が入る。トークンは含まれない
        _save_account(day_iso, {"status": "failed", "error": str(e)[:500]})
        log(f"失敗（{attempts}/{MAX_ATTEMPTS}回目）: {type(e).__name__}: {str(e)[:300]}")
        return 1
    _save_account(day_iso, {
        "status": "done", "fetched_at": now.isoformat(), "posts_collected": count,
        "followers_count": account.get("followers_count"), "views": account.get("views"),
        "views_date": (now - timedelta(days=1)).date().isoformat(),
        "error": f"投稿の数値の失敗 {failed}件" if failed else None,
    })
    log("アカウントの数値を記録した")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["run"])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    a = parser.parse_args()
    sys.exit(run(force=a.force, backfill=a.backfill))


if __name__ == "__main__":
    main()
