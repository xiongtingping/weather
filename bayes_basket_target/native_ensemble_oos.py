"""Strict-forward OOS training for native-member ensemble P0.

The module reads only append-only member runs and canonical daily-high truth.
Open-Meteo rows remain ``PRODUCTION_NO_LIVE_CANDIDATE`` evidence.  They may
earn a ``PRODUCTION_NO_LIVE_ONLY`` activation after a real forward holdout,
but can never create ``FORMAL_PROMOTED`` authority.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    native_ensemble_family_set_hash,
    validate_cross_native_ensemble_weights,
)
from .official_native_forward_packet import MIXED_NATIVE_CYCLES
from .ssot import native_ensemble_foundation_schema_status


SCHEMA = "native_ensemble_strict_forward_oos_v1"
DEFAULT_MIN_TRAIN_DAYS = 20
DEFAULT_MIN_HOLDOUT_DAYS = 8
NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS = 2
NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS = 1
FORMAL_PROMOTION_THRESHOLD_PROFILE = "FORMAL_PROMOTION_20_TRAIN_8_HOLDOUT"
NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE = (
    "PRODUCTION_NO_LIVE_BOOTSTRAP_2_TRAIN_1_FORWARD_HOLDOUT"
)
DEFAULT_VALID_DAYS = 30
DEFAULT_FAMILY_WEIGHT_CAPS = {
    "ECMWF": 0.45,
}
OFFICIAL_ARCHIVE_SCOPE_CONTRACTS = {
    "NOAA_GEFS_05": {
        "transport_provider_id": "NOAA_GEFS_PDS_S3",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
    },
    "ECMWF_IFS_ENS_OPEN_PF50": {
        "transport_provider_id": "ECMWF_OPEN_DATA_S3",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
    },
    "ECCC_GEM_GLOBAL_ENS": {
        "transport_provider_id": "ECCC_DD_WEATHER",
        "lead_bucket": "d2",
        "run_cycle": "00Z",
    },
}
EPS = 1e-12
VALID_NATIVE_RUN_CYCLES = frozenset({
    "00Z",
    "06Z",
    "12Z",
    "18Z",
    "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
})


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


def _temperature_bucket(value: float) -> str:
    return str(math.floor(float(value) + 0.5))


def _probabilities(values: Sequence[float]) -> dict[str, float]:
    if not values:
        raise ContractError("native_ensemble_oos_values_missing")
    counts: dict[str, int] = {}
    for value in values:
        if not math.isfinite(float(value)):
            raise ContractError("native_ensemble_oos_value_invalid")
        bucket = _temperature_bucket(float(value))
        counts[bucket] = counts.get(bucket, 0) + 1
    total = len(values)
    return {
        bucket: count / total
        for bucket, count in sorted(
            counts.items(),
            key=lambda item: int(item[0]),
        )
    }


def _metrics(
    probability: Mapping[str, float],
    actual: float,
) -> dict[str, float]:
    actual_bucket = _temperature_bucket(actual)
    support = sorted({
        *(int(bucket) for bucket in probability),
        int(actual_bucket),
    })
    brier = sum(
        (
            float(probability.get(str(bucket), 0.0))
            - (1.0 if str(bucket) == actual_bucket else 0.0)
        ) ** 2
        for bucket in support
    )
    nll = -math.log(
        max(float(probability.get(actual_bucket, 0.0)), EPS)
    )
    crps = 0.0
    for bucket in range(min(support), max(support) + 1):
        forecast_cdf = sum(
            float(value)
            for key, value in probability.items()
            if int(key) <= bucket
        )
        observed_cdf = 1.0 if int(actual_bucket) <= bucket else 0.0
        crps += (forecast_cdf - observed_cdf) ** 2
    return {"brier": brier, "nll": nll, "crps": crps}


def _mean_metrics(rows: Sequence[Mapping[str, float]]) -> dict[str, float]:
    if not rows:
        raise ContractError("native_ensemble_oos_metric_rows_missing")
    return {
        metric: sum(float(row[metric]) for row in rows) / len(rows)
        for metric in ("brier", "nll", "crps")
    }


def _fit_parameters(
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not cases:
        raise ContractError("native_ensemble_oos_train_cases_missing")
    errors: list[float] = []
    spreads: list[float] = []
    climate_samples: list[float] = []
    for case in cases:
        values = [float(value) for value in case["values"]]
        mean = sum(values) / len(values)
        errors.append(float(case["actual_tmax_c"]) - mean)
        spreads.append(math.sqrt(
            sum((value - mean) ** 2 for value in values) / len(values)
        ))
        climate_samples.append(float(case["actual_tmax_c"]))
    bias = sum(errors) / len(errors)
    rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
    mean_spread = sum(spreads) / len(spreads)
    spread_factor = min(
        3.0,
        max(0.5, rmse / max(mean_spread, 0.1)),
    )
    reliability_weight = len(cases) / (len(cases) + 10.0)
    return {
        "bias_c": bias,
        "spread_factor": spread_factor,
        "reliability_weight": reliability_weight,
        "climate_samples_c": climate_samples[-180:],
        "train_rmse_c": rmse,
        "train_mean_spread_c": mean_spread,
    }


def _apply_parameters(
    values: Sequence[float],
    parameters: Mapping[str, Any],
) -> dict[str, float]:
    current = [float(value) for value in values]
    mean = sum(current) / len(current)
    bias = float(parameters["bias_c"])
    spread_factor = float(parameters["spread_factor"])
    corrected = [
        mean + bias + spread_factor * (value - mean)
        for value in current
    ]
    member_probability = _probabilities(corrected)
    climate_probability = _probabilities([
        float(value)
        for value in parameters["climate_samples_c"]
    ])
    reliability = float(parameters["reliability_weight"])
    keys = set(member_probability) | set(climate_probability)
    result = {
        key: (
            reliability * member_probability.get(key, 0.0)
            + (1.0 - reliability)
            * climate_probability.get(key, 0.0)
        )
        for key in keys
    }
    total = sum(result.values())
    if total <= 0:
        raise ContractError("native_ensemble_oos_probability_empty")
    return {
        key: value / total
        for key, value in sorted(
            result.items(),
            key=lambda item: int(item[0]),
        )
    }


def _cycle_mapping_for_rows(
    rows: Sequence[tuple[str, str, Mapping[str, Any]]],
) -> tuple[dict[str, str], str, str, str]:
    """Resolve one deterministic cycle identity for a cross candidate.

    A system may contribute only one native cycle to a candidate.  The
    returned mapping is the identity used by downstream activation lookup;
    it is never replaced by the cross label.
    """

    by_system: dict[str, set[str]] = defaultdict(set)
    for system_id, run_cycle, _evaluation in rows:
        system = str(system_id or "").strip()
        cycle = str(run_cycle or "").strip()
        if not system or cycle not in VALID_NATIVE_RUN_CYCLES:
            raise ContractError(
                "native_ensemble_oos_run_cycle_mapping_missing"
            )
        by_system[system].add(cycle)
    conflicts = sorted(
        system_id
        for system_id, cycles in by_system.items()
        if len(cycles) != 1
    )
    if conflicts:
        raise ContractError(
            "native_ensemble_oos_system_cycle_conflict"
        )
    mapping = {
        system_id: next(iter(cycles))
        for system_id, cycles in sorted(by_system.items())
    }
    if not mapping:
        raise ContractError(
            "native_ensemble_oos_run_cycle_mapping_missing"
        )
    mapping_sha = content_sha256(mapping)
    cycles = set(mapping.values())
    if len(cycles) == 1:
        run_cycle = next(iter(cycles))
        run_cycle_scope = "SAME_NATIVE_CYCLE"
    else:
        run_cycle = MIXED_NATIVE_CYCLES
        run_cycle_scope = "PER_SYSTEM_NATIVE_CYCLE"
    return mapping, mapping_sha, run_cycle, run_cycle_scope


def _candidate_cycle_mapping(candidate: Mapping[str, Any]) -> tuple[
    dict[str, str], str, str, str
]:
    raw_mapping = candidate.get("run_cycle_by_system")
    if not isinstance(raw_mapping, Mapping):
        raise ContractError(
            "native_ensemble_oos_run_cycle_mapping_missing"
        )
    rows = [
        (
            str(system_id),
            str(run_cycle),
            {},
        )
        for system_id, run_cycle in raw_mapping.items()
    ]
    mapping, mapping_sha, run_cycle, run_cycle_scope = (
        _cycle_mapping_for_rows(rows)
    )
    selected = sorted(
        str(system_id)
        for system_id in candidate.get("selected_system_ids") or []
    )
    if selected != sorted(mapping):
        raise ContractError(
            "native_ensemble_oos_run_cycle_mapping_system_mismatch"
        )
    if str(candidate.get("run_cycle") or "") != run_cycle:
        raise ContractError(
            "native_ensemble_oos_run_cycle_mapping_label_mismatch"
        )
    if str(candidate.get("run_cycle_scope") or "") != run_cycle_scope:
        raise ContractError(
            "native_ensemble_oos_run_cycle_scope_mismatch"
        )
    if str(candidate.get("run_cycle_by_system_sha256") or "") != (
        mapping_sha
    ):
        raise ContractError(
            "native_ensemble_oos_run_cycle_mapping_hash_mismatch"
        )
    return mapping, mapping_sha, run_cycle, run_cycle_scope


def _correlation(
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (a - left_mean) * (b - right_mean)
        for a, b in zip(left, right)
    )
    left_scale = math.sqrt(sum(
        (value - left_mean) ** 2 for value in left
    ))
    right_scale = math.sqrt(sum(
        (value - right_mean) ** 2 for value in right
    ))
    if left_scale <= EPS or right_scale <= EPS:
        return 0.0
    return max(-1.0, min(1.0, numerator / (left_scale * right_scale)))


def _bounded_family_weights(
    *,
    train_rmse_by_system: Mapping[str, float],
    family_by_system: Mapping[str, str],
    family_caps: Mapping[str, float],
) -> dict[str, float]:
    family_scores: dict[str, float] = defaultdict(float)
    system_scores: dict[str, float] = {}
    for system_id, rmse in train_rmse_by_system.items():
        score = 1.0 / max(float(rmse), 0.1)
        system_scores[system_id] = score
        family_scores[family_by_system[system_id]] += score
    families = sorted(family_scores)
    if len(families) < 2:
        raise ContractError("cross_native_ensemble_requires_two_families")
    raw_total = sum(family_scores.values())
    family_weights = {
        family: family_scores[family] / raw_total
        for family in families
    }
    caps = {
        family: float(family_caps.get(family, 0.75))
        for family in families
    }
    if sum(caps.values()) < 1.0 - EPS:
        raise ContractError("cross_native_ensemble_family_caps_infeasible")
    for _iteration in range(len(families) + 2):
        over = [
            family
            for family in families
            if family_weights[family] > caps[family] + EPS
        ]
        if not over:
            break
        excess = 0.0
        for family in over:
            excess += family_weights[family] - caps[family]
            family_weights[family] = caps[family]
        receivers = [
            family
            for family in families
            if family not in over
            and family_weights[family] < caps[family] - EPS
        ]
        capacity = sum(
            caps[family] - family_weights[family]
            for family in receivers
        )
        if not receivers or capacity + EPS < excess:
            raise ContractError(
                "cross_native_ensemble_family_caps_infeasible"
            )
        for family in receivers:
            family_weights[family] += excess * (
                (caps[family] - family_weights[family]) / capacity
            )
    total = sum(family_weights.values())
    family_weights = {
        family: value / total
        for family, value in family_weights.items()
    }
    system_weights: dict[str, float] = {}
    for family in families:
        members = [
            system
            for system, candidate_family in family_by_system.items()
            if candidate_family == family
        ]
        family_score = sum(system_scores[system] for system in members)
        for system in members:
            system_weights[system] = (
                family_weights[family]
                * system_scores[system]
                / family_score
            )
    validate_cross_native_ensemble_weights(
        system_weights=system_weights,
        systems_by_id={
            system: {
                "independence_family_id": family_by_system[system],
            }
            for system in system_weights
        },
        family_weight_caps=caps,
    )
    return dict(sorted(system_weights.items()))


def _load_systems(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    return {
        str(row[0]): {
            "ensemble_system_id": str(row[0]),
            "canonical_name": str(row[1]),
            "independence_family_id": str(row[2]),
            "provider_id": str(row[3]),
            "expected_member_count": int(row[4]),
            "content_sha256": str(row[5]),
        }
        for row in conn.execute(
            "SELECT ensemble_system_id,canonical_name,"
            "independence_family_id,provider_id,"
            "expected_member_count,content_sha256 "
            "FROM calibration_native_ensemble_system"
        )
    }


def _historical_transport_scope_allowed(
    *,
    system_id: str,
    transport_provider_id: str,
    lead_bucket: str,
    run_cycle: str,
) -> bool:
    official = OFFICIAL_ARCHIVE_SCOPE_CONTRACTS.get(system_id)
    if official is not None:
        return (
            transport_provider_id
            == official["transport_provider_id"]
            and lead_bucket == official["lead_bucket"]
            and run_cycle == official["run_cycle"]
        )
    return transport_provider_id == "OPEN_METEO"


def _load_cases(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    as_of_utc: str,
) -> dict[tuple[str, str, str, str], list[dict[str, Any]]]:
    station = station_id.strip().upper()
    as_of = datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        raise ContractError("native_ensemble_oos_as_of_timezone_missing")
    as_of = as_of.astimezone(timezone.utc)
    station_row = conn.execute(
        "SELECT settlement_definition "
        "FROM calibration_station_registry WHERE station_id=?",
        (station,),
    ).fetchone()
    if station_row is None:
        raise ContractError("native_ensemble_oos_station_unregistered")
    timezone_rows = conn.execute(
        "SELECT DISTINCT settlement_timezone "
        "FROM basket_target_market_registry "
        "WHERE station_id=? AND settlement_timezone<>''",
        (station,),
    ).fetchall()
    if len(timezone_rows) != 1:
        raise ContractError(
            "native_ensemble_oos_market_timezone_not_unique"
        )
    timezone_name = str(timezone_rows[0][0])
    binding_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND "
        "name='calibration_station_market_timezone_binding'"
    ).fetchone()
    if binding_table is not None:
        binding = conn.execute(
            "SELECT settlement_timezone,market_registry_timezone_count "
            "FROM calibration_station_market_timezone_binding "
            "WHERE station_id=?",
            (station,),
        ).fetchone()
        if (
            binding is not None
            and (
                str(binding[0]) != timezone_name
                or int(binding[1]) != 1
            )
        ):
            raise ContractError(
                "native_ensemble_oos_market_timezone_binding_mismatch"
            )
    settlement_definition = str(station_row[0])
    systems = _load_systems(conn)
    truth = {
        str(row[0]): {
            "actual_tmax_c": float(row[1]),
            "canonical_committed_at_utc": str(row[2]),
        }
        for row in conn.execute(
            "SELECT local_date,official_tmax_c,"
            "canonical_committed_at_utc "
            "FROM calibration_station_daily_high_truth "
            "WHERE station_id=? AND qc_status='ACCEPTED' "
            "AND authority_scope IN "
            "('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')",
            (station,),
        )
    }
    grouped: dict[
        tuple[str, str, str, str, str],
        list[tuple[str, float, str, bool, str, str]],
    ] = defaultdict(list)
    for row in conn.execute(
        "SELECT ensemble_system_id,target_local_date,lead_bucket,"
        "run_cycle,aggregator_snapshot_at_utc,member_id,raw_tmax_c,"
        "content_sha256 "
        "FROM calibration_native_ensemble_member_run "
        "WHERE station_id=? AND qc_status='ACCEPTED' "
        "AND authority_scope='PRODUCTION_NO_LIVE_CANDIDATE' "
        "AND transport_provider_id='OPEN_METEO' "
        "AND ensemble_system_id NOT IN "
        "('NOAA_GEFS_05','ECMWF_IFS_ENS_OPEN_PF50',"
        "'ECCC_GEM_GLOBAL_ENS') "
        "ORDER BY target_local_date,aggregator_snapshot_at_utc,member_id",
        (station,),
    ):
        run_cycle = str(row[3] or "").strip()
        if run_cycle not in VALID_NATIVE_RUN_CYCLES:
            raise ContractError(
                "native_ensemble_oos_run_cycle_mapping_missing"
            )
        grouped[
            (
                str(row[0]),
                str(row[1]),
                str(row[2]),
                run_cycle,
                str(row[4]),
            )
        ].append((
            str(row[5]),
            float(row[6]),
            str(row[7]),
            True,
            "FORWARD_LOCAL_RECEIPT",
            "OPEN_METEO",
        ))
    historical_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND "
        "name='calibration_native_ensemble_historical_member_run'"
    ).fetchone()
    if historical_table is not None:
        for row in conn.execute(
            "SELECT ensemble_system_id,target_local_date,"
            "archive_lead_bucket,run_cycle,archive_query_at_utc,"
            "member_id,raw_tmax_c,content_sha256,"
            "transport_provider_id,native_run_init_utc,"
            "offline_oos_eligible "
            "FROM calibration_native_ensemble_historical_member_run "
            "WHERE station_id=? AND qc_status='ACCEPTED' "
            "AND authority_scope='CALIBRATION_TRAIN_ONLY' "
            "AND holdout_eligible=0 "
            "AND transport_provider_id IN "
            "('OPEN_METEO','NOAA_GEFS_PDS_S3',"
            "'ECMWF_OPEN_DATA_S3','ECCC_DD_WEATHER') "
            "AND (transport_provider_id='OPEN_METEO' "
            "OR archive_lead_bucket='d2') "
            "ORDER BY target_local_date,archive_query_at_utc,member_id",
            (station,),
        ):
            run_cycle = str(row[3] or "").strip()
            if run_cycle not in VALID_NATIVE_RUN_CYCLES:
                raise ContractError(
                    "native_ensemble_oos_run_cycle_mapping_missing"
                )
            if not _historical_transport_scope_allowed(
                system_id=str(row[0]),
                transport_provider_id=str(row[8]),
                lead_bucket=str(row[2]),
                run_cycle=run_cycle,
            ):
                continue
            offline_oos_eligible = (
                int(row[10]) == 1
                and str(row[8])
                in {
                    "NOAA_GEFS_PDS_S3",
                    "ECMWF_OPEN_DATA_S3",
                    "ECCC_DD_WEATHER",
                }
                and bool(str(row[9] or "").strip())
            )
            decision_at = (
                str(row[9])
                if offline_oos_eligible
                else str(row[4])
            )
            grouped[
                (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    run_cycle,
                    decision_at,
                )
            ].append((
                str(row[5]),
                float(row[6]),
                str(row[7]),
                offline_oos_eligible,
                (
                    "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE"
                    if offline_oos_eligible
                    else "RETROSPECTIVE_ARCHIVE_TRAIN_ONLY"
                ),
                str(row[8]),
            ))
    by_scope_day: dict[
        tuple[str, str, str, str, str],
        dict[str, Any],
    ] = {}
    tz = ZoneInfo(timezone_name)
    for (
        system_id,
        day,
        lead_bucket,
        run_cycle,
        snapshot_at,
    ), member_rows in grouped.items():
        if system_id not in systems or day not in truth:
            continue
        expected = systems[system_id]["expected_member_count"]
        member_ids = {
            member
            for member, _value, _sha256, _holdout, _class, _transport
            in member_rows
        }
        if len(member_rows) != expected or len(member_ids) != expected:
            continue
        snapshot = datetime.fromisoformat(
            snapshot_at.replace("Z", "+00:00")
        )
        day_end = (
            datetime.fromisoformat(day)
            .replace(tzinfo=tz)
            + timedelta(days=1)
        ).astimezone(timezone.utc)
        truth_committed = datetime.fromisoformat(
            truth[day]["canonical_committed_at_utc"].replace(
                "Z",
                "+00:00",
            )
        )
        if (
            snapshot.astimezone(timezone.utc) > as_of
            or truth_committed.astimezone(timezone.utc) > as_of
        ):
            continue
        oos_eligible = all(
            bool(row[3]) for row in member_rows
        )
        evidence_classes = {
            str(row[4]) for row in member_rows
        }
        if len(evidence_classes) != 1:
            continue
        evidence_timing_class = next(iter(evidence_classes))
        transport_ids = {
            str(row[5]) for row in member_rows
        }
        if len(transport_ids) != 1:
            continue
        transport_provider_id = next(iter(transport_ids))
        if (
            evidence_timing_class == "FORWARD_LOCAL_RECEIPT"
            and (
            snapshot >= day_end or snapshot >= truth_committed
            )
        ):
            continue
        scope_day = (
            system_id,
            _season_bin(day),
            lead_bucket,
            run_cycle,
            day,
        )
        existing = by_scope_day.get(scope_day)
        if (
            existing is not None
            and (
                bool(existing["holdout_eligible"])
                or not oos_eligible
            )
        ):
            continue
        by_scope_day[scope_day] = {
            "event_date": day,
            "aggregator_snapshot_at_utc": snapshot_at,
            "values": [
                value
                for _member, value, _sha256, _holdout, _class, _transport
                in sorted(member_rows)
            ],
            "member_run_content_shas": sorted(
                sha256
                for _member, _value, sha256, _holdout, _class, _transport
                in member_rows
            ),
            "member_set_sha256": content_sha256(sorted(
                sha256
                for _member, _value, sha256, _holdout, _class, _transport
                in member_rows
            )),
            "holdout_eligible": oos_eligible,
            "evidence_timing_class": evidence_timing_class,
            "transport_provider_id": transport_provider_id,
            "actual_tmax_c": truth[day]["actual_tmax_c"],
            "settlement_definition": settlement_definition,
            "timezone_name": timezone_name,
        }
    result: dict[
        tuple[str, str, str, str],
        list[dict[str, Any]],
    ] = defaultdict(list)
    for (
        system_id,
        season_bin,
        lead_bucket,
        run_cycle,
        _day,
    ), case in by_scope_day.items():
        result[
            (system_id, season_bin, lead_bucket, run_cycle)
        ].append(case)
    for cases in result.values():
        cases.sort(key=lambda item: item["event_date"])
    return dict(result)


def _evaluate_system(
    *,
    system_id: str,
    cases: Sequence[Mapping[str, Any]],
    min_train_days: int,
    min_holdout_days: int,
) -> dict[str, Any]:
    transports = {
        str(case.get("transport_provider_id") or "")
        for case in cases
    }
    if len(transports) != 1 or "" in transports:
        raise ContractError(
            "native_ensemble_oos_transport_scope_mixed"
        )
    transport_provider_id = next(iter(transports))
    forward = [
        case for case in cases
        if case.get("holdout_eligible") is True
    ]
    if len(forward) < min_holdout_days:
        return {
            "state": "ACCUMULATING_STRICT_OOS",
            "system_id": system_id,
            "transport_provider_id": transport_provider_id,
            "case_count": len(cases),
            "training_case_count": len(cases) - len(forward),
            "strict_holdout_case_count": len(forward),
            "required_train_case_count": min_train_days,
            "required_strict_holdout_case_count": min_holdout_days,
            "missing_train_case_count": max(
                0, min_train_days - (len(cases) - len(forward))
            ),
            "missing_strict_holdout_case_count": (
                min_holdout_days - len(forward)
            ),
        }
    holdout = list(forward[-min_holdout_days:])
    holdout_start = str(holdout[0]["event_date"])
    first_holdout_decision = min(
        str(case["aggregator_snapshot_at_utc"])
        for case in holdout
    )
    train = [
        case
        for case in cases
        if str(case["event_date"]) < holdout_start
        and str(case["aggregator_snapshot_at_utc"])
        < first_holdout_decision
    ]
    if len(train) < min_train_days:
        return {
            "state": "ACCUMULATING_STRICT_OOS",
            "system_id": system_id,
            "transport_provider_id": transport_provider_id,
            "case_count": len(cases),
            "training_case_count": len(train),
            "strict_holdout_case_count": len(holdout),
            "required_train_case_count": min_train_days,
            "required_strict_holdout_case_count": min_holdout_days,
            "missing_train_case_count": min_train_days - len(train),
            "missing_strict_holdout_case_count": 0,
        }
    parameters = _fit_parameters(train)
    predictions = []
    for case in holdout:
        probability = _apply_parameters(case["values"], parameters)
        predictions.append({
            "event_date": case["event_date"],
            "decision_at_utc": case["aggregator_snapshot_at_utc"],
            "member_set_sha256": case["member_set_sha256"],
            "member_run_content_shas": case[
                "member_run_content_shas"
            ],
            "probability": probability,
            "actual_tmax_c": float(case["actual_tmax_c"]),
            "metrics": _metrics(
                probability,
                float(case["actual_tmax_c"]),
            ),
        })
    return {
        "state": "OOS_READY",
        "system_id": system_id,
        "transport_provider_id": transport_provider_id,
        "case_count": len(cases),
        "train_start_date": train[0]["event_date"],
        "train_end_date": train[-1]["event_date"],
        "holdout_start_date": holdout[0]["event_date"],
        "holdout_end_date": holdout[-1]["event_date"],
        "train_n": len(train),
        "holdout_n": len(holdout),
        "train_source_counts": {
            "RETROSPECTIVE_ARCHIVE_TRAIN_ONLY": sum(
                case.get("evidence_timing_class")
                == "RETROSPECTIVE_ARCHIVE_TRAIN_ONLY"
                for case in train
            ),
            "FORWARD_LOCAL_RECEIPT": sum(
                case.get("evidence_timing_class")
                == "FORWARD_LOCAL_RECEIPT"
                for case in train
            ),
            "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE": sum(
                case.get("evidence_timing_class")
                == "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE"
                for case in train
            ),
        },
        "holdout_source_class": (
            next(iter({
                str(case["evidence_timing_class"])
                for case in holdout
            }))
            if len({
                str(case["evidence_timing_class"])
                for case in holdout
            }) == 1
            else "MIXED_STRICT_CHRONOLOGICAL"
        ),
        "parameters": parameters,
        "predictions": predictions,
        "oos_metrics": _mean_metrics([
            row["metrics"] for row in predictions
        ]),
    }


def build_native_ensemble_oos(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    as_of_utc: str,
    min_train_days: int = DEFAULT_MIN_TRAIN_DAYS,
    min_holdout_days: int = DEFAULT_MIN_HOLDOUT_DAYS,
    family_weight_caps: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Build candidate artifacts; perform no writes."""

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
    if min_train_days < 2 or min_holdout_days < 1:
        raise ContractError("native_ensemble_oos_window_invalid")
    as_of = datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        raise ContractError("native_ensemble_oos_as_of_timezone_missing")
    if (
        min_train_days == NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS
        and min_holdout_days == NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS
    ):
        threshold_profile = NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE
    elif (
        min_train_days == DEFAULT_MIN_TRAIN_DAYS
        and min_holdout_days == DEFAULT_MIN_HOLDOUT_DAYS
    ):
        threshold_profile = FORMAL_PROMOTION_THRESHOLD_PROFILE
    else:
        threshold_profile = "CUSTOM_STRICT_CHRONOLOGICAL_OOS"
    station = station_id.strip().upper()
    systems = _load_systems(conn)
    cases_by_scope = _load_cases(
        conn,
        station_id=station,
        as_of_utc=as_of_utc,
    )
    evaluations: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    readiness = []
    for scope, cases in sorted(cases_by_scope.items()):
        result = _evaluate_system(
            system_id=scope[0],
            cases=cases,
            min_train_days=min_train_days,
            min_holdout_days=min_holdout_days,
        )
        evaluations[scope] = result
        readiness.append({
            "ensemble_system_id": scope[0],
            "season_bin": scope[1],
            "lead_bucket": scope[2],
            "run_cycle": scope[3],
            "transport_provider_id": result[
                "transport_provider_id"
            ],
            **{
                key: value
                for key, value in result.items()
                if key
                in {
                    "state",
                    "case_count",
                    "training_case_count",
                    "strict_holdout_case_count",
                    "required_train_case_count",
                    "required_strict_holdout_case_count",
                    "missing_train_case_count",
                    "missing_strict_holdout_case_count",
                    "train_n",
                    "holdout_n",
                    "oos_metrics",
                }
            },
        })
    for system_id in sorted(systems):
        if not any(scope[0] == system_id for scope in cases_by_scope):
            readiness.append({
                "ensemble_system_id": system_id,
                "season_bin": "*",
                "lead_bucket": "*",
                "run_cycle": "*",
                "state": "ACCUMULATING_STRICT_OOS",
                "case_count": 0,
                "training_case_count": 0,
                "strict_holdout_case_count": 0,
                "required_train_case_count": min_train_days,
                "required_strict_holdout_case_count":
                    min_holdout_days,
                "missing_train_case_count": min_train_days,
                "missing_strict_holdout_case_count":
                    min_holdout_days,
            })
    cross_candidates: list[dict[str, Any]] = []
    scope_groups: dict[
        tuple[str, str],
        dict[str, list[tuple[str, dict[str, Any]]]],
    ] = defaultdict(lambda: defaultdict(list))
    for (
        system_id,
        season_bin,
        lead_bucket,
        run_cycle,
    ), evaluation in evaluations.items():
        scope_groups[(season_bin, lead_bucket)][system_id].append((
            run_cycle,
            evaluation,
        ))
    configured_caps = {
        **DEFAULT_FAMILY_WEIGHT_CAPS,
        **dict(family_weight_caps or {}),
    }
    for (
        season_bin,
        lead_bucket,
    ), system_rows in sorted(scope_groups.items()):
        for system_id, system_scopes in sorted(system_rows.items()):
            if len({str(scope[0]) for scope in system_scopes}) != 1:
                raise ContractError(
                    "native_ensemble_oos_system_cycle_conflict"
                )
        ready_rows = [
            (system_id, run_cycle, evaluation)
            for system_id, scopes in sorted(system_rows.items())
            for run_cycle, evaluation in scopes
            if evaluation["state"] == "OOS_READY"
        ]
        if len(ready_rows) < 2:
            continue
        cycle_by_system, cycle_mapping_sha, run_cycle, run_cycle_scope = (
            _cycle_mapping_for_rows(ready_rows)
        )
        rows = [
            (system_id, evaluation)
            for system_id, _run_cycle, evaluation in ready_rows
        ]
        family_by_system = {
            system_id: systems[system_id]["independence_family_id"]
            for system_id, _evaluation in rows
        }
        if len(set(family_by_system.values())) < 2:
            continue
        holdout_sets = [
            {
                prediction["event_date"]
                for prediction in evaluation["predictions"]
            }
            for _system_id, evaluation in rows
        ]
        common_holdout = sorted(set.intersection(*holdout_sets))
        if len(common_holdout) < min_holdout_days:
            continue
        train_rmse = {
            system_id: float(
                evaluation["parameters"]["train_rmse_c"]
            )
            for system_id, evaluation in rows
        }
        weights = _bounded_family_weights(
            train_rmse_by_system=train_rmse,
            family_by_system=family_by_system,
            family_caps=configured_caps,
        )
        predictions_by_system = {
            system_id: {
                row["event_date"]: row
                for row in evaluation["predictions"]
            }
            for system_id, evaluation in rows
        }
        cross_predictions = []
        climate_train = next(
            iter(rows)
        )[1]["parameters"]["climate_samples_c"]
        climate_probability = _probabilities([
            float(value) for value in climate_train
        ])
        baseline_metrics = []
        for day in common_holdout:
            keys = set().union(*(
                set(predictions_by_system[system][day]["probability"])
                for system in weights
            ))
            probability = {
                key: sum(
                    weights[system]
                    * float(
                        predictions_by_system[system][day][
                            "probability"
                        ].get(key, 0.0)
                    )
                    for system in weights
                )
                for key in keys
            }
            actual = float(
                predictions_by_system[
                    next(iter(weights))
                ][day]["actual_tmax_c"]
            )
            decision_at_utc = max(
                str(predictions_by_system[system][day][
                    "decision_at_utc"
                ])
                for system in weights
            )
            cross_predictions.append({
                "event_date": day,
                "decision_at_utc": decision_at_utc,
                "trained_through_event_date": max(
                    evaluation["train_end_date"]
                    for _system, evaluation in rows
                ),
                "system_member_set_shas": {
                    system: predictions_by_system[system][day][
                        "member_set_sha256"
                    ]
                    for system in sorted(weights)
                },
                "actual_tmax_c": actual,
                "probability": probability,
                "metrics": _metrics(probability, actual),
            })
            baseline_metrics.append(_metrics(
                climate_probability,
                actual,
            ))
        cross_metrics = _mean_metrics([
            row["metrics"] for row in cross_predictions
        ])
        climate_metrics = _mean_metrics(baseline_metrics)
        selected_system_ids = sorted(weights)
        family_caps_exact = {
            family: float(configured_caps.get(family, 0.75))
            for family in sorted(set(family_by_system.values()))
        }
        weight_receipt = validate_cross_native_ensemble_weights(
            system_weights=weights,
            systems_by_id=systems,
            family_weight_caps=family_caps_exact,
        )
        residuals: dict[str, dict[str, float]] = {}
        for system_id, _evaluation in rows:
            scope = (
                system_id,
                season_bin,
                lead_bucket,
                cycle_by_system[system_id],
            )
            residuals[system_id] = {
                case["event_date"]: (
                    float(case["actual_tmax_c"])
                    - sum(float(value) for value in case["values"])
                    / len(case["values"])
                )
                for case in cases_by_scope[scope]
                if case["event_date"]
                <= rows[0][1]["train_end_date"]
            }
        correlations: dict[str, dict[str, float]] = {}
        for left in selected_system_ids:
            correlations[left] = {}
            for right in selected_system_ids:
                days = sorted(
                    set(residuals[left]) & set(residuals[right])
                )
                correlations[left][right] = _correlation(
                    [residuals[left][day] for day in days],
                    [residuals[right][day] for day in days],
                )
        oos_pass = (
            cross_metrics["brier"]
            <= climate_metrics["brier"] + EPS
            and cross_metrics["crps"]
            <= climate_metrics["crps"] + EPS
        )
        cross_candidates.append({
            "state": (
                "PRODUCTION_NO_LIVE_OOS_PASS"
                if oos_pass
                else "OOS_REJECTED"
            ),
            "station_id": station,
            "season_bin": season_bin,
            "lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "run_cycle_scope": run_cycle_scope,
            "run_cycle_by_system": {
                system_id: cycle_by_system[system_id]
                for system_id in selected_system_ids
            },
            "run_cycle_by_system_sha256": cycle_mapping_sha,
            "p0_exact_match_ready": run_cycle_scope == "SAME_NATIVE_CYCLE",
            "p0_exact_match_blocker": (
                None
                if run_cycle_scope == "SAME_NATIVE_CYCLE"
                else "MIXED_CYCLE_SCHEMA_AND_P0_QUERY_REQUIRED"
            ),
            "settlement_definition": next(
                iter(cases_by_scope.values())
            )[0]["settlement_definition"],
            "settlement_timezone": cases_by_scope[(
                selected_system_ids[0],
                season_bin,
                lead_bucket,
                cycle_by_system[selected_system_ids[0]],
            )][0]["timezone_name"],
            "selected_system_ids": selected_system_ids,
            "selected_system_count": len(selected_system_ids),
            "system_transport_provider_ids": {
                system_id: str(
                    evaluation["transport_provider_id"]
                )
                for system_id, evaluation in rows
                if system_id in weights
            },
            "independence_family_ids": sorted(
                set(family_by_system.values())
            ),
            "system_family_ids": dict(sorted(
                family_by_system.items()
            )),
            "independent_family_count": len(
                set(family_by_system.values())
            ),
            "available_system_set_hash": content_sha256(
                selected_system_ids
            ),
            "available_family_set_hash":
                native_ensemble_family_set_hash(
                    list(family_by_system.values())
                ),
            "system_weights": weights,
            "family_weights": weight_receipt["family_weights"],
            "family_weight_caps": family_caps_exact,
            "train_start_date": min(
                evaluation["train_start_date"]
                for _system, evaluation in rows
            ),
            "train_end_date": max(
                evaluation["train_end_date"]
                for _system, evaluation in rows
            ),
            "holdout_start_date": common_holdout[0],
            "holdout_end_date": common_holdout[-1],
            "rolling_origin_fold_count": len(common_holdout),
            "effective_sample_n": float(len(common_holdout)),
            "oos_metrics": cross_metrics,
            "climatology_baseline_metrics": climate_metrics,
            "cross_system_residual_correlation": correlations,
            "predictions": cross_predictions,
            "system_evaluations": {
                system_id: evaluation
                for system_id, evaluation in rows
                if system_id in weights
            },
            "threshold_profile": threshold_profile,
            "min_train_days": min_train_days,
            "min_holdout_days": min_holdout_days,
            "formal_promotion_min_train_days":
                DEFAULT_MIN_TRAIN_DAYS,
            "formal_promotion_min_holdout_days":
                DEFAULT_MIN_HOLDOUT_DAYS,
            "formal_p0_eligible": False,
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        })
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "as_of_utc": as_of_utc,
        "min_train_days": min_train_days,
        "min_holdout_days": min_holdout_days,
        "threshold_profile": threshold_profile,
        "formal_promotion_threshold_profile":
            FORMAL_PROMOTION_THRESHOLD_PROFILE,
        "formal_promotion_min_train_days": DEFAULT_MIN_TRAIN_DAYS,
        "formal_promotion_min_holdout_days":
            DEFAULT_MIN_HOLDOUT_DAYS,
        "registered_system_count": len(systems),
        "readiness": sorted(
            readiness,
            key=lambda row: (
                row["ensemble_system_id"],
                row["season_bin"],
                row["lead_bucket"],
                row["run_cycle"],
            ),
        ),
        "cross_candidates": cross_candidates,
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "formal_p0_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def _insert_content_row(
    conn: sqlite3.Connection,
    *,
    table: str,
    primary_key: str,
    row: Mapping[str, Any],
) -> int:
    existing = conn.execute(
        f"SELECT content_sha256 FROM {table} WHERE {primary_key}=?",
        (row[primary_key],),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != str(row["content_sha256"]):
            raise ContractError(
                f"native_ensemble_oos_{table}_content_conflict"
            )
        return 0
    columns = tuple(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        tuple(row[column] for column in columns),
    )
    return 1


def _selection_identity(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    _mapping, mapping_sha, run_cycle, run_cycle_scope = (
        _candidate_cycle_mapping(candidate)
    )
    mapping_json = _json(_mapping)
    return {
        "station_id": candidate["station_id"],
        "season_bin": candidate["season_bin"],
        "lead_bucket": candidate["lead_bucket"],
        "run_cycle": run_cycle,
        "settlement_definition": candidate[
            "settlement_definition"
        ],
        "method_version": (
            "strict_forward_member_family_capped_v2_transport_exact::"
            + content_sha256(
                candidate["system_transport_provider_ids"]
            )
            + "::cycle_scope="
            + run_cycle_scope
            + "::run_cycle_by_system_sha256="
            + mapping_sha
            + "::run_cycle_by_system="
            + mapping_json
            + "::"
            + candidate["threshold_profile"]
        ),
        "candidate_system_ids_json": _json(
            candidate["selected_system_ids"]
        ),
        "selected_system_ids_json": _json(
            candidate["selected_system_ids"]
        ),
        "selected_system_count": candidate["selected_system_count"],
        "independent_family_count": candidate[
            "independent_family_count"
        ],
        "selected_weights_json": _json(candidate["system_weights"]),
        "family_weights_json": _json(candidate["family_weights"]),
        "family_weight_caps_json": _json(
            candidate["family_weight_caps"]
        ),
        "supported_exact_masks_json": _json([
            candidate["selected_system_ids"]
        ]),
        "train_start_date": candidate["train_start_date"],
        "train_end_date": candidate["train_end_date"],
        "holdout_start_date": candidate["holdout_start_date"],
        "holdout_end_date": candidate["holdout_end_date"],
        "rolling_origin_fold_count": candidate[
            "rolling_origin_fold_count"
        ],
        "effective_sample_n": candidate["effective_sample_n"],
        "oos_metrics_json": _json(candidate["oos_metrics"]),
        "cross_system_residual_correlation_json": _json(
            candidate["cross_system_residual_correlation"]
        ),
        "deterministic_baseline_metrics_json": _json(
            candidate["climatology_baseline_metrics"]
        ),
    }


def persist_native_ensemble_oos(
    conn: sqlite3.Connection,
    report: Mapping[str, Any],
    *,
    activated_at_utc: str,
    valid_days: int = DEFAULT_VALID_DAYS,
) -> dict[str, Any]:
    """Persist passing candidate artifacts and no-live activations."""

    if report.get("schema") != SCHEMA:
        raise ContractError("native_ensemble_oos_report_schema_invalid")
    identity = {
        key: value
        for key, value in report.items()
        if key != "content_sha256"
    }
    if str(report.get("content_sha256") or "") != content_sha256(identity):
        raise ContractError("native_ensemble_oos_report_sha_mismatch")
    if valid_days < 1 or valid_days > 90:
        raise ContractError("native_ensemble_oos_valid_days_invalid")
    activated = datetime.fromisoformat(
        activated_at_utc.replace("Z", "+00:00")
    )
    valid_until = (
        activated + timedelta(days=valid_days)
    ).isoformat().replace("+00:00", "Z")
    counts = {
        "system_artifacts": 0,
        "selection_evidence": 0,
        "cross_artifacts": 0,
        "oos_predictions": 0,
        "activations": 0,
    }
    receipts = []
    conn.execute("SAVEPOINT native_ensemble_oos_persist")
    try:
        for candidate in report.get("cross_candidates") or []:
            if candidate.get("state") != "PRODUCTION_NO_LIVE_OOS_PASS":
                continue
            cycle_by_system, cycle_mapping_sha, candidate_run_cycle, (
                candidate_run_cycle_scope
            ) = _candidate_cycle_mapping(candidate)
            if candidate_run_cycle_scope == "PER_SYSTEM_NATIVE_CYCLE":
                raise ContractError(
                    "native_ensemble_oos_mixed_cycle_persistence_requires_"
                    "schema_and_p0_query"
                )
            candidate_selection_identity = _selection_identity(candidate)
            candidate_selection_sha = content_sha256(
                candidate_selection_identity
            )
            reusable = conn.execute(
                "SELECT a.activation_id,a.artifact_id,"
                "c.selection_evidence_sha256,a.valid_until_utc "
                "FROM calibration_native_ensemble_activation a "
                "JOIN calibration_cross_native_ensemble_artifact c "
                "ON c.cross_ensemble_artifact_id=a.artifact_id "
                "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
                "AND a.station_id=? AND a.season_bin=? "
                "AND a.lead_bucket=? AND a.run_cycle=? "
                "AND a.available_system_set_hash_or_star=? "
                "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
                "ORDER BY a.generation DESC LIMIT 1",
                (
                    candidate["station_id"],
                    candidate["season_bin"],
                    candidate["lead_bucket"],
                    candidate["run_cycle"],
                    candidate["available_system_set_hash"],
                ),
            ).fetchone()
            if (
                reusable is not None
                and str(reusable[2]) == candidate_selection_sha
                and datetime.fromisoformat(
                    str(reusable[3]).replace("Z", "+00:00")
                )
                > activated
            ):
                selection_row = conn.execute(
                    "SELECT selection_evidence_id FROM "
                    "calibration_cross_native_ensemble_selection_evidence "
                    "WHERE content_sha256=?",
                    (candidate_selection_sha,),
                ).fetchone()
                if selection_row is None:
                    raise ContractError(
                        "native_ensemble_oos_reusable_selection_missing"
                    )
                receipts.append({
                    "station_id": candidate["station_id"],
                    "season_bin": candidate["season_bin"],
                    "lead_bucket": candidate["lead_bucket"],
                    "run_cycle": candidate["run_cycle"],
                    "system_artifact_ids": {},
                    "system_activation_ids": {},
                    "selection_evidence_id": str(selection_row[0]),
                    "cross_ensemble_artifact_id": str(reusable[1]),
                    "activation_id": str(reusable[0]),
                    "reused_existing": True,
                })
                continue
            system_artifact_ids: dict[str, str] = {}
            system_activation_ids: dict[str, str] = {}
            for system_id, evaluation in sorted(
                candidate["system_evaluations"].items()
            ):
                system_run_cycle = cycle_by_system.get(system_id)
                if not system_run_cycle:
                    raise ContractError(
                        "native_ensemble_oos_run_cycle_mapping_missing"
                    )
                parameters = evaluation["parameters"]
                transport_provider_id = str(
                    evaluation["transport_provider_id"]
                )
                official_native_archive = (
                    transport_provider_id != "OPEN_METEO"
                )
                train_source_counts = evaluation[
                    "train_source_counts"
                ]
                source_authority_scopes = (
                    ["CALIBRATION_TRAIN_ONLY"]
                    if official_native_archive
                    else sorted({
                        *(
                            ["CALIBRATION_TRAIN_ONLY"]
                            if int(train_source_counts[
                                "RETROSPECTIVE_ARCHIVE_TRAIN_ONLY"
                            ]) > 0
                            else []
                        ),
                        *(
                            ["PRODUCTION_NO_LIVE_CANDIDATE"]
                            if int(train_source_counts[
                                "FORWARD_LOCAL_RECEIPT"
                            ]) > 0
                            else []
                        ),
                    })
                )
                system_identity = {
                    "ensemble_system_id": system_id,
                    "station_id": candidate["station_id"],
                    "season_bin": candidate["season_bin"],
                    "lead_bucket": candidate["lead_bucket"],
                    "run_cycle": system_run_cycle,
                    "settlement_definition": candidate[
                        "settlement_definition"
                    ],
                    "calibration_method": (
                        "member_bias_spread_reliability_v1"
                    ),
                    "bias_parameters_json": _json({
                        "bias_c": parameters["bias_c"],
                    }),
                    "spread_parameters_json": _json({
                        "spread_factor": parameters["spread_factor"],
                        "train_rmse_c": parameters["train_rmse_c"],
                        "train_mean_spread_c": parameters[
                            "train_mean_spread_c"
                        ],
                    }),
                    "reliability_parameters_json": _json({
                        "reliability_weight": parameters[
                            "reliability_weight"
                        ],
                        "climate_samples_c": parameters[
                            "climate_samples_c"
                        ],
                        "train_source_counts": evaluation[
                            "train_source_counts"
                        ],
                        "holdout_source_class": evaluation[
                            "holdout_source_class"
                        ],
                        "transport_provider_id":
                            transport_provider_id,
                        "source_authority_scopes":
                            source_authority_scopes,
                        "official_native_archive":
                            official_native_archive,
                        "oos_threshold_profile": candidate[
                            "threshold_profile"
                        ],
                        "oos_min_train_days": candidate[
                            "min_train_days"
                        ],
                        "oos_min_holdout_days": candidate[
                            "min_holdout_days"
                        ],
                        "formal_promotion_min_train_days": candidate[
                            "formal_promotion_min_train_days"
                        ],
                        "formal_promotion_min_holdout_days": candidate[
                            "formal_promotion_min_holdout_days"
                        ],
                        "formal_p0_eligible": False,
                    }),
                    "member_qc_contract_sha256": content_sha256({
                        "expected_complete_member_set": True,
                        "transport_provider_id":
                            transport_provider_id,
                        "source_authority_scopes":
                            source_authority_scopes,
                        "aggregator_or_republisher": (
                            transport_provider_id == "OPEN_METEO"
                        ),
                        "official_native_archive":
                            official_native_archive,
                        "native_run_init_required":
                            official_native_archive,
                        "retrospective_archive_training_only": (
                            not official_native_archive
                        ),
                        "retrospective_archive_holdout_eligible": False,
                        "offline_chronological_oos_eligible":
                            official_native_archive,
                        "holdout_snapshot_before_day_end": True,
                        "holdout_snapshot_before_truth_commit": True,
                    }),
                    "train_start_date": evaluation["train_start_date"],
                    "train_end_date": evaluation["train_end_date"],
                    "holdout_start_date": evaluation[
                        "holdout_start_date"
                    ],
                    "holdout_end_date": evaluation["holdout_end_date"],
                    "rolling_origin_fold_count": int(
                        evaluation["holdout_n"]
                    ),
                    "effective_sample_n": float(evaluation["train_n"]),
                    "oos_metrics_json": _json(
                        evaluation["oos_metrics"]
                    ),
                    "valid_from_utc": activated_at_utc,
                    "valid_until_utc": valid_until,
                }
                system_sha = content_sha256(system_identity)
                system_row = {
                    "system_artifact_id": (
                        "native-system-artifact-" + system_sha[:24]
                    ),
                    **system_identity,
                    "content_sha256": system_sha,
                }
                counts["system_artifacts"] += _insert_content_row(
                    conn,
                    table="calibration_native_ensemble_system_artifact",
                    primary_key="system_artifact_id",
                    row=system_row,
                )
                system_artifact_ids[system_id] = str(
                    system_row["system_artifact_id"]
                )
                existing_system_activation = conn.execute(
                    "SELECT activation_id,artifact_id "
                    "FROM calibration_native_ensemble_activation "
                    "WHERE artifact_kind="
                    "'NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
                    "AND station_id=? AND ensemble_system_id_or_star=? "
                    "AND season_bin=? AND lead_bucket=? AND run_cycle=? "
                    "AND available_system_set_hash_or_star='*' "
                    "ORDER BY generation DESC LIMIT 1",
                    (
                        candidate["station_id"],
                        system_id,
                        candidate["season_bin"],
                        candidate["lead_bucket"],
                        system_run_cycle,
                    ),
                ).fetchone()
                if (
                    existing_system_activation is not None
                    and str(existing_system_activation[1])
                    == str(system_row["system_artifact_id"])
                ):
                    system_activation_ids[system_id] = str(
                        existing_system_activation[0]
                    )
                else:
                    system_generation = 1 + int(conn.execute(
                        "SELECT COALESCE(MAX(generation),0) "
                        "FROM calibration_native_ensemble_activation "
                        "WHERE artifact_kind="
                        "'NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
                        "AND station_id=? "
                        "AND ensemble_system_id_or_star=? "
                        "AND season_bin=? AND lead_bucket=? "
                        "AND run_cycle=? "
                        "AND available_system_set_hash_or_star='*'",
                        (
                            candidate["station_id"],
                            system_id,
                            candidate["season_bin"],
                            candidate["lead_bucket"],
                            system_run_cycle,
                        ),
                    ).fetchone()[0])
                    system_activation_identity = {
                        "artifact_kind": (
                            "NATIVE_ENSEMBLE_SYSTEM_CALIBRATION"
                        ),
                        "artifact_id": system_row[
                            "system_artifact_id"
                        ],
                        "station_id": candidate["station_id"],
                        "ensemble_system_id_or_star": system_id,
                        "season_bin": candidate["season_bin"],
                        "lead_bucket": candidate["lead_bucket"],
                        "run_cycle": system_run_cycle,
                        "settlement_definition": candidate[
                            "settlement_definition"
                        ],
                        "available_system_set_hash_or_star": "*",
                        "generation": system_generation,
                        "valid_from_utc": activated_at_utc,
                        "valid_until_utc": valid_until,
                        "activated_at_utc": activated_at_utc,
                        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                    }
                    system_activation_sha = content_sha256(
                        system_activation_identity
                    )
                    system_activation_row = {
                        "activation_id": (
                            "native-activation-"
                            + system_activation_sha[:24]
                        ),
                        **system_activation_identity,
                        "content_sha256": system_activation_sha,
                    }
                    counts["activations"] += _insert_content_row(
                        conn,
                        table=(
                            "calibration_native_ensemble_activation"
                        ),
                        primary_key="activation_id",
                        row=system_activation_row,
                    )
                    system_activation_ids[system_id] = str(
                        system_activation_row["activation_id"]
                    )
            selection_identity = candidate_selection_identity
            selection_sha = content_sha256(selection_identity)
            selection_row = {
                "selection_evidence_id": (
                    "native-selection-" + selection_sha[:24]
                ),
                **selection_identity,
                "content_sha256": selection_sha,
            }
            counts["selection_evidence"] += _insert_content_row(
                conn,
                table=(
                    "calibration_cross_native_ensemble_selection_evidence"
                ),
                primary_key="selection_evidence_id",
                row=selection_row,
            )
            cross_identity = {
                "station_id": candidate["station_id"],
                "season_bin": candidate["season_bin"],
                "lead_bucket": candidate["lead_bucket"],
                "run_cycle": candidate["run_cycle"],
                "settlement_definition": candidate[
                    "settlement_definition"
                ],
                "ensemble_system_ids_json": _json(
                    candidate["selected_system_ids"]
                ),
                "ensemble_system_count": candidate[
                    "selected_system_count"
                ],
                "independent_family_count": candidate[
                    "independent_family_count"
                ],
                "supported_exact_masks_json": _json([
                    candidate["selected_system_ids"]
                ]),
                "selection_evidence_sha256": selection_sha,
                "combination_method": (
                    "train_rmse_inverse_family_capped_v1::"
                    + candidate["threshold_profile"]
                    + "::cycle_scope="
                    + str(candidate["run_cycle_scope"])
                    + "::run_cycle_by_system_sha256="
                    + str(candidate["run_cycle_by_system_sha256"])
                ),
                "family_weight_caps_json": _json(
                    candidate["family_weight_caps"]
                ),
                "probability_pathway": P0_PROBABILITY_PATHWAY,
                "valid_from_utc": activated_at_utc,
                "valid_until_utc": valid_until,
            }
            cross_sha = content_sha256(cross_identity)
            cross_row = {
                "cross_ensemble_artifact_id": (
                    "native-cross-artifact-" + cross_sha[:24]
                ),
                **cross_identity,
                "content_sha256": cross_sha,
            }
            counts["cross_artifacts"] += _insert_content_row(
                conn,
                table="calibration_cross_native_ensemble_artifact",
                primary_key="cross_ensemble_artifact_id",
                row=cross_row,
            )
            for prediction in candidate["predictions"]:
                probabilities = {
                    str(key): float(value)
                    for key, value in prediction["probability"].items()
                }
                prediction_identity = {
                    "cross_ensemble_artifact_id": cross_row[
                        "cross_ensemble_artifact_id"
                    ],
                    "selection_evidence_sha256": selection_sha,
                    "station_id": candidate["station_id"],
                    "event_date": prediction["event_date"],
                    "settlement_timezone": candidate[
                        "settlement_timezone"
                    ],
                    "season_bin": candidate["season_bin"],
                    "lead_bucket": candidate["lead_bucket"],
                    "run_cycle": candidate["run_cycle"],
                    "settlement_definition": candidate[
                        "settlement_definition"
                    ],
                    "trained_through_event_date": prediction[
                        "trained_through_event_date"
                    ],
                    "decision_at_utc": prediction["decision_at_utc"],
                    "ensemble_system_ids_json": _json(
                        candidate["selected_system_ids"]
                    ),
                    "independence_family_ids_json": _json(
                        candidate["independence_family_ids"]
                    ),
                    "system_family_ids_json": _json(
                        candidate["system_family_ids"]
                    ),
                    "system_weights_json": _json(
                        candidate["system_weights"]
                    ),
                    "family_weight_caps_json": _json(
                        candidate["family_weight_caps"]
                    ),
                    "system_member_set_shas_json": _json(
                        prediction["system_member_set_shas"]
                    ),
                    "probabilities_json": _json(probabilities),
                    "bucket_centers_c_json": _json({
                        key: float(key) for key in probabilities
                    }),
                    "prediction_oos": 1,
                    "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                }
                prediction_sha = content_sha256(prediction_identity)
                prediction_row = {
                    "prediction_id": (
                        "native-oos-prediction-"
                        + prediction_sha[:24]
                    ),
                    **prediction_identity,
                    "content_sha256": prediction_sha,
                }
                counts["oos_predictions"] += _insert_content_row(
                    conn,
                    table=(
                        "calibration_cross_native_ensemble_oos_prediction"
                    ),
                    primary_key="prediction_id",
                    row=prediction_row,
                )
            activation_scope = (
                candidate["station_id"],
                candidate["season_bin"],
                candidate["lead_bucket"],
                candidate["run_cycle"],
                candidate["available_system_set_hash"],
            )
            existing_activation = conn.execute(
                "SELECT activation_id,artifact_id,content_sha256 "
                "FROM calibration_native_ensemble_activation "
                "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' "
                "AND station_id=? AND season_bin=? AND lead_bucket=? "
                "AND run_cycle=? "
                "AND available_system_set_hash_or_star=? "
                "ORDER BY generation DESC LIMIT 1",
                activation_scope,
            ).fetchone()
            if (
                existing_activation is not None
                and str(existing_activation[1])
                == str(cross_row["cross_ensemble_artifact_id"])
            ):
                activation_id = str(existing_activation[0])
            else:
                generation = 1 + int(conn.execute(
                    "SELECT COALESCE(MAX(generation),0) "
                    "FROM calibration_native_ensemble_activation "
                    "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' "
                    "AND station_id=? AND season_bin=? "
                    "AND lead_bucket=? AND run_cycle=? "
                    "AND available_system_set_hash_or_star=?",
                    activation_scope,
                ).fetchone()[0])
                activation_identity = {
                    "artifact_kind": "CROSS_NATIVE_ENSEMBLE",
                    "artifact_id": cross_row[
                        "cross_ensemble_artifact_id"
                    ],
                    "station_id": candidate["station_id"],
                    "ensemble_system_id_or_star": "*",
                    "season_bin": candidate["season_bin"],
                    "lead_bucket": candidate["lead_bucket"],
                    "run_cycle": candidate["run_cycle"],
                    "settlement_definition": candidate[
                        "settlement_definition"
                    ],
                    "available_system_set_hash_or_star": candidate[
                        "available_system_set_hash"
                    ],
                    "generation": generation,
                    "valid_from_utc": activated_at_utc,
                    "valid_until_utc": valid_until,
                    "activated_at_utc": activated_at_utc,
                    "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                }
                activation_sha = content_sha256(activation_identity)
                activation_row = {
                    "activation_id": (
                        "native-activation-" + activation_sha[:24]
                    ),
                    **activation_identity,
                    "content_sha256": activation_sha,
                }
                counts["activations"] += _insert_content_row(
                    conn,
                    table="calibration_native_ensemble_activation",
                    primary_key="activation_id",
                    row=activation_row,
                )
                activation_id = str(activation_row["activation_id"])
            receipts.append({
                "station_id": candidate["station_id"],
                "season_bin": candidate["season_bin"],
                "lead_bucket": candidate["lead_bucket"],
                "run_cycle": candidate["run_cycle"],
                "system_artifact_ids": system_artifact_ids,
                "system_activation_ids": system_activation_ids,
                "selection_evidence_id": selection_row[
                    "selection_evidence_id"
                ],
                "cross_ensemble_artifact_id": cross_row[
                    "cross_ensemble_artifact_id"
                ],
                "activation_id": activation_id,
            })
    except Exception:
        conn.execute("ROLLBACK TO native_ensemble_oos_persist")
        conn.execute("RELEASE native_ensemble_oos_persist")
        raise
    conn.execute("RELEASE native_ensemble_oos_persist")
    return {
        "schema": "native_ensemble_oos_persist_v1",
        "state": (
            "COMMITTED" if receipts else "NO_OOS_PASS_NO_WRITE"
        ),
        "counts": counts,
        "receipts": receipts,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


__all__ = [
    "DEFAULT_FAMILY_WEIGHT_CAPS",
    "DEFAULT_MIN_HOLDOUT_DAYS",
    "DEFAULT_MIN_TRAIN_DAYS",
    "FORMAL_PROMOTION_THRESHOLD_PROFILE",
    "NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS",
    "NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS",
    "NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE",
    "SCHEMA",
    "build_native_ensemble_oos",
    "persist_native_ensemble_oos",
]
