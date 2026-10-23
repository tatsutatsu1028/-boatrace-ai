"""
スレッズの朝の自動投稿（threads_morning.py）で出した3レースの結果を、自動で投稿する。

オッズ追跡（track_odds.yml・5分おき）の結果確定のあとに動く（同じジョブの次のステップ）。
  - 各レースの結果が分かったら、そのレースを載せた朝の投稿への返信として結果を投稿する
  - 3レースとも結果が分かったら、1投稿目への返信で「本日の厳選3レースの結果まとめ」を投稿する

  python threads_results.py run [--date YYYYMMDD] [--dry-run]

結果は「🌅 展示前厳選10」（pre_exhibition_picks）の結果の列（list_results.py がその日のうちに書く）を読む。
締切から CHECK_AFTER_MINUTES 分たっても結果が無いレースだけ、公式の結果ページを読んで確かめる
（CHECK_EVERY_MINUTES 分おき。結果が出ていれば一覧にも書き込む。中止・返還もここで分かる）。

安全策:
  - アプリの設定の朝の自動投稿（threads_auto_post_enabled）と結果の自動投稿（threads_result_post_enabled）の
    両方が ON の日だけ投稿する
  - 朝の投稿が3投稿とも出た日（threads_auto_posts.status = posted）だけ投稿する
  - 同じレース・まとめは1回だけ（threads_result_posts に1行ずつ記録し、行を取ってから投稿する）。
    失敗したら次の回にやり直し、MAX_ATTEMPTS 回失敗したら止めて理由を記録する。
    投稿の途中で止まった行（posting のまま）は、二重投稿を防ぐため出し直さない

文章は決まった型（build_race_text / build_summary_text）。後で Claude の文章にするときは
writer(race_date, kind, data) → 本文 を渡す（kind は "race" / "summary"。失敗・空・長すぎるときは決まった型）。
購入金額・収支は載せない（朝の投稿と同じ）。

ログは誰でも見られるので、件数・合否だけを出す（レース・買い目・本文は出さない）。
"""

from __future__ import annotations

import argparse
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import threads_morning as tm
import threads_poster as tp

JST = ZoneInfo("Asia/Tokyo")
TABLE = "threads_result_posts"
SUMMARY = "summary"
# 同じ投稿の失敗がこの回数になったら止める
MAX_ATTEMPTS = 3
# 締切からこの分数たっても一覧に結果が無ければ、公式の結果ページを自分で読む
CHECK_AFTER_MINUTES = 40
# 公式の結果ページを読み直す間隔
CHECK_EVERY_MINUTES = 15
# 締切からこの分数たっても結果が分からなければ、そのレースはあきらめる（まとめも出さない）
GIVE_UP_MINUTES = 240
# 投稿の途中で止まった（posting のまま）と見なすまでの分数
STALE_POSTING_MINUTES = 10
# 投稿と投稿の間
POST_INTERVAL_SEC = 5
# オッズ追跡（track_odds.yml）が動く分（JST 8:02〜22:57 の5分おき）。試しで投稿する時刻の見込みに使う
TICK_MINUTES = 5
TICK_OFFSET = 2
# 一覧に結果が書かれる時刻が分からないとき（翌朝の記録など）の、締切からの見込み
ESTIMATE_RESULT_MINUTES = 10

OUTCOME_LABELS = {"hit": "的中", "miss": "不的中", "cancelled": "中止", "refunded": "返還"}
VOID_OUTCOMES = ("cancelled", "refunded")


def log(*a):
    print("[THREADS_RESULTS]", *a, flush=True)


def _sb(*a, **kw):
    return tm._sb(*a, **kw)


def _json(r):
    return tm._json(r)


def _now_iso():
    return datetime.now(JST).isoformat(timespec="seconds")


# ---------------------------------------------------------------
# 判定・文章（Supabase を使わない部分。tests で確かめる）
# ---------------------------------------------------------------
def _combos(text):
    return tp._actual_combos(text)


def judge(post, result):
    """朝の投稿1件（posts の要素）と結果 → 結果の記録。結果がまだなら None。

    result: {"status": "settled" / "cancelled" / "refunded", "trifecta_actual": "1-2-3"（同着は空白区切り）,
             "trifecta_payout": 1230}
    """
    if not result:
        return None
    base = {"race_key": post.get("race_key"), "venue": post.get("venue"), "race_no": post.get("race_no")}
    status = result.get("status")
    if status in VOID_OUTCOMES:
        return {**base, "outcome": status, "trifecta_actual": None, "trifecta_payout": None, "hit_combos": []}
    actual = _combos(result.get("trifecta_actual"))
    if status != "settled" or not actual:
        return None
    posted = [str(t.get("combo") if isinstance(t, dict) else t) for t in (post.get("tickets") or [])]
    hits = [c for c in posted if c in actual]
    payout = result.get("trifecta_payout")
    return {**base, "outcome": "hit" if hits else "miss", "trifecta_actual": " ".join(actual),
            "trifecta_payout": int(payout) if payout is not None else None, "hit_combos": hits}


def _yen(v):
    return f"{int(v):,}円" if v is not None else "—"


def _race_name(r):
    try:
        return f"{tp._clean(r.get('venue'))} {int(r.get('race_no'))}R"
    except (TypeError, ValueError):
        return f"{tp._clean(r.get('venue'))} {tp._clean(r.get('race_no'))}"


def _result_line(r):
    if r["outcome"] in VOID_OUTCOMES:
        return f"結果 {OUTCOME_LABELS[r['outcome']]}"
    actual = r["trifecta_actual"].split()
    if len(actual) > 1:
        return f"結果 3連単 {' / '.join(actual)}（同着）\n配当 {_yen(r.get('trifecta_payout'))}"
    return f"結果 3連単 {actual[0]}\n配当 {_yen(r.get('trifecta_payout'))}"


def build_race_text(race_date_iso, r, index, total):
    """1レースの結果の本文（決まった型）。外れも必ず出す。購入金額・収支は載せない。"""
    parts = [f"🏁【結果】{tp._short_date(race_date_iso)} 朝の無料{total}レース（{index}/{total}）",
             _race_name(r), _result_line(r)]
    if r["outcome"] == "hit":
        parts.append("⭕ 的中\n" + "\n".join(r["hit_combos"]))
    elif r["outcome"] == "miss":
        parts.append("不的中")
    return "\n\n".join(parts)[:tp.TEXT_LIMIT]


def summary_counts(results):
    """(的中, 結果の出たレース, 中止・返還)。"""
    hits = sum(1 for r in results if r["outcome"] == "hit")
    voids = sum(1 for r in results if r["outcome"] in VOID_OUTCOMES)
    return hits, len(results) - voids, voids


def build_summary_text(race_date_iso, results):
    """まとめの本文（決まった型）。results は朝の投稿の順。"""
    hits, settled, voids = summary_counts(results)
    head = f"{settled}レース中{hits}レース的中"
    if voids:
        labels = "・".join(dict.fromkeys(OUTCOME_LABELS[r["outcome"]] for r in results
                                         if r["outcome"] in VOID_OUTCOMES))
        head += f"（{labels} {voids}レース）"
    lines = []
    for r in results:
        if r["outcome"] in VOID_OUTCOMES:
            lines.append(f"{_race_name(r)}　{OUTCOME_LABELS[r['outcome']]}")
        else:
            mark = "⭕ 的中" if r["outcome"] == "hit" else "不的中"
            lines.append(f"{_race_name(r)}　{' / '.join(r['trifecta_actual'].split())}"
                         f"（{_yen(r.get('trifecta_payout'))}）　{mark}")
    parts = [f"📋 {tp._short_date(race_date_iso)} 本日の厳選{len(results)}レースの結果まとめ", head,
             "\n".join(lines), "ご覧いただきありがとうございました。明日の朝も無料で予想を投稿します。"]
    return "\n\n".join(parts)[:tp.TEXT_LIMIT]


def make_text(race_date_iso, kind, data, writer=None):
    """writer(race_date, kind, data) があれば先に使い、だめなら決まった型。

    kind = "race": data = {"result": 結果の記録, "index": 何レース目, "total": レース数}
    kind = "summary": data = {"results": [結果の記録（朝の投稿の順）]}
    """
    if writer is not None:
        try:
            text = str(writer(race_date_iso, kind, data) or "").strip()
            if text and len(text) <= tp.TEXT_LIMIT:
                return text
        except Exception as e:  # noqa: BLE001
            log(f"文章の作成に失敗したため決まった型にする: {type(e).__name__}")
    if kind == "race":
        return build_race_text(race_date_iso, data["result"], data["index"], data["total"])
    return build_summary_text(race_date_iso, data["results"])


def morning_posts(row):
    """結果を出せる朝の記録なら、その投稿のリスト（3投稿とも出た日だけ）。出せなければ None。"""
    if not row or row.get("status") != "posted":
        return None
    posts = [p or {} for p in (row.get("posts") or [])]
    if not posts or not all(p.get("post_id") and p.get("race_key") for p in posts):
        return None
    return posts


def can_try(rec, now, max_attempts=MAX_ATTEMPTS):
    """記録の行（無ければ None）から、今回投稿してよいか。(よいか, 止める理由)。"""
    if rec is None or rec.get("status") == "waiting":
        return True, None
    status = rec.get("status")
    if status == "failed":
        if int(rec.get("attempts") or 0) >= max_attempts:
            return False, f"{max_attempts}回失敗したため止めた"
        return True, None
    if status == "posting":
        at = _parse_ts(rec.get("attempted_at"))
        if at is None or now - at >= timedelta(minutes=STALE_POSTING_MINUTES):
            return False, "前の回が投稿の途中で止まったため、二重投稿を防ぐため出し直さない"
    return False, None


def is_final(rec, now):
    """そのレースの結果の投稿が終わった（出た・止めた）か。まとめはレースが全部終わってから出す。"""
    if rec is None or rec.get("status") in (None, "waiting"):
        return False
    if rec.get("status") == "posted":
        return True
    return not can_try(rec, now)[0] and rec.get("outcome") is not None


def from_record(rec):
    """記録の行に残した結果（公式の結果ページで分かった中止・返還も、読み直さずに使う）。"""
    if not rec or not rec.get("outcome"):
        return None
    return {"race_key": rec.get("race_key"), "venue": rec.get("venue"), "race_no": rec.get("race_no"),
            "outcome": rec["outcome"], "trifecta_actual": rec.get("trifecta_actual"),
            "trifecta_payout": rec.get("trifecta_payout"), "hit_combos": list(rec.get("hit_combos") or [])}


def _parse_ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def pick_result(row):
    """pre_exhibition_picks の行 → 結果（judge に渡す形）。まだなら None。

    翌朝の記録の void（払戻が無い）は中止か返還か分からないので「中止」として扱う。
    """
    if not row or not row.get("result_at"):
        return None
    if row.get("result_status") == "void":
        return {"status": "cancelled"}
    if row.get("result_status") == "settled":
        return {"status": "settled", "trifecta_actual": row.get("trifecta_actual"),
                "trifecta_payout": row.get("trifecta_payout")}
    return None


def page_void_status(html):
    """公式の結果ページの HTML から、中止・返還を読む。分からなければ None。"""
    from official_fetcher import _norm

    text = _norm(html)
    if "レース中止" in text:
        return "cancelled"
    if re.search(r"3連単[^0-9]{0,40}?(特払い|不成立|返還)", text):
        return "refunded"
    return None


def next_tick(t):
    """t のあとの最初のオッズ追跡の時刻（JST の :02, :07, ...）。"""
    t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while (t.minute - TICK_OFFSET) % TICK_MINUTES:
        t += timedelta(minutes=1)
    return t


# ---------------------------------------------------------------
# 結果の取得
# ---------------------------------------------------------------
def _load_picks(keys):
    if not keys:
        return {}
    rows = _json(_sb("GET", f"pre_exhibition_picks?race_key=in.({','.join(keys)})"
                            "&select=race_key,race_date,jcd,race_no,deadline,result_status,trifecta_actual,"
                            "trifecta_payout,result_at"))
    return {r["race_key"]: r for r in rows}


def _check_official(race_key):
    """公式の結果ページを読む。(結果 or None)。結果が出ていれば一覧にも書き込む。"""
    from official_fetcher import _get, fetch_race_result

    date, jcd, rno = race_key.split("_")
    try:
        official = fetch_race_result(date, jcd, int(rno))
    except ValueError:
        official = None
    if official is not None:
        import list_results

        list_results.on_official_result(race_key, official)
        return {"status": "settled", "trifecta_actual": official["trifecta"],
                "trifecta_payout": official["trifecta_payout_per_100"]}
    html, _ = _get("raceresult", date, jcd, int(rno))
    status = page_void_status(html)
    return {"status": status} if status else None


def _venue_cancelled(iso, jcd):
    try:
        rows = _json(_sb("GET", f"daily_schedule?race_date=eq.{iso.replace('-', '')}&jcd=eq.{jcd}&select=status"))
    except Exception:  # noqa: BLE001
        return False
    return any(str(r.get("status") or "").strip() == "中止" for r in rows)


# ---------------------------------------------------------------
# 記録（threads_result_posts）
# ---------------------------------------------------------------
def _load_records(iso):
    rows = _json(_sb("GET", f"{TABLE}?race_date=eq.{iso}&select=*"))
    return {r["item"]: r for r in rows}


def _patch(iso, item, fields, where=""):
    r = _sb("PATCH", f"{TABLE}?race_date=eq.{iso}&item=eq.{item}{where}", prefer="return=representation",
            json=fields)
    return _json(r)


def _claim(iso, item, rec, base):
    """投稿する権利を取る（status を posting にする）。取れたら attempts を返す。取れなければ None。"""
    fields = {**base, "status": "posting", "attempted_at": _now_iso(), "error": None, "reason": None}
    if rec is None:
        r = _sb("POST", f"{TABLE}?on_conflict=race_date,item",
                prefer="resolution=ignore-duplicates,return=representation",
                json={"race_date": iso, "item": item, "attempts": 1, **fields})
        return 1 if _json(r) else None
    n = int(rec.get("attempts") or 0)
    got = _patch(iso, item, {**fields, "attempts": n + 1},
                 f"&status=eq.{rec.get('status')}&attempts=eq.{n}")
    return n + 1 if got else None


def _finish(iso, item, attempts, post_id=None, error=None):
    if post_id:
        _patch(iso, item, {"status": "posted", "post_id": post_id, "posted_at": _now_iso(), "error": None})
        return
    stop = attempts >= MAX_ATTEMPTS
    _patch(iso, item, {"status": "failed", "error": error,
                       "reason": f"{MAX_ATTEMPTS}回失敗したため止めた" if stop else "失敗（次の回にやり直す）"})


def _note_stop(iso, item, rec, reason):
    if rec and rec.get("reason") != reason:
        _patch(iso, item, {"reason": reason})


def _touch_check(iso, item, rec, base):
    """公式の結果ページを読んだ時刻を残す（結果待ちの行）。"""
    fields = {**base, "status": "waiting", "checked_at": _now_iso()}
    if rec is None:
        _sb("POST", f"{TABLE}?on_conflict=race_date,item", prefer="resolution=ignore-duplicates,return=minimal",
            json={"race_date": iso, "item": item, "attempts": 0, **fields})
    elif rec.get("status") == "waiting":
        _patch(iso, item, {"checked_at": fields["checked_at"]}, "&status=eq.waiting")


# ---------------------------------------------------------------
# 実行
# ---------------------------------------------------------------
def _settings():
    rows = _json(_sb("GET", "app_settings?id=eq.1&select=threads_auto_post_enabled,threads_result_post_enabled"))
    s = rows[0] if rows else {}
    return bool(s.get("threads_auto_post_enabled")), bool(s.get("threads_result_post_enabled"))


def _resolve(iso, post, pick, rec, now):
    """そのレースの結果（judge の結果）。分からなければ None。必要なら公式の結果ページを読む。"""
    res = judge(post, pick_result(pick))
    if res is not None:
        return res
    dl = tm.deadline_at(iso, post.get("deadline") or (pick or {}).get("deadline"))
    if dl is None or now < dl + timedelta(minutes=CHECK_AFTER_MINUTES):
        return None
    checked = _parse_ts((rec or {}).get("checked_at"))
    if checked is not None and now - checked < timedelta(minutes=CHECK_EVERY_MINUTES):
        return None
    base = {"race_key": post["race_key"], "venue": post.get("venue"), "race_no": post.get("race_no")}
    try:
        res = judge(post, _check_official(post["race_key"]))
    except Exception as e:  # noqa: BLE001
        log(f"公式の結果ページを読めない: {type(e).__name__}")
        res = None
    if res is None and now >= dl + timedelta(minutes=CHECK_AFTER_MINUTES + CHECK_EVERY_MINUTES) \
            and _venue_cancelled(iso, post["race_key"].split("_")[1]):
        res = judge(post, {"status": "cancelled"})
    if res is None:
        _touch_check(iso, post["race_key"], rec, base)
    return res


def _post(cfg, text, reply_to):
    return tm._post_with_retry(cfg, text, reply_to=reply_to, waits=(10,))


def run(date=None, dry_run=False, writer=None):
    now = datetime.now(JST)
    date = date or now.strftime("%Y%m%d")
    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    if dry_run:
        return dry_run_day(iso, writer=writer)

    morning_on, result_on = _settings()
    if not (morning_on and result_on):
        log("設定がOFFのため投稿しない")
        return
    have = _json(_sb("GET", f"threads_auto_posts?race_date=eq.{iso}&select=status,posts"))
    posts = morning_posts(have[0] if have else None)
    if posts is None:
        return
    records = _load_records(iso)
    if records.get(SUMMARY, {}).get("status") == "posted":
        return
    picks = _load_picks([p["race_key"] for p in posts])

    cfg = None
    results = {}
    counts = {"posted": 0, "failed": 0, "waiting": 0}
    total = len(posts)
    for i, post in enumerate(posts, start=1):
        rk = post["race_key"]
        rec = records.get(rk)
        res = from_record(rec) or _resolve(iso, post, picks.get(rk), rec, now)
        if res is None:
            dl = tm.deadline_at(iso, post.get("deadline"))
            if dl is not None and now >= dl + timedelta(minutes=GIVE_UP_MINUTES) and \
                    (rec is None or rec.get("status") == "waiting"):
                _give_up(iso, rk, rec, post)
            counts["waiting"] += 1
            continue
        results[rk] = res
        if rec is not None and rec.get("status") == "posted":
            continue
        ok, why = can_try(rec, now)
        if not ok:
            if why:
                _note_stop(iso, rk, rec, why)
            continue
        if cfg is None:
            cfg = tm._load_threads_config()
            if not cfg:
                log("失敗: スレッズ連携が未設定")
                return
        text = make_text(iso, "race", {"result": res, "index": i, "total": total}, writer)
        base = {"post_index": i, "race_key": rk, "venue": post.get("venue"), "race_no": post.get("race_no"),
                "outcome": res["outcome"], "trifecta_actual": res["trifecta_actual"],
                "trifecta_payout": res["trifecta_payout"], "hit_combos": res["hit_combos"],
                "text": text, "reply_to": post["post_id"]}
        n = _claim(iso, rk, rec, base)
        if n is None:
            continue
        if counts["posted"] or counts["failed"]:
            time.sleep(POST_INTERVAL_SEC)
        try:
            _finish(iso, rk, n, post_id=_post(cfg, text, post["post_id"]))
            counts["posted"] += 1
        except Exception as e:  # noqa: BLE001
            _finish(iso, rk, n, error=f"{type(e).__name__}: {e}"[:300])
            counts["failed"] += 1

    if len(results) == total:
        records = _load_records(iso)
        if all(is_final(records.get(p["race_key"]), now) for p in posts):
            r = _post_summary(iso, posts, [results[p["race_key"]] for p in posts], records.get(SUMMARY), now,
                              cfg, writer, wait=bool(counts["posted"] or counts["failed"]))
            if r:
                counts[r] += 1
    log(f"{date}: 結果の投稿 成功 {counts['posted']}・失敗 {counts['failed']}・結果待ち {counts['waiting']}")


def _give_up(iso, rk, rec, post):
    reason = f"締切から{GIVE_UP_MINUTES // 60}時間たっても結果が分からないため出さない"
    if rec is not None and rec.get("reason") == reason:
        return
    fields = {"race_key": rk, "venue": post.get("venue"), "race_no": post.get("race_no"),
              "status": "failed", "attempts": MAX_ATTEMPTS, "reason": reason}
    if rec is None:
        _sb("POST", f"{TABLE}?on_conflict=race_date,item", prefer="resolution=ignore-duplicates,return=minimal",
            json={"race_date": iso, "item": rk, **fields})
    else:
        _patch(iso, rk, fields, "&status=eq.waiting")


def _post_summary(iso, posts, results, rec, now, cfg, writer, wait):
    ok, why = can_try(rec, now)
    if not ok:
        if why:
            _note_stop(iso, SUMMARY, rec, why)
        return None
    if cfg is None:
        cfg = tm._load_threads_config()
        if not cfg:
            log("失敗: スレッズ連携が未設定")
            return None
    text = make_text(iso, "summary", {"results": results}, writer)
    hits, settled, voids = summary_counts(results)
    base = {"race_key": None, "text": text, "reply_to": posts[0]["post_id"],
            "hits": hits, "settled": settled, "voids": voids}
    n = _claim(iso, SUMMARY, rec, base)
    if n is None:
        return None
    if wait:
        time.sleep(POST_INTERVAL_SEC)
    try:
        _finish(iso, SUMMARY, n, post_id=_post(cfg, text, posts[0]["post_id"]))
        return "posted"
    except Exception as e:  # noqa: BLE001
        _finish(iso, SUMMARY, n, error=f"{type(e).__name__}: {e}"[:300])
        return "failed"


# ---------------------------------------------------------------
# 試し（投稿も記録もしない）
# ---------------------------------------------------------------
def _result_time(iso, post, pick, outcome):
    """その日のうちに一覧に結果が書かれた時刻。分からなければ締切からの見込み。(時刻, 見込みか)。

    中止・返還は一覧に書かれないので、公式の結果ページを確かめる時刻（締切の CHECK_AFTER_MINUTES 分後）。
    """
    at = _parse_ts((pick or {}).get("result_at"))
    if at is not None and outcome not in VOID_OUTCOMES:
        at = at.astimezone(JST)
        if at.date().isoformat() == iso:
            return at, False
    dl = tm.deadline_at(iso, post.get("deadline") or (pick or {}).get("deadline"))
    wait = CHECK_AFTER_MINUTES if outcome in VOID_OUTCOMES else ESTIMATE_RESULT_MINUTES
    return (dl + timedelta(minutes=wait) if dl else None), True


def dry_run_day(iso, writer=None):
    """過去の1日で、どの時刻にどんな文章が出るかを出す（投稿も記録もしない）。

    朝の投稿が3投稿とも出た日はそのレース、出ていない日は朝9:00に選ばれたはずの3レースで試す。
    """
    have = _json(_sb("GET", f"threads_auto_posts?race_date=eq.{iso}&select=status,posts"))
    posts = morning_posts(have[0] if have else None)
    if posts is None:
        picks = _json(_sb("GET", f"pre_exhibition_picks?race_date=eq.{iso}&select=*&order=pick_rank.asc"))
        chosen = tm.select_for_post(picks, tm.deadline_at(iso, "09:00"))
        if len(chosen) < tm.POST_COUNT:
            log(f"試し: 朝の投稿も、9:00に選べる厳選{tm.POST_COUNT}レースも無い（{len(chosen)}レース）")
            return
        print("※ この日は朝の投稿が3投稿とも出ていないため、9:00に選ばれたはずの3レースで試します"
              "（本番ではこの日は結果を投稿しません）")
        posts = [{**tm._post_record(p, ""), "post_id": f"(朝の{i}投稿目)"} for i, p in enumerate(chosen, 1)]
    picks = _load_picks([p["race_key"] for p in posts])
    total = len(posts)
    timeline = []
    results = []
    for i, post in enumerate(posts, start=1):
        pick = picks.get(post["race_key"])
        res = judge(post, pick_result(pick))
        if res is None:
            print("-" * 40)
            print(f"{i}レース目: 結果がまだ無い（出せない）")
            continue
        at, est = _result_time(iso, post, pick, res["outcome"])
        results.append(res)
        timeline.append((next_tick(at) if at else None, est, f"{i}投稿目への返信",
                         make_text(iso, "race", {"result": res, "index": i, "total": total}, writer)))
    if len(results) == total:
        last = max((t for t, _, _, _ in timeline if t), default=None)
        timeline.append((last, any(e for _, e, _, _ in timeline), "1投稿目への返信（まとめ）",
                         make_text(iso, "summary", {"results": results}, writer)))
    timeline.sort(key=lambda x: (x[0] is None, x[0] or datetime.min.replace(tzinfo=JST)))
    for at, est, where, text in timeline:
        print("-" * 40)
        when = at.strftime("%H:%M") if at else "--:--"
        print(f"{when}{'（見込み）' if est else ''} {where}")
        print(text)
    log(f"{iso.replace('-', '')}: 試し（投稿しない）{len(timeline)}件")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["run"])
    ap.add_argument("--date", default="")
    ap.add_argument("--dry-run", action="store_true", help="過去の1日で、出る時刻と本文を出すだけ（投稿も記録もしない）")
    a = ap.parse_args()
    if a.dry_run:
        run(a.date or None, dry_run=True)
        return
    # 5分おきに動くので、Supabase の一時的な障害で失敗のメールが続かないよう、ここで止める
    try:
        run(a.date or None)
    except Exception as e:  # noqa: BLE001
        log(f"中断: {type(e).__name__}")


if __name__ == "__main__":
    main()
