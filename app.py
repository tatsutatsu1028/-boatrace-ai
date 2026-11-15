import os as _os

# 計算ライブラリ（scikit-learn・LightGBM の OpenMP、numpy の BLAS）のスレッドを1本にする。
# 予想は1レース6艇の小さな表で何百回も呼ぶため、複数スレッドにしても速くならず、待っているスレッドが
# CPU を空回しして使い切ってしまう（手元の計測で、8レースの予想のプロセス全体の CPU が 89秒→25秒。
# 予想・買い目は1つも変わらない: tests/test_thread_limit.py）。ライブラリを読み込む前に設定する。
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    _os.environ.setdefault(_name, "1")

import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import data_paths as _data_paths
import legacy_model as _legacy_model
import ml_live as _ml_live
import prediction as _prediction
import result_tracker as _result_tracker
import stake_allocator as _stake_allocator

# 環境変数より前にライブラリが読み込まれていた場合にも効くよう、読み込み済みのものにも1本を指定する
try:
    from threadpoolctl import threadpool_limits as _threadpool_limits

    _threadpool_limits(limits=1)
except Exception:
    pass

import perf as _perf

# 1回の実行（ページを開く・ボタンを押す・再実行）ごとに、どこで CPU を使ったかを Logs に1行出す（[CPU]）。
_perf.abandon_all()
_BOAT_AI_VIEW = str(st.query_params.get("view") or "")

# note 記事の表示ページ（?view=note。PIN ログインなし・秘密のキー必須）。学習データの取得より前に出して止める
if _BOAT_AI_VIEW == "note":
    import note_page as _note_page

    _note_rec = _perf.start("記事ページ(view=note)")
    try:
        _note_page.render()
    finally:
        _perf.finish(_note_rec)
    st.stop()
# 購入者向けの展示後の買い目ページ（?view=buy&r=レース&t=レースごとの乱数。PIN・マスターキーなし。そのレースだけ）
if _BOAT_AI_VIEW == "buy":
    import note_page as _note_page

    _buy_rec = _perf.start("購入者ページ(view=buy)")
    try:
        _note_page.render_buy()
    finally:
        _perf.finish(_buy_rec)
    st.stop()

_BOAT_AI_RUN = _perf.start("画面")
_perf.lap("準備（設定の差し替え）")

# 本体は app_core.py。ここでは表示とオーナー専用の自動固定設定だけを追加してから本体を実行する。
# 予想ロジック本体は変更せず、手動固定と自動固定が同じ「保存済み本番設定」を使うよう入口だけ統一する。
if not hasattr(st, "_boat_ai_original_subheader"):
    st._boat_ai_original_subheader = st.subheader

# 予想・買い目・保存内容の差し替え（保存済みの本番設定にそろえる）は predict_runtime.py にまとめた
# （GitHub Actions で計算する予想 remote_predict.py と同じものを使う）。下の _boat_ai_legacy を入れて有効にする。
import predict_runtime as _predict_runtime

# 手動固定と自動固定の共通設定。DB側の app_settings にも同名列を持たせる。
_result_tracker.DEFAULT_SETTINGS.update(_predict_runtime.EXTRA_DEFAULT_SETTINGS)

if not hasattr(_result_tracker, "_boat_ai_original_save_settings"):
    _result_tracker._boat_ai_original_save_settings = _result_tracker.save_settings


def _runtime_settings():
    """本番予想で使う保存済み設定を正規化して返す（predict_runtime.runtime_settings）。"""
    return _predict_runtime.runtime_settings()


def _clear_settings_memo():
    _predict_runtime.clear_settings_memo()


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
    try:
        return _result_tracker._boat_ai_original_save_settings(merged)
    finally:
        _clear_settings_memo()


_result_tracker.save_settings = _boat_ai_save_settings

# 学習データは本番固定では常に sample_history.csv を使う。
# 管理画面のCSVアップロードは確認・研究用として残すが、本番固定の学習器は自動固定と同一にする。
#
# 今までのモデル（prediction.train / train_lane_agnostic）はアプリの中では学習しない。
# 毎朝の GitHub Actions（ml_model.yml → ml_nightly.py legacy → legacy_model.py）が
# sample_history.csv で学習して models/legacy_model.pkl.gz に保存したものを読むだけにする
# （Streamlit Cloud で CPU を制限されると、学習が終わらず「AI解析中…」が止まるため）。
# train()自体の挙動・出力は変えず、学習する場所だけを変える。
_TRAIN_DATA_FILES = _data_paths.APP_FILES


# 学習データは公開リポジトリに置かず、非公開のデータ用リポジトリから
# 読み取り専用トークン（Secrets の DATA_REPO_TOKEN）で取得する。
# 1時間ごとに変更の有無だけ確認し、変わったファイルだけ取り直す。
@st.cache_resource(ttl=60 * 60, show_spinner="学習データを取得しています…")
def _boat_ai_sync_data():
    try:
        token = str(st.secrets.get("DATA_REPO_TOKEN", "") or "").strip()
        repo = str(
            st.secrets.get("DATA_REPO", "") or _data_paths.DEFAULT_REPO
        ).strip()
    except Exception:
        token, repo = "", _data_paths.DEFAULT_REPO
    if not token:
        print("[DATA] DATA_REPO_TOKEN is not set; using local files", flush=True)
        return None
    result = _data_paths.sync_from_github(token, repo=repo)
    print(
        "[DATA] synced", repo,
        "updated=", result["updated"], "kept=", result["kept"],
        "errors=", result["errors"],
        flush=True,
    )
    # 新しい予想モデルの学習済みモデル・集計表（毎朝作り直す）。取れなくても今のモデルの予想は動く
    # （ml_live.production_final が今のモデルに戻す）。
    try:
        ml = _data_paths.sync_from_github(
            token, repo=repo, files=_ml_live.APP_FILES, dest=result["dir"]
        )
        model_files = _ml_live.app_model_files(
            _result_tracker.load_prediction_model_setting().get("prediction_model")
        )
        # 万舟チャンスの展示後版（manshu.py）。版の名前は manshu_latest.json を取ってから読む
        try:
            import manshu as _manshu

            _data_paths.sync_from_github(
                token, repo=repo, files=(_manshu.LATEST_FILE,), dest=result["dir"]
            )
            model_files = tuple(model_files) + _manshu.app_files()
        except Exception as e:
            print("[DATA] manshu sync error", type(e).__name__, flush=True)
        if model_files:
            more = _data_paths.sync_from_github(
                token, repo=repo, files=model_files, dest=result["dir"]
            )
            for k in ("updated", "kept"):
                ml[k] += more[k]
            ml["errors"].update(more["errors"])
        print(
            "[DATA] ml synced", "updated=", ml["updated"], "kept=", ml["kept"],
            "errors=", ml["errors"], flush=True,
        )
    except Exception as e:
        print("[DATA] ml sync error", type(e).__name__, flush=True)
    # 今までのモデル（毎朝 Actions が学習して保存したもの。アプリは読むだけで学習しない）
    try:
        legacy = _data_paths.sync_from_github(
            token, repo=repo, files=_legacy_model.APP_FILES, dest=result["dir"]
        )
        print(
            "[DATA] legacy synced", "updated=", legacy["updated"], "kept=", legacy["kept"],
            "errors=", legacy["errors"], flush=True,
        )
    except Exception as e:
        print("[DATA] legacy sync error", type(e).__name__, flush=True)
    return result


_perf.lap("データ同期")
_boat_ai_sync_data()
_perf.lap("準備（設定の差し替え）")
if not all(_data_paths.data_path(n).exists() for n in _TRAIN_DATA_FILES):
    st.error(
        "学習データを取得できませんでした。管理者は Streamlit の Secrets の "
        "DATA_REPO_TOKEN と、データ用リポジトリへのアクセス権を確認してください。"
    )
    st.stop()


def _boat_ai_legacy_cache_key():
    """保存済みの今までのモデルのキャッシュキー（データ用リポジトリの blob SHA）。

    Secrets にトークンが無い手元の実行では blob SHA が無いので、更新日時とサイズを使う。
    """
    sha = _data_paths.synced_sha(_legacy_model.MODEL_FILE)
    if sha:
        return ("sha", sha, _data_paths.synced_sha(_legacy_model.INFO_FILE))
    try:
        stat = _data_paths.data_path(_legacy_model.MODEL_FILE).stat()
        return ("stat", stat.st_mtime_ns, stat.st_size)
    except OSError:
        return ("missing",)


@st.cache_resource(show_spinner=False, max_entries=1)
def _boat_ai_legacy_cached(cache_key):
    """保存済みの今までのモデルを読む（学習はしない）。古い版はメモリに残さない（max_entries=1）。

    戻り値 {"model", "lane_agnostic", "info", "error", "stale"}。読めなければ model は None。
    """
    try:
        model, lane_agnostic, info = _legacy_model.load()
    except _legacy_model.LegacyModelUnavailable as e:
        print("[LEGACY] 今までのモデルが読めません:", e, flush=True)
        return {"model": None, "lane_agnostic": None, "info": _legacy_model.read_info(), "error": str(e),
                "stale": []}
    stale = _legacy_model.stale_sources(
        info, {n: _data_paths.synced_sha(n) for n in _legacy_model.SOURCE_FILES}
    )
    print(
        "[LEGACY] 今までのモデルを読み込み", info.get("created_at"),
        "sample=", info.get("sample_history_rows"), "position=", info.get("position_model_rows"),
        "sklearn=", (info.get("versions") or {}).get("sklearn"),
        ("学習データがアプリの取得分と違う: " + ",".join(stale)) if stale else "",
        flush=True,
    )
    return {"model": model, "lane_agnostic": lane_agnostic, "info": info, "error": None, "stale": stale}


def _boat_ai_legacy():
    return _boat_ai_legacy_cached(_boat_ai_legacy_cache_key())


def _boat_ai_lane_agnostic(race):
    """艇番なしモデルの最強艇（predict_runtime.lane_agnostic）。"""
    return _predict_runtime.lane_agnostic(race)


_predict_runtime.install(_boat_ai_legacy)

JST = ZoneInfo("Asia/Tokyo")


def _supabase_settings_config():
    try:
        cfg = st.secrets.get("supabase", {})
        url = str(cfg.get("url", "") or "").strip().rstrip("/")
        key = str(cfg.get("key", "") or "").strip()
        return url, key
    except Exception:
        return "", ""


@st.cache_data(ttl=30, show_spinner=False)
def _load_random_auto_settings():
    # 設定タブを描くたび（再実行のたび）に読んでいたので30秒は使い回す。保存したら捨てる
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
    _load_random_auto_settings.clear()


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
_perf.lap("画面の頭（app_core の読み込み）")
try:
    exec(
        compile(_core.read_text(encoding="utf-8"), str(_core), "exec"),
        globals(),
        globals(),
    )
finally:
    # st.stop()・st.rerun() で止まったときも出す（止まった時点までの分）
    _perf.finish(
        _BOAT_AI_RUN, min_seconds=0.05,
        extra=f" {st.session_state.get('auth_role') or '未ログイン'}",
    )
