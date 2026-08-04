"""Canonical source precedence for the target evidence lane."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping


# The generic helper still understands the historical two-lane vocabulary so
# offline compatibility audits can parse old receipts.  The current target
# station lane map binds all 45 formal stations to ``awc``; AMSC is therefore
# rejected as a cross-lane fallback by callers that pass ``expected_lane=awc``.
LANES = frozenset({"amsc", "awc"})
OFFICIAL_REPORT_TYPES = frozenset({"METAR", "SPECI"})
AMSC_HOUR_PRIMARY = "amsc_hourly_metar_for_amsc_station"
AMSC_HALF_HOUR_REFERENCE = "amsc_half_hour_reference_only"
AWC_HOUR_REFERENCE_FOR_AMSC = "awc_hourly_reference_only_for_amsc_station"
AMSC_METAR_TIMING_INVALID = "amsc_metar_timing_invalid"
AWC_METAR_TIMING_INVALID = "awc_metar_timing_invalid"
SOURCE_LANE_CROSS_FALLBACK_FORBIDDEN = "source_lane_cross_fallback_forbidden"


def normalize_station_lane_map(value: Mapping[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for station, lane in (value or {}).items():
        station_id = str(station or "").strip().upper()
        source_lane = str(lane or "").strip().lower()
        if not station_id or source_lane not in LANES:
            raise ValueError("station_lane_map_invalid")
        previous = result.setdefault(station_id, source_lane)
        if previous != source_lane:
            raise ValueError("station_lane_map_conflict")
    return result


def reference_only_reason(
    *,
    source_lane: str,
    report_type: str,
    valid_at: datetime,
    expected_lane: str = "",
) -> str:
    lane = str(source_lane or "").strip().lower()
    kind = str(report_type or "").strip().upper()
    expected = str(expected_lane or "").strip().lower()
    # The AMSC half-hour stream is reference-only only on an AMSC-owned
    # station.  On an AWC-owned station it is an illegal cross-lane input,
    # not a harmless reference row; callers can then fail closed on the
    # station-lane ownership check.
    if (
        (not expected or expected == "amsc")
        and lane == "amsc"
        and kind == "METAR"
        and valid_at.minute == 30
        and valid_at.second == 0
    ):
        return AMSC_HALF_HOUR_REFERENCE
    if (
        expected == "amsc"
        and lane == "awc"
        and kind in OFFICIAL_REPORT_TYPES
        and valid_at.minute == 0
        and valid_at.second == 0
    ):
        return AWC_HOUR_REFERENCE_FOR_AMSC
    return ""


def source_policy_reason(
    *,
    source_lane: str,
    report_type: str,
    valid_at: datetime,
    expected_lane: str = "",
) -> str:
    """Return the single canonical role/timing decision for one report.

    An empty string is a formal observation.  Reference-only reasons are
    returned unchanged so callers can retain the evidence without admitting
    it to truth.  Every other non-empty result is a fail-closed blocker.

    ``valid_at`` is the report's official validity time, not receive time.
    Routine METARs from either permitted lane are formal only at ``:00:00``.
    AMSC ``:30:00`` METAR remains a reference lane; effective SPECI is valid
    at any minute/second.  AWC must not turn a provider-side routine cadence
    into formal truth outside the unified hourly contract.
    """

    lane = str(source_lane or "").strip().lower()
    kind = str(report_type or "").strip().upper()
    expected = str(expected_lane or "").strip().lower()
    if lane not in LANES:
        return "source_lane_invalid"
    if kind not in OFFICIAL_REPORT_TYPES:
        return "report_type_invalid"
    if expected and expected not in LANES:
        return "station_lane_invalid"

    # Cross-lane evidence is never formal.  The one explicitly permitted
    # cross-lane observation is AWC's :00:00 report for an AMSC-owned station,
    # retained only as an independent reference check.
    if expected and lane != expected:
        reference = reference_only_reason(
            source_lane=lane,
            report_type=kind,
            valid_at=valid_at,
            expected_lane=expected,
        )
        return reference or SOURCE_LANE_CROSS_FALLBACK_FORBIDDEN

    # With no bound station map, apply the source's own semantics.  Formal
    # callers that have a registry still pass expected_lane and therefore
    # receive the cross-lane guard above.
    owned_lane = expected or lane
    if owned_lane == "amsc" and lane == "amsc" and kind == "METAR":
        if valid_at.minute == 30 and valid_at.second == 0:
            return AMSC_HALF_HOUR_REFERENCE
        if valid_at.minute == 0 and valid_at.second == 0:
            return ""
        return AMSC_METAR_TIMING_INVALID
    if owned_lane == "awc" and lane == "awc" and kind == "METAR":
        if valid_at.minute == 0 and valid_at.second == 0:
            return ""
        return AWC_METAR_TIMING_INVALID
    # SPECI is an effective special report and follows its official time.
    return ""


__all__ = [
    "AMSC_HOUR_PRIMARY",
    "AMSC_HALF_HOUR_REFERENCE",
    "AMSC_METAR_TIMING_INVALID",
    "AWC_METAR_TIMING_INVALID",
    "AWC_HOUR_REFERENCE_FOR_AMSC",
    "OFFICIAL_REPORT_TYPES",
    "LANES",
    "normalize_station_lane_map",
    "reference_only_reason",
    "SOURCE_LANE_CROSS_FALLBACK_FORBIDDEN",
    "source_policy_reason",
]
