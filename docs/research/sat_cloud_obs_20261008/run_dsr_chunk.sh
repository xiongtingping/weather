#!/bin/zsh
# One bounded GOES DSR (surface shortwave) batch, under the same production
# guard as run_chunk.sh: no batch while the market book is stale or memory
# is short.
cd "$(dirname "$0")"
MIN=${1:-5}
AGE=$(python3 -c "import os,time;print(int(time.time()-os.path.getmtime('../../../data/bayes_basket_target_official_ws_manifest.json')))")
FREE=$(memory_pressure | awk -F': ' '/free percentage/{gsub("%","",$2);print $2}')
if [ "$AGE" -gt 60 ] || [ "$FREE" -lt 25 ]; then echo "skip: ws_manifest_age=${AGE}s memory_free=${FREE}%"; exit 0; fi
../../../.venv-sat/bin/python goes_extract.py --products ABI-L2-DSRF --end 2026-10-07 --max-minutes $MIN --workers ${WORKERS:-3} 2>&1 | tail -4
echo dsr_days: $(ls goes/noaa-goes19/ABI-L2-DSRF 2>/dev/null | wc -l) east, $(ls goes/noaa-goes18/ABI-L2-DSRF 2>/dev/null | wc -l) west
python3 -c "import os,time;print('ws_manifest_age_s', round(time.time()-os.path.getmtime('../../../data/bayes_basket_target_official_ws_manifest.json'),1))"
