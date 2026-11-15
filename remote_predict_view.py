"""
予想タブ: 「予想」ボタンの計算を GitHub Actions に頼み、終わったら結果を出す（predict_race.yml → remote_predict.py）。

  - runner()      : どこで計算するか（actions / app）。管理者が設定タブで切り替える（prediction_runner）
  - request()     : 依頼を記録して Actions を起動する（Supabase の prediction_request_create）
  - render_wait() : 5秒おきに prediction_requests の状態だけを確かめる（st.fragment。ページ全体は描き直さない）。
                    終わったら結果を st.session_state["result"] に入れてページを描き直す（最大3分）

アプリの中で計算するときと同じ入力（画面の表・オッズ・設定）を送り、同じ関数で計算するので、予想・買い目・
保存の内容は同じ。
"""

from __future__ import annotations

import time

import requests
import streamlit as st

import frame_codec

WAIT_SECONDS = 180
POLL_SECONDS = 5
SESSION_KEY = "remote_prediction"
RUNNERS = ("actions", "app")
RUNNER_LABELS = {"actions": "GitHub Actions で計算（既定）", "app": "アプリの中で計算"}


def _config():
    try:
        cfg = st.secrets.get("supabase", {})
        return str(cfg.get("url", "") or "").strip().rstrip("/"), str(cfg.get("key", "") or "").strip()
    except Exception:  # noqa: BLE001
        return "", ""


def _rpc(name, payload, timeout=15):
    url, key = _config()
    if not url or not key:
        raise RuntimeError("Supabase の設定がありません")
    r = requests.post(
        f"{url}/rest/v1/rpc/{name}",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload, timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=30, show_spinner=False)
def runner():
    """どこで計算するか。読めない（表がまだ無い・Supabase に繋がらない）ときはアプリの中で計算する。"""
    try:
        value = _rpc("prediction_runner_get", {})
        return value if value in RUNNERS else "app"
    except Exception as e:  # noqa: BLE001
        print("[REMOTE_PREDICT] runner read error:", type(e).__name__, flush=True)
        return "app"


def set_runner(value):
    _rpc("prediction_runner_set", {"p_runner": value})
    runner.clear()


def render_setting():
    """設定タブ（管理者だけ）: 予想ボタンをどこで計算するか。"""
    st.markdown("#### 🛰 予想ボタンの計算場所")
    current = runner()
    choice = st.radio(
        "予想ボタンの計算場所", RUNNERS, index=RUNNERS.index(current), format_func=RUNNER_LABELS.get,
        horizontal=True, key="prediction_runner_choice", label_visibility="collapsed",
    )
    st.caption(
        "Actions で計算すると、アプリの CPU を使わずに予想します（押してから1分ほど）。"
        "Actions が使えないときは「アプリの中で計算」に切り替えてください。スタッフの予想ボタンにも効きます。"
    )
    if choice != current:
        try:
            set_runner(choice)
            st.success(f"予想ボタンの計算場所を「{RUNNER_LABELS[choice]}」にしました。")
        except Exception as e:  # noqa: BLE001
            st.error(f"保存できませんでした: {type(e).__name__}")


def request(ctx, mode, role, collector_name, payload):
    """依頼を記録して Actions を起動する。戻り値は依頼の id（失敗したら例外）。"""
    rid = _rpc("prediction_request_create", {
        "p_race_key": ctx, "p_mode": mode, "p_role": role, "p_collector_name": collector_name,
        "p_input": frame_codec.encode(payload),
    }, timeout=20)
    if not rid:
        raise RuntimeError("依頼を記録できませんでした")
    st.session_state[SESSION_KEY] = {"id": str(rid), "ctx": ctx, "mode": mode, "started": time.time()}
    return str(rid)


def pending(ctx):
    p = st.session_state.get(SESSION_KEY)
    return p if p and p.get("ctx") == ctx else None


def status(request_id):
    rows = _rpc("prediction_request_status", {"p_id": request_id})
    return rows[0] if rows else None


def restore(out, ctx):
    """Actions の結果を、アプリの中で計算したときと同じ形で st.session_state に入れる。"""
    import claude_tab

    data = frame_codec.decode(out)
    result = data["result"]
    result["remote"] = {"seconds": data.get("seconds"), "stage_text": data.get("stage_text"),
                        "legacy_error": data.get("legacy_error")}
    st.session_state["result"] = result
    bundle = data.get("claude_bundle")
    if bundle is not None:
        claude_tab._store()[ctx] = bundle
    # 保存の関数まで呼んだときは、保存済み予想の状態をその結果で覚え直す（アプリで計算したときと同じ）
    save = result.get("save") or {}
    if save.pop("rpc", False) and save.get("meta") is not None:
        st.session_state.setdefault("_snapshot_meta_by_race", {})[ctx] = save["meta"]
    return result


@st.fragment(run_every=POLL_SECONDS)
def render_wait(ctx):
    """依頼の状態を5秒おきに確かめる（このかたまりだけ描き直す）。終わったらページ全体を描き直す。"""
    p = pending(ctx)
    if not p:
        return
    elapsed = time.time() - p["started"]
    try:
        row = status(p["id"])
    except Exception as e:  # noqa: BLE001
        row = None
        print("[REMOTE_PREDICT] status error:", type(e).__name__, flush=True)
    state = (row or {}).get("status") or "queued"
    if state == "done":
        st.session_state.pop(SESSION_KEY, None)
        try:
            restore(row["result"], ctx)
            print(f"[REMOTE_PREDICT] 受け取り {p['id']} {elapsed:.0f}秒", flush=True)
        except Exception as e:  # noqa: BLE001
            st.session_state["remote_prediction_error"] = {"ctx": ctx, "message": f"結果を読めませんでした（{type(e).__name__}）"}
        st.rerun()
    if state == "error":
        st.session_state.pop(SESSION_KEY, None)
        st.session_state["remote_prediction_error"] = {"ctx": ctx, "message": (row or {}).get("error") or "不明なエラー"}
        st.rerun()
    if elapsed > WAIT_SECONDS:
        st.session_state.pop(SESSION_KEY, None)
        st.session_state["remote_prediction_error"] = {
            "ctx": ctx, "message": f"{WAIT_SECONDS // 60}分待っても Actions の計算が終わりませんでした"}
        st.rerun()
    label = "Actions の起動を待っています" if state == "queued" else "Actions で計算しています"
    with st.status(f"AI解析中…（{label}・{elapsed:.0f}秒）", expanded=True):
        st.write("⏳ GitHub Actions に予想を頼みました。終わると自動で表示します（ふだん1分ほど）。")
        st.caption("このまま待ってください。ほかの画面に移っても、戻ると続きを表示します。")
