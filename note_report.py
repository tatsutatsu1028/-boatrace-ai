"""
1日の締めの結果報告記事（note 用）。記事表示ページ（?view=note&report=YYYY-MM-DD&key=…、note_page.py）で出す。

設定タブの「📊 本日のまとめ投稿」と同じ集計・文面（threads_poster.build_daily_summary_note_texts の3パターン）を、
アプリを操作しなくても作れるようにしたもの。
  - 対象は確定済み（公開済み）の note 記事（自動下書きは、公開して記事ページで URL を登録すると入る）
  - 的中は記事に載せた買い目で判定。結果は Supabase と公式の結果一覧から引く（その日のうちは Supabase に無いことがある）
  - 結果待ちのレースは「結果待ち」として載せる
Streamlit には依存しない。
"""

from __future__ import annotations

from datetime import date as _date


def _actuals(date8, records):
    """記事にした会場の公式結果一覧から ({race_key: 3連単}, {race_key: 払戻}) 。取れなくても空で続ける。"""
    from official_fetcher import fetch_venue_result_list

    actuals, payouts = {}, {}
    codes = {str(r["race_key"]).split("_")[1] for r in records if str(r.get("race_key", "")).count("_") == 2}
    for code in sorted(codes):
        try:
            df = fetch_venue_result_list(date8, code)
        except Exception:  # noqa: BLE001
            continue
        if df is None or not len(df):
            continue
        for _, row in df.iterrows():
            try:
                key = f"{date8}_{code}_{int(float(row.get('race_no')))}"
                if str(row.get("trifecta") or "").strip():
                    actuals[key] = str(row.get("trifecta")).strip()
                payouts[key] = int(float(row.get("payout_per_100") or 0))
            except Exception:  # noqa: BLE001
                continue
    return actuals, payouts


def build(sb_url, sb_key, day):
    """結果報告記事: {"variants": [{label, title, body}] x3, "records", "pending": [...], "settled": n}。"""
    import threads_poster as tp

    day = day if isinstance(day, _date) else _date.fromisoformat(str(day))
    records = tp.fetch_daily_publication_results(sb_url, sb_key, day)
    actuals, payouts = _actuals(day.strftime("%Y%m%d"), records)
    tp.apply_actuals(records, actuals)
    variants = tp.build_daily_summary_note_texts(race_date=day, records=records, payouts=payouts,
                                                 include_pending=True)
    pending = [r for r in records if not r.get("settled")]
    return {"variants": variants, "records": records, "pending": pending,
            "settled": len(records) - len(pending)}
