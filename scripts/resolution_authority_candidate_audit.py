#!/usr/bin/env python3
"""Read-only G1 candidate report; never freezes final source authority."""

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

from resolution_authority_candidate_runtime import build_resolution_authority_candidate_audit  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--archive-dir", default=str(ROOT / "data" / "amsc_raw_history"))
    parser.add_argument("--event-date", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--slug", default="")
    args = parser.parse_args(argv)
    try:
        conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
        try:
            result = build_resolution_authority_candidate_audit(
                conn,
                event_date=args.event_date,
                station_id=args.station_id,
                slug=args.slug,
                archive_dir=args.archive_dir,
            )
        finally:
            conn.close()
    except Exception as exc:
        result = {
            "schema": "resolution_authority_candidate_audit_v1",
            "ok": False,
            "state": "blocked",
            "blockers": [f"authority_candidate_audit_failed:{type(exc).__name__}"],
            "read_only": True,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") == "authority_decision_required" else 1


if __name__ == "__main__":
    raise SystemExit(main())
