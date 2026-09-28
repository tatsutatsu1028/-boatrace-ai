"""
過去データ（モデル作り直し用）の保存先。

リポジトリ内の data/history/<種類>/<年>/<種類>_<年>-<月>.csv(.gz) に
月ごとに分けて保存する。1ファイルが100MBを超えないようにするため。

- 集めている途中の月は非圧縮CSV（追記するだけなので、Gitの差分が小さい）
- 集め終わった月は seal() で .csv.gz に圧縮して置き換える

読み込みは read_kind() を使えば、非圧縮・圧縮のどちらも区別なく読める。
Supabaseには保存しない（容量・転送量を増やさないため）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from data_paths import data_path

# データ用リポジトリの data/history（data_paths.py 参照）。
ROOT = data_path("data") / "history"

# 種類ごとの「1行を一意に決める列」。追記時の重複除去に使う。
KEYS = {
    "k_results": ["race_key", "lane"],     # 競走成績（1艇1行）
    "k_payouts": ["race_key", "bet_type", "combo"],  # 払戻（1券種・1組番1行）
    "b_programs": ["race_key", "lane"],    # 番組表（1艇1行）
    "pages": ["race_key", "lane"],         # 出走表・直前情報ページ（1艇1行）
}


def _month_of(race_date):
    s = str(race_date)
    return s[:4], s[4:6]


def month_paths(kind, year, month):
    d = ROOT / kind / str(year)
    base = f"{kind}_{year}-{month}"
    return d / f"{base}.csv", d / f"{base}.csv.gz"


def _read(path):
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def read_month(kind, year, month):
    csv, gz = month_paths(kind, year, month)
    frames = [_read(p) for p in (gz, csv) if p.exists()]
    frames = [f for f in frames if len(f)]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def list_months(kind):
    base = ROOT / kind
    if not base.exists():
        return []
    out = set()
    for p in base.glob("*/*.csv*"):
        stem = p.name.split(".")[0]          # kind_YYYY-MM
        ym = stem.rsplit("_", 1)[-1]
        y, m = ym.split("-")
        out.add((y, m))
    return sorted(out)


def read_kind(kind, start=None, end=None, columns=None):
    """全月を読み込む。start/end は YYYYMMDD（含む）。"""
    frames = []
    for y, m in list_months(kind):
        if start and f"{y}{m}" < str(start)[:6]:
            continue
        if end and f"{y}{m}" > str(end)[:6]:
            continue
        df = read_month(kind, y, m)
        if len(df):
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=columns or [])
    df = pd.concat(frames, ignore_index=True)
    if "race_date" in df.columns:
        if start:
            df = df[df["race_date"] >= str(start)]
        if end:
            df = df[df["race_date"] <= str(end)]
    keys = [k for k in KEYS.get(kind, []) if k in df.columns]
    if keys:
        df = df.drop_duplicates(keys, keep="last")
    if columns:
        df = df.reindex(columns=columns)
    return df.reset_index(drop=True)


def existing_race_keys(kind):
    keys = set()
    for y, m in list_months(kind):
        for p in month_paths(kind, y, m):
            if p.exists():
                try:
                    s = pd.read_csv(p, usecols=["race_key"], dtype=str)["race_key"]
                    keys.update(s.dropna().tolist())
                except (ValueError, pd.errors.EmptyDataError):
                    pass
    return keys


def append(kind, df):
    """行を月ごとのファイルへ追記する。列が増えた場合はその月だけ書き直す。"""
    if df is None or len(df) == 0:
        return 0
    df = df.copy()
    df["race_date"] = df["race_date"].astype(str)
    n = 0
    for (y, m), part in df.groupby(df["race_date"].map(_month_of)):
        csv, gz = month_paths(kind, y, m)
        csv.parent.mkdir(parents=True, exist_ok=True)
        part = part.astype(object)
        if gz.exists() and not csv.exists():
            # 圧縮済みの月へ後から追加する場合（再試行で取れた等）は、
            # 圧縮を解いて非圧縮CSVに戻す。次の seal() で再び圧縮する。
            old = _read(gz)
            old.to_csv(csv, index=False, encoding="utf-8")
            gz.unlink()
        if csv.exists():
            header = list(pd.read_csv(csv, nrows=0).columns)
            added = [c for c in part.columns if c not in header]
            if added:
                old = _read(csv)
                merged = pd.concat([old, part], ignore_index=True)
                merged.reindex(columns=header + added).to_csv(csv, index=False, encoding="utf-8")
            else:
                part.reindex(columns=header).to_csv(
                    csv, mode="a", header=False, index=False, encoding="utf-8"
                )
        else:
            part.to_csv(csv, index=False, encoding="utf-8")
        n += len(part)
    return n


def seal(kind, year, month):
    """集め終わった月を圧縮する（重複を除き、日付順に並べ直す）。"""
    csv, gz = month_paths(kind, year, month)
    if not csv.exists():
        return False
    df = read_month(kind, year, month)
    keys = [k for k in KEYS.get(kind, []) if k in df.columns]
    if keys:
        df = df.drop_duplicates(keys, keep="last")
    sort_cols = [c for c in ("race_date", "jcd", "race_no", "lane", "bet_type") if c in df.columns]
    if sort_cols:
        tmp = df.copy()
        for c in ("race_no", "lane"):
            if c in tmp.columns:
                tmp[c] = pd.to_numeric(tmp[c], errors="coerce")
        df = df.loc[tmp.sort_values(sort_cols).index]
    tmp_gz = gz.with_suffix(".gz.tmp")
    df.to_csv(tmp_gz, index=False, encoding="utf-8", compression="gzip")
    os.replace(tmp_gz, gz)
    csv.unlink()
    return True


def total_size_mb():
    if not ROOT.exists():
        return 0.0
    return sum(p.stat().st_size for p in ROOT.rglob("*") if p.is_file()) / 1e6
