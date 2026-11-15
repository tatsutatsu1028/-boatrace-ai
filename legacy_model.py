"""
今までのモデル（prediction.train / prediction.train_lane_agnostic）を、学習済みのまま保存・読み込みする。

  作る: 毎朝の GitHub Actions（ml_model.yml の task=state の後。手動なら task=legacy）が
        ml_nightly.py legacy から build() を呼び、データ用リポジトリの
        models/legacy_model.pkl.gz と models/legacy_model.json を書く。
  読む: Streamlit アプリ（app.py）は data_paths.sync_from_github でこの2つを取り、load() で
        読むだけにする（アプリの中では学習しない。CPU を制限されると学習が終わらないため）。

学習の中身は prediction.train(sample_history) と prediction.train_lane_agnostic(sample_history)
そのまま（アプリが今まで中で行っていた学習と同じ）。予想・買い目・資金配分は変わらない。

pickle は scikit-learn の版が違うと読めない・結果が変わることがあるので、保存した版を
legacy_model.json に残し、読み込むときに今の版と同じかを確かめる（違えば読まない）。
アプリと Actions の scikit-learn は requirements.txt で同じ版に固定している。

事後予想（hindcast.py）はその日より前のデータだけで学習し直す必要があるので、これを使わない。
"""

from __future__ import annotations

import hashlib
import json
import platform
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data_paths import data_path

JST = ZoneInfo("Asia/Tokyo")
MODEL_FILE = "models/legacy_model.pkl.gz"
INFO_FILE = "models/legacy_model.json"
# アプリが data_paths.sync_from_github で取るファイル
APP_FILES = (INFO_FILE, MODEL_FILE)
# 保存形式の版（中身の dict の形を変えたら上げる）
FORMAT = 1
SOURCE_FILES = ("sample_history.csv", "history_full.csv")


class LegacyModelUnavailable(RuntimeError):
    """保存済みの今までのモデルが無い・読めない。"""


def git_blob_sha(path) -> str:
    """git の blob SHA（data_paths.sync_from_github が覚えている値と同じ計算）。"""
    data = Path(path).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _versions():
    import joblib
    import numpy
    import pandas
    import sklearn

    return {
        "sklearn": sklearn.__version__,
        "numpy": numpy.__version__,
        "pandas": pandas.__version__,
        "joblib": joblib.__version__,
        "python": platform.python_version(),
    }


def _sources():
    out = {}
    for name in SOURCE_FILES:
        p = data_path(name)
        if p.exists():
            out[name] = {"blob_sha": git_blob_sha(p), "bytes": p.stat().st_size}
    return out


def build(model_path=None, info_path=None, log=print, force=False):
    """sample_history.csv で今までのモデルと艇番なしモデルを学習し、保存する。戻り値は info。

    学習データ（sample_history.csv・history_full.csv）とライブラリの版が前回の保存と同じなら
    学習結果も同じなので、作り直さない（データ用リポジトリに同じ中身のコミットを増やさない）。
    """
    import joblib
    import pandas as pd

    import prediction

    t0 = time.time()
    model_path = Path(model_path or data_path(MODEL_FILE))
    info_path = Path(info_path or data_path(INFO_FILE))
    sources = _sources()
    prev = read_info(info_path)
    if (not force and prev and model_path.exists() and prev.get("format") == FORMAT
            and prev.get("versions") == _versions()
            and {k: v.get("blob_sha") for k, v in (prev.get("sources") or {}).items()}
            == {k: v["blob_sha"] for k, v in sources.items()}):
        log(f"[LEGACY] 学習データと版が前回（{prev.get('created_at')}）と同じため作り直さない")
        return prev

    history = pd.read_csv(data_path("sample_history.csv"))
    model = prediction.train(history)
    t_train = time.time() - t0
    lane_agnostic = prediction.train_lane_agnostic(history)

    sources["sample_history.csv"]["rows"] = int(len(history))
    if "history_full.csv" in sources:
        sources["history_full.csv"]["rows"] = int(getattr(model, "_position_model_rows", 0))

    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {"format": FORMAT, "model": model, "lane_agnostic": lane_agnostic,
         "lane_agnostic_version": prediction.LANE_AGNOSTIC_VERSION},
        model_path, compress=("gzip", 3),
    )
    info = {
        "format": FORMAT,
        "created_at": datetime.now(JST).isoformat(timespec="seconds"),
        "sample_history_rows": int(len(history)),
        "position_model_rows": int(getattr(model, "_position_model_rows", 0)),
        "second_model_rows": int(getattr(model, "_second_model_rows", 0)),
        "third_model_rows": int(getattr(model, "_third_model_rows", 0)),
        "lane_agnostic_version": prediction.LANE_AGNOSTIC_VERSION,
        "sources": sources,
        "versions": _versions(),
        "model_bytes": model_path.stat().st_size,
        "train_seconds": round(t_train, 1),
    }
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[LEGACY] 今までのモデルを保存 sample {info['sample_history_rows']}行 / 2着・3着 "
        f"{info['position_model_rows']}行 / sklearn {info['versions']['sklearn']} / "
        f"{info['model_bytes'] // 1024}KB / {time.time() - t0:.0f}秒")
    return info


def read_info(info_path=None):
    try:
        return json.loads(Path(info_path or data_path(INFO_FILE)).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def load(model_path=None, info_path=None):
    """保存済みのモデルを読む。戻り値 (model, lane_agnostic, info)。読めなければ LegacyModelUnavailable。"""
    import joblib
    import sklearn

    model_path = Path(model_path or data_path(MODEL_FILE))
    if not model_path.exists():
        raise LegacyModelUnavailable(f"{MODEL_FILE} がありません")
    info = read_info(info_path)
    if not info:
        raise LegacyModelUnavailable(f"{INFO_FILE} がありません・読めません")
    if int(info.get("format", 0)) != FORMAT:
        raise LegacyModelUnavailable(f"保存形式の版が違います（{info.get('format')} / この版は {FORMAT}）")
    saved = (info.get("versions") or {}).get("sklearn")
    if saved != sklearn.__version__:
        raise LegacyModelUnavailable(
            f"scikit-learn の版が違います（保存 {saved} / 今 {sklearn.__version__}）"
        )
    try:
        bundle = joblib.load(model_path)
    except Exception as e:  # noqa: BLE001
        raise LegacyModelUnavailable(f"読み込みに失敗しました（{type(e).__name__}）") from e
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT or bundle.get("model") is None:
        raise LegacyModelUnavailable("中身の形が違います")
    return bundle["model"], bundle.get("lane_agnostic"), info


def stale_sources(info, local_shas):
    """保存したときの学習データと、手元（アプリが取ったファイル）のデータが違うものの名前。"""
    out = []
    for name, meta in ((info or {}).get("sources") or {}).items():
        local = (local_shas or {}).get(name)
        if local and meta.get("blob_sha") and local != meta["blob_sha"]:
            out.append(name)
    return out


def new_model_only_prediction(work, odds, params, model_setting, date_key, jcd, rno):
    """今までのモデルが読めないときの予想（新しいモデルで予想できるレースだけ）。

    model_only.model_only_prediction と同じ形の dict を返す。新しいモデルの final から、
    予想タブと同じ手順（claude_tab.build_tickets: 点数・保険・補充・資金配分）で買い目を作る。
    新しいモデルが使えるレースでは、model_only_prediction の final・買い目は今までのモデルに
    よらないので、ここで出す final・買い目と同じになる（今までのモデルとの比較だけが無い）。
    新しいモデルでも予想できなければ LegacyModelUnavailable。
    """
    import numpy as np

    import claude_tab
    import ml_live
    from model_only import complete_adaptive_tickets
    from prediction import assess_favorite_risk

    final, model_info = ml_live.production_final(model_setting, work, date_key, jcd, rno)
    if final is None:
        reason = (model_info or {}).get("fallback_reason")
        raise LegacyModelUnavailable(
            "このレースは新しいモデルでも予想できないため、予想を出せません"
            + (f"（{reason}）" if reason else "（予想モデルの設定が今までのモデルです）")
        )
    tickets, plan = claude_tab.build_tickets(final, work, odds, params, complete_adaptive_tickets)
    if len(tickets) != int(plan["point_count"]):
        raise RuntimeError(f"買い目点数の生成不整合: 予定{int(plan['point_count'])}点 / 実際{len(tickets)}点")
    favorite_lane, risk_score, risk_reasons = assess_favorite_risk(work, final)
    return {
        # 比較用（展示反映前・今までのモデル）は出せないので空にする（表示は「差」を出さない）
        "before": final[["lane"]].assign(p_first=np.nan),
        "before_label": None,
        "model_info": model_info,
        "final": final,
        "tickets": tickets,
        "hedge_lane": favorite_lane if (params["hedge_enabled"] and risk_score >= 2) else None,
        "risk_reasons": risk_reasons,
        "research_variants": {},
        "ticket_plan": plan,
    }
