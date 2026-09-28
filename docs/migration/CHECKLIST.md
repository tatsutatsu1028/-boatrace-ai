# データ非公開化の移行チェックリスト（C案）

公式データ（`sample_history.csv`・`history_full.csv`・`data/history/`）を公開リポジトリで
再配布しないよう、次の3つのリポジトリに分けます。

| リポジトリ | 公開/非公開 | 中身 |
|---|---|---|
| `tatsutatsu1028/-boatrace-ai`（新しく作り直す） | 公開 | コードだけ。**履歴なし**（データを含む過去コミットを持ち込まない） |
| `tatsutatsu1028/-boatrace-ai-data`（新規） | 非公開 | 学習・過去データだけ |
| `tatsutatsu1028/-boatrace-ai-archive`（今のリポジトリの改名） | 非公開・アーカイブ | 今までの履歴・PR・ブランチ一式。Actionsは停止 |

新しい公開リポジトリは、最初 `-boatrace-ai-new` という名前で作ります。準備と動作確認が
終わってから、名前を入れ替えます（今のリポジトリ → `-boatrace-ai-archive`、
`-boatrace-ai-new` → `-boatrace-ai`）。名前が入れ替わるまで、本番は今のリポジトリで動き続けます。

- **pg_cron の起動先URL**（`…/repos/tatsutatsu1028/-boatrace-ai/…`）は名前が同じなので変更不要
- **Streamlit のURL** は変わらない見込みです（F-4 参照。だめな場合も同じURLに戻す手順あり）
- 定期実行（schedule）は、リポジトリ名が `-boatrace-ai` のときだけ動く設定にしてあるので、
  `-boatrace-ai-new` の間に本番と二重に動くことはありません

凡例: 🤖 = Codeタブ（Claude）が行う ／ 👤 = ユーザーが手で行う

---

## A. 事前準備（🤖 済み）

- [x] 🤖 A-1 朝のスケジュール取得の修正（PR #52）を main へマージ
- [x] 🤖 A-2 過去データ収集（history_backfill）の pg_cron 起動を一時停止
      （`trigger-history-backfill-night` / `-morning` を `active = false`。H-1 で再開）
- [x] 🤖 A-3 コード修正（データ用リポジトリから読む・書く、成果物保存の削除）
- [x] 🤖 A-4 このチェックリストと引き継ぎメモ（`docs/migration/HANDOFF.md`）をpush

## B. 空のリポジトリを2つ作る（👤 約5分・本番への影響なし）

スマホのブラウザ（Safari / Chrome）で github.com にログインして操作します。
GitHubアプリではリポジトリ作成・設定変更ができない項目があるため、ブラウザを使ってください。
画面が狭くて項目が見えないときは、ブラウザのメニューから「デスクトップ用Webサイトを表示」に切り替えると確実です。

- [ ] 👤 B-1 データ用リポジトリを作る
  1. https://github.com/new を開く
  2. Repository name: `-boatrace-ai-data`
  3. **Private** を選ぶ
  4. 「Add a README file」などの初期ファイルは **すべてオフ** のまま
  5. 「Create repository」
- [ ] 👤 B-2 新しい公開リポジトリを仮の名前で作る
  1. もう一度 https://github.com/new
  2. Repository name: `-boatrace-ai-new`
  3. **Public** を選ぶ
  4. 初期ファイルは **すべてオフ**
  5. 「Create repository」
- [ ] 👤 B-3 Claude GitHubアプリに2つのリポジトリを追加する（項目12）
  1. https://github.com/settings/installations を開く
  2. 「Claude」の右の「Configure」
  3. 下の方の「Repository access」
     - 「All repositories」になっていれば、何もしなくてよい
     - 「Only select repositories」なら「Select repositories」から
       `-boatrace-ai-data` と `-boatrace-ai-new` を追加
  4. 「Save」
- [ ] 👤 B-4 Codeタブに「B終わった」と送る

## C. データとコードを入れる（🤖）

- [ ] 🤖 C-1 データ用リポジトリへ、今の main のデータ（`sample_history.csv`・`history_full.csv`・`data/history/`）をpush
- [ ] 🤖 C-2 `-boatrace-ai-new` へ、コードだけを履歴なし（1コミット）でpush
- [ ] 🤖 C-3 `-boatrace-ai-new` にデータファイルが1つも入っていないことを確認

## D. トークンとSecretsを用意する（👤 約15分・本番への影響なし）

トークンは3つ作ります。どれも **Fine-grained token**、対象リポジトリを絞ったものです。
作成直後に1回だけ表示される値（`github_pat_…`）をコピーして、すぐ貼り付け先へ入れてください
（メモ帳などに残さない）。

### 共通: Fine-grained token の作り方

1. https://github.com/settings/personal-access-tokens/new を開く
2. Token name: 下の表の名前
3. Expiration: 「Custom」で1年後（選べる最長）。**期限の日付を控えておく**
4. Resource owner: `tatsutatsu1028`
5. Repository access: 「Only select repositories」→ 下の表のリポジトリを選ぶ
6. Permissions →「Repository permissions」→ 下の表の権限を設定
   （Metadata: Read-only は自動で付きます）
7. 一番下の「Generate token」→ もう一度「Generate token」→ 表示された値をコピー

| # | Token name | リポジトリ | 権限 | 貼り付け先 |
|---|---|---|---|---|
| T1 | `boatrace-data-rw` | `-boatrace-ai-data` | Contents: **Read and write** | D-1（Actions） |
| T2 | `boatrace-data-ro` | `-boatrace-ai-data` | Contents: **Read-only** | D-3（Streamlit） |
| T3 | `boatrace-actions-dispatch` | `-boatrace-ai` と `-boatrace-ai-new` の **2つ** | Actions: **Read and write** | D-4（Supabase） |

T3 は、今のリポジトリと新しいリポジトリの両方を選ぶのがポイントです。名前を入れ替える前も
後も pg_cron の起動が止まりません。

### 手順

- [ ] 👤 D-1 `-boatrace-ai-new` の Actions Secrets を3つ登録する
  1. https://github.com/tatsutatsu1028/-boatrace-ai-new/settings/secrets/actions を開く
  2. 「New repository secret」で1つずつ登録（Name は大文字・下線までそのまま）

  | Name | Secret（値） |
  |---|---|
  | `DATA_REPO_TOKEN` | T1 の値 |
  | `SUPABASE_URL` | `https://ajhluuyrslxkattojolg.supabase.co` |
  | `SUPABASE_SERVICE_ROLE_KEY` | Supabase のシークレットキー（下の取り方） |

  Supabase のシークレットキーの取り方:
  Supabase（https://supabase.com/dashboard）→ プロジェクト `boatrace-ai` → 左下の歯車「Project Settings」
  →「API Keys」→「Secret keys」にある既存のキー（`sb_secret_…`）の目のアイコンを押してコピー。
  今のリポジトリの `SUPABASE_SERVICE_ROLE_KEY` と同じキーで構いません
  （GitHubのSecretsは登録後に値を見られないため、Supabase側からコピーします）。
- [ ] 👤 D-2 T2 を作る（まだ貼らない場合はD-3と続けて）
- [ ] 👤 D-3 今の Streamlit アプリの Secrets に2行を追加する（項目10）
  1. https://share.streamlit.io を開いてログイン
  2. アプリ一覧でこのアプリの右の「⋮」→「Settings」→「Secrets」
  3. **既存の内容はそのまま**、一番上（`[supabase]` などの `[ ]` で始まる行より上）に次の2行を追加

     ```toml
     DATA_REPO_TOKEN = "T2の値"
     DATA_REPO = "tatsutatsu1028/-boatrace-ai-data"
     ```

  4. 「Save changes」（アプリが数秒で再起動します。今のコードはこの2行を使わないので影響はありません）
  5. このアプリの **URL（`https://〇〇.streamlit.app`）を控えておく**
- [ ] 👤 D-4 pg_cron が使うトークンを T3 に入れ替える（項目13）
  1. Supabase → プロジェクト `boatrace-ai` → 左メニュー「SQL Editor」→「New query」
  2. 次を貼り付け、`ここにT3の値` を T3 に置き換えて「Run」

     ```sql
     select vault.update_secret(
       (select id from vault.secrets where name = 'github_track_odds_token'),
       'ここにT3の値'
     );
     ```

  3. 「Success」と出ればOK（エラーの場合は画面の文言をCodeタブに送ってください）
- [ ] 👤 D-5 Codeタブに「D終わった」と送る

## E. 新しいリポジトリで動作確認（🤖・本番への影響なし）

- [ ] 🤖 E-1 pg_cron の track_odds 起動が T3 で成功していることを確認（旧リポジトリの実行履歴）
- [ ] 🤖 E-2 `-boatrace-ai-new` で「Collect History」を手動実行（前日分＝取得済みなので変更なし）
      → データ用リポジトリの取り出しができることを確認
- [ ] 🤖 E-3 `-boatrace-ai-new` で「History Backfill」を数分だけ手動実行
      → 続きから収集し、データ用リポジトリへpushされることを確認
- [ ] 🤖 E-4 公開側のログ・成果物にデータが出ていないことを確認

## F. 切り替え（👤 約5分）

**E が終わった連絡のあと、すぐに**続けて行ってください。F-1〜F-3 の間（1〜2分）だけ、
オッズ追跡などの起動が1回抜けることがあります。

- [ ] 👤 F-1 今のリポジトリの Actions を止める（項目8）
  1. https://github.com/tatsutatsu1028/-boatrace-ai/settings/actions
  2. 「Actions permissions」で「Disable actions」を選ぶ →「Save」
- [ ] 👤 F-2 今のリポジトリの名前を変える
  1. https://github.com/tatsutatsu1028/-boatrace-ai/settings
  2. 「Repository name」を `-boatrace-ai-archive` に変更 →「Rename」
- [ ] 👤 F-3 新しいリポジトリの名前を本番名にする
  1. https://github.com/tatsutatsu1028/-boatrace-ai-new/settings
  2. 「Repository name」を `-boatrace-ai` に変更 →「Rename」
- [ ] 👤 F-4 Streamlit を新しいコードで起動し直す（項目9・11）
  1. https://share.streamlit.io → このアプリの「⋮」→「Reboot app」→「Reboot」
  2. 1〜2分後にアプリを開き、ログイン画面が出ることを確認
  3. アプリ一覧の「⋮」→「Logs」（または、アプリ画面右下の「Manage app」）で、
     `[DATA] synced tatsutatsu1028/-boatrace-ai-data` という行が出ていれば切り替え成功
  4. **その行が出ない・エラーになる場合** → 下の「F-4がうまくいかない場合」へ
- [ ] 👤 F-5 旧リポジトリを非公開にしてアーカイブする
  1. https://github.com/tatsutatsu1028/-boatrace-ai-archive/settings
  2. 一番下「Danger Zone」→「Change visibility」→「Change to private」→ 確認の文言を入力して確定
  3. 同じ「Danger Zone」→「Archive this repository」→ 確定
- [ ] 👤 F-6 Codeタブに「F終わった」と送る

### F-4がうまくいかない場合（URLを変えずに新しいアプリへ移す）

Streamlit が旧リポジトリ（改名後）を見続けている場合の手順です。旧アプリを消すまで、今のアプリは動き続けます。

1. 旧アプリの「⋮」→「Settings」→「Secrets」を開き、中身を **全部コピー**（まだ閉じない）
2. share.streamlit.io 右上「Create app」→「Deploy a public app from GitHub」
   - Repository: `tatsutatsu1028/-boatrace-ai`、Branch: `main`、Main file path: `app.py`
   - App URL: 仮の名前（例 `boatrace-ai-tmp`）
   - 「Advanced settings」→ Python version を旧アプリと同じにし、Secrets に 1 でコピーした内容を貼り付け →「Save」
   - 「Deploy」
3. 仮URLのアプリでログインと予想ができることを確認
4. 旧アプリの「⋮」→「Delete」（ここから数分、旧URLが使えません）
5. 新アプリの「⋮」→「Settings」→「General」→「App URL」を、D-3 で控えた旧URLの名前に変更 →「Save」
6. 旧URLでアプリが開けることを確認

## G. 仕上げ・確認（🤖／👤）

- [ ] 🤖 G-1 過去データ収集の pg_cron を再開（**今夜 22:40 JST の起動に間に合わせる**）
- [ ] 🤖 G-2 新リポジトリで、pg_cron 起動の track_odds・refresh_schedule_status が成功していることを確認
- [ ] 🤖 G-3 auto_result_collect（結果確定）・auto_random_fix（自動固定）を手動実行して成功を確認
- [ ] 🤖 G-4 prefetch_schedule（朝のスケジュール取得）を ensure で手動実行して成功を確認
- [ ] 🤖 G-5 旧リポジトリ（archive）で新しい実行が起きていないことを確認
- [ ] 👤 G-6 アプリでログイン → 1レース予想できることを確認（スタッフのPINでも1回）
- [ ] 🤖 G-7 翌朝、00:30 の日次収集・夜間の過去データ収集・朝のスケジュール取得の結果を確認

## トークンの期限

| トークン | 使う場所 | 期限が切れると |
|---|---|---|
| T1 `boatrace-data-rw` | 公開リポジトリの Secrets `DATA_REPO_TOKEN` | データ収集・自動固定が失敗 |
| T2 `boatrace-data-ro` | Streamlit Secrets `DATA_REPO_TOKEN` | アプリ再起動時に学習データを取れない（取得済みの分で動き続ける） |
| T3 `boatrace-actions-dispatch` | Supabase Vault `github_track_odds_token` | pg_cron からの起動（オッズ追跡など）が止まる |

期限の前に、同じ設定で作り直して各貼り付け先を更新してください（T3 は D-4 のSQL）。
