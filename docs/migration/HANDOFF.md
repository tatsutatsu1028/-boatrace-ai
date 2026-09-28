# 引き継ぎメモ: データ非公開化の移行（2026-09-28 開始）

Codeタブのセッションを新しいリポジトリで開き直した場合は、このメモと
`docs/migration/CHECKLIST.md` を読んでから続きを行ってください。

## 目的

公式データ（`sample_history.csv`・`history_full.csv`・`data/history/`）を公開リポジトリで
再配布しないよう、コードとデータを分ける（C案）。

| リポジトリ | 公開 | 役割 |
|---|---|---|
| `tatsutatsu1028/-boatrace-ai` | 公開 | コードだけ（履歴なしで作り直し）。Actions はここで動く（無料） |
| `tatsutatsu1028/-boatrace-ai-data` | 非公開 | データだけ |
| `tatsutatsu1028/-boatrace-ai-archive` | 非公開・アーカイブ | 旧リポジトリ（移行前の履歴・PR・ブランチ）。Actions 停止 |

移行作業中は、新しい公開リポジトリを `-boatrace-ai-new` という仮の名前で作り、
最後に名前を入れ替える（CHECKLIST の F）。

## 仕組み（コード側の変更点）

- `data_paths.py`: データの置き場所。環境変数 `BOATRACE_DATA_DIR` があればそこ、
  無ければリポジトリ直下。Streamlit 用に `sync_from_github()`（GitHub Contents API で
  blob SHA を比べ、変わったファイルだけ一時フォルダへ取得）
- `app.py`: 起動時に `_boat_ai_sync_data()`（`st.cache_resource`、ttl 1時間）で
  Secrets の `DATA_REPO_TOKEN`（読み取り専用）を使って `sample_history.csv`・`history_full.csv` を取得。
  取れなければ `st.error` を出して止まる。ログに `[DATA] synced …` を出す
- 各スクリプトの CSV の場所は `data_paths.data_path()` 経由
  （prediction / course_baseline / auto_random_fix / history_store / collect_history /
  backfill_kimarite / build_history_full / backtest・validate 系）
- ワークフロー:
  - データを使うもの（auto_random_fix / collect_history / history_backfill / backfill_kimarite）は
    `secrets.DATA_REPO_TOKEN`（Contents 読み書き）でデータ用リポジトリを `_data/` に取り出し、
    `BOATRACE_DATA_DIR=${{ github.workspace }}/_data`。コミットは `_data/` 側へ
  - collect_history の成果物（artifact）保存は削除。公開リポジトリへの書き込み権限は `contents: read` に
  - schedule 実行はリポジトリ名が `tatsutatsu1028/-boatrace-ai` のときだけ動く
    （`-boatrace-ai-new` の間に本番と二重に動かないため）
- `.gitignore` にデータファイルと `_data/` を追加（誤ってコミットしないため）

## 必要な Secrets / トークン

| 置き場所 | キー | 中身 |
|---|---|---|
| 公開リポジトリ Actions Secrets | `DATA_REPO_TOKEN` | T1: データ用リポジトリ Contents 読み書き |
| 公開リポジトリ Actions Secrets | `SUPABASE_URL` | `https://ajhluuyrslxkattojolg.supabase.co` |
| 公開リポジトリ Actions Secrets | `SUPABASE_SERVICE_ROLE_KEY` | Supabase のシークレットキー |
| Streamlit Secrets | `DATA_REPO_TOKEN` / `DATA_REPO` | T2: データ用リポジトリ Contents 読み取り専用 / `tatsutatsu1028/-boatrace-ai-data` |
| Streamlit Secrets（既存・変更なし） | `ADMIN_PIN`・`STAFF_<ID>_PIN`・`AUTH_COOKIE_SECRET`・`THREADS_APP_SECRET`・`[supabase] url / key` | そのまま |
| Supabase Vault | `github_track_odds_token` | T3: 公開リポジトリ Actions 読み書き（pg_cron の workflow_dispatch 用） |

## pg_cron（Supabase プロジェクト `ajhluuyrslxkattojolg`）

| jobid | 名前 | 移行中 |
|---|---|---|
| 1 | trigger-track-odds-every-5-minutes | 動かしたまま |
| 4 | trigger-refresh-schedule-status | 動かしたまま |
| 5 | trigger-prefetch-schedule | 動かしたまま |
| 6 | trigger-history-backfill-night（22:40 JST） | 11時頃に停止 → 14時頃に再開 |
| 7 | trigger-history-backfill-morning（04:40 JST） | 11時頃に停止 → 14時頃に再開 |

再開（CHECKLIST G-1、今夜 22:40 JST より前に）:

```sql
select cron.alter_job(job_id := 6, active := true);
select cron.alter_job(job_id := 7, active := true);
```

起動先URLは `…/repos/tatsutatsu1028/-boatrace-ai/…` のまま（名前を引き継ぐので変更不要）。
過去データ収集は取得済みを飛ばすので、データ用リポジトリの `data/history/` から続きを取る。

## 進み具合

CHECKLIST.md のチェック欄を参照。Codeタブが最後に更新した時点:

- A〜F: 完了（2026-09-28 14時頃に切り替え済み）
  - 公開 `-boatrace-ai`（新・履歴なし）/ 非公開 `-boatrace-ai-data` / 非公開・アーカイブ `-boatrace-ai-archive`
  - pg_cron の Vault トークンは T3（旧・新両方の Actions 権限）に入れ替え済み。
    旧リポジトリはアーカイブ済みなので、次に作り直すときは新リポジトリだけでよい
  - 過去データ収集の pg_cron（jobid 6・7）は再開済み（`active = true`）
- G（確認）: CHECKLIST の G 参照。翌朝に 00:30 の日次収集・夜間の過去データ収集・朝のスケジュール取得を確認する

## 注意

- 新しい公開リポジトリには、データファイルを含むコミットを絶対に入れない
  （旧リポジトリのブランチをそのまま push しない。コードだけを1コミットで入れる）
- 旧リポジトリの `claude/*`・`codex/*` ブランチは archive 側にだけ残る。続きの作業が必要なら
  データファイルを含めずにコードだけ新リポジトリへ移す
- 旧リポジトリの Actions は停止するので、旧リポジトリ側で PR を作っても CI・定期実行は動かない
