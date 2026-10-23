"""
調査用の一時スクリプト（本番では使わない）その2。

  python probe_orig2.py boatcast         # BOATCAST の bc_oriten（全場共通のテキスト）を当日・過去日で確認
  python probe_orig2.py monitor END_HHMM # 当日の締切前後に掲載時刻を観測（BOATCAST＋一部の場サイト）
  python probe_orig2.py render           # 各場サイトをブラウザで描画し、オリジナル展示の掲載ページと通信先を探す

結果は環境変数 PROBE_OUT（非公開のデータ用リポジトリ内）に書き出す。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUT = Path(os.environ.get("PROBE_OUT") or Path(__file__).parent / "out_orig")
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "html").mkdir(exist_ok=True)
(OUT / "shots").mkdir(exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (compatible; BoatraceAIMobile/2.5; personal-analysis-tool; research-probe)"}
WAIT = 1.5
JST = timezone(timedelta(hours=9))
BC = "https://race.boatcast.jp/txt/{jcd}/bc_oriten_{d}_{jcd}_{rr}.txt"


def norm(s):
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip()


def now():
    return datetime.now(JST)


def get(url, wait=WAIT):
    time.sleep(wait)
    try:
        r = requests.get(url, headers=UA, timeout=25)
        r.encoding = r.apparent_encoding or "utf-8"
        return r
    except Exception as e:
        print("ERR", url, type(e).__name__, e, flush=True)
        return None


def open_venues(hd):
    r = get(f"https://www.boatrace.jp/owpc/pc/race/index?hd={hd}")
    return sorted(set(re.findall(r"jcd=(\d{2})", r.text))) if r is not None and r.status_code == 200 else []


def deadlines(hd, jcd):
    r = get(f"https://www.boatrace.jp/owpc/pc/race/raceindex?jcd={jcd}&hd={hd}")
    out = {}
    if r is None or r.status_code != 200:
        return out
    for tb in BeautifulSoup(r.text, "lxml").select("tbody"):
        m = re.match(r"(\d{1,2})R\s+(\d{1,2}:\d{2})", norm(tb.get_text(" ")))
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def parse_bc(text):
    """bc_oriten の中身を {header, rows, n_rows} に要約する。"""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines or not lines[0].startswith("data="):
        return {"raw_head": (text or "")[:120]}
    head = lines[2].split("\t") if len(lines) > 2 else []
    rows = [l.split("\t") for l in lines[3:]]
    filled = [r for r in rows if any(re.fullmatch(r"\d+\.\d+", c.strip()) for c in r[2:])]
    return {"meta": lines[1], "header": [norm(h) for h in head], "n_rows": len(rows), "n_filled": len(filled),
            "sample": rows[0] if rows else None}


# ---------------------------------------------------------------
def boatcast():
    res = {"today": {}, "venues_recent": {}, "past": {}}
    t = now()
    hd = t.strftime("%Y%m%d")
    # 1) 当日開催場の全レース
    for jcd in open_venues(hd):
        res["today"][jcd] = {}
        for rno in range(1, 13):
            u = BC.format(jcd=jcd, d=hd, rr=f"{rno:02d}")
            r = get(u)
            res["today"][jcd][rno] = {"status": None if r is None else r.status_code,
                                     "lm": None if r is None else r.headers.get("last-modified"),
                                     **(parse_bc(r.text) if r is not None and r.status_code == 200 else {})}
        print("[BC today]", jcd, [(k, v["status"], v.get("n_filled")) for k, v in res["today"][jcd].items()], flush=True)
    # 2) 全24場: 直近30日で開催のあった日の 1R・12R（見出しの違いを見る）
    need = {f"{i:02d}" for i in range(1, 25)} - set(res["today"])
    for back in range(1, 31):
        if not need:
            break
        d = (t - timedelta(days=back)).strftime("%Y%m%d")
        for jcd in sorted(need & set(open_venues(d))):
            rec = {}
            for rno in (1, 12):
                r = get(BC.format(jcd=jcd, d=d, rr=f"{rno:02d}"))
                rec[rno] = {"status": None if r is None else r.status_code,
                            **(parse_bc(r.text) if r is not None and r.status_code == 200 else {})}
            res["venues_recent"][jcd] = {"date": d, **rec}
            need.discard(jcd)
            print("[BC recent]", jcd, d, rec[1].get("header"), rec[1]["status"], flush=True)
    res["never_found"] = sorted(need)
    # 3) どこまでさかのぼれるか
    for back in [7, 30, 60, 90, 120, 180, 270, 365, 545, 730, 1095, 1460, 1825, 2190]:
        d = (t - timedelta(days=back)).strftime("%Y%m%d")
        vs = open_venues(d)
        rec = {}
        for jcd in vs[:3]:
            r = get(BC.format(jcd=jcd, d=d, rr="06"))
            rec[jcd] = {"status": None if r is None else r.status_code,
                        **(parse_bc(r.text) if r is not None and r.status_code == 200 else {})}
        res["past"][d] = {"open": vs, "checked": rec}
        print("[BC past]", d, {k: (v["status"], v.get("n_filled")) for k, v in rec.items()}, flush=True)
    (OUT / f"boatcast_{hd}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------
_FLOAT = re.compile(r"(?<![\d.])(\d{2}\.\d{2})(?![\d.])")


def lap_count(text):
    """一周タイムらしい値（30〜42秒）の個数。0なら未掲載とみなす。"""
    return sum(1 for v in _FLOAT.findall(norm(text)) if 30 <= float(v) <= 42)


SITE_PAGES = {
    "12": "https://www.boatrace-suminoe.jp/asp/kyogi/12/sp/yoso05{rr}.htm",
    "04": "https://www.heiwajima.gr.jp/asp/kyogi/04/pc/yoso05{rr}.htm",
    "15": "https://www.marugameboat.jp/asp/kyogi/15/sp/yoso05{rr}.htm",
    "16": "https://www.kojimaboat.jp/asp/kyogi/16/sp/yoso05{rr}.htm",
}


def monitor(end_hhmm):
    """締切40分前〜締切5分後のレースについて、BOATCAST と一部の場サイトを
    約60秒ごとに見て、オリジナル展示が初めて載った時刻を記録する。"""
    hd = now().strftime("%Y%m%d")
    end = datetime.strptime(hd + end_hhmm, "%Y%m%d%H%M").replace(tzinfo=JST)
    if end < now():
        end += timedelta(days=1)
    dl = {}
    for jcd in open_venues(hd):
        for rno, hm in deadlines(hd, jcd).items():
            dl[(jcd, rno)] = datetime.strptime(hd + hm, "%Y%m%d%H:%M").replace(tzinfo=JST)
    log = {"hd": hd, "events": [], "first": {}}
    state = {}
    fn = OUT / f"monitor_{hd}_{now().strftime('%H%M')}.json"
    while now() < end:
        loop_start = time.time()
        t = now()
        active = [(k, v) for k, v in dl.items() if v - timedelta(minutes=40) <= t <= v + timedelta(minutes=5)]
        for (jcd, rno), dline in sorted(active, key=lambda x: x[1]):
            rr = f"{rno:02d}"
            srcs = [("bc", BC.format(jcd=jcd, d=hd, rr=rr))]
            if jcd in SITE_PAGES:
                srcs.append(("site", SITE_PAGES[jcd].format(d=hd, rr=rr)))
            for src, u in srcs:
                key = f"{jcd}-{rno}-{src}"
                if log["first"].get(key, {}).get("done"):
                    continue
                r = get(u, wait=1.6)
                ts = now()
                if r is None:
                    continue
                if src == "bc":
                    p = parse_bc(r.text) if r.status_code == 200 else {}
                    filled = p.get("n_filled", 0)
                else:
                    filled = lap_count(r.text) if r.status_code == 200 else 0
                h = hashlib.md5(r.content).hexdigest()[:10]
                mins = round((dline - ts).total_seconds() / 60, 1)
                if state.get(key) != (r.status_code, filled, h):
                    state[key] = (r.status_code, filled, h)
                    log["events"].append({"key": key, "t": ts.strftime("%H:%M:%S"), "min_before": mins,
                                          "status": r.status_code, "filled": filled})
                f = log["first"].setdefault(key, {"deadline": dline.strftime("%H:%M")})
                if filled and "first_seen" not in f:
                    f["first_seen"] = ts.strftime("%H:%M:%S")
                    f["min_before"] = mins
                    f["last_empty_min_before"] = f.get("_last_empty")
                    print("[MON]", key, "first", f["first_seen"], "min_before", mins, "prev_empty", f.get("_last_empty"), flush=True)
                elif not filled:
                    f["_last_empty"] = mins
                if filled and ts > dline:
                    f["done"] = True
                if not filled and ts > dline + timedelta(minutes=4):
                    f["done"] = True
        fn.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
        time.sleep(max(5, 60 - (time.time() - loop_start)))
    fn.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------
VENUE_TOP = {
    "01": "https://www.kiryu-kyotei.com/", "02": "https://www.boatrace-toda.jp/",
    "03": "https://www.boatrace-edogawa.com/", "04": "https://www.heiwajima.gr.jp/",
    "05": "https://www.boatrace-tamagawa.com/", "06": "https://www.boatrace-hamanako.jp/",
    "07": "https://www.gamagori-kyotei.com/", "08": "https://www.boatrace-tokoname.jp/",
    "09": "https://www.boatrace-tsu.com/", "10": "https://www.boatrace-mikuni.jp/",
    "11": "https://www.boatrace-biwako.jp/", "12": "https://www.boatrace-suminoe.jp/",
    "13": "https://www.boatrace-amagasaki.jp/", "14": "https://www.n14.jp/",
    "15": "https://www.marugameboat.jp/", "16": "https://www.kojimaboat.jp/",
    "17": "https://www.boatrace-miyajima.com/", "18": "https://www.boatrace-tokuyama.jp/",
    "19": "https://www.boatrace-shimonoseki.jp/", "20": "https://www.wmb.jp/",
    "21": "https://www.boatrace-ashiya.com/", "22": "https://www.boatrace-fukuoka.com/",
    "23": "https://www.boatrace-karatsu.jp/", "24": "https://omurakyotei.jp/",
}
LINK_WORDS = re.compile(r"オリジナル展示|オリ展|周回|まわり足|回り足|直前情報|直前|展示")
TERMS_WORDS = re.compile(r"利用規約|ご利用にあたって|ご利用について|サイトポリシー|サイトのご利用|当サイトについて|著作権|免責")
RULE_WORDS = ["自動", "機械的", "ロボット", "クローラ", "スクレイピング", "プログラム", "複製", "転載", "転用", "私的", "個人的", "営利", "商用", "大量", "許諾", "禁止", "負荷"]
NET_SKIP = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|woff2?|ttf|ico|mp4|m3u8|ts)(\?|$)|google|doubleclick|facebook|twitter|yahoo|gstatic|youtube|clarity|adobe|tiktok|line-scdn|criteo", re.I)


def around(text, word, width=90, limit=4):
    out, start = [], 0
    while len(out) < limit:
        i = text.find(word, start)
        if i < 0:
            break
        out.append(text[max(0, i - width): i + len(word) + width])
        start = i + len(word)
    return out


def render():
    from playwright.sync_api import sync_playwright

    report = {}
    with sync_playwright() as p:
        br = p.chromium.launch()
        for jcd, top in VENUE_TOP.items():
            entry = {"visits": []}
            for mobile in (False, True):
                ctx = br.new_context(user_agent=UA["User-Agent"] + (" Mobile iPhone" if mobile else ""),
                                     viewport={"width": 390, "height": 844} if mobile else {"width": 1280, "height": 900},
                                     is_mobile=mobile, locale="ja-JP")
                page = ctx.new_page()
                reqs = []
                page.on("request", lambda rq: reqs.append((rq.resource_type, rq.url)) if rq.resource_type in ("xhr", "fetch", "document", "script") and not NET_SKIP.search(rq.url) else None)
                visited = set()

                def visit(url, via):
                    if url in visited or len(visited) >= 9:
                        return None
                    visited.add(url)
                    time.sleep(WAIT)
                    reqs.clear()
                    try:
                        page.goto(url, wait_until="networkidle", timeout=40000)
                    except Exception as e:
                        try:
                            page.wait_for_timeout(3000)
                        except Exception:
                            pass
                        entry["visits"].append({"url": url, "via": via, "error": f"{type(e).__name__}"})
                    try:
                        txt = norm(page.inner_text("body"))
                    except Exception:
                        txt = ""
                    links = page.eval_on_selector_all("a[href]", "els => els.map(e => [ (e.innerText||e.title||(e.querySelector('img')||{}).alt||'').trim(), e.href ])")
                    imgs = page.eval_on_selector_all("img", "els => els.map(e => [e.alt||'', e.src])")
                    v = {"url": url, "final": page.url, "via": via, "mobile": mobile, "title": page.title()[:60],
                         "words": {w: txt.count(w) for w in ["オリジナル展示", "一周", "1周", "周回", "まわり足", "回り足", "直線", "半周", "展示タイム"] if w in txt},
                         "laps": lap_count(txt),
                         "ctx": around(txt, "まわり足", 120, 2) + around(txt, "回り足", 120, 1) + around(txt, "オリジナル展示", 120, 2),
                         "orig_imgs": [i for i in imgs if re.search(r"tenji|oriten|orgten|original|chokuzen|展示", " ".join(i), re.I)][:8],
                         "net": [r for r in reqs if r[0] in ("xhr", "fetch") or re.search(r"tenji|oriten|orgten|original|chokuzen|cyokuzen|boatcast|json|txt", r[1], re.I)][:40]}
                    entry["visits"].append(v)
                    if v["laps"] >= 3 or v["words"].get("まわり足") or v["words"].get("回り足"):
                        tag = f"{jcd}_{'sp' if mobile else 'pc'}_{len(entry['visits'])}"
                        (OUT / "html" / f"r{tag}.html").write_text(f"<!-- {page.url} -->\n" + page.content(), encoding="utf-8")
                        try:
                            page.screenshot(path=str(OUT / "shots" / f"{tag}.png"), full_page=True)
                        except Exception:
                            pass
                        v["saved"] = tag
                    return links

                start = top + ("sp/" if mobile and jcd not in ("17", "24", "02") else "")
                links = visit(start, "top") or []
                terms = [(t, h) for t, h in links if TERMS_WORDS.search(t or "")]
                entry.setdefault("terms_links", []).extend(terms[:6])
                cands = []
                for t, h in links:
                    t = norm(t)
                    if not h.startswith("http") or NET_SKIP.search(h) or "javascript" in h:
                        continue
                    sc = 3 if re.search(r"オリジナル展示|まわり足|回り足|周回", t) else 2 if "直前" in t else 1 if "展示" in t and "リプレイ" not in t else 0
                    if sc:
                        cands.append((sc, t, h))
                cands = sorted(dict.fromkeys(cands), key=lambda x: -x[0])
                for sc, t, h in cands[:5]:
                    sub = visit(h, t) or []
                    # 1段だけ深く: 直前ページ内の「オリジナル展示」タブ・レース番号リンク
                    for t2, h2 in sub:
                        t2 = norm(t2)
                        if re.search(r"オリジナル展示|まわり足|周回|オリ展", t2) and h2.startswith("http"):
                            visit(h2, t + " > " + t2)
                            break
                ctx.close()
            # 規約ページ本文
            tpages = []
            seen = set()
            for t, h in entry.get("terms_links", []):
                if h in seen or not h.startswith("http"):
                    continue
                seen.add(h)
                r = get(h)
                if r is None or r.status_code != 200:
                    tpages.append({"url": h, "status": None if r is None else r.status_code})
                    continue
                txt = norm(BeautifulSoup(r.text, "lxml").get_text(" "))
                (OUT / "terms").mkdir(exist_ok=True)
                (OUT / "terms" / f"{jcd}_{len(tpages)}.txt").write_text(f"{r.url}\n\n{txt[:30000]}", encoding="utf-8")
                tpages.append({"url": h, "link_text": t, "len": len(txt),
                               "hits": {w: around(txt, w, 70, 3) for w in RULE_WORDS if w in txt}})
                if len(tpages) >= 3:
                    break
            entry["terms_pages"] = tpages
            report[jcd] = entry
            hits = [(v.get("final"), v.get("words"), v.get("laps")) for v in entry["visits"] if v.get("saved")]
            print(f"[R] {jcd} visits={len(entry['visits'])} hits={hits[:3]} terms={len(tpages)}", flush=True)
            (OUT / "render.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        # BOATCAST 本体（どのテキストを読んでいるか・規約）
        ctx = br.new_context(locale="ja-JP", user_agent=UA["User-Agent"])
        page = ctx.new_page()
        reqs = []
        page.on("request", lambda rq: reqs.append(rq.url) if rq.resource_type in ("xhr", "fetch") else None)
        bc = {}
        for u in ["https://race.boatcast.jp/", "https://race.boatcast.jp/?jo=20"]:
            time.sleep(WAIT)
            reqs.clear()
            try:
                page.goto(u, wait_until="networkidle", timeout=40000)
                page.wait_for_timeout(4000)
            except Exception as e:
                bc[u] = {"error": type(e).__name__}
                continue
            links = page.eval_on_selector_all("a[href]", "els => els.map(e => [(e.innerText||'').trim(), e.href])")
            bc[u] = {"final": page.url, "net": sorted(set(reqs))[:80], "links": links[:120]}
        (OUT / "render_boatcast.json").write_text(json.dumps(bc, ensure_ascii=False, indent=1), encoding="utf-8")
        br.close()


def bcpast():
    """BOATCAST の過去分がいつまで残っているかを月単位で絞り込む。"""
    res = {}
    t = now()
    for back in list(range(330, 800, 30)):
        d = (t - timedelta(days=back)).strftime("%Y%m%d")
        vs = open_venues(d)
        rec = {}
        for jcd in [v for v in vs if v != "03"][:2]:
            r = get(BC.format(jcd=jcd, d=d, rr="06"))
            rec[jcd] = None if r is None else r.status_code
        res[d] = rec
        print("[BCPAST]", d, rec, flush=True)
    (OUT / "boatcast_past.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


def coverage(days=3):
    """直近の開催日について、全場・全レースの bc_oriten が揃っているかを数える。"""
    res = {}
    t = now()
    for back in range(1, days + 1):
        d = (t - timedelta(days=back)).strftime("%Y%m%d")
        for jcd in open_venues(d):
            rec = {}
            for rno in range(1, 13):
                r = get(BC.format(jcd=jcd, d=d, rr=f"{rno:02d}"))
                p = parse_bc(r.text) if r is not None and r.status_code == 200 else {}
                rec[rno] = [None if r is None else r.status_code, p.get("n_filled"), p.get("n_rows")]
            res[f"{d}_{jcd}"] = rec
            print("[COV]", d, jcd, [v[1] for v in rec.values()], flush=True)
    (OUT / "boatcast_coverage.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


def sitecheck():
    """開催中の場のサイト（PC・スマホのトップと直前情報ページ）を描画し、
    オリジナル展示の欄が文字（HTML）か画像かを確かめる。"""
    from playwright.sync_api import sync_playwright

    hd = now().strftime("%Y%m%d")
    vs = open_venues(hd)
    extra = {
        "07": ["https://www.gamagori-kyotei.com/asp/gamagori/kyogi/kyogihtml/index.htm"],
        "24": ["https://omurakyotei.jp/yosou/chokuzen.php", "https://omurakyotei.jp/yosou/pitreport.php"],
        "17": ["https://www.boatrace-miyajima.com/racecard.html"],
        "02": ["https://www.boatrace-toda.jp/race/shusso_list.html"],
    }
    res = {}
    with sync_playwright() as p:
        br = p.chromium.launch()
        for jcd in vs:
            top = VENUE_TOP[jcd]
            urls = [top, top + "sp/", top + "sp/index.php?page=yosou-cyokuzen", top + "modules/yosou/?mode=cyokuzen"] + extra.get(jcd, [])
            if jcd in ("04", "12", "15", "16"):
                urls += [f"{top}asp/kyogi/{jcd}/pc/yoso0501.htm", f"{top}asp/kyogi/{jcd}/sp/yoso0501.htm"]
            out = []
            for i, u in enumerate(urls):
                mobile = "/sp/" in u
                ctx = br.new_context(user_agent=UA["User-Agent"] + (" Mobile iPhone" if mobile else ""), is_mobile=mobile,
                                     viewport={"width": 390, "height": 844} if mobile else {"width": 1280, "height": 900}, locale="ja-JP")
                page = ctx.new_page()
                reqs = []
                page.on("request", lambda rq: reqs.append(rq.url) if rq.resource_type in ("xhr", "fetch") and not NET_SKIP.search(rq.url) else None)
                time.sleep(WAIT)
                try:
                    page.goto(u, wait_until="networkidle", timeout=40000)
                except Exception:
                    page.wait_for_timeout(3000)
                try:
                    txt = norm(page.inner_text("body"))
                except Exception:
                    txt = ""
                # 「オリジナル展示」見出しの近くにある要素が表か画像か
                info = page.evaluate("""() => {
                    const hs = [...document.querySelectorAll('body *')].filter(e => e.children.length < 3 && /オリジナル展示|まわり足|回り足/.test(e.textContent || '') && (e.textContent||'').length < 40);
                    return hs.slice(0, 4).map(h => { let c = h; for (let k = 0; k < 5 && c; k++) { if (c.querySelector && (c.querySelector('table') || c.querySelector('img'))) break; c = c.parentElement; }
                      return {head: (h.textContent||'').trim().slice(0, 40), tables: c ? c.querySelectorAll('table').length : 0,
                              imgs: c ? [...c.querySelectorAll('img')].map(i => i.src).slice(0, 5) : [], canvas: c ? c.querySelectorAll('canvas').length : 0,
                              text: c ? (c.innerText||'').replace(/\\s+/g, ' ').slice(0, 600) : ''}; });
                }""")
                rec = {"url": u, "final": page.url, "laps": lap_count(txt), "info": info,
                       "net": [r for r in reqs if not re.search(r"microad|analytics", r)][:20],
                       "ctx": around(txt, "オリジナル展示", 200, 2) + around(txt, "まわり足", 200, 1) + around(txt, "回り足", 200, 1)}
                if rec["laps"] >= 3 or info:
                    tag = f"{jcd}_s{i}"
                    try:
                        page.screenshot(path=str(OUT / "shots" / f"{tag}.png"), full_page=True)
                    except Exception:
                        pass
                    (OUT / "html" / f"s{tag}.html").write_text(f"<!-- {page.url} -->\n" + page.content(), encoding="utf-8")
                    rec["saved"] = tag
                out.append(rec)
                ctx.close()
            res[jcd] = out
            print("[SITE]", jcd, [(o["final"][-50:], o["laps"], len(o["info"])) for o in out], flush=True)
            (OUT / f"sitecheck_{hd}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        br.close()


def jsgrep():
    """共通CMSの直前情報ページが読む JS から ajax の URL とパラメータを拾う。"""
    res = {}
    for f in sorted((OUT / "html").glob("*_f*.html")):
        jcd = f.name[:2]
        html = f.read_text(encoding="utf-8", errors="ignore")
        base = re.search(r"<!-- (\S+) -->", html).group(1)
        soup = BeautifulSoup(html, "lxml")
        found = set(re.findall(r"[\w/\-.]*ajax[\w/\-.]*\.php[^'\"\s]*", html))
        for s in soup.find_all("script", src=True):
            src = requests.compat.urljoin(base, s["src"])
            if re.search(r"yosou|cyokuzen|chokuzen|race", src) and not re.search(r"jquery|google", src):
                r = get(src)
                if r is not None and r.status_code == 200:
                    for m in re.finditer(r"[\w/\-.]*ajax[\w/\-.]*\.php", r.text):
                        found.add(m.group(0))
                        found.add(r.text[max(0, m.start() - 200): m.end() + 300].replace("\n", " "))
        res.setdefault(jcd, {})[f.name] = sorted(found)[:20]
    (OUT / "jsgrep.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "boatcast":
        boatcast()
    elif cmd == "monitor":
        monitor(sys.argv[2])
    elif cmd == "bcpast":
        bcpast()
    elif cmd == "coverage":
        coverage()
    elif cmd == "sitecheck":
        sitecheck()
    elif cmd == "jsgrep":
        jsgrep()
    elif cmd == "render":
        render()
