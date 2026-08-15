どれ？
どれ？
どこ？
どこ？
どうやってテキスト開く？
再起動はどこ？
再起動できてる？
昨日 17:26
AI予想したら
どうしたらいい？
この画面ではない？
元々そうなっています
貼り付けられたテキスト（1 点）.txt
ドキュメント
貼り付けられたテキスト（1 点）(1).txt
ドキュメント
貼り付けられたテキスト（1 点）(2).txt
ドキュメント
貼り付けられたテキスト（1 点）(3).txt
ドキュメント
貼り付けられたテキスト（1 点）(4).txt
ドキュメント
どこに追加？
貼り付けられたテキスト（1 点）(5).txt
ドキュメント
あってます？
2026 08 15T15 22 Export
日本語にして
貼り付けられたテキスト（1 点）(6).txt
ドキュメント
貼り付けられたテキスト（1 点）(7).txt
ドキュメント
貼り付けられたテキスト（1 点）(8).txt
ドキュメント
あってます？
貼り付けられたテキスト（1 点）(9).txt
ドキュメント
貼り付けられたテキスト（1 点）(10).txt
ドキュメント
貼り付けられたテキスト（1 点）(20260815-161508).txt
ドキュメント

はい、確認しました。追加位置は合っています。 👍

import も正しく追加されています。

さらに fetch_official_race() 内も、

今節成績取得 → コース別成績取得 → 展示情報取得

という狙いどおりの順番になっています。

特に、取得した course_stats を lane で base に結合し、失敗時には3項目を NaN にする処理まで入っているのでOKです。

次は実際に取得できるか確認です

この official_fetcher.py をGitHubに保存・コミットして、Streamlitが再起動したらレースを1つ読み込んでください。

ログに、

[COURSE_STATS]

または

[COURSE_STATS ERROR]

が出るはずです。

その部分のログをそのまま送ってください。

ここが成功したら、course_top3_rate・course_avg_st・course_start_rank が本当に6艇分取れているか確認します。

まだ予想ロジックには入れません。取得確認 → データの正しさ確認 → その後に予想へ組み込み、の順で進めましょう。

logs-tatsutatsu1028--boatrace-ai-main-app.py-2026-08-15T16_18_36.007Z.txt
ドキュメント

ログ確認しました。かなり重要なところまで来ています。

今回のログでは、course_stats_fetcher.py 自体は実行されています。 course_stats_fetcher.py:95 の警告が6回出ているので、6艇それぞれについてページ取得・解析処理まで入っていると考えられます。

一方で、期待していた

[COURSE_STATS] ...

も

[COURSE_STATS ERROR] ...

もログには出ていません。検索しても両方とも0件でした。

つまり現状は、

「新しい取得処理はちゃんと呼ばれている。でも取得結果の中身が正しいかは、まだ確認できていない」

という状態です。

なお大量に出ている XMLParsedAsHTMLWarning は、今回の本命エラーではありません。アプリもその後まで動いています。

なので、ここで予想ロジックへ組み込むのはまだ待った方がいいです。

次は course_stats_fetcher.py の実際の取得値を直接ログへ出すようにしましょう。そこで6艇について、

course_top3_rate / course_avg_st / course_start_rank

が正しい数字になっているか確認します。

次は私の方で、確認用ログを入れた course_stats_fetcher.py をそのままGitHubへアップロードできるファイルにして渡すのが一番確実です。

お願い
分析が終了しました
from pathlib import Path
                "lane=", lane,
                "racer_id=EMPTY",
            )
            rows.append(rec)
            continue


        try:
            stats = fetch_racer_course_stats(racer_id)


            print(
                "[COURSE_STATS DEBUG RAW]",
                "lane=", lane,
                "racer_id=", racer_id,
                stats.to_dict("records"),
            )


            hit = stats[stats["course"] == lane]


            if len(hit):
                x = hit.iloc[0]
                rec["course_top3_rate"] = x["course_top3_rate"]
                rec["course_avg_st"] = x["course_avg_st"]
                rec["course_start_rank"] = x["course_start_rank"]


            print(
                "[COURSE_STATS DEBUG PICK]",
                "lane=", lane,
                "racer_id=", racer_id,
                "top3=", rec["course_top3_rate"],
                "avg_st=", rec["course_avg_st"],
                "start_rank=", rec["course_start_rank"],
            )


        except Exception as e:
            print(
                "[COURSE_STATS ERROR]",
                "lane=", lane,
                "racer_id=", racer_id,
                type(e).__name__,
                str(e),
            )


        rows.append(rec)


    out = (
        pd.DataFrame(rows)
        .sort_values("lane")
        .reset_index(drop=True)
    )


    print(
        "[COURSE_STATS DEBUG FINAL]",
        out.to_dict("records"),
    )


    return out
'''


if old not in text:
    raise RuntimeError("対象関数が見つかりませんでした。")


text = text.replace(old, new, 1)
compile(text, "course_stats_fetcher_debug.py", "exec")


out = Path("/mnt/data/course_stats_fetcher_debug.py")
out.write_text(text, encoding="utf-8")


print("✅ course_stats_fetcher_debug.py を作成しました")


STDOUT/STDERR
✅ course_stats_fetcher_debug.py を作成しました


作りました。今回は取得値をログへ直接出す確認用版です。

course_stats_fetcher_debug.py をダウンロード

GitHubでは、今ある course_stats_fetcher.py の中身をこのファイルの中身で置き換えてください。ファイル名は最終的に course_stats_fetcher.py のままです。

コミット後にレースを1つ取得すると、ログへ

[COURSE_STATS DEBUG RAW]
[COURSE_STATS DEBUG PICK]
[COURSE_STATS DEBUG FINAL]

が出ます。

その3種類が出ている部分を送ってください。そこで6艇分のコース3連対率・コース平均ST・コースST順位が本当に正しく取れているか確認します。 

course_stats_fetcher_debug.py
コード

ライブラリ
/
course_stats_fetcher_debug.py
from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup


BASE = "https://www.boatrace.jp/owpc/pc/data/racersearch/course"

UA = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(compatible; BoatraceAIMobile/3.0; personal-analysis-tool)"
    )
}


def _norm(x):
    s = "" if x is None else str(x)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()


def _num(x):
    s = _norm(x).replace("%", "")

    if not s or s in {"-", "--"}:
        return np.nan

    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else np.nan


def _get_course_page(racer_id):
    url = f"{BASE}?toban={str(racer_id).strip()}"

    r = requests.get(
        url,
        headers=UA,
        timeout=20,
    )
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"

    return r.text, url


def _extract_six_values(text, heading):
    pos = text.find(heading)

    if pos < 0:
        return {}

    tail = text[pos + len(heading):]

    next_heads = (
        "コース別進入率",
        "コース別3連対率",
        "コース別平均スタートタイミング",
        "コース別スタート順",
    )

    ends = []

    for h in next_heads:
        p = tail.find(h)
        if p >= 0:
            ends.append(p)

    if ends:
        tail = tail[:min(ends)]

    out = {}

    pat = re.compile(
        r"(?:^|\s)([1-6])\s+(-{1,2}|\d+(?:\.\d+)?%?)"
    )

    for lane_s, value_s in pat.findall(tail):
        lane = int(lane_s)

        if lane not in out:
            out[lane] = _num(value_s)

    return out


def fetch_racer_course_stats(racer_id):
    html, url = _get_course_page(racer_id)

    soup = BeautifulSoup(html, "lxml")
    text = _norm(soup.get_text(" ", strip=True))

    top3 = _extract_six_values(
        text,
        "コース別3連対率",
    )

    avg_st = _extract_six_values(
        text,
        "コース別平均スタートタイミング",
    )

    start_rank = _extract_six_values(
        text,
        "コース別スタート順",
    )

    rows = []

    for course in range(1, 7):
        rows.append({
            "course": course,
            "course_top3_rate": top3.get(course, np.nan),
            "course_avg_st": avg_st.get(course, np.nan),
            "course_start_rank": start_rank.get(course, np.nan),
        })

    out = pd.DataFrame(rows)
    out.attrs["source"] = url

    return out


def fetch_course_stats_for_race(race):
    rows = []

    for _, row in race.iterrows():
        lane = int(row["lane"])
        racer_id = _norm(row.get("racer_id", ""))

        rec = {
            "lane": lane,
            "course_top3_rate": np.nan,
            "course_avg_st": np.nan,
            "course_start_rank": np.nan,
        }

        if not racer_id:
            print(
                "[COURSE_STATS DEBUG]",
                "lane=", lane,
                "racer_id=EMPTY",
            )
            rows.append(rec)
            continue

        try:
            stats = fetch_racer_course_stats(racer_id)

            print(
                "[COURSE_STATS DEBUG RAW]",
                "lane=", lane,
                "racer_id=", racer_id,
                stats.to_dict("records"),
            )

            hit = stats[stats["course"] == lane]

            if len(hit):
                x = hit.iloc[0]
                rec["course_top3_rate"] = x["course_top3_rate"]
                rec["course_avg_st"] = x["course_avg_st"]
                rec["course_start_rank"] = x["course_start_rank"]

            print(
                "[COURSE_STATS DEBUG PICK]",
                "lane=", lane,
                "racer_id=", racer_id,
                "top3=", rec["course_top3_rate"],
                "avg_st=", rec["course_avg_st"],
                "start_rank=", rec["course_start_rank"],
            )

        except Exception as e:
            print(
                "[COURSE_STATS ERROR]",
                "lane=", lane,
                "racer_id=", racer_id,
                type(e).__name__,
                str(e),
            )

        rows.append(rec)

    out = (
        pd.DataFrame(rows)
        .sort_values("lane")
        .reset_index(drop=True)
    )

    print(
        "[COURSE_STATS DEBUG FINAL]",
        out.to_dict("records"),
    )

    return out
