set -x
cd docs/data_investigation/probe
TZ=Asia/Tokyo date
python probe_orig_exhibition.py today
d=$(TZ=Asia/Tokyo date +%Y%m%d)
cat > /tmp/bc.json <<J
{"bc": ["https://race.boatcast.jp/", "https://race.boatcast.jp/txt/01/bc_oriten_${d}_01_01.txt", "https://race.boatcast.jp/txt/20/bc_oriten_${d}_20_01.txt", "https://race.boatcast.jp/txt/24/bc_oriten_${d}_24_01.txt"]}
J
python probe_orig_exhibition.py fetch /tmp/bc.json
python probe_orig_exhibition.py discover
