"""
予想の入力・結果（DataFrame を含む dict）を JSON（Supabase の jsonb）でやり取りするための変換。

アプリ → GitHub Actions には予想に使う表（work・オッズ）と設定を、Actions → アプリには予想の結果
（final・買い目・比較用の予想・Claude の予想）を送る。予想・買い目が変わらないよう、列の型・欠損（NaN）・
行の並び・attrs（信頼度・艇番なしモデルの判定）まで元どおりに戻す（tests/test_frame_codec.py）。
pickle は使わない（データベースの中身をそのまま実行しない）。
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

_DF = "$df"


def _enc_scalar(v):
    if v is None:
        return None
    if v is pd.NA:
        return {"$na": 1}
    if v is pd.NaT:
        return {"$nat": 1}
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        if math.isnan(f):
            return {"$f": "nan"}
        if math.isinf(f):
            return {"$f": "inf" if f > 0 else "-inf"}
        return f
    if isinstance(v, str):
        return v
    if isinstance(v, pd.Timestamp):
        return {"$ts": v.isoformat()}
    if isinstance(v, np.datetime64):
        return {"$ts": pd.Timestamp(v).isoformat()}
    return encode(v)


def _dec_scalar(v):
    if isinstance(v, dict):
        if "$f" in v:
            return float(v["$f"])
        if "$na" in v:
            return pd.NA
        if "$nat" in v:
            return pd.NaT
        if "$ts" in v:
            return pd.Timestamp(v["$ts"])
        return decode(v)
    if isinstance(v, list):
        return decode(v)
    return v


def encode(obj):
    """JSON にできる形へ（DataFrame・numpy・NaN・tuple を印付きで残す）。"""
    if isinstance(obj, pd.DataFrame):
        cols = list(obj.columns)
        return {_DF: {
            "columns": [_enc_scalar(c) for c in cols],
            "dtypes": [str(obj[c].dtype) for c in cols] if obj.columns.is_unique else [str(t) for t in obj.dtypes],
            "index": [_enc_scalar(i) for i in obj.index],
            "index_dtype": str(obj.index.dtype),
            "range_index": isinstance(obj.index, pd.RangeIndex),
            "data": [[_enc_scalar(v) for v in obj.iloc[:, j].tolist()] for j in range(obj.shape[1])],
            "attrs": encode(dict(obj.attrs)),
        }}
    if isinstance(obj, pd.Series):
        return {"$series": encode(obj.to_frame(name=obj.name if obj.name is not None else 0))}
    if isinstance(obj, dict):
        if all(isinstance(k, str) for k in obj):
            return {"$dict": {k: encode(v) for k, v in obj.items()}}
        return {"$items": [[_enc_scalar(k), encode(v)] for k, v in obj.items()]}
    if isinstance(obj, tuple):
        return {"$tuple": [encode(v) for v in obj]}
    if isinstance(obj, list):
        return [encode(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return {"$nd": [_enc_scalar(v) for v in obj.tolist()], "dtype": str(obj.dtype)}
    return _enc_scalar(obj)


def _restore_dtype(col, dtype):
    if str(col.dtype) == dtype:
        return col
    try:
        return col.astype(dtype)
    except (TypeError, ValueError):
        return col


def decode(obj):
    """encode() の逆。"""
    if isinstance(obj, list):
        return [decode(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    if _DF in obj:
        d = obj[_DF]
        cols = [_dec_scalar(c) for c in d["columns"]]
        data = {}
        series = []
        for j, (c, dtype, values) in enumerate(zip(cols, d["dtypes"], d["data"])):
            vals = [_dec_scalar(v) for v in values]
            if dtype in ("object",):
                s = pd.Series(vals, dtype=object)
            else:
                s = pd.Series(vals, dtype=object)
                s = _restore_dtype(s.infer_objects() if not dtype.startswith(("str", "string")) else s, dtype)
            series.append(s)
            data[j] = s
        df = pd.concat(series, axis=1) if series else pd.DataFrame(index=range(len(d["index"])))
        df.columns = pd.Index(cols, dtype=object) if any(not isinstance(c, str) for c in cols) else cols
        if d.get("range_index"):
            idx = d["index"]
            if idx and idx == list(range(idx[0], idx[0] + len(idx))):
                df.index = pd.RangeIndex(idx[0], idx[0] + len(idx))
            else:
                df.index = pd.Index([_dec_scalar(i) for i in idx])
        else:
            df.index = _restore_dtype(pd.Index([_dec_scalar(i) for i in d["index"]]).to_series(), d["index_dtype"])
            df.index = pd.Index(df.index)
        df.attrs.update(decode(d.get("attrs")) or {})
        return df
    if "$series" in obj:
        f = decode(obj["$series"])
        s = f.iloc[:, 0]
        return s
    if "$dict" in obj:
        return {k: decode(v) for k, v in obj["$dict"].items()}
    if "$items" in obj:
        return {_dec_scalar(k): decode(v) for k, v in obj["$items"]}
    if "$tuple" in obj:
        return tuple(decode(v) for v in obj["$tuple"])
    if "$nd" in obj:
        return np.array([_dec_scalar(v) for v in obj["$nd"]], dtype=obj.get("dtype"))
    return _dec_scalar(obj)


def dumps(obj):
    return json.dumps(encode(obj), ensure_ascii=False, allow_nan=False)


def loads(text):
    return decode(json.loads(text) if isinstance(text, str) else text)
