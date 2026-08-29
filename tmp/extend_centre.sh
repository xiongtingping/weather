#!/bin/bash
set -u
cd /Users/xiong/weather
# 等主扫描让出写锁，避免两边互相拖垮（今晚已因此废掉两格）
while pgrep -f "resweep_through|sweep_cell" >/dev/null; do sleep 20; done
echo "════ 中心 λ 补扫（定形：1.0 是不是真的局部最低）"
for v in 1.2 2.0 3.0; do
  bash tmp/sweep_cell.sh "  中心λ=$v" PAPER_LEDGER_MAKER_CENTRE_LAMBDA="$v"
done
echo "════ 补扫完成"
