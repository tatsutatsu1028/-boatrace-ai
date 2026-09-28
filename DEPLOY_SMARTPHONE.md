
# スマホ利用までの最短手順

1. このフォルダをGitHubのリポジトリへアップロード（学習データは含めない。README「学習データの置き場所」参照）。
2. Streamlit Community Cloudで「New app」を選択。
3. Repositoryを指定し、Main file pathに `app.py`。
4. Advanced settings の Secrets に、既存の設定（`ADMIN_PIN`・`STAFF_<ID>_PIN`・`AUTH_COOKIE_SECRET`・
   `THREADS_APP_SECRET`・`[supabase]`）に加えて、学習データ取得用の
   `DATA_REPO_TOKEN`（データ用リポジトリの読み取り専用トークン）を入れる。
5. Deploy。
6. 発行されたURLをスマホのSafari/Chromeで開く。
7. 「ホーム画面に追加」。

## 注意
公式BOAT RACEページへのアクセスがデプロイ先ネットワークから許可されている必要があります。
自動取得に失敗した場合でも、アプリ内の表を手入力/編集できます。
