"""Bounded read-only adapter from resident official reports to P1 facts."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import ContractError
from .correction_review import assert_station_day_corrections_resolved
from .settlement_time import (
    registry_settlement_timezone,
    settlement_day_window,
)


MAX_FORMAL_OBSERVATIONS_PER_SCOPE = 64


def _utc(value: Any, *, reason: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(reason)
    return parsed.astimezone(timezone.utc)


def load_resident_formal_observations(
    conn: sqlite3.Connection,
    *,
    registry_rows: Sequence[Mapping[str, Any]],
    station_id: str,
    event_date: str,
    as_of_utc: str,
) -> list[dict[str, Any]]:
    """Load one station/day's official hourly rows with strict route/time gates."""

    station = str(station_id or "").strip().upper()
    decision_at = _utc(
        as_of_utc,
        reason="resident_p1_as_of_invalid",
    )
    scoped_registry = [
        row
        for row in registry_rows
        if str(row.get("station_id") or "").strip().upper() == station
        and str(row.get("event_date") or "") == event_date
    ]
    timezone_name = registry_settlement_timezone(
        scoped_registry,
        station_id=station,
        event_date=event_date,
    )
    lanes = {
        str(row.get("source_lane") or "").strip().lower()
        for row in scoped_registry
    }
    if len(lanes) != 1 or next(iter(lanes)) not in {"amsc", "awc"}:
        raise ContractError("resident_p1_source_lane_not_unique")
    source_lane = next(iter(lanes))
    window = settlement_day_window(event_date, timezone_name)
    start = _utc(
        window["target_day_start_utc"],
        reason="resident_p1_window_start_invalid",
    )
    end = _utc(
        window["target_day_end_utc"],
        reason="resident_p1_window_end_invalid",
    )
    try:
        correction_review = assert_station_day_corrections_resolved(
            conn,
            source_lane=source_lane,
            station_id=station,
            event_date=event_date,
            window_start_utc=window["target_day_start_utc"],
            window_end_utc=window["target_day_end_utc"],
            as_of_utc=as_of_utc,
        )
    except ContractError as exc:
        raise ContractError(
            f"resident_p1_correction_requires_manual_review:{exc}"
        ) from exc
    rows = conn.execute(
        "SELECT observation_id,source_lane,station_id,report_type,valid_at,"
        "reported_temperature_c,raw_sha256,canonical_committed_at,"
        "correction_suspected "
        "FROM reported_trajectory_observations "
        "WHERE station_id=? AND source_lane=? "
        "AND julianday(valid_at)>=julianday(?) "
        "AND julianday(valid_at)<julianday(?) "
        "AND julianday(canonical_committed_at)<=julianday(?) "
        "ORDER BY valid_at,canonical_committed_at,observation_id "
        "LIMIT ?",
        (
            station,
            source_lane,
            window["target_day_start_utc"],
            window["target_day_end_utc"],
            as_of_utc,
            MAX_FORMAL_OBSERVATIONS_PER_SCOPE + 1,
        ),
    ).fetchall()
    if len(rows) > MAX_FORMAL_OBSERVATIONS_PER_SCOPE:
        raise ContractError("resident_p1_observation_scope_cap_exceeded")
    result: list[dict[str, Any]] = []
    for row in rows:
        valid_at = _utc(
            row[4],
            reason="resident_p1_valid_at_invalid",
        )
        committed_at = _utc(
            row[7],
            reason="resident_p1_committed_at_invalid",
        )
        if not (start <= valid_at < end):
            raise ContractError("resident_p1_observation_outside_window")
        if committed_at > decision_at or valid_at > committed_at:
            raise ContractError("resident_p1_observation_not_strict_asof")
        report_type = str(row[3] or "").strip().upper()
        if report_type not in {"METAR", "SPECI"}:
            raise ContractError("resident_p1_report_type_invalid")
        # A correction/revision halts the whole station-day even when that
        # particular report is not eligible for formal hourly truth.  Checking
        # the minute first would silently discard a COR SPECI and let later
        # probability/decision writes continue through a mandatory manual-
        # review boundary.
        if int(row[8] or 0) != 0:
            # The scope-level gate above proved an append-only clear receipt
            # for this exact non-hourly correction.  It remains reference-only
            # and can never become a formal P1 observation.
            continue
        # Current truth constitution admits only the official :00 report.
        if valid_at.minute != 0 or valid_at.second != 0:
            continue
        raw_sha = str(row[6] or "").strip().lower()
        if (
            len(raw_sha) != 64
            or any(char not in "0123456789abcdef" for char in raw_sha)
        ):
            raise ContractError("resident_p1_raw_sha256_invalid")
        result.append({
            "observation_id": str(row[0] or ""),
            "station_id": station,
            "event_date_local": event_date,
            "reported_temperature_c": float(row[5]),
            "valid_at_utc": valid_at.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "canonical_committed_at_utc": committed_at.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "raw_sha256": raw_sha,
            "source_lane": source_lane,
            "report_type": report_type,
            "evidence_role": "FORMAL",
            "qc_status": "ACCEPTED",
            "correction_suspected": False,
            "settlement_timezone": timezone_name,
            "correction_review_ids": list(
                correction_review.get("review_ids") or []
            ),
        })
    return result


__all__ = [
    "MAX_FORMAL_OBSERVATIONS_PER_SCOPE",
    "load_resident_formal_observations",
]
