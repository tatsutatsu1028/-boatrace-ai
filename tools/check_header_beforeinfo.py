"""一時（確認用）: レース共通の見出し（グレード・何日目・レース名・進入固定・安定板・締切）を
直前情報ページから読んでも、出走表ページから読んだ値と同じになるかを確かめる。
ログには項目ごとの一致件数だけを出す。"""
from bs4 import BeautifulSoup

import history_store as store
from history_pages import parse_race_header, _fetch

FIELDS = ["grade", "grade_class", "grade_extra", "meet_title", "meet_days", "day_no", "day_label",
          "is_final_day", "race_name", "distance_m", "fixed_entry", "stabilizer", "deadline", "race_category"]
k = store.read_kind("k_results", "20250101", "20260927",
                    columns=["race_key", "race_type", "fixed_entry"]).drop_duplicates("race_key")
picks = []
picks += list(k[k.fixed_entry == "1"].race_key.sample(3, random_state=1))
picks += list(k[k.race_type == "優勝戦"].race_key.sample(3, random_state=1))
picks += list(k[k.race_type == "準優勝戦"].race_key.sample(2, random_state=1))
picks += list(k[~k.race_type.isin(["優勝戦", "準優勝戦"])].race_key.sample(4, random_state=1))
same = {f: 0 for f in FIELDS}
n = 0
for rk in picks:
    hd, jcd, rno = rk.split("_")
    a_html = _fetch("racelist", hd, jcd, rno)
    b_html = _fetch("beforeinfo", hd, jcd, rno)
    a = parse_race_header(BeautifulSoup(a_html, "lxml"), a_html, hd, rno)
    b = parse_race_header(BeautifulSoup(b_html, "lxml"), b_html, hd, rno)
    n += 1
    diff = [f for f in FIELDS if str(a.get(f)) != str(b.get(f))]
    for f in FIELDS:
        same[f] += f not in diff
    print(f"[HEADER] {rk} 不一致: {diff or 'なし'}  進入固定(出走表/直前/K)={a.get('fixed_entry')}/{b.get('fixed_entry')}/"
          f"{k.set_index('race_key').fixed_entry.get(rk)}", flush=True)
print(f"[HEADER] {n}レース 項目ごとの一致数: {same}", flush=True)
