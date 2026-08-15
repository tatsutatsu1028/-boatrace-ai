
from __future__ import annotations
import itertools, re
from io import StringIO
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://www.boatrace.jp/owpc/pc/race"
UA = {"User-Agent":"Mozilla/5.0 (compatible; BoatraceAIMobile/2.0; personal-analysis-tool)"}

VENUES = {
"01":"桐生","02":"戸田","03":"江戸川","04":"平和島","05":"多摩川","06":"浜名湖",
"07":"蒲郡","08":"常滑","09":"津","10":"三国","11":"びわこ","12":"住之江",
"13":"尼崎","14":"鳴門","15":"丸亀","16":"児島","17":"宮島","18":"徳山",
"19":"下関","20":"若松","21":"芦屋","22":"福岡","23":"唐津","24":"大村"
}

def _get(path, date_yyyymmdd, jcd, rno):
    url = f"{BASE}/{path}?hd={date_yyyymmdd}&jcd={jcd}&rno={int(rno)}"
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text, url

def _tables(html):
    return pd.read_html(StringIO(html))

def fetch_beforeinfo(date_yyyymmdd, jcd, rno):
    html, url = _get("beforeinfo", date_yyyymmdd, jcd, rno)
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    out = pd.DataFrame({"lane":range(1,7)})
    tables = _tables(html)
    # Find the 6-row table containing 展示タイム/体重/チルト.
    target = None
    for t in tables:
        flat = " ".join(map(str, t.columns.tolist())) + " " + " ".join(map(str, t.astype(str).head(10).values.ravel()))
        if "展示" in flat and "タイム" in flat and len(t) >= 6:
            target = t.copy()
            break

    if target is not None:
        target.columns = ["_".join([str(x) for x in c if str(x)!="nan"]) if isinstance(c, tuple) else str(c) for c in target.columns]
        # Identify likely columns by names; fallback to numeric patterns.
        colmap={}
        for c in target.columns:
            if "枠" in c: colmap["lane"]=c
            if "体重" in c and "調整" not in c: colmap["weight"]=c
            if "展示" in c and "タイム" in c: colmap["exhibition_time"]=c
            if "チルト" in c: colmap["tilt"]=c
            if "ボートレーサー" in c or "レーサー" in c: colmap["racer_name"]=c
        temp = pd.DataFrame()
        if "lane" in colmap:
            temp["lane"] = pd.to_numeric(target[colmap["lane"]], errors="coerce")
        else:
            temp["lane"] = range(1, len(target)+1)
        for k in ("weight","exhibition_time","tilt"):
            if k in colmap:
                temp[k] = pd.to_numeric(target[colmap[k]].astype(str).str.extract(r"(-?\d+(?:\.\d+)?)")[0], errors="coerce")
        if "racer_name" in colmap:
            temp["racer_name"] = target[colmap["racer_name"]].astype(str)
        temp = temp[temp["lane"].between(1,6)].drop_duplicates("lane")
        out = out.merge(temp, on="lane", how="left")

    # Weather text fallback.
    def grab(pattern):
        m=re.search(pattern,text)
        return float(m.group(1)) if m else np.nan
    out["temperature"] = grab(r"気温\s*([0-9.]+)")
    out["wind_speed"] = grab(r"風速\s*([0-9.]+)")
    out["water_temperature"] = grab(r"水温\s*([0-9.]+)")
    out["wave_height"] = grab(r"波高\s*([0-9.]+)")

    # Start exhibition: text pattern is imperfect but useful; users can edit in UI.
    # F.08 becomes -0.08, .14 becomes 0.14
    sts = re.findall(r"(?:F\s*)?\.?\d{1,2}", text[text.find("スタート展示"):]) if "スタート展示" in text else []
    parsed=[]
    for s in sts[:6]:
        s=s.replace(" ","")
        if s.startswith("F"):
            num=float("0."+re.sub(r"\D","",s))
            parsed.append(-num)
        else:
            dig=re.sub(r"\D","",s)
            parsed.append(float("0."+dig) if dig else np.nan)
    if len(parsed)==6:
        out["exhibition_st"] = parsed
    else:
        out["exhibition_st"] = np.nan
    out["source_beforeinfo"] = url
    return out

def fetch_racelist(date_yyyymmdd, jcd, rno):
    html, url = _get("racelist", date_yyyymmdd, jcd, rno)
    tables=_tables(html)
    target=None
    for t in tables:
        flat=" ".join(map(str,t.columns.tolist()))+" "+" ".join(map(str,t.astype(str).head(10).values.ravel()))
        if ("全国" in flat or "当地" in flat or "勝率" in flat) and len(t)>=6:
            target=t.copy(); break
    out=pd.DataFrame({"lane":range(1,7)})
    if target is None:
        out["source_racelist"]=url
        return out
    target.columns=["_".join([str(x) for x in c if str(x)!="nan"]) if isinstance(c,tuple) else str(c) for c in target.columns]
    temp=pd.DataFrame({"lane":range(1,min(6,len(target))+1)})
    # Best-effort extraction by headers.
    for c in target.columns:
        cs=str(c)
        vals=target[c].astype(str)
        if ("レーサー" in cs or "ボートレーサー" in cs) and "racer_name" not in temp:
            temp["racer_name"]=vals.iloc[:len(temp)].str.replace(r"\s+"," ",regex=True)
        elif "全国" in cs and "勝率" in cs and "racer_win_rate" not in temp:
            temp["racer_win_rate"]=pd.to_numeric(vals.str.extract(r"(\d+\.\d+)")[0],errors="coerce").iloc[:len(temp)].to_numpy()
        elif ("当地" in cs or "当地" in " ".join(vals.iloc[:3])) and "勝率" in cs and "local_win_rate" not in temp:
            temp["local_win_rate"]=pd.to_numeric(vals.str.extract(r"(\d+\.\d+)")[0],errors="coerce").iloc[:len(temp)].to_numpy()
        elif "モーター" in cs and ("2連" in cs or "２連" in cs) and "motor_2ren" not in temp:
            temp["motor_2ren"]=pd.to_numeric(vals.str.extract(r"(\d+\.\d+)")[0],errors="coerce").iloc[:len(temp)].to_numpy()
        elif "ボート" in cs and ("2連" in cs or "２連" in cs) and "boat_2ren" not in temp:
            temp["boat_2ren"]=pd.to_numeric(vals.str.extract(r"(\d+\.\d+)")[0],errors="coerce").iloc[:len(temp)].to_numpy()
    out=out.merge(temp,on="lane",how="left")
    out["source_racelist"]=url
    return out

def fetch_odds3t(date_yyyymmdd, jcd, rno):
    html, url = _get("odds3t", date_yyyymmdd, jcd, rno)
    tables=_tables(html)
    # BOAT RACE's 3T table is laid out in six first-boat blocks.
    # Parse triples of integer boat labels + following numeric odds from cell stream.
    candidates=[]
    for t in tables:
        vals=[str(x).strip() for x in t.astype(str).values.ravel()]
        nums=[]
        for x in vals:
            if re.fullmatch(r"[1-6]",x):
                nums.append(("boat",int(x)))
            elif re.fullmatch(r"\d+(?:\.\d+)?",x):
                v=float(x)
                nums.append(("num",v))
        # Heuristic state machine: locate valid distinct boat,boat,boat,odds sequences
        for i in range(len(nums)-3):
            a,b,c,d=nums[i:i+4]
            if a[0]==b[0]==c[0]=="boat" and d[0]=="num":
                boats=(a[1],b[1],c[1])
                if len(set(boats))==3 and d[1]>=1:
                    candidates.append((f"{boats[0]}-{boats[1]}-{boats[2]}",d[1]))
    # Keep the first plausible value for each combo.
    seen={}
    for combo,odd in candidates:
        seen.setdefault(combo,odd)
    out=pd.DataFrame([{"combo":k,"odds":v} for k,v in seen.items()])
    if len(out)<60:
        # Don't pretend weak parsing is complete.
        out=pd.DataFrame(columns=["combo","odds"])
    out.attrs["source"]=url
    return out

def fetch_official_race(date_yyyymmdd, jcd, rno):
    base = fetch_racelist(date_yyyymmdd,jcd,rno)
    before = fetch_beforeinfo(date_yyyymmdd,jcd,rno)
    out = base.merge(before.drop(columns=["source_beforeinfo"],errors="ignore"),on="lane",how="outer")
    out["venue"] = VENUES[str(jcd).zfill(2)]
    out["race_no"] = int(rno)
    out["date"] = pd.to_datetime(date_yyyymmdd).strftime("%Y-%m-%d")
    out["source_beforeinfo"] = before["source_beforeinfo"].iloc[0] if len(before) else ""
    return out.sort_values("lane").reset_index(drop=True)
