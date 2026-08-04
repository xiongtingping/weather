from __future__ import annotations

from scripts.audit_p1_hourly_curve_receipts_45station import (
    classify_hourly_curve_receipt,
)


def test_late_archive_curve_never_becomes_strict_asof_p1_evidence() -> None:
    state, blockers = classify_hourly_curve_receipt(
        family_count=3,
        minimum_point_count=8,
        available_at_utc="2026-08-01T16:21:15Z",
        station_day_end_utc="2026-07-28T04:00:00Z",
        probability_root_count=1,
        root_curve_bound_count=0,
    )

    assert state == "ARCHIVE_CURVE_PRESENT_UNBINDABLE"
    assert blockers == [
        "archive_query_after_station_day_not_strict_asof",
        "materialized_p0_root_curve_binding_missing",
    ]


def test_forward_receipt_still_requires_explicit_root_binding() -> None:
    state, blockers = classify_hourly_curve_receipt(
        family_count=3,
        minimum_point_count=8,
        available_at_utc="2026-07-27T01:00:00Z",
        station_day_end_utc="2026-07-28T04:00:00Z",
        probability_root_count=1,
        root_curve_bound_count=1,
    )

    assert state == "RECEIPT_REQUIRES_FORWARD_ARRIVAL_AUTHORITY"
    assert blockers == []
