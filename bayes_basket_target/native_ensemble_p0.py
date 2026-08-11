"""Compile activated calibrated native-member ensembles into P0 packets."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, ProbabilityEnvelope, content_sha256
from .cross_consensus import (
    CONSENSUS_BRIDGE_METHOD_TAG,
    bridged_cross_probability,
)
from .future_temperature_curve import (
    build_native_family_temperature_curve,
    build_root_future_temperature_curve,
)
from .native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
    validate_cross_native_ensemble_weights,
    validate_native_ensemble_transport_gate,
)
from .native_archive_lead import lead_hour_bucket_for_run
from .native_ensemble_authority import (
    NativeOosSnapshot,
    pointer_activation_mismatch_reasons,
    pointer_for_scope,
    station_has_pointer,
    validate_native_oos_snapshot_manifest,
)
from .open_meteo_ensemble import (
    FORWARD_OOS_TEMPORAL_SCOPE,
    OBSERVATIONAL_EVIDENCE_ROLE,
    SCHEMA as FORWARD_PACKET_SCHEMA,
)
from .official_native_forward_packet import (
    MIXED_NATIVE_CYCLES,
    SCHEMA as OFFICIAL_FORWARD_PACKET_SCHEMA,
)
from .ssot import native_ensemble_foundation_schema_status


SCHEMA = "calibrated_native_cross_ensemble_p0_packet_v1"
POSTERIOR_VERSION = "calibrated_native_cross_ensemble_p0_v1"
DEFAULT_FRESH_SECONDS = 6 * 60 * 60


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _season_bin(day: str) -> str:
    month = int(day[5:7])
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


# Leads P0 may be compiled at, freshest first.  A shorter lead supersedes a
# longer one for the same station-day: the newest run replaces the previous
# P0 rather than sitting alongside it.
SUPPORTED_P0_LEAD_BUCKETS = ("d0", "d1", "d2")


def _lead_bucket(
    *,
    event_date: str,
    timezone_name: str,
    captured_at_utc: str,
) -> str:
    captured = datetime.fromisoformat(
        captured_at_utc.replace("Z", "+00:00")
    ).astimezone(ZoneInfo(timezone_name))
    delta = (
        datetime.fromisoformat(event_date).date()
        - captured.date()
    ).days
    return f"d{delta}" if delta >= 0 else f"d_minus_{abs(delta)}"


def _hour_lead_bucket(
    *,
    event_date: str,
    timezone_name: str,
    run_init_utc_by_system: Mapping[str, Any],
    observed_system_ids: Sequence[str],
) -> str | None:
    """The training group this packet's runs belong to, or None.

    OOS trains by how far ahead a run was issued rather than by the calendar
    label the collector wrote, because the two disagree badly: the calendar
    ``d1`` and ``d2`` groups overlap across 18-31h and ``d2`` alone spans 37h.
    P0 has to ask for weights under the same key they were fitted under, and
    that key is derived here from the runs the packet actually carries.

    ``None`` means no single hour bucket describes this packet -- either no run
    is stated at all, or the systems that state one were issued far enough
    apart to fall in different groups.  A cross-ensemble artifact holds systems
    that all share one bucket, so a genuinely split packet cannot match one,
    and saying so is different from guessing which half to believe.

    A system with no run initialisation is skipped rather than treated as a
    split.  It cannot be in a cross-ensemble artifact -- that requires a native
    run -- so it has no bucket to disagree about, and letting it veto the
    others made a bystander decide their key.  ZUUU carried ECCC as observed
    while excluding it for having no exact OOS activation; ECCC states no run,
    and the two systems actually used were both issued 22h ahead, yet the
    packet fell back to the calendar label and asked for weights under a key
    nothing is fitted under any more.
    """

    if not run_init_utc_by_system or not observed_system_ids:
        return None
    try:
        day_start = datetime.fromisoformat(event_date).replace(
            tzinfo=ZoneInfo(timezone_name)
        ).astimezone(timezone.utc)
    except (ValueError, TypeError, KeyError):
        return None
    buckets: set[str] = set()
    for system_id in observed_system_ids:
        raw = str(run_init_utc_by_system.get(system_id) or "").strip()
        if not raw:
            continue
        try:
            run_init = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if run_init.tzinfo is None or run_init.utcoffset() is None:
            return None
        buckets.add(
            lead_hour_bucket_for_run(
                day_start_utc=day_start, run_init=run_init
            )
        )
    if len(buckets) != 1:
        return None
    return next(iter(buckets))


def _activated_lead_bucket(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    season_bin: str,
    decision_at_utc: str,
    observed_system_ids: Sequence[str],
    candidates: Sequence[str],
) -> str:
    """The first candidate lead bucket with an admissible cross activation.

    Candidates are ordered freshest-vocabulary first: the hour bucket the
    trainer now writes, then the calendar bucket earlier artifacts were keyed
    under.  Both are real trained groupings of the same rows, so falling back
    is not borrowing another lead's weights -- it is reading a coarser grouping
    that still contains this run.  Preferring the hour bucket matters because
    no new calendar-keyed artifact is written any more; without it P0 would
    coast on artifacts already in their validity window and then fail closed
    when they expire.

    Only admissibility is probed here -- scope window and whether the selected
    systems are among those observed.  The OOS-pass and lineage checks stay
    where they were: this chooses which grouping to ask under, not whether the
    answer is usable.
    """

    observed = set(observed_system_ids)
    for candidate in candidates:
        rows = conn.execute(
            "SELECT r.ensemble_system_ids_json "
            "FROM calibration_native_ensemble_activation a "
            "JOIN calibration_cross_native_ensemble_artifact r "
            "ON r.cross_ensemble_artifact_id=a.artifact_id "
            "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
            "AND a.station_id=? AND a.season_bin=? AND a.lead_bucket=? "
            "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
            "AND a.valid_from_utc<=? AND a.valid_until_utc>? "
            "ORDER BY a.generation DESC,a.activation_id LIMIT 256",
            (
                station_id,
                season_bin,
                candidate,
                decision_at_utc,
                decision_at_utc,
            ),
        ).fetchall()
        for row in rows:
            try:
                selected = {str(value) for value in json.loads(str(row[0]))}
            except (TypeError, ValueError):
                continue
            if selected and selected.issubset(observed):
                return candidate
    return candidates[-1]


def _interval_probability(
    values: Sequence[float],
    intervals: Mapping[str, Sequence[float | None]],
) -> dict[str, float]:
    if not values:
        raise ContractError("native_ensemble_p0_values_missing")
    counts = {str(bucket): 0 for bucket in intervals}
    for raw_value in values:
        value = float(raw_value)
        if not math.isfinite(value):
            raise ContractError("native_ensemble_p0_value_invalid")
        matches = []
        for bucket, bounds in intervals.items():
            if (
                not isinstance(bounds, Sequence)
                or isinstance(bounds, (str, bytes, bytearray))
                or len(bounds) != 2
            ):
                raise ContractError(
                    "native_ensemble_p0_bucket_interval_invalid"
                )
            lower = bounds[0]
            upper = bounds[1]
            if (
                (lower is None or value >= float(lower))
                and (upper is None or value < float(upper))
            ):
                matches.append(str(bucket))
        if len(matches) != 1:
            raise ContractError(
                "native_ensemble_p0_bucket_contract_not_exhaustive"
            )
        counts[matches[0]] += 1
    total = len(values)
    return {
        bucket: count / total
        for bucket, count in counts.items()
    }


def _calibrated_system_probability(
    *,
    values: Sequence[float],
    intervals: Mapping[str, Sequence[float | None]],
    bias_parameters: Mapping[str, Any],
    spread_parameters: Mapping[str, Any],
    reliability_parameters: Mapping[str, Any],
) -> dict[str, float]:
    current = [float(value) for value in values]
    mean = sum(current) / len(current)
    bias = float(bias_parameters["bias_c"])
    spread = float(spread_parameters["spread_factor"])
    reliability = float(
        reliability_parameters["reliability_weight"]
    )
    if (
        not -20.0 <= bias <= 20.0
        or not 0.25 <= spread <= 4.0
        or not 0.0 < reliability <= 1.0
    ):
        raise ContractError(
            "native_ensemble_p0_calibration_parameters_invalid"
        )
    # The correction form is the artifact's recorded choice, made on a
    # validation tail at fit time.  The affine form corrects by an amount
    # that tracks the forecast level -- what a station-local regime shift
    # usually is -- while an artifact without the field keeps the constant
    # path byte-identical.  Serving a form the artifact does not record
    # would split evaluation from serving, so the parameters are the only
    # authority here.
    if str(bias_parameters.get("correction_form") or "constant") == "affine":
        slope = float(bias_parameters["affine_slope"])
        intercept = float(bias_parameters["affine_intercept_c"])
        if not 0.3 <= slope <= 1.7 or not -40.0 <= intercept <= 40.0:
            raise ContractError(
                "native_ensemble_p0_calibration_parameters_invalid"
            )
        center = intercept + slope * mean
    else:
        center = mean + bias
    corrected = [
        center + spread * (value - mean)
        for value in current
    ]
    member_probability = _interval_probability(corrected, intervals)
    climate_samples = reliability_parameters.get("climate_samples_c")
    if not isinstance(climate_samples, list) or not climate_samples:
        raise ContractError(
            "native_ensemble_p0_reliability_samples_missing"
        )
    climate_probability = _interval_probability(
        [float(value) for value in climate_samples],
        intervals,
    )
    probability = {
        bucket: (
            reliability * member_probability[bucket]
            + (1.0 - reliability) * climate_probability[bucket]
        )
        for bucket in intervals
    }
    total = sum(probability.values())
    return {
        bucket: value / total
        for bucket, value in probability.items()
    }


def _ordered_buckets(
    intervals: Mapping[str, Sequence[float | None]],
) -> list[str]:
    def key(item: tuple[str, Sequence[float | None]]) -> tuple[float, float]:
        lower, upper = item[1]
        return (
            float("-inf") if lower is None else float(lower),
            float("inf") if upper is None else float(upper),
        )

    ordered = [str(bucket) for bucket, _bounds in sorted(
        intervals.items(),
        key=key,
    )]
    if len(ordered) != len(set(ordered)):
        raise ContractError("native_ensemble_p0_bucket_duplicate")
    return ordered


def _bucket_centers(
    intervals: Mapping[str, Sequence[float | None]],
    ordered: Sequence[str],
) -> list[float]:
    """A representative temperature per bucket, for histogram shifting.

    Interior buckets take their midpoint.  An open edge bucket has no
    midpoint, so it takes the point half a degree beyond its finite bound --
    the same 1C pitch as its neighbours -- which only matters as the anchor
    the consensus shift measures distances against.
    """

    centers: list[float] = []
    for bucket in ordered:
        lower, upper = intervals[str(bucket)]
        if lower is None and upper is None:
            raise ContractError("native_ensemble_p0_bucket_interval_invalid")
        if lower is None:
            centers.append(float(upper) - 0.5)
        elif upper is None:
            centers.append(float(lower) + 0.5)
        else:
            centers.append((float(lower) + float(upper)) / 2.0)
    return centers


def _bucket_for_value(
    value: float,
    intervals: Mapping[str, Sequence[float | None]],
) -> str:
    matches = [
        str(bucket)
        for bucket, bounds in intervals.items()
        if (
            len(bounds) == 2
            and (bounds[0] is None or value >= float(bounds[0]))
            and (bounds[1] is None or value < float(bounds[1]))
        )
    ]
    if len(matches) != 1:
        raise ContractError(
            "native_ensemble_p0_bucket_contract_not_exhaustive"
        )
    return matches[0]


_VALID_NATIVE_RUN_CYCLES = frozenset({"00Z", "06Z", "12Z", "18Z"})


def _require_aggregator_fallback_authority(
    forward_packet: Mapping[str, Any],
) -> None:
    """Reject a republisher packet unless it carries its non-native limits.

    This is deliberately checked again at P0 compilation rather than relying
    on the candidate-store ingress: a forward inbox is a handoff boundary and
    must not be able to turn an Open-Meteo payload into native arrival proof.
    """

    authority = forward_packet.get("source_authority")
    if not isinstance(authority, Mapping):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:aggregator_fallback_authority_missing"
        )
    if (
        authority.get("source_role") != "aggregator_republisher"
        or authority.get("native_run_init_known") is not False
        or authority.get("native_publication_at_known") is not False
        or authority.get("native_arrival_index_eligible") is not False
        or authority.get("formal_p0_eligible") is not False
        or authority.get("production_no_live_calibration_candidate")
        is not True
        or forward_packet.get("evidence_role")
        != OBSERVATIONAL_EVIDENCE_ROLE
        or forward_packet.get("oos_event_temporal_scope")
        != FORWARD_OOS_TEMPORAL_SCOPE
        or forward_packet.get("strict_oos_sample_eligible") is not False
        or forward_packet.get("can_substitute_for_formal_promotion")
        is not False
        or authority.get("evidence_role")
        != OBSERVATIONAL_EVIDENCE_ROLE
        or authority.get("oos_event_temporal_scope")
        != FORWARD_OOS_TEMPORAL_SCOPE
        or authority.get("counts_as_strict_oos") is not False
        or authority.get("historical_oos_contract_bound") is not False
        or authority.get("can_substitute_for_formal_promotion")
        is not False
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:aggregator_fallback_authority_invalid"
        )


def _official_forward_lineage(
    forward_packet: Mapping[str, Any],
    *,
    observed_system_ids: Sequence[str],
    captured: datetime,
) -> dict[str, Any]:
    """Resolve official packet timing without inventing a common cycle."""

    packet_cycle = str(forward_packet.get("run_cycle") or "").strip()
    packet_scope = str(
        forward_packet.get("run_cycle_scope") or ""
    ).strip()
    cycle_by_system_raw = forward_packet.get("run_cycle_by_system")
    init_by_system_raw = forward_packet.get("run_init_utc_by_system")
    query_by_system_raw = forward_packet.get(
        "archive_query_at_utc_by_system"
    )
    mixed = (
        packet_cycle == MIXED_NATIVE_CYCLES
        or packet_scope == "PER_SYSTEM_NATIVE_CYCLE"
    )
    observed = {str(system_id) for system_id in observed_system_ids}

    def _mapping_or_missing(
        value: Any,
        error: str,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ContractError(error)
        resolved = {str(key): item for key, item in value.items()}
        if set(resolved) != observed:
            raise ContractError(error)
        return resolved

    def _timestamp(value: Any, error: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(
                str(value or "").replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise ContractError(error) from exc
        if parsed.tzinfo is None:
            raise ContractError(error)
        return parsed.astimezone(captured.tzinfo)

    if mixed:
        cycle_by_system = _mapping_or_missing(
            cycle_by_system_raw,
            "native_ensemble_p0_mixed_cycle_lineage_missing",
        )
        init_by_system = _mapping_or_missing(
            init_by_system_raw,
            "native_ensemble_p0_mixed_init_lineage_missing",
        )
        query_by_system = _mapping_or_missing(
            query_by_system_raw,
            "native_ensemble_p0_mixed_arrival_lineage_missing",
        )
        for system_id in sorted(observed):
            cycle = str(cycle_by_system[system_id] or "").strip()
            if cycle not in _VALID_NATIVE_RUN_CYCLES:
                raise ContractError(
                    "native_ensemble_p0_mixed_cycle_lineage_invalid"
                )
            run_init = _timestamp(
                init_by_system[system_id],
                "native_ensemble_p0_mixed_init_lineage_invalid",
            )
            archive_query = _timestamp(
                query_by_system[system_id],
                "native_ensemble_p0_mixed_arrival_lineage_invalid",
            )
            if (
                run_init.hour != int(cycle[:2])
                or archive_query < run_init
                or archive_query > captured
                or run_init > captured
            ):
                raise ContractError(
                    "native_ensemble_p0_mixed_lineage_timestamp_mismatch"
                )
        availability = forward_packet.get("availability_evidence")
        if not isinstance(availability, Mapping):
            raise ContractError(
                "native_ensemble_p0_mixed_availability_evidence_missing"
            )
        availability_by_system = availability.get("by_system")
        if not isinstance(availability_by_system, Mapping):
            raise ContractError(
                "native_ensemble_p0_mixed_availability_evidence_missing"
            )
        if {
            str(key) for key in availability_by_system
        } != observed:
            raise ContractError(
                "native_ensemble_p0_mixed_availability_evidence_incomplete"
            )
        availability_identity = {
            key: value
            for key, value in availability.items()
            if key != "content_sha256"
        }
        if (
            availability.get("content_sha256")
            != content_sha256(availability_identity)
        ):
            raise ContractError(
                "native_ensemble_p0_mixed_availability_evidence_invalid"
            )
        for system_id in sorted(observed):
            receipt = availability_by_system[system_id]
            if not isinstance(receipt, Mapping):
                raise ContractError(
                    "native_ensemble_p0_mixed_availability_evidence_invalid"
                )
            if (
                str(receipt.get("run_cycle") or "")
                != str(cycle_by_system[system_id])
                or str(receipt.get("run_init_utc") or "")
                != str(init_by_system[system_id])
                or str(receipt.get("archive_query_at_utc") or "")
                != str(query_by_system[system_id])
            ):
                raise ContractError(
                    "native_ensemble_p0_mixed_availability_evidence_mismatch"
                )
        return {
            "mixed": True,
            "run_cycle": MIXED_NATIVE_CYCLES,
            "run_cycle_scope": "PER_SYSTEM_NATIVE_CYCLE",
            "run_cycle_by_system": {
                system_id: str(cycle_by_system[system_id])
                for system_id in sorted(observed)
            },
            "run_init_utc_by_system": {
                system_id: str(init_by_system[system_id])
                for system_id in sorted(observed)
            },
            "archive_query_at_utc_by_system": {
                system_id: str(query_by_system[system_id])
                for system_id in sorted(observed)
            },
            "availability_evidence": dict(availability),
        }

    if packet_cycle not in _VALID_NATIVE_RUN_CYCLES:
        raise ContractError("native_ensemble_p0_official_scope_invalid")
    if cycle_by_system_raw is None:
        cycle_by_system = {
            system_id: packet_cycle
            for system_id in sorted(observed)
        }
    else:
        cycle_by_system = _mapping_or_missing(
            cycle_by_system_raw,
            "native_ensemble_p0_same_cycle_lineage_invalid",
        )
        if any(
            str(value or "").strip() != packet_cycle
            for value in cycle_by_system.values()
        ):
            raise ContractError(
                "native_ensemble_p0_same_cycle_lineage_mismatch"
            )
    init_by_system = (
        None
        if init_by_system_raw is None
        else _mapping_or_missing(
            init_by_system_raw,
            "native_ensemble_p0_same_cycle_init_lineage_invalid",
        )
    )
    query_by_system = (
        None
        if query_by_system_raw is None
        else _mapping_or_missing(
            query_by_system_raw,
            "native_ensemble_p0_same_cycle_arrival_lineage_invalid",
        )
    )
    if init_by_system is not None:
        for system_id in sorted(observed):
            run_init = _timestamp(
                init_by_system[system_id],
                "native_ensemble_p0_same_cycle_init_lineage_invalid",
            )
            if run_init.hour != int(packet_cycle[:2]) or run_init > captured:
                raise ContractError(
                    "native_ensemble_p0_same_cycle_init_lineage_mismatch"
                )
    if query_by_system is not None:
        for system_id in sorted(observed):
            archive_query = _timestamp(
                query_by_system[system_id],
                "native_ensemble_p0_same_cycle_arrival_lineage_invalid",
            )
            if archive_query > captured:
                raise ContractError(
                    "native_ensemble_p0_same_cycle_arrival_lineage_mismatch"
                )
    return {
        "mixed": False,
        "run_cycle": packet_cycle,
        "run_cycle_scope": "SAME_NATIVE_CYCLE",
        "run_cycle_by_system": cycle_by_system,
        "run_init_utc_by_system": init_by_system or {},
        "archive_query_at_utc_by_system": query_by_system or {},
        "availability_evidence": forward_packet.get(
            "availability_evidence"
        ),
    }


def compile_native_ensemble_p0(
    conn: sqlite3.Connection,
    *,
    forward_packet: Mapping[str, Any],
    decision_at_utc: str,
    fresh_seconds: int = DEFAULT_FRESH_SECONDS,
    native_oos_snapshot: NativeOosSnapshot | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile one station-day, or fail closed with ``NO_P0``."""

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_ensemble_foundation_schema_missing"
        )
    packet_schema = str(forward_packet.get("schema") or "")
    if packet_schema not in {
        FORWARD_PACKET_SCHEMA,
        OFFICIAL_FORWARD_PACKET_SCHEMA,
    }:
        raise ContractError("native_ensemble_p0_forward_schema_invalid")
    if fresh_seconds < 60 or fresh_seconds > 24 * 60 * 60:
        raise ContractError("native_ensemble_p0_freshness_invalid")
    try:
        decision = datetime.fromisoformat(
            decision_at_utc.replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "native_ensemble_p0_decision_timestamp_invalid"
        ) from exc
    if decision.tzinfo is None:
        raise ContractError("native_ensemble_p0_decision_timestamp_invalid")
    decision = decision.astimezone(timezone.utc)
    official_snapshot = packet_schema == OFFICIAL_FORWARD_PACKET_SCHEMA
    native_pointer_snapshot: NativeOosSnapshot | None = None
    station_attested = False
    if official_snapshot:
        official_identity = {
            key: value
            for key, value in forward_packet.items()
            if key != "snapshot_content_sha256"
        }
        if (
            forward_packet.get("snapshot_content_sha256")
            != content_sha256(official_identity)
        ):
            raise ContractError(
                "native_ensemble_p0_official_snapshot_invalid"
            )
    else:
        _require_aggregator_fallback_authority(forward_packet)
    captured_at = str(
        forward_packet.get(
            (
                "received_at_utc"
                if official_snapshot
                else "aggregator_snapshot_at_utc"
            )
        )
        or ""
    )
    try:
        captured = datetime.fromisoformat(
            captured_at.replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "native_ensemble_p0_capture_timestamp_invalid"
        ) from exc
    if captured.tzinfo is None:
        raise ContractError("native_ensemble_p0_capture_timestamp_invalid")
    captured = captured.astimezone(timezone.utc)
    if captured > decision:
        raise ContractError("native_ensemble_p0_future_capture_forbidden")
    # A compile-time refresh must not revive an old native packet.  Besides
    # preserving the P0 TTL, this is the decisive ``native late`` boundary:
    # the production worker can then try an independently governed
    # Open-Meteo republisher packet for the same station/day.  The fallback
    # still has to pass its own historical-OOS activation checks below; this
    # branch only prevents a stale native receipt from suppressing it.
    # ``ProbabilityEnvelope`` requires a strictly positive freshness window,
    # so an age exactly equal to the TTL is already stale as well.
    if (decision - captured).total_seconds() >= fresh_seconds:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_ensemble_snapshot_stale"
        )
    station_id = str(
        forward_packet.get("station_id") or ""
    ).strip().upper()
    event_date = str(forward_packet.get("event_date") or "")
    timezone_name = str(
        forward_packet.get("settlement_timezone") or ""
    )
    try:
        settlement_end = (
            datetime.fromisoformat(event_date)
            .replace(tzinfo=ZoneInfo(timezone_name))
            + timedelta(days=1)
        )
    except (ValueError, TypeError) as exc:
        raise ContractError(
            "native_ensemble_p0_settlement_window_invalid"
        ) from exc
    if decision >= settlement_end:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:settlement_day_already_closed"
        )
    station_row = conn.execute(
        "SELECT settlement_definition "
        "FROM calibration_station_registry WHERE station_id=?",
        (station_id,),
    ).fetchone()
    timezone_rows = conn.execute(
        "SELECT DISTINCT settlement_timezone "
        "FROM basket_target_market_registry "
        "WHERE station_id=? AND event_date=? "
        "AND settlement_timezone<>''",
        (station_id, event_date),
    ).fetchall()
    if station_row is None or len(timezone_rows) != 1:
        raise ContractError(
            "native_ensemble_p0_station_scope_invalid"
        )
    if str(timezone_rows[0][0]) != timezone_name:
        raise ContractError(
            "native_ensemble_p0_market_timezone_mismatch"
        )
    binding_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND "
        "name='calibration_station_market_timezone_binding'"
    ).fetchone()
    if binding_table is not None:
        binding = conn.execute(
            "SELECT settlement_timezone,market_registry_timezone_count "
            "FROM calibration_station_market_timezone_binding "
            "WHERE station_id=?",
            (station_id,),
        ).fetchone()
        if (
            binding is not None
            and (
                str(binding[0]) != timezone_name
                or int(binding[1]) != 1
            )
        ):
            raise ContractError(
                "native_ensemble_p0_market_timezone_binding_mismatch"
            )
    settlement_definition = str(station_row[0])
    season_bin = _season_bin(event_date)
    if official_snapshot:
        lead_bucket = str(
            forward_packet.get("lead_bucket") or ""
        )
        # P0 follows the freshest run available, so its lead shortens as the
        # event day approaches: d2 two days out, then d1, then d0.  Pinning the
        # official path to d2 meant the event day itself was still priced off a
        # forecast issued two days earlier, with every later model update
        # discarded -- P0-M and P1 correct from observations and cannot recover
        # what the model itself has since learned.
        #
        # Admitting the lead here is not the same as trusting it.  Skill at d0
        # is far higher than at d2, so the weights are not interchangeable, and
        # the OOS snapshot is keyed by lead further down: a lead with no trained
        # weights fails closed there rather than silently borrowing d2's.
        if lead_bucket not in SUPPORTED_P0_LEAD_BUCKETS:
            raise ContractError(
                "native_ensemble_p0_official_scope_invalid"
            )
    else:
        lead_bucket = _lead_bucket(
            event_date=event_date,
            timezone_name=timezone_name,
            captured_at_utc=captured_at,
        )
        run_cycle = "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
    if official_snapshot and native_oos_snapshot is not None:
        try:
            snapshot = (
                native_oos_snapshot
                if isinstance(native_oos_snapshot, NativeOosSnapshot)
                else validate_native_oos_snapshot_manifest(native_oos_snapshot)
            )
            # The scope is not known yet -- it depends on the run this packet
            # carries -- so only establish here that the station is attested at
            # all.  The binding for the scope in hand is resolved per candidate
            # activation below, once the lead is settled.
            native_pointer_snapshot = snapshot
            station_attested = station_has_pointer(snapshot, station_id)
        except ContractError as exc:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_oos_snapshot_manifest_invalid"
            ) from exc
        if not station_attested:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_current_pointer_binding_missing"
            )
    systems_raw = forward_packet.get("systems")
    intervals_raw = forward_packet.get("bucket_intervals")
    if (
        not isinstance(systems_raw, list)
        or len(systems_raw) < 2
        or not isinstance(intervals_raw, Mapping)
    ):
        raise ContractError(
            "native_ensemble_p0_forward_members_missing"
        )
    observed_systems = {
        str(system["ensemble_system_id"]): system
        for system in systems_raw
        if isinstance(system, Mapping)
    }
    if len(observed_systems) != len(systems_raw):
        raise ContractError("native_ensemble_p0_system_identity_invalid")
    observed_system_ids = sorted(observed_systems)
    official_lineage = (
        _official_forward_lineage(
            forward_packet,
            observed_system_ids=observed_system_ids,
            captured=captured,
        )
        if official_snapshot
        else {
            "mixed": False,
            "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
            "run_cycle_scope": "AGGREGATOR_NATIVE_CYCLE_UNKNOWN",
            "run_cycle_by_system": {
                system_id: "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
                for system_id in observed_system_ids
            },
            "run_init_utc_by_system": {},
            "archive_query_at_utc_by_system": {},
            "availability_evidence": None,
        }
    )
    run_cycle = str(official_lineage["run_cycle"])
    # Two different questions share the name "lead", and conflating them is
    # what stranded the retrained weights.
    #
    # ``lead_bucket`` is the calendar label: how far from the event day this
    # decision is being made.  Station policy, the TAF fingerprint identity and
    # the operator-facing scope are all keyed by it, and it stays exactly as the
    # capture stated it.
    #
    # ``training_lead_bucket`` is the group the weights were fitted in.  OOS
    # switched that to lead measured in hours to the station's own local day,
    # because the calendar groups overlap: d1 and d2 both contain runs issued
    # 18-31h ahead, and d2 alone spans 37h.  Asking for weights under the
    # calendar label after that switch finds nothing -- no calendar-keyed
    # artifact is written any more -- so P0 has to ask under the key the fit
    # used.
    hour_lead_bucket = _hour_lead_bucket(
        event_date=event_date,
        timezone_name=timezone_name,
        run_init_utc_by_system=official_lineage["run_init_utc_by_system"],
        observed_system_ids=observed_system_ids,
    )
    training_lead_bucket = _activated_lead_bucket(
        conn,
        station_id=station_id,
        season_bin=season_bin,
        decision_at_utc=decision_at_utc,
        observed_system_ids=observed_system_ids,
        candidates=(
            (hour_lead_bucket, lead_bucket)
            if hour_lead_bucket is not None
            else (lead_bucket,)
        ),
    )
    activation_sql = (
        "SELECT a.activation_id,a.artifact_id,a.content_sha256,"
        "a.generation,a.valid_from_utc,"
        "a.available_system_set_hash_or_star,"
        "r.ensemble_system_ids_json,r.ensemble_system_count,"
        "r.run_cycle,a.run_cycle_by_system_json,"
        "a.run_cycle_by_system_sha256,"
        "r.run_cycle_by_system_json,"
        "r.run_cycle_by_system_sha256,"
        "r.selection_evidence_sha256,"
        "r.content_sha256 "
        "FROM calibration_native_ensemble_activation a "
        "JOIN calibration_cross_native_ensemble_artifact r "
        "ON r.cross_ensemble_artifact_id=a.artifact_id "
        "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        "AND a.station_id=? AND a.season_bin=? AND a.lead_bucket=? "
        "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "AND a.valid_from_utc<=? AND a.valid_until_utc>? "
        "ORDER BY a.generation DESC,a.activation_id LIMIT 256"
    )
    activation_parameters: tuple[Any, ...] = (
        station_id,
        season_bin,
        training_lead_bucket,
    )
    activation_parameters += (decision_at_utc, decision_at_utc)
    activation_rows = conn.execute(
        activation_sql,
        activation_parameters,
    ).fetchall()
    observed_set = set(observed_system_ids)
    compatible_activations: list[
        tuple[Any, list[str]]
    ] = []
    rejected_or_unproven_activation_seen = False
    for row in activation_rows:
        if station_attested:
            # Resolve the attestation for this activation's own scope.  The
            # pointer names one activation by id and content hash, so it can
            # only speak for the scope that activation was fitted in; a
            # station attested at another lead has nothing to say about this
            # one, and treating its pointer as authoritative here rejected
            # every correctly calibrated candidate.
            native_pointer_binding = pointer_for_scope(
                native_pointer_snapshot,
                station_id,
                season_bin=season_bin,
                lead_bucket=training_lead_bucket,
                run_cycle=str(row[8] or ""),
            )
            if native_pointer_binding is None:
                continue
            pointer_mismatches = pointer_activation_mismatch_reasons(
                native_pointer_binding,
                activation_id=str(row[0]),
                artifact_id=str(row[1]),
                activation_sha256=str(row[2]),
                artifact_sha256=str(row[14] or ""),
                selection_evidence_sha256=str(row[13]),
                generation=int(row[3]),
                season_bin=season_bin,
                lead_bucket=training_lead_bucket,
                run_cycle=str(row[8] or ""),
                run_cycle_by_system_json=str(row[9] or ""),
                authority_scope="PRODUCTION_NO_LIVE_ONLY",
            )
            if pointer_mismatches:
                continue
        selected_ids = sorted(
            str(value) for value in json.loads(str(row[6]))
        )
        selected_cycle_mapping = {
            system_id: str(
                official_lineage["run_cycle_by_system"].get(system_id)
                or ""
            )
            for system_id in selected_ids
        }
        selected_cycle_mapping_json = _json(selected_cycle_mapping)
        selected_cycle_mapping_sha = content_sha256(
            selected_cycle_mapping
        )
        selected_run_cycles = set(selected_cycle_mapping.values())
        selected_run_cycle = (
            next(iter(selected_run_cycles))
            if len(selected_run_cycles) == 1
            else MIXED_NATIVE_CYCLES
        )
        if (
            len(selected_ids) != int(row[7])
            or len(set(selected_ids)) != len(selected_ids)
            or str(row[5]) != content_sha256(selected_ids)
            or not set(selected_ids).issubset(observed_set)
            or not all(selected_cycle_mapping.values())
            or str(row[8] or "") != selected_run_cycle
            or str(row[9] or "") != selected_cycle_mapping_json
            or str(row[10] or "") != selected_cycle_mapping_sha
            or str(row[11] or "") != selected_cycle_mapping_json
            or str(row[12] or "") != selected_cycle_mapping_sha
        ):
            continue
        evaluation_rows = conn.execute(
            "SELECT oos_state,selection_evidence_sha256,evaluated_at_utc "
            "FROM calibration_cross_native_ensemble_oos_evaluation "
            "WHERE station_id=? AND season_bin=? AND lead_bucket=? "
            "AND run_cycle=? AND run_cycle_by_system_json=? "
            "AND run_cycle_by_system_sha256=? "
            "AND settlement_definition=? "
            "AND ensemble_system_ids_json=? "
            "AND available_system_set_hash=? AND evaluated_at_utc<=? "
            "ORDER BY evaluated_at_utc DESC,evaluation_id DESC LIMIT 2",
            (
                station_id,
                season_bin,
                training_lead_bucket,
                selected_run_cycle,
                selected_cycle_mapping_json,
                selected_cycle_mapping_sha,
                settlement_definition,
                _json(selected_ids),
                str(row[5]),
                decision_at_utc,
            ),
        ).fetchall()
        if (
            not evaluation_rows
            or (
                len(evaluation_rows) > 1
                and str(evaluation_rows[0][2])
                == str(evaluation_rows[1][2])
            )
            or str(evaluation_rows[0][0])
            != "PRODUCTION_NO_LIVE_OOS_PASS"
            or str(evaluation_rows[0][1])
            != str(row[13])
        ):
            rejected_or_unproven_activation_seen = True
            continue
        compatible_activations.append((row, selected_ids))
    compatible_activations.sort(
        key=lambda item: (
            set(item[1]) == observed_set,
            len(item[1]),
            int(item[0][3]),
            str(item[0][0]),
        ),
        reverse=True,
    )
    activations = compatible_activations[:2]
    if not activations:
        if station_attested:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_current_pointer_not_bound"
            )
        if rejected_or_unproven_activation_seen:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:"
                "native_cross_oos_evaluation_not_passing"
            )
        raise ContractError(
            "FAIL_CLOSED_NO_P0:"
            "native_cross_ensemble_activation_not_exact"
        )
    if (
        len(activations) > 1
        and len(activations[0][1]) == len(activations[1][1])
        and set(activations[0][1]) == set(activations[1][1])
        and int(activations[0][0][3])
        == int(activations[1][0][3])
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:"
            "native_cross_ensemble_activation_not_exact"
        )
    selected_activation, system_ids = activations[0]
    systems = {
        system_id: observed_systems[system_id]
        for system_id in system_ids
    }
    if official_snapshot:
        source_archives = forward_packet.get("source_archives")
        if not isinstance(source_archives, list):
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_official_transport_lineage_missing"
            )
        transport_provider_by_system: dict[str, str] = {}
        for source_archive in source_archives:
            if not isinstance(source_archive, Mapping):
                raise ContractError(
                    "FAIL_CLOSED_NO_P0:native_official_transport_lineage_missing"
                )
            source_system_id = str(
                source_archive.get("ensemble_system_id") or ""
            ).strip()
            if source_system_id not in systems:
                continue
            transport_provider_id = str(
                source_archive.get("transport_provider_id") or ""
            ).strip()
            if (
                source_system_id in transport_provider_by_system
                and transport_provider_by_system[source_system_id]
                != transport_provider_id
            ):
                raise ContractError(
                    "FAIL_CLOSED_NO_P0:native_official_transport_lineage_ambiguous"
                )
            transport_provider_by_system[source_system_id] = (
                transport_provider_id
            )
        systems_by_id: dict[str, dict[str, Any]] = {}
        for system_id in system_ids:
            system_registry = conn.execute(
                "SELECT independence_family_id,provider_id,"
                "expected_member_count FROM calibration_native_ensemble_system "
                "WHERE ensemble_system_id=?",
                (system_id,),
            ).fetchone()
            if system_registry is None:
                raise ContractError(
                    "native_ensemble_p0_system_unregistered"
                )
            systems_by_id[system_id] = {
                "ensemble_system_id": system_id,
                "independence_family_id": str(system_registry[0]),
                "provider_id": str(system_registry[1]),
                "expected_member_count": int(system_registry[2]),
                "source_class": "NATIVE_ENSEMBLE_PROVIDER",
                "aggregator_or_republisher": 0,
            }
        try:
            validate_native_ensemble_transport_gate(
                selected_system_ids=system_ids,
                systems_by_id=systems_by_id,
                transport_provider_by_system=transport_provider_by_system,
            )
        except ContractError as exc:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:" + str(exc)
            ) from exc
    excluded_observed_system_ids = sorted(
        observed_set - set(system_ids)
    )
    available_set_hash = content_sha256(system_ids)
    activation_id = str(selected_activation[0])
    artifact_id = str(selected_activation[1])
    activation_sha = str(selected_activation[2])
    cross_artifact_run_cycle = str(selected_activation[8] or "")
    if not cross_artifact_run_cycle:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_cross_ensemble_cycle_missing"
        )
    selected_run_cycle = cross_artifact_run_cycle
    run_cycle = selected_run_cycle
    artifact = conn.execute(
        "SELECT ensemble_system_ids_json,ensemble_system_count,"
        "independent_family_count,selection_evidence_sha256,"
        "family_weight_caps_json,content_sha256,"
        "run_cycle_by_system_json,run_cycle_by_system_sha256,"
        "combination_method "
        "FROM calibration_cross_native_ensemble_artifact "
        "WHERE cross_ensemble_artifact_id=? "
        "AND probability_pathway=? AND valid_from_utc<=? "
        "AND valid_until_utc>?",
        (
            artifact_id,
            P0_PROBABILITY_PATHWAY,
            decision_at_utc,
            decision_at_utc,
        ),
    ).fetchone()
    if artifact is None:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_cross_ensemble_artifact_missing"
        )
    selected_system_ids = json.loads(str(artifact[0]))
    expected_cycle_mapping = {
        system_id: str(
            official_lineage["run_cycle_by_system"][system_id]
        )
        for system_id in system_ids
    }
    expected_cycle_mapping_json = _json(expected_cycle_mapping)
    expected_cycle_mapping_sha = content_sha256(
        expected_cycle_mapping
    )
    if (
        selected_system_ids != system_ids
        or int(artifact[1]) != len(system_ids)
        or int(artifact[2]) < 2
        or str(artifact[6] or "") != expected_cycle_mapping_json
        or str(artifact[7] or "") != expected_cycle_mapping_sha
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_cross_ensemble_mask_mismatch"
        )
    selection_sha = str(artifact[3])
    cross_artifact_sha = str(artifact[5])
    latest_oos_evaluations = conn.execute(
        "SELECT oos_state,selection_evidence_sha256,evaluated_at_utc "
        "FROM calibration_cross_native_ensemble_oos_evaluation "
        "WHERE station_id=? AND season_bin=? AND lead_bucket=? "
        "AND run_cycle=? AND run_cycle_by_system_json=? "
        "AND run_cycle_by_system_sha256=? "
        "AND settlement_definition=? AND ensemble_system_ids_json=? "
        "AND available_system_set_hash=? AND evaluated_at_utc<=? "
        "ORDER BY evaluated_at_utc DESC,evaluation_id DESC LIMIT 2",
        (
            station_id,
            season_bin,
            training_lead_bucket,
            selected_run_cycle,
            expected_cycle_mapping_json,
            expected_cycle_mapping_sha,
            settlement_definition,
            _json(system_ids),
            available_set_hash,
            decision_at_utc,
        ),
    ).fetchall()
    if (
        not latest_oos_evaluations
        or (
            len(latest_oos_evaluations) > 1
            and str(latest_oos_evaluations[0][2])
            == str(latest_oos_evaluations[1][2])
        )
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_cross_oos_evaluation_not_exact"
        )
    latest_oos_evaluation = latest_oos_evaluations[0]
    if (
        str(latest_oos_evaluation[0])
        != "PRODUCTION_NO_LIVE_OOS_PASS"
        or str(latest_oos_evaluation[1]) != selection_sha
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_cross_oos_evaluation_not_passing"
        )
    selection = conn.execute(
        "SELECT selected_weights_json,family_weights_json,"
        "family_weight_caps_json,"
        "cross_system_residual_correlation_json,content_sha256,"
        "train_end_date,run_cycle_by_system_json,"
        "run_cycle_by_system_sha256 "
        "FROM calibration_cross_native_ensemble_selection_evidence "
        "WHERE content_sha256=?",
        (selection_sha,),
    ).fetchone()
    if selection is None:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_selection_evidence_missing"
        )
    if (
        str(selection[6] or "") != expected_cycle_mapping_json
        or str(selection[7] or "") != expected_cycle_mapping_sha
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_selection_cycle_mapping_mismatch"
        )
    system_weights = {
        str(key): float(value)
        for key, value in json.loads(str(selection[0])).items()
    }
    family_caps = {
        str(key): float(value)
        for key, value in json.loads(str(selection[2])).items()
    }
    if system_weights.keys() != systems.keys():
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_selection_mask_mismatch"
        )
    system_probabilities: dict[str, dict[str, float]] = {}
    system_artifact_shas: dict[str, str] = {}
    system_activation_shas: dict[str, str] = {}
    aggregator_strict_oos_proof_by_system: dict[str, dict[str, Any]] = {}
    calibrated_system_centers_c: dict[str, float] = {}
    system_bias_c: dict[str, float] = {}
    family_by_system: dict[str, str] = {}
    for system_id in system_ids:
        system_config = conn.execute(
            (
                "SELECT independence_family_id,expected_member_count "
                "FROM calibration_native_ensemble_system "
                "WHERE ensemble_system_id=?"
                if official_snapshot
                else
                "SELECT independence_family_id,expected_member_count "
                "FROM calibration_republisher_ensemble_product "
                "WHERE ensemble_system_id=? "
                "AND transport_provider_id='OPEN_METEO' "
                "AND source_role='aggregator_republisher' "
                "AND native_arrival_index_eligible=0 "
                "AND formal_p0_eligible=0"
            ),
            (system_id,),
        ).fetchone()
        if system_config is None:
            raise ContractError(
                (
                    "native_ensemble_p0_system_unregistered"
                    if official_snapshot
                    else "republisher_p0_product_unregistered"
                )
            )
        family_by_system[system_id] = str(system_config[0])
        members = systems[system_id].get("members")
        if (
            not isinstance(members, list)
            or len(members) != int(system_config[1])
        ):
            raise ContractError(
                "native_ensemble_p0_member_set_incomplete"
            )
        system_run_cycle = str(
            official_lineage["run_cycle_by_system"].get(system_id)
            or ""
        )
        if not system_run_cycle:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_system_cycle_lineage_missing"
            )
        system_activation = conn.execute(
            "SELECT activation_id,artifact_id,content_sha256,generation,"
            "run_cycle_by_system_json,run_cycle_by_system_sha256 "
            "FROM calibration_native_ensemble_activation "
            "WHERE artifact_kind="
            "'NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
            "AND station_id=? AND ensemble_system_id_or_star=? "
            "AND season_bin=? AND lead_bucket=? AND run_cycle=? "
            "AND available_system_set_hash_or_star='*' "
            "AND run_cycle_by_system_json=? "
            "AND run_cycle_by_system_sha256=? "
            "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
            "AND valid_from_utc<=? AND valid_until_utc>? "
            "ORDER BY generation DESC LIMIT 2",
            (
                station_id,
                system_id,
                season_bin,
                training_lead_bucket,
                system_run_cycle,
                _json({system_id: system_run_cycle}),
                content_sha256({system_id: system_run_cycle}),
                decision_at_utc,
                decision_at_utc,
            ),
        ).fetchall()
        if (
            not system_activation
            or (
                len(system_activation) > 1
                and int(system_activation[0][3])
                == int(system_activation[1][3])
            )
        ):
            raise ContractError(
                "FAIL_CLOSED_NO_P0:"
                "native_system_activation_not_exact"
            )
        system_artifact = conn.execute(
            "SELECT bias_parameters_json,spread_parameters_json,"
            "reliability_parameters_json,content_sha256,run_cycle "
            "FROM calibration_native_ensemble_system_artifact "
            "WHERE system_artifact_id=? AND ensemble_system_id=? "
            "AND valid_from_utc<=? AND valid_until_utc>?",
            (
                str(system_activation[0][1]),
                system_id,
                decision_at_utc,
                decision_at_utc,
            ),
        ).fetchone()
        if (
            system_artifact is None
            or str(system_artifact[4]) != system_run_cycle
        ):
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_system_artifact_cycle_mismatch"
            )
        reliability_parameters = json.loads(str(system_artifact[2]))
        if not official_snapshot:
            source_scopes = reliability_parameters.get(
                "source_authority_scopes"
            )
            if not isinstance(source_scopes, list):
                raise ContractError(
                    "FAIL_CLOSED_NO_P0:"
                    "aggregator_fallback_strict_oos_proof_missing"
                )
            aggregator_proof = {
                "transport_provider_id": reliability_parameters.get(
                    "transport_provider_id"
                ),
                "holdout_source_class": reliability_parameters.get(
                    "holdout_source_class"
                ),
                "oos_event_temporal_scope": reliability_parameters.get(
                    "oos_event_temporal_scope"
                ),
                "future_event_count": reliability_parameters.get(
                    "future_event_count", 0
                ),
                "oos_min_train_days": reliability_parameters.get(
                    "oos_min_train_days"
                ),
                "oos_min_holdout_days": reliability_parameters.get(
                    "oos_min_holdout_days"
                ),
                "source_authority_scopes": sorted(
                    str(value) for value in source_scopes
                ),
            }
            if (
                aggregator_proof["transport_provider_id"] != "OPEN_METEO"
                or aggregator_proof["holdout_source_class"]
                != "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"
                or aggregator_proof["oos_event_temporal_scope"]
                != "CLOSED_HISTORICAL_AWC_TRUTH"
                or aggregator_proof["future_event_count"] != 0
                or not isinstance(
                    aggregator_proof["oos_min_train_days"], int
                )
                or not isinstance(
                    aggregator_proof["oos_min_holdout_days"], int
                )
                or aggregator_proof["oos_min_train_days"] < 1
                or aggregator_proof["oos_min_holdout_days"] < 1
            ):
                raise ContractError(
                    "FAIL_CLOSED_NO_P0:"
                    "aggregator_fallback_strict_oos_proof_missing"
                )
            aggregator_strict_oos_proof_by_system[system_id] = (
                aggregator_proof
            )
        bias_parameters = json.loads(str(system_artifact[0]))
        system_probabilities[system_id] = (
            _calibrated_system_probability(
                values=[
                    float(member["raw_tmax_c"])
                    for member in members
                ],
                intervals=intervals_raw,
                bias_parameters=bias_parameters,
                spread_parameters=json.loads(str(system_artifact[1])),
                reliability_parameters=reliability_parameters,
            )
        )
        raw_center = (
            sum(float(member["raw_tmax_c"]) for member in members)
            / len(members)
        )
        if (
            str(bias_parameters.get("correction_form") or "constant")
            == "affine"
        ):
            calibrated_system_centers_c[system_id] = (
                float(bias_parameters["affine_intercept_c"])
                + float(bias_parameters["affine_slope"]) * raw_center
            )
        else:
            calibrated_system_centers_c[system_id] = (
                raw_center + float(bias_parameters["bias_c"])
            )
        system_bias_c[system_id] = float(bias_parameters["bias_c"])
        system_artifact_shas[system_id] = str(system_artifact[3])
        system_activation_shas[system_id] = str(
            system_activation[0][2]
        )
    weight_receipt = validate_cross_native_ensemble_weights(
        system_weights=system_weights,
        systems_by_id={
            system: {
                "independence_family_id": family_by_system[system],
            }
            for system in system_ids
        },
        family_weight_caps=family_caps,
    )
    ordered = _ordered_buckets(intervals_raw)
    # The bridge is applied exactly when this artifact's holdout was scored
    # with it (the combination_method carries the tag).  Serving bridged math
    # under a pre-bridge artifact would put live probabilities outside what
    # the OOS gate measured; the plain mixture remains byte-identical for
    # those artifacts until the resident OOS rotation regenerates them.
    artifact_combination_method = str(artifact[8] or "")
    if CONSENSUS_BRIDGE_METHOD_TAG in artifact_combination_method:
        probability, consensus_bridge_receipt = bridged_cross_probability(
            system_probabilities=system_probabilities,
            system_weights=system_weights,
            system_centers_c=calibrated_system_centers_c,
            ordered_keys=ordered,
            key_centers_c=_bucket_centers(intervals_raw, ordered),
        )
    else:
        consensus_bridge_receipt = None
        probability = {
            str(bucket): sum(
                system_weights[system]
                * system_probabilities[system][str(bucket)]
                for system in system_ids
            )
            for bucket in intervals_raw
        }
        total = sum(probability.values())
        probability = {
            bucket: value / total
            for bucket, value in probability.items()
        }
    # The basket anchor is where the models actually point, not whichever
    # bucket holds the most mass.  An open-ended edge bucket ("35°C or higher")
    # covers an unbounded interval, so it accumulates the entire upper tail of
    # every system: with ZUUU's 3.5°C system disagreement (ECMWF 30.87 vs GEFS
    # 34.39) that bucket carried 0.3344 and won an argmax over 1°C buckets --
    # outside the model envelope of 31°C..34°C, which the two centers define.
    # Weighting the calibrated centers by the same system weights used for the
    # probability mixture gives one temperature, and the bucket containing it
    # is the anchor.  An edge bucket is still eligible; it simply has to be
    # where the models point (centers of 36/37 land there legitimately).
    model_center_c = sum(
        system_weights[system] * calibrated_system_centers_c[system]
        for system in system_ids
    )
    model_center_bucket = _bucket_for_value(model_center_c, intervals_raw)
    lower_center_c = min(calibrated_system_centers_c.values())
    upper_center_c = max(calibrated_system_centers_c.values())
    model_envelope_lower_bucket = _bucket_for_value(
        lower_center_c,
        intervals_raw,
    )
    model_envelope_upper_bucket = _bucket_for_value(
        upper_center_c,
        intervals_raw,
    )
    active_run_cycle_scope = (
        "PER_SYSTEM_NATIVE_CYCLE"
        if official_snapshot and run_cycle == MIXED_NATIVE_CYCLES
        else (
            "SAME_NATIVE_CYCLE"
            if official_snapshot
            else official_lineage["run_cycle_scope"]
        )
    )
    native_forward_lineage = {
        "run_cycle_scope": active_run_cycle_scope,
        "run_cycle": run_cycle,
        "run_cycle_by_system": {
            system_id: official_lineage["run_cycle_by_system"][system_id]
            for system_id in system_ids
        },
        "run_init_utc_by_system": {
            system_id: official_lineage["run_init_utc_by_system"][system_id]
            for system_id in system_ids
            if system_id in official_lineage["run_init_utc_by_system"]
        },
        "archive_query_at_utc_by_system": {
            system_id: official_lineage[
                "archive_query_at_utc_by_system"
            ][system_id]
            for system_id in system_ids
            if system_id in official_lineage[
                "archive_query_at_utc_by_system"
            ]
        },
        "availability_evidence": official_lineage[
            "availability_evidence"
        ],
        "source_class": forward_packet.get("source_class"),
        "source_archive_set_sha256": forward_packet.get(
            "source_archive_set_sha256"
        ),
        "source_archives": forward_packet.get("source_archives", []),
        "transport_lineage_class": (
            "official_native"
            if official_snapshot
            else "aggregator_republisher"
        ),
        "native_arrival_index_eligible": official_snapshot,
    }
    # Open-Meteo is never allowed to claim native publication/arrival.  When
    # its packet is backed by an activated strict-OOS cross-ensemble artifact
    # with at least two independent families, however, it may serve as the
    # explicit no-live fallback if no official packet is available.  Keep this
    # decision separate from the native transport label so downstream inbox
    # selection can prefer official evidence without silently discarding a
    # governed fallback candidate.
    aggregator_fallback_p0_eligible = bool(
        not official_snapshot
        and len(system_ids) >= 2
        and len(set(family_by_system.values())) >= 2
        and int(artifact[2]) >= 2
        and bool(selection_sha)
        and bool(activation_sha)
    )
    native_forward_lineage[
        "aggregator_fallback_p0_eligible"
    ] = aggregator_fallback_p0_eligible
    native_forward_lineage[
        "republisher_fallback_p0_eligible"
    ] = aggregator_fallback_p0_eligible
    if not official_snapshot:
        native_forward_lineage[
            "aggregator_strict_oos_proof_by_system"
        ] = aggregator_strict_oos_proof_by_system
    native_forward_lineage_sha = content_sha256(
        native_forward_lineage
    )
    evidence_identity = {
        "forward_snapshot_content_sha256": forward_packet[
            "snapshot_content_sha256"
        ],
        "native_forward_lineage": native_forward_lineage,
        "native_forward_lineage_sha256": native_forward_lineage_sha,
        "cross_calibration_scope_run_cycle": cross_artifact_run_cycle,
        "cross_artifact_sha256": cross_artifact_sha,
        "selection_evidence_sha256": selection_sha,
        "cross_activation_sha256": activation_sha,
        "system_artifact_shas": system_artifact_shas,
        "system_activation_shas": system_activation_shas,
        "system_probabilities": system_probabilities,
        "calibrated_system_centers_c": calibrated_system_centers_c,
        "system_weights": system_weights,
        "family_weights": weight_receipt["family_weights"],
        "observed_ensemble_system_ids": observed_system_ids,
        "excluded_observed_system_ids": excluded_observed_system_ids,
        "excluded_observed_system_reason": (
            "NO_EXACT_OOS_ACTIVATION"
        ),
        "probabilities": probability,
    }
    evidence_sha = content_sha256(evidence_identity)
    bridge_sha = content_sha256({
        "bucket_intervals": dict(intervals_raw),
        "settlement_definition": settlement_definition,
        "settlement_timezone": timezone_name,
    })
    taf_compatibility_identity = {
        "schema": TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "station_id": station_id,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        # Both leads, for the same reason the lineage carries both: the
        # calendar label identifies the decision to an operator, while a TAF
        # artifact is scoped by the grouping its evidence was fitted in.  A
        # fingerprint naming only the calendar label could never bind an
        # artifact trained under the hour buckets.
        "training_lead_bucket": training_lead_bucket,
        "run_cycle": run_cycle,
        "run_cycle_scope": active_run_cycle_scope,
        "run_cycle_by_system": native_forward_lineage[
            "run_cycle_by_system"
        ],
        "run_init_utc_by_system": native_forward_lineage[
            "run_init_utc_by_system"
        ],
        "archive_query_at_utc_by_system": native_forward_lineage[
            "archive_query_at_utc_by_system"
        ],
        "availability_evidence": official_lineage[
            "availability_evidence"
        ],
        "source_archive_set_sha256": forward_packet.get(
            "source_archive_set_sha256"
        ),
        "settlement_definition": settlement_definition,
        "available_ensemble_system_set_hash": available_set_hash,
        "ensemble_system_ids": system_ids,
        "independence_family_ids": sorted(
            set(family_by_system.values())
        ),
        "system_family_ids": family_by_system,
        "resolved_system_weights": system_weights,
        "resolved_family_weights": weight_receipt["family_weights"],
        "family_weight_caps": family_caps,
        "cross_ensemble_artifact_sha256": cross_artifact_sha,
        "cross_calibration_scope_run_cycle": cross_artifact_run_cycle,
        "cross_selection_evidence_sha256": selection_sha,
        "cross_activation_sha256": activation_sha,
        "system_calibration_artifacts": {
            system_id: {
                "artifact_content_sha256": system_artifact_shas[
                    system_id
                ],
                "activation_content_sha256": system_activation_shas[
                    system_id
                ],
                "run_cycle": native_forward_lineage[
                    "run_cycle_by_system"
                ][system_id],
                "run_init_utc": native_forward_lineage[
                    "run_init_utc_by_system"
                ].get(system_id),
            }
            for system_id in system_ids
        },
        "native_forward_lineage": native_forward_lineage,
        "native_forward_lineage_sha256": native_forward_lineage_sha,
    }
    taf_compatibility = {
        **taf_compatibility_identity,
        "fingerprint_sha256": content_sha256(
            taf_compatibility_identity
        ),
    }
    posterior_sha = content_sha256({
        "station_id": station_id,
        "event_date": event_date,
        "decision_at_utc": decision_at_utc,
        "probabilities": probability,
        "native_probability_evidence_sha256": evidence_sha,
    })
    checkpoint_identity = {
        "station_id": station_id,
        "event_date": event_date,
        "decision_at_utc": decision_at_utc,
        "posterior_sha256": posterior_sha,
    }
    checkpoint_id = (
        "p0-native-" + content_sha256(checkpoint_identity)[:32]
    )
    root_future_curve = None
    if official_snapshot and all(
        isinstance(systems[system_id].get("family_temperature_curve"), Mapping)
        for system_id in system_ids
    ):
        family_curves: dict[str, dict[str, Any]] = {}
        for system_id in system_ids:
            raw_curve = dict(systems[system_id]["family_temperature_curve"])
            sealed_curve = build_native_family_temperature_curve(raw_curve)
            if (
                raw_curve.get("content_sha256")
                != sealed_curve["content_sha256"]
                or sealed_curve["ensemble_system_id"] != system_id
                or sealed_curve["independence_family_id"]
                != family_by_system[system_id]
                or sealed_curve["station_id"] != station_id
                or sealed_curve["event_date"] != event_date
                or sealed_curve["settlement_timezone"] != timezone_name
                or datetime.fromisoformat(
                    sealed_curve["available_at_utc"].replace("Z", "+00:00")
                ) > decision
            ):
                raise ContractError(
                    "native_ensemble_p0_family_temperature_curve_invalid"
                )
            family_curves[system_id] = sealed_curve
        common_times = set.intersection(*(
            {point["valid_time_utc"] for point in curve["points"]}
            for curve in family_curves.values()
        ))
        if len(common_times) >= 4:
            values_by_system = {
                system_id: {
                    point["valid_time_utc"]: float(point["temperature_c"])
                    for point in curve["points"]
                }
                for system_id, curve in family_curves.items()
            }
            curve_source_identity = {
                "family_curve_sha256_by_system": {
                    system_id: family_curves[system_id]["content_sha256"]
                    for system_id in system_ids
                },
                "system_weights": system_weights,
                "system_bias_c": system_bias_c,
            }
            root_future_curve = build_root_future_temperature_curve({
                "station_id": station_id,
                "event_date": event_date,
                "settlement_timezone": timezone_name,
                "root_checkpoint_id": checkpoint_id,
                "root_probability_sha256": posterior_sha,
                "available_at_utc": max(
                    curve["available_at_utc"]
                    for curve in family_curves.values()
                ),
                "source_snapshot_sha256": content_sha256(
                    curve_source_identity
                ),
                "independence_family_ids": sorted(set(
                    family_by_system.values()
                )),
                "points": [
                    {
                        "valid_time_utc": valid_time,
                        "temperature_c": sum(
                            system_weights[system_id]
                            * (
                                values_by_system[system_id][valid_time]
                                + system_bias_c[system_id]
                            )
                            for system_id in system_ids
                        ),
                    }
                    for valid_time in sorted(common_times)
                ],
            })
    lineage = {
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "p0_probability_pathway": P0_PROBABILITY_PATHWAY,
        "native_authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": timezone_name,
        "settlement_definition": settlement_definition,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        # The grouping the weights came from, recorded separately because it no
        # longer equals the calendar label above.  Without it a plan cannot be
        # traced back to the fit that priced it.
        "training_lead_bucket": training_lead_bucket,
        "run_cycle": run_cycle,
        "run_cycle_scope": active_run_cycle_scope,
        "run_cycle_by_system_json": _json(
            native_forward_lineage["run_cycle_by_system"]
        ),
        "run_cycle_by_system_sha256": content_sha256(
            native_forward_lineage["run_cycle_by_system"]
        ),
        "run_init_utc_by_system_json": _json(
            native_forward_lineage["run_init_utc_by_system"]
        ),
        "archive_query_at_utc_by_system_json": _json(
            native_forward_lineage["archive_query_at_utc_by_system"]
        ),
        "native_forward_availability_evidence_json": _json(
            official_lineage["availability_evidence"]
        ),
        "native_forward_source_archive_set_sha256": str(
            forward_packet.get("source_archive_set_sha256")
            or "NOT_PROVIDED_BY_FORWARD_TRANSPORT"
        ),
        "native_forward_lineage_sha256": native_forward_lineage_sha,
        "cross_calibration_scope_run_cycle": cross_artifact_run_cycle,
        "bucket_intervals_c_json": _json(dict(intervals_raw)),
        "ensemble_system_ids_json": _json(system_ids),
        "observed_ensemble_system_ids_json": _json(
            observed_system_ids
        ),
        "excluded_observed_system_ids_json": _json(
            excluded_observed_system_ids
        ),
        "excluded_observed_system_reason": (
            "NO_EXACT_OOS_ACTIVATION"
        ),
        "independence_family_ids_json": _json(sorted(
            set(family_by_system.values())
        )),
        "system_family_ids_json": _json(family_by_system),
        "system_weights_json": _json(system_weights),
        "family_weight_caps_json": _json(family_caps),
        "family_weights_json": _json(
            weight_receipt["family_weights"]
        ),
        "system_artifact_shas_json": _json(system_artifact_shas),
        "system_activation_shas_json": _json(
            system_activation_shas
        ),
        "native_cross_ensemble_artifact_sha256":
            cross_artifact_sha,
        "native_selection_evidence_sha256": selection_sha,
        "native_cross_activation_sha256": activation_sha,
        "native_probability_evidence_sha256": evidence_sha,
        "native_forward_snapshot_sha256": str(
            forward_packet["snapshot_content_sha256"]
        ),
        "native_transport_provider": (
            (
                "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT"
                if official_lineage["mixed"]
                else "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT"
            )
            if official_snapshot
            else "OPEN_METEO"
        ),
        "aggregator_fallback_p0_eligible": (
            "true" if aggregator_fallback_p0_eligible else "false"
        ),
        "republisher_fallback_p0_eligible": (
            "true" if aggregator_fallback_p0_eligible else "false"
        ),
        "transport_lineage_class": (
            "official_native"
            if official_snapshot
            else "aggregator_republisher"
        ),
        "native_arrival_index_eligible": (
            "true" if official_snapshot else "false"
        ),
        "native_formal_p0_eligible": "false",
        "model_envelope_lower_bucket": model_envelope_lower_bucket,
        "model_envelope_upper_bucket": model_envelope_upper_bucket,
        "model_envelope_span_c": (
            f"{upper_center_c - lower_center_c:.12g}"
        ),
        "model_center_bucket": model_center_bucket,
        "model_center_c": f"{model_center_c:.12g}",
        # What the consensus bridge did to this packet, or absent when the
        # serving artifact predates the bridge and the plain mixture ran.
        **({"consensus_bridge_json": json.dumps(
            consensus_bridge_receipt, sort_keys=True
        )} if consensus_bridge_receipt is not None else {}),
        "native_calibrated_system_centers_c_json": _json(
            calibrated_system_centers_c
        ),
        "taf_nwp_live_compatibility_fingerprint_sha256": (
            taf_compatibility["fingerprint_sha256"]
        ),
        "taf_nwp_live_compatibility_json": _json(
            taf_compatibility
        ),
    }
    # TTL belongs to the evidence receipt, not to the moment a caller happens
    # to compile it.  Otherwise a nearly expired packet could be recompiled
    # repeatedly and made arbitrarily fresh without a new model arrival.
    fresh_until = (
        captured + timedelta(seconds=fresh_seconds)
    ).isoformat().replace("+00:00", "Z")
    if root_future_curve is not None:
        lineage.update({
            "conditional_future_temperature_curve_json": _json(
                root_future_curve.to_dict()
            ),
            "conditional_future_temperature_curve_sha256": (
                root_future_curve.content_sha256
            ),
            "conditional_future_temperature_curve_root_stage": "P0",
            "conditional_future_temperature_curve_root_checkpoint_id": (
                checkpoint_id
            ),
            "conditional_future_temperature_curve_root_probability_sha256": (
                posterior_sha
            ),
        })
    envelope = ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": checkpoint_id,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until,
        "bucket_ordinals": {
            bucket: index for index, bucket in enumerate(ordered)
        },
        "probabilities": probability,
        "posterior_version": POSTERIOR_VERSION,
        "posterior_sha256": posterior_sha,
        "bridge_sha256": bridge_sha,
        "lineage": lineage,
    })
    packet = {
        "stage": "P0",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until,
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "native_probability_envelope": envelope.to_dict(),
    }
    bucket_centers_c = {}
    for bucket, bounds in intervals_raw.items():
        lower, upper = bounds
        if lower is None:
            bucket_centers_c[str(bucket)] = float(upper) - 0.5
        elif upper is None:
            bucket_centers_c[str(bucket)] = float(lower) + 0.5
        else:
            bucket_centers_c[str(bucket)] = (
                float(lower) + float(upper)
            ) / 2.0
    trained_through_event_date = str(selection[5])
    if trained_through_event_date >= event_date:
        raise ContractError(
            "native_ensemble_forward_prediction_training_leakage"
        )
    forward_prediction_identity = {
        "cross_ensemble_artifact_id": artifact_id,
        "selection_evidence_sha256": selection_sha,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "run_cycle_scope": active_run_cycle_scope,
        "run_cycle_by_system_json": _json(
            native_forward_lineage["run_cycle_by_system"]
        ),
        "run_cycle_by_system_sha256": content_sha256(
            native_forward_lineage["run_cycle_by_system"]
        ),
        "run_init_utc_by_system_json": _json(
            native_forward_lineage["run_init_utc_by_system"]
        ),
        "archive_query_at_utc_by_system_json": _json(
            native_forward_lineage["archive_query_at_utc_by_system"]
        ),
        "native_forward_lineage_sha256": native_forward_lineage_sha,
        "settlement_definition": settlement_definition,
        "trained_through_event_date": trained_through_event_date,
        "decision_at_utc": decision_at_utc,
        "ensemble_system_ids_json": _json(system_ids),
        "independence_family_ids_json": _json(sorted(
            set(family_by_system.values())
        )),
        "system_family_ids_json": _json(family_by_system),
        "system_weights_json": _json(system_weights),
        "family_weight_caps_json": _json(family_caps),
        "system_member_set_shas_json": _json({
            system_id: str(
                systems[system_id].get("member_set_sha256")
                or content_sha256([
                    {
                        "member_id": str(member["member_id"]),
                        "member_role": str(member["member_role"]),
                        "raw_tmax_c": float(member["raw_tmax_c"]),
                    }
                    for member in systems[system_id]["members"]
                ])
            )
            for system_id in system_ids
        }),
        "probabilities_json": _json(probability),
        "bucket_centers_c_json": _json(bucket_centers_c),
        "prediction_oos": 1,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    forward_prediction_sha = content_sha256(
        forward_prediction_identity
    )
    forward_prediction = {
        "prediction_id": (
            "native-forward-prediction-"
            + forward_prediction_sha[:24]
        ),
        **forward_prediction_identity,
        "content_sha256": forward_prediction_sha,
    }
    return {
        "schema": SCHEMA,
        "packet": packet,
        "packet_content_sha256": content_sha256(packet),
        "source_class": "calibrated_native_cross_ensemble",
        "activation_id": activation_id,
        "cross_ensemble_artifact_id": artifact_id,
        "selection_evidence_sha256": selection_sha,
        "forward_prediction": forward_prediction,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


def persist_native_ensemble_forward_prediction(
    conn: sqlite3.Connection,
    compiled: Mapping[str, Any],
) -> dict[str, Any]:
    """Append the first strict-forward P0 fact for one artifact/day."""

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError(
            "native_ensemble_forward_prediction_schema_missing"
        )
    raw = compiled.get("forward_prediction")
    if not isinstance(raw, Mapping):
        raise ContractError(
            "native_ensemble_forward_prediction_missing"
        )
    row = dict(raw)
    identity = {
        key: value
        for key, value in row.items()
        if key not in {"prediction_id", "content_sha256"}
    }
    expected_sha = content_sha256(identity)
    if (
        row.get("content_sha256") != expected_sha
        or row.get("prediction_id")
        != "native-forward-prediction-" + expected_sha[:24]
        or row.get("prediction_oos") != 1
        or row.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY"
    ):
        raise ContractError(
            "native_ensemble_forward_prediction_invalid"
        )
    existing = conn.execute(
        "SELECT prediction_id,content_sha256,decision_at_utc "
        "FROM calibration_cross_native_ensemble_oos_prediction "
        "WHERE cross_ensemble_artifact_id=? AND station_id=? "
        "AND event_date=? "
        "ORDER BY decision_at_utc,prediction_id LIMIT 1",
        (
            row["cross_ensemble_artifact_id"],
            row["station_id"],
            row["event_date"],
        ),
    ).fetchone()
    if existing is not None:
        return {
            "state": "EXISTING_FIRST_FORWARD_PREDICTION",
            "prediction_id": str(existing[0]),
            "content_sha256": str(existing[1]),
            "decision_at_utc": str(existing[2]),
            "wrote_count": 0,
        }
    table_columns = {
        str(info[1])
        for info in conn.execute(
            "PRAGMA table_info(calibration_cross_native_ensemble_oos_prediction)"
        ).fetchall()
    }
    if not table_columns:
        raise ContractError(
            "native_ensemble_forward_prediction_table_missing"
        )
    columns = tuple(
        column for column in row if column in table_columns
    )
    conn.execute(
        "INSERT INTO calibration_cross_native_ensemble_oos_prediction("
        + ",".join(columns)
        + ") VALUES("
        + ",".join("?" for _ in columns)
        + ")",
        tuple(row[column] for column in columns),
    )
    return {
        "state": "COMMITTED",
        "prediction_id": str(row["prediction_id"]),
        "content_sha256": expected_sha,
        "decision_at_utc": str(row["decision_at_utc"]),
        "wrote_count": 1,
    }


__all__ = [
    "DEFAULT_FRESH_SECONDS",
    "POSTERIOR_VERSION",
    "SCHEMA",
    "compile_native_ensemble_p0",
    "persist_native_ensemble_forward_prediction",
]
