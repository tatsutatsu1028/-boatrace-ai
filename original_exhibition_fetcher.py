"""
万舟チャンスのレースだけ、各場の公式サイトのオリジナル展示（一周・まわり足・直線）を取って記録する。

  - 対象は manshu_chance_picks に入っている当日の万舟チャンスのレースだけ。
    展示後の自動予想（auto_post_predict.py）の run() の最後に、そのレースの展示後予想が済んだら
    （auto_post_log が done / already）1回取る。予想・頭本命・買い目・通知には使わない（記録のみ）
  - 1レースにつき取得は1回。まだ出ていなければ（未公開）、次の起動（RETRY_AFTER 分以上あと）に1回だけ取り直す。
    通信の失敗も同じく1回だけ取り直す。形式違い・対象外の場は取り直さない
  - 文字（HTML の表）で載せている場だけを対象にする（VENUES）。それ以外の場は通信せず「対象外」と理由を残す
  - 通信は polite_http.get（開始間隔 3.4 秒以上・メンテナンス時間は待つ）。URL は各場のページ自身が
    読み込むものだけを使う（推測した URL は使わない）

保存先（supabase/migrations/20261110000000_original_exhibition.sql）
  original_exhibition     : 1艇1行（race_key, lane, original_lap, original_turn, original_straight, source_url, fetched_at）
  original_exhibition_log : 1レース1行。status = ok / unpublished（未公開）/ format（形式違い）/ error（取得失敗）
                            / unsupported（対象外の場）と理由・回数
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
TABLE = "original_exhibition"
LOG_TABLE = "original_exhibition_log"
MAX_ATTEMPTS = 2          # 1回 + 取り直し1回
RETRY_AFTER = 4           # 取り直しまでの最短の分（auto_post_predict の起動は5分おき）
GIVE_UP_AFTER = 60        # 締切からこの分を過ぎたら、取り直しはしない
RETRYABLE = ("unpublished", "error")

# 各場の公式サイトのオリジナル展示。どれも「直前情報」の中のタブ（iframe）で、ページ自身の JavaScript が
# 組み立てる URL をそのまま使う。{day} = YYYYMMDD, {race} = レース番号
#   oriten        : /modules/yosou/oriten.php?day=&race=&jo=&if=1（多摩川のタブ「オリジナル展示データ」）
#   group-cyokuzen: /modules/yosou/group-cyokuzen.php?day=&race=&kind=2&if=1（直前情報の小タブ「オリジナル展示データ」）
#   group-tenji   : /modules/yosou/group-tenji.php?type=group-tenji&day=&race=&kind=1&if=1（展示情報の小タブ「オリジナル展示」）
VENUES = {
    "05": "https://www.boatrace-tamagawa.com/modules/yosou/oriten.php?day={day}&race={race}&jo=05&if=1",
    "08": "https://www.boatrace-tokoname.jp/modules/yosou/group-cyokuzen.php?day={day}&race={race}&kind=2&if=1",
    "09": "https://www.boatrace-tsu.com/modules/yosou/group-tenji.php?type=group-tenji&day={day}&race={race}&kind=1&if=1",
    "10": "https://www.boatrace-mikuni.jp/modules/yosou/group-cyokuzen.php?day={day}&race={race}&kind=2&if=1",
    "14": "https://www.n14.jp/modules/yosou/group-cyokuzen.php?day={day}&race={race}&kind=2&if=1",
    "19": "https://www.boatrace-shimonoseki.jp/modules/yosou/group-cyokuzen.php?day={day}&race={race}&kind=2&if=1",
    "21": "https://www.boatrace-ashiya.com/modules/yosou/group-cyokuzen.php?day={day}&race={race}&kind=2&if=1",
}

# 対象外の場の理由（無い場は UNSUPPORTED_DEFAULT）
UNSUPPORTED = {
    "01": "対象外（一周ではなく半周タイムの公開で、項目が違う）",
    "03": "対象外（オリジナル展示を公開していない）",
}
UNSUPPORTED_DEFAULT = "対象外（文字で取れる公開ページを確認できていない場）"

LABELS = {"一周": "original_lap", "まわり足": "original_turn", "直線": "original_straight"}
# 値の範囲（外れたら形式違いとみなす。単位は秒）
RANGES = {"original_lap": (30.0, 45.0), "original_turn": (3.0, 10.0), "original_straight": (5.0, 10.0)}
NOT_YET = ("ただいまデータはございません", "しばらくお待ちください", "データがありません")


def log(*a):
    print("[ORIGINAL_EXHIBITION]", *a, flush=True)


def source_url(jcd, race_key):
    tpl = VENUES.get(str(jcd).zfill(2))
    if not tpl:
        return None
    return tpl.format(day=race_key[:8], race=int(race_key.split("_")[2]))


# ---------------------------------------------------------------
# 表を読む（通信しない）
# ---------------------------------------------------------------
def _text(s):
    return _html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("　", " ").strip()


def _num(s):
    s = _text(s).replace(" ", "")
    if not s or set(s) <= {"-", "－", "‐", "ー"}:
        return None
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"数値でない値: {s[:10]}") from None


def parse(page):
    """オリジナル展示の表 → ("ok", {lane: {original_lap, original_turn, original_straight}}, None)
    または ("unpublished" / "format", None, 理由)。

    表の作り（各場共通）: 枠番のセル（class に waku）がある行の、最後の3つのセルが見出しの順
    （一周・まわり足・直線。場によって順番が違ってもよい）。値が「-」の艇は計測なし（None）。"""
    if not page:
        return "format", None, "空のページ"
    for w in NOT_YET:
        if w in page:
            return "unpublished", None, f"未公開（{w}）"
    head = page.split("<tbody", 1)[0]
    pos = {}
    for label, col in LABELS.items():
        m = [x.start() for x in re.finditer(label, head)]
        if m:
            pos[col] = m[-1]   # 見出しの表の中のもの（タブ名などより後ろ）
    if len(pos) != 3:
        return "format", None, "形式違い（一周・まわり足・直線の見出しがそろっていない）"
    order = sorted(pos, key=pos.get)
    rows = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        tds = re.findall(r"<td([^>]*)>(.*?)</td>", tr, re.S)
        if not tds or "waku" not in tds[0][0]:
            continue
        lane = _text(tds[0][1])
        if not lane.isdigit() or not 1 <= int(lane) <= 6 or len(tds) < 4:
            continue
        try:
            vals = [_num(v) for _, v in tds[-3:]]
        except ValueError as e:
            return "format", None, f"形式違い（{e}）"
        rows[int(lane)] = dict(zip(order, vals))
    if sorted(rows) != [1, 2, 3, 4, 5, 6]:
        return "format", None, f"形式違い（枠の行が6つそろわない: {len(rows)}）"
    if all(v is None for r in rows.values() for v in r.values()):
        return "unpublished", None, "未公開（値が入っていない）"
    for r in rows.values():
        for col, v in r.items():
            lo, hi = RANGES[col]
            if v is not None and not lo <= v <= hi:
                return "format", None, f"形式違い（{col} の値が範囲外: {v}）"
    return "ok", rows, None


# ---------------------------------------------------------------
# 取る・記録する
# ---------------------------------------------------------------
def fetch(url, timeout=15):
    """1回だけ取る。(status, rows, reason)。"""
    import polite_http

    try:
        r = polite_http.get(url, timeout=timeout, binary=True)
    except Exception as e:  # noqa: BLE001
        return "error", None, f"取得失敗（{type(e).__name__}）"
    if r.status_code != 200:
        return "error", None, f"取得失敗（HTTP {r.status_code}）"
    try:
        return parse(r.content.decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        return "format", None, f"形式違い（読み取りでエラー {type(e).__name__}）"


def _minutes_from_deadline(t, now):
    try:
        dl = datetime.strptime(f"{t['race_date']} {t.get('deadline')}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except (TypeError, ValueError):
        return None
    return (now - dl).total_seconds() / 60


def needs_fetch(t, prev, post_status, now):
    """このレースで今回取るか。
    t: auto_post_predict の対象 / prev: original_exhibition_log の行 / post_status: auto_post_log の status"""
    if "manshu" not in (t.get("sources") or []) or t.get("cancelled"):
        return False
    if not prev:
        return post_status in ("done", "already")   # 展示後予想が済んでから1回目
    if prev.get("status") not in RETRYABLE or int(prev.get("attempts") or 0) >= MAX_ATTEMPTS:
        return False
    after = _minutes_from_deadline(t, now)
    if after is not None and after > GIVE_UP_AFTER:
        return False
    try:
        last = datetime.fromisoformat(str(prev.get("last_tried_at")))
    except (TypeError, ValueError):
        return True
    return (now - last).total_seconds() / 60 >= RETRY_AFTER


def record(sb, t, prev, now=None):
    """1レース取って記録する。戻り値は log の status。sb は auto_post_predict._sb と同じ形。"""
    now = now or datetime.now(JST)
    url = source_url(t["jcd"], t["race_key"])
    if url is None:
        status, rows, reason = "unsupported", None, UNSUPPORTED.get(t["jcd"], UNSUPPORTED_DEFAULT)
        tried = False
    else:
        status, rows, reason = fetch(url)
        tried = True
    fetched_at = datetime.now(JST).isoformat(timespec="seconds")
    if rows:
        sb("POST", f"{TABLE}?on_conflict=race_key,lane", prefer="resolution=merge-duplicates,return=minimal",
           json=[{"race_key": t["race_key"], "lane": lane, **vals, "source_url": url, "fetched_at": fetched_at}
                 for lane, vals in sorted(rows.items())])
    attempts = int((prev or {}).get("attempts") or 0) + (1 if tried else 0)
    if status in RETRYABLE and attempts < MAX_ATTEMPTS:
        reason = f"{reason}・{RETRY_AFTER}分以上あとに1回だけ取り直す"
    sb("POST", f"{LOG_TABLE}?on_conflict=race_key", prefer="resolution=merge-duplicates,return=minimal", json={
        "race_key": t["race_key"], "race_date": t["race_date"], "jcd": t["jcd"], "race_no": t["race_no"],
        "status": status, "reason": reason, "source_url": url, "attempts": attempts,
        "last_tried_at": fetched_at if tried else (prev or {}).get("last_tried_at"),
        "updated_at": fetched_at,
    })
    return status


def run_for(sb, targets, post_log, now=None):
    """auto_post_predict.run() の最後に呼ぶ。万舟チャンスのレースのうち、取る必要があるものだけ取る。
    失敗しても例外を外に出さない（展示後予想・通知には影響させない）。戻り値は {status: 件数}。"""
    now = now or datetime.now(JST)
    manshu = [t for t in targets if "manshu" in (t.get("sources") or [])]
    if not manshu:
        return {}
    try:
        keys = ",".join(t["race_key"] for t in manshu)
        prev = {r["race_key"]: r for r in (sb("GET", f"{LOG_TABLE}?race_key=in.({keys})&select=*").json() or [])}
    except Exception as e:  # noqa: BLE001
        log(f"記録を読めませんでした（{type(e).__name__}）")
        return {}
    counts = {}
    for t in manshu:
        p = prev.get(t["race_key"])
        if not needs_fetch(t, p, (post_log.get(t["race_key"]) or {}).get("status"), now):
            continue
        try:
            st = record(sb, t, p, now)
        except Exception as e:  # noqa: BLE001
            log(f"締切 {t.get('deadline')}: 記録でエラー（{type(e).__name__}）")
            st = "error"
        counts[st] = counts.get(st, 0) + 1
    if counts:
        log("オリジナル展示: " + "・".join(f"{k} {v}" for k, v in counts.items()))
    return counts
