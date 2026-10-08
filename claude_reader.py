"""
Claude による予想（各艇の1着確率・3連単の買い目と確率・理由・展開のまとめ）と、3つの予想。

  - 管理者が「Claudeのみ予想」か「両方の予想平均」を押したとき、レースの入力データとモデルの確率を
    Claude に渡し、1〜3着の展開を読んだ3連単の買い目（本線・抑え、10点前後）とその確率・理由を受け取る（read_race）
  - 3つの予想（claude_tab.py が買い目と資金配分まで作る）:
      モデルのみ   … 今までどおり
      Claudeのみ   … Claude が選んだ買い目そのまま（資金配分はアプリの仕組み）
      両方の予想平均 … モデルと Claude の3連単の確率を組み合わせごとに平均し、そこから買い目を選び直す
        Claude が挙げなかった組み合わせには、Claude の確率の残り（1 − 挙げた分の合計）を
        モデルの確率の比率で配る（claude_full_tri）。Claude の分も120通りで合計1になり、
        モデルが強く推す組み合わせが平均で消えすぎない
  - 結果確定後に、3つの予想それぞれの当たり外れと払戻の列を作る（result_fields。結果の保存処理3か所から呼ぶ）

今のモデル（prediction.predict）でも新しいモデル（ml_model）でも、final の形
（p_first・p_second_given_<a>・p_third_given_<a>_<b>）は同じなので、そのまま使える。

API の呼び出しは、同じレース・同じ入力・同じモデル名なら1回だけ（プロセス内のキャッシュと、
保存済み予想の中身で判定）。失敗・時間切れのときは status="error" を返すだけで例外は出さない。
"""

from __future__ import annotations

import hashlib
import json
import math
import time

import numpy as np
import pandas as pd

# 予想の読みに使うモデル（Streamlit の Secrets の CLAUDE_READ_MODEL で変えられる）
DEFAULT_READ_MODEL = "claude-opus-5-5"
READ_EFFORT = "medium"
READ_TIMEOUT_SEC = 60.0
READ_MAX_TOKENS = 12000
PROMPT_VERSION = "claude-read-v2"

# 1回あたりの費用の目安（米ドル / 100万トークン）。2026-10 時点の公開価格。
PRICES_PER_MTOK = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.00, 50.00),
}
USD_JPY = 150.0
# 拒否（安全上の判断）のときに、別のモデルで自動でやり直してもらう（server-side fallback）
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}

# Claude に渡す艇ごとの項目（列名 → 表示名）。無い列は渡さない。
LANE_FIELDS = [
    ("racer_name", "選手"),
    ("racer_class", "級別"),
    ("racer_win_rate", "全国勝率"),
    ("local_win_rate", "当地勝率"),
    ("motor_2ren", "モーター2連率"),
    ("boat_2ren", "ボート2連率"),
    ("avg_st", "平均ST"),
    ("f_count", "F数"),
    ("l_count", "L数"),
    ("weight", "体重kg"),
    ("exhibition_time", "展示タイム"),
    ("exhibition_st", "展示ST"),
    ("tilt", "チルト"),
    ("parts_exchanged", "部品交換あり"),
    ("current_meet_races", "今節出走数"),
    ("current_meet_avg_finish", "今節平均着順"),
    ("current_meet_avg_finish_adjusted", "今節コース補正着順(負ほど良い)"),
    ("current_meet_top2_rate", "今節2連対率%"),
    ("current_meet_avg_st", "今節平均ST"),
    ("course_top3_rate", "この艇番コースでの3連対率%"),
    ("course_avg_st", "この艇番コースでの平均ST"),
]
RACE_FIELDS = [
    ("wind_speed", "風速m"),
    ("wind_direction", "風向"),
    ("wave_height", "波高cm"),
    ("temperature", "気温"),
    ("water_temperature", "水温"),
    ("weather", "天候"),
]

SYSTEM_PROMPT = """あなたはボートレースの予想を検討するアナリストです。
1レース分の入力データと、統計モデルが出した各艇の1着・2着・3着の確率が渡されます。
入力データを読んでスタートから1マークまでの展開を考え、1着〜3着を予想してください。

返すもの:
- boats: 各艇の1着確率（6艇の合計が1）と、その艇の短い理由（40字程度）
- tickets: 3連単の買い目を10点前後。group は本線（最も自信のある4〜5点）か抑え。
  prob はその組み合わせ（1着-2着-3着の順）になる確率。挙げた買い目の prob の合計は1より小さくてよい
  （挙げなかった組み合わせにも確率は残る）。reason は30字程度
- summary: 1〜3着の展開のまとめ（80〜120字）

守ること:
- 理由とまとめには、渡されたデータに書かれていることだけを使う。データに無いこと（選手の評判・過去の対戦・
  記者コメント・ピットの様子・オッズ・モーターの整備内容など）は書かない。値が空欄の項目には触れない。
- 数値を挙げるときは入力の値をそのまま使う。
- ボートレースは1号艇（1コース）の1着が全体の約半分を占める。展示や今節の数字が少し悪いだけで
  1号艇を低く見すぎる傾向があるので注意し、1号艇を下げるのは、はっきりした根拠がデータにあるときだけにする。
- モデルの確率は参考にしてよいが、そのまま写さず、データから見て妥当かどうかを自分で判断する。
- 買い目は 1-2-3 の形で、出走している艇番だけを使い、同じ艇を2回使わない。同じ買い目を重ねない。"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "boats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "lane": {"type": "integer"},
                    "p_first": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["lane", "p_first", "reason"],
                "additionalProperties": False,
            },
        },
        "tickets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "combo": {"type": "string"},
                    "group": {"type": "string", "enum": ["本線", "抑え"]},
                    "prob": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["combo", "group", "prob", "reason"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["boats", "tickets", "summary"],
    "additionalProperties": False,
}

_CACHE = {}  # (race_key, model, input_hash) -> 結果（プロセス内。Streamlit の再実行で呼び直さない）


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        v = float(v)
        return round(v, 4) if math.isfinite(v) else None
    if isinstance(v, (np.bool_,)):
        return bool(v)
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s or None


def build_brief(race, final, race_label=""):
    """Claude に渡すレースのデータ（JSON にできる dict）。モデルの確率も含める。"""
    race = race.copy()
    race["lane"] = pd.to_numeric(race["lane"], errors="coerce")
    fin = final.copy()
    fin["lane"] = pd.to_numeric(fin["lane"], errors="coerce")
    probs = fin.set_index("lane")
    boats = []
    for _, row in race.sort_values("lane").iterrows():
        lane = int(row["lane"])
        item = {"艇番": lane}
        for col, label in LANE_FIELDS:
            if col in race.columns:
                item[label] = _clean(row.get(col))
        if lane in probs.index:
            for col, label in (("p_first", "モデル1着確率"), ("p_second", "モデル2着確率"),
                               ("p_third", "モデル3着確率")):
                if col in probs.columns:
                    v = _clean(pd.to_numeric(probs.loc[lane, col], errors="coerce"))
                    item[label] = round(v, 3) if isinstance(v, float) else v
        boats.append(item)
    race_info = {"レース": race_label}
    for col, label in RACE_FIELDS:
        if col in race.columns:
            race_info[label] = _clean(race[col].iloc[0])
    model_version = str(final["model_version"].iloc[0]) if "model_version" in final.columns else ""
    return {"race": race_info, "boats": boats, "model_version": model_version}


def input_hash(brief, model):
    text = json.dumps({"brief": brief, "model": model, "prompt": PROMPT_VERSION},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def estimate_cost(model, usage):
    """usage（入力・出力トークン）から1回の費用（ドル・円）を出す。価格表に無いモデルは None。"""
    price = PRICES_PER_MTOK.get(model)
    if not price or not usage:
        return None
    inp = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
    cached = usage.get("cache_read_input_tokens") or 0
    out = usage.get("output_tokens") or 0
    usd = inp * price[0] / 1e6 + cached * price[0] * 0.1 / 1e6 + out * price[1] / 1e6
    return {"usd": round(usd, 5), "jpy": round(usd * USD_JPY, 2)}


def _normalize(boats, lanes):
    """Claude の確率を艇番ごとに取り出し、合計1にする。足りない艇・負の値があれば None。"""
    p, reasons = {}, {}
    for b in boats:
        try:
            lane = int(b.get("lane"))
            prob = float(b.get("p_first"))
        except (TypeError, ValueError):
            continue
        if lane in lanes and math.isfinite(prob) and prob >= 0:
            p[lane] = prob
            reasons[lane] = str(b.get("reason") or "").strip()
    if set(p) != set(lanes):
        return None, None
    total = sum(p.values())
    if total <= 0:
        return None, None
    return {ln: v / total for ln, v in p.items()}, reasons


def _clean_tickets(rows, lanes):
    """Claude の買い目を確かめる（形・艇番・重なり）。確率の合計が1を超えたら1に縮める。"""
    out, seen = [], set()
    for r in rows:
        parts = str(r.get("combo") or "").replace("－", "-").replace("ー", "-").strip().split("-")
        try:
            a, b, c = (int(x) for x in parts)
            prob = float(r.get("prob"))
        except (TypeError, ValueError):
            continue
        combo = f"{a}-{b}-{c}"
        if len({a, b, c}) < 3 or not {a, b, c} <= set(lanes) or combo in seen or not math.isfinite(prob):
            continue
        seen.add(combo)
        out.append({"combo": combo, "group": "本線" if r.get("group") == "本線" else "抑え",
                    "prob": max(prob, 0.0), "reason": str(r.get("reason") or "").strip()})
    total = sum(t["prob"] for t in out)
    if total > 1.0:
        for t in out:
            t["prob"] /= total
    return out


def read_race(api_key, race_key, race, final, model=None, race_label="", reuse=None):
    """
    Claude の読みを返す（例外は出さない）。
      {"status": "ok"|"error", "model", "p_first": {lane: p}, "reasons": {lane: str}, "summary",
       "usage", "cost", "seconds", "input_hash", "error"}
    reuse: 保存済み予想にある前回の読み。同じ入力・同じモデルならそれを返し、API は呼ばない。
    """
    model = (model or DEFAULT_READ_MODEL).strip()
    brief = build_brief(race, final, race_label)
    h = input_hash(brief, model)
    key = (str(race_key), model, h)
    if key in _CACHE:
        return _CACHE[key]
    if reuse and reuse.get("status") == "ok" and reuse.get("input_hash") == h:
        _CACHE[key] = reuse
        return reuse
    out = {"status": "error", "model": model, "input_hash": h, "prompt_version": PROMPT_VERSION}
    if not api_key:
        out["error"] = "ANTHROPIC_API_KEY が未設定です"
        return out

    lanes = [int(v["艇番"]) for v in brief["boats"]]
    t0 = time.time()
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key, timeout=READ_TIMEOUT_SEC, max_retries=1)
        params = dict(
            model=model,
            max_tokens=READ_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            output_config={"effort": READ_EFFORT,
                           "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": json.dumps(brief, ensure_ascii=False)}],
        )
        if model in FALLBACK_MODELS:
            resp = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        else:
            resp = client.messages.create(**params)
        out["seconds"] = round(time.time() - t0, 1)
        out["served_model"] = getattr(resp, "model", model)
        usage = getattr(resp, "usage", None)
        out["usage"] = {k: getattr(usage, k, None) or 0 for k in
                        ("input_tokens", "output_tokens", "cache_read_input_tokens",
                         "cache_creation_input_tokens")} if usage else {}
        out["cost"] = estimate_cost(model, out["usage"])
        if resp.stop_reason == "refusal":
            out["error"] = "Claude が回答を控えました"
            return out
        if resp.stop_reason == "max_tokens":
            out["error"] = "回答が途中で切れました"
            return out
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        data = json.loads(text)
        p, reasons = _normalize(data.get("boats") or [], lanes)
        if p is None:
            out["error"] = "6艇分の確率がそろっていませんでした"
            return out
        tickets = _clean_tickets(data.get("tickets") or [], lanes)
        if not tickets:
            out["error"] = "買い目を読み取れませんでした"
            return out
        out.update({"status": "ok", "p_first": p, "reasons": reasons, "tickets": tickets,
                    "summary": str(data.get("summary") or "").strip()})
        _CACHE[key] = out
        return out
    except Exception as e:  # noqa: BLE001  失敗しても予想の画面は止めない
        out["seconds"] = round(time.time() - t0, 1)
        out["error"] = f"{type(e).__name__}"
        return out


def _tri_dict(tri):
    t = tri[["combo", "prob"]].copy()
    t["prob"] = pd.to_numeric(t["prob"], errors="coerce").fillna(0.0).clip(lower=0.0)
    total = t["prob"].sum()
    return {str(c): (p / total if total > 0 else 0.0) for c, p in zip(t["combo"], t["prob"])}


def claude_full_tri(claude_tickets, model_tri):
    """
    Claude の買い目（10点前後）を120通りに広げる。挙げた組み合わせはその確率、
    挙げなかった組み合わせには残り（1 − 挙げた分の合計）をモデルの確率の比率で配る。
    """
    m = _tri_dict(model_tri)
    listed = {t["combo"]: float(t["prob"]) for t in claude_tickets if t["combo"] in m}
    rest = max(1.0 - sum(listed.values()), 0.0)
    others = {c: p for c, p in m.items() if c not in listed}
    z = sum(others.values())
    full = dict(listed)
    for c, p in others.items():
        full[c] = rest * (p / z if z > 0 else 1.0 / max(len(others), 1))
    total = sum(full.values())
    return {c: v / total for c, v in full.items()} if total > 0 else m


def average_tri(model_tri, claude_tickets):
    """モデルと Claude の3連単の確率を組み合わせごとに平均した表（combo, prob。合計1）。"""
    m = _tri_dict(model_tri)
    c = claude_full_tri(claude_tickets, model_tri)
    rows = [(k, (m.get(k, 0.0) + c.get(k, 0.0)) / 2.0) for k in m]
    out = pd.DataFrame(rows, columns=["combo", "prob"])
    out["prob"] = out["prob"] / out["prob"].sum()
    return out


def final_from_tri(final, tri):
    """3連単の表から、それと矛盾しない final（1着・条件付き2着・条件付き3着）を作る。買い目の点数決めに使う。"""
    t = tri.copy()
    parts = t["combo"].str.split("-", expand=True).astype(int)
    t["a"], t["b"], t["c"] = parts[0], parts[1], parts[2]
    out = final.copy()
    lanes = pd.to_numeric(out["lane"], errors="coerce").astype(int).tolist()
    p1 = t.groupby("a")["prob"].sum()
    p_ab = t.groupby(["a", "b"])["prob"].sum()
    out["p_first"] = [float(p1.get(ln, 0.0)) for ln in lanes]
    p2m = dict.fromkeys(lanes, 0.0)
    p3m = dict.fromkeys(lanes, 0.0)
    for a in lanes:
        pa = float(p1.get(a, 0.0))
        out[f"p_second_given_{a}"] = [
            0.0 if b == a or pa <= 0 else float(p_ab.get((a, b), 0.0)) / pa for b in lanes]
        for b in lanes:
            if b == a:
                continue
            pab = float(p_ab.get((a, b), 0.0))
            p2m[b] += pab
            sub = t[(t["a"] == a) & (t["b"] == b)].set_index("c")["prob"]
            out[f"p_third_given_{a}_{b}"] = [
                0.0 if c in (a, b) or pab <= 0 else float(sub.get(c, 0.0)) / pab for c in lanes]
            for c in lanes:
                p3m[c] += float(sub.get(c, 0.0))
    out["p_second"] = [p2m[ln] for ln in lanes]
    out["p_third"] = [p3m[ln] for ln in lanes]
    return out


def tickets_payload(tickets):
    keep = ["combo", "group", "prob", "odds", "expected_return", "stake"]
    rows = []
    for _, r in tickets.iterrows():
        rows.append({c: _clean(r[c]) for c in keep if c in tickets.columns})
    return rows


def snapshot_section(reading, model_final=None, predictions=None):
    """
    prediction_snapshots.payload_json の "claude" に入れる中身。
    predictions: {"claude": (tickets, hit_probability), "avg": (tickets, hit_probability)}
    （モデルの買い目は今までどおり payload の "tickets"）。
    """
    sec = {k: reading.get(k) for k in ("status", "model", "served_model", "input_hash", "prompt_version",
                                        "summary", "usage", "cost", "seconds", "error")}
    if reading.get("status") == "ok":
        sec["p_first"] = {str(k): round(float(v), 6) for k, v in reading["p_first"].items()}
        sec["reasons"] = {str(k): v for k, v in reading["reasons"].items()}
        sec["claude_raw_tickets"] = reading.get("tickets") or []
        if model_final is not None:
            sec["model_p_first"] = {
                str(int(r.lane)): round(float(r.p_first), 6) for r in model_final.itertuples()}
        for name, key in (("claude", "claude_tickets"), ("avg", "avg_tickets")):
            if predictions and predictions.get(name) is not None:
                tickets, hit_prob = predictions[name]
                sec[key] = tickets_payload(tickets)
                sec[f"{name}_hit_probability"] = hit_prob
    return sec


def reading_from_section(sec):
    """保存済みの "claude" から read_race と同じ形に戻す（キャッシュの代わりに使う）。"""
    if not isinstance(sec, dict) or sec.get("status") != "ok":
        return None
    out = dict(sec)
    out["p_first"] = {int(k): float(v) for k, v in (sec.get("p_first") or {}).items()}
    out["reasons"] = {int(k): v for k, v in (sec.get("reasons") or {}).items()}
    out["tickets"] = sec.get("claude_raw_tickets") or []
    if not out["tickets"]:  # 1着確率だけの古い読み（v1）は使い回さない
        return None
    return out


# ---------------------------------------------------------------
# 結果確定後: どちらの買い目が当たったか（prediction_results の列）
# ---------------------------------------------------------------
RESULT_COLUMNS = [
    "claude_model", "claude_p1_lane", "claude_first_hit",
    "claude_candidate_count", "claude_candidate_hit", "claude_hit_any_ticket", "claude_total_stake",
    "claude_payout",
    "mix_p1_lane", "mix_first_hit",
    "mix_candidate_count", "mix_candidate_hit", "mix_hit_any_ticket", "mix_total_stake", "mix_payout",
]


def _ticket_hits(tickets, actual, payout_per_100, prefix):
    combos = [str(t.get("combo")) for t in tickets]
    stakes = [float(t.get("stake") or 0) for t in tickets]
    hit_stake = sum(s for c, s in zip(combos, stakes) if c == actual and s > 0)
    out = {
        f"{prefix}_candidate_count": len(combos),
        f"{prefix}_candidate_hit": actual in combos,
        f"{prefix}_hit_any_ticket": hit_stake > 0,
        f"{prefix}_total_stake": int(sum(stakes)),
    }
    try:
        out[f"{prefix}_payout"] = int(round(hit_stake * float(payout_per_100) / 100))
    except (TypeError, ValueError):
        pass
    return out


def result_fields(payload, actual_combo, payout_per_100):
    """
    保存済み予想に Claude の予想があれば、prediction_results に足す列を返す（無ければ空の dict）。
      claude_* … Claudeのみ予想 / mix_* … 両方の予想平均
    モデルのみ予想の当たり外れは既存の列（candidate_hit・hit_any_ticket・payout）にある。
    """
    sec = payload.get("claude") if isinstance(payload, dict) else None
    if not isinstance(sec, dict) or sec.get("status") != "ok":
        return {}
    actual = str(actual_combo or "").strip()
    try:
        first = int(actual.split("-")[0])
    except (ValueError, IndexError):
        first = None
    cp = {int(k): float(v) for k, v in (sec.get("p_first") or {}).items()}
    mp = {int(k): float(v) for k, v in (sec.get("model_p_first") or {}).items()}
    out = {"claude_model": sec.get("served_model") or sec.get("model")}
    if cp:
        out["claude_p1_lane"] = max(cp, key=cp.get)
        out["claude_first_hit"] = (first == out["claude_p1_lane"]) if first else None
    ap = {int(k): float(v) for k, v in (sec.get("avg_p_first") or {}).items()}
    if not ap and cp and mp:  # 1着確率の平均だけを持つ古い保存形
        ap = {ln: (cp.get(ln, 0.0) + mp.get(ln, 0.0)) / 2 for ln in set(cp) | set(mp)}
    if ap:
        avg = ap
        out["mix_p1_lane"] = max(avg, key=avg.get)
        out["mix_first_hit"] = (first == out["mix_p1_lane"]) if first else None
    if sec.get("claude_tickets"):
        out.update(_ticket_hits(sec["claude_tickets"], actual, payout_per_100, "claude"))
    avg_tickets = sec.get("avg_tickets") or sec.get("mix_tickets")  # mix_tickets は v1 の保存形
    if avg_tickets:
        out.update(_ticket_hits(avg_tickets, actual, payout_per_100, "mix"))
    return {k: v for k, v in out.items() if v is not None}
