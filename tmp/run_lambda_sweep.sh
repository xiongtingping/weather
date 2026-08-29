#!/bin/bash
set -u
cd /Users/xiong/weather
echo "════ 肩腿 λ 扫描（价值 λ 固定 0.53，天花板开）"
for v in 0.20 0.30 0.40 0.53 0.70 exempt; do
  bash tmp/sweep_cell.sh "  肩腿λ=$v" PAPER_LEDGER_MAKER_SHOULDER_LAMBDA="$v"
done
echo "════ 价值 λ 扫描（肩腿 λ 固定 0.40，天花板开）"
for v in 0.35 0.53 0.70 0.85; do
  bash tmp/sweep_cell.sh "  价值λ=$v" PAPER_LEDGER_MAKER_VALUE_LAMBDA="$v"
done
echo "════ 完成"
