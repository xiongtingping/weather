#!/usr/bin/env python3
"""只用现役模型链重建：事件日 2026-09-26..2026-10-02，构建器与 build.py 相同（H=60..37），
新文件 dataset_current.jsonl.gz；之后由 replay.py --min-version 19 只留 P0 v19+ 的行。"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bayes_kelly_replay_20260924"))
import build_dataset as B  # noqa: E402
B.FIRST_DAY, B.LAST_DAY = "2026-09-26", "2026-10-02"
B.GRID_H = (60, 54, 48, 44, 40, 37)
if __name__ == "__main__":
    B.main()
