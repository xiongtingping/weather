#!/bin/bash
set -u
cd /Users/xiong/weather
echo "════ 诚实基准 (through)"
bash tmp/sweep_cell.sh "基准 through" PAPER_LEDGER_FILL_MODEL=through
echo "════ 天花板还值多少"
bash tmp/sweep_cell.sh "天花板 关" PAPER_LEDGER_METAR_CEILING=0
echo "════ 肩腿 λ"
for v in 0.20 0.30 0.53 0.70 exempt; do
  bash tmp/sweep_cell.sh "  肩腿λ=$v" PAPER_LEDGER_MAKER_SHOULDER_LAMBDA="$v"
done
echo "════ 价值 λ"
for v in 0.35 0.70 0.85; do
  bash tmp/sweep_cell.sh "  价值λ=$v" PAPER_LEDGER_MAKER_VALUE_LAMBDA="$v"
done
echo "════ 中心 λ"
for v in 0.8 1.5; do
  bash tmp/sweep_cell.sh "  中心λ=$v" PAPER_LEDGER_MAKER_CENTRE_LAMBDA="$v"
done
echo "════ 中心/宽度层 按 stage"
for v in P0 P0-M P1; do
  bash tmp/sweep_cell.sh "  中心宽度=$v" PAPER_LEDGER_CENTER_WIDTH="$v"
done
echo "════ 全部完成"
