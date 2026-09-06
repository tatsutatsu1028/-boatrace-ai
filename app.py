import json
import hmac
import time
import re
import base64
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import extra_streamlit_components as stx
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
import requests

from official_fetcher import VENUES, fetch_official_race, fetch_odds3t, fetch_race_result
from today_schedule_fetcher import fetch_today_schedule, fetch_venue_deadlines
from prediction import train, predict, trifecta, rank_tickets, confidence, assess_favorite_risk, research_prediction_variants
from stake_allocator import allocate_stakes_smart
from original_exhibition_ocr import extract_original_exhibition, OCR_AVAILABLE
from result_tracker import (
    load_results,
    load_analysis_view,
    save_race_result,
    delete_result,
    result_exists,
    metrics as validation_metrics,
    calibration_table,
    load_settings,
    save_settings,
    odds_tracking_available,
    add_to_odds_watchlist,
    save_odds_snapshot_now,
    load_odds_history,
    snapshot_payout_from_official,
    deactivate_odds_watchlist,
    load_prediction_snapshot,
    save_prediction_snapshot,
    supabase_config,
)
# スレッズ投稿は任意機能。threads_poster.py を置いていない場合でも
# アプリ本体は動くようにしておく（未導入で全体が落ちるのを防ぐ）。
try:
    from threads_poster import (
        build_post_text as threads_build_post_text,
        post_text as threads_post_text,
        load_config as threads_load_config,
        save_config as threads_save_config,
        token_age_days as threads_token_age_days,
        fetch_user_id as threads_fetch_user_id,
        TEXT_LIMIT as THREADS_TEXT_LIMIT,
    )
    THREADS_AVAILABLE = True
except Exception:
    THREADS_AVAILABLE = False
    THREADS_TEXT_LIMIT = 500

    def threads_fetch_user_id(*args, **kwargs):
        raise RuntimeError("threads_poster.py が未導入です。")

    def threads_build_post_text(*args, **kwargs):
        return ""

    def threads_post_text(*args, **kwargs):
        raise RuntimeError("threads_poster.py が未導入です。")

    def threads_load_config(*args, **kwargs):
        return None

    def threads_save_config(*args, **kwargs):
        raise RuntimeError("threads_poster.py が未導入です。")

    def threads_token_age_days(*args, **kwargs):
        return None
st.set_page_config(page_title="BOAT AI Mobile", page_icon="🚤", layout="centered", initial_sidebar_state="collapsed")
st.markdown("""
<style>
.block-container{max-width:760px;padding-top:.8rem;padding-bottom:5rem}
.stButton>button{width:100%;min-height:3rem;border-radius:14px;font-weight:700}
.ticket{padding:.8rem 1rem;border:1px solid rgba(150,150,150,.28);border-radius:14px;margin:.45rem 0}
.small{opacity:.72;font-size:.86rem}
.money{margin-top:.45rem;font-size:1.02rem;font-weight:700}
/* 会場グリッドはスマホ幅でもStreamlit標準の縦積みにせず、
   横4列のまま表示する（狭い画面でも横スクロールなしで一覧できるように）。 */
div[class*="st-key-venue_grid"] [data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important;gap:.35rem!important}
div[class*="st-key-venue_grid"] [data-testid="column"],
div[class*="st-key-venue_grid"] [data-testid="stColumn"]{min-width:0!important;flex:1 1 0!important;width:auto!important;padding:0 .15rem!important}
div[class*="st-key-venue_grid"] .stButton>button{min-height:2.7rem;font-size:.76rem;padding:.3rem .15rem;white-space:normal;line-height:1.15}
div[class*="st-key-venue_grid"] .stCaption{text-align:center;font-size:.68rem}
</style>
""", unsafe_allow_html=True)

st.title("🚤 BOAT AI Mobile")
st.caption("公式情報＋展示データを合わせて、3連単を『本線・抑え・穴』に整理")

def _secret_text(name):
    try:
        return str(st.secrets.get(name, "") or "").strip()
    except Exception:
        return ""


AUTH_COOKIE_NAME = "boat_ai_auth_v2"


@st.cache_data(ttl=60, show_spinner=False)
def _research_rule_status_map():
    """保存済みレース用に、研究ルールA/B/C/Dの確定判定だけを取得する。"""
    try:
        url, key = supabase_config()
        if not url or not key:
            return {}

        endpoint = f"{url.rstrip('/')}/rest/v1/rpc/get_research_rule_statuses"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        r = requests.post(endpoint, headers=headers, json={}, timeout=15)
        r.raise_for_status()
        rows = r.json() or []

        out = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            race_key = str(row.get("race_key", "") or "").strip()
            if not race_key:
                continue

            active = []
            if bool(row.get("rule_a_eligible")):
                active.append("A")
            if bool(row.get("rule_b_eligible")):
                active.append("B")
            if bool(row.get("rule_c_eligible")):
                active.append("C")
            if bool(row.get("rule_d_eligible")):
                active.append("D")

            out[race_key] = {
                "label": "・".join(active) if active else "—",
                "A": bool(row.get("rule_a_eligible")),
                "B": bool(row.get("rule_b_eligible")),
                "C": bool(row.get("rule_c_eligible")),
                "D": bool(row.get("rule_d_eligible")),
            }
        return out
    except Exception:
        return {}


@st.cache_data(ttl=60, show_spinner=False)
def _research_rule_d_progress():
    """Supabase RPCからD対象数だけを取得する。研究テーブル本体は公開しない。"""
    try:
        url, key = supabase_config()
        if not url or not key:
            return None
        endpoint = f"{url.rstrip('/')}/rest/v1/rpc/get_rule_d_progress"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        r = requests.post(endpoint, headers=headers, json={}, timeout=10)
        r.raise_for_status()
        value = r.json()
        # scalar RPC は数値そのもの、環境によっては1要素配列で返る場合にも対応。
        if isinstance(value, list) and value:
            value = value[0]
        if isinstance(value, dict):
            value = value.get("get_rule_d_progress", value.get("count"))
        return int(value) if value is not None else None
    except Exception:
        return None


def _current_research_rule_status(final, tickets, odds_history):
    """
    AI予想画面用の研究ルール判定。
    Aは予想時点で確定可能。B/C/Dは追跡中オッズの現時点値による暫定判定。
    本番予想・買い目・資金配分は変更しない。
    """
    try:
        f = final.copy()
        f["p_first"] = pd.to_numeric(f["p_first"], errors="coerce")
        p1_prob = float(f["p_first"].max()) if len(f) else 0.0
    except Exception:
        p1_prob = 0.0

    t = tickets.copy() if tickets is not None else pd.DataFrame()
    if len(t):
        t["stake"] = pd.to_numeric(t.get("stake", 0), errors="coerce").fillna(0).astype(int)
        t["prob"] = pd.to_numeric(t.get("prob", 0), errors="coerce").fillna(0.0)
        purchased = t[t["stake"] > 0].copy()
    else:
        purchased = pd.DataFrame()

    mainline = purchased[purchased.get("group", pd.Series(index=purchased.index, dtype=str)).astype(str).str.strip() == "本線"] if len(purchased) else purchased
    a_ok = bool(p1_prob >= 0.80 and len(mainline))

    bounds = {}
    if odds_history is not None and len(odds_history):
        h = odds_history.copy()
        if "fetched_at" in h.columns:
            h = h.sort_values("fetched_at")
        for combo, g in h.groupby("combo", sort=False):
            vals = pd.to_numeric(g["odds"], errors="coerce").dropna()
            if len(vals):
                bounds[str(combo).strip()] = {
                    "first": float(vals.iloc[0]),
                    "last": float(vals.iloc[-1]),
                    "snapshots": int(len(vals)),
                }

    b_tickets = []
    if p1_prob >= 0.70:
        for _, row in mainline.iterrows():
            combo = str(row.get("combo", "")).strip()
            bound = bounds.get(combo)
            if bound and float(row.get("prob", 0) or 0) * bound["last"] >= 1.20:
                b_tickets.append(combo)
    b_ok = bool(b_tickets)

    c_tickets = []
    for _, row in purchased.iterrows():
        combo = str(row.get("combo", "")).strip()
        bound = bounds.get(combo)
        if not bound or bound["snapshots"] < 2 or bound["first"] <= 0:
            continue
        first_ev = float(row.get("prob", 0) or 0) * bound["first"]
        change_pct = (bound["last"] - bound["first"]) / bound["first"] * 100.0
        if first_ev >= 1.20 and -10.0 <= change_pct < 10.0:
            c_tickets.append(combo)
    c_ok = bool(c_tickets)
    d_ok = bool(b_ok and c_ok)

    return {"A": a_ok, "B": b_ok, "C": c_ok, "D": d_ok, "p1_prob": p1_prob, "snapshots": int(odds_history["fetched_at"].nunique()) if odds_history is not None and len(odds_history) and "fetched_at" in odds_history.columns else 0}
AUTH_TTL_SECONDS = 24 * 60 * 60

JST = ZoneInfo("Asia/Tokyo")


def _today_jst():
    """Streamlit Cloudのサーバー時刻に依存せず、日本時間の今日を返す。"""
    return datetime.now(JST).date()



def _staff_accounts():
    """Secrets の STAFF_<ID>_PIN を自動収集する。"""
    accounts = {}

    try:
        items = dict(st.secrets).items()
    except Exception:
        items = []

    for key, value in items:
        m = re.fullmatch(r"STAFF_([A-Za-z0-9_]+)_PIN", str(key))
        if not m:
            continue

        pin = str(value or "").strip()
        if not pin:
            continue

        staff_id = m.group(1)
        accounts[staff_id] = {
            "pin": pin,
            "collector_name": f"{staff_id}さん",
        }

    return accounts


def _auth_secret():
    explicit = _secret_text("AUTH_COOKIE_SECRET")
    if explicit:
        return explicit.encode("utf-8")

    staff_part = "|".join(
        sorted(
            f"{staff_id}:{info['pin']}"
            for staff_id, info in _staff_accounts().items()
        )
    )
    fallback = _secret_text("ADMIN_PIN") + "|" + staff_part
    return hashlib.sha256(fallback.encode("utf-8")).digest()


def _encode_field(value):
    return base64.urlsafe_b64encode(
        str(value).encode("utf-8")
    ).decode("ascii")


def _decode_field(value):
    return base64.urlsafe_b64decode(
        str(value).encode("ascii")
    ).decode("utf-8")


def _sign_auth(role, collector_name, expires_at):
    collector_enc = _encode_field(collector_name)
    payload = f"{role}|{collector_enc}|{int(expires_at)}"
    sig = hmac.new(
        _auth_secret(),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    raw = f"{payload}|{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _verify_auth(token):
    if not token:
        return None

    try:
        raw = base64.urlsafe_b64decode(
            str(token).encode("ascii")
        ).decode("utf-8")

        role, collector_enc, expires_s, sig = raw.split("|", 3)

        if role not in {"admin", "staff"}:
            return None

        expires_at = int(expires_s)
        if expires_at <= int(time.time()):
            return None

        payload = f"{role}|{collector_enc}|{expires_at}"
        expected = hmac.new(
            _auth_secret(),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(sig, expected):
            return None

        collector_name = _decode_field(collector_enc)

        if role == "admin":
            collector_name = "owner"
        else:
            valid_names = {
                info["collector_name"]
                for info in _staff_accounts().values()
            }
            if collector_name not in valid_names:
                return None

        return {
            "role": role,
            "collector_name": collector_name,
        }
    except Exception:
        return None


def _get_cookie_manager():
    if "_cookie_manager" not in st.session_state:
        st.session_state["_cookie_manager"] = stx.CookieManager(
            key="boat_ai_cookie_manager"
        )
    return st.session_state["_cookie_manager"]


def _login_gate():
    admin_pin = _secret_text("ADMIN_PIN")
    staff_accounts = _staff_accounts()

    if not admin_pin:
        st.error(
            "ログイン設定が未完了です。Streamlit Secrets に "
            "ADMIN_PIN を登録してください。"
        )
        st.stop()

    if not staff_accounts:
        st.warning(
            "スタッフPINが未登録です。"
            "STAFF_A_PIN のような形式でSecretsに追加できます。"
        )

    cookie_manager = _get_cookie_manager()

    if not st.session_state.get("_cookie_bootstrap_done"):
        st.session_state["_cookie_bootstrap_done"] = True
        with st.spinner("ログイン状態を確認しています…"):
            time.sleep(1.5)

    if st.session_state.get("auth_role") not in {"admin", "staff"}:
        cookies = cookie_manager.get_all(key="boat_ai_auth_read")
        token = (cookies or {}).get(AUTH_COOKIE_NAME)
        restored = _verify_auth(token)

        if restored:
            st.session_state["auth_role"] = restored["role"]
            st.session_state["collector_name"] = restored["collector_name"]

    if st.session_state.get("auth_role") not in {"admin", "staff"}:
        st.subheader("🔐 ログイン")
        pin = st.text_input("PIN", type="password", key="login_pin")

        if st.button("ログイン", key="login_button"):
            role = None
            collector_name = None

            if hmac.compare_digest(pin, admin_pin):
                role = "admin"
                collector_name = "owner"
            else:
                for info in staff_accounts.values():
                    if hmac.compare_digest(pin, info["pin"]):
                        role = "staff"
                        collector_name = info["collector_name"]
                        break

            if role is None:
                st.error("PINが違います。")
                st.stop()

            expires_ts = int(time.time()) + AUTH_TTL_SECONDS
            token = _sign_auth(role, collector_name, expires_ts)

            cookie_manager.set(
                AUTH_COOKIE_NAME,
                token,
                key=f"boat_ai_auth_set_{int(time.time() * 1000)}",
                path="/",
                expires_at=datetime.now() + timedelta(hours=24),
                max_age=AUTH_TTL_SECONDS,
                secure=True,
                same_site="strict",
            )

            st.session_state["auth_role"] = role
            st.session_state["collector_name"] = collector_name
            st.session_state.pop("login_pin", None)

            with st.spinner("ログイン情報を保存しています…"):
                time.sleep(0.8)

            st.rerun()

        st.stop()

    role = st.session_state.get("auth_role")
    collector = st.session_state.get(
        "collector_name",
        "owner" if role == "admin" else "スタッフ",
    )

    globals()["IS_ADMIN"] = role == "admin"
    globals()["COLLECTOR_NAME"] = collector

    st.caption(
        f"👤 {collector} / "
        f"{'管理者' if role == 'admin' else '収集スタッフ'}"
    )

    if st.button("ログアウト", key="logout_button"):
        try:
            cookie_manager.delete(
                AUTH_COOKIE_NAME,
                key=f"boat_ai_auth_delete_{int(time.time() * 1000)}",
            )
            time.sleep(0.5)
        except Exception:
            pass

        for _key in (
            "auth_role",
            "collector_name",
            "login_pin",
            "_cookie_bootstrap_done",
        ):
            st.session_state.pop(_key, None)

        st.rerun()


_login_gate()

@st.cache_data
def load_demo_history():
    return pd.read_csv(Path(__file__).parent / "sample_history.csv")

def safe_name(v):
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none"} else s

def ensure_columns(df, defaults):
    out = df.copy()
    for c, default in defaults.items():
        if c not in out.columns:
            out[c] = default
    return out

def _virtual_ticket_rows(raw):
    """保存済みtickets_jsonを安全にlist[dict]へ戻す。"""
    if raw is None:
        return []

    try:
        if pd.isna(raw):
            return []
    except Exception:
        pass

    try:
        parsed = json.loads(str(raw))
    except Exception:
        return []

    if not isinstance(parsed, list):
        return []

    return [item for item in parsed if isinstance(item, dict)]


def _virtual_backtest(results_df, min_p1_prob=None, groups=None):
    """
    保存済みの固定買い目から「見送るレース／買わない区分」を差し引いた場合の
    仮想成績を再計算する。

    重要:
    - 新しい買い目を追加したり、購入額を再配分したりはしない。
    - 保存済み買い目の一部だけを残す研究なので、未来情報を使わない。
    - 的中買い目を残した場合の払戻は保存済みの実受取額をそのまま使う。
    """
    if results_df is None or len(results_df) == 0:
        return {
            "対象R": 0,
            "購入点数": 0,
            "的中R": 0,
            "的中率": np.nan,
            "投資": 0,
            "払戻": 0,
            "収支": 0,
            "回収率": np.nan,
        }

    allowed_groups = None if groups is None else {str(g) for g in groups}

    races = 0
    ticket_count = 0
    hit_races = 0
    total_stake = 0
    total_payout = 0

    for _, row in results_df.iterrows():
        try:
            p1_prob = float(row.get("p1_prob"))
        except Exception:
            p1_prob = np.nan

        if min_p1_prob is not None:
            if not np.isfinite(p1_prob) or p1_prob < float(min_p1_prob):
                continue

        tickets = _virtual_ticket_rows(row.get("tickets_json", ""))
        selected = []

        for item in tickets:
            if allowed_groups is not None and str(item.get("group", "")) not in allowed_groups:
                continue

            try:
                stake = int(float(item.get("stake", 0) or 0))
            except Exception:
                stake = 0

            if stake <= 0:
                continue

            selected.append((item, stake))

        stake_sum = sum(stake for _, stake in selected)
        if stake_sum <= 0:
            continue

        races += 1
        ticket_count += len(selected)
        total_stake += stake_sum

        actual_combo = str(row.get("trifecta_actual", "")).strip().lstrip("'")
        selected_hit = any(
            str(item.get("combo", "")).strip().lstrip("'") == actual_combo
            for item, _ in selected
        )

        if selected_hit:
            hit_races += 1
            try:
                race_payout = int(float(row.get("payout", 0) or 0))
            except Exception:
                race_payout = 0
            total_payout += max(race_payout, 0)

    profit = total_payout - total_stake
    hit_rate = hit_races / races if races else np.nan
    recovery = total_payout / total_stake if total_stake else np.nan

    return {
        "対象R": int(races),
        "購入点数": int(ticket_count),
        "的中R": int(hit_races),
        "的中率": hit_rate,
        "投資": int(total_stake),
        "払戻": int(total_payout),
        "収支": int(profit),
        "回収率": recovery,
    }


def _virtual_backtest_table(results_df):
    """代表的な仮想購入ルールを同条件で比較する。"""
    rules = [
        ("現行", None, None),
        ("本命70%以上", 0.70, None),
        ("本命75%以上", 0.75, None),
        ("本命80%以上", 0.80, None),
        ("本命85%以上", 0.85, None),
        ("本線のみ", None, {"本線"}),
        ("抑えなし", None, {"本線", "穴"}),
        ("80%以上＋本線のみ", 0.80, {"本線"}),
        ("85%以上＋本線のみ", 0.85, {"本線"}),
    ]

    rows = []
    for label, threshold, groups in rules:
        result = _virtual_backtest(
            results_df,
            min_p1_prob=threshold,
            groups=groups,
        )
        result["仮想ルール"] = label
        rows.append(result)

    out = pd.DataFrame(rows)
    return out[
        [
            "仮想ルール",
            "対象R",
            "購入点数",
            "的中R",
            "的中率",
            "投資",
            "払戻",
            "収支",
            "回収率",
        ]
    ]


def race_key(d, jcd, rno):
    return f"{d.strftime('%Y%m%d')}_{jcd}_{int(rno)}"

@st.cache_data(ttl=1800, show_spinner=False)
def cached_fetch_race(date_str, jcd, rno):
    """
    公式データ取得結果をサーバー側（プロセス単位）でキャッシュする。

    スマホのブラウザはバックグラウンド化などでWebSocket接続が切れやすく、
    再接続時にst.session_stateがリセットされることがある。
    session_stateだけに頼ると「せっかく取得したデータが消えた」状態に
    なるため、同じレース（date/jcd/rno）の再取得はここから即座に
    復元できるようにしておく。
    """
    return fetch_official_race(date_str, jcd, rno)

@st.cache_data(ttl=1800, show_spinner=False)
def cached_fetch_odds(date_str, jcd, rno):
    return fetch_odds3t(date_str, jcd, rno)

@st.cache_data(ttl=86400, show_spinner=False)
def cached_fetch_schedule(date_str):
    """
    開催会場一覧は日付単位で固定する。
    date_str がキャッシュキーになるため、日本時間の日付が変わると
    新しい日付の開催情報を取得する。同じ日付では最大24時間再利用する。
    """
    return fetch_today_schedule(date_str)


@st.cache_data(ttl=86400, show_spinner=False)
def cached_fetch_deadlines(date_str, jcd):
    """
    選択会場の1R〜12R締切予定時刻も日付・会場単位で固定する。
    同じ日付・同じ会場ではアプリを閉じて開き直しても
    Streamlitサーバー側キャッシュを再利用する。
    """
    return fetch_venue_deadlines(date_str, jcd)


def _deadline_minutes_left(d, hhmm):
    """日本時間で締切までの残り分数を返す。"""
    try:
        hour, minute = map(int, str(hhmm).split(":"))
        deadline = datetime(
            d.year, d.month, d.day, hour, minute, tzinfo=JST
        )
        return (deadline - datetime.now(JST)).total_seconds() / 60.0
    except Exception:
        return None


def _deadline_html(rno, hhmm, d):
    """
    全R締切一覧用HTML。
    0〜30分以内の締切だけ時刻を赤系で強調する。
    """
    mins = _deadline_minutes_left(d, hhmm)
    if mins is not None and 0 <= mins <= 30:
        time_html = (
            f'<span style="color:#e53935;font-weight:800">{hhmm}</span>'
        )
    else:
        time_html = f'<span style="font-weight:700">{hhmm}</span>'

    return (
        '<div style="padding:.30rem .18rem;text-align:center;'
        'border:1px solid rgba(150,150,150,.22);border-radius:10px;'
        'margin:.12rem 0;font-size:.88rem">'
        f'<b>{int(rno)}R</b><br>{time_html}</div>'
    )

def missing_summary(df):
    checks = [
        ("racer_win_rate", "全国勝率"),
        ("local_win_rate", "当地勝率"),
        ("motor_2ren", "モーター2連率"),
        ("boat_2ren", "ボート2連率"),
        ("avg_st", "平均ST"),
    ]
    out = []
    for col, label in checks:
        n = pd.to_numeric(df.get(col, pd.Series(dtype=float)), errors="coerce").notna().sum()
        if n < 6:
            out.append(f"{label}({n}/6)")
    return out

st.session_state.setdefault("race", None)
st.session_state.setdefault("odds", None)
st.session_state.setdefault("race_context", None)

tab1, tab2, tab3, tab4 = st.tabs(["🎯 予想", "🧠 学習データ", "⚙️ 設定", "📊 検証"])

with tab2:
    st.subheader("学習データ")
    if IS_ADMIN:
        hist_up = st.file_uploader("過去成績CSV", type="csv", key="hist")
        history = pd.read_csv(hist_up) if hist_up else load_demo_history()
        if hist_up:
            st.success(f"アップロードした学習データ：{len(history):,}行")
        else:
            st.warning(f"動作確認用の合成データ {len(history):,}行を使用中。実運用前に公式過去データへ置き換えてください。")
        with st.expander("学習データ先頭を見る"):
            st.dataframe(history.head(12), use_container_width=True, hide_index=True)
    else:
        history = load_demo_history()
        st.info("🔒 収集スタッフは学習データを変更できません。管理者と同じ既定データを使用します。")

with tab3:
    st.subheader("買い目設定")
    if not IS_ADMIN:
        st.info("🔒 収集スタッフは設定を保存できません。管理者の保存設定を使用します。")

    if "app_settings" not in st.session_state:
        st.session_state["app_settings"] = load_settings()
    saved = st.session_state["app_settings"]

    main_n = st.number_input("🔥 本線 点数", 1, 10, int(saved.get("main_n", 3)))
    cover_n = st.number_input("🛟 抑え 点数", 0, 10, int(saved.get("cover_n", 3)))
    hole_n = st.number_input(
        "💎 穴 点数", 0, 10, int(saved.get("hole_n", 0)),
        help=(
            "検証111レースでは穴は106点買って的中ゼロ（回収率0%）でした。"
            "平均的中確率が1%前後しかなく、控除率25%の中では勝ちにくい帯です。"
            "既定を0にしています。"
        ),
    )
    if int(hole_n) > 0:
        st.caption(
            "⚠️ 検証データ上、穴の回収率は0%（106点・13,200円で的中ゼロ）でした。"
        )

    st.divider()
    total_budget = st.number_input(
        "💴 1レース予算",
        min_value=500,
        max_value=100000,
        value=int(saved.get("total_budget", 2000)),
        step=100,
    )
    min_bet = st.number_input(
        "1点あたり最低購入額",
        min_value=100,
        max_value=1000,
        value=int(saved.get("min_bet", 100)),
        step=100,
    )
    longshot_min_prob_pct = st.slider(
        "穴の最低的中確率（%）",
        min_value=0.0,
        max_value=2.0,
        value=float(saved.get("longshot_min_prob_pct", 0.30)),
        step=0.05,
        help="原則、この確率未満の超低確率買い目は穴から除外します。",
    )
    value_bias = st.slider(
        "💰 妙味重視度（回収率志向）", 0.0, 1.0, float(saved.get("value_bias", 0.0)), 0.1,
        help=(
            "0は従来通り本線を厚めに配分。上げるほど「本線・抑え・穴」という"
            "カテゴリの序列より期待値（市場オッズに対するAIの優位性＝妙味）を"
            "重視した配分になります。的中率より回収率を狙うなら高めに。"
        ),
    )

    st.divider()
    _style_options = ["バランス", "展示重視"]
    _saved_style = saved.get("prediction_style", "バランス")
    prediction_style = st.selectbox(
        "🧭 予想スタイル",
        _style_options,
        index=_style_options.index(_saved_style) if _saved_style in _style_options else 0,
    )

    if prediction_style == "展示重視":
        default_display = 0.42
    else:
        default_display = 0.32

    display_weight = st.slider(
        "展示情報の反映度", 0.0, 0.8, default_display, 0.02,
        key=f"display_weight_{prediction_style}",
    )
    weather_weight = st.slider(
        "天候（風・波）の反映度", 0.0, 0.5, 0.10, 0.02,
        key=f"weather_weight_{prediction_style}",
        help="風速5m/s以上または波高3cm以上の荒れ水面で、アウトコース（5・6号艇）を不利側に補正します。",
    )
    venue_course_weight = st.slider(
        "場のコース特性（逃げ率・決まり手）の反映度", 0.0, 0.5, 0.12, 0.02,
        key=f"venue_course_weight_{prediction_style}",
        help="選手個人の実績ではなく、その場自体が「インが強いか」「まくりが決まりやすいか」という特性をBOAT RACE公式データから反映します。",
    )
    hedge_enabled = st.toggle(
        "🛟 本命艇の保険買い目", value=True,
        key=f"hedge_enabled_{prediction_style}",
        help="本命買い目には毎回、本命艇（多くは1号艇）がどこかしらの着順で含まれがちです。展示・モーター/ボート・今節成績を総合して本命艇に不安要素が多いと判定された場合、穴の1点を本命艇を含まない組み合わせに差し替え、「本命艇が飛ぶ」リスクに備えます。",
    )

    st.divider()
    if IS_ADMIN and st.button("💾 この設定を保存（次回起動時も復元）"):
        new_settings = {
            "main_n": int(main_n),
            "cover_n": int(cover_n),
            "hole_n": int(hole_n),
            "total_budget": int(total_budget),
            "min_bet": int(min_bet),
            "longshot_min_prob_pct": float(longshot_min_prob_pct),
            "value_bias": float(value_bias),
            "prediction_style": prediction_style,
        }
        if save_settings(new_settings):
            st.session_state["app_settings"] = new_settings
            st.success("設定を保存しました。アプリを再起動しても復元されます。")
        else:
            st.error("設定の保存に失敗しました。")

    # -------------------------------------------------
    # Threads（スレッズ）連携
    # -------------------------------------------------
    # 投稿はオーナー（管理者）だけの機能。収集スタッフには設定画面ごと
    # 見せない。アクセストークンは公開アカウントへの投稿権限そのもので、
    # 画面に出る＝渡すのと同じ意味になるため。
    if IS_ADMIN:
        st.divider()
        st.subheader("🧵 スレッズ連携")

        _sb_url, _sb_key = supabase_config()

        if not THREADS_AVAILABLE:
            st.info(
                "スレッズ投稿は未導入です。使う場合は threads_poster.py を"
                "リポジトリに追加してください。追加しなくてもアプリは通常どおり動きます。"
            )
        elif not _sb_url or not _sb_key:
            st.info(
                "スレッズ投稿はSupabaseの設定が必要です。"
                "トークンを安全に保存し、自動更新するために使います。"
            )
        else:
            _threads_cfg = threads_load_config(_sb_url, _sb_key)

            if _threads_cfg:
                _age = threads_token_age_days(_threads_cfg)
                if _age is None:
                    st.success("✅ 連携済みです。")
                elif _age >= 50:
                    st.error(
                        f"⚠️ トークンの更新から{_age}日経過しています。"
                        "60日で失効します。GitHub Actionsが動いていない可能性があるため、"
                        "下から再登録してください。"
                    )
                else:
                    st.success(
                        f"✅ 連携済みです。（トークン更新から{_age}日経過 / 期限60日）"
                        " GitHub Actionsが自動で更新します。"
                    )

                # 保存されているユーザーIDが正しいかは、投稿するまで分からない。
                # 誤ったIDを入れてしまった場合に、トークンを貼り直さずに
                # 直せるようにしておく（トークンはSupabaseに保存済みなので
                # そこから引き直せる）。
                st.caption(f"登録中のユーザーID: `{_threads_cfg.get('user_id', '')}`")

                if st.button(
                    "🔄 ユーザーIDをトークンから取り直す",
                    key="refetch_threads_user_id",
                ):
                    try:
                        with st.spinner("アカウント情報を取得しています…"):
                            _uid2, _uname2 = threads_fetch_user_id(
                                _threads_cfg["access_token"]
                            )
                        # token_updated_at は引き継ぐ。ここで now にすると
                        # 自動更新の起点がずれてしまうため。
                        threads_save_config(
                            _sb_url,
                            _sb_key,
                            _uid2,
                            _threads_cfg["access_token"],
                            token_updated_at=_threads_cfg.get("token_updated_at"),
                        )
                        st.success(
                            f"@{_uname2} のユーザーID（{_uid2}）に更新しました。"
                            if _uname2
                            else f"ユーザーIDを {_uid2} に更新しました。"
                        )
                        st.rerun()
                    except Exception as e:
                        st.error("ユーザーIDの取得に失敗しました。")
                        st.code(str(e))
            else:
                st.info(
                    "未連携です。Meta for Developersで取得した"
                    "ユーザーIDと長期アクセストークンを登録してください。"
                )

            with st.expander("トークンを登録・更新する", expanded=not _threads_cfg):
                st.caption(
                    "必要な権限は threads_basic と threads_content_publish の2つです。"
                    "登録した長期トークンは、以後GitHub Actionsが自動で延長します。"
                )
                _ttoken = st.text_input(
                    "長期アクセストークン",
                    value="",
                    type="password",
                    key="threads_token_input",
                    help="貼り付けると保存されます。既存のトークンは表示されません。",
                )
                _tid = st.text_input(
                    "ThreadsユーザーID（空欄でOK）",
                    value=str((_threads_cfg or {}).get("user_id", "")),
                    key="threads_user_id_input",
                    help=(
                        "空欄のまま保存すると、トークンから自動で取得します。"
                        "手入力する場合は数字のみです。"
                    ),
                )

                if st.button("🧵 スレッズ連携を保存", key="save_threads_config"):
                    if not _ttoken.strip():
                        st.error("長期アクセストークンを入力してください。")
                    else:
                        try:
                            # ユーザーIDが空ならトークンから引く。
                            # 先に引いてユーザーネームを表示することで、
                            # 別アカウントのトークンを貼っていないか確認できる。
                            _uid = _tid.strip()
                            _uname = ""
                            if not _uid:
                                with st.spinner("トークンからアカウント情報を取得しています…"):
                                    _uid, _uname = threads_fetch_user_id(_ttoken.strip())

                            threads_save_config(_sb_url, _sb_key, _uid, _ttoken)

                            if _uname:
                                st.success(f"保存しました。連携アカウント: @{_uname}")
                            else:
                                st.success("保存しました。予想タブから投稿できます。")
                            st.rerun()
                        except Exception as e:
                            st.error("保存に失敗しました。")
                            st.code(str(e))


# 収集スタッフは画面上の一時操作で予想条件を変えられないよう、
# 実際に予想へ渡す値を管理者の保存設定へ戻す。
if not IS_ADMIN:
    main_n = int(saved.get("main_n", 3))
    cover_n = int(saved.get("cover_n", 3))
    hole_n = int(saved.get("hole_n", 0))
    total_budget = int(saved.get("total_budget", 2000))
    min_bet = int(saved.get("min_bet", 100))
    longshot_min_prob_pct = float(saved.get("longshot_min_prob_pct", 0.30))
    value_bias = float(saved.get("value_bias", 0.0))
    prediction_style = str(saved.get("prediction_style", "バランス"))
    display_weight = 0.42 if prediction_style == "展示重視" else 0.32
    weather_weight = 0.10
    venue_course_weight = 0.12
    hedge_enabled = True


with tab4:
    st.subheader("📊 予想検証")
    if not IS_ADMIN:
        st.caption("🔒 収集スタッフは検証画面を閲覧のみで利用します。")
    results_df = load_results()
    st.markdown("### 📈 AI成績分析")

    # Supabaseの分析ビューをまとめて表示
    analysis_views = [
        ("📊 総合", "summary"),
        ("🎯 1号艇予測確率帯", "p1_prob"),
        ("🎫 買い目区分", "ticket_group"),
        ("💰 期待値帯", "expected_return"),
    ]

    analysis_tabs = st.tabs([label for label, _ in analysis_views])

    for analysis_tab, (label, view_name) in zip(analysis_tabs, analysis_views):
        with analysis_tab:
            analysis_df = load_analysis_view(view_name)

            if analysis_df.empty:
                st.info(f"{label} の分析データはまだありません。")
            else:
                st.dataframe(
                    analysis_df,
                    use_container_width=True,
                    hide_index=True,
                )
    vm = validation_metrics(results_df)

    if vm["races"] == 0:
        st.info("まだ検証データがありません。AI予想後に実着順と払戻を登録すると、ここへ蓄積されます。")
    else:
        m1, m2 = st.columns(2)
        with m1:
            st.metric("検証レース数", f"{vm['races']}R")
            st.metric(
                "1着トップ予想 的中率",
                f"{vm['first_hit_rate']*100:.1f}%" if pd.notna(vm["first_hit_rate"]) else "-"
            )
            st.metric(
                "購入買い目 的中率",
                f"{vm['ticket_hit_rate']*100:.1f}%" if pd.notna(vm["ticket_hit_rate"]) else "-"
            )
        with m2:
            st.metric("累計収支", f"{vm['profit']:+,}円")
            st.metric(
                "回収率",
                f"{vm['roi']*100:.1f}%" if pd.notna(vm["roi"]) else "-"
            )
            st.metric(
                "簡易Brier score",
                f"{vm['brier_first']:.3f}" if pd.notna(vm["brier_first"]) else "-"
            )

        st.caption("Brier score は小さいほど、予測確率と実際の結果のズレが小さい指標です。")

        st.markdown("#### 1着予測の確率校正")
        cal = calibration_table(results_df)
        st.dataframe(cal, use_container_width=True, hide_index=True)

        st.markdown("#### 🧪 仮想バックテスト")
        st.caption(
            "保存済みの固定予想を使い、『そのレースを見送っていたら／一部の買い目区分を買わなかったら』"
            "を再計算します。実際の予想ロジック・設定・保存データは変更しません。"
        )

        virtual_compare = _virtual_backtest_table(results_df)
        virtual_show = virtual_compare.copy()
        virtual_show["的中率"] = (
            pd.to_numeric(virtual_show["的中率"], errors="coerce") * 100
        )
        virtual_show["回収率"] = (
            pd.to_numeric(virtual_show["回収率"], errors="coerce") * 100
        )

        st.dataframe(
            virtual_show,
            use_container_width=True,
            hide_index=True,
            column_config={
                "的中率": st.column_config.NumberColumn(format="%.1f%%"),
                "回収率": st.column_config.NumberColumn(format="%.1f%%"),
                "投資": st.column_config.NumberColumn(format="%d円"),
                "払戻": st.column_config.NumberColumn(format="%d円"),
                "収支": st.column_config.NumberColumn(format="%+d円"),
            },
        )

        with st.expander("🔬 条件を変えて試す", expanded=False):
            custom_prob_pct = st.slider(
                "AI本命確率の最低ライン",
                min_value=0,
                max_value=90,
                value=80,
                step=5,
                format="%d%%",
                key="virtual_bt_min_prob",
            )
            custom_groups = st.multiselect(
                "残す買い目区分",
                ["本線", "抑え", "穴"],
                default=["本線"],
                key="virtual_bt_groups",
            )

            if custom_groups:
                custom_bt = _virtual_backtest(
                    results_df,
                    min_p1_prob=custom_prob_pct / 100.0 if custom_prob_pct > 0 else None,
                    groups=set(custom_groups),
                )

                bc1, bc2 = st.columns(2)
                with bc1:
                    st.metric("対象レース", f"{custom_bt['対象R']}R")
                    st.metric("的中レース", f"{custom_bt['的中R']}R")
                    st.metric(
                        "的中率",
                        f"{custom_bt['的中率']*100:.1f}%"
                        if pd.notna(custom_bt["的中率"])
                        else "-",
                    )
                with bc2:
                    st.metric("仮想収支", f"{custom_bt['収支']:+,}円")
                    st.metric(
                        "仮想回収率",
                        f"{custom_bt['回収率']*100:.1f}%"
                        if pd.notna(custom_bt["回収率"])
                        else "-",
                    )
                    st.metric("仮想投資", f"{custom_bt['投資']:,}円")
            else:
                st.info("少なくとも1つの買い目区分を選んでください。")

        st.caption(
            "※ この仮想バックテストは『保存済み買い目を削る／レースを見送る』比較専用です。"
            "未購入だった新規買い目の追加や、購入額の再配分までは再現しません。"
        )

        st.markdown("#### 保存済みレース")

        # 研究ルールは結果確定後に research_rule_results へ保存された
        # A/B/C/D の確定判定を表示する。予想画面の「暫定判定」とは別物。
        _rule_status_map = _research_rule_status_map()

        show_cols = [
            "race_date","venue","race_no","trifecta_actual","p1_lane","p1_prob",
            "total_stake","payout","profit","roi"
        ]
        show = results_df[show_cols].copy()

        if "race_key" in results_df.columns:
            show["研究ルール"] = (
                results_df["race_key"]
                .astype(str)
                .map(lambda k: _rule_status_map.get(k, {}).get("label", "未判定"))
            )
        else:
            show["研究ルール"] = "未判定"

        show = show.rename(columns={
            "race_date":"日付",
            "venue":"場",
            "race_no":"R",
            "trifecta_actual":"実3連単",
            "p1_lane":"AI本命",
            "p1_prob":"本命確率",
            "total_stake":"購入",
            "payout":"払戻",
            "profit":"収支",
            "roi":"回収倍率",
        })

        # スマホで最初に見えるよう、研究ルールをRのすぐ後ろへ移動。
        show = show[[
            "日付", "場", "R", "研究ルール", "実3連単", "AI本命", "本命確率",
            "購入", "払戻", "収支", "回収倍率"
        ]]

        st.caption("研究ルールはレース終了後に確定したA/B/C/D判定です。『—』はどのルールにも非該当、『未判定』は研究結果がまだ保存されていないレースです。")
        st.dataframe(
            show.sort_values(["日付","場","R"], ascending=False),
            use_container_width=True,
            hide_index=True,
        )

        csv_export_results = results_df.copy()
        for _c in ("trifecta_actual", "top_ticket"):
            if _c in csv_export_results.columns:
                # combo表記("3-1-4"等)をExcelが日付だと誤解釈するのを防ぐ
                csv_export_results[_c] = "'" + csv_export_results[_c].astype(str)

        csv_export_results = csv_export_results.rename(columns={
            "saved_at": "保存日時",
            "race_date": "日付",
            "venue": "場",
            "race_no": "R",
            "race_key": "レースキー",
            "first_actual": "実1着",
            "second_actual": "実2着",
            "third_actual": "実3着",
            "trifecta_actual": "実3連単",
            "p1_lane": "AI本命艇",
            "p1_prob": "本命確率",
            "top_ticket": "本命買い目",
            "top_ticket_prob": "本命買い目確率",
            "top_ticket_odds": "本命買い目オッズ",
            "top_ticket_stake": "本命買い目購入額",
            "total_stake": "購入合計",
            "payout": "払戻",
            "profit": "収支",
            "roi": "回収倍率",
            "hit_top_ticket": "本命的中",
            "hit_any_ticket": "いずれか的中",
            "predicted_first_hit": "1着予想的中",
            "tickets_json": "買い目詳細",
            "lane_probs_json": "6艇予測詳細",
        })

        csv_results = csv_export_results.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 検証履歴CSV保存",
            csv_results,
            file_name="prediction_results.csv",
            mime="text/csv",
        )

        # -------------------------------------------------
        # 研究用：補正を1段ずつ足したときの長期成績比較
        # -------------------------------------------------
        research_rows = []
        for _, _rr in results_df.iterrows():
            raw = _rr.get("lane_probs_json", "")
            if not raw or pd.isna(raw):
                continue

            try:
                parsed = json.loads(raw)
            except Exception:
                continue

            if not isinstance(parsed, dict):
                continue

            variants_saved = parsed.get("research", {})
            if not isinstance(variants_saved, dict) or not variants_saved:
                continue

            try:
                actual_first = int(_rr.get("first_actual"))
            except Exception:
                continue

            for label, lanes in variants_saved.items():
                if not isinstance(lanes, list) or not lanes:
                    continue

                probs = {}
                for item in lanes:
                    try:
                        lane_no = int(item.get("lane"))
                        prob = float(item.get("p_first"))
                    except Exception:
                        continue
                    if 1 <= lane_no <= 6 and np.isfinite(prob):
                        probs[lane_no] = prob

                if len(probs) < 2:
                    continue

                top_lane = max(probs, key=probs.get)
                actual_prob = probs.get(actual_first, np.nan)
                brier6 = np.mean([
                    (probs.get(lane, 0.0) - (1.0 if lane == actual_first else 0.0)) ** 2
                    for lane in range(1, 7)
                ])

                research_rows.append({
                    "方式": label,
                    "本命的中": int(top_lane == actual_first),
                    "6艇Brier": float(brier6),
                    "実1着確率": actual_prob,
                })

        if research_rows:
            research_df = pd.DataFrame(research_rows)
            research_summary = (
                research_df.groupby("方式", sort=False)
                .agg(
                    検証R=("本命的中", "size"),
                    本命的中率=("本命的中", "mean"),
                    六艇Brier=("6艇Brier", "mean"),
                    実1着平均確率=("実1着確率", "mean"),
                )
                .reset_index()
            )
            research_summary["本命的中率"] = (research_summary["本命的中率"] * 100).round(1)
            research_summary["六艇Brier"] = research_summary["六艇Brier"].round(4)
            research_summary["実1着平均確率"] = (research_summary["実1着平均確率"] * 100).round(1)

            st.markdown("#### 🧪 研究用・補正別の長期比較")
            st.caption(
                "本番予想は『現行全部入り』のまま固定。ここは各補正を段階的に足した研究結果だけを比較します。"
                "6艇Brierは小さいほど良好です。"
            )
            st.dataframe(
                research_summary,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "本命的中率": st.column_config.NumberColumn(format="%.1f%%"),
                    "実1着平均確率": st.column_config.NumberColumn(format="%.1f%%"),
                },
            )
        else:
            st.info(
                "🧪 補正別比較データはまだありません。研究比較機能追加後に保存したレースから自動で蓄積されます。"
            )

        st.markdown("#### 🔍 1レース詳細比較")
        st.caption("保存済みレースを選ぶと、AI予想（6艇の確率・買い目）と実際の結果を並べて確認できます。")

        detail_df = results_df.sort_values(["race_date", "venue", "race_no"], ascending=False).reset_index(drop=True)
        detail_labels = [
            f"{r['race_date']} {r['venue']} {int(r['race_no'])}R（実{r['trifecta_actual']}）"
            for _, r in detail_df.iterrows()
        ]

        if detail_labels:
            sel_idx = st.selectbox(
                "レースを選択",
                range(len(detail_labels)),
                format_func=lambda i: detail_labels[i],
                key="detail_race_select",
            )
            sel = detail_df.iloc[sel_idx]

            actual_combo = str(sel["trifecta_actual"])
            actual_lanes = actual_combo.split("-") if "-" in actual_combo else []

            st.markdown(f"##### {sel['race_date']} {sel['venue']} {int(sel['race_no'])}R")

            _detail_rule = "未判定"
            if "race_key" in sel.index:
                _detail_rule = _rule_status_map.get(
                    str(sel.get("race_key", "")), {}
                ).get("label", "未判定")
            st.caption(f"🧪 確定研究ルール：{_detail_rule}")

            dc1, dc2, dc3 = st.columns(3)
            with dc1:
                st.metric("実際の3連単", actual_combo)
            with dc2:
                st.metric("購入合計", f"{int(sel.get('total_stake', 0) or 0):,}円")
            with dc3:
                payout_v = sel.get("payout", 0)
                profit_v = sel.get("profit", 0)
                st.metric("払戻 / 収支", f"{int(payout_v or 0):,}円", delta=f"{int(profit_v or 0):+,}円")

            lane_probs_raw = sel.get("lane_probs_json", "")
            try:
                lane_probs_parsed = json.loads(lane_probs_raw) if lane_probs_raw and pd.notna(lane_probs_raw) else []
            except Exception:
                lane_probs_parsed = []

            # 旧データはlist、新データは {final: [...], research: {...}} 形式。
            if isinstance(lane_probs_parsed, dict):
                lane_probs = lane_probs_parsed.get("final", [])
                detail_research = lane_probs_parsed.get("research", {})
            else:
                lane_probs = lane_probs_parsed
                detail_research = {}

            if lane_probs:
                st.markdown("###### 6艇の予測確率 vs 実際の着順")
                lp = pd.DataFrame(lane_probs)
                lp["lane"] = pd.to_numeric(lp["lane"], errors="coerce").astype("Int64")

                def _actual_rank(lane_no):
                    lane_s = str(lane_no)
                    if lane_s in actual_lanes:
                        return actual_lanes.index(lane_s) + 1
                    return None

                lp["実着順"] = lp["lane"].apply(_actual_rank)
                lp = lp.sort_values("p_first", ascending=False)

                view = lp.rename(columns={
                    "lane": "艇",
                    "racer_name": "選手",
                    "p_first": "AI1着予測確率",
                    "reason": "根拠",
                })
                view["AI1着予測確率"] = (pd.to_numeric(view["AI1着予測確率"], errors="coerce") * 100).round(1)
                view["実着順"] = view["実着順"].apply(lambda v: f"{v}着" if v else "着外/不明")

                st.dataframe(
                    view[["艇", "選手", "AI1着予測確率", "実着順", "根拠"]],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "AI1着予測確率": st.column_config.NumberColumn(format="%.1f%%"),
                    },
                )
            else:
                st.info("このレースは6艇分の予測確率データが保存される前に記録されたため、詳細比較はできません。")

            if detail_research:
                rows = []
                try:
                    actual_first = int(sel.get("first_actual"))
                except Exception:
                    actual_first = None

                for label, lanes in detail_research.items():
                    if not isinstance(lanes, list) or not lanes:
                        continue
                    probs = {}
                    for item in lanes:
                        try:
                            probs[int(item.get("lane"))] = float(item.get("p_first"))
                        except Exception:
                            pass
                    if not probs:
                        continue
                    top_lane = max(probs, key=probs.get)
                    rows.append({
                        "方式": label,
                        "本命艇": top_lane,
                        "本命確率": probs[top_lane] * 100,
                        "実1着確率": probs.get(actual_first, np.nan) * 100 if actual_first else np.nan,
                        "本命的中": "○" if actual_first and top_lane == actual_first else "",
                    })

                if rows:
                    st.markdown("###### 🧪 このレースの補正別比較")
                    st.dataframe(
                        pd.DataFrame(rows),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "本命確率": st.column_config.NumberColumn(format="%.1f%%"),
                            "実1着確率": st.column_config.NumberColumn(format="%.1f%%"),
                        },
                    )

            tickets_raw = sel.get("tickets_json", "")
            try:
                tickets_saved = json.loads(tickets_raw) if tickets_raw and pd.notna(tickets_raw) else []
            except Exception:
                tickets_saved = []

            if tickets_saved:
                st.markdown("###### 購入した買い目 vs 結果")
                tv = pd.DataFrame(tickets_saved)
                tv["的中"] = tv["combo"].astype(str).apply(lambda c: "🎯 的中" if c == actual_combo else "")
                tv["prob"] = (pd.to_numeric(tv.get("prob"), errors="coerce") * 100).round(2)

                tv = tv.rename(columns={
                    "combo": "買い目", "group": "区分", "prob": "的中確率(%)",
                    "odds": "オッズ", "expected_return": "期待値", "stake": "購入額",
                })
                cols = [c for c in ["買い目", "区分", "的中確率(%)", "オッズ", "期待値", "購入額", "的中"] if c in tv.columns]
                st.dataframe(tv[cols], use_container_width=True, hide_index=True)
            else:
                st.caption("このレースは購入した買い目の記録がありません。")




with tab1:
    d = st.date_input("日付", value=_today_jst())

    st.session_state.setdefault("selected_jcd", "01")

    st.markdown("### 🏟️ 会場を選択")
    try:
        schedule = cached_fetch_schedule(d.strftime("%Y%m%d"))
        schedule_by_jcd = {
            str(row["jcd"]): row for row in schedule.to_dict("records")
        }
    except Exception as e:
        st.caption("本日の開催状況を取得できませんでした（会場は手動で選べます）。")
        st.code(str(e))
        schedule_by_jcd = {}

    venue_codes = list(VENUES.keys())
    cols_per_row = 4

    venue_grid = st.container(key="venue_grid")

    for i in range(0, len(venue_codes), cols_per_row):
        row_codes = venue_codes[i:i + cols_per_row]
        cols = venue_grid.columns(len(row_codes))

        for col, code in zip(cols, row_codes):
            info = schedule_by_jcd.get(code, {})
            holding = bool(info.get("holding"))
            is_selected = st.session_state["selected_jcd"] == code

            # 開催有無はアイコンだけで判別。開催日目・開催中・休み等の
            # 補助テキストはスマホ画面をすっきりさせるため表示しない。
            marker = "🟢" if holding else "▫️"
            label = f"{marker} {VENUES[code]}"

            with col:
                if st.button(
                    label,
                    key=f"venue_btn_{code}",
                    use_container_width=True,
                    type="primary" if is_selected else "secondary",
                ):
                    st.session_state["selected_jcd"] = code
                    st.rerun()

    jcd = st.session_state["selected_jcd"]
    st.info(f"選択中の会場：{jcd} {VENUES[jcd]}")

    # 会場を選んだら、その日の1R〜12R締切予定時刻を一度取得して固定。
    # 締切30分以内のレースだけ「時刻」を赤系で強調する。
    try:
        deadlines = cached_fetch_deadlines(d.strftime("%Y%m%d"), jcd)
    except Exception:
        deadlines = {}

    # R選択と締切表示を一体化。
    # カードをタップした時点で、そのRの公式データ取得まで実行する。
    st.session_state.setdefault("selected_rno", 12)
    auto_odds = st.toggle("3連単オッズも取得", value=True)

    st.markdown("#### ⏰ レース・締切予定時刻")
    st.caption("Rと締切時刻のカードをタップすると、そのレースの公式データを取得します。")

    # 締切30分以内はカード文字を赤で強調。
    # Streamlitのkey付きcontainerは st-key-<key> のCSSクラスになるため、
    # 対象Rだけ安全に色を変えられる。
    near_deadline = []
    for rr in range(1, 13):
        hhmm = deadlines.get(rr, "--:--") if deadlines else "--:--"
        mins = _deadline_minutes_left(d, hhmm)
        if mins is not None and 0 <= mins <= 30:
            near_deadline.append(rr)

    if near_deadline:
        css = ["<style>"]
        for rr in near_deadline:
            css.append(
                f".st-key-race_deadline_{rr} button p "
                "{color:#e53935 !important;font-weight:800 !important;}"
            )
        css.append("</style>")
        st.markdown("".join(css), unsafe_allow_html=True)

    # v1.10.12:
    # 3行×4列のStreamlitボタンを使う。選択中Rの背景色は付けず全R同色にする。
    # on_click callback は本体の再実行より先に selected_rno を更新するため、
    # 「6Rを押したのに12Rが選択表示」のようなズレを防げる。
    def _queue_race_fetch(rr):
        st.session_state["selected_rno"] = int(rr)
        st.session_state["_pending_race_fetch"] = int(rr)

    st.markdown(
        """
        <style>
        /* 各行を4列のCSS Gridとして固定。スマホでも縦1列化しない。 */
        div[class*="st-key-race_row_"] [data-testid="stHorizontalBlock"]{
            display:grid !important;
            grid-template-columns:repeat(4, minmax(0, 1fr)) !important;
            gap:5px !important;
            width:100% !important;
        }
        div[class*="st-key-race_row_"] [data-testid="column"]{
            width:auto !important;
            min-width:0 !important;
            flex:none !important;
        }
        div[class*="st-key-race_row_"] button{
            width:100% !important;
            min-height:50px !important;
            height:50px !important;
            padding:2px 0 !important;
            border-radius:9px !important;
        }
        div[class*="st-key-race_row_"] button p{
            margin:0 !important;
            font-size:11px !important;
            line-height:1.05 !important;
            white-space:pre-line !important;
            font-weight:800 !important;
        }
        @media (max-width:480px){
            div[class*="st-key-race_row_"] [data-testid="stHorizontalBlock"]{
                gap:4px !important;
            }
            div[class*="st-key-race_row_"] button{
                min-height:46px !important;
                height:46px !important;
            }
            div[class*="st-key-race_row_"] button p{
                font-size:10px !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    current_selected = int(st.session_state.get("selected_rno", 12))

    # 締切30分以内のRだけ時刻文字を赤くする。
    near_deadline = []
    for rr in range(1, 13):
        hhmm = deadlines.get(rr, "--:--") if deadlines else "--:--"
        mins = _deadline_minutes_left(d, hhmm)
        if mins is not None and 0 <= mins <= 30:
            near_deadline.append(rr)

    if near_deadline:
        _near_css = ["<style>"]
        for rr in near_deadline:
            _near_css.append(
                f".st-key-race_btn_wrap_{rr} button p "
                "{color:#e53935 !important;font-weight:900 !important;}"
            )
        _near_css.append("</style>")
        st.markdown("".join(_near_css), unsafe_allow_html=True)

    # 1-4R / 5-8R / 9-12R の3段。
    for row_start in (1, 5, 9):
        with st.container(key=f"race_row_{row_start}"):
            cols = st.columns(4)
            for col, rr in zip(cols, range(row_start, row_start + 4)):
                hhmm = deadlines.get(rr, "--:--") if deadlines else "--:--"
                with col:
                    with st.container(key=f"race_btn_wrap_{rr}"):
                        st.button(
                            f"{rr}R\n{hhmm}",
                            key=f"race_pick_btn_{rr}",
                            use_container_width=True,
                            type="secondary",
                            on_click=_queue_race_fetch,
                            args=(rr,),
                        )

    requested_rno = st.session_state.pop("_pending_race_fetch", None)
    fetch_requested = requested_rno is not None
    if fetch_requested:
        # callbackですでにselected_rnoは更新済みだが、念のため同期する。
        st.session_state["selected_rno"] = int(requested_rno)

    if not deadlines:
        st.caption("締切予定時刻は取得できませんでしたが、Rカードから公式データ取得はできます。")
    else:
        st.caption("締切30分以内のレースは赤で強調表示します。")

    rno = int(st.session_state.get("selected_rno", 12))
    ctx = race_key(d, jcd, rno)

    if st.session_state.get("race_context") not in (None, ctx) and not fetch_requested:
        st.info("会場・Rが変更されています。Rカードをタップすると公式データを取得します。")

    if fetch_requested:
        try:
            with st.spinner(f"{VENUES[jcd]} {rno}R の公式ページを読み込み中…"):
                # Rカードを明示的にタップした時は最新データを取得する。
                cached_fetch_race.clear()
                cached_fetch_odds.clear()
                race = cached_fetch_race(d.strftime("%Y%m%d"), jcd, rno)
                odds = cached_fetch_odds(d.strftime("%Y%m%d"), jcd, rno) if auto_odds else None
            st.session_state["race"] = race
            st.session_state["odds"] = odds
            st.session_state["race_context"] = ctx
            st.session_state.pop("result", None)
            st.query_params["fetched_ctx"] = ctx
            st.success(f"{VENUES[jcd]} {rno}R の公式データを取得しました。")
        except Exception as e:
            st.error("自動取得できませんでした。手動入力も利用できます。")
            st.code(str(e))

    race = st.session_state.get("race") if st.session_state.get("race_context") == ctx else None

    if race is None and st.query_params.get("fetched_ctx") == ctx:
        # 接続が切れてsession_stateが失われたケース。以前に取得済みの
        # 目印があるので、キャッシュから静かに復元を試みる
        # （キャッシュが生きていれば通信は発生せず一瞬で戻る）。
        try:
            with st.spinner("接続が切れたため復元しています…"):
                race = cached_fetch_race(d.strftime("%Y%m%d"), jcd, rno)
                odds = cached_fetch_odds(d.strftime("%Y%m%d"), jcd, rno) if auto_odds else None
            st.session_state["race"] = race
            st.session_state["odds"] = odds
            st.session_state["race_context"] = ctx
            st.info("接続切れから公式データを復元しました。")
        except Exception:
            race = None

    if race is None:
        st.info("公式データ取得を押すか、手動入力を作成してください。")
        if st.button("✍️ このレースの手動入力を作る"):
            race = pd.DataFrame({
                "date":[d.isoformat()]*6,"venue":[VENUES[jcd]]*6,"race_no":[rno]*6,"lane":range(1,7),
                "racer_name":[""]*6,"racer_class":[""]*6,"racer_win_rate":[np.nan]*6,"local_win_rate":[np.nan]*6,
                "motor_2ren":[np.nan]*6,"boat_2ren":[np.nan]*6,"avg_st":[0.16]*6,
                "exhibition_time":[np.nan]*6,"exhibition_st":[np.nan]*6,"weight":[np.nan]*6,"tilt":[np.nan]*6,
                "wind_speed":[np.nan]*6,"wave_height":[np.nan]*6,"temperature":[np.nan]*6,
            })
            st.session_state["race"] = race
            st.session_state["race_context"] = ctx
            st.session_state["odds"] = None
            st.rerun()

    if race is not None:
        race = ensure_columns(race, {
            "lane":np.nan,"racer_name":"","racer_class":"","racer_win_rate":np.nan,"local_win_rate":np.nan,
            "motor_2ren":np.nan,"boat_2ren":np.nan,"avg_st":np.nan,
            "exhibition_time":np.nan,"exhibition_st":np.nan,"weight":np.nan,"tilt":np.nan,
            "wind_speed":np.nan,"wave_height":np.nan,"temperature":np.nan,
        }).sort_values("lane").reset_index(drop=True)
        st.markdown("### 📊 今節成績")

        meet_cols = [
            "lane",
            "racer_name",
            "current_meet_avg_finish",
            "current_meet_top2_rate",
            "current_meet_avg_st",
            "current_meet_races",
        ]

        existing_meet_cols = [
            c for c in meet_cols
            if c in race.columns
        ]

        meet_display = race[existing_meet_cols].copy()

        meet_display = meet_display.rename(columns={
            "lane": "艇",
            "racer_name": "選手",
            "current_meet_avg_finish": "今節平均着順",
            "current_meet_top2_rate": "今節2連対率",
            "current_meet_avg_st": "今節平均ST",
            "current_meet_races": "今節走数",
        })

        st.dataframe(
            meet_display,
            width="stretch",
            hide_index=True,
        )
        st.markdown("### 📐 コース適性")

        course_cols = [
            "lane",
            "racer_name",
            "course_top3_rate",
            "course_avg_st",
            "course_start_rank",
        ]

        existing_course_cols = [
            c for c in course_cols
            if c in race.columns
        ]

        course_display = race[existing_course_cols].copy()

        course_display = course_display.rename(columns={
            "lane": "艇",
            "racer_name": "選手",
            "course_top3_rate": "コース3連対率",
            "course_avg_st": "コース平均ST",
            "course_start_rank": "コースST順位",
        })

        st.dataframe(
            course_display,
            width="stretch",
            hide_index=True,
        )
        st.markdown("### ① 選手・機力データ")
        basic_cols = ["lane","racer_name","racer_class","racer_win_rate","local_win_rate","motor_2ren","boat_2ren","avg_st"]
        basic = st.data_editor(
            race[basic_cols], use_container_width=True, hide_index=True, num_rows="fixed", key=f"basic_{ctx}",
            column_config={
                "lane":st.column_config.NumberColumn("艇", disabled=True, format="%d"),
                "racer_name":st.column_config.TextColumn("選手"),
                "racer_class":st.column_config.SelectboxColumn("級別", options=["", "A1", "A2", "B1", "B2"]),
                "racer_win_rate":st.column_config.NumberColumn("全国勝率", format="%.2f"),
                "local_win_rate":st.column_config.NumberColumn("当地勝率", format="%.2f"),
                "motor_2ren":st.column_config.NumberColumn("モーター2連率", format="%.2f"),
                "boat_2ren":st.column_config.NumberColumn("ボート2連率", format="%.2f"),
                "avg_st":st.column_config.NumberColumn("平均ST", format="%.3f"),
            })
        miss = missing_summary(basic)
        if miss:
            st.warning("未取得・不足あり：" + " / ".join(miss))
        else:
            st.success("選手・機力の主要データは6艇分そろっています。")

        st.markdown("### ② 展示・直前データ")
        expo_cols = ["lane","exhibition_time","exhibition_st","weight","tilt","wind_speed","wave_height","temperature"]
        expo = st.data_editor(
            race[expo_cols], use_container_width=True, hide_index=True, num_rows="fixed", key=f"expo_{ctx}",
            column_config={
                "lane":st.column_config.NumberColumn("艇", disabled=True, format="%d"),
                "exhibition_time":st.column_config.NumberColumn("展示タイム", format="%.2f"),
                "exhibition_st":st.column_config.NumberColumn("展示ST", format="%.2f"),
                "weight":st.column_config.NumberColumn("体重kg", format="%.1f"),
                "tilt":st.column_config.NumberColumn("チルト", format="%.1f"),
                "wind_speed":st.column_config.NumberColumn("風速m/s", format="%.1f"),
                "wave_height":st.column_config.NumberColumn("波高cm", format="%.1f"),
                "temperature":st.column_config.NumberColumn("気温℃", format="%.1f"),
            })
        n_display = pd.to_numeric(expo["exhibition_time"], errors="coerce").notna().sum()
        if n_display == 6:
            st.success("展示タイム：6艇分取得済み")
        elif n_display:
            st.warning(f"展示タイム：{n_display}/6艇")
        else:
            st.info("展示タイム未取得。展示前なら正常です。")

        edited = race.copy()

        for c in basic_cols:
            edited[c] = basic[c].to_numpy()
        for c in expo_cols:
            if c != "lane":
                edited[c] = expo[c].to_numpy()

        st.markdown("### ③ オリジナル展示")
        st.caption("直線・まわり足・1周タイム等を独自公開している場だけ入力。データがない場は空欄のままでOKです。")

        _orig_auto_cols = ["original_straight", "original_turn", "original_lap"]

        if OCR_AVAILABLE:
            with st.expander("📷 画像から自動入力（オリジナル展示のスクリーンショット）", expanded=False):
                st.caption(
                    "オリジナル展示のページのスクリーンショットをアップロードすると、"
                    "一周・まわり足・直線のタイムを自動で読み取って下の表に仮入力します。"
                    "OCR（文字認識）による読み取りのため誤りが混ざることがあります。"
                    "反映後は必ず下の表で数値を確認・修正してください。"
                )
                ocr_file = st.file_uploader(
                    "画像を選択", type=["png", "jpg", "jpeg"], key=f"orig_upload_{ctx}",
                )
                if ocr_file is not None and st.button("この画像から読み取る", key=f"orig_ocr_btn_{ctx}"):
                    with st.spinner("画像を解析中…"):
                        ocr_df = extract_original_exhibition(ocr_file.getvalue())
                    if ocr_df is None or ocr_df.empty:
                        st.warning(
                            "表を読み取れませんでした。表全体がはっきり写っている画像か、"
                            "拡大・トリミングして再度お試しください。"
                        )
                    else:
                        st.session_state[f"orig_ocr_data_{ctx}"] = ocr_df
                        st.session_state[f"orig_ver_{ctx}"] = (
                            st.session_state.get(f"orig_ver_{ctx}", 0) + 1
                        )
                        st.success("読み取りました。下の表に仮入力しています。数値を確認してください。")
                        st.rerun()

        orig = pd.DataFrame({"lane": range(1, 7)})
        for c in _orig_auto_cols:
            orig[c] = pd.to_numeric(race[c], errors="coerce") if c in race.columns else np.nan

        _ocr_df = st.session_state.get(f"orig_ocr_data_{ctx}")
        if _ocr_df is not None:
            orig = orig.drop(columns=_orig_auto_cols).merge(_ocr_df, on="lane", how="left")

        _orig_source = safe_name(race["original_exhibition_source"].dropna().iloc[0]) if (
            "original_exhibition_source" in race.columns
            and race["original_exhibition_source"].astype(str).str.strip().any()
        ) else ""

        _orig_ver = st.session_state.get(f"orig_ver_{ctx}", 0)
        orig = st.data_editor(
            orig, use_container_width=True, hide_index=True, num_rows="fixed", key=f"orig_{ctx}_{_orig_ver}",
            column_config={
                "lane":st.column_config.NumberColumn("艇", disabled=True, format="%d"),
                "original_straight":st.column_config.NumberColumn("直線", format="%.2f"),
                "original_turn":st.column_config.NumberColumn("まわり足", format="%.2f"),
                "original_lap":st.column_config.NumberColumn("1周", format="%.2f"),
            })
        if orig[["original_straight","original_turn","original_lap"]].apply(pd.to_numeric, errors="coerce").notna().any().any():
            if _orig_source:
                st.success(f"オリジナル展示を自動取得済み（{_orig_source}）。AI補正に使用します。")
            else:
                st.success("オリジナル展示をAI補正に使用します。")
        else:
            st.info("オリジナル展示：未取得・未入力。通常展示・基礎データ中心で予想します。")

        work = edited.merge(orig, on="lane", how="left")
        work["date"] = d.isoformat()
        work["venue"] = VENUES[jcd]
        work["race_no"] = rno

        st.markdown("### ④ 3連単オッズ")
        odds = st.session_state.get("odds")
        if odds is not None and len(odds):
            st.success(f"3連単オッズ {len(odds)}通りを自動取得")
        else:
            st.info("オッズなしでも確率予想は可能。期待値を出す場合はCSVを追加してください。")
            odds_up = st.file_uploader("オッズCSV（任意）", type="csv", key=f"oddsfile_{ctx}", help="combo,odds の2列。例：1-2-3,12.5")
            if odds_up:
                odds = pd.read_csv(odds_up)

        if odds_tracking_available():
            with st.expander("📈 オッズの時系列追跡", expanded=False):
                st.caption("登録すると、締切まで数分おきに自動でオッズを記録し、動きを確認できるようになります。")
                if st.button("この開催レースの追跡を開始", key=f"watch_{ctx}"):
                    ok, msg = add_to_odds_watchlist(d.strftime("%Y%m%d"), jcd, rno)
                    if ok:
                        st.success(msg)
                        saved_now, save_msg = save_odds_snapshot_now(
                            d.strftime("%Y%m%d"), jcd, rno, odds
                        )
                        if saved_now:
                            st.success(f"⚡ {save_msg}")
                        else:
                            st.warning(
                                f"{save_msg} 定期追跡は開始済みなので、"
                                "次回のGitHub Actionsでも保存を試みます。"
                            )
                    else:
                        st.error(msg)

                hist = load_odds_history(d.strftime("%Y%m%d"), jcd, rno)
                if len(hist):
                    st.caption(f"記録済み：{hist['fetched_at'].nunique()}時点分")
                    pivot = hist.pivot_table(index="fetched_at", columns="combo", values="odds")
                    # 動きが大きい（下落幅が大きい）上位5点だけをグラフ表示
                    if len(pivot.columns) and len(pivot) >= 2:
                        change = pivot.iloc[-1] - pivot.iloc[0]
                        top_movers = change.sort_values().head(5).index.tolist()
                        st.line_chart(pivot[top_movers])
                        st.caption("下落幅が大きい上位5買い目のオッズ推移（人気が集まっている＝妙味が薄れつつある買い目）")
                    else:
                        st.info("まだ記録が1時点分しかありません。もう少し待ってから確認してください。")
                else:
                    st.info("まだ追跡記録がありません。「追跡を開始」を押してから数分待ってください。")

        st.divider()
        if st.button("🤖 AI最終予想", type="primary"):
            try:
                with st.spinner("AI解析中…"):
                    model = train(history)
                    pre = work.copy()
                    pre["exhibition_time"] = np.nan
                    pre["original_straight"] = np.nan
                    pre["original_turn"] = np.nan
                    pre["original_lap"] = np.nan
                    pre["exhibition_st"] = np.nan
                    before = predict(model, pre, display_weight=0, weather_weight=0, venue_course_weight=0)
                    final = predict(model, work, display_weight=display_weight, weather_weight=weather_weight, venue_course_weight=venue_course_weight)

                    # 研究用の比較は別計算。final（本番予想）は一切変更しない。
                    research_variants = research_prediction_variants(
                        model,
                        work,
                        display_weight=display_weight,
                        weather_weight=weather_weight,
                        venue_course_weight=venue_course_weight,
                    )
                    tri = trifecta(final)

                    favorite_lane, risk_score, risk_reasons = assess_favorite_risk(work, final)
                    hedge_lane = favorite_lane if (hedge_enabled and risk_score >= 2) else None

                    tickets = rank_tickets(
                        tri,
                        odds,
                        main_n=int(main_n),
                        cover_n=int(cover_n),
                        longshot_n=int(hole_n),
                        longshot_min_prob=float(longshot_min_prob_pct) / 100.0,
                        hedge_lane=hedge_lane,
                    )
                    tickets = allocate_stakes_smart(
                        tickets,
                        budget=int(total_budget),
                        unit=100,
                        min_bet=int(min_bet),
                        max_longshot_share=0.15,
                        max_ticket_share=0.35,
                        value_bias=float(value_bias),
                    )
                    st.session_state["result"] = {
                        "context": ctx,
                        "before": before,
                        "final": final,
                        "tickets": tickets,
                        "work": work,
                        "hedge_lane": hedge_lane,
                        "risk_reasons": risk_reasons,
                        "research_variants": research_variants,
                    }
            except Exception as e:
                st.error("AI予想でエラーが発生しました。")
                st.code(str(e))

        result = st.session_state.get("result")
        if result and result.get("context") == ctx:
            before, final, tickets, work_result = result["before"], result["final"], result["tickets"], result["work"]

            if "stake" not in tickets.columns:
                tickets = allocate_stakes_smart(
                    tickets,
                    budget=int(total_budget),
                    unit=100,
                    min_bet=int(min_bet),
                    max_longshot_share=0.15,
                    max_ticket_share=0.35,
                    value_bias=float(value_bias),
                )
                st.session_state["result"]["tickets"] = tickets

            tickets = tickets.copy()
            tickets["stake"] = pd.to_numeric(tickets["stake"], errors="coerce").fillna(0).astype(int)

            st.divider()
            st.subheader(f"{VENUES[jcd]} {rno}R AI最終予想")

            mc1, mc2 = st.columns(2)
            with mc1:
                st.metric("AI総合信頼度", confidence(final, work_result))
            with mc2:
                total_stake_metric = int(tickets["stake"].sum())
                st.metric("推奨購入総額", f"{total_stake_metric:,}円")

            # -------------------------------------------------
            # 研究ルール A/B/C/D の現在判定（表示専用）
            # -------------------------------------------------
            _rule_hist = load_odds_history(d.strftime("%Y%m%d"), jcd, rno)
            _rule_status = _current_research_rule_status(final, tickets, _rule_hist)
            with st.expander("🧪 研究ルール判定", expanded=True):
                _a_mark = "✅ 該当" if _rule_status["A"] else "❌ 非該当"
                _b_mark = "✅ 暫定該当" if _rule_status["B"] else "❌ 暫定非該当"
                _c_mark = "✅ 暫定該当" if _rule_status["C"] else "❌ 暫定非該当"
                _d_mark = "🔥 暫定該当" if _rule_status["D"] else "❌ 暫定非該当"
                st.markdown(
                    f"**A：{_a_mark}**  — 本命80%以上＋本線  \n"
                    f"**B：{_b_mark}**  — 本命70%以上＋本線＋現在の追跡EV 1.20以上  \n"
                    f"**C：{_c_mark}**  — 初回EV 1.20以上＋オッズ変動 -10%〜+10%  \n"
                    f"**D：{_d_mark}**  — BとCが両方成立"
                )
                st.caption(
                    f"現在の本命確率：{_rule_status['p1_prob']:.1%} ／ "
                    f"オッズ記録：{_rule_status['snapshots']}時点。"
                    "B/C/Dは締切までオッズが動くため暫定判定です。研究表示のみで、本番予想・買い目・資金配分は変更しません。"
                )

                _d_count = _research_rule_d_progress()
                if _d_count is not None:
                    _goal = 50
                    _remain = max(0, _goal - _d_count)
                    st.progress(min(_d_count / _goal, 1.0))
                    if _d_count < _goal:
                        st.info(f"📈 ルールD進捗：{_d_count} / {_goal}R　あと{_remain}R")
                    else:
                        st.success(f"🎯 ルールDが{_d_count}Rに到達しました。第1回・未来データ耐久検証のタイミングです。")
                else:
                    st.caption("ルールDの累計件数は現在取得できませんでした。")

            # -------------------------------------------------
            # スレッズ投稿
            # -------------------------------------------------
            # 投稿できるのはオーナーだけ。収集スタッフの画面には出さない。
            _sb_url, _sb_key = supabase_config()
            _threads_cfg = (
                threads_load_config(_sb_url, _sb_key)
                if (IS_ADMIN and THREADS_AVAILABLE and _sb_url and _sb_key)
                else None
            )

            if _threads_cfg:
                with st.expander("🧵 スレッズに投稿", expanded=False):
                    _default_text = threads_build_post_text(
                        race_date=d.strftime("%-m/%-d") if hasattr(d, "strftime") else str(d),
                        venue=VENUES[jcd],
                        race_no=rno,
                        final=final,
                        tickets=tickets,
                    )

                    _posted_key = f"threads_posted_{ctx}"
                    if st.session_state.get(_posted_key):
                        st.success(
                            "✅ このレースは投稿済みです。"
                            f" 投稿ID: {st.session_state[_posted_key]}"
                        )

                    _text = st.text_area(
                        "投稿内容（送信前に編集できます）",
                        value=_default_text,
                        height=240,
                        key=f"threads_text_{ctx}",
                    )
                    _len = len(_text)
                    if _len > THREADS_TEXT_LIMIT:
                        st.error(f"{_len} / {THREADS_TEXT_LIMIT}文字（超過しています）")
                    else:
                        st.caption(f"{_len} / {THREADS_TEXT_LIMIT}文字")

                    if st.button(
                        "🧵 この内容でスレッズに投稿",
                        key=f"post_threads_{ctx}",
                        disabled=_len > THREADS_TEXT_LIMIT,
                    ):
                        try:
                            _post_id = threads_post_text(
                                _threads_cfg["user_id"],
                                _threads_cfg["access_token"],
                                _text,
                            )
                            st.session_state[_posted_key] = _post_id
                            st.success(f"投稿しました。（投稿ID: {_post_id}）")
                            st.rerun()
                        except Exception as e:
                            st.error("スレッズへの投稿に失敗しました。")
                            st.code(str(e))
                            st.caption(
                                "トークンが失効している可能性があります。"
                                "設定タブから再登録してください。"
                            )

            snapshot = load_prediction_snapshot(ctx)
            if snapshot:
                kind_label = (
                    "過去レース・バックテスト"
                    if snapshot.get("snapshot_kind") == "backtest"
                    else "当日予想"
                )
                st.success(
                    "📌 検証用予想は固定済みです。"
                    f" 固定時刻: {snapshot.get('saved_at', '-')} / {kind_label}。"
                    "このあとAIを再計算しても、結果保存では固定済み予想を使います。"
                )
            else:
                if d < _today_jst():
                    st.warning(
                        "📌 このレースは過去レースです。ここで固定するとバックテスト扱いになります。"
                        "買い目・回収率は締切後データが混ざる可能性があるため参考値として扱ってください。"
                    )
                    snapshot_kind = "backtest"
                else:
                    st.warning(
                        "📌 レース前に『この予想を検証用に固定』を押してください。"
                        "固定後はレース終了後に再取得・再予想しても、検証成績はこの時点の予想で判定します。"
                    )
                    snapshot_kind = "same_day"

                if st.button(
                    "📌 この予想を検証用に固定",
                    key=f"lock_prediction_{ctx}",
                ):
                    try:
                        snap = save_prediction_snapshot(
                            race_key=ctx,
                            race_date=d.isoformat(),
                            venue=VENUES[jcd],
                            race_no=rno,
                            final=final,
                            tickets=tickets,
                            research_variants=st.session_state["result"].get(
                                "research_variants",
                                {},
                            ),
                            snapshot_kind=snapshot_kind,
                            collector_name=COLLECTOR_NAME,
                        )
                        st.success(
                            "検証用予想を固定しました。"
                            f" 固定時刻: {snap.get('saved_at', '-')}"
                        )

                        # 本番（当日・レース前）で予想を固定したら、
                        # 同じレースをオッズ追跡対象へ自動登録する。
                        # バックテストでは終了後オッズを追跡しても意味がないため登録しない。
                        if snapshot_kind == "same_day" and odds_tracking_available():
                            ok, msg = add_to_odds_watchlist(
                                d.strftime("%Y%m%d"),
                                jcd,
                                rno,
                            )
                            if ok:
                                st.success(
                                    "📈 オッズ自動追跡も開始しました。"
                                    " GitHub Actionsが数分おきに記録します。"
                                )
                                saved_now, save_msg = save_odds_snapshot_now(
                                    d.strftime("%Y%m%d"), jcd, rno, odds
                                )
                                if saved_now:
                                    st.success(f"⚡ {save_msg}")
                                else:
                                    st.warning(
                                        f"{save_msg} 定期追跡は開始済みなので、"
                                        "次回のGitHub Actionsでも保存を試みます。"
                                    )
                            else:
                                # 予想固定自体は成功済みなので、追跡登録の失敗だけを警告する。
                                st.warning(
                                    "予想の固定は成功しましたが、"
                                    f"オッズ追跡の開始に失敗しました。 {msg}"
                                )

                        st.rerun()
                    except Exception as e:
                        st.error("検証用予想の固定に失敗しました。")
                        st.code(str(e))

            merged = final.merge(before[["lane","p_first"]], on="lane", suffixes=("_after","_before"))
            merged["変化"] = merged["p_first_after"] - merged["p_first_before"]
            merged = merged.sort_values("p_first_after", ascending=False)

            research_variants = st.session_state["result"].get("research_variants", {})
            if research_variants:
                with st.expander("🧪 研究用：補正を1段ずつ比較", expanded=False):
                    st.caption(
                        "本番表示・買い目は『現行全部入り』の結果をそのまま使用しています。"
                        "この表は研究用で、予想結果には影響しません。"
                    )
                    rows = []
                    for label, variant_df in research_variants.items():
                        ranked = variant_df.sort_values("p_first", ascending=False).reset_index(drop=True)
                        if len(ranked) == 0:
                            continue
                        top = ranked.iloc[0]
                        rows.append({
                            "方式": label,
                            "本命艇": int(top["lane"]),
                            "本命確率": float(top["p_first"]) * 100,
                        })
                    if rows:
                        st.dataframe(
                            pd.DataFrame(rows),
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "本命確率": st.column_config.NumberColumn(format="%.1f%%"),
                            },
                        )

            hedge_lane = st.session_state["result"].get("hedge_lane")
            risk_reasons = st.session_state["result"].get("risk_reasons", [])
            if hedge_lane is not None:
                st.warning(
                    f"🛟 {hedge_lane}号艇（本命）に不安要素あり（{' / '.join(risk_reasons)}）のため、"
                    f"穴の1点を{hedge_lane}号艇を含まない保険買い目に差し替えています。"
                )

            st.markdown("### 1着確率")
            for _, row in merged.iterrows():
                lane = int(row["lane"])
                nm = safe_name(row.get("racer_name", ""))

                st.markdown(
                    f"""<div class="ticket"><b>{lane}号艇 {nm}</b><br>
1着 <b>{row['p_first_after']*100:.1f}%</b>
<span class="small">展示前 {row['p_first_before']*100:.1f}% / {row['変化']*100:+.1f}pt</span><br>
<span class="small">{row['reason']}</span></div>""",
                    unsafe_allow_html=True
                )

            for group, emoji in [("本線","🔥"),("抑え","🛟"),("穴","💎")]:
                st.markdown(f"### {emoji} {group}")
                g = tickets[tickets["group"] == group]

                if len(g) == 0:
                    st.caption("なし")

                for _, row in g.iterrows():
                    oddtxt = f"{row['odds']:.1f}倍" if pd.notna(row.get("odds")) else "オッズ未取得"
                    evtxt = f" / 期待値 {row['expected_return']:.2f}" if pd.notna(row.get("expected_return")) else ""
                    stake = int(row.get("stake", 0) or 0)
                    stake_txt = f"{stake:,}円" if stake > 0 else "見送り"

                    st.markdown(
                        f"""<div class="ticket">
<b>{row['combo']}</b>　的中確率 <b>{row['prob']*100:.2f}%</b><br>
<span class="small">{oddtxt}{evtxt}</span><br>
<div class="money">💴 推奨 {stake_txt}</div>
<span class="small">{row.get('stake_reason', '')}</span>
</div>""",
                        unsafe_allow_html=True
                    )

            st.markdown("### 💴 購入配分")
            buy_view = tickets[["combo", "group", "stake"]].copy()
            buy_view = buy_view[buy_view["stake"] > 0]

            if len(buy_view):
                st.dataframe(
                    buy_view,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "combo":"買い目",
                        "group":"区分",
                        "stake":st.column_config.NumberColumn("購入額", format="%d円"),
                    }
                )
                st.success(f"購入合計：{int(buy_view['stake'].sum()):,}円")
            else:
                st.info("購入推奨額はありません。")

            csv_export = tickets.copy()
            if "combo" in csv_export.columns:
                # Excelは "3-1-4" のような買い目表記を日付だと誤解釈して
                # 例えば "2003/1/4" のように勝手に変換してしまうことがある。
                # 先頭に ' を付けるとExcel上ではテキスト扱いになり、
                # セルの見た目には出ない（数式バーにのみ残る）。
                csv_export["combo"] = "'" + csv_export["combo"].astype(str)

            csv_export = csv_export.rename(columns={
                "combo": "買い目",
                "prob": "的中確率",
                "odds": "オッズ",
                "expected_return": "期待値",
                "group": "区分",
                "stake": "購入額",
                "stake_reason": "配分理由",
            })

            csv = csv_export.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 買い目CSV保存",
                csv,
                file_name=f"{d}_{VENUES[jcd]}_{rno}R_tickets.csv",
                mime="text/csv"
            )

            st.divider()
            st.markdown("### ✅ レース結果を検証保存")

            if result_exists(ctx):
                st.success("このレースは検証履歴に保存済みです。再保存すると上書きします。")

            # 半自動検証 Phase 2：
            # 公式結果取得 → 固定予想で払戻計算 → 検証履歴保存 → オッズ追跡停止
            # を1クリックで実行する。固定予想がないレースは保存しない。
            if st.button(
                "🏁 結果取得＋検証保存",
                key=f"fetch_and_save_result_{ctx}",
                type="primary",
            ):
                snapshot_for_auto = load_prediction_snapshot(ctx)

                if snapshot_for_auto is None:
                    st.error(
                        "⚠️ このレースは検証用予想が固定されていません。"
                        "レース終了後の再予想が混ざるのを防ぐため、自動保存は行いません。"
                    )
                else:
                    try:
                        official_result = fetch_race_result(
                            d.strftime("%Y%m%d"),
                            jcd,
                            rno,
                        )

                        combo = official_result["trifecta"]
                        # 固定予想は1回だけ読み込み、払戻計算と結果保存で
                        # 同じものを使う。別々に読み込むと、片方だけ通信に
                        # 失敗したときに「払戻0円なのに的中扱い」のような
                        # 食い違った行が保存されてしまう。
                        received, hit_stake = snapshot_payout_from_official(
                            ctx,
                            combo,
                            official_result["trifecta_payout_per_100"],
                            snapshot=snapshot_for_auto,
                        )

                        # 画面の手動確認欄にも取得値を反映しておく。
                        st.session_state[f"actual1_{ctx}"] = int(official_result["first"])
                        st.session_state[f"actual2_{ctx}"] = int(official_result["second"])
                        st.session_state[f"actual3_{ctx}"] = int(official_result["third"])
                        st.session_state[f"payout_{ctx}"] = int(received)
                        st.session_state[f"official_result_{ctx}"] = {
                            **official_result,
                            "received": int(received),
                            "hit_stake": int(hit_stake),
                        }

                        rec = save_race_result(
                            race_key=ctx,
                            race_date=d.isoformat(),
                            venue=VENUES[jcd],
                            race_no=rno,
                            final=final,
                            tickets=tickets,
                            first_actual=int(official_result["first"]),
                            second_actual=int(official_result["second"]),
                            third_actual=int(official_result["third"]),
                            payout=int(received),
                            research_variants=st.session_state["result"].get("research_variants", {}),
                            prefer_snapshot=True,
                            snapshot=snapshot_for_auto,
                            require_snapshot=True,
                            collector_name=COLLECTOR_NAME,
                        )

                        # 保存まで成功した後だけ追跡を停止する。
                        deactivate_odds_watchlist(
                            d.strftime("%Y%m%d"),
                            jcd,
                            rno,
                        )

                        hit_text = "的中" if rec["hit_any_ticket"] else "不的中"
                        source_text = (
                            "固定予想で判定"
                            if rec.get("_used_snapshot")
                            else "現在予想で判定"
                        )
                        st.success(
                            f"自動保存しました：実結果 {rec['trifecta_actual']} / "
                            f"購入買い目 {hit_text} / 収支 {rec['profit']:+,}円 / "
                            f"{source_text}"
                        )

                    except Exception as e:
                        st.warning(
                            "公式結果の取得または検証保存を完了できませんでした。"
                            "結果確定後にもう一度押してください。"
                        )
                        st.caption(str(e))

            st.caption(
                "Phase 2：結果確定後は上のボタン1回で、公式結果取得・固定予想での判定・"
                "検証保存・オッズ追跡停止まで実行します。下の欄は確認／手動フォールバック用です。"
            )

            official_result_state = st.session_state.get(
                f"official_result_{ctx}"
            )
            if official_result_state:
                hit_text = (
                    f"的中購入額 {official_result_state['hit_stake']:,}円"
                    if official_result_state["hit_stake"] > 0
                    else "固定買い目は不的中"
                )
                st.success(
                    "🏁 公式結果取得済み："
                    f"{official_result_state['trifecta']} / "
                    f"3連単 {official_result_state['trifecta_payout_per_100']:,}円（100円あたり） / "
                    f"{hit_text} / "
                    f"実受取 {official_result_state['received']:,}円"
                )
                st.caption(
                    "着順と払戻受取額を下に自動入力しました。"
                    "内容を確認してから検証履歴へ保存してください。"
                )

            rc1, rc2, rc3 = st.columns(3)
            with rc1:
                actual_1 = st.selectbox("実1着", range(1,7), key=f"actual1_{ctx}")
            with rc2:
                actual_2 = st.selectbox("実2着", range(1,7), index=1, key=f"actual2_{ctx}")
            with rc3:
                actual_3 = st.selectbox("実3着", range(1,7), index=2, key=f"actual3_{ctx}")

            payout_input = st.number_input(
                "このレースの実払戻受取額（円）",
                min_value=0,
                max_value=10000000,
                value=0,
                step=100,
                key=f"payout_{ctx}",
                help="購入した買い目が外れなら0円。当たった場合は実際に受け取った合計払戻額を入力。",
            )

            if len({actual_1, actual_2, actual_3}) < 3:
                st.warning("1着・2着・3着は別々の艇を選んでください。")
            else:
                snapshot_for_result = load_prediction_snapshot(ctx)

                if snapshot_for_result is None:
                    st.error(
                        "⚠️ このレースは検証用予想が固定されていません。"
                        "レース終了後の再予想が混ざるのを防ぐため、結果保存は行いません。"
                    )
                elif st.button("💾 実結果を検証履歴へ保存", key=f"save_result_{ctx}"):
                    rec = save_race_result(
                        race_key=ctx,
                        race_date=d.isoformat(),
                        venue=VENUES[jcd],
                        race_no=rno,
                        final=final,
                        tickets=tickets,
                        first_actual=actual_1,
                        second_actual=actual_2,
                        third_actual=actual_3,
                        payout=int(payout_input),
                        research_variants=st.session_state["result"].get("research_variants", {}),
                        prefer_snapshot=True,
                        collector_name=COLLECTOR_NAME,
                    )
                    hit_text = "的中" if rec["hit_any_ticket"] else "不的中"
                    source_text = (
                        "固定予想で判定"
                        if rec.get("_used_snapshot")
                        else "現在予想で判定"
                    )
                    st.success(
                        f"保存しました：実結果 {rec['trifecta_actual']} / "
                        f"購入買い目 {hit_text} / 収支 {rec['profit']:+,}円 / "
                        f"{source_text}"
                    )

            st.warning("AI予想は確率推定であり、的中・利益を保証しません。オッズ変動、欠場・返還、展示と本番の進入差にも注意してください。")