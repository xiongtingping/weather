"""Bounded read-only adapter from resident official reports to P1 facts."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import ContractError
from .correction_review import assert_station_day_corrections_resolved
from .source_policy import source_policy_reason
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
    """Load one station/day's official AWC rows with strict route/time gates."""

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
    # The current production constitution has exactly one formal lane.  Keep
    # this check local to the P1 reader as a second fail-closed boundary, so a
    # stale registry snapshot cannot revive retired AMSC observations.
    if lanes != {"awc"}:
        raise ContractError("resident_p1_source_lane_not_awc")
    source_lane = "awc"
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
        "reported_temperature_c,raw_sha256,raw_text,canonical_committed_at,"
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
            row[8],
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
        if int(row[9] or 0) != 0:
            # The scope-level gate above proved an append-only clear receipt
            # for this exact non-hourly correction.  It remains reference-only
            # and can never become a formal P1 observation.
            continue
        # P1 must read the same physical evidence settlement will read.
        # Every routine report at the provider's own official minute is
        # formal -- station cadences range from :00 through :20/:50 to
        # :51-:58 -- and settlement_truth admits the identical set, so the
        # LFPB 2026-07-31 divergence (P1 reading sub-hourly rows settlement
        # then rejected) cannot recur: the two sides now agree by
        # construction.  Any surviving policy reason is a fail-closed
        # blocker, never a cadence artifact.
        policy_reason = source_policy_reason(
            source_lane=source_lane.lower(),
            report_type=report_type,
            valid_at=valid_at,
            expected_lane="awc",
        )
        if policy_reason:
            raise ContractError(
                "resident_p1_observation_source_policy_invalid:"
                + policy_reason
            )
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
            "raw_text": str(row[7] or ""),
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


def attach_truth_observation_revisions(
    conn: sqlite3.Connection,
    observations: Sequence[Mapping[str, Any]],
    *,
    station_id: str,
    as_of_utc: str,
) -> list[dict[str, Any]]:
    """Name the canonical revision fact behind each observation.

    P1's envelope has to carry it -- the truth ledger is keyed on the revision,
    not on the raw METAR object -- but it cannot be read from the same place as
    the observations.  ``reported_trajectory_observations`` lives in the
    read-only canonical archive while the revision facts live in the target
    SSOT, so this takes the SSOT connection and is applied after loading.

    Strict as-of: the newest revision whose canonical commit is at or before
    the decision clock wins.  A later revision exists for corrected reports and
    must not be visible to a decision taken before it landed.

    An observation with no revision fact is returned unchanged rather than
    rejected: it simply cannot close a P1 packet, and failing here would deny
    the whole station-day over one report the canonical ingress has not caught
    up with.
    """

    rows = list(observations)
    if not rows:
        return []
    wanted = {
        str(row.get("observation_id") or "").strip()
        for row in rows
        if str(row.get("observation_id") or "").strip()
    }
    if not wanted:
        return [dict(row) for row in rows]
    by_observation: dict[str, tuple[int, str]] = {}
    placeholders = ",".join("?" for _ in sorted(wanted))
    try:
        found = conn.execute(
            "SELECT json_extract(fact_json,'$.observation_id'),"
            "  json_extract(fact_json,'$.revision_no'),"
            "  json_extract(fact_json,'$.canonical_committed_at_utc'),"
            "  content_sha256 "
            "FROM canonical_ssot_fact "
            "WHERE fact_type='truth_observation_revision' "
            "  AND json_extract(fact_json,'$.station_id')=? "
            f"  AND json_extract(fact_json,'$.observation_id') IN ({placeholders})",
            (str(station_id).strip().upper(), *sorted(wanted)),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ContractError(
            f"resident_p1_truth_revision_lookup_failed:{exc}"
        ) from exc
    for observation_id, revision_no, committed_at, content_sha in found:
        key = str(observation_id or "")
        committed = str(committed_at or "")
        if not key or not committed or committed > str(as_of_utc):
            continue
        candidate = (int(revision_no or 0), str(content_sha or ""))
        current = by_observation.get(key)
        if current is None or candidate[0] > current[0]:
            by_observation[key] = candidate
    result: list[dict[str, Any]] = []
    for row in rows:
        enriched = dict(row)
        found_revision = by_observation.get(
            str(row.get("observation_id") or "").strip()
        )
        if found_revision is not None:
            enriched["truth_observation_revision_content_sha256"] = (
                found_revision[1]
            )
        result.append(enriched)
    return result


__all__ = [
    "MAX_FORMAL_OBSERVATIONS_PER_SCOPE",
    "attach_truth_observation_revisions",
    "load_resident_formal_observations",
]
