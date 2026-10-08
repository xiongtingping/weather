#!/bin/zsh
# One bounded foreground batch: both extractors side by side, each under its
# own deadline and rate budget; waits for both, then reports progress and the
# age of production's WS manifest (the book must stay fresh while we fetch).
cd "$(dirname "$0")"
PY=../../../.venv-sat/bin/python
MIN=${1:-7}
# Production first: no batch while the market book is stale or memory is
# short (2026-10-08 04:46-04:50Z the WS publisher stalled 257 s with swap at
# 11.1/12.3 GB; our 10 worker processes were part of that pressure).
AGE=$(python3 -c "import os,time;print(int(time.time()-os.path.getmtime('../../../data/bayes_basket_target_official_ws_manifest.json')))")
FREE=$(memory_pressure | awk -F': ' '/free percentage/{gsub("%","",$2);print $2}')
if [ "$AGE" -gt 60 ] || [ "$FREE" -lt 25 ]; then echo "skip: ws_manifest_age=${AGE}s memory_free=${FREE}%"; exit 0; fi
$PY goes_extract.py --end 2026-10-07 --max-minutes $MIN --workers 2 > .goes_chunk.log 2>&1 &
$PY himawari_extract.py --end 2026-10-07 --max-minutes $MIN --workers 3 2>&1 | tail -1
wait
grep -c '"day"' .goes_chunk.log | xargs echo goes_days_this_chunk:
grep -h -E 'error|ERROR' .goes_chunk.log | head -2
echo goes_days_total: $(ls goes/noaa-goes19/ABI-L2-CCLF 2>/dev/null | wc -l) east, $(ls goes/noaa-goes18/ABI-L2-CCLF 2>/dev/null | wc -l) west
echo himawari_hours_total: $(find himawari -name '*.jsonl.gz' | wc -l)
python3 -c "import os,time;print('ws_manifest_age_s', round(time.time()-os.path.getmtime('../../../data/bayes_basket_target_official_ws_manifest.json'),1))"
