from __future__ import annotations

import html as html_lib
import io
import re
import unicodedata

import pandas as pd
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

UA = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
    )
}

GAMAGORI_COMMENT_ALL = (
    "https://www.gamagori-kyotei.com/asp/gamagori/sp/kyogi/"
    "kyogihtml/comment_all/comment_all07.htm"
)

GAMAGORI_LINE_VOOM = (
    "https://linevoom.line.me/user/"
    "_dUn_YlpPps1E9pNPqbT9isaymloPeRIYud9x4l4"
)


def _norm(x):
    s = "" if x is None else str(x)
    s = unicodedata.normalize("NFKC", s)
    s = html_lib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _compact_name(x):
    return re.sub(r"\s+", "", _norm(x))


def _get(url):
    r = requests.get(
        url,
        headers={**UA, "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.6"},
        timeout=25,
        allow_redirects=True,
    )
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _looks_like_no_data_page(html):
    """
    開催なし・未掲載のページを早期に検知する。
    レース場サイトはCMS共通のため、この文言もほぼ共通。
    """
    t = _norm(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    return any(
        w in t
        for w in (
            "ただいまデータはございません",
            "次節開催までしばらくお待ちください",
            "本日は非開催",
        )
    )


def _empty(race):
    out = race[["lane", "racer_name"]].copy()
    out["venue_comment"] = ""
    out["venue_comment_source"] = ""
    out["venue_comment_url"] = ""
    return out


def _name_regex(name):
    n = _compact_name(name)
    if not n:
        return None
    return r"\s*".join(re.escape(ch) for ch in n)


def _visible_text_and_scripts(html):
    soup = BeautifulSoup(html, "lxml")
    visible = _norm(soup.get_text(" ", strip=True))
    scripts = []
    for sc in soup.find_all("script"):
        txt = sc.string or sc.get_text(" ", strip=True)
        if txt:
            scripts.append(txt)
    raw = "\n".join(scripts)
    try:
        raw = bytes(raw, "utf-8").decode("unicode_escape")
    except Exception:
        pass
    return visible, _norm(raw)


def _all_name_positions(text, race):
    found = []
    for _, row in race.iterrows():
        name = _norm(row.get("racer_name", ""))
        if not name:
            continue
        pat = _name_regex(name)
        if not pat:
            continue
        for m in re.finditer(pat, text):
            found.append((m.start(), m.end(), int(row["lane"]), name))
    return sorted(found, key=lambda x: x[0])


def _candidate_blocks(text, race, lane, name):
    text = _norm(text)
    positions = _all_name_positions(text, race)
    blocks = []
    for i, (_, end, ln, _) in enumerate(positions):
        if ln != int(lane):
            continue
        next_start = len(text)
        for j in range(i + 1, len(positions)):
            if positions[j][0] > end:
                next_start = positions[j][0]
                break
        cut = min(next_start, end + 900)
        block = _norm(text[end:cut])
        if block:
            blocks.append(block)
    return blocks


def _looks_like_motor_table_noise(s):
    s = _norm(s)
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    digits = sum(ch.isdigit() for ch in s)

    if re.search(r"\b[AB][12]\)?\s+\d+", s):
        return True
    if len(re.findall(r"\b\d{4}\b", s)) >= 2:
        return True
    if s.count("0.00") >= 2:
        return True

    table_words = ("通算", "近況", "パワー", "勝率", "2連率", "登録番号", "級別", "No.", "NO.")
    table_hits = sum(1 for w in table_words if w in s)

    return len(nums) >= 8 or digits >= 14 or table_hits >= 3


def _looks_like_natural_comment(s):
    s = _norm(s)

    if not (5 <= len(s) <= 220):
        return False
    if _looks_like_motor_table_noise(s):
        return False
    if len(re.findall(r"[一-龥々ぁ-んァ-ヶー]", s)) < 5:
        return False

    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if len(nums) >= 4:
        return False
    if re.search(r"(?:^|\s)[AB][12](?:\s|$|\))", s) and len(nums) >= 2:
        return False

    table_words = (
        "登録番号", "級別", "勝率/2連率", "勝率", "2連率",
        "通算", "近況", "モーター一覧", "コメント&モーター", "コメント＆モーター",
    )
    if sum(1 for k in table_words if k in s) >= 2:
        return False

    race_words = (
        "足", "伸び", "出足", "回り", "まわり", "乗り",
        "スタート", "ターン", "気配", "エンジン", "モーター",
        "調整", "ペラ", "行き足", "直線", "押し", "舟足",
        "走り", "水準", "違和感", "手応え", "反応",
    )
    natural_words = (
        "は", "が", "けど", "けれど", "と思う", "です", "ます",
        "ない", "いい", "良い", "悪い", "感じ", "欲しい",
        "している", "なった", "ならない", "来なかった",
        "普通", "十分", "まずまず", "上向き",
    )
    return any(k in s for k in race_words) and any(k in s for k in natural_words)


def _split_sentences(block):
    block = _norm(block)
    block = block.replace("\\n", " ").replace("\\r", " ").replace("\\/", "/")
    block = re.sub(r"(No\.?|通算|近況|パワー|勝率/2連率|登録番号|級別)", " | ", block)
    parts = re.split(r"(?<=[。！？!?])\s*|[|｜]{1,2}", block)
    return [_norm(p).strip(" \"'[]{}(),") for p in parts if _norm(p)]


def _score_comment(s):
    if not _looks_like_natural_comment(s):
        return -10_000

    score = len(re.findall(r"[一-龥々ぁ-んァ-ヶー]", s))
    for k in ("足", "伸び", "出足", "回り", "まわり足", "乗り", "スタート", "ターン", "行き足", "直線", "水準", "違和感", "良く", "悪く"):
        if k in s:
            score += 12

    score -= len(re.findall(r"\d+(?:\.\d+)?", s)) * 8
    score -= s.count("0.00") * 50
    return score


def _best_comment_for_racer(document, race, lane, name):
    candidates = []

    for block in _candidate_blocks(document, race, lane, name):
        if _looks_like_natural_comment(block):
            candidates.append((_score_comment(block), block))

        for p in _split_sentences(block):
            if _looks_like_natural_comment(p):
                candidates.append((_score_comment(p), p))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _extract_comments_from_document(html, race):
    out = {}
    visible, scripts = _visible_text_and_scripts(html)

    for _, row in race.iterrows():
        lane = int(row["lane"])
        name = _norm(row.get("racer_name", ""))
        if not name:
            continue

        best = ""
        for doc in (visible, scripts):
            c = _best_comment_for_racer(doc, race, lane, name)
            if c and _score_comment(c) > _score_comment(best):
                best = c

        if best:
            out[lane] = best

    return out


def _fetch_gamagori_pdf_comments(date_yyyymmdd, rno, race):
    """
    ガマスポPDFから該当レースの6艇コメントを取得する。

    PDFのレース展望ブロックは概ね次の並びで構成される
    （pypdf等の抽出結果は改行位置がライブラリ依存でぶれるため、
    空白類を単一スペースへ正規化した「1行化テキスト」に対して
    パターンマッチする）。

        …レース展望の地の文…
        [進入コース] [.ST] [枠番] [苗字] [コメント文]  ×6艇分
    """
    out = _empty(race)

    print("[GAMAGORI PDF] CODE_VERSION=v4-whitespace-fix-20260817")

    pdf_url = (
        "https://www.gamagori-kyotei.com/asp/gamagori/kyogi/"
        "kyogihtml/pdf_A3/"
        f"pdf_A3{date_yyyymmdd}07.pdf"
    )

    try:
        r = requests.get(pdf_url, headers=UA, timeout=25)
        r.raise_for_status()

        reader = PdfReader(io.BytesIO(r.content))
        raw_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        raw_text = unicodedata.normalize("NFKC", raw_text)

        print("[GAMAGORI PDF]", pdf_url, "pages=", len(reader.pages), "text_len=", len(raw_text))

    except Exception as e:
        print("[GAMAGORI PDF ERROR]", type(e).__name__, str(e))
        return out

    # 改行位置はPDFライブラリ依存でぶれるため、空白類はすべて単一スペースに
    # 正規化した「1行化テキスト」を基準に処理する。
    flat = re.sub(r"\s+", " ", raw_text)

    rno_int = int(rno)

    race_positions = [
        (m.start(), int(m.group(1)))
        for m in re.finditer(r"(\d{1,2})\s+R(?=\s)", flat)
    ]

    print("[GAMAGORI PDF] debug race_positions=", race_positions[:14])

    if not race_positions:
        # 空白追従の前提が外れている可能性があるため、より緩い条件と
        # 生テキストの先頭・「R」出現箇所の前後をログに出して原因を特定する。
        loose = [(m.start(), m.group(0)) for m in re.finditer(r"\d{1,2}\s*R", flat)][:14]
        print("[GAMAGORI PDF] debug loose_R_matches=", loose)
        print("[GAMAGORI PDF] debug flat[:300]=", repr(flat[:300]))
        print("[GAMAGORI PDF] debug raw_text[:300]=", repr(raw_text[:300]))
        print("[GAMAGORI PDF] race markers not found")
        return out

    block = None
    for i, (pos, r_no) in enumerate(race_positions):
        if r_no != rno_int:
            continue
        end = (
            race_positions[i + 1][0]
            if i + 1 < len(race_positions)
            else len(flat)
        )
        block = flat[pos:end]
        break

    if block is None:
        print("[GAMAGORI PDF] race marker not found:", rno_int)
        return out

    comment_pos = block.find("コメント")
    tail = block[comment_pos + len("コメント"):] if comment_pos >= 0 else block

    # デバッグ: 実際にpypdfが吐き出すテキストのレイアウトを確認するため、
    # 「コメント」直後の200文字をそのままログに出す。
    # これでレイアウト前提（空白の有無など）のズレを特定できる。
    print("[GAMAGORI PDF] debug tail[:200]=", repr(tail[:200]))

    name_pat = r"[一-龥々ぁ-んァ-ヶー]{1,4}"
    entry_head = re.compile(
        rf"(?:[A-Z]\s*\.\s*\d{{1,2}}\s+)?([1-6])\s+({name_pat})\s+"
    )

    entries = list(entry_head.finditer(tail))
    found = {}

    for j, m in enumerate(entries):
        lane = int(m.group(1))
        seg_end = entries[j + 1].start() if j + 1 < len(entries) else len(tail)
        comment = _norm(tail[m.end():seg_end])

        if comment and lane not in found:
            found[lane] = comment

    print("[GAMAGORI PDF] entries=", len(entries), "found=", len(found))

    for idx, row in race.iterrows():
        lane = int(row["lane"])
        comment = found.get(lane, "")

        # PDFは進入コース→枠番→苗字→コメントという固定レイアウトの
        # 位置関係から抽出しているため、テキスト内容自体の確度は高い。
        # _looks_like_natural_comment の語彙チェックは全文検索用に
        # 作られたもので短いコメントを過剰に弾くことがあるため、
        # ここでは文字数とテーブル的ノイズの有無だけを見る。
        ok = (
            bool(comment)
            and 3 <= len(comment) <= 220
            and not _looks_like_motor_table_noise(comment)
        )

        if ok:
            out.loc[idx, "venue_comment"] = comment
            out.loc[idx, "venue_comment_source"] = "蒲郡公式・ガマスポPDF"
            out.loc[idx, "venue_comment_url"] = pdf_url

    print(
        "[GAMAGORI PDF] comments=",
        int((out["venue_comment"].astype(str).str.strip() != "").sum()),
    )

    return out


def fetch_gamagori_comments(date_yyyymmdd, jcd, rno, race):
    out = _empty(race)

    # 1) コメント＆モーター一覧
    # www. で「ただいまデータはございません」の場合、www1. ミラーに
    # データが乗っていることがあるためフォールバックする。
    for url in (GAMAGORI_COMMENT_ALL, GAMAGORI_COMMENT_ALL.replace("www.", "www1.", 1)):
        try:
            html = _get(url)

            if _looks_like_no_data_page(html):
                print("[GAMAGORI HTML] no data page:", url)
                continue

            found = _extract_comments_from_document(html, race)
            print("[GAMAGORI HTML] comments=", len(found), "url=", url)

            for idx, row in race.iterrows():
                lane = int(row["lane"])
                c = found.get(lane, "")
                if c and _looks_like_natural_comment(c):
                    out.loc[idx, "venue_comment"] = c
                    out.loc[idx, "venue_comment_source"] = "蒲郡公式・コメント＆モーター一覧"
                    out.loc[idx, "venue_comment_url"] = url

            if (out["venue_comment"].astype(str).str.strip() != "").any():
                break

        except Exception as e:
            print("[GAMAGORI HTML ERROR]", url, type(e).__name__, str(e))

    # 2) LINE VOOM
    if (out["venue_comment"].astype(str).str.strip() != "").sum() < 6:
        try:
            html = _get(GAMAGORI_LINE_VOOM)
            found = _extract_comments_from_document(html, race)

            print("[GAMAGORI VOOM] comments=", len(found))

            for idx, row in race.iterrows():
                if _norm(out.loc[idx, "venue_comment"]):
                    continue

                lane = int(row["lane"])
                c = found.get(lane, "")
                if c and _looks_like_natural_comment(c):
                    out.loc[idx, "venue_comment"] = c
                    out.loc[idx, "venue_comment_source"] = "蒲郡公式・出場選手コメントボード"
                    out.loc[idx, "venue_comment_url"] = GAMAGORI_LINE_VOOM

        except Exception as e:
            print("[GAMAGORI VOOM ERROR]", type(e).__name__, str(e))

    # 3) ガマスポPDF
    if out["venue_comment"].astype(str).str.strip().eq("").any():
        pdf_out = _fetch_gamagori_pdf_comments(date_yyyymmdd, rno, race)

        for idx in out.index:
            if _norm(out.loc[idx, "venue_comment"]):
                continue

            c = _norm(pdf_out.loc[idx, "venue_comment"])
            if c:
                out.loc[idx, "venue_comment"] = c
                out.loc[idx, "venue_comment_source"] = pdf_out.loc[idx, "venue_comment_source"]
                out.loc[idx, "venue_comment_url"] = pdf_out.loc[idx, "venue_comment_url"]

    print(
        "[GAMAGORI FINAL] comments=",
        int((out["venue_comment"].astype(str).str.strip() != "").sum()),
    )

    return out


OMURA_COMMENT_ALL = "https://omurakyotei.jp/yosou/comment.php?day={date}"


def fetch_omura_comments(date_yyyymmdd, jcd, rno, race):
    """
    大村公式サイトの「全選手コメント・モーター評価一覧」から取得。
    ページはテーブル形式（選手名 / コメント / モーター / 過去コメント）。
    レース番号での絞り込みは無いため、選手名で該当艇にマッチさせる。
    """
    out = _empty(race)
    url = OMURA_COMMENT_ALL.format(date=date_yyyymmdd)

    try:
        html = _get(url)
    except Exception as e:
        print("[OMURA ERROR]", url, type(e).__name__, str(e))
        return out

    if _looks_like_no_data_page(html):
        print("[OMURA] no data page:", url)
        return out

    soup = BeautifulSoup(html, "lxml")
    found_by_name = {}

    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue

        name = _compact_name(cells[0].get_text(" ", strip=True))
        if not name or len(name) > 12:
            continue

        raw_comment = _norm(cells[1].get_text(" ", strip=True))
        # 「レース後「…」[17:22]」のような後追いコメントは除き、
        # 展示前の主コメントのみを使う。
        comment = raw_comment.split("「")[0].strip() or raw_comment

        if comment and name not in found_by_name:
            found_by_name[name] = comment

    print("[OMURA] entries=", len(found_by_name))

    for idx, row in race.iterrows():
        name = _compact_name(row.get("racer_name", ""))
        if not name:
            continue

        comment = found_by_name.get(name, "")
        if comment and _looks_like_natural_comment(comment):
            out.loc[idx, "venue_comment"] = comment
            out.loc[idx, "venue_comment_source"] = "大村公式・全選手コメント"
            out.loc[idx, "venue_comment_url"] = url

    print(
        "[OMURA] comments=",
        int((out["venue_comment"].astype(str).str.strip() != "").sum()),
    )

    return out


# 「index.php?page=...」という共通CMSを使っている場のドメイン一覧。
# 唐津(23)で「page=raceinfo-racers_comment」が全選手コメントページである
# ことを実機確認済み。ただし津(09)のように同じCMSでもページ名が
# 異なる／存在しない場があるため、複数の候補ページを順に試す。
COMMON_CMS_VENUES = {
    "02": "https://www.boatrace-toda.jp",
    "03": "https://www.boatrace-edogawa.com",
    "05": "https://www.boatrace-tamagawa.com",
    "06": "https://www.boatrace-hamanako.jp",
    "08": "https://www.boatrace-tokoname.jp",
    "09": "https://www.boatrace-tsu.com",
    "10": "https://www.boatrace-mikuni.jp",
    "11": "https://www.boatrace-biwako.jp",
    "12": "https://www.boatrace-suminoe.jp",
    "13": "https://www.boatrace-amagasaki.jp",
    "17": "https://www.boatrace-miyajima.com",
    "18": "https://www.boatrace-tokuyama.jp",
    "19": "https://www.boatrace-shimonoseki.jp",
    "21": "https://www.boatrace-ashiya.com",
    "22": "https://www.boatrace-fukuoka.com",
    "23": "https://www.boatrace-karatsu.jp",
}

# ページ名は場によって異なることがあるため、候補を順に試す。
# 「raceinfo-racers_comment」は唐津で実在確認済み。
# 「yosou-yosou」は津で実在確認済み（ただしそのままではコメント欄が
# 見つからないこともある）。他の候補は同系CMSでの命名慣習からの推測。
COMMON_CMS_PAGE_CANDIDATES = (
    "index.php?page=raceinfo-racers_comment",
    "index.php?page=yosou-yosou&race={rno}",
    "index.php?page=raceinfo-tenbo",
)

VENUE_SOURCE_LABELS = {
    "02": "戸田公式", "03": "江戸川公式", "05": "多摩川公式", "06": "浜名湖公式",
    "08": "常滑公式", "09": "津公式", "10": "三国公式", "11": "びわこ公式",
    "12": "住之江公式", "13": "尼崎公式", "17": "宮島公式", "18": "徳山公式",
    "19": "下関公式", "21": "芦屋公式", "22": "福岡公式", "23": "唐津公式",
}


def fetch_common_cms_comments(date_yyyymmdd, jcd, rno, race):
    """
    「index.php?page=...」形式の共通CMSを使っている場向けの汎用取得。

    場ごとにページ構成の細部が異なる可能性があるため、複数の候補URLを
    順に試し、「データなしページ」でなく実際にコメントが見つかった
    時点で採用する。全滅した場合は空を返し、呼び出し側で
    BOAT RACE公式ピットレポートへフォールバックする。
    """
    code = str(jcd).zfill(2)
    out = _empty(race)

    base = COMMON_CMS_VENUES.get(code)
    if not base:
        return out

    label = VENUE_SOURCE_LABELS.get(code, f"{code}公式")

    for page_tmpl in COMMON_CMS_PAGE_CANDIDATES:
        url = f"{base}/sp/{page_tmpl.format(rno=int(rno))}"

        try:
            html = _get(url)
        except Exception as e:
            print(f"[CMS {code} ERROR]", url, type(e).__name__, str(e))
            continue

        if _looks_like_no_data_page(html):
            print(f"[CMS {code}] no data page:", url)
            continue

        found = _extract_comments_from_document(html, race)
        print(f"[CMS {code}] comments=", len(found), "url=", url)

        for idx, row in race.iterrows():
            lane = int(row["lane"])
            c = found.get(lane, "")
            if c and _looks_like_natural_comment(c):
                out.loc[idx, "venue_comment"] = c
                out.loc[idx, "venue_comment_source"] = f"{label}・全選手コメント"
                out.loc[idx, "venue_comment_url"] = url

        if (out["venue_comment"].astype(str).str.strip() != "").any():
            break

    return out


def fetch_venue_comments(date_yyyymmdd, jcd, rno, race):
    code = str(jcd).zfill(2)

    try:
        if code == "07":
            return fetch_gamagori_comments(date_yyyymmdd, jcd, rno, race)

        if code == "24":
            return fetch_omura_comments(date_yyyymmdd, jcd, rno, race)

        if code in COMMON_CMS_VENUES:
            return fetch_common_cms_comments(date_yyyymmdd, jcd, rno, race)

    except Exception as e:
        print("[VENUE COMMENT ERROR]", code, type(e).__name__, str(e))

    return _empty(race)
