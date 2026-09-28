#!/usr/bin/env bash
# 過去データ収集の途中保存。データ用リポジトリ（非公開。BOATRACE_DATA_DIR に
# 取り出したもの）の data/history/ だけをコミットしてpushする。
# history_full.csv は日次収集（Collect History）が書くので触らない。
# 収集中のプロセスが書き足した未ステージの変更があっても pull できるよう --autostash を付ける。
set -u
cd "${BOATRACE_DATA_DIR:?BOATRACE_DATA_DIR が未設定です}" || exit 0
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add data/history
if ! git diff --staged --quiet; then
  git commit -q -m "collect history backfill data"
elif [ -z "$(git log --oneline '@{u}..HEAD' 2>/dev/null)" ]; then
  echo "[COMMIT] 変更なし"
  exit 0
fi
# 前回pushに失敗したコミットが残っていれば、新しい変更が無くてもここでpushする
for i in 1 2 3 4 5; do
  if git pull -q --rebase --autostash && git push -q; then
    echo "[COMMIT] push完了"
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  sleep $((i * 10))
done
echo "[COMMIT] pushに失敗（次回のコミットで再試行）"
exit 0
