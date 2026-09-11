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


def post_text(user_id, access_token, text):
    """テキスト投稿をコンテナ作成からpublishまで行い、投稿IDを返す。"""
    user_id = str(user_id or "").strip()
    access_token = str(access_token or "").strip()
    text = str(text or "").strip()

    if not user_id or not access_token:
        raise ValueError("ユーザーIDとアクセストークンが必要です。")
    if not text:
        raise ValueError("投稿内容が空です。")
    if len(text) > TEXT_LIMIT:
        raise ValueError(f"本文が{TEXT_LIMIT}文字を超えています（{len(text)}文字）。")

    r = requests.post(
        f"{API_BASE}/{user_id}/threads",
        params={
            "media_type": "TEXT",
            "text": text,
            "access_token": access_token,
        },
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"投稿コンテナの作成に失敗しました（HTTP {r.status_code}）: {r.text[:300]}"
        )

    creation_id = str((r.json() or {}).get("id") or "").strip()
    if not creation_id:
        raise RuntimeError(f"creation_id が取得できませんでした: {r.text[:300]}")

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


_MARKS = ["◎", "○", "▲", "△", "×", "注"]
_FOOTER = (
    "AI予想です。的中を保証するものではありません。"
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

    df = tickets.copy()
    if "stake" in df.columns:
        stake = df["stake"].map(lambda v: _num(v, 0.0))
        if stake.sum() > 0:
            df = df[stake > 0]

    for _, row in df.iterrows():
        group = _clean(row.get("group")) or "抑え"
        combo = _clean(row.get("combo"))
        if combo:
            out.setdefault(group, []).append(combo)
    return out


def _assemble(header, fav_lines, groups, hashtags):
    parts = [header]
    if fav_lines:
        parts.append("\n".join(fav_lines))

    for label, key in (("本線", "本線"), ("抑え", "抑え"), ("穴", "穴")):
        combos = groups.get(key) or []
        if combos:
            parts.append(f"【{label}】\n" + "\n".join(combos))

    parts.append(_FOOTER)
    if hashtags:
        parts.append(" ".join(hashtags))
    return "\n\n".join(p for p in parts if p)


def build_post_text(race_date, venue, race_no, final=None, tickets=None):
    """Threads投稿本文の下書きを作る。500文字を超える場合は段階的に短縮する。"""
    venue = _clean(venue)
    race_date = _clean(race_date)
    try:
        rno = f"{int(race_no)}R"
    except Exception:
        rno = _clean(race_no)

    header = " ".join(x for x in ["🚤", race_date, venue, rno] if x)
    groups = _ticket_groups(tickets)
    hashtags = ["#競艇", "#ボートレース", "#AI予想"]
    if venue:
        hashtags.append(f"#{venue}")

    candidates = [
        (_favorites(final, 3, with_names=True), dict(groups), list(hashtags)),
        (_favorites(final, 3, with_names=False), dict(groups), list(hashtags)),
    ]
    no_hole = {k: v for k, v in groups.items() if k != "穴"}
    candidates.append((_favorites(final, 3, with_names=False), no_hole, list(hashtags)))
    candidates.append((_favorites(final, 3, with_names=False), no_hole, []))
    candidates.append(([], no_hole, []))

    for fav_lines, gr, tags in candidates:
        text = _assemble(header, fav_lines, gr, tags)
        if len(text) <= TEXT_LIMIT:
            return text

    only_main = {"本線": groups.get("本線") or []}
    return _assemble(header, [], only_main, [])[:TEXT_LIMIT]
