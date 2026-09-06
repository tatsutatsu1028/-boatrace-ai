"""
Threads（スレッズ）投稿ユーティリティ。

app.py（Streamlit）と track_odds.py（GitHub Actions）の両方から使うため、
streamlit には依存しない。Supabase の接続情報は呼び出し側から受け取る。

Threads API の流れ
------------------
投稿は2段階。1回のリクエストでは投稿できない。

  1. POST /{user-id}/threads          … 投稿コンテナを作る → creation_id
  2. POST /{user-id}/threads_publish  … creation_id を公開する → post_id

トークンについて
----------------
- 長期アクセストークン（long-lived）の有効期限は60日。
- GET /refresh_access_token で延長できる（発行から24時間以上経過が条件）。
- 失効すると再取得にMeta for Developersでの手作業が必要になるため、
  track_odds.py が20日間隔で自動更新する。

必要な権限（スコープ）
--------------------
  threads_basic / threads_content_publish

Supabase側のテーブル（threads_config_table.sql 参照）
--------------------------------------------------
  id BIGINT PRIMARY KEY   … 常に 1（単一行）
  user_id TEXT
  access_token TEXT
  token_updated_at TIMESTAMPTZ
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import requests


API_BASE = "https://graph.threads.net/v1.0"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"

# スレッズ本文の上限。API側は500文字。
TEXT_LIMIT = 500

# トークンの有効期限（Meta側の仕様）
TOKEN_LIFETIME_DAYS = 60

TIMEOUT = 30

_TABLE = "threads_config"
_ROW_ID = 1


# ---------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------
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
    """
    保存済みの連携情報を返す。未連携・未設定・通信失敗のときは None。

    戻り値: {"user_id": str, "access_token": str, "token_updated_at": str|None}
    """
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
        rows = r.json()
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


def save_config(sb_url, sb_key, user_id, access_token, token_updated_at=None):
    """連携情報を保存（既存があれば上書き）。失敗時は例外を投げる。"""
    sb_url = str(sb_url or "").strip().rstrip("/")
    sb_key = str(sb_key or "").strip()

    if not sb_url or not sb_key:
        raise RuntimeError("Supabaseの接続情報が設定されていません。")

    user_id = str(user_id or "").strip()
    access_token = str(access_token or "").strip()

    if not user_id or not access_token:
        raise ValueError("ユーザーIDとアクセストークンの両方が必要です。")

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
    """トークンを最後に更新してからの日数。分からなければ None。"""
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


# ---------------------------------------------------------------
# Threads API
# ---------------------------------------------------------------
def refresh_access_token(sb_url, sb_key):
    """
    長期トークンを延長して保存し直す。

    有効期限60日に対し、track_odds.py が20日間隔で呼ぶ想定。
    延長できない状態（発行から24時間未満・すでに失効）では例外を投げる。
    """
    cfg = load_config(sb_url, sb_key)
    if not cfg:
        raise RuntimeError("スレッズ連携が未設定です。")

    r = requests.get(
        REFRESH_URL,
        params={
            "grant_type": "th_refresh_token",
            "access_token": cfg["access_token"],
        },
        timeout=TIMEOUT,
    )

    if r.status_code >= 400:
        raise RuntimeError(
            f"トークンの更新に失敗しました（HTTP {r.status_code}）: {r.text[:300]}"
        )

    data = r.json()
    new_token = str(data.get("access_token") or "").strip()

    if not new_token:
        raise RuntimeError(f"更新後のトークンが取得できませんでした: {str(data)[:300]}")

    save_config(sb_url, sb_key, cfg["user_id"], new_token)
    return new_token


def post_text(user_id, access_token, text):
    """
    テキストのみの投稿を publish まで行い、投稿IDを返す。

    2段階（コンテナ作成 → 公開）のどちらで失敗したか分かるように
    エラーメッセージを分けている。
    """
    user_id = str(user_id or "").strip()
    access_token = str(access_token or "").strip()
    text = str(text or "").strip()

    if not user_id or not access_token:
        raise ValueError("ユーザーIDとアクセストークンが必要です。")

    if not text:
        raise ValueError("投稿内容が空です。")

    if len(text) > TEXT_LIMIT:
        raise ValueError(f"本文が{TEXT_LIMIT}文字を超えています（{len(text)}文字）。")

    # 1. コンテナ作成
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

    # 2. 公開
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


# ---------------------------------------------------------------
# 投稿文の組み立て
# ---------------------------------------------------------------
_MARKS = ["◎", "○", "▲", "△", "×", "注"]

# 投稿の末尾に必ず付ける文。ここを書き換えれば全投稿に反映される。
#
# 免責文について
# --------------
# 「確実に当たるものではありません」の類は入れておいて損はないが、
# 効果を過信しないこと。景品表示法の優良誤認は本文の主張で判断され、
# 打消し表示（免責文）を添えても、主張自体が誤認を招くなら消えない。
# つまり守ってくれるのは免責文ではなく、本文に何を書かないかの方。
#
#   入れてはいけない: 的中率○○% / 回収率○○% / 絶対 / 必ず / 鉄板
#                     万券的中の実績、当たった投稿だけの再掲
#
# 投稿には購入金額も入れない。金額は人によって違ううえ、
# 収支の話にすると実績訴求とみなされやすくなるため。
_FOOTER = (
    "AI予想です。的中を保証するものではありません。"
    "舟券の購入は自己責任でお願いします。"
)

# 公営競技の宣伝として付けるなら、この行も足せる（法的な義務ではない）。
# 使う場合は _assemble() の parts に加える。
#   "20歳未満の方は舟券を購入できません。"


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
    """1着確率の高い順に ◎○▲ を並べた行のリスト。"""
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
    """{"本線": [...], "抑え": [...], "穴": [...]} を買い目順で返す。"""
    out = {"本線": [], "抑え": [], "穴": []}

    if tickets is None or len(tickets) == 0 or "combo" not in tickets.columns:
        return out

    df = tickets.copy()

    # 購入額0（見送り）は投稿に載せない。金額そのものは載せない。
    if "stake" in df.columns:
        stake = df["stake"].map(lambda v: _num(v, 0.0))
        if stake.sum() > 0:
            df = df[stake > 0]

    for _, row in df.iterrows():
        group = _clean(row.get("group")) or "抑え"
        combo = _clean(row.get("combo"))
        if not combo:
            continue
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
    """
    投稿本文の下書きを作る。app.py 側で編集してから送信する前提。

    TEXT_LIMIT を超えないよう、超えた場合は
      選手名を落とす → 穴を落とす → ハッシュタグを落とす
      → 印を落とす
    の順に短くしていく。
    """
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

    # 段階的に短くする候補を上から順に試す
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

    # ここまで来たら買い目が多すぎる。最後は本線だけに絞る。
    only_main = {"本線": groups.get("本線") or []}
    text = _assemble(header, [], only_main, [])
    return text[:TEXT_LIMIT]
