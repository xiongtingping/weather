"""Bounded read-only adapter from resident official reports to P1 facts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import (
    MAX_REPORT_TRANSMISSION_LEAD_SECONDS,
    ContractError,
)
from .correction_review import assert_station_day_corrections_resolved
from .source_policy import source_policy_reason
from .settlement_time import (
    registry_settlement_timezone,
    settlement_day_window,
)


# A resource bound on one station-day's read, not an evidence policy.  64
# was sized for hourly reporters and fail-closed LTAC on 2026-08-28 at 66
# legitimate formal rows: a half-hourly cadence is 48 routine METARs before
# the first SPECI, and a stormy day adds a burst of those.  96 holds
# half-hourly plus a severe SPECI day plus the bounded ASOS substitutes
# (at most one per missed slot) with headroom, while still refusing a
# runaway ingest.
MAX_FORMAL_OBSERVATIONS_PER_SCOPE = 96
_MAX_TRANSMISSION_LEAD = timedelta(
    seconds=MAX_REPORT_TRANSMISSION_LEAD_SECONDS
)
# ASOS carrier rows beside the formal AWC lane.  The lane names its report
# type exactly; a row claiming any other pairing is not one this reader may
# admit.  Since the 2026-08-29 soft-in-hard-out ruling every row in these
# lanes is SOFT_PATH evidence: it feeds the path state and nothing else --
# measured against settlement (which follows the METAR series), the carrier
# maxima overshoot it on a third of days, so they may never touch the
# running high.  Rows are minted only by the slot materializer behind the
# station's OOS_READY artifact, and carry its identity prefix as a second
# boundary ("asos_p1_" from the retired formal-substitute era, "asos_soft_"
# for the intra-hour soft carrier).
P1_ASOS_SUBSTITUTE_LANE_REPORT_TYPES = {
    "asos_1min": "ASOS_1MIN",
    "madis_hfmetar": "ASOS_HF",
}
_P1_ASOS_CARRIER_ID_PREFIXES = ("asos_p1_", "asos_soft_")
MAX_SOFT_PATH_OBSERVATIONS_PER_SCOPE = 400


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
    formal_rows = conn.execute(
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
    if len(formal_rows) > MAX_FORMAL_OBSERVATIONS_PER_SCOPE:
        raise ContractError("resident_p1_observation_scope_cap_exceeded")
    # Soft path-evidence lanes on their own resource bound: an intra-hour
    # carrier day is ~288 rows, an order of magnitude past the formal
    # cadence the formal cap was sized for, and one bound covering both
    # would let a carrier flood evict the very reports settlement follows.
    soft_rows = conn.execute(
        "SELECT observation_id,source_lane,station_id,report_type,valid_at,"
        "reported_temperature_c,raw_sha256,raw_text,canonical_committed_at,"
        "correction_suspected "
        "FROM reported_trajectory_observations "
        "WHERE station_id=? AND source_lane IN ('asos_1min','madis_hfmetar') "
        "AND julianday(valid_at)>=julianday(?) "
        "AND julianday(valid_at)<julianday(?) "
        "AND julianday(canonical_committed_at)<=julianday(?) "
        "ORDER BY valid_at,canonical_committed_at,observation_id "
        "LIMIT ?",
        (
            station,
            window["target_day_start_utc"],
            window["target_day_end_utc"],
            as_of_utc,
            MAX_SOFT_PATH_OBSERVATIONS_PER_SCOPE + 1,
        ),
    ).fetchall()
    if len(soft_rows) > MAX_SOFT_PATH_OBSERVATIONS_PER_SCOPE:
        raise ContractError("resident_p1_soft_observation_scope_cap_exceeded")
    rows = sorted(
        [*formal_rows, *soft_rows],
        key=lambda row: (str(row[4]), str(row[8]), str(row[0])),
    )
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
        # Two different claims live in this gate.  ``committed_at <=
        # decision_at`` is the as-of rule and stays absolute: nothing may be
        # read before it existed.  ``valid_at <= committed_at`` was meant to
        # catch a record back-dated into the future, but it also caught
        # ordinary wire jitter -- a routine METAR is transmitted a few seconds
        # before the minute it is stamped for, so SBGR (25 reports, up to
        # 15.2s early) and MPMG fail-closed for the whole station-day and P1
        # never ran there.  Allow the transmission lead, keep the rest.
        if (
            committed_at > decision_at
            or valid_at - committed_at > _MAX_TRANSMISSION_LEAD
        ):
            raise ContractError("resident_p1_observation_not_strict_asof")
        report_type = str(row[3] or "").strip().upper()
        row_lane = str(row[1] or "").strip().lower()
        if row_lane == source_lane:
            if report_type not in {"METAR", "SPECI"}:
                raise ContractError("resident_p1_report_type_invalid")
        elif row_lane in P1_ASOS_SUBSTITUTE_LANE_REPORT_TYPES:
            # An OOS-governed ASOS carrier row.  Its admission (station
            # OOS_READY artifact, QC eligibility) was proved by the
            # materializer that minted it; this reader re-checks only the
            # identity stamp and the lane/report-type pairing as a second
            # fail-closed boundary.
            if report_type != P1_ASOS_SUBSTITUTE_LANE_REPORT_TYPES[row_lane]:
                raise ContractError("resident_p1_report_type_invalid")
            if not str(row[0] or "").startswith(
                _P1_ASOS_CARRIER_ID_PREFIXES
            ):
                raise ContractError(
                    "resident_p1_asos_substitute_identity_invalid"
                )
        else:
            raise ContractError("resident_p1_source_lane_not_awc")
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
        # blocker, never a cadence artifact.  The ASOS substitute lanes are
        # outside this policy's two-lane vocabulary by design: their timing
        # legality is the frozen schedule the adapter already enforced.
        if row_lane == source_lane:
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
            "source_lane": row_lane,
            "report_type": report_type,
            "evidence_role": (
                "FORMAL" if row_lane == source_lane else "SOFT_PATH"
            ),
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
    station = str(station_id).strip().upper()
    # The station range is an accelerator, not a filter.  Every predicate
    # that decided the row set before is still below it; this only tells
    # SQLite where to start reading.
    #
    # Without it the plan seeks to fact_type and then scans all fourteen
    # thousand revision facts, calling json_extract twice on each -- 3.1
    # seconds a call, forty-four calls a cycle, 136 of the cycle's 735
    # seconds.  ``scope_key`` opens with the station id and the table
    # carries UNIQUE(fact_type, scope_key), so the same rows are reachable
    # by a range seek that touches a few hundred.  Verified on all 14,471
    # rows before this was written: the first pipe-delimited field equals
    # ``$.station_id`` in every one, and no station id contains a pipe.
    # Measured 43.3ms -> 0.4ms with identical row sets.
    #
    # The upper bound is the successor of the delimiter, so it stops after
    # this station's keys.  It can over-include a longer station id that
    # begins with this one (none exist -- ICAO ids are four characters),
    # which is exactly why the station equality below is kept: the seek is
    # allowed to be generous, the predicate is not.
    scope_low = f"{station}|"
    scope_high = station + chr(ord("|") + 1)
    try:
        # Two identity lanes, one join: AWC reports carry PR12 truth
        # revisions, scheduled ASOS substitutes carry their own parallel
        # identity facts.  Both are keyed the same way and both scope_keys
        # open with the station id, so the same range seek serves each.
        found = conn.execute(
            "SELECT json_extract(fact_json,'$.observation_id'),"
            "  json_extract(fact_json,'$.revision_no'),"
            "  json_extract(fact_json,'$.canonical_committed_at_utc'),"
            "  content_sha256 "
            "FROM canonical_ssot_fact "
            "WHERE fact_type IN ("
            "  'truth_observation_revision','substitute_observation_revision'"
            ") "
            "  AND scope_key >= ? AND scope_key < ? "
            "  AND json_extract(fact_json,'$.station_id')=? "
            f"  AND json_extract(fact_json,'$.observation_id') IN ({placeholders})",
            (scope_low, scope_high, station, *sorted(wanted)),
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
    "MAX_SOFT_PATH_OBSERVATIONS_PER_SCOPE",
    "P1_ASOS_SUBSTITUTE_LANE_REPORT_TYPES",
    "attach_truth_observation_revisions",
    "load_resident_formal_observations",
]
