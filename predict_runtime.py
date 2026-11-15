"""
本番の予想（アプリの「予想」ボタンと、GitHub Actions で計算する予想 remote_predict.py）で共通の差し替え。

もとは app.py の中にあったもの。アプリと Actions で同じ予想・買い目・保存内容にするため、ここにまとめた。
予想ロジック本体（prediction.py / model_only.py）は変えず、入口だけを「保存済みの本番設定」にそろえる:

  - prediction.train          : 保存済みの今までのモデル（legacy_getter が返すもの）を返す（学習しない）
  - prediction.predict        : 展示前比較以外は保存済み設定の重みで予想し、艇番なしモデルの判定を attrs に載せる
  - ml_live.production_final  : 新しいモデルの final にも艇番なしモデルの判定を載せ、保険の判定の基にする
  - prediction.rank_tickets   : 保険（hedge）を保存済み設定と本番の final から決める。オッズは使わない
  - stake_allocator.allocate_stakes_smart : 予算・最低額などを保存済み設定にそろえる
  - prediction.research_prediction_variants : 研究用の比較も保存済みの重みを起点にする
  - prediction.confidence / result_tracker._snapshot_payload : 信頼度(A/B/C)と艇番なしモデルを保存内容に入れる

install() は何度呼んでも同じ関数を入れ直すだけ（Streamlit の再実行のたびに呼ばれる）。
"""

from __future__ import annotations

import time

import ml_live
import perf
import prediction
import result_tracker
import stake_allocator

# 手動固定と自動固定の共通設定。DB側の app_settings にも同名列を持たせる。
EXTRA_DEFAULT_SETTINGS = {
    "display_weight": 0.32,
    "weather_weight": 0.10,
    "venue_course_weight": 0.12,
    "hedge_enabled": True,
}

# 保存済み設定（app_settings）を読み直す間隔（秒）。予想1回で十数回呼ばれるので、同じ数秒の間は使い回す。
# 設定を保存したら捨てる（clear_settings_memo）。
SETTINGS_REUSE_SECONDS = 5.0
_settings_memo = {}

# 保存済みの今までのモデルを返す関数（{"model", "lane_agnostic", ...} の dict）。install() で入れる
_legacy_getter = None

# 直近の本番 final/race。買い目選定時の保険判定に使う。
_LAST_PRODUCTION_RACE = None
_LAST_PRODUCTION_FINAL = None


def _keep_originals():
    pairs = (
        (result_tracker, "save_settings"),
        (prediction, "train"),
        (prediction, "predict"),
        (prediction, "rank_tickets"),
        (prediction, "research_prediction_variants"),
        (prediction, "confidence"),
        (stake_allocator, "allocate_stakes_smart"),
        (ml_live, "production_final"),
        (ml_live, "_programs"),
        (result_tracker, "_snapshot_payload"),
    )
    for mod, name in pairs:
        # 例: prediction._boat_ai_original_predict / ml_live._boat_ai_original_programs（app.py の頃と同じ名前）
        orig = f"_boat_ai_original_{name.lstrip('_')}"
        if not hasattr(mod, orig):
            setattr(mod, orig, getattr(mod, name))


# ---------------------------------------------------------------
# 保存済みの本番設定
# ---------------------------------------------------------------
def load_settings_reused():
    now = time.monotonic()
    if _settings_memo.get("value") is not None and now - _settings_memo.get("at", -1e9) < SETTINGS_REUSE_SECONDS:
        return dict(_settings_memo["value"])
    value = result_tracker.load_settings()
    _settings_memo.update(value=dict(value), at=now)
    return dict(value)


def clear_settings_memo():
    _settings_memo.clear()


def runtime_settings():
    """本番予想で使う保存済み設定を正規化して返す。"""
    s = load_settings_reused()
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


def staff_params(saved):
    """収集スタッフの予想に渡す値（app_core の「スタッフは保存設定へ戻す」と同じ）。"""
    style = str(saved.get("prediction_style", "バランス"))
    return {
        "display_weight": 0.42 if style == "展示重視" else 0.32,
        "weather_weight": 0.10,
        "venue_course_weight": 0.12,
        "hedge_enabled": True,
        "longshot_min_prob_pct": float(saved.get("longshot_min_prob_pct", 0.30)),
        "total_budget": int(saved.get("total_budget", 2000)),
        "min_bet": int(saved.get("min_bet", 100)),
        "value_bias": float(saved.get("value_bias", 0.0)),
    }


# ---------------------------------------------------------------
# 今までのモデル（保存済み）
# ---------------------------------------------------------------
def legacy():
    try:
        return (_legacy_getter() if _legacy_getter else None) or {}
    except Exception:  # noqa: BLE001
        return {}


def train(_history):
    """prediction.train の代わり。保存済みの今までのモデルを返す（読めなければ None。学習はしない）。
    _history（管理者がアップロードしたCSVを含む）は、今までどおり本番の学習器には使わない。"""
    return legacy().get("model")


def lane_agnostic(race):
    """艇番なしモデルの最強艇を求める。失敗しても本番予想には影響させない。"""
    try:
        model = legacy().get("lane_agnostic")
        if model is None:
            return None
        return prediction.lane_agnostic_strongest(model, race)
    except Exception:  # noqa: BLE001
        return None


def _remember_production(race, out):
    global _LAST_PRODUCTION_RACE, _LAST_PRODUCTION_FINAL
    la = lane_agnostic(race)
    if la:
        try:
            out.attrs["_boat_ai_lane_agnostic"] = la
        except Exception:  # noqa: BLE001
            pass
    try:
        _LAST_PRODUCTION_RACE = race.copy()
        _LAST_PRODUCTION_FINAL = out.copy()
    except Exception:  # noqa: BLE001
        _LAST_PRODUCTION_RACE = race
        _LAST_PRODUCTION_FINAL = out


# ---------------------------------------------------------------
# 差し替える関数
# ---------------------------------------------------------------
def predict(model, race, *args, **kwargs):
    # research_prediction_variants は current_meet_weight 等を明示して呼ぶ。
    # それ以外の通常予想（展示前比較／最終予想）だけ保存済み本番設定へ固定する。
    is_research_variant = any(
        k in kwargs
        for k in ("current_meet_weight", "course_weight", "class_weight", "kimarite_weight")
    )
    if not is_research_variant:
        cfg = runtime_settings()
        requested_display = kwargs.get("display_weight", cfg["display_weight"])
        # 展示前比較の display_weight=0 は維持する。
        if float(requested_display) != 0.0:
            kwargs["display_weight"] = cfg["display_weight"]
        kwargs["weather_weight"] = cfg["weather_weight"]
        kwargs["venue_course_weight"] = cfg["venue_course_weight"]
        kwargs["original_display_scale"] = 0.0

    with perf.section("今までのモデル予想"):
        out = prediction._boat_ai_original_predict(model, race, *args, **kwargs)

    if not is_research_variant and float(kwargs.get("display_weight", 0.0)) != 0.0:
        # 艇番なしモデルの判定は attrs に載せるだけで、確率列には一切触れない。
        _remember_production(race, out)
    return out


def production_final(settings, race, *args, **kwargs):
    """新しい予想モデルで予想したときも、保険（hedge）の判定と艇番なしモデルの記録を
    今のモデルの本番予想と同じように、本番に使う final に合わせる。"""
    with perf.section("新モデル予想"):
        out, info = ml_live._boat_ai_original_production_final(settings, race, *args, **kwargs)
    if out is not None:
        _remember_production(race, out)
    return out, info


def programs(date_yyyymmdd):
    """新しいモデルが使う番組表（公式の B ファイル）の取得。時間を測るだけ。"""
    with perf.section("公式データ取得"):
        return ml_live._boat_ai_original_programs(date_yyyymmdd)


def research_prediction_variants(model, race, *args, **kwargs):
    """研究用保存も本番と同じ保存済み重みを起点にする。"""
    cfg = runtime_settings()
    kwargs["display_weight"] = cfg["display_weight"]
    kwargs["weather_weight"] = cfg["weather_weight"]
    kwargs["venue_course_weight"] = cfg["venue_course_weight"]
    with perf.section("今までのモデル予想"):
        return prediction._boat_ai_original_research_prediction_variants(model, race, *args, **kwargs)


def rank_tickets(tri, odds=None, *args, **kwargs):
    cfg = runtime_settings()
    # app_core / 自動固定側が8〜10点の自動構成を明示した場合は、
    # 保存済みの固定点数で上書きしない。未指定の旧呼び出しだけ設定値を使う。
    kwargs.setdefault("main_n", cfg["main_n"])
    kwargs.setdefault("cover_n", cfg["cover_n"])
    kwargs.setdefault("longshot_n", cfg["hole_n"])
    kwargs.setdefault("longshot_min_prob", cfg["longshot_min_prob_pct"] / 100.0)
    kwargs["use_odds"] = False

    # hedge_lane は画面の一時トグルではなく保存済み設定から再計算する。
    hedge_lane = None
    if cfg["hedge_enabled"] and _LAST_PRODUCTION_RACE is not None and _LAST_PRODUCTION_FINAL is not None:
        try:
            fav_lane, risk_score, _ = prediction.assess_favorite_risk(_LAST_PRODUCTION_RACE, _LAST_PRODUCTION_FINAL)
            if risk_score >= 2:
                hedge_lane = fav_lane
        except Exception:  # noqa: BLE001
            hedge_lane = None
    kwargs["hedge_lane"] = hedge_lane
    return prediction._boat_ai_original_rank_tickets(tri, odds, *args, **kwargs)


def allocate_stakes_smart(tickets, *args, **kwargs):
    cfg = runtime_settings()
    kwargs["budget"] = cfg["total_budget"]
    kwargs["unit"] = 100
    kwargs["min_bet"] = cfg["min_bet"]
    kwargs["max_longshot_share"] = 0.15
    kwargs["max_ticket_share"] = 0.35
    kwargs["value_bias"] = cfg["value_bias"]
    kwargs["use_odds"] = False
    return stake_allocator._boat_ai_original_allocate_stakes_smart(tickets, *args, **kwargs)


def confidence(first, race):
    # AI総合信頼度(A/B/C)は、固定時点の表示値をそのまま検証できるよう payload_json に保存する。
    label = prediction._boat_ai_original_confidence(first, race)
    try:
        first.attrs["_boat_ai_confidence_label"] = str(label)
    except Exception:  # noqa: BLE001
        pass
    return label


def snapshot_payload(final, tickets, research_variants=None, race_features=None):
    payload = result_tracker._boat_ai_original_snapshot_payload(
        final, tickets, research_variants=research_variants, race_features=race_features,
    )
    try:
        label = str(final.attrs.get("_boat_ai_confidence_label", "")).strip()
    except Exception:  # noqa: BLE001
        label = ""
    if label in {"A", "B", "C"}:
        payload["confidence"] = label
    # 艇番なしモデルの最強艇（表示専用の判定）を後日の効果検証用に保存する。
    try:
        la = prediction.lane_agnostic_snapshot(final, final.attrs.get("_boat_ai_lane_agnostic"))
    except Exception:  # noqa: BLE001
        la = None
    if la:
        payload["lane_agnostic"] = la
    return payload


def install(legacy_getter):
    """差し替えを入れる。legacy_getter() は保存済みの今までのモデルの dict を返す関数。"""
    global _legacy_getter
    _keep_originals()
    _legacy_getter = legacy_getter
    result_tracker.DEFAULT_SETTINGS.update(EXTRA_DEFAULT_SETTINGS)
    prediction.train = train
    prediction.predict = predict
    prediction.research_prediction_variants = research_prediction_variants
    prediction.rank_tickets = rank_tickets
    prediction.confidence = confidence
    stake_allocator.allocate_stakes_smart = allocate_stakes_smart
    ml_live.production_final = production_final
    ml_live._programs = programs
    result_tracker._snapshot_payload = snapshot_payload


def uninstall():
    """差し替えを元に戻す（テスト用）。"""
    global _legacy_getter, _LAST_PRODUCTION_RACE, _LAST_PRODUCTION_FINAL
    for mod, name in (
        (prediction, "train"), (prediction, "predict"), (prediction, "research_prediction_variants"),
        (prediction, "rank_tickets"), (prediction, "confidence"), (stake_allocator, "allocate_stakes_smart"),
        (ml_live, "production_final"), (ml_live, "_programs"), (result_tracker, "_snapshot_payload"),
    ):
        orig = getattr(mod, f"_boat_ai_original_{name.lstrip('_')}", None)
        if orig is not None:
            setattr(mod, name, orig)
    _legacy_getter = None
    _LAST_PRODUCTION_RACE = _LAST_PRODUCTION_FINAL = None
    clear_settings_memo()
