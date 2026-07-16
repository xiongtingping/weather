#!/usr/bin/env python3
"""Read-only integrity and schedule-coverage audit for AMSC H backfill."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_historical_backfill_archive import (  # noqa: E402
    audit_amsc_historical_backfill_archive,
    build_amsc_historical_daily_coverage,
)
from amsc_historical_physical_x_samples import build_amsc_historical_physical_x_samples  # noqa: E402


def _compact_coverage(value: dict | None) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {
        "schema": value.get("schema"),
        "ok": value.get("ok"),
        "state": value.get("state"),
        "event_date": value.get("event_date"),
        "station_id": value.get("station_id"),
        "reporting_schedule_id": value.get("reporting_schedule_id"),
        "expected_regular_metar_slot_count": value.get("expected_regular_metar_slot_count"),
        "returned_regular_metar_slot_count": value.get("returned_regular_metar_slot_count"),
        "missing_regular_metar_slot_count": len(value.get("missing_regular_metar_slots_utc") or []),
        "unexpected_regular_metar_slot_count": len(value.get("unexpected_regular_metar_slots_utc") or []),
        "correction_slot_count": len(value.get("correction_slots_utc") or []),
        "offline_physical_sequence_eligible": value.get("offline_physical_sequence_eligible"),
        "strict_asof_or_live_replay_eligible": value.get("strict_asof_or_live_replay_eligible"),
        "strict_asof_or_live_replay_na_reason": value.get("strict_asof_or_live_replay_na_reason"),
        "archive_integrity": value.get("archive_integrity"),
    }


def _compact_sample_manifest(value: dict | None) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {
        "schema": value.get("schema"),
        "station_id": value.get("station_id"),
        "event_date": value.get("event_date"),
        "reporting_schedule_id": value.get("reporting_schedule_id"),
        "sample_count": len(value.get("sample_ids") or []),
        "physical_x_raw_object_count": len(value.get("physical_x_raw_sha256_list") or []),
        "manifest_sha256": value.get("manifest_sha256"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, default=ROOT / "data" / "shadow" / "amsc_historical_backfill")
    parser.add_argument("--event-date", default="", help="optional Asia/Shanghai physical date")
    args = parser.parse_args(argv)

    archive = audit_amsc_historical_backfill_archive(args.archive_dir)
    coverage = build_amsc_historical_daily_coverage(args.archive_dir, event_date=args.event_date) if args.event_date else None
    samples = build_amsc_historical_physical_x_samples(args.archive_dir, event_date=args.event_date) if args.event_date else None
    report = {
        "archive": archive,
        "coverage": _compact_coverage(coverage),
        "auxiliary_physical_x_samples": (
            {
                "schema": samples.get("schema"),
                "ok": samples.get("ok"),
                "state": samples.get("state"),
                "sample_count": samples.get("sample_count"),
                "block_reasons": samples.get("block_reasons") or [],
                "manifest": _compact_sample_manifest(samples.get("manifest")),
                "boundary": samples.get("boundary"),
            }
            if samples is not None else None
        ),
        "boundary": {
            "historical_data_class": "H",
            "strict_asof_oos_are_offline_promotion_only": True,
            "creates_canonical_t1": False,
            "creates_p10_arrival_evidence": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if archive.get("ok") is True and (coverage is None or coverage.get("ok") is True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
