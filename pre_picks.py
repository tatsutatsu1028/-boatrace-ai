"""
「🌅 展示前厳選10」: 毎朝、その日の全レースを展示前モデル（ml_pre.py）で予想し、
「この買い目の的中確率」が高い順に10レースを選んで Supabase に保存する。前の日までの
厳選レースの結果（的中・払戻）も記録する。

  python pre_picks.py run      [--date YYYYMMDD] [--force]   結果の記録 → その日の10レースの選定
  python pre_picks.py results                                 結果の記録だけ
  python pre_picks.py manshu   [--date YYYYMMDD]              万舟チャンスだけ選び直す（厳選10は変えない）
  python pre_picks.py aim      [--date YYYYMMDD]              買い目が空の万舟チャンスだけ、今のオッズで作り直す
  python pre_picks.py check    [--date YYYYMMDD]              万舟チャンスなのに買い目が空のレースがあれば失敗にする

起動は pg_cron → pre_picks.yml（毎朝 08:05 JST。朝の過去データ収集〜08:00 の後）。
朝の時点でオッズが取れず中穴〜万舟狙いの買い目を作れなかったレースは、08:30・09:00 に task=aim で取り直す
（作れなかった理由は manshu_chance_picks.aim_error に残し、一覧に出す）。
公式サイトへのアクセスは polite_http（開始間隔3.4秒以上）を通し、過去データの収集が
動いている間は終わるまで待ってから始める。取るのは番組表（Bファイル）1つと、各レースの
出走表ページ1つずつ（直前情報ページは取らない）。

選ぶレース・買い目は展示前の情報だけで作る（買い目の作り方・資金配分は自動固定と同じ）。
展示後にアプリか自動固定でそのレースを予想すると、Supabase のトリガーが展示後の予想
（今の新モデル）を同じ行に書き、本命が変わった・的中確率が10ポイント以上下がったレースに印を付ける
（supabase/migrations/20261016000000_create_pre_exhibition_picks.sql）。

同じ処理で「💥 万舟チャンス（荒れやすいレース）」も選ぶ（manshu.py）。その日の全レースの展示前版の
万舟確率を出し、高い順に10レースを manshu_chance_picks に保存する（本線の買い目は厳選10と同じ作り方、
穴寄りの買い目は1号艇を1着から外した上位10点）。結果（万舟だったか・本線と穴寄りの的中・払戻）も
翌朝に記録する。万舟チャンスだけ失敗しても厳選10は続ける（理由は pre_exhibition_runs.manshu_error）。

ログは誰でも見られるので、件数・時間・合否だけを出す（確率・買い目・成績は出さない）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

JST = ZoneInfo("Asia/Tokyo")
PICK_COUNT = 10
# 締切までこれより短いレースは選ばない（選んでも予想を見る時間が無い）
MIN_LEAD_MINUTES = 15
# 過去データの収集（公式サイトを読む）が動いていれば、この時間まで終わるのを待つ
WAIT_CRAWLER_MINUTES = 30
CRAWLER_WORKFLOWS = ("history_backfill.yml", "collect_history_workflow.yml")
# 出走表ページを同時に待つ本数（開始間隔は polite_http が3.4秒以上に保つ）
FETCH_WORKERS = 3
# 展示後に「評価が大きく変わった」とみなす的中確率の下がり幅（Supabase のトリガーと同じ値）
FLAG_HIT_DROP = 0.10
VOID_COMBOS = ("特払い", "不成立", "返還")


def log(*a):
    print("[PRE_PICKS]", *a, flush=True)


def _today():
    return datetime.now(JST).strftime("%Y%m%d")


# ---------------------------------------------------------------
# Supabase（service_role。自動固定と同じ接続）
# ---------------------------------------------------------------
def _sb(method, path, **kwargs):
    from auto_random_fix import _cfg, _headers, _request

    url, _ = _cfg()
    prefer = kwargs.pop("prefer", None)
    return _request(method, f"{url}/rest/v1/{path}", headers=_headers(prefer), timeout=30, **kwargs)


def _runtime():
    from auto_random_fix import _load_settings, _runtime_settings

    return _runtime_settings(_load_settings())


# ---------------------------------------------------------------
# 公式サイト
# ---------------------------------------------------------------
def wait_for_crawlers(max_minutes=WAIT_CRAWLER_MINUTES):
    """過去データの収集（公式サイトを読む）が動いていれば終わるまで待つ。確かめられなければ待たない。"""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return
    until = time.time() + max_minutes * 60
    while True:
        running = []
        for wf in CRAWLER_WORKFLOWS:
            try:
                r = requests.get(
                    f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs",
                    params={"status": "in_progress", "per_page": 1},
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                    timeout=15,
                )
                if r.ok and r.json().get("total_count", 0) > 0:
                    running.append(wf)
            except Exception:  # noqa: BLE001
                pass
        if not running:
            return
        if time.time() > until:
            log(f"過去データの収集が終わらないため待つのをやめる: {', '.join(running)}")
            return
        log(f"過去データの収集の終了を待っています: {', '.join(running)}")
        time.sleep(60)


def fetch_programs(date):
    from official_download import download_text, parse_b

    text = download_text("B", date)
    if text is None:
        return None
    b = parse_b(text, date)
    b["racer_id"] = b["racer_id"].astype(str)
    return b


def _fetch_racelist(rk, attempts=2):
    """1レースの出走表ページ → 6艇の行（取れなければ None）。"""
    import ml_pre
    from history_pages import _fetch, parse_racelist

    d, jcd, rno = rk.split("_")
    for i in range(attempts):
        try:
            race, boats = parse_racelist(_fetch("racelist", d, jcd, int(rno)), d, int(rno))
            if not any(b.get("racer_id") for b in boats.values()):
                raise ValueError("選手が読めない")
            rows = []
            for ln, b in boats.items():
                rec = {"race_key": rk, "lane": ln}
                rec.update({c: race.get(c) for c in ("grade", "is_final_day")})
                rec.update({c: b.get(c) for c in ml_pre.RACELIST_COLS if c not in ("grade", "is_final_day")})
                rows.append(rec)
            return rows
        except Exception:  # noqa: BLE001
            if i + 1 == attempts:
                return None
    return None


def fetch_racelists(race_keys, workers=FETCH_WORKERS):
    """各レースの出走表ページ（1レース1ページ）。取れなかったレースは除いて返す。

    公式サイトの応答は1ページ数秒かかるので、数本を並べて待つ。リクエストの開始間隔は
    polite_http が全スレッド合計で3.4秒以上に保つので、並べても公式サイトへの頻度は上がらない
    （過去データの収集 history_pages.run と同じやり方）。
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_fetch_racelist, race_keys))
    rows = [r for res in results if res for r in res]
    failed = [rk for rk, res in zip(race_keys, results) if not res]
    return pd.DataFrame(rows), failed


# ---------------------------------------------------------------
# 予想・買い目
# ---------------------------------------------------------------
def risk_race(feats_race):
    """本命の危険度（保険の買い目）の判定に使う入力。展示前なので展示タイムは無い。"""
    x = feats_race[["lane", "motor_2ren", "boat_2ren", "meet_top2"]].rename(
        columns={"meet_top2": "current_meet_top2_rate"})
    x["lane"] = x["lane"].astype(int)
    x["exhibition_time"] = np.nan
    return x.reset_index(drop=True)


def build_tickets(final, race, runtime):
    """自動固定（auto_random_fix._process_candidate）と同じ買い目・資金配分（オッズは使わない）。"""
    from prediction import adaptive_ticket_plan, assess_favorite_risk, rank_tickets, second_favorite_n_for, trifecta
    from stake_allocator import allocate_stakes_smart, ticket_hit_probability

    tri = trifecta(final)
    plan = adaptive_ticket_plan(final)
    target = int(plan["point_count"])
    main_n = min(int(plan["main_n"]), target)
    favorite_lane, risk, _ = assess_favorite_risk(race, final)
    hedge = favorite_lane if runtime["hedge_enabled"] and risk >= 2 else None
    tickets = rank_tickets(
        tri, odds=None, main_n=main_n, cover_n=target - main_n, longshot_n=0,
        longshot_min_prob=runtime["longshot_min_prob_pct"] / 100.0, hedge_lane=hedge, use_odds=False,
        first=final, min_second_coverage=plan["min_second_coverage"], close_third_gap=None,
        close_third_coverage=4, second_favorite_n=second_favorite_n_for(final),
    )
    if len(tickets) != target:
        raise RuntimeError(f"買い目点数の生成不整合: 予定{target}点 / 実際{len(tickets)}点")
    tickets = allocate_stakes_smart(
        tickets, budget=runtime["total_budget"], unit=100, min_bet=runtime["min_bet"],
        max_longshot_share=0.15, max_ticket_share=0.35, value_bias=runtime["value_bias"],
        use_odds=False, guarantee_col="second_favorite",
    )
    return tickets, ticket_hit_probability(tickets)


def _deadline_ok(date, hhmm, now, lead=MIN_LEAD_MINUTES):
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
    except Exception:  # noqa: BLE001  締切が読めなければ候補に残す
        return True
    d = pd.Timestamp(date)
    deadline = datetime(d.year, d.month, d.day, h, m, tzinfo=JST)
    return (deadline - now).total_seconds() >= lead * 60


def select_picks(cands, n=PICK_COUNT):
    """的中確率が高い順に n レース（同じ確率なら締切が早い順）。"""
    c = cands.sort_values(["hit_probability", "deadline"], ascending=[False, True]).head(n).copy()
    c["pick_rank"] = np.arange(1, len(c) + 1)
    return c


def predict_day(date, runtime, programs, racelists, now=None, lead=MIN_LEAD_MINUTES):
    """その日の全レースを展示前モデルで予想し、候補（1レース1行）と使った版を返す。"""
    import ml_live
    import ml_model as mm
    import ml_pre
    from data_paths import data_path
    from ml_nightly import HISTORY_START, _k_with_recent

    version = ml_pre.latest_version()
    model = mm.ChainModel.load(data_path(ml_live.model_file(version)))
    state = ml_live.load_state()
    reason = ml_live._check_state_date(state, date)
    if reason:
        raise RuntimeError(f"集計表が使えない: {reason}")
    start = (pd.Timestamp(date) - pd.Timedelta(days=12)).strftime("%Y%m%d")
    k = _k_with_recent(max(start, HISTORY_START), date)
    feats = ml_pre.live_pre_table(programs, racelists, state, k)

    # 6艇そろうレース・締切まで時間のあるレースだけ
    info = programs.drop_duplicates("race_key").set_index("race_key")
    n_boats = feats.groupby("race_key")["lane"].transform("count")
    feats = feats[n_boats == 6]
    if now is not None:
        ok = {rk for rk in feats["race_key"].unique() if _deadline_ok(date, info.at[rk, "deadline"], now, lead)}
        feats = feats[feats["race_key"].isin(ok)]
    names = {rk: dict(zip(g["lane"].astype(int), g["racer_name"].astype(str).str.strip()))
             for rk, g in programs.groupby("race_key")}
    rows = []
    for rk, f in feats.groupby("race_key", sort=False):
        f = f.reset_index(drop=True)
        final = ml_pre.finals(model, f, names)[rk]
        tickets, hp = build_tickets(final, risk_race(f), runtime)
        p1 = final.set_index("lane")["p_first"].astype(float)
        fav = int(p1.idxmax())
        rows.append({
            "race_key": rk, "race_date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
            "jcd": str(info.at[rk, "jcd"]).zfill(2), "venue": str(info.at[rk, "venue"]),
            "race_no": int(info.at[rk, "race_no"]), "deadline": str(info.at[rk, "deadline"] or ""),
            "model_version": version, "hit_probability": float(hp),
            "favorite_lane": fav, "favorite_name": names.get(rk, {}).get(fav, ""),
            "favorite_prob": float(p1.max()),
            "p_first": [float(p1.get(ln, 0.0)) for ln in range(1, 7)],
            "tickets": [{"combo": t["combo"], "group": t["group"], "prob": float(t["prob"]),
                         "stake": int(t["stake"])} for t in tickets.to_dict("records")],
        })
    cands = add_manshu(pd.DataFrame(rows), model, feats)
    return cands, version, str(pd.Timestamp(state["as_of"]).date())


def add_manshu(cands, model, feats):
    """候補の全レースに展示前版の万舟確率と穴寄りの買い目を付ける。失敗したら列 manshu_error だけ付ける。"""
    import manshu

    cands = cands.copy()
    if cands.empty:
        return cands
    try:
        probs, longs, mversion = manshu.predict_with_chain("pre", model, feats)
        cands["manshu_prob"] = cands["race_key"].map(probs).astype(float)
        cands["longshot_tickets"] = cands["race_key"].map(lambda rk: longs.get(rk, []))
        # 3連単の全120通りの確率（展示前）。一覧で、その時点のオッズから万舟狙いの買い目を作るのに使う
        tri = probs.attrs.get("tri")
        if tri is not None:
            by = {rk: {str(c): round(float(p), 6) for c, p in zip(g["combo"], g["prob"])}
                  for rk, g in tri.groupby("race_key", sort=False)}
            cands["tri_probs"] = cands["race_key"].map(lambda rk: by.get(rk))
        cands.attrs["manshu_version"] = mversion
    except Exception as e:  # noqa: BLE001  万舟チャンスが出せなくても厳選10は続ける
        cands.attrs["manshu_error"] = f"{type(e).__name__}: {e}"[:300]
        log(f"万舟チャンスを出せない: {type(e).__name__}")
    return cands


def select_manshu(cands, n=None):
    """万舟確率が高い順に n レース（印を付けるレース）。確率の無いレースは選ばない。"""
    import manshu

    if "manshu_prob" not in cands.columns:
        return cands.iloc[0:0]
    c = cands[cands["manshu_prob"].notna()]
    c = c.sort_values(["manshu_prob", "deadline"], ascending=[False, True]).head(n or manshu.PICK_COUNT).copy()
    c["pick_rank"] = np.arange(1, len(c) + 1)
    return c


def manshu_rows(marks):
    cols = ["race_key", "race_date", "jcd", "venue", "race_no", "deadline", "pick_rank", "manshu_prob",
            "model_version", "favorite_lane", "favorite_prob", "p_first", "tickets", "longshot_tickets"]
    cols += [c for c in ("tri_probs",) if c in marks.columns]
    out = []
    for r in marks[cols].to_dict("records"):
        r["manshu_model_version"] = marks.attrs.get("manshu_version", "")
        r["manshu_prob"] = round(float(r["manshu_prob"]), 4)
        out.append(r)
    return out


def _code_sha():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def run_picks(date=None, force=False):
    date = date or _today()
    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    have = _sb("GET", f"pre_exhibition_picks?race_date=eq.{iso}&select=race_key").json() or []
    if have and not force:
        log(f"{date}: 選定済み（{len(have)}レース）。選び直すときは --force")
        return
    t0 = time.time()
    day = _predict_today(date, t0)
    if day is None:
        return
    runtime, programs, failed, cands, version, state_as_of = day
    picks = select_picks(cands)
    pick_cols = [c for c in picks.columns if c not in ("manshu_prob", "longshot_tickets", "tri_probs")]
    if have:
        _sb("DELETE", f"pre_exhibition_picks?race_date=eq.{iso}")
    _sb("POST", "pre_exhibition_picks", json=picks[pick_cols].to_dict("records"), prefer="return=minimal")
    _sb("POST", "pre_exhibition_runs?on_conflict=race_date", prefer="resolution=merge-duplicates,return=minimal", json={
        "race_date": iso, "model_version": version, "state_as_of": state_as_of,
        "race_count": int(programs["race_key"].nunique()), "candidate_count": int(len(cands)),
        "racelist_failed": len(failed), "picked_count": int(len(picks)),
        "hit_probability_by_race": {r.race_key: round(float(r.hit_probability), 4) for r in cands.itertuples()},
        "settings": {**runtime, "pick_count": PICK_COUNT, "min_lead_minutes": MIN_LEAD_MINUTES,
                     "flag_hit_drop": FLAG_HIT_DROP},
        "code_sha": _code_sha(),
        "picked_at": datetime.now(JST).isoformat(timespec="seconds"),
    })
    # 万舟チャンス（失敗しても厳選10は保存済み）
    n_marks, manshu_error = save_manshu(date, cands)
    # すでに展示後の予想があるレース（選び直したとき）にも展示後の値を付ける
    _sb("POST", "rpc/pre_picks_sync_post", json={"p_race_date": iso})
    log(f"{date}: 候補 {len(cands)}レースから {len(picks)}レースを保存・万舟チャンス "
        f"{'失敗' if manshu_error else f'{n_marks}レース'} {time.time() - t0:.0f}秒")


def _predict_today(date, t0):
    """番組表・出走表を取り、その日の全レースを展示前モデルで予想する（締切が近いレースは除く）。"""
    runtime = _runtime()
    wait_for_crawlers()
    programs = fetch_programs(date)
    if programs is None or programs.empty:
        log(f"{date}: 番組表が無い")
        return None
    now = datetime.now(JST) if date == _today() else None
    keys = programs.drop_duplicates("race_key")
    if now is not None:
        keys = keys[[_deadline_ok(date, dl, now) for dl in keys["deadline"]]]
    racelists, failed = fetch_racelists(list(keys["race_key"]))
    log(f"{date}: 出走表 {len(keys) - len(failed)}/{len(keys)}レース {time.time() - t0:.0f}秒")
    # 出走表が取れなかったレースも番組表だけで予想する（学習にも出走表の無いレースが入っている）
    cands, version, state_as_of = predict_day(date, runtime, programs, racelists, now=datetime.now(JST) if now else None)
    if cands.empty:
        log(f"{date}: 予想できたレースが無い")
        return None
    return runtime, programs, failed, cands, version, state_as_of


def save_manshu(date, cands):
    """万舟チャンスの10レースを保存し、pre_exhibition_runs に全レースの確率を書く。戻り値 (印の数, 失敗の理由)。"""
    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    marks = select_manshu(cands)
    marks.attrs = dict(cands.attrs)
    manshu_error = cands.attrs.get("manshu_error")
    if not manshu_error:
        try:
            _sb("DELETE", f"manshu_chance_picks?race_date=eq.{iso}")
            if len(marks):
                rows = manshu_rows(marks)
                add_morning_aim(rows)
                try:
                    _sb("POST", "manshu_chance_picks", json=rows, prefer="return=minimal")
                except Exception:  # noqa: BLE001  新しい列（tri_probs・aim_*）がまだ無いデータベースでは無しで保存する
                    log(f"{date}: 万舟チャンスを買い目なしで保存（新しい列がまだ無いデータベース）")
                    _sb("POST", "manshu_chance_picks", json=[{k: v for k, v in r.items()
                                                              if k != "tri_probs" and not k.startswith("aim_")}
                                                             for r in rows], prefer="return=minimal")
                _sb("POST", "rpc/manshu_picks_sync_post", json={"p_race_date": iso})
        except Exception as e:  # noqa: BLE001  厳選10の保存は続ける
            manshu_error = f"保存に失敗: {type(e).__name__}"[:300]
            log(f"{date}: 万舟チャンスの保存に失敗: {type(e).__name__}")
    # 列は厳選10の記録とは別に書く（列がまだ無いデータベースでも厳選10の記録は残す）
    try:
        _sb("PATCH", f"pre_exhibition_runs?race_date=eq.{iso}", prefer="return=minimal", json={
            "manshu_model_version": cands.attrs.get("manshu_version"),
            "manshu_picked_count": 0 if manshu_error else int(len(marks)),
            "manshu_prob_by_race": ({r.race_key: round(float(r.manshu_prob), 4) for r in cands.itertuples()
                                     if r.manshu_prob == r.manshu_prob} if "manshu_prob" in cands.columns else None),
            "manshu_error": manshu_error,
        })
    except Exception as e:  # noqa: BLE001
        log(f"{date}: 万舟チャンスの記録（runs）に失敗: {type(e).__name__}")
    return int(len(marks)), manshu_error


class NoOdds(Exception):
    """そのレースの買い目を作れなかった理由（画面と pre_exhibition_runs にそのまま出す文）。"""


# 朝の買い目に使うオッズの組の数の下限（120通り中）。票が入っていない組はオッズが出ない
AIM_MIN_COMBOS = 100
# 締切までこれより短いレースは、取り直しの対象にしない
AIM_RETRY_LEAD_MINUTES = 5
AIM_COLS = ("aim_tickets", "aim_method", "aim_source", "aim_odds_at", "aim_saved_at", "aim_error", "aim_tried_at")


def fetch_odds3t(race_key):
    """3連単オッズ {組番: オッズ}（公式サイト。polite_http で開始間隔3.4秒以上）。
    読めなければ NoOdds（理由つき）。"""
    import polite_http
    from official_fetcher import BASE, parse_odds3t

    d, jcd, rno = race_key.split("_")
    try:
        r = polite_http.get(f"{BASE}/odds3t?hd={d}&jcd={jcd}&rno={int(rno)}", timeout=30)
    except requests.RequestException as e:
        raise NoOdds(f"公式サイトにつながりません（{type(e).__name__}）") from e
    if r.status_code != 200:
        raise NoOdds(f"公式サイトの応答が {r.status_code}")
    o = parse_odds3t(r.text, min_count=AIM_MIN_COMBOS)
    if not len(o):
        n = o.attrs.get("read", 0)
        raise NoOdds("オッズがまだ出ていません（発売前など）" if n == 0
                     else f"オッズがまだそろっていません（120通り中{n}通り）")
    return {str(c): float(v) for c, v in zip(o["combo"], o["odds"])}


def make_aim(probs, race_key, fetch=None):
    """1レースの中穴〜万舟狙いの買い目を作る。戻り値 (買い目, 理由)。作れなければ買い目は空で理由を返す。"""
    import manshu

    if not probs:
        return [], "朝の3連単の確率がありません"
    try:
        odds = (fetch or fetch_odds3t)(race_key)
    except NoOdds as e:
        return [], str(e)[:200]
    except Exception as e:  # noqa: BLE001  1レースが読めなくても他のレースは続ける
        return [], f"オッズを読めませんでした（{type(e).__name__}）"
    if not odds:
        return [], "オッズがまだ出ていません（発売前など）"
    tickets = manshu.aim_tickets(probs, odds)
    if not tickets:
        return [], "オッズ30倍以上の組がありません"
    return tickets, None


def _log_aim(label, done, total, reasons):
    why = "・".join(f"{k} {v}件" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
    log(f"{label} {done}/{total}レース" + (f"（作れなかった理由: {why}）" if why else ""))


def add_morning_aim(rows, fetch=None):
    """万舟チャンスの各レースに、朝のオッズで作った中穴〜万舟狙いの買い目を付ける（aim_*。一覧を開いたら出ている）。
    作れなかったレースは aim_error に理由を残す（8:30・9:00 の task=aim で取り直す）。10レースで10回の取得。"""
    import manshu

    done, reasons = 0, {}
    for r in rows:
        # まとめて保存するので、全部の行に同じ列をそろえる（PostgREST は行ごとに列が違うと受け付けない）
        for k in AIM_COLS:
            r.setdefault(k, None)
        now = datetime.now(JST).isoformat(timespec="seconds")
        tickets, why = make_aim(r.get("tri_probs"), r["race_key"], fetch)
        r["aim_tried_at"] = now
        if not tickets:
            r["aim_error"] = why
            reasons[why] = reasons.get(why, 0) + 1
            continue
        r.update(aim_tickets=tickets, aim_method=manshu.aim_method_name(), aim_source="morning",
                 aim_odds_at=now, aim_saved_at=now, aim_error=None)
        done += 1
    _log_aim("朝の中穴〜万舟狙いの買い目", done, len(rows), reasons)
    return done


def _minutes_to_deadline(r, now):
    try:
        dl = datetime.strptime(f"{r['race_date']} {r['deadline']}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except (TypeError, ValueError):
        return None
    return (dl - now).total_seconds() / 60


def missing_aim(date_iso, now=None):
    """万舟チャンスなのに買い目が空のレース（締切が近い・過ぎたレースは除く）。"""
    rows = _sb("GET", f"manshu_chance_picks?race_date=eq.{date_iso}&aim_tickets=is.null&result_at=is.null"
                      "&select=*&order=pick_rank.asc").json() or []
    now = now or datetime.now(JST)
    out = []
    for r in rows:
        left = _minutes_to_deadline(r, now)
        if left is None or left >= AIM_RETRY_LEAD_MINUTES:
            out.append(r)
    return out


def run_aim(date=None, fetch=None):
    """task=aim: 買い目が空のレースだけ、今のオッズで作り直す（8:30・9:00。厳選10・万舟チャンスの選定は変えない）。"""
    import manshu

    date = date or _today()
    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    rows = missing_aim(iso)
    done, reasons = 0, {}
    for r in rows:
        probs = r.get("post_tri_probs") or r.get("tri_probs")
        tickets, why = make_aim(probs, r["race_key"], fetch)
        now = datetime.now(JST).isoformat(timespec="seconds")
        patch = {"aim_tried_at": now, "aim_error": why}
        if tickets:
            patch.update(aim_tickets=tickets, aim_method=manshu.aim_method_name(), aim_source="morning",
                         aim_odds_at=now, aim_saved_at=now)
            done += 1
        else:
            reasons[why] = reasons.get(why, 0) + 1
        # 画面で先に作った買い目は上書きしない（aim_tickets が空の行だけ）
        _sb("PATCH", f"manshu_chance_picks?race_key=eq.{r['race_key']}&aim_tickets=is.null&result_at=is.null",
            prefer="return=minimal", json=patch)
    _log_aim(f"{date}: 買い目の取り直し", done, len(rows), reasons)
    return done, len(rows)


def run_check(date=None):
    """task=check: 万舟チャンスなのに買い目が空のレースを数える（あれば終了コード1。毎日の自動点検用）。"""
    date = date or _today()
    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    picks = _sb("GET", f"manshu_chance_picks?race_date=eq.{iso}&select=race_key").json() or []
    rows = missing_aim(iso)
    reasons = {}
    for r in rows:
        why = r.get("aim_error") or "理由の記録なし"
        reasons[why] = reasons.get(why, 0) + 1
    _log_aim(f"{date}: 点検 万舟チャンスなのに買い目が空のレース", len(rows), len(picks), reasons)
    if rows:
        raise SystemExit(1)


def run_manshu(date=None):
    """万舟チャンスだけ選び直す（展示前厳選10は変えない）。締切が近い・過ぎたレースは候補に入らない。"""
    date = date or _today()
    t0 = time.time()
    day = _predict_today(date, t0)
    if day is None:
        return
    cands = day[3]
    n_marks, manshu_error = save_manshu(date, cands)
    log(f"{date}: 万舟チャンス {'失敗' if manshu_error else f'{n_marks}レース'}（候補 {len(cands)}レース）"
        f" {time.time() - t0:.0f}秒")
    if manshu_error:
        raise SystemExit(1)


# ---------------------------------------------------------------
# 結果
# ---------------------------------------------------------------
def trifecta_payouts(date):
    """その日の3連単の払戻 {race_key: [(組番, 払戻), ...]}。競走成績がまだ無ければ None。"""
    import history_store as store

    p = store.read_kind("k_payouts", date, date)
    if p.empty:
        from official_download import download_text, parse_k

        text = download_text("K", date)
        if text is None:
            return None
        _, p = parse_k(text, date)
        if p.empty:
            return None
    p = p[p["bet_type"].astype(str).str.contains("3連単")].copy()
    p["race_key"] = p["race_key"].astype(str)
    p["payout"] = pd.to_numeric(p["payout"], errors="coerce")
    return {rk: list(zip(g["combo"].astype(str).str.strip(), g["payout"])) for rk, g in p.groupby("race_key")}


def ticket_result(tickets, pays):
    """買い目 [{combo, stake}] と3連単の払戻 [(組番, 払戻)] → (的中, 賭け金, 払戻額)。"""
    if not tickets:
        return None, None, None
    stake = sum(int(t.get("stake") or 0) for t in tickets)
    by_combo = {}
    for t in tickets:
        by_combo[t["combo"]] = by_combo.get(t["combo"], 0) + int(t.get("stake") or 0)
    hit, ret = False, 0
    for combo, po in pays:
        # 資金配分で0円になった候補（買っていない・画面に出していない買い目）は的中に数えない
        if by_combo.get(combo, 0) > 0:
            hit = True
            ret += int(round(by_combo[combo] * float(po) / 100.0)) if po == po and po is not None else 0
    return hit, stake, ret


def result_fields(row, pays):
    """1レースの結果の列。pays が None（そのレースの払戻が無い）なら中止・不成立として返す。"""
    out = {"result_at": datetime.now(JST).isoformat(timespec="seconds")}
    real = [(c, po) for c, po in (pays or []) if c not in VOID_COMBOS and po == po and po is not None]
    if not real:
        out["result_status"] = "void"
        return out
    out["result_status"] = "settled"
    out["trifecta_actual"] = " ".join(c for c, _ in real)
    out["trifecta_payout"] = int(max(po for _, po in real))
    out["pre_hit"], out["pre_stake"], out["pre_return"] = ticket_result(row.get("tickets") or [], real)
    if row.get("post_tickets"):
        out["post_hit"], out["post_stake"], out["post_return"] = ticket_result(row["post_tickets"], real)
    return out


def manshu_result_fields(row, pays):
    """万舟チャンスの1レースの結果の列。穴寄りの買い目は1点100円として記録する。"""
    import manshu

    out = {"result_at": datetime.now(JST).isoformat(timespec="seconds")}
    real = [(c, po) for c, po in (pays or []) if c not in VOID_COMBOS and po == po and po is not None]
    if not real:
        out["result_status"] = "void"
        return out
    out["result_status"] = "settled"
    out["trifecta_actual"] = " ".join(c for c, _ in real)
    out["trifecta_payout"] = int(max(po for _, po in real))
    out["is_manshu"] = manshu.is_manshu(out["trifecta_payout"])
    out["main_hit"], out["main_stake"], out["main_return"] = ticket_result(row.get("tickets") or [], real)
    out["longshot_hit"], out["longshot_stake"], out["longshot_return"] = ticket_result(
        manshu.stake_longshots(row.get("longshot_tickets")), real)
    if row.get("post_tickets"):
        out["post_main_hit"], out["post_main_stake"], out["post_main_return"] = ticket_result(
            row["post_tickets"], real)
    if row.get("post_longshot_tickets"):
        out["post_longshot_hit"], out["post_longshot_stake"], out["post_longshot_return"] = ticket_result(
            manshu.stake_longshots(row["post_longshot_tickets"]), real)
    if row.get("aim_tickets"):
        # 表示した万舟狙いの買い目（締切前に最後に表示したもの）
        out["aim_hit"], out["aim_stake"], out["aim_return"] = ticket_result(row["aim_tickets"], real)
    return out


def record_manshu_results(today=None):
    today = today or _today()
    iso = f"{today[:4]}-{today[4:6]}-{today[6:]}"
    rows = _sb("GET", f"manshu_chance_picks?result_at=is.null&race_date=lt.{iso}&select=race_date").json() or []
    done = 0
    for d in sorted({r["race_date"] for r in rows}):
        _sb("POST", "rpc/manshu_picks_sync_post", json={"p_race_date": d})
        pays = trifecta_payouts(d.replace("-", ""))
        if pays is None:
            log(f"{d.replace('-', '')}: 競走成績がまだ無いため万舟チャンスの結果は次回に回す")
            continue
        # select=*（列がまだ無いデータベースでも読める）
        day = _sb("GET", f"manshu_chance_picks?race_date=eq.{d}&result_at=is.null&select=*").json() or []
        for r in day:
            _sb("PATCH", f"manshu_chance_picks?race_key=eq.{r['race_key']}", prefer="return=minimal",
                json=manshu_result_fields(r, pays.get(r["race_key"])))
            done += 1
    if done:
        log(f"万舟チャンスの結果を記録 {done}レース")


def record_source_compare_results(today=None):
    """万舟チャンスの予想3種比較（manshu_source.py）で、その日のうちに結果を書けなかった行の保険。"""
    import manshu_source

    try:
        done = manshu_source.record_results(_sb, today or _today())
    except Exception as e:  # noqa: BLE001  表がまだ無い（マイグレーション前）などでも、ほかの結果の記録は失敗にしない
        log(f"予想3種比較の結果を記録できない: {type(e).__name__}")
        return
    if done:
        log(f"予想3種比較の結果を記録 {done}行")


def record_results(today=None):
    today = today or _today()
    iso = f"{today[:4]}-{today[4:6]}-{today[6:]}"
    rows = _sb("GET", f"pre_exhibition_picks?result_at=is.null&race_date=lt.{iso}"
                      "&select=race_key,race_date,tickets").json() or []
    if not rows:
        return
    done = 0
    for d in sorted({r["race_date"] for r in rows}):
        ymd = d.replace("-", "")
        _sb("POST", "rpc/pre_picks_sync_post", json={"p_race_date": d})
        pays = trifecta_payouts(ymd)
        if pays is None:
            log(f"{ymd}: 競走成績がまだ無いため次回に回す")
            continue
        day = _sb("GET", f"pre_exhibition_picks?race_date=eq.{d}&result_at=is.null"
                         "&select=race_key,tickets,post_tickets").json() or []
        for r in day:
            _sb("PATCH", f"pre_exhibition_picks?race_key=eq.{r['race_key']}", prefer="return=minimal",
                json=result_fields(r, pays.get(r["race_key"])))
            done += 1
    log(f"結果を記録 {done}レース")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["run", "results", "manshu", "aim", "check"])
    ap.add_argument("--date", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    # 買い目の取り直し・点検は、結果の記録をしない（8:30・9:00 に短く動かす）
    if a.task == "aim":
        run_aim(a.date or None)
        return
    if a.task == "check":
        run_check(a.date or None)
        return
    failed = None
    for fn, name in ((record_results, "結果"), (record_manshu_results, "万舟チャンスの結果"),
                     (record_source_compare_results, "予想3種比較の結果")):
        try:
            fn()
        except Exception as e:  # noqa: BLE001  結果の記録に失敗しても、その日の選定は続ける
            log(f"{name}の記録に失敗: {type(e).__name__}: {e}"[:300])
            failed = failed or e
    if failed is not None and a.task == "results":
        raise failed
    if a.task == "run":
        run_picks(a.date or None, force=a.force)
    elif a.task == "manshu":
        run_manshu(a.date or None)


if __name__ == "__main__":
    main()
