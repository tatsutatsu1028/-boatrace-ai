"""
スレッズの朝の自動投稿（無料・毎朝3レース・買い目はモデルのみ）。

毎朝 09:00 JST（pg_cron → threads_morning.yml）に、その日の「🌅 展示前厳選10」
（Supabase の pre_exhibition_picks）から、投稿時刻から30分以上先に締切があるレースを
的中確率が高い順に3つ選び、スレッド形式（1投稿目に1レース目、返信で2・3レース目）で投稿する。
締切が過ぎた・間近のレースは飛ばし、次に的中確率が高いレースを繰り上げる。

  python threads_morning.py run [--date YYYYMMDD] [--retry] [--dry-run]

安全策:
  - アプリの設定（app_settings.threads_auto_post_enabled）が OFF なら投稿しない
  - 厳選が3レースそろわない日、厳選のデータに異常がある日は投稿しない（理由を記録する）
  - 投稿の成否・投稿したレース・買い目を threads_auto_posts に1日1行で記録する（アプリに表示）
  - note の記事にもなっているレースは note_overlap に記録する（アプリで警告する）
  - 同じ日に2回は投稿しない（--retry は、1投稿もできなかった日のやり直しだけ）

文章は threads_poster.build_morning_pick_text の決まった型。後で Claude の文章にするときは
build_texts に writer を渡す（失敗・空・長すぎるときはその投稿だけ決まった型に戻す）。

ログは誰でも見られるので、件数・合否だけを出す（レース・買い目・本文は出さない）。
"""

from __future__ import annotations

import argparse
import math
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import threads_poster as tp

JST = ZoneInfo("Asia/Tokyo")
POST_COUNT = 3
# 投稿時刻から締切までこれより短いレースは選ばない
MIN_LEAD_MINUTES = 30
# 厳選（pre_picks.yml 08:05〜）が終わっていなければ、この時間まで待つ
WAIT_PICKS_MINUTES = 40
# 投稿と投稿の間（返信の前に親の投稿が公開されるのを待つ）
POST_INTERVAL_SEC = 5
# 長期トークンはこの日数を過ぎたら延長する（期限60日）
TOKEN_REFRESH_DAYS = 30
TICKET_SOURCE = "model"
_COMBO = re.compile(r"^([1-6])-([1-6])-([1-6])$")


def log(*a):
    print("[THREADS_MORNING]", *a, flush=True)


# ---------------------------------------------------------------
# Supabase（service_role）
# ---------------------------------------------------------------
def _cfg():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY が未設定です。")
    return url, key


def _sb(method, path, prefer=None, attempts=3, **kwargs):
    url, key = _cfg()
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    last = None
    for i in range(attempts):
        try:
            r = requests.request(method, f"{url}/rest/v1/{path}", headers=headers, timeout=30, **kwargs)
            if r.status_code < 500 and r.status_code != 401:
                r.raise_for_status()
                return r
            last = requests.HTTPError(f"HTTP {r.status_code}", response=r)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
        time.sleep(2 ** i)
    raise last


def _json(r):
    try:
        return r.json() or []
    except ValueError:
        return []


# ---------------------------------------------------------------
# 選び方・確かめ方（Supabase を使わない部分。tests で確かめる）
# ---------------------------------------------------------------
def deadline_at(race_date_iso, hhmm):
    """締切の日時（JST）。読めなければ None。"""
    try:
        h, m = (int(x) for x in str(hhmm).strip().split(":"))
        d = datetime.strptime(str(race_date_iso)[:10], "%Y-%m-%d")
        return datetime(d.year, d.month, d.day, h, m, tzinfo=JST)
    except Exception:  # noqa: BLE001
        return None


def posted_tickets(pick):
    """投稿する買い目: 厳選の画面と同じく、資金配分で0円になった候補は載せない。"""
    return [{"combo": str(t.get("combo")), "group": str(t.get("group") or "抑え")}
            for t in (pick.get("tickets") or []) if int(t.get("stake") or 0) > 0]


def validate_picks(picks, run, race_date_iso):
    """その日の厳選のデータの異常（投稿をやめる理由）のリスト。空なら異常なし。"""
    problems = []
    if not run:
        problems.append("その日の厳選の記録（pre_exhibition_runs）が無い")
    elif run.get("picked_count") is not None and int(run["picked_count"]) != len(picks):
        problems.append(f"厳選の件数が記録と合わない（{len(picks)} / {run.get('picked_count')}）")
    keys = [p.get("race_key") for p in picks]
    if len(set(keys)) != len(keys):
        problems.append("同じレースが2回入っている")
    ranks = [p.get("pick_rank") for p in picks]
    if len(set(ranks)) != len(ranks):
        problems.append("順位が重なっている")
    for p in picks:
        label = str(p.get("race_key"))
        bad = []
        if str(p.get("race_date"))[:10] != race_date_iso:
            bad.append("日付")
        if not str(p.get("venue") or "").strip():
            bad.append("会場")
        try:
            if not 1 <= int(p.get("race_no")) <= 12:
                bad.append("R")
        except (TypeError, ValueError):
            bad.append("R")
        if deadline_at(race_date_iso, p.get("deadline")) is None:
            bad.append("締切")
        if not str(p.get("model_version") or "").strip():
            bad.append("モデルの版")
        hp = _num(p.get("hit_probability"))
        if hp is None or not 0 < hp <= 1:
            bad.append("的中確率")
        pf = [_num(v) for v in (p.get("p_first") or [])]
        if len(pf) != 6 or any(v is None or not 0 <= v <= 1 for v in pf) or abs(sum(pf) - 1) > 0.02:
            bad.append("1着確率")
        else:
            try:
                fav = int(p.get("favorite_lane"))
            except (TypeError, ValueError):
                fav = None
            if fav is None or not 1 <= fav <= 6 or pf[fav - 1] < max(pf) - 1e-6:
                bad.append("本命")
        tickets = p.get("tickets") or []
        combos = [str(t.get("combo")) for t in tickets]
        if not tickets or len(set(combos)) != len(combos):
            bad.append("買い目")
        else:
            for c in combos:
                m = _COMBO.match(c)
                if not m or len(set(m.groups())) != 3:
                    bad.append("買い目")
                    break
        if not posted_tickets(p):
            bad.append("載せる買い目が無い")
        if bad:
            problems.append(f"{label}: {'・'.join(dict.fromkeys(bad))}")
    return problems


def select_for_post(picks, now, n=POST_COUNT, lead=MIN_LEAD_MINUTES):
    """締切が投稿時刻から lead 分以上先のレースを、的中確率が高い順に n 個（同じ確率なら締切が早い順）。"""
    ok = []
    for p in picks:
        dl = deadline_at(p.get("race_date"), p.get("deadline"))
        if dl is not None and dl - now >= timedelta(minutes=lead):
            ok.append((-float(p["hit_probability"]), dl, p))
    ok.sort(key=lambda x: (x[0], x[1]))
    return [p for _, _, p in ok[:n]]


def build_texts(race_date_iso, chosen, writer=None):
    """投稿する本文のリスト。writer(race_date, pick, index, total) → 本文 があれば先に使う。"""
    texts = []
    total = len(chosen)
    for i, p in enumerate(chosen, start=1):
        pick = {**p, "tickets": posted_tickets(p)}
        text = None
        if writer is not None:
            try:
                text = str(writer(race_date_iso, pick, i, total) or "").strip()
            except Exception as e:  # noqa: BLE001
                log(f"文章の作成に失敗したため決まった型にする: {type(e).__name__}")
                text = None
            if not text or len(text) > tp.TEXT_LIMIT:
                text = None
        texts.append(text or tp.build_morning_pick_text(race_date_iso, pick, i, total))
    return texts


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------
# 記録
# ---------------------------------------------------------------
def _save(race_date_iso, **fields):
    row = {"race_date": race_date_iso, "ticket_source": TICKET_SOURCE,
           "attempted_at": datetime.now(JST).isoformat(timespec="seconds"), **fields}
    _sb("POST", "threads_auto_posts?on_conflict=race_date", prefer="resolution=merge-duplicates,return=minimal",
        json=row)


def _skip(race_date_iso, reason, attempts):
    log(f"投稿しない: {reason.split('（')[0]}")
    _save(race_date_iso, status="skipped", reason=reason, attempts=attempts)


def _post_record(pick, text):
    return {
        "pick_rank": pick.get("pick_rank"), "race_key": pick.get("race_key"), "venue": pick.get("venue"),
        "race_no": pick.get("race_no"), "deadline": pick.get("deadline"),
        "favorite_lane": pick.get("favorite_lane"), "favorite_name": pick.get("favorite_name"),
        "hit_probability": pick.get("hit_probability"), "tickets": posted_tickets(pick),
        "text": text, "post_id": None, "error": None,
    }


def _wait_for_picks(race_date_iso, max_minutes=WAIT_PICKS_MINUTES):
    until = time.time() + max_minutes * 60
    while True:
        run = _json(_sb("GET", f"pre_exhibition_runs?race_date=eq.{race_date_iso}&select=*"))
        if run:
            return run[0]
        if time.time() > until:
            return None
        log("厳選の終了を待っています")
        time.sleep(60)


def _load_threads_config():
    url, key = _cfg()
    cfg = tp.load_config(url, key)
    if not cfg:
        return None
    age = tp.token_age_days(cfg)
    if age is not None and age >= TOKEN_REFRESH_DAYS:
        try:
            tp.refresh_access_token(url, key)
            cfg = tp.load_config(url, key) or cfg
            log("トークンを延長しました")
        except Exception as e:  # noqa: BLE001  延長に失敗しても、期限内なら投稿は続ける
            log(f"トークンの延長に失敗: {type(e).__name__}")
    return cfg


def _post_with_retry(cfg, text, reply_to=None, attempts=2):
    last = None
    for i in range(attempts):
        try:
            return tp.post_text(cfg["user_id"], cfg["access_token"], text, reply_to_id=reply_to)
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < attempts:
                time.sleep(10)
    raise last


def run(date=None, retry=False, dry_run=False, writer=None):
    now = datetime.now(JST)
    date = date or now.strftime("%Y%m%d")
    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"

    have = _json(_sb("GET", f"threads_auto_posts?race_date=eq.{iso}&select=status,posts,attempts"))
    attempts = 1
    if have and not dry_run:
        prev = have[0]
        any_posted = any((p or {}).get("post_id") for p in (prev.get("posts") or []))
        if not retry or any_posted or prev.get("status") in ("posted", "partial"):
            log(f"{date}: 記録済み（{prev.get('status')}）。同じ日には2回投稿しない")
            return
        attempts = int(prev.get("attempts") or 1) + 1

    def skip(reason):
        if dry_run:
            log(f"試し: 投稿しない（{reason}）")
        else:
            _skip(iso, reason, attempts)

    settings = _json(_sb("GET", "app_settings?id=eq.1&select=threads_auto_post_enabled"))
    if not dry_run and (not settings or not settings[0].get("threads_auto_post_enabled")):
        skip("自動投稿がOFF")
        return

    run_row = _wait_for_picks(iso)
    picks = _json(_sb("GET", f"pre_exhibition_picks?race_date=eq.{iso}&select=*&order=pick_rank.asc"))
    problems = validate_picks(picks, run_row, iso)
    if problems:
        skip("厳選のデータに異常（" + " / ".join(problems)[:500] + "）")
        return
    # 待っている間に時間がたつので、締切は今の時刻で比べる
    now = datetime.now(JST)
    chosen = select_for_post(picks, now)
    if len(chosen) < POST_COUNT:
        skip(f"締切まで{MIN_LEAD_MINUTES}分以上ある厳選が{POST_COUNT}レースそろわない（{len(chosen)}レース）")
        return

    keys = [p["race_key"] for p in chosen]
    notes = _json(_sb("GET", f"note_publications?race_key=in.({','.join(keys)})&select=race_key"))
    overlap = [r["race_key"] for r in notes]
    texts = build_texts(iso, chosen, writer=writer)
    posts = [_post_record(p, t) for p, t in zip(chosen, texts)]
    if dry_run:
        for t in texts:
            print("-" * 40)
            print(t)
        log(f"{date}: 試し（投稿しない）{len(texts)}件")
        return

    cfg = _load_threads_config()
    if not cfg:
        _save(iso, status="failed", reason="スレッズ連携が未設定", posts=posts, race_keys=keys,
              note_overlap=overlap, attempts=attempts)
        log("失敗: スレッズ連携が未設定")
        return

    # 途中で止まっても2回投稿しないよう、先に記録してから投稿する
    _save(iso, status="failed", reason="投稿を始めましたが終わっていません", posts=posts, race_keys=keys,
          note_overlap=overlap, attempts=attempts)
    parent = None
    for i, rec in enumerate(posts):
        if i:
            time.sleep(POST_INTERVAL_SEC)
        try:
            rec["post_id"] = _post_with_retry(cfg, rec["text"], reply_to=parent)
            parent = rec["post_id"]
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
            status = "failed" if i == 0 else "partial"
            _save(iso, status=status, reason=f"{i + 1}投稿目で失敗", posts=posts, race_keys=keys,
                  note_overlap=overlap, attempts=attempts,
                  posted_at=datetime.now(JST).isoformat(timespec="seconds") if i else None)
            log(f"{date}: {i + 1}投稿目で失敗（{type(e).__name__}）")
            return
        _save(iso, status="partial" if i + 1 < len(posts) else "posted",
              reason=None if i + 1 == len(posts) else "投稿の途中", posts=posts, race_keys=keys,
              note_overlap=overlap, attempts=attempts, posted_at=datetime.now(JST).isoformat(timespec="seconds"))
    log(f"{date}: {len(posts)}件を投稿" + (f"（noteと重複 {len(overlap)}件）" if overlap else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["run"])
    ap.add_argument("--date", default="")
    ap.add_argument("--retry", action="store_true", help="1投稿もできなかった日をやり直す")
    ap.add_argument("--dry-run", action="store_true", help="本文を作るだけで投稿も記録もしない")
    a = ap.parse_args()
    run(a.date or None, retry=a.retry, dry_run=a.dry_run)


if __name__ == "__main__":
    main()
