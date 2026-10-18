#!/usr/bin/env python3
"""Record the deterministic centre's final chain beside F (charter 267 补记九, round 77).

Reads the SSOT ``mode=ro``; appends to ``data/runtime/listing_forecast_f_mos_ledger_v1.jsonl``
one line per station-day whose listing instant passed within the last 24 hours
and that has no line yet.  Run after the deterministic capture each sweep.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import listing_forecast_f_mos as M  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60.0)
    try:
        receipt = M.record(conn)
    finally:
        conn.close()
    print(f"影子中心：到期事件日 {receipt['due_days']}，写入 {receipt['written']}，跳过 {receipt['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
