"""
予想ボタンの計算を GitHub Actions で行う（predict_race.yml から動かす）。

  python remote_predict.py --request-id <prediction_requests.id>

アプリが prediction_request_create() で依頼（予想に使う表・設定）を記録し、このワークフローを起動する。
ここでは、アプリの中で計算するときと同じ関数（predict_runtime.py・predict_flow.py）で
予想 → Claude（管理者のときだけ）→ 保存（prediction_snapshots。締切後は上書きしない）を行い、
予想タブに出す結果を prediction_requests.result に書く。アプリは5秒おきに状態を見て、終わったら表示する。

今までのモデルは毎朝の Actions が保存したもの（models/legacy_model.pkl.gz）を読む（アプリと同じ）。
Streamlit の st.secrets を使う関数（result_tracker など）はそのまま使うので、ワークフローが
.streamlit/secrets.toml を作ってから動かす。

ログは誰でも見られるので、依頼の id・段階ごとの時間・保存の結果だけを出す（会場・確率・買い目・名前は出さない）。
"""

from __future__ import annotations

import os

# アプリと同じく計算ライブラリのスレッドを1本にする（読み込む前に。予想は変わらない）
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

import argparse  # noqa: E402
import sys  # noqa: E402
import traceback  # noqa: E402
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import requests  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
TABLE = "prediction_requests"
STAGES = ("学習読込", "公式データ取得", "新モデル予想", "今までのモデル予想", "買い目", "Claude", "保存")


def log(*a):
    print("[REMOTE_PREDICT]", *a, flush=True)


def _sb_config():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY がありません")
    return url, key


def _sb(method, path, json=None, prefer=None):
    url, key = _sb_config()
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    r = requests.request(method, f"{url}/rest/v1/{path}", headers=headers, json=json, timeout=30)
    r.raise_for_status()
    return r


def load_request(request_id):
    rows = _sb("GET", f"{TABLE}?id=eq.{request_id}&select=*").json() or []
    return rows[0] if rows else None


def update_request(request_id, **fields):
    _sb("PATCH", f"{TABLE}?id=eq.{request_id}", json=fields, prefer="return=minimal")


def _now():
    return datetime.now(JST).isoformat(timespec="seconds")


_LEGACY = {}


def _legacy():
    """保存済みの今までのモデル（1回だけ読む）。読めなければ model=None（アプリと同じく学習はしない）。"""
    if not _LEGACY:
        import legacy_model

        try:
            model, lane_agnostic, info = legacy_model.load()
            _LEGACY.update(model=model, lane_agnostic=lane_agnostic, info=info, error=None)
        except legacy_model.LegacyModelUnavailable as e:
            _LEGACY.update(model=None, lane_agnostic=None, info=None, error=str(e))
    return _LEGACY


def predict(req):
    """依頼1件の予想。戻り値 (表示用の結果 dict, 保存の結果の status, [TIMING] の行)。"""
    import claude_tab
    import frame_codec
    import perf
    import predict_flow
    import predict_runtime
    import prediction
    from result_tracker import load_prediction_model_setting, load_prediction_snapshot_meta

    predict_runtime.install(_legacy)
    inp = frame_codec.decode(req["input"])
    d = predict_flow.parse_date(inp["date"])
    jcd, rno = str(inp["jcd"]).zfill(2), int(inp["rno"])
    ctx = req["race_key"]
    mode = req["mode"]
    is_admin = req["role"] == "admin"

    rec = perf.start("Actions予想")
    try:
        with perf.section("学習読込"):
            model = prediction.train(None)
            model_setting = load_prediction_model_setting()
        result = predict_flow.compute(
            model, inp["work"], inp.get("odds"), inp["params"], model_setting, d.strftime("%Y%m%d"), jcd, rno, ctx,
        )
        bundle = None
        if is_admin:
            predict_flow.claude_step(
                result, ctx, inp["label"], inp["work"], inp.get("odds"), inp["claude_params"], mode,
                api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
                read_model=os.environ.get("CLAUDE_READ_MODEL", "").strip() or None,
            )
            bundle = claude_tab.bundle(ctx)
        try:
            meta = load_prediction_snapshot_meta(ctx)
            meta = {"exists": False} if meta is None else {**meta, "exists": True}
        except Exception as e:  # noqa: BLE001
            log("保存済み予想の確認に失敗", type(e).__name__)
            meta = None
        result["save"] = predict_flow.save(
            ctx, d, jcd, inp.get("venue") or "", rno, inp.get("deadlines") or {}, inp.get("odds"), result,
            req.get("collector_name") or "", meta,
        )
    finally:
        line = perf.finish(rec, tag="[TIMING]", order=list(STAGES), extra=f" mode={mode}")
    out = {
        "result": result,
        "claude_bundle": bundle,
        "legacy_error": _legacy().get("error"),
        "stage_text": perf.stage_text(rec, STAGES),
        "seconds": round(rec.totals()[0], 1),
    }
    return out, result["save"].get("status"), line


def run(request_id):
    import frame_codec

    req = load_request(request_id)
    if not req:
        log("依頼が見つかりません", request_id)
        return 1
    if req["status"] not in ("queued", "running"):
        log("依頼はもう終わっています", request_id, req["status"])
        return 0
    update_request(request_id, status="running", started_at=_now())
    try:
        out, save_status, line = predict(req)
        update_request(
            request_id, status="done", finished_at=_now(), result=frame_codec.encode(out),
            save_status=save_status, timing=line.split("| ", 1)[-1][:1000],
        )
        log("完了", request_id, "保存:", save_status)
        return 0
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"[:500]
        traceback.print_exc(limit=3, file=sys.stdout)
        try:
            update_request(request_id, status="error", finished_at=_now(), error=msg)
        finally:
            log("失敗", request_id, type(e).__name__)
        return 1


def mark_error(request_id, message):
    """ワークフローの準備（インストールなど）で失敗したとき、アプリが3分待たずに済むよう失敗を記録する。"""
    req = load_request(request_id)
    if req and req["status"] in ("queued", "running"):
        update_request(request_id, status="error", finished_at=_now(), error=message[:500])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--request-id", required=True)
    ap.add_argument("--mark-error", default="")
    a = ap.parse_args()
    if a.mark_error:
        mark_error(a.request_id, a.mark_error)
        sys.exit(0)
    sys.exit(run(a.request_id))
