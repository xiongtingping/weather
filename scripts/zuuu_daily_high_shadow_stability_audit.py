#!/usr/bin/env python3
"""Replay sequential ZUUU shadow candidates and report stability diagnostics."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_raw_history_archive import load_amsc_daily_coverage_snapshot
from scripts.build_trajectory_label_samples import _source_rows
from scripts.evaluate_trajectory_shadow import DEFAULT_BUCKETS
from trajectory_shadow_stability_runtime import build_shadow_stability_audit
from zuuu_daily_high_candidate_runtime import build_zuuu_daily_high_candidate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--archive-dir", default=str(ROOT / "data" / "amsc_raw_history"))
    return parser.parse_args()


def _availability(row: dict) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return str(payload.get("first_seen_at") or row.get("fetched_at") or row.get("parsed_at") or row.get("valid_time") or "")


def main() -> int:
    args = parse_args()
    coverage = load_amsc_daily_coverage_snapshot(
        args.archive_dir,
        event_date=args.event_date,
        station_id=args.station_id,
    )
    if coverage is None:
        print(json.dumps({"ok": False, "reason": "persisted_coverage_snapshot_missing", "event_date": args.event_date}, ensure_ascii=False))
        return 1
    hashes = {str(value) for value in coverage.get("raw_object_sha256_list") or [] if str(value)}
    conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        source_rows = _source_rows(conn, event_date=args.event_date, station_id=args.station_id, allowed_hashes=hashes)
    finally:
        conn.close()
    cutoffs = sorted({value for value in (_availability(row) for row in source_rows) if value})
    candidates = [
        build_zuuu_daily_high_candidate(
            buckets=DEFAULT_BUCKETS,
            source_observations=source_rows,
            as_of=cutoff,
            event_date=args.event_date,
        )
        for cutoff in cutoffs
    ]
    report = build_shadow_stability_audit(candidates)
    print(
        json.dumps(
            {
                "event_date": args.event_date,
                "coverage_manifest_sha256": coverage.get("coverage_sha256"),
                "coverage_snapshot_generated_at": coverage.get("snapshot_generated_at"),
                "source_row_count": len(source_rows),
                "candidate_as_of_count": len(candidates),
                "stability": report,
                "guards": {
                    "writes_archive_db": False,
                    "writes_facts_sqlite": False,
                    "writes_posterior": False,
                    "places_orders": False,
                    "changes_parameters": False,
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
