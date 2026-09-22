"""コース（艇番）ごとの基準着順を history_full.csv から算出する。

今節成績のコース補正（current_meet_avg_finish_adjusted）で使う
「そのコースなら普通どれくらいの着順か」という基準値を提供する。

注意:
  history_full.csv の finish 列は 1〜3位のみ実際の着順で、
  4位以下はまとめて 4 として記録されている
  （collect_history.py の finish_map 参照。公式サイトの3連単確定結果には
  4〜6着の区別が出ないため）。そのため、この基準値は
  「1〜3位に入れたかどうか」までを反映した疑似着順の平均であり、
  真の1〜6着まで区別した平均着順ではない。コース間の相対的な
  強弱（1号艇が良く、5・6号艇が悪い、という傾向）を捉える目的では
  妥当だが、絶対値をライブ取得の今節平均着順（真の1〜6着）と
  単純比較する際はこの違いを踏まえること。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

_DEFAULT_HISTORY_PATH = Path(__file__).parent / "history_full.csv"

# history_full.csv が読めない場合のフォールバック値。
# 2026年6月〜9月分の history_full.csv (約5,600走/コース) から算出。
_FALLBACK_BASELINE = {
    1: 1.92,
    2: 2.88,
    3: 2.99,
    4: 3.18,
    5: 3.42,
    6: 3.61,
}


@lru_cache(maxsize=4)
def _load_baseline(path_str):
    path = Path(path_str)
    if not path.exists():
        return None

    try:
        df = pd.read_csv(path, usecols=["lane", "finish"])
    except Exception:
        return None

    lane = pd.to_numeric(df["lane"], errors="coerce")
    finish = pd.to_numeric(df["finish"], errors="coerce")
    ok = lane.between(1, 6) & finish.notna()
    df = pd.DataFrame({"lane": lane[ok].astype(int), "finish": finish[ok]})

    if df.empty:
        return None

    grouped = df.groupby("lane")["finish"].mean()
    return {int(k): float(v) for k, v in grouped.items()}


def get_course_baseline_finish(path=None):
    """コース（艇番 1〜6）ごとの基準着順を dict で返す。

    history_full.csv の実データから算出できたコースはその値を、
    データ不足などで算出できなかったコースはフォールバック値を使う。
    """
    target = Path(path) if path else _DEFAULT_HISTORY_PATH
    baseline = _load_baseline(str(target))

    out = dict(_FALLBACK_BASELINE)
    if baseline:
        out.update(baseline)
    return out
