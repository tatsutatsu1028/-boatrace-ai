"""
万舟チャンスと厳選10のレースだけ、各場の公式サイトのオリジナル展示（一周・まわり足・直線）を取って記録する。

  - 対象は当日の万舟チャンス（manshu_chance_picks）と展示前厳選10（pre_exhibition_picks）のレース。
    両方に入っているレースは1レース1件（auto_post_predict.merge_targets）なので、取るのも1回。
    どちらの対象として取ったかは log の target_kind（manshu / pick / both）に残す
  - 展示後の自動予想（auto_post_predict.py）の run() の最後に、そのレースの展示後予想が済んだら
    （auto_post_log が done / already）1回取る。予想・頭本命・買い目・通知には使わない（記録のみ）
  - 1回の起動で通信して取るのは MAX_PER_RUN レースまで（締切前のレースを先に。残りは次の起動）。
    対象外の場（通信しない）は数えず、毎回すべて記録する
  - 1レースにつき取得は1回。まだ出ていなければ（未公開）、次の起動（RETRY_AFTER 分以上あと）に1回だけ取り直す。
    通信の失敗も同じく1回だけ取り直す。形式違い・対象外の場は取り直さない
  - 文字（HTML の表・XML）で載せている場を対象にする（VENUES）。確かめて載せていないと分かった場（UNSUPPORTED）は
    通信せず「対象外」と理由を残す。どちらにも無い未確認の場は、公式サイトのレース情報の枠を辿って試し（probe）、
    取れなかったら理由を残す（attempts=0 で終わらせない）
  - 前に「対象外（未確認の場）」で記録したレースも、その場に対応したあとの起動で取り直す
  - 通信は polite_http.get / post（開始間隔 3.4 秒以上・メンテナンス時間は待つ）。URL は各場のページ自身が
    読み込むものだけを使う（推測した URL は使わない）

保存先（supabase/migrations/20261110000000_original_exhibition.sql）
  original_exhibition     : 1艇1行（race_key, lane, original_lap, original_turn, original_straight, source_url, fetched_at）
  original_exhibition_log : 1レース1行。target_kind（manshu / pick / both。20261113000000_original_exhibition_pick.sql）。status = ok / unpublished（未公開）/ format（形式違い）/ error（取得失敗）
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
MAX_PER_RUN = 6           # 1回の起動で通信して取りに行くレースの上限（3.4 秒間隔 × 6 ≒ 20 秒強。宮島は応答が大きい。
                          # 対象外の場は通信しないので数えない）
SOURCES = ("manshu", "pre")   # 万舟チャンス・展示前厳選10

# 各場の公式サイトのオリジナル展示。どれもページ自身が読み込む URL（タブ・リンク・JavaScript が組み立てるもの）を
# そのまま使う。{day} = YYYYMMDD, {race} = レース番号, {rr} = 2桁のレース番号
#   url      : 取る URL
#   post     : POST で送る値（無ければ GET）。宮島はトップページが POST で全タブ分をまとめて読み込む
#   section  : 応答を "####" で区切ったときのオリジナル展示の位置（宮島の list_start_02）
#   labels   : 見出し（住之江は一周・まわり足だけで直線なし）
#   trailing : 値の後ろに続く列の数（福岡は展示評価、平和島は展示タイム・体重・部品交換）
#   race_mark: 応答の中にこのレースだと分かる文字（宮島は日付・レースを取り違えていないか確かめる）
#   ranges   : 値の範囲を場ごとに変えるもの（尼崎・徳山のまわり足は計測区間が長く 11 秒台）
#   plain_lane: 枠番のセルに waku などの印が無い場（丸亀。行の最初のセルが 1〜6 なら枠番とみなす）
#   cut      : 応答の中のオリジナル展示の部分（開始の印, 終わりの印）。丸亀は1ページに出走表・予想などのスライドが並ぶ
#   page_mark: 応答（タグを消す前）の中にこのレースだと分かる文字。丸亀は URL に日付が無いので、ページ内の
#              展示リプレイの raceDate・raceNumber で日付とレースを確かめる
#   xml      : XML で載せている場（戸田。ページが ./xml/kaisai/{日付}/race_table_original_{RR}.xml を読み込む。
#              まだ無いとき（404）はページも「表示するデータがありません」と出すので未公開として扱う）
_XOOPS_C = "/modules/yosou/group-cyokuzen.php?day={day}&race={race}&kind=2&if=1"
VENUES = {
    "02": {"url": "https://www.boatrace-toda.jp/xml/kaisai/{day}/race_table_original_{rr}.xml", "xml": True},
    "04": {"url": "https://www.heiwajima.gr.jp/asp/kyogi/04/pc/yoso05{rr}.htm", "trailing": 3},
    "05": {"url": "https://www.boatrace-tamagawa.com/modules/yosou/oriten.php?day={day}&race={race}&jo=05&if=1"},
    "08": {"url": "https://www.boatrace-tokoname.jp" + _XOOPS_C},
    "09": {"url": "https://www.boatrace-tsu.com/modules/yosou/group-tenji.php?type=group-tenji&day={day}&race={race}"
                  "&kind=1&if=1"},
    "10": {"url": "https://www.boatrace-mikuni.jp" + _XOOPS_C},
    "11": {"url": "https://www.boatrace-biwako.jp/modules/yosou/cyokuzen.php?day={day}&race={race}&if=1&kind=2"},
    "12": {"url": "https://www.boatrace-suminoe.jp/asp/kyogi/12/pc/st02{rr}.htm", "labels": ("一周", "まわり足")},
    # 尼崎: トップの「直前情報」タブ → 「オリジナル展示データ」（kind=2）。一周・まわり足だけ（直線なし）
    "13": {"url": "https://www.boatrace-amagasaki.jp" + _XOOPS_C, "labels": ("一周", "まわり足"),
           "ranges": {"original_turn": (8.0, 15.0)}},
    "14": {"url": "https://www.n14.jp" + _XOOPS_C},
    # 丸亀: トップの iframe（出走表）から辿る「予想」ページ（yoso05RR.htm）の「オリジナル展示データ」の表
    "15": {"url": "https://www.marugameboat.jp/asp/kyogi/15/pc/yoso05{rr}.htm", "plain_lane": True,
           "cut": ('id="yoso03_03"', 'class="swiper-slide"'),
           "page_mark": r"raceDate={day}&(?:amp;)?raceNumber={race}(?![0-9])"},
    "16": {"url": "https://www.kojimaboat.jp/asp/kyogi/16/pc/st02{rr}.htm"},
    # 徳山: トップの「展示」タブ（tenji.php）。一周・まわり足の後ろに前走成績の4列
    "18": {"url": "https://www.boatrace-tokuyama.jp/modules/yosou/tenji.php?day={day}&race={race}&if=1",
           "labels": ("一周", "まわり足"), "trailing": 4, "ranges": {"original_turn": (8.0, 15.0)}},
    "17": {"url": "https://www.boatrace-miyajima.com/race_common/require/kaisai_reload.php",
           "post": {"race": "{race}", "date": "{day}"}, "section": 7, "race_mark": r"(?<![0-9]){race}\s+R"},
    "19": {"url": "https://www.boatrace-shimonoseki.jp" + _XOOPS_C},
    # 若松: 「直前情報」タブ → 「オリジナル展示データ」（kind=2）
    "20": {"url": "https://www.wmb.jp" + _XOOPS_C},
    "21": {"url": "https://www.boatrace-ashiya.com" + _XOOPS_C},
    "22": {"url": "https://www.boatrace-fukuoka.com/modules/yosou/tenji_info.php?day={day}&race={race}&if=1&nowmode=1",
           "trailing": 1},
    # 唐津: 「直前情報」タブ → 「展示情報」（kind=2）。一周・まわり足・直線の後ろに展示評価の1列
    "23": {"url": "https://www.boatrace-karatsu.jp" + _XOOPS_C, "trailing": 1},
}

# 確かめて対象外とした場と理由（通信しない）
UNSUPPORTED = {
    "01": "対象外（一周ではなく半周タイムの公開で、項目が違う）",
    "03": "対象外（オリジナル展示を公開していない）",
    "07": "対象外（開催中の公式サイトのレース情報にオリジナル展示が見当たらない）",
    "24": "対象外（オリジナル展示を公開していない。公式サイトの直前情報・スタート展示情報に無い）",
}

# VENUES にも UNSUPPORTED にも無い場（未確認の場）は、公式サイトのトップページが読み込むレース情報の枠
# （/modules/yosou/<タブ>.php。尼崎・若松・唐津・徳山などと同じ作り）を辿って試し、取れなければ理由を残す
OFFICIAL_SITES = {"06": "https://www.boatrace-hamanako.jp"}
PROBE_RANGES = {"original_turn": (3.0, 15.0)}   # 計測区間が分からないので、まわり足は広め

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


def _lane(attrs, inner, plain=False):
    """枠番のセルなら 1〜6、違えば None（class に waku / tei、または中に wakuban の印。plain なら印は問わない）。"""
    if not plain and not re.search(r"waku|tei", attrs) and "wakuban" not in inner:
        return None
    t = _text(inner)
    return int(t) if t.isdigit() and 1 <= int(t) <= 6 else None


# 値の欄に入る「データが無い」の文言（津は計測できなかった艇の欄に「表示するデータがありません」が入る）。
# この欄がある艇の行は読み飛ばし（値は None）、数値のある艇だけ取る
NO_DATA_CELL = re.compile(r"データ|ございません|ありません")


def _squash(s):
    return re.sub(r"\s+", "", _text(s))


def _not_yet(page):
    t = _squash(page)
    return next((w for w in NOT_YET if w in t), None)


def _check_ranges(out, ranges=None):
    rng = {**RANGES, **(ranges or {})}
    for r in out.values():
        for col, v in r.items():
            lo, hi = rng[col]
            if v is not None and not lo <= v <= hi:
                return f"形式違い（{col} の値が範囲外: {v}）"
    return None


def parse(page, labels=DEFAULT_LABELS, trailing=0, ranges=None, plain_lane=False):
    """オリジナル展示の表 → ("ok", {lane: {original_lap, original_turn, original_straight}}, None)
    または ("unpublished" / "format", None, 理由)。

    表の作り（各場共通）: 枠番のセル（行の1つ目か2つ目。徳山は1つ目が気配）で始まる行の、後ろから trailing 列を
    除いた最後の len(labels) 個のセルが見出しの順（場によって順番が違ってもよい）。値が「-」「--.--」の艇は
    計測なし（None）。見出しに無い項目も None。値の欄に「表示するデータがありません」などの文言が入った艇は
    その行を読み飛ばす（None）。"""
    if not page:
        return "format", None, "空のページ"
    for w in NOT_YET:   # ページ全体が「まだ無い」の案内（これまでと同じ）
        if w in page:
            return "unpublished", None, f"未公開（{w}）"
    rows, first, skipped = {}, None, []
    n = len(labels)
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        tds = re.findall(r"<td([^>]*)>(.*?)</td>", m.group(1), re.S)
        lane, at = None, 0
        for i, (a, v) in enumerate(tds[:2]):
            lane = _lane(a, v, plain_lane and i == 0)
            if lane is not None:
                at = i
                break
        if lane is None or lane in rows:
            continue
        cells = tds[at + 1:]
        msg = [(a, v) for a, v in cells[:len(cells) - trailing] if not _num_like(v) and NO_DATA_CELL.search(_text(v))]
        if msg:
            span = max(int((re.search(r"rowspan=['\"]?(\d+)", a) or [0, 1])[1]) for a, _ in msg)
            if span > 2:   # 1つの欄が何艇分もまたがる（若松・津）→ このレースはまだ値が無い
                return "unpublished", None, f"未公開（{_squash(msg[0][1])[:20]}）"
            first = m.start() if first is None else first
            rows[lane] = [None] * n   # この艇だけ値が無い → 行を読み飛ばす
            skipped.append(lane)
            continue
        if len(tds) < at + 2 + n + trailing:
            continue
        first = m.start() if first is None else first
        vals = tds[len(tds) - trailing - n: len(tds) - trailing]
        try:
            rows[lane] = [_num(v) for _, v in vals]
        except ValueError as e:
            return "format", None, f"形式違い（{e}）"
    if first is None:
        w = _not_yet(page)
        if w:
            return "unpublished", None, f"未公開（{w}）"
        return "format", None, "形式違い（枠の行が見つからない）"
    head = re.sub(r"<br\s*/?>", "", page[:first])   # 「まわ<br>り足」（徳山）
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
        w = _not_yet(page)
        return "unpublished", None, f"未公開（{w}）" if w else "未公開（値が入っていない）"
    bad = _check_ranges(out, ranges)
    if bad:
        return "format", None, bad
    if skipped:
        log(f"データの無い艇を読み飛ばした: {skipped}")
    return "ok", out, None


def _num_like(v):
    try:
        _num(v)
        return True
    except ValueError:
        return False


XML_TAGS = {"rnd": "original_lap", "cnr": "original_turn", "str": "original_straight"}


def parse_xml(page):
    """戸田の XML（<record> ごとに teiban・rnd（一周）・cnr（まわり足）・str（直線））→ parse と同じ形。"""
    import xml.etree.ElementTree as ET

    if not page or not page.strip():
        return "unpublished", None, "未公開（空のデータ）"
    try:
        root = ET.fromstring(page.encode("utf-8"))
    except ET.ParseError:
        return "format", None, "形式違い（XML として読めない）"
    out = {}
    for rec in root.iter("record"):
        lane = (rec.findtext("teiban") or "").strip()
        if not lane.isdigit() or not 1 <= int(lane) <= 6:
            continue
        try:
            out[int(lane)] = {col: _num(rec.findtext(tag) or "") for tag, col in XML_TAGS.items()}
        except ValueError as e:
            return "format", None, f"形式違い（{e}）"
    if sorted(out) != [1, 2, 3, 4, 5, 6]:
        return "format", None, f"形式違い（艇の記録が6つそろわない: {len(out)}）"
    if all(v is None for r in out.values() for v in r.values()):
        return "unpublished", None, "未公開（値が入っていない）"
    bad = _check_ranges(out)
    return ("format", None, bad) if bad else ("ok", out, None)


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
    if r.status_code == 404 and v.get("xml"):
        return "unpublished", None, "未公開（データのファイルがまだ無い）"
    if r.status_code != 200:
        return "error", None, f"取得失敗（HTTP {r.status_code}）"
    try:
        page = r.content.decode("utf-8", errors="replace")
        if v.get("xml"):
            return parse_xml(page)
        if v.get("page_mark") and not re.search(_fill(v["page_mark"], race_key), page):
            return "format", None, "形式違い（別の日・別のレースのページ）"
        if v.get("cut"):
            start, end = v["cut"]
            i = page.find(start)
            if i < 0:
                return "format", None, "形式違い（オリジナル展示の部分が見つからない）"
            j = page.find(end, i + len(start))
            page = page[i: j if j > 0 else None]
        if v.get("section") is not None:
            parts = page.split("####")
            if len(parts) <= v["section"]:
                return "format", None, "形式違い（応答の区切りが足りない）"
            page = parts[v["section"]]
        if v.get("race_mark") and not re.search(_fill(v["race_mark"], race_key), _text(page)):
            return "format", None, "形式違い（別のレースの表）"
        return parse(page, v.get("labels", DEFAULT_LABELS), v.get("trailing", 0), v.get("ranges"),
                     v.get("plain_lane", False))
    except Exception as e:  # noqa: BLE001
        return "format", None, f"形式違い（読み取りでエラー {type(e).__name__}）"


def _get_text(url, timeout):
    import polite_http

    r = polite_http.get(url, timeout=timeout, binary=True)
    return r.status_code, r.content.decode("utf-8", errors="replace")


def probe(jcd, race_key, timeout=15):
    """未確認の場で試す。(status, rows, reason, url)。

    公式サイトのトップページが iframe に読み込むレース情報（'/modules/yosou/' + タブ名 + '.php?day=..&race=..&if=1'）の
    直前情報・展示のタブを開き、「一周」の表がそのページに無ければ、ページの中のサブタブ（data-kind）のうち
    「オリジナル展示」「展示情報」のものを開く。どれもページ自身が読み込む URL だけ。取れなかったら理由を返す。"""
    jcd = str(jcd).zfill(2)
    base = OFFICIAL_SITES.get(jcd)
    if not base:
        return "format", None, "未確認の場（公式サイトが未登録のため試せない）", None
    race = int(race_key.split("_")[2])
    day = race_key[:8]
    url = base + "/"
    try:
        code, top = _get_text(url, timeout)
        if code != 200:
            return "error", None, f"未確認の場で試した: 取得失敗（トップ HTTP {code}）", url
        if not re.search(r"typelink\s*=\s*'/modules/yosou/'", top):
            return "format", None, "未確認の場で試した: トップページにレース情報の枠が無い", url
        tabs = [t for t in dict.fromkeys(re.findall(r'data-tab="([\w-]+)"', top)) if re.search(r"cyokuzen|tenji", t)]
        if not tabs:
            return "format", None, "未確認の場で試した: 直前情報・展示のタブが無い", url
        last = ("format", None, "未確認の場で試した: オリジナル展示の表が見つからない", url)
        for tab in tabs:
            url = f"{base}/modules/yosou/{tab}.php?day={day}&race={race}&if=1"
            code, page = _get_text(url, timeout)
            if code != 200:
                last = ("error", None, f"未確認の場で試した: 取得失敗（HTTP {code}）", url)
                continue
            if "一周" not in _squash(page):
                kinds = [k for k, name in re.findall(r'data-kind="(\d+)"[^>]*>(?:\s*<[^>]+>)*\s*([^<]*)', page)
                         if re.search(r"オリジナル展示|展示情報", name)]
                if not kinds:
                    continue
                url = f"{base}/modules/yosou/{tab}.php?day={day}&race={race}&kind={kinds[0]}&if=1"
                code, page = _get_text(url, timeout)
                if code != 200:
                    last = ("error", None, f"未確認の場で試した: 取得失敗（HTTP {code}）", url)
                    continue
            labels = tuple(x for x in DEFAULT_LABELS if x in _squash(page)) or DEFAULT_LABELS
            # 値の後ろの列の数が分からないので、範囲に収まる読み方を探す（取れる読み方 → 未公開 → それ以外の順）
            tries = [(tr, parse(page, labels, tr, PROBE_RANGES)) for tr in range(0, 5)]
            trailing, (st, rows, why) = min(tries, key=lambda x: {"ok": 0, "unpublished": 1}.get(x[1][0], 2))
            if st == "ok":
                log(f"未確認の場 {jcd} で取れた（VENUES に追加する候補）: {url}")
                return st, rows, f"未確認の場で試して取れた（labels={'・'.join(labels)}, trailing={trailing}）", url
            last = (st, None, f"未確認の場で試した: {why}", url)
        return last
    except Exception as e:  # noqa: BLE001
        return "error", None, f"未確認の場で試した: 取得失敗（{type(e).__name__}）", url


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


def target_kind(t):
    """どちらの対象か: manshu（万舟チャンスだけ）/ pick（厳選10だけ）/ both（両方）。どちらでもなければ None。"""
    src = set(t.get("sources") or [])
    if {"manshu", "pre"} <= src:
        return "both"
    return "manshu" if "manshu" in src else "pick" if "pre" in src else None


def needs_fetch(t, prev, post_status, now):
    """このレースで今回取るか。
    t: auto_post_predict の対象 / prev: original_exhibition_log の行 / post_status: auto_post_log の status"""
    if target_kind(t) is None or t.get("cancelled"):
        return False
    if not prev:
        return post_status in ("done", "already")   # 展示後予想が済んでから1回目
    if prev.get("status") == "unsupported" and str(t.get("jcd")).zfill(2) not in UNSUPPORTED:
        # 前は「対象外（未確認の場）」で通信しなかったが、その後に対応した場・試すことにした場。公開済みの値は
        # レース後も同じページに載っているので、締切を過ぎていても（対象はその日のレースだけ）1回取る
        return True
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
    jcd = str(t["jcd"]).zfill(2)
    url = source_url(jcd, t["race_key"])
    if jcd in UNSUPPORTED:
        status, rows, reason = "unsupported", None, UNSUPPORTED[jcd]
        tried = False
    elif url is None:
        # 未確認の場: 対象外で終わらせず試す。取れなかったら理由を残す（取り直しは他の場と同じ）
        status, rows, reason, url = probe(jcd, t["race_key"])
        tried = True
    else:
        status, rows, reason = fetch(jcd, t["race_key"])
        tried = True
    fetched = datetime.now(JST)
    fetched_at = fetched.isoformat(timespec="seconds")
    if rows:
        sb("POST", f"{TABLE}?on_conflict=race_key,lane", prefer="resolution=merge-duplicates,return=minimal",
           json=[{"race_key": t["race_key"], "lane": lane, **vals, "source_url": url, "fetched_at": fetched_at}
                 for lane, vals in sorted(rows.items())])
    base = 0 if (prev or {}).get("status") == "unsupported" else int((prev or {}).get("attempts") or 0)
    attempts = base + (1 if tried else 0)
    if status in RETRYABLE and attempts < MAX_ATTEMPTS:
        reason = f"{reason}・{RETRY_AFTER}分以上あとに1回だけ取り直す"
    sb("POST", f"{LOG_TABLE}?on_conflict=race_key", prefer="resolution=merge-duplicates,return=minimal", json={
        "race_key": t["race_key"], "race_date": t["race_date"], "jcd": t["jcd"], "race_no": t["race_no"],
        "target_kind": target_kind(t), "status": status, "reason": reason, "source_url": url, "attempts": attempts,
        "last_tried_at": fetched_at if tried else (prev or {}).get("last_tried_at"),
        "deadline": t.get("deadline"),
        # 値が取れたときだけ判定（取れていなければ null）
        "before_deadline": before_deadline(t, fetched) if rows else None,
        "updated_at": fetched_at,
    })
    return status


def run_for(sb, targets, post_log, now=None):
    """auto_post_predict.run() の最後に呼ぶ。万舟チャンス・厳選10のレースのうち、取る必要があるものだけ取る
    （1回に MAX_PER_RUN レースまで。締切がまだ先のレースを先に）。
    失敗しても例外を外に出さない（展示後予想・通知には影響させない）。戻り値は {status: 件数}。"""
    now = now or datetime.now(JST)
    cands = [t for t in targets if target_kind(t)]
    if not cands:
        return {}
    try:
        keys = ",".join(t["race_key"] for t in cands)
        prev = {r["race_key"]: r for r in (sb("GET", f"{LOG_TABLE}?race_key=in.({keys})&select=*").json() or [])}
    except Exception as e:  # noqa: BLE001
        log(f"記録を読めませんでした（{type(e).__name__}）")
        return {}
    due = [t for t in cands
           if needs_fetch(t, prev.get(t["race_key"]), (post_log.get(t["race_key"]) or {}).get("status"), now)]

    def order(t):
        after = _minutes_from_deadline(t, now)
        # 締切前（まだ間に合う）を先に、その中は締切の早い順。その後に締切後（締切から間もない順。
        # 取り直しは締切から GIVE_UP_AFTER 分まで）、締切不明は最後
        # （after は締切からの分。締切前は負で、締切が近いほど 0 に近い）
        return (0, -after) if after is not None and after < 0 else (1, after if after is not None else 1e9)

    due.sort(key=order)
    # 上限は通信するレースだけに数える（対象外の場は通信せず理由を書くだけなので、毎回すべて記録する）
    offline = [t for t in due if str(t["jcd"]).zfill(2) in UNSUPPORTED]
    online = [t for t in due if str(t["jcd"]).zfill(2) not in UNSUPPORTED]
    if len(online) > MAX_PER_RUN:
        log(f"オリジナル展示: {len(online) - MAX_PER_RUN}レースは次の起動で取る")
    counts = {}
    for t in offline + online[:MAX_PER_RUN]:
        p = prev.get(t["race_key"])
        try:
            st = record(sb, t, p, now)
        except Exception as e:  # noqa: BLE001
            log(f"締切 {t.get('deadline')}: 記録でエラー（{type(e).__name__}）")
            st = "error"
        counts[st] = counts.get(st, 0) + 1
    if counts:
        log("オリジナル展示: " + "・".join(f"{k} {v}" for k, v in counts.items()))
    return counts
