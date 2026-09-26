#!/usr/bin/env python3
"""Extend the bayes_kelly_replay_20260924 dataset to every settled event day.

Reuses that study's builder unchanged (same grid, same stage priority, same
book-not-older-than-belief rule), only widening its event-date window to
2026-08-15..2026-09-25.  Resumable: station-days already in OUT are skipped, so
the 30 days already built are kept and only the missing ones are read.  Each
pass stops after 540 s (the builder's own bound); run until remaining is 0.

    python3 build_full.py dataset_full.jsonl.gz
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bayes_kelly_replay_20260924"))
import build_dataset as B  # noqa: E402

B.FIRST_DAY, B.LAST_DAY = "2026-08-15", "2026-09-25"
if __name__ == "__main__":
    B.main()
