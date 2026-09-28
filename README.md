
# BOAT AI Mobile v2

スマホで使うことを前提にした Streamlit Webアプリ試作版です。

## 主な機能
- 日付 / 競艇場 / R を選択
- BOAT RACE公式ページから出走表系・直前情報をベストエフォート取得
- 展示タイム、展示ST、気象を反映
- 場別オリジナル展示（直線 / まわり足 / 1周）を入力可能
- 選手コメントを「伸び / 出足 / 回り足 / 乗り心地 / 総合気配」に解析
- 展示前 → 展示後の1着確率変化を表示
- 3連単120通りを計算
- 本線 / 抑え / 穴 に自動分類
- 3連単オッズ取得に成功した場合は期待値も表示
- CSV保存

## 学習データの置き場所
学習・過去データ（`sample_history.csv`・`history_full.csv`・`data/history/`）は公式データを
含むため、このリポジトリには置かず、非公開のデータ用リポジトリ
`tatsutatsu1028/-boatrace-ai-data` に置いています（`data_paths.py`）。

- Streamlitアプリ: Secrets の `DATA_REPO_TOKEN`（データ用リポジトリの Contents 読み取り専用トークン）で
  起動時に取得し、1時間ごとに更新を確認します
- GitHub Actions: Secrets の `DATA_REPO_TOKEN`（同 Contents 読み書き）で `_data/` に取り出し、
  収集したデータもデータ用リポジトリへコミットします
- 手元: データ用リポジトリのファイルをこのフォルダ直下に置くか、`BOATRACE_DATA_DIR` にその場所を指定します

## PCで起動
```bash
pip install -r requirements.txt
streamlit run app.py
```

同じWi-Fi内のスマホから見る場合は、PCのローカルIPを使って
`http://PCのIP:8501` にアクセスします。OSのファイアウォール設定が必要な場合があります。

## スマホでいつでも使う
Streamlit Community Cloud等へこのフォルダをGitHub経由でデプロイすると、
Safari/ChromeからURLで利用できます。ホーム画面に追加すればアプリ風に使えます。

## 学習CSVの必須列
`venue,race_no,lane,racer_win_rate,local_win_rate,motor_2ren,boat_2ren,avg_st,finish`

1艇1行、finish=1が勝者です。

## 重要
- 公式ページのHTML構造が変更された場合、自動取得部分の修正が必要になることがあります。
- オリジナル展示・選手コメントは24場で公開形式が統一されていないため、v2では手入力併用です。
- コメント解析はルールベース。過去コメントを蓄積できれば、将来は学習型NLPに置き換え可能です。
- 3連単確率はv2では1着強度をもとにしたPlackett-Luce近似です。
- 利益を保証するものではありません。実運用前に日付順のバックテストを行ってください。
