"""
万舟狙いの買い目の検証 D「Claude に選ばせる」（検証用。本番には使わない）。

万舟チャンスの印のレース（展示後版の毎日上位10レース・テスト期間）から無作為に --sample レースを選び、
レースの入力データ・新モデルの確率・オッズ100倍以上の組（モデルの確率とオッズ）を Claude に渡して、
「どう荒れるか」の展開を読んだうえで、候補の中から買う順に最大15点を選んでもらう。
結果は --out（JSON Lines。1レース1行）に追記し、途中で止まっても続きから行う。評価は
tools/manshu_eval.py odds（Claude の上位5・10・15点を A・B・C と同じ条件で比べる）。

- オッズは最終オッズ（data/odds3t。Odds Backfill が取ったもの）。実際に買うときのオッズとは少し違う
- 日付は渡さない（結果を知っている可能性を減らすため）。場名・R・入力データだけ
- 費用の上限 --max-usd を超えたら止める。ログには件数・費用・時間だけを出す（買い目・結果は出さない）

  BOATRACE_DATA_DIR=<データ用リポジトリ> ANTHROPIC_API_KEY=... python tools/manshu_claude_aim.py \
      --out <データ用リポジトリ>/reports/manshu/claude_aim.jsonl --sample 100
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_paths import data_path  # noqa: E402

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TICKETS = 15
ODDS_MIN = 100.0
PROMPT_VERSION = "manshu-aim-v1"
CLASS_NAME = {4: "A1", 3: "A2", 2: "B1", 1: "B2"}
WEATHER = {1: "晴", 2: "曇", 3: "雨", 4: "雪", 5: "台風", 6: "霧"}

SYSTEM_PROMPT = """あなたはボートレースの予想を検討するアナリストです。
このレースは統計モデルが「3連単が万舟（100円あたり1万円以上）になりやすい」と判定したレースです。
1レース分の入力データ、統計モデルが出した各艇の1着・2着・3着の確率、オッズ100倍以上の3連単の組み合わせの一覧
（各組のモデルの確率とオッズ）が渡されます。

やること:
1. 入力データを読み、このレースが荒れるとしたら、どの艇がどう動いて（スタート・1マークの攻め方・差し/まくり・
   展示の気配など）どんな決着になりそうかを考える（scenario。120〜200字）
2. その展開に沿って、候補の一覧の中から買う組み合わせを、買いたい順に最大15点選ぶ（tickets）。
   1番目が最も買いたい組。各組に短い理由（25字程度）を付ける

守ること:
- 選べるのは候補の一覧にある組み合わせだけ（1着-2着-3着の形のまま）。同じ組を重ねない
- 理由と展開には、渡されたデータに書かれていることだけを使う。データに無いこと（選手の評判・過去の対戦・
  記者コメント・ピットの様子など）は書かない。値が空欄の項目には触れない
- モデルの確率は参考にしてよいが、そのまま確率の高い順に並べるのではなく、展開の読みで選ぶ。
  オッズ（＝当たったときの払戻）と当たりやすさの釣り合いも考える
- ボートレースは1号艇（1コース）の1着が全体の約半分を占める。荒れるレースでも、1号艇が2着・3着に残る決着は多い"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "scenario": {"type": "string"},
        "tickets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"combo": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["combo", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["scenario", "tickets"],
    "additionalProperties": False,
}


def log(*a):
    print("[CLAUDE_AIM]", *a, flush=True)


def _num(v, nd=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, nd)


def sample_races(report_dir, odds_dir, n, seed):
    """展示後版の毎日上位10レース（テスト期間）のうち、オッズの取れたレースから無作為に n レース。"""
    from manshu_eval import picks

    rl = pd.read_csv(Path(report_dir) / "race_level_post.csv.gz", dtype={"race_date": str})
    marked = picks(rl, "p_full", "day10")
    f = pd.read_csv(Path(odds_dir) / "fetched.csv", dtype={"race_key": str})
    ok = set(f.loc[f["n"] >= 100, "race_key"])
    keys = sorted(set(marked["race_key"]) & ok)
    rng = np.random.default_rng(seed)
    return sorted(rng.choice(keys, size=min(n, len(keys)), replace=False).tolist())


def build_inputs(keys, model_path):
    """対象レースの特徴量（展示後）と、新モデル（本番と同じ学習期間）の確率・3連単確率。"""
    import ml_features as mf
    import ml_model as mm
    from ml_pre_exhibition import build_tables

    post, _ = build_tables(*mf.load_sources())
    t = post[post["race_key"].isin(set(keys))].copy()
    model = mm.ChainModel.load(model_path)
    first, second, third = model.predict_tables(t)
    tri = mm.trifecta_table(first, second, third)
    p2 = second.merge(first.rename(columns={"lane": "w_lane"}), on=["race_key", "w_lane"])
    p2 = (p2["p"] * p2["p_first"]).groupby([p2["race_key"], p2["lane"]]).sum()
    return t, first, p2, tri


def brief_for(rk, t, first, p2, tri, odds, venue):
    f = t[t["race_key"] == rk].sort_values("lane")
    p1 = first[first["race_key"] == rk].set_index("lane")["p_first"]
    tr = tri[tri["race_key"] == rk].set_index("combo")["prob"]
    p3 = {ln: float(tr[[c for c in tr.index if c[4] == str(int(ln))]].sum()) for ln in f["lane"]}
    boats = []
    for _, r in f.iterrows():
        ln = int(r["lane"])
        boats.append({
            "艇番": ln, "級別": CLASS_NAME.get(int(r["class_ord"])) if r["class_ord"] == r["class_ord"] else None,
            "全国勝率": _num(r.get("national_win_rate")), "当地勝率": _num(r.get("local_win_rate")),
            # 0 は記録の無い新しいモーター・ボート（空欄として渡す）
            "モーター2連率": _num(r.get("motor_2ren"), 1) or None, "ボート2連率": _num(r.get("boat_2ren"), 1) or None,
            "平均ST": _num(r.get("avg_st")), "F数": _num(r.get("f_count"), 0),
            "展示タイム": _num(r.get("exhibition_time")), "展示ST": _num(r.get("exhibition_st")),
            "今節2連対率": _num(r.get("meet_top2"), 3), "この艇番コースでの1着率": _num(r.get("rc_win"), 3),
            "この艇番コースでの3連対率": _num(r.get("rc_top3"), 3),
            "モデル1着確率": _num(p1.get(ln), 3), "モデル2着確率": _num(p2.get((rk, ln)), 3),
            "モデル3着確率": _num(p3.get(ln), 3),
        })
    head = f.iloc[0]
    race = {"場": venue, "R": int(rk.split("_")[2]), "距離m": _num(head.get("distance_m"), 0),
            "風速m": _num(head.get("wind_speed"), 0), "波高cm": _num(head.get("wave_cm"), 0),
            "天候": WEATHER.get(int(head["weather_code"])) if head.get("weather_code") == head.get("weather_code")
            and head.get("weather_code") is not None else None}
    o = odds[odds["race_key"] == rk]
    o = o[o["odds"] >= ODDS_MIN].assign(prob=lambda d: d["combo"].map(tr))
    o = o.sort_values("prob", ascending=False)
    cands = [{"組": c, "モデル確率%": round(float(p) * 100, 2), "オッズ": float(od)}
             for c, p, od in zip(o["combo"], o["prob"], o["odds"])]
    return {"race": race, "boats": boats, "候補（オッズ100倍以上）": cands}


def ask(client, brief):
    params = dict(
        model=MODEL, max_tokens=16000, system=SYSTEM_PROMPT,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        messages=[{"role": "user", "content": json.dumps(brief, ensure_ascii=False)}],
    )
    return client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)


def main():
    import anthropic

    import claude_reader

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--report-dir", default=str(data_path("reports/manshu")))
    ap.add_argument("--odds-dir", default=str(data_path("data/odds3t")))
    ap.add_argument("--model-path", default=str(data_path("reports/ml_v2/models/ml-chain-v2-lgb-2y.joblib")))
    ap.add_argument("--sample", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-usd", type=float, default=20.0)
    args = ap.parse_args()

    out = Path(args.out)
    done = set()
    spent = 0.0
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            spent += float((d.get("cost") or {}).get("usd") or 0)
            if d.get("status") == "ok":
                done.add(d["race_key"])
    keys = sample_races(args.report_dir, args.odds_dir, args.sample, args.seed)
    todo = [k for k in keys if k not in done]
    log(f"対象 {len(keys)}レース・済み {len(keys) - len(todo)}・残り {len(todo)}・これまでの費用 ${spent:.2f}")
    if not todo:
        return
    t0 = time.time()
    t, first, p2, tri = build_inputs(todo, args.model_path)
    from manshu_eval import load_odds, race_info

    odds = load_odds(args.odds_dir)
    venues = race_info(min(todo)[:8], max(todo)[:8]).set_index("race_key")["venue"]
    log(f"入力の準備 {time.time() - t0:.0f}秒")
    client = anthropic.Anthropic(max_retries=2, timeout=300.0)
    ok = ng = 0
    for rk in todo:
        if spent >= args.max_usd:
            log(f"費用の上限 ${args.max_usd} に達したので止める")
            break
        brief = brief_for(rk, t, first, p2, tri, odds, str(venues.get(rk, "")))
        cands = {c["組"] for c in brief["候補（オッズ100倍以上）"]}
        rec = {"race_key": rk, "version": "post", "model": MODEL, "effort": EFFORT, "prompt": PROMPT_VERSION,
               "status": "error"}
        try:
            resp = ask(client, brief)
            u = resp.usage
            rec["usage"] = {k: getattr(u, k, None) or 0 for k in
                            ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
            rec["cost"] = claude_reader.estimate_cost(MODEL, rec["usage"])
            rec["served_model"] = getattr(resp, "model", MODEL)
            if resp.stop_reason in ("refusal", "max_tokens"):
                rec["error"] = resp.stop_reason
            else:
                data = json.loads(next(b.text for b in resp.content if getattr(b, "type", "") == "text"))
                picked = []
                for x in data.get("tickets") or []:
                    c = str(x.get("combo") or "").strip()
                    if c in cands and c not in picked:
                        picked.append(c)
                rec.update(status="ok" if picked else "error", tickets=picked[:MAX_TICKETS],
                           reasons=[str(x.get("reason") or "") for x in (data.get("tickets") or [])][:MAX_TICKETS],
                           scenario=str(data.get("scenario") or ""), n_candidates=len(cands),
                           n_invalid=len(data.get("tickets") or []) - len(picked))
        except Exception as e:  # noqa: BLE001  1レースの失敗では止めない
            rec["error"] = type(e).__name__
        spent += float((rec.get("cost") or {}).get("usd") or 0)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        ok += rec["status"] == "ok"
        ng += rec["status"] != "ok"
        if (ok + ng) % 10 == 0:
            log(f"{ok + ng}/{len(todo)} 成功 {ok}・失敗 {ng}・費用 ${spent:.2f} {time.time() - t0:.0f}秒")
    log(f"終了 成功 {ok}・失敗 {ng}・費用の合計 ${spent:.2f} {time.time() - t0:.0f}秒")


if __name__ == "__main__":
    main()
