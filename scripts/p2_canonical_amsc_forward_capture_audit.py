#!/usr/bin/env python3
"""Read-only audit of current P2 primary AMSC canonical capture."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p2_canonical_amsc_forward_capture_audit import audit_p2_canonical_amsc_forward_capture  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--raw-archive-dir", default=str(ROOT / "data" / "amsc_raw_history"))
    parser.add_argument(
        "--event-date",
        default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
    )
    parser.add_argument("--station-id", default="ZUUU")
    args = parser.parse_args(argv)
    try:
        conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
        try:
            result = audit_p2_canonical_amsc_forward_capture(
                conn,
                event_date=args.event_date,
                station_id=args.station_id,
                raw_archive_dir=args.raw_archive_dir,
            )
        finally:
            conn.close()
    except Exception as exc:
        result = {
            "schema": "p2_canonical_amsc_forward_capture_audit_v1",
            "ok": False,
            "state": "blocked",
            "blockers": [f"p2_canonical_capture_audit_failed:{exc.__class__.__name__}"],
            "read_only": True,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
