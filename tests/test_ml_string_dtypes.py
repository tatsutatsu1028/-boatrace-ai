"""
新しい予想モデルの直前データの経路（ml_live / ml_features.live_table）で、文字列の列の保存形式が
違っても結合できることの確認（データ用リポジトリは不要）。

pandas 3 の文字列型は pyarrow がある環境（Streamlit 本番）では pyarrow 保存、無い環境（GitHub Actions）
では python 保存になる。Actions で作って pickle した集計表を本番で読むと、結合で
「MergeError: incompatible merge keys」になっていた（2026-10-15）。

  python tests/test_ml_string_dtypes.py   （pytest でも動く）
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_features as mf  # noqa: E402

PY = pd.StringDtype("python", na_value=np.nan)


def _python_str(values):
    return pd.Series(values, dtype=PY)


def test_unify_strings_makes_mergeable():
    left = pd.DataFrame({"racer_id": pd.Series(["4001", "4002"]).astype("str"), "x": [1, 2]})
    right = pd.DataFrame({"racer_id": _python_str(["4001", np.nan]), "y": [3, 4]})
    mf.unify_strings(right)
    out = left.merge(right, on="racer_id", how="left")
    assert out["y"].tolist()[0] == 3
    assert pd.isna(right["racer_id"].iloc[1]), "欠損が文字列 'nan' になってはいけない"


def test_asof_with_python_storage_state():
    """本番で落ちていた結合（merge_asof の by。保存形式が違うと MergeError）。"""
    targets = pd.DataFrame({
        "racer_id": pd.Series(["4001", "4002"]).astype("str"),
        "d": pd.to_datetime(["2026-10-15", "2026-10-15"]),
    })
    cum = pd.DataFrame({
        "racer_id": _python_str(["4001", "4002"]),
        "d": pd.to_datetime(["2026-10-10", "2026-10-11"]),
        "n": [5.0, 7.0],
    })
    got = mf._asof(targets, cum, ["racer_id"], ["n"])
    assert got[:, 0].tolist() == [5.0, 7.0]


def test_unify_state():
    state = {"as_of": pd.Timestamp("2026-10-15"),
             "racer": pd.DataFrame({"racer_id": _python_str(["4001"]), "n": [1.0]})}
    mf.unify_state(state)
    assert state["racer"]["racer_id"].dtype == pd.Series(["a"]).astype("str").dtype


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
