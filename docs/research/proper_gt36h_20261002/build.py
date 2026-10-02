#!/usr/bin/env python3
"""Proper 下注线 >36h 回放数据集：bayes_kelly_replay_20260924 构建器原样，
只把网格换成距结算 >36h 的六个时点、窗口扩到 2026-08-15..2026-10-01。
可续跑，每轮 540 s。

    python3 build.py dataset.jsonl.gz
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bayes_kelly_replay_20260924"))
import build_dataset as B  # noqa: E402

B.FIRST_DAY, B.LAST_DAY = "2026-08-15", "2026-10-01"
B.GRID_H = (60, 54, 48, 44, 40, 37)
if __name__ == "__main__":
    B.main()
