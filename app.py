from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import prediction as _prediction
import result_tracker as _result_tracker
import stake_allocator as _stake_allocator

# 本体は app_core.py。ここでは表示とオーナー専用の自動固定設定だけを追加してから本体を実行する。
# 予想ロジック本体は変更せず、手動固定と自動固定が同じ「保存済み本番設定」を使うよう入口だけ統一する。
if not hasattr(st, "_boat_ai_original_subheader"):
    st._boat_ai_original_subheader = st.subheader

# 手動固定と自動固定の共通設定。DB側の app_settings にも同名列を持たせる。
_result_tracker.DEFAULT_SETTINGS.update({
    "display_weight": 0.32,
    "weather_weight": 0.10,
    "venue_course_weight": 0.12,
    "hedge_enabled": True,
})

if not hasattr(_result_tracker, "_boat_ai_original_save_settings"):
    _result_tracker._boat_ai_original_save_settings = _result_tracker.save_settings
if not hasattr(_prediction, "_boat_ai_original_train"):
    _prediction._boat_ai_original_train = _prediction.train
if not hasattr(_prediction, "_boat_ai_original_predict"):
    _prediction._boat_ai_original_predict = _prediction.predict
if not hasattr(_prediction, "_boat_ai_original_rank_tickets"):
    _prediction._boat_ai_original_rank_tickets = _prediction.rank_tickets
if not hasattr(_prediction, "_boat_ai_original_research_prediction_variants"):
    _prediction._boat_ai_original_research_prediction_variants = _prediction.research_prediction_variants
if not hasattr(_stake_allocator, "_boat_ai_original_allocate_stakes_smart"):
    _stake_allocator._boat_ai_original_allocate_stakes_smart = _stake_allocator.allocate_stakes_smart


def _runtime_settings():
    """本番予想で使う保存済み設定を正規化して返す。"""
    s = _result_tracker.load_settings()
    style = str(s.get("prediction_style", "バランス"))
    default_display = 0.42 if style == "展示重視" else 0.32
    return {
        "main_n": int(s.get("main_n", 3)),
        "cover_n": int(s.get("cover_n", 3)),
        "hole_n": int(s.get("hole_n", 0)),
        "total_budget": int(s.get("total_budget", 2000)),
        "min_bet": int(s.get("min_bet", 100)),
        "longshot_min_prob_pct": float(s.get("longshot_min_prob_pct", 0.30)),
        "value_bias": float(s.get("value_bias", 0.0)),
        "prediction_style": style,
        "display_weight": float(s.get("display_weight", default_display)),
        "weather_weight": float(s.get("weather_weight", 0.10)),
        "venue_course_weight": float(s.get("venue_course_weight", 0.12)),
        "hedge_enabled": bool(s.get("hedge_enabled", True)),
    }


def _boat_ai_save_settings(settings):
    """従来の設定に、予想重みと保険ON/OFFも一緒に保存する。"""
    merged = dict(settings or {})
    style = str(merged.get("prediction_style", "バランス"))
    default_display = 0.42 if style == "展示重視" else 0.32
    merged["display_weight"] = float(
        st.session_state.get(f"display_weight_{style}", default_display)
    )
    merged["weather_weight"] = float(
        st.session_state.get(f"weather_weight_{style}", 0.10)
    )
    merged["venue_course_weight"] = float(
        st.session_state.get(f"venue_course_weight_{style}", 0.12)
    )
    merged["hedge_enabled"] = bool(
        st.session_state.get(f"hedge_enabled_{style}", True)
    )
    return _result_tracker._boat_ai_original_save_settings(merged)


_result_tracker.save_settings = _boat_ai_save_settings

# 学習データは本番固定では常に sample_history.csv を使う。
# 管理画面のCSVアップロードは確認・研究用として残すが、本番固定の学習器は自動固定と同一にする。
#
# train()自体はsample_history.csv（約3.1万行）に加え、内部でhistory_full.csv
# （約8.6万行）も読んで2着・3着モデル等を学習するため、予想のたびに毎回
# 全量再学習すると重い。学習に使うCSVの更新日時・サイズが変わっていなければ
# st.cache_resourceでプロセス全体に共有されたモデルを再利用し、変わった
# ときだけ再学習する。train()自体の挙動・出力は変えず、呼ばれる頻度だけを
# 最適化する。
_TRAIN_DATA_FILES = ("sample_history.csv", "history_full.csv")


def _boat_ai_train_cache_key():
    """学習に使う各CSVの更新日時とサイズからキャッシュキーを作る。

    ファイル内容のハッシュ計算は約8.6万行のCSVを毎回読み直すことになり
    本末転倒なので、mtime+size（1回のstat呼び出し）で更新検知する。
    """
    key = []
    for name in _TRAIN_DATA_FILES:
        path = Path(__file__).with_name(name)
        try:
            stat = path.stat()
            key.append((name, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            key.append((name, None, None))
    return tuple(key)


@st.cache_resource(show_spinner=False)
def _boat_ai_train_cached(cache_key):
    canonical = pd.read_csv(Path(__file__).with_name("sample_history.csv"))
    return _prediction._boat_ai_original_train(canonical)


def _boat_ai_train(_history):
    return _boat_ai_train_cached(_boat_ai_train_cache_key())


_prediction.train = _boat_ai_train


# 艇番なし1着モデル（表示・検証専用）も本番train()と同じキーでキャッシュし、
# 予想のたびに再学習しない。学習データも本番1着モデルと同じ sample_history.csv。
@st.cache_resource(show_spinner=False)
def _boat_ai_train_lane_agnostic_cached(cache_key):
    canonical = pd.read_csv(Path(__file__).with_name("sample_history.csv"))
    return _prediction.train_lane_agnostic(canonical)


def _boat_ai_lane_agnostic(race):
    """艇番なしモデルの最強艇を求める。失敗しても本番予想には影響させない。"""
    try:
        model = _boat_ai_train_lane_agnostic_cached(_boat_ai_train_cache_key())
        return _prediction.lane_agnostic_strongest(model, race)
    except Exception:
        return None


# 直近の本番final/raceを保持し、買い目選定時の保険判定も保存設定に統一する。
_LAST_PRODUCTION_RACE = None
_LAST_PRODUCTION_FINAL = None


def _boat_ai_predict(model, race, *args, **kwargs):
    global _LAST_PRODUCTION_RACE, _LAST_PRODUCTION_FINAL

    # research_prediction_variants は current_meet_weight 等を明示して呼ぶ。
    # それ以外の通常予想（展示前比較／最終予想）だけ保存済み本番設定へ固定する。
    is_research_variant = any(
        k in kwargs
        for k in ("current_meet_weight", "course_weight", "class_weight", "kimarite_weight")
    )

    if not is_research_variant:
        cfg = _runtime_settings()
        requested_display = kwargs.get("display_weight", cfg["display_weight"])
        # 展示前比較の display_weight=0 は維持する。
        if float(requested_display) != 0.0:
            kwargs["display_weight"] = cfg["display_weight"]
        kwargs["weather_weight"] = cfg["weather_weight"]
        kwargs["venue_course_weight"] = cfg["venue_course_weight"]
        kwargs["original_display_scale"] = 0.0

    out = _prediction._boat_ai_original_predict(model, race, *args, **kwargs)

    if not is_research_variant and float(kwargs.get("display_weight", 0.0)) != 0.0:
        # 艇番なしモデルの判定は attrs に載せるだけで、確率列には一切触れない。
        lane_agnostic = _boat_ai_lane_agnostic(race)
        if lane_agnostic:
            try:
                out.attrs["_boat_ai_lane_agnostic"] = lane_agnostic
            except Exception:
                pass
        try:
            _LAST_PRODUCTION_RACE = race.copy()
            _LAST_PRODUCTION_FINAL = out.copy()
        except Exception:
            _LAST_PRODUCTION_RACE = race
            _LAST_PRODUCTION_FINAL = out

    return out


_prediction.predict = _boat_ai_predict


def _boat_ai_research_prediction_variants(model, race, *args, **kwargs):
    """研究用保存も本番と同じ保存済み重みを起点にする。"""
    cfg = _runtime_settings()
    kwargs["display_weight"] = cfg["display_weight"]
    kwargs["weather_weight"] = cfg["weather_weight"]
    kwargs["venue_course_weight"] = cfg["venue_course_weight"]
    return _prediction._boat_ai_original_research_prediction_variants(
        model,
        race,
        *args,
        **kwargs,
    )


_prediction.research_prediction_variants = _boat_ai_research_prediction_variants


def _boat_ai_rank_tickets(tri, odds=None, *args, **kwargs):
    cfg = _runtime_settings()
    # app_core / 自動固定側が8〜10点の自動構成を明示した場合は、
    # 保存済みの固定点数で上書きしない。未指定の旧呼び出しだけ設定値を使う。
    kwargs.setdefault("main_n", cfg["main_n"])
    kwargs.setdefault("cover_n", cfg["cover_n"])
    kwargs.setdefault("longshot_n", cfg["hole_n"])
    kwargs.setdefault(
        "longshot_min_prob",
        cfg["longshot_min_prob_pct"] / 100.0,
    )
    kwargs["use_odds"] = False

    # hedge_lane は画面の一時トグルではなく保存済み設定から再計算する。
    hedge_lane = None
    if cfg["hedge_enabled"] and _LAST_PRODUCTION_RACE is not None and _LAST_PRODUCTION_FINAL is not None:
        try:
            fav_lane, risk_score, _ = _prediction.assess_favorite_risk(
                _LAST_PRODUCTION_RACE,
                _LAST_PRODUCTION_FINAL,
            )
            if risk_score >= 2:
                hedge_lane = fav_lane
        except Exception:
            hedge_lane = None
    kwargs["hedge_lane"] = hedge_lane

    return _prediction._boat_ai_original_rank_tickets(tri, odds, *args, **kwargs)


_prediction.rank_tickets = _boat_ai_rank_tickets


def _boat_ai_allocate_stakes_smart(tickets, *args, **kwargs):
    cfg = _runtime_settings()
    kwargs["budget"] = cfg["total_budget"]
    kwargs["unit"] = 100
    kwargs["min_bet"] = cfg["min_bet"]
    kwargs["max_longshot_share"] = 0.15
    kwargs["max_ticket_share"] = 0.35
    kwargs["value_bias"] = cfg["value_bias"]
    kwargs["use_odds"] = False
    return _stake_allocator._boat_ai_original_allocate_stakes_smart(
        tickets,
        *args,
        **kwargs,
    )


_stake_allocator.allocate_stakes_smart = _boat_ai_allocate_stakes_smart

# AI総合信頼度(A/B/C)は、固定時点の表示値をそのまま検証できるよう
# prediction_snapshots.payload_json に保存する。
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


def _boat_ai_snapshot_payload(
    final,
    tickets,
    research_variants=None,
    race_features=None,
):
    payload = _result_tracker._boat_ai_original_snapshot_payload(
        final,
        tickets,
        research_variants=research_variants,
        race_features=race_features,
    )
    try:
        label = str(final.attrs.get("_boat_ai_confidence_label", "")).strip()
    except Exception:
        label = ""
    if label in {"A", "B", "C"}:
        payload["confidence"] = label
    # 艇番なしモデルの最強艇（表示専用の判定）を後日の効果検証用に保存する。
    try:
        lane_agnostic = _prediction.lane_agnostic_snapshot(
            final,
            final.attrs.get("_boat_ai_lane_agnostic"),
        )
    except Exception:
        lane_agnostic = None
    if lane_agnostic:
        payload["lane_agnostic"] = lane_agnostic
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
        "ONの間、締切前の開催レースからランダムに選び、1日あたり設定したR数だけ予想→固定します。"
        "その日の分が終わると翌日まで待機し、日付が変わると自動的に再開します"
        "（OFFにするまで毎日続きます）。舟券購入はしません。"
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
    _count_options = [1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 40, 50, 60]
    new_count = st.selectbox(
        "1回の自動固定数",
        options=_count_options,
        index=_count_options.index(daily_count) if daily_count in _count_options else 2,
        key="random_auto_daily_count_owner",
        disabled=not new_enabled,
    )

    if new_enabled != enabled or int(new_count) != int(daily_count):
        try:
            _save_random_auto_settings(new_enabled, new_count, previous_enabled=enabled)
            if new_enabled and not enabled:
                st.success(f"ランダム自動固定をONにしました。1日 {int(new_count)}R を上限に、OFFにするまで毎日自動固定します。")
            elif not new_enabled and enabled:
                st.success("ランダム自動固定をOFFにしました。")
            else:
                st.success("ランダム自動固定の件数を更新しました。")
        except Exception as e:
            st.error(f"自動固定設定を保存できませんでした: {e}")


def _boat_ai_subheader(body, *args, **kwargs):
    rendered = st._boat_ai_original_subheader(body, *args, **kwargs)

    try:
        if isinstance(body, str) and body == "学習データ":
            st.caption(
                "📌 本番固定は手動・自動とも sample_history.csv を共通学習データとして使用します。"
                "アップロードCSVは確認・研究用で、本番固定の学習器には混ぜません。"
            )

        if isinstance(body, str) and body == "買い目設定":
            _render_random_auto_settings()
            st.caption("📌 本番固定は保存済み設定を使用します。変更した場合は『この設定を保存』後の予想から反映されます。")

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

            lane_agnostic = (
                final.attrs.get("_boat_ai_lane_agnostic")
                if final is not None
                else None
            )
            if _prediction.lane1_strongest_badge(final, lane_agnostic):
                st.success(
                    "💪 1号艇は選手としても最強"
                    f"（艇番なしモデル {float(lane_agnostic['strongest_prob']):.1%}）"
                )
                st.caption(
                    "艇番を除いた特徴量で学習した別モデルでも1号艇が1着確率トップです。"
                    "表示のみで、予想・買い目・購入額は変更しません。"
                )
    except Exception:
        pass

    return rendered


st.subheader = _boat_ai_subheader

# 保存済み重みを設定画面の初期表示にも反映する。ユーザーが変更した未保存値は上書きしない。
try:
    _boot_cfg = _runtime_settings()
    _boot_style = _boot_cfg["prediction_style"]
    st.session_state.setdefault(
        f"display_weight_{_boot_style}", _boot_cfg["display_weight"]
    )
    st.session_state.setdefault(
        f"weather_weight_{_boot_style}", _boot_cfg["weather_weight"]
    )
    st.session_state.setdefault(
        f"venue_course_weight_{_boot_style}", _boot_cfg["venue_course_weight"]
    )
    st.session_state.setdefault(
        f"hedge_enabled_{_boot_style}", _boot_cfg["hedge_enabled"]
    )
except Exception:
    pass

_core = Path(__file__).with_name("app_core.py")
exec(
    compile(_core.read_text(encoding="utf-8"), str(_core), "exec"),
    globals(),
    globals(),
)
