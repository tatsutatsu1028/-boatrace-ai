"""
調査用の一時スクリプト（本番では使わない）。

公式サイトの過去ページが「何年前まで」「どの項目まで」残っているかを、
少数のページだけ取得して確かめる。負荷を避けるため、全体で数百リクエスト
以内・1リクエストごとに1.5秒以上待つ。

結果は docs/data_investigation/probe/out/ にテキストで書き出す。
"""

from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from official_fetcher import (  # noqa: E402
    VENUES, fetch_racelist, fetch_beforeinfo, fetch_race_result,
)

OUT = Path(__file__).parent / "out"
OUT.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (compatible; BoatraceAIMobile/2.5; personal-analysis-tool; research-probe)"}
BASE = "https://www.boatrace.jp/owpc/pc/race"
WAIT = 1.5


def norm(s):
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip()


def get(url, **kw):
    time.sleep(WAIT)
    try:
        r = requests.get(url, headers=UA, timeout=25, **kw)
        if not kw.get("stream"):
            r.encoding = r.apparent_encoding or "utf-8"
        return r
    except Exception as e:  # noqa: BLE001
        print(f"[ERR] {url} -> {type(e).__name__}: {e}", flush=True)
        return None


def around(text, word, width=80, limit=3):
    out = []
    for m in re.finditer(re.escape(word), text):
        s = max(0, m.start() - width)
        out.append(text[s:m.end() + width])
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------
# A. boatrace.jp の過去ページ保持期間と項目
# ---------------------------------------------------------------
DATES = ["20260920", "20260301", "20251001", "20250401", "20241001", "20240401", "20231001", "20220401", "20200401", "20150401", "20100401"]
KEYWORDS = [
    "進入固定", "優勝戦", "準優", "予選", "特選", "選抜", "初日", "日目", "最終日",
    "3連率", "2連率", "F", "L", "部品交換", "プロペラ", "チルト", "展示タイム",
    "スタート展示", "風向", "風速", "波高", "気温", "水温", "潮", "安定板",
    "SG", "G1", "G2", "G3", "一般", "決まり手", "進入", "周回",
]


def open_venues(hd):
    r = get(f"{BASE}/index?hd={hd}")
    if r is None or r.status_code != 200:
        return [], (r.status_code if r is not None else None), ""
    soup = BeautifulSoup(r.text, "lxml")
    codes = []
    for a in soup.find_all("a", href=re.compile(r"jcd=(\d{2})")):
        m = re.search(r"jcd=(\d{2})", a["href"])
        if m and m.group(1) not in codes:
            codes.append(m.group(1))
    # 開催一覧のグレードアイコン（class名）を控える
    grades = sorted(set(re.findall(r'class="[^"]*\b(is-(?:SG|G1|G2|G3|IPPAN|ippan|G1b|lady|venus|rookie|master)[^" ]*)', r.text)))
    return codes, r.status_code, " ".join(grades)


def probe_page(kind, hd, jcd, rno):
    url = f"{BASE}/{kind}?hd={hd}&jcd={jcd}&rno={rno}"
    r = get(url)
    if r is None:
        return {"url": url, "status": None}
    soup = BeautifulSoup(r.text, "lxml")
    body = soup.select_one(".contentsFrame1_inner") or soup
    text = norm(body.get_text(" ", strip=True))
    info = {
        "url": url,
        "status": r.status_code,
        "len": len(r.text),
        "has_nodata": any(w in text for w in ["データがありません", "該当するデータ", "レース中止", "エラー"]),
        "keywords": {k: (k in text) for k in KEYWORDS},
        "title_area": text[:600],
        "classes": sorted(set(re.findall(r'\bis-(?:wind|weather|direction|fixed|SG|G1|G2|G3|ippan|IPPAN|fBold|boatColor)\w*', r.text)))[:40],
    }
    for w in ["進入固定", "日目", "最終日", "初日", "部品交換", "プロペラ", "潮", "風向", "安定板", "3連率", "スタート展示", "決まり手", "進入"]:
        hit = around(text, w)
        if hit:
            info.setdefault("context", {})[w] = hit
    (OUT / "html").mkdir(exist_ok=True)
    # 生HTMLは容量の関係で保存しない。本文テキストの先頭だけ残す。
    (OUT / "html" / f"{kind}_{hd}_{jcd}_{rno}.txt").write_text(text[:12000], encoding="utf-8")
    return info


def section_a():
    results = {}
    for hd in DATES:
        codes, st, grades = open_venues(hd)
        print(f"[A] {hd} index status={st} venues={codes}", flush=True)
        entry = {"index_status": st, "venues": codes, "grade_classes": grades, "pages": {}, "parsed": {}}
        if codes:
            jcd = codes[0]
            for rno in (1, 12):
                for kind in ("racelist", "beforeinfo", "raceresult"):
                    entry["pages"][f"{kind}_{rno}R"] = probe_page(kind, hd, jcd, rno)
            # 既存パーサで読めるか
            try:
                time.sleep(WAIT)
                rl = fetch_racelist(hd, jcd, 12)
                entry["parsed"]["racelist_cols"] = [c for c in rl.columns if not c.startswith("source")]
                entry["parsed"]["racelist_row1"] = json.loads(rl.drop(columns=[c for c in rl.columns if c.startswith("source")]).head(1).to_json(orient="records", force_ascii=False))
            except Exception as e:  # noqa: BLE001
                entry["parsed"]["racelist_err"] = repr(e)[:200]
            try:
                time.sleep(WAIT)
                bi = fetch_beforeinfo(hd, jcd, 12)
                entry["parsed"]["beforeinfo"] = json.loads(bi.drop(columns=[c for c in bi.columns if c.startswith("source")]).to_json(orient="records", force_ascii=False))
            except Exception as e:  # noqa: BLE001
                entry["parsed"]["beforeinfo_err"] = repr(e)[:200]
            try:
                time.sleep(WAIT)
                rr = fetch_race_result(hd, jcd, 12)
                entry["parsed"]["result"] = {k: v for k, v in (rr or {}).items() if k in ("first", "second", "third", "trifecta", "trifecta_payout_per_100", "kimarite")}
            except Exception as e:  # noqa: BLE001
                entry["parsed"]["result_err"] = repr(e)[:200]
        results[hd] = entry
        (OUT / "a_official_pages.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "a_official_pages.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------
# B. 公式ダウンロード（競走成績K / 番組表B）の保持状況
# ---------------------------------------------------------------
def section_b():
    res = {}
    for ymd in ["260920", "250401", "241001", "231001", "200401", "150401"]:
        yyyymm = "20" + ymd[:4]
        for kind, prefix in (("K", "k"), ("B", "b")):
            url = f"https://www1.mbrace.or.jp/od2/{kind}/{yyyymm}/{prefix}{ymd}.lzh"
            r = get(url)
            res[url] = None if r is None else {"status": r.status_code, "bytes": len(r.content), "ctype": r.headers.get("content-type")}
            print(f"[B] {url} -> {res[url]}", flush=True)
    # ダウンロードページ本体
    for url in ["https://www1.mbrace.or.jp/od2/K/dindex.html", "https://www1.mbrace.or.jp/od2/B/dindex.html"]:
        r = get(url)
        if r is not None:
            res[url] = {"status": r.status_code, "text": norm(BeautifulSoup(r.text, "lxml").get_text(" "))[:1500]}
    (OUT / "b_downloads.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    # K/Bファイル1日分を解凍して中身の見出しを確認する（lhafileが使えれば）
    try:
        import lhafile  # type: ignore
        import io
        for kind, prefix, ymd in (("K", "k", "241001"), ("B", "b", "241001")):
            url = f"https://www1.mbrace.or.jp/od2/{kind}/20{ymd[:4]}/{prefix}{ymd}.lzh"
            r = get(url)
            if r is None or r.status_code != 200:
                continue
            f = lhafile.Lhafile(io.BytesIO(r.content))
            for info in f.infolist():
                raw = f.read(info.filename)
                txt = raw.decode("cp932", errors="replace")
                (OUT / f"b_{kind}_{ymd}_head.txt").write_text(txt[:9000], encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        (OUT / "b_lha_error.txt").write_text(repr(e), encoding="utf-8")


# ---------------------------------------------------------------
# C. 各場公式サイト: ピットレポート・規約・robots.txt
# ---------------------------------------------------------------
PIT_WORDS = re.compile(r"ピット\s*(レポート|リポート|情報|通信|便り|だより|速報|インタビュー|コメント)|PIT\s*REPORT|pitreport|pit_report|pit-report", re.I)
TERMS_WORDS = re.compile(r"利用規約|ご利用にあたって|サイトポリシー|サイトご利用|著作権|免責|このサイトについて|当サイトについて|プライバシー|リンクについて|ご利用上の注意|terms|policy", re.I)
RULE_WORDS = ["自動", "機械的", "プログラム", "クローラ", "クローリング", "スクレイピング", "ロボット", "複製", "転載", "転用", "二次利用", "営利", "商用", "無断", "禁止", "負荷", "著作権"]


def venue_sites():
    sites = {}
    for jcd in VENUES:
        r = get(f"https://www.boatrace.jp/owpc/pc/data/stadium?jcd={jcd}")
        if r is None:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        cand = []
        for a in soup.find_all("a", href=True):
            h = a["href"]
            host = urlparse(h).netloc
            if h.startswith("http") and "boatrace.jp" not in host and not any(x in host for x in ["twitter", "x.com", "facebook", "youtube", "instagram", "line.me", "google", "apple", "tiktok"]):
                cand.append((norm(a.get_text(" ")), h))
        sites[jcd] = cand
    (OUT / "c_venue_links.json").write_text(json.dumps(sites, ensure_ascii=False, indent=1), encoding="utf-8")
    return sites


VENUE_URLS = {
    "01": ["https://www.kiryu-kyotei.com/", "https://www.boatrace-kiryu.jp/"],
    "02": ["https://www.boatrace-toda.jp/"],
    "03": ["https://www.edogawa-kyotei.co.jp/", "https://www.boatrace-edogawa.com/"],
    "04": ["https://www.heiwajima.gr.jp/", "https://www.boatrace-heiwajima.jp/"],
    "05": ["https://www.boatrace-tamagawa.com/"],
    "06": ["https://www.boatrace-hamanako.jp/"],
    "07": ["https://www.gamagori-kyotei.com/", "https://www.boatrace-gamagori.jp/"],
    "08": ["https://www.boatrace-tokoname.jp/"],
    "09": ["https://www.boatrace-tsu.com/", "https://www.boatrace-tsu.jp/"],
    "10": ["https://www.boatrace-mikuni.jp/"],
    "11": ["https://www.boatrace-biwako.jp/"],
    "12": ["https://www.boatrace-suminoe.jp/"],
    "13": ["https://www.boatrace-amagasaki.jp/"],
    "14": ["https://www.n14.jp/", "https://www.boatrace-naruto.jp/"],
    "15": ["https://www.marugameboat.jp/", "https://www.boatrace-marugame.jp/"],
    "16": ["https://www.kojimaboat.jp/", "https://www.boatrace-kojima.jp/"],
    "17": ["https://www.boatrace-miyajima.com/"],
    "18": ["https://www.boatrace-tokuyama.jp/"],
    "19": ["https://www.boatrace-shimonoseki.jp/"],
    "20": ["https://www.wmb.jp/", "https://www.boatrace-wakamatsu.jp/"],
    "21": ["https://www.boatrace-ashiya.com/"],
    "22": ["https://www.boatrace-fukuoka.com/"],
    "23": ["https://www.boatrace-karatsu.jp/"],
    "24": ["https://omurakyotei.jp/", "https://www.omurakyotei.jp/"],
}
TERMS_GUESS = ["agreement.html", "policy.html", "sitepolicy/", "policy/", "site/policy.html", "privacy/", "req.html", "about/", "terms/", "kiyaku.html", "guide/policy.html"]


def _anchor_text(a):
    return norm(a.get_text(" ")) or norm(a.get("title", "")) or norm(" ".join(i.get("alt", "") for i in a.find_all("img")))


def _collect_links(html, base):
    soup = BeautifulSoup(html, "lxml")
    pit, terms = [], []
    for a in soup.find_all("a", href=True):
        t = _anchor_text(a)
        h = urljoin(base, a["href"])
        if PIT_WORDS.search(t) or PIT_WORDS.search(a["href"]) or re.search(r"pit", a["href"], re.I):
            pit.append((t, h))
        if TERMS_WORDS.search(t):
            terms.append((t, h))
    return list(dict.fromkeys(pit))[:10], list(dict.fromkeys(terms))[:8], soup


def _terms_page(url, jcd, idx):
    p = get(url)
    if p is None or p.status_code != 200:
        return {"url": url, "status": None if p is None else p.status_code}
    ptxt = norm(BeautifulSoup(p.text, "lxml").get_text(" "))
    hits = {w: around(ptxt, w, width=80, limit=4) for w in RULE_WORDS if w in ptxt}
    (OUT / "terms").mkdir(exist_ok=True)
    (OUT / "terms" / f"{jcd}_{idx}.txt").write_text(f"{p.url}\n\n{ptxt[:20000]}", encoding="utf-8")
    return {"url": url, "final_url": p.url, "status": p.status_code, "len": len(ptxt), "rule_hits": hits}


def section_c():
    report = {}
    for jcd, urls in VENUE_URLS.items():
        name = VENUES[jcd]
        entry = {"venue": name, "tried": []}
        r = None
        for u in urls:
            r = get(u)
            entry["tried"].append((u, None if r is None else r.status_code))
            if r is not None and r.status_code == 200:
                break
        if r is None or r.status_code != 200:
            report[jcd] = entry
            continue
        entry["top"] = r.url
        pr = urlparse(r.url)
        origin = f"{pr.scheme}://{pr.netloc}/"
        rb = get(origin + "robots.txt")
        entry["robots"] = None if rb is None else {"status": rb.status_code, "text": rb.text[:1500] if rb.status_code == 200 else ""}
        pit, terms, soup = _collect_links(r.text, r.url)
        # スマホ版トップも見る（PC版にリンクが無い場合がある）
        body = norm(soup.get_text(" "))
        entry["top_len"] = len(body)
        entry["top_js_only"] = len(body) < 300
        entry["top_pit_context"] = around(body, "ピット", 60, 5)
        entry["pit_links"] = pit
        entry["terms_links"] = terms
        pit_pages = []
        for t, h in pit[:3]:
            p = get(h)
            if p is None:
                continue
            ps = BeautifulSoup(p.text, "lxml")
            ptxt = norm(ps.get_text(" "))
            dates = sorted(set(re.findall(r"20\d{2}[./年-]\s?\d{1,2}[./月-]\s?\d{1,2}", ptxt)))
            archive = [(_anchor_text(a), urljoin(p.url, a["href"])) for a in ps.find_all("a", href=True)
                       if re.search(r"過去|バックナンバー|アーカイブ|一覧|archive|前の|次の|page|日目|hd=|date", _anchor_text(a) + a["href"], re.I)]
            pit_pages.append({
                "link_text": t, "url": h, "status": p.status_code, "final_url": p.url,
                "ctype": p.headers.get("content-type"), "len": len(ptxt),
                "dates_found": dates[:3] + dates[-3:],
                "archive_links": list(dict.fromkeys(archive))[:12],
                "text_head": ptxt[:500],
                "pit_context": around(ptxt, "ピット", 80, 3),
            })
        entry["pit_pages"] = pit_pages
        term_pages = []
        seen = set()
        for i, (t, h) in enumerate(terms[:4]):
            if h in seen:
                continue
            seen.add(h)
            d = _terms_page(h, jcd, i)
            d["link_text"] = t
            term_pages.append(d)
        if not any(tp.get("status") == 200 for tp in term_pages):
            for g in TERMS_GUESS:
                d = _terms_page(origin + g, jcd, "g" + g.strip("/").replace("/", "_"))
                if d.get("status") == 200:
                    d["link_text"] = "(guess)"
                    term_pages.append(d)
                    break
        entry["terms_pages"] = term_pages
        report[jcd] = entry
        print(f"[C] {jcd} {name} top={entry.get('top')} pit={len(pit)} terms={len(terms)}", flush=True)
        (OUT / "c_venues.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "c_venues.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


def section_d():
    """boatrace.jp 本体の規約・robots.txt"""
    res = {}
    for url in ["https://www.boatrace.jp/robots.txt", "https://www1.mbrace.or.jp/robots.txt"]:
        r = get(url)
        res[url] = None if r is None else {"status": r.status_code, "text": r.text[:3000]}
    r = get("https://www.boatrace.jp/owpc/pc/extra/index.html") or get("https://www.boatrace.jp/")
    links = []
    if r is not None:
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.find_all("a", href=True):
            t = norm(a.get_text(" "))
            if TERMS_WORDS.search(t):
                links.append((t, urljoin(r.url, a["href"])))
    links = list(dict.fromkeys(links))[:8]
    res["links"] = links
    pages = []
    for t, h in links:
        p = get(h)
        if p is None:
            continue
        ptxt = norm(BeautifulSoup(p.text, "lxml").get_text(" "))
        hits = {w: around(ptxt, w, 90, 4) for w in RULE_WORDS if w in ptxt}
        pages.append({"text": t, "url": h, "status": p.status_code, "rule_hits": hits})
        (OUT / "terms").mkdir(exist_ok=True)
        (OUT / "terms" / f"boatracejp_{len(pages)}.txt").write_text(f"{h}\n\n{ptxt[:20000]}", encoding="utf-8")
    res["pages"] = pages
    (OUT / "d_boatracejp_terms.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------
# E. boatrace.jp 内のピットレポート（場ごとの掲載有無・過去分）
# ---------------------------------------------------------------
PIT_DATES = ["20260920", "20260915", "20260301", "20250401", "20241001", "20230401", "20210402", "20180409"]


def section_e():
    res = {}
    for hd in PIT_DATES:
        codes, st, _ = open_venues(hd)
        day = {}
        for jcd in codes:
            url = f"{BASE}/pitreport?hd={hd}&jcd={jcd}&rno=1"
            r = get(url)
            if r is None:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            body = soup.select_one(".contentsFrame1_inner") or soup
            text = norm(body.get_text(" ", strip=True))
            # タブ（出走表/直前情報/ピットレポート…）にピットレポートがあるか
            tab = bool(soup.find("a", href=re.compile(r"pitreport")))
            day[jcd] = {
                "venue": VENUES.get(jcd), "status": r.status_code, "final_url": r.url,
                "tab_link": tab, "len_text": len(text),
                "nodata": any(w in text for w in ["データがありません", "該当するデータ", "掲載されていません", "準備中"]),
                "head": text[:300],
            }
        res[hd] = day
        print(f"[E] {hd} venues={len(codes)}", flush=True)
        (OUT / "e_pitreport.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "e_pitreport.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


def section_f():
    """boatrace.jp のサイトポリシー・本サイトについて の全文"""
    (OUT / "terms").mkdir(exist_ok=True)
    for name, url in [
        ("policy", "https://www.boatrace.jp/owpc/pc/extra/policy.html"),
        ("about", "https://www.boatrace.jp/owpc/pc/extra/about.html"),
    ]:
        r = get(url)
        if r is None:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        body = soup.select_one(".contentsFrame1_inner") or soup.select_one("main") or soup
        (OUT / "terms" / f"boatracejp_{name}.txt").write_text(f"{url} status={r.status_code}\n\n" + norm(body.get_text(" ", strip=True))[:30000], encoding="utf-8")


# ---------------------------------------------------------------
# G. boatrace.jp ピットレポートの場別掲載状況（週1日×半年＋月1日×1年半）
# ---------------------------------------------------------------
def _pit_dates():
    from datetime import date, timedelta
    out = []
    d = date(2026, 9, 26)
    while d >= date(2026, 3, 28):
        out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=7)
    for y, m in [(2026, 3), (2026, 2), (2026, 1), (2025, 12), (2025, 11), (2025, 10), (2025, 9), (2025, 8), (2025, 7),
                 (2025, 6), (2025, 5), (2025, 4), (2025, 3), (2025, 2), (2025, 1), (2024, 12), (2024, 11), (2024, 10)]:
        out.append(f"{y}{m:02d}15")
    return out


def _pit_msg(text):
    m = re.search(r"ピットレポートは\s*(\d+)R\s*から\s*(\d+)R\s*まで", text)
    if m:
        return "range", int(m.group(1)), int(m.group(2))
    if "表示対象レースではありません" in text:
        return "none", None, None
    return "body", None, None


def section_g():
    res = {}
    body_checks = {}
    for i, hd in enumerate(_pit_dates()):
        codes, st, grades = open_venues(hd)
        day = {}
        for jcd in codes:
            r = get(f"{BASE}/pitreport?hd={hd}&jcd={jcd}&rno=1")
            if r is None:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            body = soup.select_one(".contentsFrame1_inner") or soup
            text = norm(body.get_text(" ", strip=True))
            kind, a, b = _pit_msg(text)
            rec = {"kind": kind, "from": a, "to": b}
            # 1Rで「対象外」でも後半レースだけ掲載の節がないか、最初の4日だけ12Rでも確認
            if kind == "none" and i < 4:
                r2 = get(f"{BASE}/pitreport?hd={hd}&jcd={jcd}&rno=12")
                if r2 is not None:
                    t2 = norm((BeautifulSoup(r2.text, "lxml").select_one(".contentsFrame1_inner") or BeautifulSoup(r2.text, "lxml")).get_text(" ", strip=True))
                    rec["check12"] = _pit_msg(t2)[0]
                    rec["check12_len"] = len(t2)
            # 掲載範囲のレースの本文が残っているか（場ごとに最初の2回だけ）
            if kind == "range" and body_checks.get(jcd, 0) < 2:
                body_checks[jcd] = body_checks.get(jcd, 0) + 1
                r3 = get(f"{BASE}/pitreport?hd={hd}&jcd={jcd}&rno={b}")
                if r3 is not None:
                    s3 = BeautifulSoup(r3.text, "lxml")
                    t3 = norm((s3.select_one(".contentsFrame1_inner") or s3).get_text(" ", strip=True))
                    # 本文そのものは保存しない（長さと構造だけ）
                    rec["body_len"] = len(t3)
                    rec["body_has_racer_names"] = len(re.findall(r"[一-龥]{1,4}\s[一-龥ぁ-んァ-ヶ]{1,4}", t3))
                    rec["body_struct"] = t3[t3.find("結果") + 2: t3.find("結果") + 60] if "結果" in t3 else ""
            day[jcd] = rec
        res[hd] = {"grades": grades, "venues": day}
        print(f"[G] {hd} venues={len(codes)} pit={[j for j, v in day.items() if v['kind'] != 'none']}", flush=True)
        (OUT / "g_pit_scan.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------
# H. 各場サイトのサイトマップ等からピットレポート・規約ページを探す
# ---------------------------------------------------------------
SITEMAP_WORDS = re.compile(r"サイトマップ|sitemap", re.I)


def section_h():
    report = {}
    extra_pages = {
        "24": ["https://omurakyotei.jp/yosou/sp/sankou/report/", "https://omurakyotei.jp/sitemap/"],
    }
    for jcd, urls in VENUE_URLS.items():
        entry = {"venue": VENUES[jcd]}
        r = None
        for u in urls:
            r = get(u)
            if r is not None and r.status_code == 200:
                break
        if r is None or r.status_code != 200:
            report[jcd] = entry
            continue
        pr = urlparse(r.url)
        origin = f"{pr.scheme}://{pr.netloc}/"
        soup = BeautifulSoup(r.text, "lxml")
        pages = []
        for a in soup.find_all("a", href=True):
            if SITEMAP_WORDS.search(_anchor_text(a) + a["href"]):
                pages.append(urljoin(r.url, a["href"]))
        pages = list(dict.fromkeys(pages))[:2]
        # 共通CMSのSP版トップ・レース情報トップ
        pages += [origin + "sp/", origin + "sp/index.php?page=raceinfo", origin + "index.php?page=raceinfo"]
        pages += extra_pages.get(jcd, [])
        found_pit, found_terms, visited = [], [], []
        for u in pages:
            p = get(u)
            if p is None or p.status_code != 200:
                visited.append((u, None if p is None else p.status_code))
                continue
            visited.append((u, 200))
            pit, terms, ps = _collect_links(p.text, p.url)
            found_pit += pit
            found_terms += terms
            if "report" in u:
                pt = norm(ps.get_text(" "))
                dates = sorted(set(re.findall(r"20\d{2}[./年-]\s?\d{1,2}[./月-]\s?\d{1,2}|\d{1,2}月\d{1,2}日", pt)))
                entry["report_page"] = {"url": u, "len": len(pt), "dates": dates[:5] + dates[-5:], "head": pt[:300]}
        entry["visited"] = visited
        entry["pit_links"] = list(dict.fromkeys(found_pit))[:12]
        entry["terms_links"] = list(dict.fromkeys(found_terms))[:8]
        # 規約ページ（未取得のもの）
        tps = []
        for i, (t, h) in enumerate(entry["terms_links"][:4]):
            if not h.startswith("http"):
                continue
            d = _terms_page(h, jcd, f"h{i}")
            d["link_text"] = t
            tps.append(d)
        if not any(tp.get("status") == 200 and tp.get("rule_hits") for tp in tps):
            for g in TERMS_GUESS:
                d = _terms_page(origin + g, jcd, "hg" + g.strip("/").replace("/", "_").replace(".", "_"))
                if d.get("status") == 200:
                    d["link_text"] = "(guess)"
                    tps.append(d)
                    break
        entry["terms_pages"] = tps
        report[jcd] = entry
        print(f"[H] {jcd} pit={len(entry['pit_links'])} terms={len(entry['terms_links'])}", flush=True)
        (OUT / "h_venue_sitemaps.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    which = sys.argv[1:] or ["a", "b", "c", "d"]
    for w in which:
        print(f"==== section {w} ====", flush=True)
        try:
            globals()[f"section_{w}"]()
        except Exception as e:  # noqa: BLE001
            print(f"[FATAL] section {w}: {type(e).__name__}: {e}", flush=True)
            (OUT / f"{w}_fatal.txt").write_text(repr(e), encoding="utf-8")
