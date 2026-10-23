set -x
cd docs/data_investigation/probe
TZ=Asia/Tokyo date
pip install -q playwright && python -m playwright install --with-deps chromium > /dev/null
python probe_orig2.py monitor 2300 > monitor.log 2>&1 &
MON=$!
python probe_orig2.py boatcast
python probe_orig2.py render
wait $MON
grep MON monitor.log | tail -40
