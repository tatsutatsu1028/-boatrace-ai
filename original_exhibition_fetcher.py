"""
万舟チャンスのレースだけ、各場の公式サイトのオリジナル展示（一周・まわり足・直線）を取って記録する。

  - 対象は manshu_chance_picks に入っている当日の万舟チャンスのレースだけ。
    展示後の自動予想（auto_post_predict.py）の run() の最後に、そのレースの展示後予想が済んだら
    （auto_post_log が done / already）1回取る。予想・頭本命・買い目・通知には使わない（記録のみ）
  - 1レースにつき取得は1回。まだ出ていなければ（未公開）、次の起動（RETRY_AFTER 分以上あと）に1回だけ取り直す。
    通信の失敗も同じく1回だけ取り直す。形式違い・対象外の場は取り直さない
  - 文字（HTML の表）で載せている場だけを対象にする（VENUES）。それ以外の場は通信せず「対象外」と理由を残す
  - 通信は polite_http.get / post（開始間隔 3.4 秒以上・メンテナンス時間は待つ）。URL は各場のページ自身が
    読み込むものだけを使う（推測した URL は使わない）

保存先（supabase/migrations/20261110000000_original_exhibition.sql）
  original_exhibition     : 1艇1行（race_key, lane, original_lap, original_turn, original_straight, source_url, fetched_at）
  original_exhibition_log : 1レース1行。status = ok / unpublished（未公開）/ format（形式違い）/ error（取得失敗）
                            / unsupported（対象外の場）と理由・回数。締切（deadline）と、値を締切前に取れたか
                            （before_deadline。取れなかったレース・締切が読めないレースは null）
                            （20261111000000_original_exhibition_before_deadline.sql）
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

# 各場の公式サイトのオリジナル展示。どれもページ自身が読み込む URL（タブ・リンク・JavaScript が組み立てるもの）を
# そのまま使う。{day} = YYYYMMDD, {race} = レース番号, {rr} = 2桁のレース番号
#   url      : 取る URL
#   post     : POST で送る値（無ければ GET）。宮島はトップページが POST で全タブ分をまとめて読み込む
#   section  : 応答を "####" で区切ったときのオリジナル展示の位置（宮島の list_start_02）
#   labels   : 見出し（住之江は一周・まわり足だけで直線なし）
#   trailing : 値の後ろに続く列の数（福岡は展示評価、平和島は展示タイム・体重・部品交換）
#   race_mark: 応答の中にこのレースだと分かる文字（宮島は日付・レースを取り違えていないか確かめる）
_XOOPS_C = "/modules/yosou/group-cyokuzen.php?day={day}&race={race}&kind=2&if=1"
VENUES = {
    "04": {"url": "https://www.heiwajima.gr.jp/asp/kyogi/04/pc/yoso05{rr}.htm", "trailing": 3},
    "05": {"url": "https://www.boatrace-tamagawa.com/modules/yosou/oriten.php?day={day}&race={race}&jo=05&if=1"},
    "08": {"url": "https://www.boatrace-tokoname.jp" + _XOOPS_C},
    "09": {"url": "https://www.boatrace-tsu.com/modules/yosou/group-tenji.php?type=group-tenji&day={day}&race={race}"
                  "&kind=1&if=1"},
    "10": {"url": "https://www.boatrace-mikuni.jp" + _XOOPS_C},
    "11": {"url": "https://www.boatrace-biwako.jp/modules/yosou/cyokuzen.php?day={day}&race={race}&if=1&kind=2"},
    "12": {"url": "https://www.boatrace-suminoe.jp/asp/kyogi/12/pc/st02{rr}.htm", "labels": ("一周", "まわり足")},
    "14": {"url": "https://www.n14.jp" + _XOOPS_C},
    "17": {"url": "https://www.boatrace-miyajima.com/race_common/require/kaisai_reload.php",
           "post": {"race": "{race}", "date": "{day}"}, "section": 7, "race_mark": r"(?<![0-9]){race}\s+R"},
    "19": {"url": "https://www.boatrace-shimonoseki.jp" + _XOOPS_C},
    "21": {"url": "https://www.boatrace-ashiya.com" + _XOOPS_C},
    "22": {"url": "https://www.boatrace-fukuoka.com/modules/yosou/tenji_info.php?day={day}&race={race}&if=1&nowmode=1",
           "trailing": 1},
}

# 対象外の場の理由（無い場は UNSUPPORTED_DEFAULT）
UNSUPPORTED = {
    "01": "対象外（一周ではなく半周タイムの公開で、項目が違う）",
    "03": "対象外（オリジナル展示を公開していない）",
    "07": "対象外（開催中の公式サイトのレース情報にオリジナル展示が見当たらない）",
}
UNSUPPORTED_DEFAULT = "対象外（未確認の場。次の開催時に確認して追加する）"

LABELS = {"一周": "original_lap", "まわり足": "original_turn", "直線": "original_straight"}
DEFAULT_LABELS = ("一周", "まわり足", "直線")
# 値の範囲（外れたら形式違いとみなす。単位は秒。場によって計測区間が違うので広め）
RANGES = {"original_lap": (30.0, 45.0), "original_turn": (3.0, 10.0), "original_straight": (5.0, 10.0)}
NOT_YET = ("ただいまデータはございません", "しばらくお待ちください", "データがありません")


def log(*a):
    print("[ORIGINAL_EXHIBITION]", *a, flush=True)


def _fill(v, race_key):
    race = int(race_key.split("_")[2])
    return v.format(day=race_key[:8], race=race, rr=f"{race:02d}")


def source_url(jcd, race_key):
    v = VENUES.get(str(jcd).zfill(2))
    return _fill(v["url"], race_key) if v else None


# ---------------------------------------------------------------
# 表を読む（通信しない）
# ---------------------------------------------------------------
def _text(s):
    return _html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("　", " ").strip()


def _num(s):
    s = _text(s).replace(" ", "")
    if not s or set(s) <= set("-－‐ー.．"):
        return None
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"数値でない値: {s[:10]}") from None


def _lane(attrs, inner):
    """枠番のセルなら 1〜6、違えば None（class に waku / tei、または中に wakuban の印）。"""
    if not re.search(r"waku|tei", attrs) and "wakuban" not in inner:
        return None
    t = _text(inner)
    return int(t) if t.isdigit() and 1 <= int(t) <= 6 else None


def parse(page, labels=DEFAULT_LABELS, trailing=0):
    """オリジナル展示の表 → ("ok", {lane: {original_lap, original_turn, original_straight}}, None)
    または ("unpublished" / "format", None, 理由)。

    表の作り（各場共通）: 枠番のセルで始まる行の、後ろから trailing 列を除いた最後の len(labels) 個のセルが
    見出しの順（場によって順番が違ってもよい）。値が「-」「--.--」の艇は計測なし（None）。見出しに無い項目も None。"""
    if not page:
        return "format", None, "空のページ"
    for w in NOT_YET:
        if w in page:
            return "unpublished", None, f"未公開（{w}）"
    rows, first = {}, None
    n = len(labels)
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        tds = re.findall(r"<td([^>]*)>(.*?)</td>", m.group(1), re.S)
        lane = _lane(*tds[0]) if tds else None
        if lane is None or len(tds) < 2 + n + trailing:
            continue
        first = m.start() if first is None else first
        cells = tds[len(tds) - trailing - n: len(tds) - trailing]
        try:
            rows[lane] = [_num(v) for _, v in cells]
        except ValueError as e:
            return "format", None, f"形式違い（{e}）"
    if first is None:
        return "format", None, "形式違い（枠の行が見つからない）"
    head = page[:first]
    pos = {}
    for label in labels:
        found = [x.start() for x in re.finditer(label, head)]
        if not found:
            return "format", None, f"形式違い（見出し「{label}」が無い）"
        pos[LABELS[label]] = found[-1]   # 表の見出しのもの（タブ名などより後ろ）
    order = sorted(pos, key=pos.get)
    if sorted(rows) != [1, 2, 3, 4, 5, 6]:
        return "format", None, f"形式違い（枠の行が6つそろわない: {len(rows)}）"
    out = {lane: {**{c: None for c in LABELS.values()}, **dict(zip(order, vals))} for lane, vals in rows.items()}
    if all(v is None for r in out.values() for v in r.values()):
        return "unpublished", None, "未公開（値が入っていない）"
    for r in out.values():
        for col, v in r.items():
            lo, hi = RANGES[col]
            if v is not None and not lo <= v <= hi:
                return "format", None, f"形式違い（{col} の値が範囲外: {v}）"
    return "ok", out, None


# ---------------------------------------------------------------
# 取る・記録する
# ---------------------------------------------------------------
def fetch(jcd, race_key, timeout=15):
    """1回だけ取る。(status, rows, reason)。"""
    import polite_http

    v = VENUES[str(jcd).zfill(2)]
    url = _fill(v["url"], race_key)
    try:
        if v.get("post"):
            data = {k: _fill(x, race_key) for k, x in v["post"].items()}
            r = polite_http.post(url, data, timeout=timeout, referer=url.split("/race_common/")[0] + "/")
        else:
            r = polite_http.get(url, timeout=timeout, binary=True)
    except Exception as e:  # noqa: BLE001
        return "error", None, f"取得失敗（{type(e).__name__}）"
    if r.status_code != 200:
        return "error", None, f"取得失敗（HTTP {r.status_code}）"
    try:
        page = r.content.decode("utf-8", errors="replace")
        if v.get("section") is not None:
            parts = page.split("####")
            if len(parts) <= v["section"]:
                return "format", None, "形式違い（応答の区切りが足りない）"
            page = parts[v["section"]]
        if v.get("race_mark") and not re.search(_fill(v["race_mark"], race_key), _text(page)):
            return "format", None, "形式違い（別のレースの表）"
        return parse(page, v.get("labels", DEFAULT_LABELS), v.get("trailing", 0))
    except Exception as e:  # noqa: BLE001
        return "format", None, f"形式違い（読み取りでエラー {type(e).__name__}）"


def _minutes_from_deadline(t, now):
    try:
        dl = datetime.strptime(f"{t['race_date']} {t.get('deadline')}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except (TypeError, ValueError):
        return None
    return (now - dl).total_seconds() / 60


def before_deadline(t, fetched):
    """値を締切前に取れたか（True / False）。締切が読めなければ None。fetched は取った時刻（JST の datetime）。"""
    after = _minutes_from_deadline(t, fetched)
    return None if after is None else after < 0


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
        status, rows, reason = fetch(t["jcd"], t["race_key"])
        tried = True
    fetched = datetime.now(JST)
    fetched_at = fetched.isoformat(timespec="seconds")
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
        "deadline": t.get("deadline"),
        # 値が取れたときだけ判定（取れていなければ null）
        "before_deadline": before_deadline(t, fetched) if rows else None,
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
