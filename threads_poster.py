"""Threads（スレッズ）投稿ユーティリティ。"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone

import requests


API_BASE = "https://graph.threads.net/v1.0"
EXCHANGE_URL = "https://graph.threads.net/access_token"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"
TEXT_LIMIT = 500
TOKEN_LIFETIME_DAYS = 60
TIMEOUT = 30

_TABLE = "threads_config"
_ROW_ID = 1


def _headers(sb_key, prefer=None):
    h = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def load_config(sb_url, sb_key):
    """保存済みのThreads連携情報を返す。未設定・通信失敗ならNone。"""
    sb_url = str(sb_url or "").strip().rstrip("/")
    sb_key = str(sb_key or "").strip()
    if not sb_url or not sb_key:
        return None

    try:
        r = requests.get(
            f"{sb_url}/rest/v1/{_TABLE}",
            headers=_headers(sb_key),
            params={"select": "*", "id": f"eq.{_ROW_ID}"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json() or []
    except Exception:
        return None

    if not rows:
        return None

    row = rows[0]
    user_id = str(row.get("user_id") or "").strip()
    token = str(row.get("access_token") or "").strip()
    if not user_id or not token:
        return None

    return {
        "user_id": user_id,
        "access_token": token,
        "token_updated_at": row.get("token_updated_at"),
    }


def fetch_user_id(access_token):
    """アクセストークンからThreadsユーザーIDとユーザーネームを取得する。"""
    access_token = str(access_token or "").strip()
    if not access_token:
        raise ValueError("アクセストークンが空です。")

    r = requests.get(
        f"{API_BASE}/me",
        params={"fields": "id,username", "access_token": access_token},
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"トークンからユーザー情報を取得できませんでした"
            f"（HTTP {r.status_code}）: {r.text[:300]}"
        )

    data = r.json() or {}
    uid = str(data.get("id") or "").strip()
    if not uid:
        raise RuntimeError(f"ユーザーIDが取得できませんでした: {str(data)[:300]}")
    return uid, str(data.get("username") or "").strip()


def exchange_access_token(access_token, app_secret=None):
    """短期Threadsユーザートークンを60日有効の長期トークンへ交換する。"""
    access_token = str(access_token or "").strip()
    app_secret = str(app_secret or os.environ.get("THREADS_APP_SECRET", "") or "").strip()

    if not access_token:
        raise ValueError("アクセストークンが空です。")
    if not app_secret:
        raise RuntimeError(
            "THREADS_APP_SECRET が未設定です。Streamlit Secrets にThreads App Secretを登録してください。"
        )

    r = requests.get(
        EXCHANGE_URL,
        params={
            "grant_type": "th_exchange_token",
            "client_secret": app_secret,
            "access_token": access_token,
        },
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"長期アクセストークンへの交換に失敗しました"
            f"（HTTP {r.status_code}）: {r.text[:300]}"
        )

    data = r.json() or {}
    new_token = str(data.get("access_token") or "").strip()
    if not new_token:
        raise RuntimeError(f"長期アクセストークンが取得できませんでした: {str(data)[:300]}")

    return new_token


def save_config(
    sb_url,
    sb_key,
    user_id,
    access_token,
    token_updated_at=None,
    exchange_if_possible=True,
):
    """Threads連携情報をSupabaseへ保存する。

    新しく貼り付けたトークン（token_updated_at未指定）は、THREADS_APP_SECRETが
    設定されていれば保存前に長期トークンへ交換する。既存トークンのメタ情報更新や
    refresh後の保存では再交換しない。
    """
    sb_url = str(sb_url or "").strip().rstrip("/")
    sb_key = str(sb_key or "").strip()
    user_id = str(user_id or "").strip()
    access_token = str(access_token or "").strip()

    if not sb_url or not sb_key:
        raise RuntimeError("Supabaseの接続情報が設定されていません。")
    if not access_token:
        raise ValueError("アクセストークンが必要です。")

    app_secret = str(os.environ.get("THREADS_APP_SECRET", "") or "").strip()
    if exchange_if_possible and token_updated_at is None:
        if not app_secret:
            raise RuntimeError(
                "THREADS_APP_SECRET が未設定のため、短期トークンを長期トークンへ交換できません。"
            )
        access_token = exchange_access_token(access_token, app_secret=app_secret)

    if not user_id:
        user_id, _ = fetch_user_id(access_token)

    if token_updated_at is None:
        token_updated_at = datetime.now(timezone.utc).isoformat()

    payload = {
        "id": _ROW_ID,
        "user_id": user_id,
        "access_token": access_token,
        "token_updated_at": token_updated_at,
    }
    r = requests.post(
        f"{sb_url}/rest/v1/{_TABLE}",
        headers=_headers(sb_key, prefer="resolution=merge-duplicates"),
        json=payload,
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"Supabaseへの保存に失敗しました（HTTP {r.status_code}）: {r.text[:300]}"
        )
    return True


def token_age_days(cfg):
    """トークンを最後に更新してからの日数。分からなければNone。"""
    if not cfg:
        return None
    raw = cfg.get("token_updated_at")
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    delta = datetime.now(timezone.utc) - dt
    return max(0, int(delta.total_seconds() // 86400))


def refresh_access_token(sb_url, sb_key):
    """保存済み長期トークンを延長してSupabaseへ保存し直す。"""
    cfg = load_config(sb_url, sb_key)
    if not cfg:
        raise RuntimeError("スレッズ連携が未設定です。")

    r = requests.get(
        REFRESH_URL,
        params={
            "grant_type": "th_refresh_token",
            "access_token": cfg["access_token"],
        },
        headers={"Authorization": f"Bearer {cfg['access_token']}"},
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"トークンの更新に失敗しました（HTTP {r.status_code}）: {r.text[:300]}"
        )

    data = r.json() or {}
    new_token = str(data.get("access_token") or "").strip()
    if not new_token:
        raise RuntimeError(f"更新後のトークンが取得できませんでした: {str(data)[:300]}")

    save_config(
        sb_url,
        sb_key,
        cfg["user_id"],
        new_token,
        token_updated_at=datetime.now(timezone.utc).isoformat(),
        exchange_if_possible=False,
    )
    return new_token


def _check_post_args(user_id, access_token, text):
    user_id = str(user_id or "").strip()
    access_token = str(access_token or "").strip()
    text = str(text or "").strip()
    if not user_id or not access_token:
        raise ValueError("ユーザーIDとアクセストークンが必要です。")
    if not text:
        raise ValueError("投稿内容が空です。")
    if len(text) > TEXT_LIMIT:
        raise ValueError(f"本文が{TEXT_LIMIT}文字を超えています（{len(text)}文字）。")
    return user_id, access_token, text


def _create_container(user_id, access_token, params):
    r = requests.post(
        f"{API_BASE}/{user_id}/threads",
        params={**params, "access_token": access_token},
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"投稿コンテナの作成に失敗しました（HTTP {r.status_code}）: {r.text[:300]}"
        )
    creation_id = str((r.json() or {}).get("id") or "").strip()
    if not creation_id:
        raise RuntimeError(f"creation_id が取得できませんでした: {r.text[:300]}")
    return creation_id


def _publish(user_id, access_token, creation_id):
    r2 = requests.post(
        f"{API_BASE}/{user_id}/threads_publish",
        params={
            "creation_id": creation_id,
            "access_token": access_token,
        },
        timeout=TIMEOUT,
    )
    if r2.status_code >= 400:
        raise RuntimeError(
            f"投稿の公開に失敗しました（HTTP {r2.status_code}）: {r2.text[:300]}"
        )
    post_id = str((r2.json() or {}).get("id") or "").strip()
    if not post_id:
        raise RuntimeError(f"投稿IDが取得できませんでした: {r2.text[:300]}")
    return post_id


def post_text(user_id, access_token, text, reply_to_id=None):
    """テキスト投稿をコンテナ作成からpublishまで行い、投稿IDを返す。

    reply_to_id を渡すと、その投稿への返信（スレッド形式のつなぎ）として投稿する。
    """
    user_id, access_token, text = _check_post_args(user_id, access_token, text)
    params = {"media_type": "TEXT", "text": text}
    if reply_to_id:
        params["reply_to_id"] = str(reply_to_id)
    return _publish(user_id, access_token, _create_container(user_id, access_token, params))


IMAGE_WAIT_SEC = 60
IMAGE_POLL_SEC = 3


def wait_container(creation_id, access_token, max_sec=IMAGE_WAIT_SEC, poll_sec=IMAGE_POLL_SEC, sleep=None):
    """画像付きの投稿コンテナが、スレッズ側で画像を読み終わる（FINISHED）まで待つ。"""
    import time

    sleep = sleep or time.sleep
    waited = 0
    while True:
        r = requests.get(
            f"{API_BASE}/{creation_id}",
            params={"fields": "status,error_message", "access_token": access_token},
            timeout=TIMEOUT,
        )
        data = (r.json() or {}) if r.status_code < 400 else {}
        status = str(data.get("status") or "").upper()
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"画像を読み込めませんでした: {str(data.get('error_message') or status)[:300]}")
        if waited >= max_sec:
            raise RuntimeError(f"画像の読み込みが{max_sec}秒で終わりませんでした（{status or r.status_code}）")
        sleep(poll_sec)
        waited += poll_sec


def post_image(user_id, access_token, text, image_url, reply_to_id=None):
    """画像1枚と本文の投稿（画像は公開URLから、スレッズ側が取りに行く）。投稿IDを返す。"""
    user_id, access_token, text = _check_post_args(user_id, access_token, text)
    if not str(image_url or "").startswith("https://"):
        raise ValueError("画像の公開URL（https）が必要です。")
    params = {"media_type": "IMAGE", "image_url": str(image_url), "text": text}
    if reply_to_id:
        params["reply_to_id"] = str(reply_to_id)
    creation_id = _create_container(user_id, access_token, params)
    wait_container(creation_id, access_token)
    return _publish(user_id, access_token, creation_id)


_MARKS = ["◎", "○", "▲", "△", "×", "注"]
_FOOTER = (
    "的中を保証するものではありません。"
    "舟券の購入は自己責任でお願いします。"
)


def _num(v, default=float("nan")):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _clean(v):
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none"} else s


def _favorites(final, n=3, with_names=True):
    if final is None or len(final) == 0 or "p_first" not in final.columns:
        return []

    df = final.copy()
    df["_p"] = df["p_first"].map(_num)
    df = df.sort_values("_p", ascending=False).head(int(n))

    lines = []
    for i, (_, row) in enumerate(df.iterrows()):
        lane = _clean(row.get("lane"))
        try:
            lane = str(int(float(lane)))
        except Exception:
            pass
        name = _clean(row.get("racer_name")) if with_names else ""
        mark = _MARKS[i] if i < len(_MARKS) else "・"
        lines.append(f"{mark}{lane}号艇 {name}".rstrip())
    return lines


def _ticket_groups(tickets):
    out = {"本線": [], "抑え": [], "穴": []}
    if tickets is None or len(tickets) == 0 or "combo" not in tickets.columns:
        return out

    # Threadsには予想された買い目をすべて載せる。stake は仮の資金配分であり、
    # 0円でも予想点数や投稿文の買い目から除外しない。
    df = tickets.copy()

    for _, row in df.iterrows():
        group = _clean(row.get("group")) or "抑え"
        combo = _clean(row.get("combo"))
        if combo:
            out.setdefault(group, []).append(combo)
    return out


def _assemble(header, fav_lines, groups, insight=""):
    parts = [header]
    if insight:
        parts.append(insight)
    if fav_lines:
        parts.append("\n".join(fav_lines))

    for label, key in (("本線", "本線"), ("抑え", "抑え"), ("穴", "穴")):
        combos = groups.get(key) or []
        if combos:
            parts.append(f"【{label}】\n" + "\n".join(combos))

    parts.append(_FOOTER)
    return "\n\n".join(p for p in parts if p)


def _lane_number(value):
    try:
        return int(float(value))
    except Exception:
        return None


def _race_insight(final, tickets, style_seed=""):
    """予想値と補正理由から、レース固有の短い説明を作る。"""
    if final is None or len(final) == 0 or "p_first" not in final.columns:
        return "AI予測をもとに上位候補を選びました。"

    ranked = final.copy()
    ranked["_p_first"] = ranked["p_first"].map(_num)
    ranked = ranked.sort_values("_p_first", ascending=False)
    top = ranked.iloc[0]
    top_lane = _lane_number(top.get("lane"))
    top_label = f"{top_lane}号艇" if top_lane else "本命艇"
    top_prob = _num(top.get("_p_first"), 0.0)
    second_prob = _num(ranked.iloc[1].get("_p_first"), 0.0) if len(ranked) > 1 else 0.0
    margin_pt = max(0.0, (top_prob - second_prob) * 100.0)

    positive_reasons = []
    for reason in _clean(top.get("reason")).split("/"):
        reason = reason.strip()
        if reason and not any(word in reason for word in ("下位", "低調", "不利", "展示F")):
            positive_reasons.append(reason)

    primary = "・".join(positive_reasons[:2])
    if primary:
        evidence_action = f"{primary}を評価"
        evidence_noun = primary
    elif margin_pt >= 10:
        evidence_action = (
            f"1着確率2位との差{margin_pt:.1f}ポイントを評価"
        )
        evidence_noun = f"1着確率2位との差{margin_pt:.1f}ポイント"
    else:
        evidence_action = "1着予測で最上位と判断"
        evidence_noun = "1着予測トップ"

    groups = _ticket_groups(tickets)
    point_count = sum(len(values) for values in groups.values())
    if point_count >= 10:
        ticket_note = "2・3着候補が接近しているため10点で組み立てます。"
    elif point_count == 9:
        ticket_note = "相手候補が接近しているため9点で組み立てます。"
    elif point_count:
        ticket_note = f"候補差を見ながら{point_count}点に絞りました。"
    else:
        ticket_note = "上位候補を中心に見ます。"

    styles = [
        f"{top_label}を中心視。{evidence_action}。{ticket_note}",
        f"注目は{top_label}。{evidence_action}し、軸候補としました。{ticket_note}",
        f"1着候補は{top_label}。今回のポイントは{evidence_noun}です。{ticket_note}",
        f"{evidence_action}し、{top_label}を本命に選びました。{ticket_note}",
        f"{top_label}が1着予測トップ。{evidence_noun}が判断材料です。{ticket_note}",
    ]
    style_index = sum(ord(char) for char in str(style_seed)) % len(styles)
    return styles[style_index]


def build_post_text(
    race_date,
    venue,
    race_no,
    final=None,
    tickets=None,
    deadline=None,
    publication_type="FREE",
    note_url="https://note.com/tenji_kara",
):
    """「今日も展示から。」用Threads下書き。投稿前にアプリ上で編集できる。"""
    venue = _clean(venue)
    try:
        rno = f"{int(race_no)}R"
    except Exception:
        rno = _clean(race_no)

    ranked = final.copy() if final is not None else None
    top_lane = None
    opponents = []
    if ranked is not None and len(ranked) and "p_first" in ranked.columns:
        ranked["_p"] = ranked["p_first"].map(_num)
        ranked = ranked.sort_values("_p", ascending=False)
        lanes = [_lane_number(v) for v in ranked["lane"].tolist()]
        lanes = [x for x in lanes if x]
        if lanes:
            top_lane = lanes[0]
            opponents = lanes[1:4]

    kind = str(publication_type or "FREE").upper()
    title = f"【{venue} {rno}｜展示後予想】"
    link = str(note_url or "").strip()

    if kind == "PAID":
        parts = [
            "🔒 " + title,
            "展示データ反映完了。",
            "ここは記事にします。",
            "本命・相手本線・抑え・\n3連単の最終買い目をまとめました。",
            "続きはプロフィールのnoteから☝️",
        ]
    else:
        main = f"◎ {top_lane}号艇" if top_lane else "◎ 本命はnoteに掲載"
        if opponents:
            main += f"\n相手本線：{'・'.join(f'{x}号艇' for x in opponents[:2])}"
            if len(opponents) >= 3:
                main += f"\n抑え：{opponents[2]}号艇"
        parts = [
            "🚤 " + title,
            "展示データ反映後の最終予想。",
            main,
            "無料で公開します。",
            "買い目はnoteに公開しました。\nプロフィールのリンクからどうぞ☝️",
        ]

    text = "\n\n".join(parts)
    return text[:TEXT_LIMIT]



# -------------------------------------------------
# 買い目の書き方（スレッズ・note・予想カードで共通）
# -------------------------------------------------
def compact_combos(combos):
    """同じ1着-2着の3着候補をまとめる。例: 1-2-3,1-2-4,1-2-6 -> 1-2-346"""
    grouped = {}
    fallback = []
    for combo in combos:
        parts = [p.strip() for p in str(combo).split("-")]
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            key = (parts[0], parts[1])
            grouped.setdefault(key, [])
            if parts[2] not in grouped[key]:
                grouped[key].append(parts[2])
        else:
            fallback.append(str(combo))
    out = [f"{a}-{b}-{''.join(thirds)}" for (a, b), thirds in grouped.items()]
    return out + fallback


def split_main_cover(tickets):
    """買い目（DataFrame か [{combo, group}]）→ (本線の組番, 抑えの組番)。穴は抑えに入れる。"""
    if tickets is None:
        return [], []
    rows = tickets.to_dict("records") if hasattr(tickets, "to_dict") else list(tickets)
    main, cover = [], []
    for t in rows:
        combo = _clean(t.get("combo"))
        if not combo:
            continue
        (main if _clean(t.get("group")) == "本線" else cover).append(combo)
    return main, cover


# -------------------------------------------------
# 朝の無料3レース（展示前予想）の自動投稿
# -------------------------------------------------
# 文章は後で Claude の文章に差し替えられるよう、1レース分を作る関数を1つにしている
# （threads_morning.py の build_texts が writer を受け取り、失敗したらこの型に戻す）。
MORNING_FOOTER = "※展示前の情報だけの予想です。的中を保証するものではありません。舟券の購入は自己責任でお願いします。"
# 最後の投稿に入れる note の宣伝（文章を変えるときはここだけ直す）
MORNING_NOTE_PROMO = (
    "📝 展示後の最終予想はnoteで\n"
    "展示データを反映した本命・本線・抑えの買い目を、有料記事で公開しています。\n"
    "プロフィールのnoteからどうぞ☝️"
)


def build_morning_pick_text(race_date, pick, index, total, promo=""):
    """朝の無料レース1つ分の投稿（決まった型）。pick は pre_exhibition_picks の1行（買い目はモデルのみ）。

    promo（note の宣伝）を渡すと、注意書きの前に入れる。
    """
    venue = _clean(pick.get("venue"))
    try:
        rno = f"{int(pick.get('race_no'))}R"
    except Exception:
        rno = _clean(pick.get("race_no"))
    deadline = _clean(pick.get("deadline")) or "--:--"
    lane = _lane_number(pick.get("favorite_lane"))
    name = _clean(pick.get("favorite_name"))
    main, cover = split_main_cover(pick.get("tickets"))

    parts = [
        f"🌅【展示前予想】{_short_date(race_date)} 朝の無料{total}レース（{index}/{total}）",
        f"{venue} {rno}｜締切 {deadline}",
        f"◎本命 {lane}号艇 {name}".rstrip() if lane else "",
    ]
    if main:
        parts.append("【本線】\n" + "\n".join(compact_combos(main)))
    if cover:
        parts.append("【抑え】\n" + "\n".join(compact_combos(cover)))
    parts.append(promo)
    parts.append(MORNING_FOOTER)
    return "\n\n".join(p for p in parts if p)[:TEXT_LIMIT]


# -------------------------------------------------
# 1日の発信まとめ
# -------------------------------------------------
_PUBLICATIONS_TABLE = "note_publications"
_MORNING_TABLE = "threads_auto_posts"
_RESULTS_TABLE = "prediction_results"
_RESULT_FIELDS = (
    "race_key,trifecta_actual,top_ticket,"
    "hit_top_ticket,hit_any_ticket,candidate_hit,candidate_hit_rank"
)
VOID_COMBOS = ("特払い", "不成立", "返還")


def _date_text(value):
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value or "").strip()[:10]


def _short_date(value):
    s = _date_text(value)
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
        return f"{d.month}/{d.day}"
    except Exception:
        return s


def _race_label(venue, race_no):
    venue = _clean(venue)
    try:
        return f"{venue}{int(race_no)}R"
    except Exception:
        return f"{venue}{_clean(race_no)}"


def _actual_combos(text):
    """3連単の結果（同着なら複数）を組番のリストにする。"""
    return [c for c in str(text or "").replace(",", " ").split() if c and c not in VOID_COMBOS]


def judge_record(record):
    """結果の分かったレースの的中を、そのレースで載せた買い目で決める（record を書き換えて返す）。

    買い目（tickets）が無い古い note の行だけ、今までどおり保存予想の候補全体の的中
    （candidate_hit、無ければ hit_any_ticket）を使う。
    """
    actual = _actual_combos(record.get("trifecta_actual"))
    record["settled"] = bool(actual)
    if not actual:
        record["hit"] = False
        record["hit_top_ticket"] = False
        return record
    combos = [str(c) for c in (record.get("tickets") or [])]
    if combos:
        record["hit"] = any(c in combos for c in actual)
        record["hit_top_ticket"] = combos[0] in actual
    else:
        legacy = record.get("legacy") or {}
        value = legacy.get("candidate_hit")
        if value is None:
            value = legacy.get("hit_any_ticket")
        record["hit"] = bool(value)
        record["hit_top_ticket"] = bool(legacy.get("hit_top_ticket"))
    return record


def apply_actuals(records, actuals):
    """公式の結果一覧などで分かった3連単 {race_key: 組番} で、結果待ちのレースを埋めて判定し直す。"""
    for r in records or []:
        if not _actual_combos(r.get("trifecta_actual")) and actuals.get(r.get("race_key")):
            r["trifecta_actual"] = str(actuals[r["race_key"]])
        judge_record(r)
    return records


def _ticket_combos(tickets):
    out = []
    for t in tickets or []:
        combo = _clean(t.get("combo") if isinstance(t, dict) else t)
        if combo:
            out.append(combo)
    return out


def _get(sb_url, sb_key, table, params, what):
    r = requests.get(f"{sb_url}/rest/v1/{table}", headers=_headers(sb_key), params=params, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"{what}の取得に失敗しました（HTTP {r.status_code}）: {r.text[:300]}")
    return r.json() or []


def _db_actuals(sb_url, sb_key, race_keys):
    """Supabase にある3連単の結果 {race_key: 組番}（保存予想の結果と、展示前厳選の結果）。"""
    keys = sorted({k for k in race_keys if k})
    if not keys:
        return {}, {}
    rows = _get(sb_url, sb_key, _RESULTS_TABLE,
                {"select": _RESULT_FIELDS, "race_key": f"in.({','.join(keys)})"}, "的中結果")
    legacy = {str(r.get("race_key")): r for r in rows}
    actuals = {k: _clean(r.get("trifecta_actual")) for k, r in legacy.items() if _clean(r.get("trifecta_actual"))}
    try:
        pre = _get(sb_url, sb_key, "pre_exhibition_picks",
                   {"select": "race_key,trifecta_actual", "race_key": f"in.({','.join(keys)})"}, "厳選の結果")
        for r in pre:
            if _clean(r.get("trifecta_actual")):
                actuals.setdefault(str(r["race_key"]), _clean(r["trifecta_actual"]))
    except Exception:
        pass
    return actuals, legacy


def fetch_daily_publication_results(sb_url, sb_key, publication_date):
    """指定日のnote記事のレースと、確定済みなら的中結果を返す（sequence_no 順の dict のリスト）。

    的中は記事に載せた買い目（article_tickets）で判定する。結果がまだ分からないレースは
    ``settled=False``（apply_actuals で公式の結果一覧から埋められる）。
    """
    sb_url = str(sb_url or "").strip().rstrip("/")
    sb_key = str(sb_key or "").strip()
    if not sb_url or not sb_key:
        raise RuntimeError("Supabaseの接続情報が設定されていません。")

    publications = _get(sb_url, sb_key, _PUBLICATIONS_TABLE, {
        "select": "race_key,venue,race_no,sequence_no,publication_type,ticket_source,article_tickets",
        "publication_date": f"eq.{_date_text(publication_date)}",
        "order": "sequence_no.asc",
    }, "note確定レース")
    actuals, legacy = _db_actuals(sb_url, sb_key, [str(p.get("race_key") or "").strip() for p in publications])

    records = []
    for pub in publications:
        key = str(pub.get("race_key") or "").strip()
        records.append(judge_record({
            "section": "note",
            "race_key": key,
            "venue": _clean(pub.get("venue")),
            "race_no": pub.get("race_no"),
            "sequence_no": pub.get("sequence_no"),
            "publication_type": str(pub.get("publication_type") or "PAID").upper(),
            "ticket_source": _clean(pub.get("ticket_source")) or "model",
            "tickets": _ticket_combos(pub.get("article_tickets")),
            "legacy": legacy.get(key) or {},
            "trifecta_actual": actuals.get(key, ""),
        }))
    return records


def fetch_morning_post_results(sb_url, sb_key, race_date):
    """指定日の朝の自動投稿で投稿できたレースと、確定済みなら的中結果（投稿した買い目で判定）。"""
    sb_url = str(sb_url or "").strip().rstrip("/")
    sb_key = str(sb_key or "").strip()
    if not sb_url or not sb_key:
        raise RuntimeError("Supabaseの接続情報が設定されていません。")
    rows = _get(sb_url, sb_key, _MORNING_TABLE,
                {"select": "status,posts", "race_date": f"eq.{_date_text(race_date)}"}, "朝の自動投稿")
    posts = [p for p in ((rows[0].get("posts") if rows else None) or []) if p.get("post_id")]
    actuals, _ = _db_actuals(sb_url, sb_key, [str(p.get("race_key") or "") for p in posts])
    return [judge_record({
        "section": "morning",
        "race_key": str(p.get("race_key") or ""),
        "venue": _clean(p.get("venue")),
        "race_no": p.get("race_no"),
        "ticket_source": "model",
        "tickets": _ticket_combos(p.get("tickets")),
        "trifecta_actual": actuals.get(str(p.get("race_key") or ""), ""),
    }) for p in posts]


def _closing(hits, settled):
    """締めの一言（的中率で3パターン）。浮動小数の誤差を避けるため 50% 判定は整数比較で行う。"""
    if len(hits) * 2 > len(settled):
        return "明日の予想もお楽しみに✨"
    if len(hits) * 2 == len(settled):
        return "五分五分でした、明日に繋げます🙏"
    return "もっと予想精度上げれる様に頑張ります💪"


def _section_lines(records, payouts, include_pending):
    lines = []
    for r in records:
        if not r.get("settled"):
            if include_pending:
                lines.append(f"⏳ {_race_label(r.get('venue'), r.get('race_no'))} 結果待ち")
            continue
        mark = "⭕" if r.get("hit") else "❌"
        line = f"{mark} {_race_label(r.get('venue'), r.get('race_no'))} {r.get('trifecta_actual')}"
        payout = _num(payouts.get(r.get("race_key")), 0.0)
        if r.get("hit") and payout > 0:
            line += f"（{int(payout):,}円）"
        lines.append(line)
    return lines


def _section_headline(records):
    settled = [r for r in records if r.get("settled")]
    hits = [r for r in settled if r.get("hit")]
    result_text = f"{len(hits)}レース的中🎯" if hits else "的中なし"
    if len(settled) == len(records):
        return f"{len(records)}レース中、{result_text}"
    if not settled:
        return f"{len(records)}レースとも結果待ちです。"
    return f"{len(records)}レースのうち、結果確定{len(settled)}レース中、{result_text}"


def build_daily_summary_text(
    race_date,
    records,
    payouts=None,
    include_pending=True,
    note_url="https://note.com/tenji_kara",
    morning_records=None,
):
    """「今日も展示から。」用の1日まとめThreads下書き。投稿前にアプリ上で編集できる。

    「朝の無料3レース」（morning_records。投稿した買い目で判定）と「noteのレース」
    （records。記事に載せた買い目で判定）を分けて載せる。payouts は
    {race_key: 3連単100円払戻} があれば的中レースに払戻を添える。結果待ちのレースは
    的中率の分母に入れず、include_pending=True なら「結果待ち」として一覧に残す。
    締めの一言は、両方を合わせた結果確定分の的中率で決める。
    """
    note = list(records or [])
    morning = list(morning_records or [])
    payouts = payouts or {}
    title = f"【{_short_date(race_date)}｜本日の予想まとめ】"
    if not note and not morning:
        return ("📊 " + title + "\n\n本日の発信はありませんでした。")[:TEXT_LIMIT]

    parts = ["📊 " + title]
    for label, recs, empty in (
        ("🌅 朝の無料3レース（展示前予想）", morning, "本日の投稿はありませんでした。"),
        ("📝 noteのレース（展示後予想）", note, "本日の記事はありませんでした。"),
    ):
        if not recs:
            parts.append(f"{label}\n{empty}")
            continue
        body = [label, _section_headline(recs)] + _section_lines(recs, payouts, include_pending)
        parts.append("\n".join(body))

    everything = morning + note
    settled = [r for r in everything if r.get("settled")]
    hits = [r for r in settled if r.get("hit")]
    if any(not r.get("settled") for r in everything):
        parts.append("残りのレースは結果が出たらお知らせします。")
    if settled:
        parts.append(_closing(hits, settled))
    if str(note_url or "").strip():
        parts.append("予想はプロフィールのnoteから☝️")
    return "\n\n".join(parts)[:TEXT_LIMIT]


# -------------------------------------------------
# 1日の発信まとめ（note記事用）
# -------------------------------------------------
NOTE_SUMMARY_HASHTAGS = "#ボートレース #競艇 #展示 #予想 #今日の結果"


def _summary_counts(records, include_pending):
    records = list(records or [])
    settled = [r for r in records if r.get("settled")]
    pending = [r for r in records if not r.get("settled")]
    hits = [r for r in settled if r.get("hit")]
    shown = records if include_pending else settled
    return records, settled, pending, hits, shown


def _note_result_line(r, payouts, with_actual=True):
    label = _race_label(r.get("venue"), r.get("race_no"))
    if not r.get("settled"):
        return f"⏳ {label}　結果待ち"
    mark = "⭕" if r.get("hit") else "❌"
    line = f"{mark} {label}"
    if with_actual:
        line += f"　{r.get('trifecta_actual')}"
    payout = _num(payouts.get(r.get("race_key")), 0.0)
    if r.get("hit") and payout > 0:
        line += f"（{int(payout):,}円）"
    return line


def _hit_rate_text(hits, settled):
    if not settled:
        return "—"
    return f"{len(hits) / len(settled) * 100:.0f}%"


def _closing_by_rate(hits, settled):
    """Threadsまとめと同じ的中率の分岐で締めの一言を返す。"""
    if not settled:
        return "結果が出そろったら、あらためて振り返ります。"
    return _closing(hits, settled)


def build_daily_summary_note_texts(
    race_date,
    records,
    payouts=None,
    include_pending=True,
):
    """1日まとめのnote記事を3パターン作る。

    戻り値は {"label", "title", "body"} の dict を3つ並べたリスト。
    パターンは「シンプル結果報告」「振り返り（数字多め）」「ひとこと＋結果」。
    集計の考え方（結果待ちは的中率の分母に入れない等）は
    build_daily_summary_text() と同じ。
    """
    payouts = payouts or {}
    records, settled, pending, hits, shown = _summary_counts(records, include_pending)
    day = _short_date(race_date)

    if not records:
        body = "本日のnote発信はありませんでした。\n\nまた次回の予想でお会いしましょう。"
        return [
            {"label": label, "title": f"【{day}】本日の予想まとめ", "body": body}
            for label in ("① シンプル結果報告", "② 振り返り（数字多め）", "③ ひとこと＋結果")
        ]

    if pending:
        score = f"確定{len(settled)}R中{len(hits)}的中（{len(pending)}R結果待ち）"
    else:
        score = f"{len(records)}R中{len(hits)}的中"
    pending_note = "残りのレースは結果が出たら追記します。" if pending else ""
    result_lines = "\n".join(_note_result_line(r, payouts) for r in shown)
    hit_payouts = [
        int(_num(payouts.get(r.get("race_key")), 0.0))
        for r in hits
        if _num(payouts.get(r.get("race_key")), 0.0) > 0
    ]
    closing = _closing_by_rate(hits, settled)

    # ① シンプル結果報告
    p1 = [
        f"{day}の予想結果をまとめました。",
        f"■ 本日の成績\n{score}",
        f"■ レース別結果\n{result_lines}" if result_lines else "",
        pending_note,
        closing,
        NOTE_SUMMARY_HASHTAGS,
    ]

    # ② 振り返り（数字多め）
    top_hits = [r for r in settled if r.get("hit_top_ticket")]
    stats = [
        f"・発信レース数：{len(records)}R",
        f"・結果確定：{len(settled)}R",
        f"・的中：{len(hits)}R（的中率 {_hit_rate_text(hits, settled)}）",
        f"・本線（最上位の買い目）的中：{len(top_hits)}R",
    ]
    if hit_payouts:
        stats.append(f"・最高払戻（3連単100円あたり）：{max(hit_payouts):,}円")
    if pending:
        stats.append(f"・結果待ち：{len(pending)}R")
    if hits:
        review = "的中したレースは、展示から読んだ展開がハマった形でした。"
    else:
        review = "今日は展示から読んだ展開が結果に結びつきませんでした。"
    if len(hits) < len(settled):
        review += "外れたレースは、どこで読みがズレたかを見直して次に活かします。"
    p2 = [
        f"{day}の発信レースを数字で振り返ります。",
        "■ 本日の数字\n" + "\n".join(stats),
        f"■ レース別結果（3連単の着順）\n{result_lines}" if result_lines else "",
        f"■ ひとこと振り返り\n{review}",
        pending_note,
        closing,
        NOTE_SUMMARY_HASHTAGS,
    ]

    # ③ ひとこと＋結果（短め）
    if hits and hit_payouts:
        lead = f"今日は{len(hits)}本的中🎯 最高{max(hit_payouts):,}円の払戻でした。"
    elif hits:
        lead = f"今日は{len(hits)}本的中🎯"
    elif settled:
        lead = "今日は的中なし…悔しい一日でした。"
    else:
        lead = "今日の発信レースはまだ結果待ちです。"
    short_lines = "\n".join(_note_result_line(r, payouts, with_actual=False) for r in shown)
    p3 = [
        f"{day}もお疲れさまでした！\n{lead}",
        short_lines,
        pending_note,
        "明日も展示を見てから予想を出します。\nフォローしてお待ちください☝️",
        NOTE_SUMMARY_HASHTAGS,
    ]

    def _join(parts):
        return "\n\n".join(p for p in parts if p)

    return [
        {
            "label": "① シンプル結果報告",
            "title": f"【{day}｜本日の予想結果】{score}",
            "body": _join(p1),
        },
        {
            "label": "② 振り返り（数字多め）",
            "title": f"【{day}｜予想振り返り】的中率{_hit_rate_text(hits, settled)}・{score}",
            "body": _join(p2),
        },
        {
            "label": "③ ひとこと＋結果",
            "title": f"【{day}】今日の結果まとめ｜{len(hits)}本的中" if hits else f"【{day}】今日の結果まとめ",
            "body": _join(p3),
        },
    ]
