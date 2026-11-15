"""frame_codec.py: 予想の入力・結果を JSON でやり取りしても、型・欠損・並び・attrs まで元どおりに戻ること。"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pandas.testing as pdt

import frame_codec as fc


def _round(obj):
    return fc.decode(json.loads(json.dumps(fc.encode(obj), allow_nan=False)))


def test_dataframe_dtypes_nan_index_and_attrs():
    df = pd.DataFrame({
        "lane": [1, 2, 3],
        "p": [0.5, np.nan, float("inf")],
        "name": ["a", None, "c"],
        "cls": pd.Series(["A1", np.nan, "B1"], dtype="str"),
        "mixed": pd.Series([1, "x", None], dtype=object),
        "flag": [True, False, True],
        "n": pd.array([1, None, 3], dtype="Int64"),
    })
    df.attrs["_boat_ai_lane_agnostic"] = {"strongest_lane": 1, "strongest_prob": 0.41}
    df.attrs["_boat_ai_confidence_label"] = "A"
    out = _round(df)
    pdt.assert_frame_equal(df, out, check_exact=True)
    assert out.attrs == df.attrs
    assert list(out.dtypes.astype(str)) == list(df.dtypes.astype(str))


def test_non_range_index_and_empty_frame():
    df = pd.DataFrame({"combo": ["1-2-3", "2-1-3"], "odds": [5.1, 7.2]}, index=[5, 2])
    pdt.assert_frame_equal(df, _round(df), check_exact=True)
    empty = pd.DataFrame(columns=["combo", "odds"])
    pdt.assert_frame_equal(empty, _round(empty), check_exact=True, check_index_type=False)


def test_nested_structures():
    obj = {
        "deadlines": {1: "10:30", 12: "16:45"},
        "plan": {"point_count": 9, "main_n": 4, "min_second_coverage": None},
        "pair": (1, 2.5),
        "arr": np.array([1.0, np.nan]),
        "none": None,
        "frames": {"今までのモデル": pd.DataFrame({"lane": [1], "p_first": [0.3]})},
    }
    out = _round(obj)
    assert out["deadlines"] == {1: "10:30", 12: "16:45"}
    assert out["plan"] == obj["plan"]
    assert out["pair"] == (1, 2.5)
    np.testing.assert_array_equal(out["arr"], obj["arr"])
    assert out["none"] is None
    pdt.assert_frame_equal(out["frames"]["今までのモデル"], obj["frames"]["今までのモデル"], check_exact=True)
