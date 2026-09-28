"""
オッズ生データの集約とクリーンアップ。

odds_snapshots には5分おきに3連単の全120通りを保存しているため、
1レースを6時間追跡すると 72回 × 120通り = 8,640行 になる。
このまま貯め続けるとSupabase無料枠(1プロジェクト500MB)を
数ヶ月で使い切ってしまう。

そこで、結果が確定して一定日数が過ぎたレースについては
「回収率の検証に直結する部分だけ」を odds_summary に集約し、
生データ(odds_snapshots)は削除する。

残す対象は次の3つの和集合とする。
  1. 実際に購入した買い目
  2. AIが候補に挙げた買い目（買わなかったものも含む）
  3. 実際に的中した買い目

1だけだと「買った買い目が当たったか」しか分からず、
「その買い方が正しかったか」が検証できないため2を含める。
逆にAIが候補にすら挙げなかった残り約100通りは、
回収率の議論に影響しないので保存しない。

これにより1レースあたり 8,640行 → 20〜30行 まで圧縮される。

集約で特に重要なのが odds_at_lock と odds_final の差。
stake_allocator は「予想を固定した時点のオッズ」で期待値を計算して
資金配分を決めているが、実際の払戻は締切時点のオッズで決まる。
両者が体系的にズレていると期待値の計算自体が間違っていることになり、
ev_floor などの閾値調整では直せない。この差を残しておくことで、
後から「AIの本命は直前に人気が集まってオッズが落ちるのか」等を検証できる。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import requests


SUMMARY_TABLE = "odds_summary"

# 結果確定からこの日数が過ぎたレースを集約・削除の対象にする。
# 直近はグラフで生データを確認したいので、少し余裕を持たせる。
DEFAULT_RETENTION_DAYS = 7

# 1回の実行で処理するレース数の上限。
# GitHub Actionsが5分おきに走るので、一度に全部やる必要はない。
MAX_RACES_PER_RUN = 20


def _iso_to_dt(value):
    """SupabaseのISO文字列をaware datetimeにする。失敗したらNone。"""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        # タイムゾーン無しは（Streamlit Cloud基準で）UTCとみなす。
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_float(value):
    try:
        f = float(value)
    except Exception:
        return None
    if f != f:  # NaN
        return None
    return f


def _collect_target_combos(snapshot_row, actual_combo):
    """
    このレースで保存対象にする買い目と、その付随情報を返す。

    戻り値: {combo: {"was_purchased": bool, "stake": int,
                     "ticket_group": str, "ai_prob": float|None}}
    """
    targets = {}

    payload = {}
    if snapshot_row:
        raw = snapshot_row.get("payload_json", "")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}

    for item in payload.get("tickets", []) or []:
        if not isinstance(item, dict):
            continue

        combo = str(item.get("combo", "")).strip()
        if not combo:
            continue

        try:
            stake = int(float(item.get("stake", 0) or 0))
        except Exception:
            stake = 0

        targets[combo] = {
            "was_purchased": stake > 0,
            "stake": stake,
            "ticket_group": str(item.get("group", "") or ""),
            "ai_prob": _safe_float(item.get("prob")),
        }

    # 的中した買い目は、AIが候補に挙げていなくても必ず残す。
    # 「AIが見落とした買い目がどう動いていたか」は回収率の検証に直結する。
    actual = str(actual_combo or "").strip()
    if actual and actual not in targets:
        targets[actual] = {
            "was_purchased": False,
            "stake": 0,
            "ticket_group": "",
            "ai_prob": None,
        }

    return targets


def _aggregate_combo(samples):
    """
    1つの買い目のオッズ時系列を集約する。
    samples: [{"odds": float, "fetched_at": datetime}, ...] （時刻昇順）
    """
    values = [s["odds"] for s in samples]

    return {
        "odds_at_lock": values[0],
        "odds_final": values[-1],
        "odds_min": min(values),
        "odds_max": max(values),
        "n_samples": len(values),
        "first_at": samples[0]["fetched_at"].isoformat(),
        "last_at": samples[-1]["fetched_at"].isoformat(),
    }


def build_summary_rows(race_key, race_date, jcd, rno, odds_rows, targets, actual_combo):
    """
    生オッズ行と保存対象の買い目から、odds_summary に入れる行を組み立てる。

    Supabaseに依存しない純粋な関数にしてあるので、単体でテストできる。
    """
    by_combo = {}

    for row in odds_rows:
        combo = str(row.get("combo", "")).strip()
        if combo not in targets:
            continue

        odds = _safe_float(row.get("odds"))
        if odds is None or odds < 1:
            continue

        fetched_at = _iso_to_dt(row.get("fetched_at"))
        if fetched_at is None:
            continue

        by_combo.setdefault(combo, []).append(
            {"odds": odds, "fetched_at": fetched_at}
        )

    actual = str(actual_combo or "").strip()
    out = []

    for combo, samples in by_combo.items():
        samples.sort(key=lambda s: s["fetched_at"])
        agg = _aggregate_combo(samples)
        meta = targets[combo]

        ai_prob = meta.get("ai_prob")
        odds_lock = agg["odds_at_lock"]
        odds_final = agg["odds_final"]

        # オッズがどれだけ動いたか。1.0なら不変、
        # 1.0未満なら人気が集まってオッズが下がった（＝配当が減った）。
        odds_drift = odds_final / odds_lock if odds_lock else None

        # 固定時点の期待値と、締切時点の実際の期待値。
        # この2つの差が資金配分ロジックの体系的な誤差になる。
        ev_at_lock = ai_prob * odds_lock if ai_prob is not None else None
        ev_final = ai_prob * odds_final if ai_prob is not None else None

        out.append({
            "race_key": race_key,
            "race_date": str(race_date),
            "jcd": str(jcd).zfill(2),
            "rno": int(rno),
            "combo": combo,
            "was_purchased": bool(meta.get("was_purchased")),
            "stake": int(meta.get("stake", 0) or 0),
            "ticket_group": meta.get("ticket_group", ""),
            "is_hit": combo == actual,
            "ai_prob": ai_prob,
            "odds_at_lock": odds_lock,
            "odds_final": odds_final,
            "odds_min": agg["odds_min"],
            "odds_max": agg["odds_max"],
            "odds_drift": odds_drift,
            "ev_at_lock": ev_at_lock,
            "ev_final": ev_final,
            "n_samples": agg["n_samples"],
            "first_at": agg["first_at"],
            "last_at": agg["last_at"],
        })

    out.sort(key=lambda r: (not r["was_purchased"], r["combo"]))
    return out


# -------------------------------------------------
# Supabaseとのやり取り
# -------------------------------------------------

def _get(url, headers, endpoint, timeout=30):
    r = requests.get(f"{url}/rest/v1/{endpoint}", headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _find_rollup_targets(url, headers, retention_days, result_table, snapshot_table):
    """
    集約対象のレースを探す。

    条件は「結果が確定済み」かつ「保存から retention_days 日以上経過」。
    結果が確定していないレースを消すと、あとから検証できなくなるため、
    prediction_results に行があることを必須にする。

    探す向きが重要で、「終わったレース」側から古い生データを探すと、
    追いついたあとも毎回大量に空振りの問い合わせが発生する
    （5分おきの実行なので1日あたり数万クエリになりうる）。
    そこで「古い生データ」側から出発し、そのレースの結果が
    確定しているかを確認する形にしている。
    片付いていれば最初の1クエリで終わる。
    """
    cutoff_compact = (
        datetime.now(timezone.utc) - timedelta(days=retention_days)
    ).date().strftime("%Y%m%d")

    # 保持期間を過ぎた生データを持つレースを洗い出す。
    # PostgRESTにDISTINCTが無いので、行を取ってからクライアント側で畳む。
    old_rows = _get(
        url, headers,
        f"{snapshot_table}"
        f"?select=race_date,jcd,rno"
        f"&race_date=lt.{cutoff_compact}"
        f"&order=race_date.asc"
        f"&limit=5000",
    )

    if not old_rows:
        return []

    seen = []
    seen_keys = set()
    for row in old_rows:
        hd = str(row.get("race_date", "")).strip().replace("-", "")
        jcd = str(row.get("jcd", "")).strip().zfill(2)
        try:
            rno = int(row.get("rno"))
        except Exception:
            continue
        if not hd or jcd == "00":
            continue

        race_key = f"{hd}_{jcd}_{rno}"
        if race_key in seen_keys:
            continue
        seen_keys.add(race_key)
        seen.append({"race_key": race_key, "hd": hd, "jcd": jcd, "rno": rno})

        if len(seen) >= MAX_RACES_PER_RUN:
            break

    if not seen:
        return []

    # 結果が確定しているレースだけを対象にする。
    # in.(...) でまとめて問い合わせ、1クエリで済ませる。
    key_list = ",".join(f'"{s["race_key"]}"' for s in seen)
    finished = _get(
        url, headers,
        f"{result_table}"
        f"?select=race_key,trifecta_actual"
        f"&race_key=in.({key_list})",
    )

    actual_by_key = {
        str(row.get("race_key", "")).strip():
            str(row.get("trifecta_actual", "") or "").strip()
        for row in finished
    }

    targets = []
    for s in seen:
        if s["race_key"] not in actual_by_key:
            # 結果未確定のレースは消さずに残す。
            continue
        targets.append({**s, "actual_combo": actual_by_key[s["race_key"]]})

    return targets


def rollup_and_prune(
    url,
    headers_fn,
    retention_days=DEFAULT_RETENTION_DAYS,
    snapshot_table="odds_snapshots",
    result_table="prediction_results",
    prediction_snapshot_table="prediction_snapshots",
    summary_table=SUMMARY_TABLE,
):
    """
    結果確定済みかつ retention_days 日を過ぎたレースについて、
    オッズ生データを集約してから削除する。

    集約の保存に成功したレースだけ生データを消す。
    途中で失敗しても、そのレースの生データは残るので次回再試行できる。
    """
    headers = headers_fn()
    processed = 0
    freed_rows = 0

    try:
        targets = _find_rollup_targets(
            url, headers, retention_days, result_table, snapshot_table,
        )
    except Exception as e:
        print(
            "[ROLLUP] 対象レースの取得に失敗:",
            type(e).__name__, str(e), flush=True,
        )
        return 0, 0

    if not targets:
        print("[ROLLUP] 集約対象なし", flush=True)
        return 0, 0

    for t in targets:
        race_key = t["race_key"]

        try:
            # 予想固定（買い目と予測確率の出どころ）
            snap_rows = _get(
                url, headers,
                f"{prediction_snapshot_table}"
                f"?race_key=eq.{race_key}&select=*&order=saved_at.asc&limit=1",
            )
            snapshot_row = snap_rows[0] if snap_rows else None

            combos = _collect_target_combos(snapshot_row, t["actual_combo"])

            if not combos:
                # 買い目情報が無いレースは集約しても意味がないが、
                # 生データを残し続ける理由も無いので、そのまま削除に進む。
                print(
                    "[ROLLUP] 買い目情報なし（集約せず削除）:",
                    race_key, flush=True,
                )
                summary_rows = []
            else:
                odds_rows = _get(
                    url, headers,
                    f"{snapshot_table}"
                    f"?select=combo,odds,fetched_at"
                    f"&race_date=eq.{t['hd']}&jcd=eq.{t['jcd']}&rno=eq.{t['rno']}"
                    f"&order=fetched_at.asc",
                )

                summary_rows = build_summary_rows(
                    race_key, t["hd"], t["jcd"], t["rno"],
                    odds_rows, combos, t["actual_combo"],
                )

            if summary_rows:
                r = requests.post(
                    f"{url}/rest/v1/{summary_table}?on_conflict=race_key,combo",
                    headers=headers_fn("resolution=merge-duplicates,return=minimal"),
                    json=summary_rows,
                    timeout=30,
                )
                r.raise_for_status()

            # 集約の保存に成功した後だけ生データを消す。
            d = requests.delete(
                f"{url}/rest/v1/{snapshot_table}"
                f"?race_date=eq.{t['hd']}&jcd=eq.{t['jcd']}&rno=eq.{t['rno']}",
                headers=headers_fn("return=representation"),
                timeout=60,
            )
            d.raise_for_status()

            try:
                deleted = len(d.json())
            except Exception:
                deleted = 0

            processed += 1
            freed_rows += deleted

            print(
                "[ROLLUP] 集約完了:", race_key,
                "summary=", len(summary_rows),
                "deleted=", deleted,
                flush=True,
            )

        except Exception as e:
            # 1レース失敗しても他は続行する。生データは残るので次回再試行。
            print(
                "[ROLLUP] エラー:", race_key,
                type(e).__name__, str(e), flush=True,
            )

    print(
        f"[ROLLUP] {processed}レースを集約、{freed_rows}行を削除しました。",
        flush=True,
    )
    return processed, freed_rows
