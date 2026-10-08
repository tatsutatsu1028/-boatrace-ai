"""
Claude API の使用額と残高の目安（管理者だけ）。

  - 実際に API を呼ぶたびに claude_usage テーブルへ1行（費用のドル・トークン数）を残す（log_call）
  - 「今日」「今月」「チャージした日以降」の合計は Supabase の claude_usage_summary() が計算し、
    数字だけを返す（行は取らない。転送量はごくわずか）
  - チャージした金額（ドル）と日は app_settings の claude_credit_usd・claude_credit_date に保存し、
    残高の目安 = チャージ額 − その日以降の使用額
正確な残高は Anthropic の Console（console.anthropic.com）でしか見られない。ここの値は、このアプリが
記録した呼び出しの費用（公開価格からの計算）の合計なので、ずれることがある。
"""

from __future__ import annotations

from datetime import date

import requests
import streamlit as st

from claude_reader import USD_JPY
from result_tracker import _headers, supabase_config

LOW_BALANCE_USD = 2.0
CONSOLE_NOTE = ("残高は、このアプリが記録した呼び出しの費用（公開価格からの計算）から出した目安です。"
                "正確な残高は Anthropic の Console（console.anthropic.com）の Billing でしか見られません。")


def log_call(reading, race_key, purpose="race_prediction"):
    """実際に API を呼んだ1回を記録する（費用が分かるときだけ。失敗しても予想は止めない）。"""
    cost, usage = reading.get("cost") or {}, reading.get("usage") or {}
    if cost.get("usd") is None:
        return False
    url, _ = supabase_config()
    if not url:
        return False
    row = {
        "race_key": str(race_key or ""), "purpose": purpose,
        "model": reading.get("served_model") or reading.get("model"),
        "usd": float(cost["usd"]), "status": reading.get("status"),
        **{k: int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens")},
    }
    try:
        r = requests.post(f"{url}/rest/v1/claude_usage", headers=_headers("return=minimal"), json=row, timeout=10)
        r.raise_for_status()
        summary.clear()
        return True
    except Exception as e:  # noqa: BLE001
        print("[CLAUDE_USAGE] log error:", type(e).__name__, str(e), flush=True)
        return False


@st.cache_data(ttl=60, show_spinner=False)
def summary(since=None):
    """今日・今月（日本時間）とチャージ日以降の使用額（ドル）と回数。1分だけ覚えておく。"""
    url, _ = supabase_config()
    if not url:
        return None
    r = requests.post(f"{url}/rest/v1/rpc/claude_usage_summary", headers=_headers(),
                      json={"p_since": since}, timeout=10)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=300, show_spinner=False)
def load_credit():
    """チャージした金額（ドル）と日。未設定なら (None, None)。"""
    url, _ = supabase_config()
    if not url:
        return None, None
    r = requests.get(f"{url}/rest/v1/app_settings",
                     params={"select": "claude_credit_usd,claude_credit_date", "id": "eq.1"},
                     headers=_headers(), timeout=10)
    r.raise_for_status()
    rows = r.json() or []
    if not rows:
        return None, None
    usd, d = rows[0].get("claude_credit_usd"), rows[0].get("claude_credit_date")
    return (float(usd) if usd is not None else None), (str(d) if d else None)


def save_credit(usd, credit_date):
    url, _ = supabase_config()
    r = requests.patch(f"{url}/rest/v1/app_settings", params={"id": "eq.1"},
                       headers=_headers("return=minimal"),
                       json={"claude_credit_usd": float(usd), "claude_credit_date": credit_date.isoformat()},
                       timeout=10)
    r.raise_for_status()
    load_credit.clear()
    summary.clear()


def _money(usd):
    return f"{usd * USD_JPY:,.0f}円（${usd:,.2f}）"


def render_header():
    """「＋Claude予想」タブの上部: 今日・今月の使用額、残高の目安、少ないときの注意。"""
    try:
        credit_usd, credit_date = load_credit()
        s = summary(credit_date) or {}
    except Exception as e:  # noqa: BLE001
        st.caption(f"Claude の使用額を読み込めませんでした（{type(e).__name__}）")
        return
    today, month = float(s.get("today_usd") or 0), float(s.get("month_usd") or 0)
    c1, c2, c3 = st.columns(3)
    c1.metric("今日の使用額", _money(today), f"{int(s.get('today_calls') or 0)}回", delta_color="off")
    c2.metric("今月の使用額", _money(month), f"{int(s.get('month_calls') or 0)}回", delta_color="off")
    if credit_usd is not None and credit_date:
        balance = credit_usd - float(s.get("since_usd") or 0)
        c3.metric("残高の目安", f"${balance:,.2f}", f"{credit_date} のチャージ ${credit_usd:,.2f} から", delta_color="off")
        if balance < LOW_BALANCE_USD:
            st.warning(f"⚠️ Claude の残高の目安が ${balance:,.2f} です（${LOW_BALANCE_USD:.0f}を下回りました）。"
                       "Console でチャージしてください。チャージしたら設定タブの金額と日も更新してください。")
    else:
        c3.metric("残高の目安", "－", "設定タブでチャージ額を入力", delta_color="off")
    st.caption(f"円は1ドル={USD_JPY:.0f}円で換算。{CONSOLE_NOTE}")


def render_settings():
    """設定タブ: チャージした金額（ドル）と日の入力・保存（管理者だけ）。"""
    st.subheader("🧠 Claude API のチャージ")
    try:
        credit_usd, credit_date = load_credit()
    except Exception as e:  # noqa: BLE001
        st.caption(f"保存済みの値を読み込めませんでした（{type(e).__name__}）")
        credit_usd, credit_date = None, None
    c1, c2 = st.columns(2)
    usd = c1.number_input("チャージした金額（ドル）", min_value=0.0, max_value=10000.0,
                          value=float(credit_usd or 0.0), step=5.0, format="%.2f", key="claude_credit_usd")
    d = c2.date_input("チャージした日", value=date.fromisoformat(credit_date) if credit_date else date.today(),
                      key="claude_credit_date")
    if st.button("💾 チャージ額を保存", key="claude_credit_save"):
        try:
            save_credit(usd, d)
            st.success("保存しました。残高の目安は「チャージ額 − その日以降の使用額」で「＋Claude予想」タブに出ます。")
        except Exception as e:  # noqa: BLE001
            st.error(f"保存できませんでした（{type(e).__name__}）")
    st.caption("残高が残っている状態でチャージしたときは、Console に出ている残高の合計を入れ、日付をその日にしてください。"
               + CONSOLE_NOTE)
