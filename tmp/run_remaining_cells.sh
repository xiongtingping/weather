#!/bin/bash
set -u
cd /Users/xiong/weather
echo "════ 补跑（含作废的两格）"
bash tmp/sweep_cell.sh "肩腿λ=0.20(补)" PAPER_LEDGER_MAKER_SHOULDER_LAMBDA=0.20
bash tmp/sweep_cell.sh "价值λ=0.53(基线·补)" PAPER_LEDGER_MAKER_VALUE_LAMBDA=0.53
bash tmp/sweep_cell.sh "价值λ=0.70" PAPER_LEDGER_MAKER_VALUE_LAMBDA=0.70
bash tmp/sweep_cell.sh "价值λ=0.85" PAPER_LEDGER_MAKER_VALUE_LAMBDA=0.85
echo "════ 全部完成"
