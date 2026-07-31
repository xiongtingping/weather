"""Compile activated calibrated native-member ensembles into P0 packets."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, ProbabilityEnvelope, content_sha256
from .native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
    validate_cross_native_ensemble_weights,
)
from .open_meteo_ensemble import SCHEMA as FORWARD_PACKET_SCHEMA
from .official_native_forward_packet import (
    MIXED_NATIVE_CYCLES,
    SCHEMA as OFFICIAL_FORWARD_PACKET_SCHEMA,
)


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
    corrected = [
        mean + bias + spread * (value - mean)
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
) -> dict[str, Any]:
    """Compile one station-day, or fail closed with ``NO_P0``."""

    packet_schema = str(forward_packet.get("schema") or "")
    if packet_schema not in {
        FORWARD_PACKET_SCHEMA,
        OFFICIAL_FORWARD_PACKET_SCHEMA,
    }:
        raise ContractError("native_ensemble_p0_forward_schema_invalid")
    if fresh_seconds < 60 or fresh_seconds > 24 * 60 * 60:
        raise ContractError("native_ensemble_p0_freshness_invalid")
    decision = datetime.fromisoformat(
        decision_at_utc.replace("Z", "+00:00")
    )
    official_snapshot = packet_schema == OFFICIAL_FORWARD_PACKET_SCHEMA
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
    captured = datetime.fromisoformat(
        captured_at.replace("Z", "+00:00")
    )
    if captured > decision:
        raise ContractError("native_ensemble_p0_future_capture_forbidden")
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
        if lead_bucket != "d2":
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
    activation_sql = (
        "SELECT a.activation_id,a.artifact_id,a.content_sha256,"
        "a.generation,a.valid_from_utc,"
        "a.available_system_set_hash_or_star,"
        "r.ensemble_system_ids_json,r.ensemble_system_count,"
        "r.run_cycle "
        "FROM calibration_native_ensemble_activation a "
        "JOIN calibration_cross_native_ensemble_artifact r "
        "ON r.cross_ensemble_artifact_id=a.artifact_id "
        "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        "AND a.station_id=? AND a.season_bin=? AND a.lead_bucket=? "
        + ("AND a.run_cycle=? " if not official_lineage["mixed"] else "")
        + "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "AND a.valid_from_utc<=? AND a.valid_until_utc>? "
        "ORDER BY a.generation DESC,a.activation_id LIMIT 256"
    )
    activation_parameters: tuple[Any, ...] = (
        station_id,
        season_bin,
        lead_bucket,
    )
    if not official_lineage["mixed"]:
        activation_parameters += (run_cycle,)
    activation_parameters += (decision_at_utc, decision_at_utc)
    activation_rows = conn.execute(
        activation_sql,
        activation_parameters,
    ).fetchall()
    observed_set = set(observed_system_ids)
    compatible_activations: list[
        tuple[Any, list[str]]
    ] = []
    for row in activation_rows:
        selected_ids = sorted(
            str(value) for value in json.loads(str(row[6]))
        )
        if (
            len(selected_ids) != int(row[7])
            or len(set(selected_ids)) != len(selected_ids)
            or str(row[5]) != content_sha256(selected_ids)
            or not set(selected_ids).issubset(observed_set)
        ):
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
    if (
        not activations
        or (
            len(activations) > 1
            and len(activations[0][1]) == len(activations[1][1])
            and int(activations[0][0][3])
            == int(activations[1][0][3])
        )
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
    activation_available = datetime.fromisoformat(
        str(selected_activation[4]).replace("Z", "+00:00")
    )
    decision = max(captured, activation_available)
    decision_at_utc = decision.isoformat().replace("+00:00", "Z")
    artifact = conn.execute(
        "SELECT ensemble_system_ids_json,ensemble_system_count,"
        "independent_family_count,selection_evidence_sha256,"
        "family_weight_caps_json,content_sha256 "
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
    if (
        selected_system_ids != system_ids
        or int(artifact[1]) != len(system_ids)
        or int(artifact[2]) < 2
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_cross_ensemble_mask_mismatch"
        )
    selection_sha = str(artifact[3])
    cross_artifact_sha = str(artifact[5])
    selection = conn.execute(
        "SELECT selected_weights_json,family_weights_json,"
        "family_weight_caps_json,"
        "cross_system_residual_correlation_json,content_sha256,"
        "train_end_date "
        "FROM calibration_cross_native_ensemble_selection_evidence "
        "WHERE content_sha256=?",
        (selection_sha,),
    ).fetchone()
    if selection is None:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_selection_evidence_missing"
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
    calibrated_system_centers_c: dict[str, float] = {}
    family_by_system: dict[str, str] = {}
    for system_id in system_ids:
        system_config = conn.execute(
            "SELECT independence_family_id,expected_member_count "
            "FROM calibration_native_ensemble_system "
            "WHERE ensemble_system_id=?",
            (system_id,),
        ).fetchone()
        if system_config is None:
            raise ContractError(
                "native_ensemble_p0_system_unregistered"
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
            "SELECT activation_id,artifact_id,content_sha256,generation "
            "FROM calibration_native_ensemble_activation "
            "WHERE artifact_kind="
            "'NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
            "AND station_id=? AND ensemble_system_id_or_star=? "
            "AND season_bin=? AND lead_bucket=? AND run_cycle=? "
            "AND available_system_set_hash_or_star='*' "
            "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
            "AND valid_from_utc<=? AND valid_until_utc>? "
            "ORDER BY generation DESC LIMIT 2",
            (
                station_id,
                system_id,
                season_bin,
                lead_bucket,
                system_run_cycle,
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
        system_probabilities[system_id] = (
            _calibrated_system_probability(
                values=[
                    float(member["raw_tmax_c"])
                    for member in members
                ],
                intervals=intervals_raw,
                bias_parameters=json.loads(str(system_artifact[0])),
                spread_parameters=json.loads(str(system_artifact[1])),
                reliability_parameters=json.loads(
                    str(system_artifact[2])
                ),
            )
        )
        calibrated_system_centers_c[system_id] = (
            sum(float(member["raw_tmax_c"]) for member in members)
            / len(members)
            + float(json.loads(str(system_artifact[0]))["bias_c"])
        )
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
    ordered = _ordered_buckets(intervals_raw)
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
    native_forward_lineage = {
        "run_cycle_scope": official_lineage["run_cycle_scope"],
        "run_cycle": run_cycle,
        "run_cycle_by_system": official_lineage[
            "run_cycle_by_system"
        ],
        "run_init_utc_by_system": official_lineage[
            "run_init_utc_by_system"
        ],
        "archive_query_at_utc_by_system": official_lineage[
            "archive_query_at_utc_by_system"
        ],
        "availability_evidence": official_lineage[
            "availability_evidence"
        ],
        "source_class": forward_packet.get("source_class"),
        "source_archive_set_sha256": forward_packet.get(
            "source_archive_set_sha256"
        ),
        "source_archives": forward_packet.get("source_archives", []),
    }
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
        "run_cycle": run_cycle,
        "run_cycle_scope": official_lineage["run_cycle_scope"],
        "run_cycle_by_system": official_lineage[
            "run_cycle_by_system"
        ],
        "run_init_utc_by_system": official_lineage[
            "run_init_utc_by_system"
        ],
        "archive_query_at_utc_by_system": official_lineage[
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
                "run_cycle": official_lineage[
                    "run_cycle_by_system"
                ][system_id],
                "run_init_utc": official_lineage[
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
    lineage = {
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "p0_probability_pathway": P0_PROBABILITY_PATHWAY,
        "native_authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": timezone_name,
        "settlement_definition": settlement_definition,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "run_cycle_scope": official_lineage["run_cycle_scope"],
        "run_cycle_by_system_json": _json(
            official_lineage["run_cycle_by_system"]
        ),
        "run_init_utc_by_system_json": _json(
            official_lineage["run_init_utc_by_system"]
        ),
        "archive_query_at_utc_by_system_json": _json(
            official_lineage["archive_query_at_utc_by_system"]
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
        "native_formal_p0_eligible": "false",
        "model_envelope_lower_bucket": model_envelope_lower_bucket,
        "model_envelope_upper_bucket": model_envelope_upper_bucket,
        "model_envelope_span_c": (
            f"{upper_center_c - lower_center_c:.12g}"
        ),
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
    fresh_until = (
        decision + timedelta(seconds=fresh_seconds)
    ).isoformat().replace("+00:00", "Z")
    checkpoint_identity = {
        "station_id": station_id,
        "event_date": event_date,
        "decision_at_utc": decision_at_utc,
        "posterior_sha256": posterior_sha,
    }
    envelope = ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": (
            "p0-native-" + content_sha256(checkpoint_identity)[:32]
        ),
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
        "run_cycle_scope": official_lineage["run_cycle_scope"],
        "run_cycle_by_system_json": _json(
            official_lineage["run_cycle_by_system"]
        ),
        "run_init_utc_by_system_json": _json(
            official_lineage["run_init_utc_by_system"]
        ),
        "archive_query_at_utc_by_system_json": _json(
            official_lineage["archive_query_at_utc_by_system"]
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
