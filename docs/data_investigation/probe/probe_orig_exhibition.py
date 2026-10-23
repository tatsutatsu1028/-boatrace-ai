"""
調査用の一時スクリプト（本番では使わない）。

各場の公式サイトで「オリジナル展示」（一周・まわり足・直線・半周など）が
どこに・どの形式で載っているかを確かめる。負荷を避けるため 1.5秒以上の
間隔をあけ、1場あたりのページ数にも上限を設ける。

  python probe_orig_exhibition.py discover   # 各場サイトを浅く巡回して掲載ページを探す
  python probe_orig_exhibition.py fetch FILE # FILE(JSON: {jcd: [url,...]}) のページを取得して保存

結果は環境変数 PROBE_OUT（非公開のデータ用リポジトリ内）に書き出す。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

OUT = Path(os.environ.get("PROBE_OUT") or Path(__file__).parent / "out_orig")
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "html").mkdir(exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (compatible; BoatraceAIMobile/2.5; personal-analysis-tool; research-probe)"}
WAIT = 1.5
JST = timezone(timedelta(hours=9))

VENUE_URLS = {
    "01": ["https://www.kiryu-kyotei.com/", "https://www.boatrace-kiryu.jp/"],
    "02": ["https://www.boatrace-toda.jp/"],
    "03": ["https://www.boatrace-edogawa.com/", "https://www.edogawa-kyotei.co.jp/"],
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

# 巡回時に優先するリンク（強いほど先に辿る）
STRONG = re.compile(r"オリジナル展示|オリ展|展示データ|周回|まわり足|回り足|tenji|orgten|oriten|original|chokuzen|直前", re.I)
WEAK = re.compile(r"展示|レース情報|出走表|予想|raceinfo|race|yosou|kyogi|racelist|番組|本日|today|sp/", re.I)
SKIP = re.compile(r"\.(jpg|jpeg|png|gif|webp|svg|mp4|zip|lzh|css|js)(\?|$)|twitter|facebook|youtube|instagram|line\.me|tiktok|mailto:|tel:|javascript:|#$", re.I)
PAGE_WORDS = ["オリジナル展示", "一周", "1周", "まわり足", "回り足", "直線", "半周", "周回", "展示タイム"]
RULE_WORDS = ["自動", "機械的", "ロボット", "クローラ", "スクレイピング", "プログラム", "複製", "転載", "私的使用", "個人的", "営利", "大量", "許諾", "禁止"]
TERMS_WORDS = re.compile(r"利用規約|ご利用|サイトポリシー|サイトについて|当サイト|著作権|免責|規約|policy|terms|kiyaku", re.I)

_n = 0


def norm(s):
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip()


def get(url, **kw):
    global _n
    time.sleep(WAIT)
    _n += 1
    try:
        r = requests.get(url, headers=UA, timeout=25, **kw)
        if not kw.get("stream") and "html" in (r.headers.get("content-type") or "") or url.endswith(".txt"):
            r.encoding = r.apparent_encoding or "utf-8"
        return r
    except Exception as e:
        print("ERR", url, type(e).__name__, e, flush=True)
        return None


def around(text, word, width=60, limit=3):
    out, start = [], 0
    while len(out) < limit:
        i = text.find(word, start)
        if i < 0:
            break
        out.append(text[max(0, i - width): i + len(word) + width])
        start = i + len(word)
    return out


def anchor_text(a):
    return norm(a.get_text(" ")) or norm(a.get("title", "")) or norm(" ".join(i.get("alt", "") for i in a.find_all("img")))


def site_key(host):
    parts = host.split(".")
    return ".".join(parts[-3:]) if parts[-1] == "jp" and parts[-2] in ("co", "gr", "or", "ne") else ".".join(parts[-2:])


def describe_page(r, jcd, save_tag):
    ctype = r.headers.get("content-type") or ""
    rec = {"url": r.url, "status": r.status_code, "ctype": ctype, "len": len(r.content),
           "last_modified": r.headers.get("last-modified")}
    if "html" not in ctype and "text" not in ctype:
        return rec, None
    soup = BeautifulSoup(r.text, "lxml")
    text = norm(soup.get_text(" "))
    rec["title"] = norm(soup.title.get_text()) if soup.title else ""
    rec["text_len"] = len(text)
    rec["words"] = {w: text.count(w) for w in PAGE_WORDS if w in text}
    rec["orig_context"] = around(text, "オリジナル展示", 100, 3) + around(text, "まわり足", 100, 2)
    rec["tables"] = len(soup.find_all("table"))
    rec["iframes"] = [urljoin(r.url, f.get("src", "")) for f in soup.find_all("iframe") if f.get("src")][:8]
    rec["scripts"] = [urljoin(r.url, s.get("src")) for s in soup.find_all("script", src=True)][:15]
    rec["inline_script_hints"] = sorted(set(m for s in soup.find_all("script", src=False)
                                            for m in re.findall(r"[\w/\-.]*(?:tenji|oriten|orgten|original|chokuzen|json|api|ajax)[\w/\-.?=&]*", s.get_text() or "", re.I)))[:25]
    imgs = []
    for im in soup.find_all("img"):
        src, alt = im.get("src", ""), im.get("alt", "")
        if re.search(r"tenji|展示|oriten|orgten|original|chokuzen|data|graph", src + alt, re.I):
            imgs.append((alt, urljoin(r.url, src)))
    rec["imgs"] = imgs[:10]
    rec["pdfs"] = [(anchor_text(a), urljoin(r.url, a["href"])) for a in soup.find_all("a", href=True) if ".pdf" in a["href"].lower()][:10]
    rec["date_selectors"] = sorted(set(re.findall(r"(?:day|hd|date|ymd|kaisaibi)=\d{6,8}", r.text)))[:10]
    if rec["words"].get("オリジナル展示") or rec["words"].get("まわり足") or rec["words"].get("回り足"):
        fn = OUT / "html" / f"{jcd}_{save_tag}.html"
        fn.write_text(f"<!-- {r.url} -->\n" + r.text, encoding="utf-8")
        rec["saved"] = fn.name
    return rec, soup


def discover(max_pages=45):
    report = {}
    for jcd, urls in VENUE_URLS.items():
        entry = {"pages": [], "links": []}
        top = None
        for u in urls:
            top = get(u)
            if top is not None and top.status_code == 200:
                break
        if top is None or top.status_code != 200:
            entry["error"] = [(u, None if top is None else top.status_code) for u in urls]
            report[jcd] = entry
            continue
        origin = "{0.scheme}://{0.netloc}/".format(urlparse(top.url))
        key = site_key(urlparse(top.url).netloc)
        rb = get(origin + "robots.txt")
        entry["robots"] = None if rb is None else {"status": rb.status_code, "text": rb.text[:1500] if rb.status_code == 200 else ""}
        seen = {top.url}
        queue = []  # (priority, depth, url, via_text)
        terms = []
        idx = 0

        def handle(r, depth, via):
            nonlocal idx
            idx += 1
            rec, soup = describe_page(r, jcd, f"d{idx}")
            rec["depth"], rec["via"] = depth, via
            entry["pages"].append(rec)
            if soup is None:
                return
            for a in soup.find_all("a", href=True):
                h = urljoin(r.url, a["href"]).split("#")[0]
                t = anchor_text(a)
                if TERMS_WORDS.search(t):
                    terms.append((t, h))
                if SKIP.search(h) or h in seen:
                    continue
                if site_key(urlparse(h).netloc) != key:
                    if STRONG.search(t + " " + h):
                        entry["links"].append(("external", t, h))
                    continue
                pr = 2 if STRONG.search(t + " " + h) else 1 if WEAK.search(t + " " + h) else 0
                if pr == 0 or depth >= 3:
                    continue
                if pr == 2:
                    entry["links"].append(("strong", t, h))
                seen.add(h)
                queue.append((pr, depth + 1, h, t))
            for f in rec.get("iframes", []):
                if f not in seen and site_key(urlparse(f).netloc) == key:
                    seen.add(f)
                    queue.append((2, depth + 1, f, "iframe"))

        handle(top, 0, "top")
        for extra in [origin + "sp/", origin + "sp/index.php?page=raceinfo", origin + "index.php?page=raceinfo"]:
            if extra not in seen:
                seen.add(extra)
                queue.append((1, 1, extra, "guess"))
        n = 1
        while queue and n < max_pages:
            queue.sort(key=lambda x: (-x[0], x[1]))
            pr, depth, u, via = queue.pop(0)
            r = get(u)
            n += 1
            if r is None:
                continue
            if r.status_code != 200:
                entry["pages"].append({"url": u, "status": r.status_code, "via": via})
                continue
            handle(r, depth, via)
        entry["terms_links"] = list(dict.fromkeys(terms))[:10]
        report[jcd] = entry
        hits = [p["url"] for p in entry["pages"] if p.get("words", {}).get("オリジナル展示") or p.get("words", {}).get("まわり足")]
        print(f"[D] {jcd} pages={len(entry['pages'])} hits={len(hits)} {hits[:3]} req={_n}", flush=True)
        (OUT / "discover.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_list(path):
    """{jcd: [url, ...]} を取得し、ページの要約と HTML を保存する。"""
    targets = json.loads(Path(path).read_text(encoding="utf-8"))
    res = {}
    stamp = datetime.now(JST).strftime("%H%M")
    for jcd, urls in targets.items():
        res[jcd] = []
        for i, u in enumerate(urls):
            r = get(u)
            if r is None:
                res[jcd].append({"url": u, "status": None})
                continue
            if r.status_code != 200:
                res[jcd].append({"url": u, "status": r.status_code})
                continue
            rec, soup = describe_page(r, jcd, f"f{stamp}_{i}")
            if soup is not None and "saved" not in rec:
                fn = OUT / "html" / f"{jcd}_f{stamp}_{i}.html"
                fn.write_text(f"<!-- {r.url} -->\n" + r.text, encoding="utf-8")
                rec["saved"] = fn.name
            elif soup is None and r.status_code == 200:
                ext = (urlparse(r.url).path.rsplit(".", 1) + ["bin"])[1][:4]
                fn = OUT / "html" / f"{jcd}_f{stamp}_{i}.{ext}"
                fn.write_bytes(r.content[:3_000_000])
                rec["saved"] = fn.name
            res[jcd].append(rec)
        print(f"[F] {jcd} {[(x.get('status'), x.get('words')) for x in res[jcd]]}", flush=True)
    name = Path(path).stem
    (OUT / f"fetch_{name}_{stamp}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


def today(hd=None):
    """boatrace.jp から当日の開催場と各レースの締切予定時刻を取る。"""
    hd = hd or datetime.now(JST).strftime("%Y%m%d")
    r = get(f"https://www.boatrace.jp/owpc/pc/race/index?hd={hd}")
    codes = sorted(set(re.findall(r"jcd=(\d{2})", r.text))) if r is not None else []
    res = {"hd": hd, "venues": {}}
    for jcd in codes:
        r2 = get(f"https://www.boatrace.jp/owpc/pc/race/raceindex?jcd={jcd}&hd={hd}")
        if r2 is None or r2.status_code != 200:
            continue
        soup = BeautifulSoup(r2.text, "lxml")
        times = {}
        for tb in soup.select("tbody"):
            t = norm(tb.get_text(" "))
            m = re.match(r"(\d{1,2})R\s+(\d{1,2}:\d{2})", t)
            if m:
                times[int(m.group(1))] = m.group(2)
        res["venues"][jcd] = times
    (OUT / f"today_{hd}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[T]", hd, {k: (v.get(1), v.get(12)) for k, v in res["venues"].items()}, flush=True)
    return res


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "discover"
    if cmd == "discover":
        discover()
    elif cmd == "today":
        today(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "fetch":
        fetch_list(sys.argv[2])
    print("requests:", _n)
