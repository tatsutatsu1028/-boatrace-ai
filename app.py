from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st
import prediction as _prediction
import result_tracker as _result_tracker

# 本体は app_core.py。ここでは表示とオーナー専用の自動固定設定だけを追加してから本体を実行する。
# 予想ロジック・買い目・確率計算には触れない。
if not hasattr(st, "_boat_ai_original_subheader"):
    st._boat_ai_original_subheader = st.subheader

# AI総合信頼度(A/B/C)は、固定時点の表示値をそのまま検証できるよう
# prediction_snapshots.payload_json に保存する。
# confidence() の計算式自体は変更せず、呼ばれた結果をDataFrame attrsへ一時保持し、
# result_tracker の既存スナップショットpayloadへ追記するだけにする。
if not hasattr(_prediction, "_boat_ai_original_confidence"):
    _prediction._boat_ai_original_confidence = _prediction.confidence

if not hasattr(_result_tracker, "_boat_ai_original_snapshot_payload"):
    _result_tracker._boat_ai_original_snapshot_payload = _result_tracker._snapshot_payload


def _boat_ai_confidence(first, race):
    label = _prediction._boat_ai_original_confidence(first, race)
    try:
        first.attrs["_boat_ai_confidence_label"] = str(label)
    except Exception:
        pass
    return label


def _boat_ai_snapshot_payload(final, tickets, research_variants=None):
    payload = _result_tracker._boat_ai_original_snapshot_payload(
        final,
        tickets,
        research_variants=research_variants,
    )
    try:
        label = str(final.attrs.get("_boat_ai_confidence_label", "")).strip()
    except Exception:
        label = ""
    if label in {"A", "B", "C"}:
        payload["confidence"] = label
    return payload


_prediction.confidence = _boat_ai_confidence
_result_tracker._snapshot_payload = _boat_ai_snapshot_payload

JST = ZoneInfo("Asia/Tokyo")


def _supabase_settings_config():
    try:
        cfg = st.secrets.get("supabase", {})
        url = str(cfg.get("url", "") or "").strip().rstrip("/")
        key = str(cfg.get("key", "") or "").strip()
        return url, key
    except Exception:
        return "", ""


def _load_random_auto_settings():
    url, key = _supabase_settings_config()
    if not url or not key:
        return False, 3
    try:
        r = requests.get(
            f"{url}/rest/v1/app_settings?id=eq.1&select=random_auto_enabled,random_auto_daily_count",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=10,
        )
        r.raise_for_status()
        rows = r.json() or []
        if rows:
            row = rows[0]
            return bool(row.get("random_auto_enabled", False)), int(row.get("random_auto_daily_count", 3) or 3)
    except Exception:
        pass
    return False, 3


def _save_random_auto_settings(enabled, daily_count, previous_enabled=False):
    url, key = _supabase_settings_config()
    if not url or not key:
        raise RuntimeError("Supabase設定が見つかりません。")

    payload = {
        "random_auto_enabled": bool(enabled),
        "random_auto_daily_count": int(daily_count),
    }
    # OFF→ONのたびに新しい実行単位として開始時刻を更新する。
    # これにより同じ日に何度ONにしても、その回ごとに設定R数を実行できる。
    if bool(enabled) and not bool(previous_enabled):
        payload["random_auto_started_at"] = datetime.now(JST).isoformat(timespec="seconds")

    r = requests.patch(
        f"{url}/rest/v1/app_settings?id=eq.1",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json=payload,
        timeout=10,
    )
    r.raise_for_status()


def _render_random_auto_settings():
    enabled, daily_count = _load_random_auto_settings()
    st.markdown("#### 🎲 ランダム自動固定")
    st.caption(
        "ONにするたび、締切前の開催レースからランダムに選び、設定したR数だけ予想→固定します。"
        "完了すると自動でOFFになります。舟券購入はしません。"
    )

    is_owner = st.session_state.get("auth_role") == "admin"
    if not is_owner:
        st.info(f"現在：{'ON' if enabled else 'OFF'} / 1回 {daily_count}R（変更はオーナーのみ）")
        return

    new_enabled = st.toggle(
        "ランダム自動固定をONにする",
        value=enabled,
        key="random_auto_enabled_owner",
    )
    new_count = st.selectbox(
        "1回の自動固定数",
        options=[1, 2, 3, 4, 5, 6, 8, 10],
        index=[1, 2, 3, 4, 5, 6, 8, 10].index(daily_count) if daily_count in [1, 2, 3, 4, 5, 6, 8, 10] else 2,
        key="random_auto_daily_count_owner",
        disabled=not new_enabled,
    )

    if new_enabled != enabled or int(new_count) != int(daily_count):
        try:
            _save_random_auto_settings(new_enabled, new_count, previous_enabled=enabled)
            if new_enabled and not enabled:
                st.success(f"ランダム自動固定をONにしました。今回は {int(new_count)}R 固定すると自動でOFFになります。")
            elif not new_enabled and enabled:
                st.success("ランダム自動固定をOFFにしました。")
            else:
                st.success("ランダム自動固定の件数を更新しました。")
        except Exception as e:
            st.error(f"自動固定設定を保存できませんでした: {e}")


def _boat_ai_subheader(body, *args, **kwargs):
    rendered = st._boat_ai_original_subheader(body, *args, **kwargs)

    try:
        if isinstance(body, str) and body == "買い目設定":
            _render_random_auto_settings()

        if isinstance(body, str) and body.endswith("AI最終予想"):
            current = st.session_state.get("result") or {}
            final = current.get("final")

            if final is not None and len(final) and "p_first" in final.columns:
                probs = final["p_first"].astype(float)
                top_idx = probs.idxmax()
                top_prob = float(probs.loc[top_idx])

                if top_prob >= 0.80:
                    top_lane = int(final.loc[top_idx, "lane"])
                    top_name = ""
                    if "racer_name" in final.columns:
                        raw_name = final.loc[top_idx, "racer_name"]
                        if raw_name is not None:
                            top_name = str(raw_name).strip()
                            if top_name.lower() in {"nan", "none"}:
                                top_name = ""

                    name_text = f" {top_name}" if top_name else ""
                    st.success(
                        f"🔥 80%以上対象　{top_lane}号艇{name_text}　{top_prob:.1%}"
                    )
                    st.caption(
                        "直近バックテストで注目している本命80%以上のレースです。"
                        "表示のみで、予想・買い目・購入額は変更しません。"
                    )
    except Exception:
        pass

    return rendered


st.subheader = _boat_ai_subheader

_core = Path(__file__).with_name("app_core.py")
exec(
    compile(_core.read_text(encoding="utf-8"), str(_core), "exec"),
    globals(),
    globals(),
)
