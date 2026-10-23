set -x
cd docs/data_investigation/probe
TZ=Asia/Tokyo date
commit_mid() {
  ( cd "$GITHUB_WORKSPACE/_data" && git config user.name "github-actions[bot]" && git config user.email "github-actions[bot]@users.noreply.github.com" \
    && git add reports/orig_exhibition_probe && git commit -q -m "probe: 途中結果（一時）" && git pull -q --rebase && git push -q ) || true
}
pip install -q playwright && python -m playwright install --with-deps chromium > /dev/null
python probe_orig2.py monitor 2300 > monitor.log 2>&1 &
MON=$!
python probe_orig2.py boatcast
commit_mid
python probe_orig2.py render
commit_mid
while kill -0 $MON 2>/dev/null; do sleep 1200; commit_mid; done
grep MON monitor.log | tail -40
