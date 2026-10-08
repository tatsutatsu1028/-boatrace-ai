"""
Claude による「読み」（各艇の1着確率と短い理由）と、モデル＋Claude の買い目。

  - 「AI最終予想」を押したとき（管理者だけ）に、レースの入力データとモデルの確率を Claude に渡し、
    各艇の1着確率と理由を受け取る（read_race）
  - 1着確率をモデルと Claude の平均に置き換え、2着・3着はモデルの条件付き確率のまま
    3連単を組み立てた「モデル＋Claude」の final を作る（mix_final）。買い目の作り方と資金配分は
    モデルの買い目と同じ関数を通す（claude_tab.py）
  - 結果確定後に、モデルの買い目とモデル＋Claude の買い目のどちらが当たったかの列を作る
    （result_fields。結果の保存処理3か所から呼ぶ）

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
READ_MAX_TOKENS = 8000
PROMPT_VERSION = "claude-read-v1"

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
1レース分の入力データと、統計モデルが出した各艇の1着確率が渡されます。
入力データを読み、各艇の1着確率（6艇の合計が1）と、その艇の短い理由を返してください。

守ること:
- 理由には、渡されたデータに書かれていることだけを使う。データに無いこと（選手の評判・過去の対戦・
  記者コメント・ピットの様子・オッズ・モーターの整備内容など）は書かない。値が空欄の項目には触れない。
- 理由は1艇につき40字程度の日本語。数値を挙げるときは入力の値をそのまま使う。
- ボートレースは1号艇（1コース）の1着が全体の約半分を占める。展示や今節の数字が少し悪いだけで
  1号艇を低く見すぎる傾向があるので注意し、1号艇を下げるのは、はっきりした根拠がデータにあるときだけにする。
- モデルの確率は参考にしてよいが、そのまま写さず、データから見て妥当かどうかを自分で判断する。
- summary には、レース全体の見立てを80字程度で書く（同じくデータに無いことは書かない）。"""

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
        "summary": {"type": "string"},
    },
    "required": ["boats", "summary"],
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
        out.update({"status": "ok", "p_first": p, "reasons": reasons,
                    "summary": str(data.get("summary") or "").strip()})
        _CACHE[key] = out
        return out
    except Exception as e:  # noqa: BLE001  失敗しても予想の画面は止めない
        out["seconds"] = round(time.time() - t0, 1)
        out["error"] = f"{type(e).__name__}"
        return out


def mix_final(final, claude_p):
    """1着確率をモデルと Claude の平均にした final。2着・3着の条件付き確率はモデルのまま。"""
    mix = final.copy()
    lanes = pd.to_numeric(mix["lane"], errors="coerce").astype(int)
    model_p = pd.to_numeric(mix["p_first"], errors="coerce").fillna(0.0)
    claude = lanes.map({int(k): float(v) for k, v in claude_p.items()}).fillna(0.0)
    avg = (model_p + claude) / 2.0
    avg = avg / avg.sum() if avg.sum() > 0 else model_p
    mix["p_first"] = avg.to_numpy()
    mix["p_first_model"] = model_p.to_numpy()
    mix["p_first_claude"] = claude.to_numpy()
    # 2着・3着の周辺確率（表示用）も新しい1着確率で組み直す
    lane_list = lanes.tolist()
    p1 = dict(zip(lane_list, mix["p_first"]))
    p2 = dict.fromkeys(lane_list, 0.0)
    p3 = dict.fromkeys(lane_list, 0.0)
    for a in lane_list:
        col2 = f"p_second_given_{a}"
        if col2 not in mix.columns:
            continue
        cond2 = dict(zip(lane_list, pd.to_numeric(mix[col2], errors="coerce").fillna(0.0)))
        for b in lane_list:
            if b == a:
                continue
            p2[b] += p1[a] * cond2[b]
            col3 = f"p_third_given_{a}_{b}"
            if col3 in mix.columns:
                cond3 = dict(zip(lane_list, pd.to_numeric(mix[col3], errors="coerce").fillna(0.0)))
                for c in lane_list:
                    if c not in (a, b):
                        p3[c] += p1[a] * cond2[b] * cond3[c]
    if sum(p2.values()) > 0:
        mix["p_second"] = [p2[ln] for ln in lane_list]
    if sum(p3.values()) > 0:
        mix["p_third"] = [p3[ln] for ln in lane_list]
    return mix


def tickets_payload(tickets):
    keep = ["combo", "group", "prob", "odds", "expected_return", "stake"]
    rows = []
    for _, r in tickets.iterrows():
        rows.append({c: _clean(r[c]) for c in keep if c in tickets.columns})
    return rows


def snapshot_section(reading, model_final=None, mix_tickets=None, mix_hit_probability=None):
    """prediction_snapshots.payload_json の "claude" に入れる中身。"""
    sec = {k: reading.get(k) for k in ("status", "model", "served_model", "input_hash", "prompt_version",
                                        "summary", "usage", "cost", "seconds", "error")}
    if reading.get("status") == "ok":
        sec["p_first"] = {str(k): round(float(v), 6) for k, v in reading["p_first"].items()}
        sec["reasons"] = {str(k): v for k, v in reading["reasons"].items()}
        if model_final is not None:
            sec["model_p_first"] = {
                str(int(r.lane)): round(float(r.p_first), 6) for r in model_final.itertuples()}
        if mix_tickets is not None:
            sec["mix_tickets"] = tickets_payload(mix_tickets)
            sec["mix_hit_probability"] = mix_hit_probability
    return sec


def reading_from_section(sec):
    """保存済みの "claude" から read_race と同じ形に戻す（キャッシュの代わりに使う）。"""
    if not isinstance(sec, dict) or sec.get("status") != "ok":
        return None
    out = dict(sec)
    out["p_first"] = {int(k): float(v) for k, v in (sec.get("p_first") or {}).items()}
    out["reasons"] = {int(k): v for k, v in (sec.get("reasons") or {}).items()}
    return out


# ---------------------------------------------------------------
# 結果確定後: どちらの買い目が当たったか（prediction_results の列）
# ---------------------------------------------------------------
RESULT_COLUMNS = [
    "claude_model", "claude_p1_lane", "claude_first_hit", "mix_p1_lane", "mix_first_hit",
    "mix_candidate_count", "mix_candidate_hit", "mix_hit_any_ticket", "mix_total_stake", "mix_payout",
]


def result_fields(payload, actual_combo, payout_per_100):
    """
    保存済み予想に Claude の読みがあれば、prediction_results に足す列を返す（無ければ空の dict）。
    モデルの買い目の当たり外れは既存の列（candidate_hit・hit_any_ticket・payout）にある。
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
    if cp and mp:
        avg = {ln: (cp.get(ln, 0.0) + mp.get(ln, 0.0)) / 2 for ln in set(cp) | set(mp)}
        out["mix_p1_lane"] = max(avg, key=avg.get)
        out["mix_first_hit"] = (first == out["mix_p1_lane"]) if first else None
    tickets = sec.get("mix_tickets") or []
    if tickets:
        combos = [str(t.get("combo")) for t in tickets]
        stakes = [float(t.get("stake") or 0) for t in tickets]
        hit_stake = sum(s for c, s in zip(combos, stakes) if c == actual and s > 0)
        out["mix_candidate_count"] = len(combos)
        out["mix_candidate_hit"] = actual in combos
        out["mix_hit_any_ticket"] = hit_stake > 0
        out["mix_total_stake"] = int(sum(stakes))
        try:
            out["mix_payout"] = int(round(hit_stake * float(payout_per_100) / 100))
        except (TypeError, ValueError):
            out["mix_payout"] = None
    return {k: v for k, v in out.items() if v is not None}
