#!/bin/bash
# 固定したソースで、回帰テスト → 300 回の反復試験を最初から行う。どれか1つでも失敗したら非 0 で終わり、DONE を書かない。
set -euo pipefail
cd "$(dirname "$0")/../.."
export QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=alsa
OUT=${OUT:-stress_logs/final}
rm -rf "$OUT"; mkdir -p "$OUT"
SRC=$(tools/stress/source_id.sh)
echo "$SRC" > "$OUT/source_id.txt"
python3 -m unittest discover -s tests > "$OUT/regression.log" 2>&1 || { echo "回帰テストに失敗"; exit 1; }
for c in storage scan groups trash gui; do
  python3 tools/stress/run_stress.py --category $c --count 60 --base-seed "${BASE_SEED:-1}" \
      --out "$OUT/stress_300.jsonl" > "$OUT/$c.log" 2>&1 || { echo "反復試験 $c に失敗"; exit 1; }
done
[ "$(tools/stress/source_id.sh)" = "$SRC" ] || { echo "実行中にソースが変わった"; exit 1; }
[ "$(wc -l < "$OUT/stress_300.jsonl")" -eq 300 ] || { echo "試行の件数が 300 ではない"; exit 1; }
echo finished > "$OUT/DONE"
