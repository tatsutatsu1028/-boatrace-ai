# Streamlitアプリへのプッシュ通知（PWA/Web Push）組み込み 実現可能性調査

「予想を固定したレースが的中したか」を、iPhoneのホーム画面に追加したアプリから直接プッシュ通知できるかの調査結果。
iOS 16.4以降、ホーム画面に追加したPWAであればLINE等の外部サービスを使わずWeb Push通知が可能という前提のもとで検証した。

**結論: 実現可能。既存のSupabase＋GitHub Actions構成がそのままWeb Push送信元として使えるため、大掛かりな新規インフラは不要。**

本ドキュメントは調査・設計方針の報告のみであり、実装はまだ行っていない。

---

## 1. Streamlitへの manifest.json / Service Worker 組み込みの可否

### 静的ファイル配信
`requirements.txt` は `streamlit>=1.37` を指定済みで、Streamlit 1.31以降の
`[server] enableStaticServing = true`（`.streamlit/config.toml`）+ `static/` フォルダによる
静的ファイル配信機能が使える。`manifest.json` と `sw.js` を `static/` に置けば、
`https://<app>.streamlit.app/app/static/manifest.json` のようなURLで同一オリジン配信できる。

### `<head>` へのタグ挿入（`<link rel="manifest">` 等）
Streamlitには `<head>` を直接編集する公式APIはない。ただし `app_core.py` では既に
`streamlit.components.v1.html()` から `window.parent.document` を操作する手法
（会場一覧の長押しリフレッシュ機能、`app_core.py` 内 `components.html(...)` で
`window.parent` に到達して独自スクリプトを常駐させている実装）が本番で動作している。

`components.html()` が生成するiframeは `srcdoc` かつ `sandbox` 未指定のため、
親ドキュメントと同一オリジン扱いになる。既存コードの実績から、同じ手法で

- `window.parent.document.head` に `<link rel="manifest">` を追加
- `window.parent.navigator.serviceWorker.register()` を呼び出す

ことは技術的に可能と判断できる。

### 注意点・リスク
- `sw.js` を `/app/static/sw.js` に置くと、デフォルトのService Worker制御スコープは
  `/app/static/` 止まりになる（`Service-Worker-Allowed` レスポンスヘッダーをStreamlit側で
  制御できないため、スコープを `/` まで広げられない）。
  ただし**プッシュ通知の受信・表示・タップ時の起動はSWの制御スコープに依存しない**ため、
  今回の用途（プッシュ通知のみ）では実害がない。ページ全体のオフラインキャッシュなど
  本格的なPWA化をする場合は別途対応が必要。
- `window.parent` への侵入はStreamlit公式サポート外の手法。Streamlitのバージョンアップで
  iframeのサンドボックス仕様が変わると壊れる可能性があるため、アップデート時の動作確認が必要。
- Streamlit Community Cloud上で `enableStaticServing` がCDN/キャッシュ層の影響を受けないか
  （実運用で確認が必要な項目）。

---

## 2. 必要な実装要素

| 要素 | 内容 |
|---|---|
| VAPIDキー | `py-vapid` / `pywebpush` で1回生成。秘密鍵はGitHub Actions Secretsへ、公開鍵はフロントJSに埋め込み |
| 送信用Pythonライブラリ | `pywebpush`（依存: `cryptography`, `py-vapid`, `http_ece`）を `requirements.txt` に追加 |
| 購読情報の保存先 | Supabase新テーブル `push_subscriptions`（4章参照） |
| manifest.json / sw.js | `static/` フォルダに配置し `enableStaticServing` で配信 |
| 購読処理 | `sw.js`（push / notificationclick ハンドラ）＋ `app_core.py` 内 `components.html()` 注入JS（権限リクエスト・`pushManager.subscribe()`・Supabaseへの直接POST） |
| 権限リクエストの制約 | iOS 16.4+は**ホーム画面追加後のスタンドアロン起動時のみ** `Notification.requestPermission()` が使用可能。`window.matchMedia('(display-mode: standalone)')` で判定し、非対応時は「ホーム画面に追加してください」と案内するUI分岐が必要 |
| ユーザー操作起点 | 通知許可のリクエストはブラウザのユーザージェスチャー起点が必須。StreamlitのボタンはPython往復（WebSocket）を挟むためジェスチャーの連続性が切れる。`components.html()` 内に生HTMLの `<button>` を置き、その `onclick`内で `requestPermission()` → `subscribe()` まで完結させる必要がある |
| 送信元 | `track_odds.py`（GitHub Actionsの5分おきcronで既に稼働中）から `pywebpush` で直接送信。新規サーバーは不要 |

---

## 3. track_odds.py への組み込み設計

結果確定処理 `_try_finalize_result()` 内、`_insert_result(record)` が成功した直後
（＝ `prediction_results` へのINSERTが確定した瞬間）にフックするのが最も自然。

```python
# track_odds.py の _try_finalize_result() 内、_insert_result(record) の直後

_insert_result(record)

# 保存成功後だけwatchlistを止める。（既存処理）
_deactivate(race_date, jcd, rno)

# --- 追加: 結果確定のプッシュ通知 ---
try:
    from push_notifier import notify_result
    notify_result(SUPABASE_URL, SUPABASE_KEY, record)
except Exception as e:
    print(
        "[PUSH] 通知送信をスキップしました:",
        type(e).__name__, str(e),
        flush=True,
    )
```

新規モジュール `push_notifier.py` の役割:

1. `push_subscriptions` から `active=true` の購読を全件取得（Supabase REST経由、`SUPABASE_SERVICE_ROLE_KEY` 使用）。
2. `record`（`race_date`, `venue`, `race_no`, `trifecta_actual`, `hit_any_ticket`, `profit` 等）から通知本文を組み立て。
   例: 「🚤 桐生10R 結果確定：3-1-5（的中）払戻¥12,400 収支+¥10,400」
3. 各購読エンドポイントへ `pywebpush.webpush(subscription_info, data=json.dumps(payload), vapid_private_key=..., vapid_claims=...)` で送信。
4. 送信結果が404/410（購読切れ）の場合は該当行を `active=false` に更新（無効購読の自動掃除）。
5. 既存の設計方針（Supabase/外部連携の失敗はtrack_odds全体を止めずtry/exceptで握りつぶす。`threads_poster.py` 連携の実装と同じ考え方）を踏襲。

---

## 4. Supabaseテーブル設計案

```sql
create table push_subscriptions (
  id uuid primary key default gen_random_uuid(),
  endpoint text unique not null,
  p256dh text not null,
  auth text not null,
  label text,                          -- 例: "iPhone (自分)" 端末識別用
  user_agent text,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

alter table push_subscriptions enable row level security;

-- フロント（Supabase匿名キー）からは「追加（購読登録）」のみ許可。
-- endpoint に UNIQUE 制約 + upsert で、同一端末の再購読は上書きにする。
create policy "anon can upsert own subscription"
  on push_subscriptions for insert
  to anon
  with check (true);

-- 読み取り・更新・削除は service_role（track_odds.py 側）のみ。
-- anonロールにSELECT/UPDATE/DELETEポリシーを作らないことで、
-- 他人の購読情報の閲覧・改ざん・削除を防ぐ。
```

送信ログを残したい場合は、任意で以下も追加可能（必須ではない。重複送信のデバッグ・可視化用）:

```sql
create table push_notification_log (
  id uuid primary key default gen_random_uuid(),
  race_key text not null,
  subscription_id uuid references push_subscriptions(id),
  sent_at timestamptz not null default now(),
  status_code int
);
```

---

## 5. 難易度評価

**大掛かりではない。** 理由:

- Web Pushの送信はサーバー側（VAPID秘密鍵を持つ環境）から行う必要があるが、`track_odds.py` は
  既にGitHub Actionsの5分おきcronで動く「結果確定サーバー」として存在しており、送信専用の
  新規バックエンドを立てる必要がない。
- 購読情報の保存先も既存のSupabaseプロジェクトをそのまま使える。
- 主なリスクは以下の2点で、いずれも「実装不可能」ではなく「丁寧な実装・実機検証が必要」という
  性質のもの:
  1. Streamlit公式APIにない `<head>` 操作・iframe越境という非公式手法への依存
     （Streamlitアップデート時の動作確認が必要）。
  2. iOS Safari特有の制約（ホーム画面からのスタンドアロン起動必須、ユーザージェスチャー起点必須）
     への対応の細かさ。

---

## 6. 次のステップ（実装する場合）

1. VAPIDキーペア生成、GitHub Actions Secretsへ登録。
2. `push_subscriptions` テーブルをSupabaseに作成。
3. `static/manifest.json` / `static/sw.js` を作成し、`.streamlit/config.toml` で
   `enableStaticServing = true` を設定。
4. `app_core.py` に `components.html()` 経由の manifest link 挿入・SW登録・
   購読ボタンUI（スタンドアロン判定つき）を追加。
5. `push_notifier.py` を新規作成し、`track_odds.py` の結果確定処理にフック。
6. 実機（iPhone Safari、ホーム画面追加済み）で通知の受信・タップ時の起動を検証。

※ 本調査時点では上記1〜6は未実装。
