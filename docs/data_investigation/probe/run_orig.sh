set -x
cd docs/data_investigation/probe
TZ=Asia/Tokyo date
python probe_orig_exhibition.py fetch targets3.json
python probe_orig2.py jsgrep
python probe_orig2.py bcpast
