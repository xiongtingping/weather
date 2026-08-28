"""Strict historical OOS for deterministic Open-Meteo republisher curves.

This module is the deterministic counterpart of the native-ensemble
republisher lane.  It consumes already retained Open-Meteo historical
forecast receipts and a read-only AWC truth snapshot, then emits an explicit
no-live candidate.  It never upgrades a republisher into native arrival,
P1, formal/live authority, or an order input.

The retrospective packet is valid only because an immutable closed-day AWC
truth contract is created for the exact station/day.  Forward packets and
future event dates are rejected and cannot count toward the 20-train/8-
holdout requirement.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
import json
import math
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from .awc_observation_contract import (
    CanonicalAWCObservationDayBoundsFact,
    CanonicalAWCObservationRevisionFact,
)
from .canonical_ssot_schema import inspect_canonical_ssot_schema
from .contracts import ContractError, content_sha256
from .settlement_time import settlement_day_window
from .fleet import expected_station_count


SCHEMA = "deterministic_republisher_strict_oos_v2"
CANDIDATE_SCHEMA = "deterministic_republisher_strict_oos_candidate_v2"
HISTORICAL_RECEIPT_CLASS = "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"
TEMPORAL_SCOPE = "CLOSED_HISTORICAL_AWC_TRUTH"
SOURCE_ROLE = "aggregator_republisher"
TRANSPORT_PROVIDER_ID = "OPEN_METEO"
MIN_TRAIN_DAYS = 20
MIN_HOLDOUT_DAYS = 8
MAX_CURVES = expected_station_count() * 366


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"deterministic_republisher_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"deterministic_republisher_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"deterministic_republisher_{name}_sha_invalid")
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"deterministic_republisher_{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"deterministic_republisher_{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"deterministic_republisher_{name}_invalid")
    return result


def _verify_capture(capture: Mapping[str, Any]) -> tuple[str, dict[str, str], list[dict[str, Any]]]:
    if capture.get("schema") != "bayes_basket_target_historical_aggregator_rebuild_evidence_v1":
        raise ContractError("deterministic_republisher_capture_schema_invalid")
    supplied = _sha("capture_receipt", capture.get("receipt_sha256"))
    identity = {key: value for key, value in capture.items() if key != "receipt_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError("deterministic_republisher_capture_sha_mismatch")
    if (
        capture.get("source", {}).get("source_class") != SOURCE_ROLE
        or capture.get("source", {}).get("open_meteo") is not True
        or capture.get("checks", {}).get("native_arrival_index_eligible") is not False
        or capture.get("checks", {}).get("prospective_strict_asof_eligible") is not False
    ):
        raise ContractError("deterministic_republisher_capture_lineage_invalid")
    provider = str(capture.get("provider_id") or "").strip().lower()
    model = str(capture.get("model_id") or "").strip().upper()
    if not provider or not model:
        raise ContractError("deterministic_republisher_capture_model_missing")
    raw_timezones = capture.get("station_timezones")
    if not isinstance(raw_timezones, Mapping) or not raw_timezones:
        raise ContractError("deterministic_republisher_capture_timezones_missing")
    timezones = {
        str(station).strip().upper(): str(value).strip()
        for station, value in raw_timezones.items()
    }
    try:
        for value in timezones.values():
            ZoneInfo(value)
    except Exception as exc:  # ZoneInfoNotFoundError is platform-specific
        raise ContractError("deterministic_republisher_capture_timezone_invalid") from exc
    curves_container = capture.get("curves")
    curves = curves_container.get("eligible_model_rows") if isinstance(curves_container, Mapping) else None
    if not isinstance(curves, list) or not curves or len(curves) > MAX_CURVES:
        raise ContractError("deterministic_republisher_capture_curves_missing")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in curves:
        if not isinstance(raw, Mapping):
            raise ContractError("deterministic_republisher_curve_mapping_required")
        station = str(raw.get("station_id") or "").strip().upper()
        event_date = str(raw.get("event_date") or "").strip()
        if station not in timezones:
            raise ContractError("deterministic_republisher_curve_station_invalid")
        try:
            date.fromisoformat(event_date)
        except ValueError as exc:
            raise ContractError("deterministic_republisher_curve_date_invalid") from exc
        key = (station, event_date)
        if key in seen:
            raise ContractError("deterministic_republisher_curve_duplicate")
        seen.add(key)
        if (
            str(raw.get("source_class") or "") != SOURCE_ROLE
            or raw.get("aggregator_or_republisher") is not True
            or raw.get("open_meteo") is not True
            or raw.get("complete") is not True
        ):
            raise ContractError("deterministic_republisher_curve_lineage_invalid")
        forecast = _finite("forecast_daily_max", raw.get("forecast_daily_max_c"))
        raw_sha = _sha("curve_raw_message", raw.get("raw_message_sha256"))
        raw_ref = str(raw.get("raw_object_ref") or "").strip()
        if not raw_ref:
            raise ContractError("deterministic_republisher_curve_raw_ref_missing")
        normalized.append({
            "station_id": station,
            "event_date": event_date,
            "target_timezone": timezones[station],
            "forecast_daily_max_c": forecast,
            "raw_message_sha256": raw_sha,
            "raw_object_ref": raw_ref,
            "receipt_sha256": supplied,
            "reconstructed_at_utc": str(capture.get("reconstructed_at_utc") or ""),
            "provider_id": provider,
            "model_id": model,
            "model_run_initialization_utc": str(
                raw.get("model_run_initialization_utc") or ""
            ),
            "run_initialization_provenance": str(
                raw.get("run_initialization_provenance")
                or "aggregator_replay_bucket_not_native_cycle"
            ),
            "temperature_point_count": int(
                raw.get("temperature_point_count") or 0
            ),
            "valid_time_start_utc": str(raw.get("valid_time_start_utc") or ""),
            "valid_time_end_utc": str(raw.get("valid_time_end_utc") or ""),
            # This retained capture is explicitly retrospective.  It proves
            # payload identity, not that the model value was available before
            # the historical decision/day close.
            "strict_asof_model_availability_proven": False,
            "model_available_at_utc": None,
            "forecast_decision_at_utc": None,
        })
    return provider, timezones, normalized


def _load_v12_truth_catalog(
    database: sqlite3.Connection,
    *,
    scopes: Iterable[tuple[str, str]],
    as_of: datetime,
    timezones: Mapping[str, str],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[tuple[str, str], set[str]],
]:
    normalized = sorted(set(scopes))
    if not normalized:
        raise ContractError("deterministic_republisher_truth_scope_empty")
    inspection = inspect_canonical_ssot_schema(database)
    if inspection.get("state") not in {"V12", "V13", "V14"} or inspection.get("errors"):
        raise ContractError("deterministic_republisher_canonical_ssot_v12_required")
    requested = set(normalized)
    revision_rows = database.execute(
        "SELECT scope_key,content_sha256,fact_json,lineage_json "
        "FROM canonical_ssot_fact WHERE fact_type='truth_observation_revision' "
        "ORDER BY scope_key"
    ).fetchall()
    revisions_by_day: dict[
        tuple[str, str, str], list[CanonicalAWCObservationRevisionFact]
    ] = defaultdict(list)
    revisions_by_hash: dict[str, CanonicalAWCObservationRevisionFact] = {}
    for row in revision_rows:
        try:
            fact_raw = json.loads(str(row[2]))
            lineage = json.loads(str(row[3]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "deterministic_republisher_v12_revision_json_invalid"
            ) from exc
        if not isinstance(fact_raw, Mapping) or not isinstance(lineage, Mapping):
            raise ContractError(
                "deterministic_republisher_v12_revision_mapping_required"
            )
        fact = CanonicalAWCObservationRevisionFact.from_mapping(fact_raw)
        if str(row[0]) != fact.scope_key or str(row[1]) != fact.content_sha256:
            raise ContractError(
                "deterministic_republisher_v12_revision_row_mismatch"
            )
        expected_lineage = {
            "source_lane": "AWC",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "raw_object_kind": fact.raw["raw_object_kind"],
            "raw_object_sha256": fact.raw["raw_object_sha256"],
            "awc_source_receipt_sha256": fact.raw["awc_source_receipt_sha256"],
            "origin_source_id": fact.raw["origin_source_id"],
            "origin_url": fact.raw["origin_url"],
            "origin_transport": fact.raw["origin_transport"],
            "origin_registry_sha256": fact.raw["origin_registry_sha256"],
            "origin_record_sha256": fact.raw["origin_record_sha256"],
            "production_no_live_only": True,
            "formal_live_authority": False,
            "p1_eligible": True,
        }
        if dict(lineage) != expected_lineage:
            raise ContractError(
                "deterministic_republisher_v12_revision_lineage_mismatch"
            )
        key = (
            str(fact.raw["station_id"]),
            str(fact.raw["event_date"]),
            str(fact.raw["settlement_timezone"]),
        )
        revisions_by_day[key].append(fact)
        revisions_by_hash[fact.content_sha256] = fact

    rows = database.execute(
        "SELECT scope_key,content_sha256,fact_json,lineage_json "
        "FROM canonical_ssot_fact WHERE fact_type='truth_observation_day_bounds' "
        "ORDER BY scope_key"
    ).fetchall()
    result: dict[tuple[str, str], dict[str, Any]] = {}
    scope_timezones: dict[tuple[str, str], set[str]] = defaultdict(set)
    selected_versions: dict[tuple[str, str], int] = {}
    for row in rows:
        try:
            fact_raw = json.loads(str(row[2]))
            lineage = json.loads(str(row[3]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "deterministic_republisher_v12_day_bounds_json_invalid"
            ) from exc
        if not isinstance(fact_raw, Mapping) or not isinstance(lineage, Mapping):
            raise ContractError(
                "deterministic_republisher_v12_day_bounds_mapping_required"
            )
        fact = CanonicalAWCObservationDayBoundsFact.from_mapping(fact_raw)
        if str(row[0]) != fact.scope_key or str(row[1]) != fact.content_sha256:
            raise ContractError(
                "deterministic_republisher_v12_day_bounds_row_mismatch"
            )
        expected_lineage = {
            "source_lane": "AWC",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "observation_revision_vector_sha256": fact.raw[
                "observation_revision_vector_sha256"
            ],
            "coverage_receipt_sha256": fact.raw["coverage_receipt_sha256"],
            "production_no_live_only": True,
            "formal_live_authority": False,
            "p1_eligible": True,
        }
        if dict(lineage) != expected_lineage:
            raise ContractError(
                "deterministic_republisher_v12_day_bounds_lineage_mismatch"
            )
        key = (str(fact.raw["station_id"]), str(fact.raw["event_date"]))
        if key not in requested:
            continue
        timezone_name = str(fact.raw["settlement_timezone"])
        scope_timezones[key].add(timezone_name)
        committed = _utc(
            "truth_committed", fact.raw["canonical_committed_at_utc"]
        )
        if committed > as_of:
            continue
        expected_timezone = str(timezones.get(key[0]) or "")
        if not expected_timezone:
            raise ContractError("deterministic_republisher_truth_timezone_missing")
        if timezone_name != expected_timezone:
            continue
        closed_at = _utc("station_day_end", fact.raw["day_end_utc"])
        expected_day_end = _utc(
            "expected_station_day_end",
            settlement_day_window(key[1], timezone_name)["target_day_end_utc"],
        )
        if closed_at != expected_day_end:
            raise ContractError("deterministic_republisher_truth_day_end_mismatch")
        if closed_at > as_of:
            continue
        bounds_version = int(fact.raw["bounds_version"])
        if bounds_version <= selected_versions.get(key, -1):
            continue
        day_revisions = sorted(
            (
                item
                for item in revisions_by_day.get(
                    (key[0], key[1], timezone_name), ()
                )
                if _utc(
                    "revision_committed", item.raw["canonical_committed_at_utc"]
                )
                <= committed
            ),
            key=lambda item: int(item.raw["sequence"]),
        )
        if not day_revisions or [int(item.raw["sequence"]) for item in day_revisions] != list(
            range(1, len(day_revisions) + 1)
        ):
            raise ContractError(
                "deterministic_republisher_v12_revision_vector_incomplete"
            )
        revision_vector_sha = content_sha256(
            [item.content_sha256 for item in day_revisions]
        )
        if revision_vector_sha != fact.raw["observation_revision_vector_sha256"]:
            raise ContractError(
                "deterministic_republisher_v12_revision_vector_mismatch"
            )
        maximum_value = max(
            float(item.raw["reported_temperature_c"]) for item in day_revisions
        )
        ties = sorted(
            (
                item
                for item in day_revisions
                if float(item.raw["reported_temperature_c"]) == maximum_value
            ),
            key=lambda item: (int(item.raw["sequence"]), item.content_sha256),
        )
        if content_sha256(sorted(item.content_sha256 for item in ties)) != fact.raw[
            "max_tie_vector_sha256"
        ]:
            raise ContractError(
                "deterministic_republisher_v12_max_tie_vector_mismatch"
            )
        maximum = revisions_by_hash.get(str(fact.raw["max_observation_fact_sha256"]))
        if (
            maximum is None
            or maximum.content_sha256 != ties[0].content_sha256
            or float(fact.raw["running_max_c"]) != maximum_value
        ):
            raise ContractError(
                "deterministic_republisher_v12_max_reference_mismatch"
            )
        selected_versions[key] = bounds_version
        result[key] = {
            "station_id": key[0],
            "event_date": key[1],
            "official_tmax_c": _finite("truth_tmax", fact.raw["running_max_c"]),
            "bounds_version": bounds_version,
            "canonical_committed_at_utc": _stamp(committed),
            "content_sha256": fact.content_sha256,
            "observation_revision_vector_sha256": revision_vector_sha,
            "max_observation_fact_sha256": maximum.content_sha256,
            "coverage_receipt_sha256": str(fact.raw["coverage_receipt_sha256"]),
            "station_day_end_utc": _stamp(closed_at),
        }
    return result, scope_timezones


def _truth_index(
    database: sqlite3.Connection,
    *,
    scopes: Iterable[tuple[str, str]],
    as_of: datetime,
    timezones: Mapping[str, str],
) -> dict[tuple[str, str], dict[str, Any]]:
    result, _ = _load_v12_truth_catalog(
        database, scopes=scopes, as_of=as_of, timezones=timezones
    )
    return result


def _season(event_date: str) -> str:
    month = int(event_date[5:7])
    return "DJF" if month in (12, 1, 2) else "MAM" if month in (3, 4, 5) else "JJA" if month in (6, 7, 8) else "SON"


def _rmse(values: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in values) / len(values)) if values else 0.0


def build_deterministic_republisher_oos(
    captures: Iterable[Mapping[str, Any]],
    database: sqlite3.Connection,
    *,
    as_of_utc: str,
    min_train_days: int = MIN_TRAIN_DAYS,
    min_holdout_days: int = MIN_HOLDOUT_DAYS,
) -> dict[str, Any]:
    """Build one no-live candidate per station from closed historical curves."""

    as_of = _utc("as_of", as_of_utc)
    canonical_schema = inspect_canonical_ssot_schema(database)
    canonical_state = str(canonical_schema.get("state") or "")
    if canonical_state not in {"V12", "V13", "V14"} or canonical_schema.get("errors"):
        raise ContractError("deterministic_republisher_canonical_ssot_v12_required")
    if min_train_days != MIN_TRAIN_DAYS or min_holdout_days != MIN_HOLDOUT_DAYS:
        raise ContractError("deterministic_republisher_oos_thresholds_fixed")
    all_curves: list[dict[str, Any]] = []
    provider_ids: set[str] = set()
    models: set[str] = set()
    timezones: dict[str, str] = {}
    capture_shas: list[str] = []
    for capture in captures:
        provider, capture_timezones, curves = _verify_capture(capture)
        provider_ids.add(provider)
        models.add(str(capture.get("model_id") or "").strip().upper())
        for station, timezone_name in capture_timezones.items():
            prior = timezones.setdefault(station, timezone_name)
            if prior != timezone_name:
                raise ContractError("deterministic_republisher_timezone_conflict")
        capture_shas.append(_sha("capture_receipt", capture.get("receipt_sha256")))
        all_curves.extend(curves)
    if len(provider_ids) != 1 or len(models) != 1:
        raise ContractError("deterministic_republisher_model_scope_mixed")
    if not all_curves or len(all_curves) > MAX_CURVES:
        raise ContractError("deterministic_republisher_curve_count_invalid")
    by_scope: dict[tuple[str, str], dict[str, Any]] = {}
    for curve in all_curves:
        key = (curve["station_id"], curve["event_date"])
        if key in by_scope:
            raise ContractError("deterministic_republisher_curve_overlap")
        reconstructed = _utc("capture_reconstructed", curve["reconstructed_at_utc"])
        event_end = _utc(
            "event_day_end",
            settlement_day_window(curve["event_date"], curve["target_timezone"])["target_day_end_utc"],
        )
        if reconstructed <= event_end:
            raise ContractError("deterministic_republisher_capture_not_retrospective")
        by_scope[key] = curve
    truth, truth_scope_timezones = _load_v12_truth_catalog(
        database,
        scopes=by_scope,
        as_of=as_of,
        timezones=timezones,
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_truth: list[dict[str, Any]] = []
    model_lineage_gaps: list[dict[str, Any]] = []
    for key, curve in sorted(by_scope.items()):
        actual = truth.get(key)
        if actual is None:
            available_timezones = sorted(truth_scope_timezones.get(key, ()))
            failure_code = (
                "TIMEZONE_MISMATCH"
                if available_timezones
                and curve["target_timezone"] not in available_timezones
                else "DATA_MISSING"
            )
            missing_truth.append({
                "station_id": key[0],
                "event_date": key[1],
                "settlement_timezone": curve["target_timezone"],
                "available_truth_timezones": available_timezones,
                "failure_code": failure_code,
            })
        if curve["strict_asof_model_availability_proven"] is not True:
            model_lineage_gaps.append({
                "station_id": key[0],
                "event_date": key[1],
                "settlement_timezone": curve["target_timezone"],
                "model_id": curve["model_id"],
                "provider_id": curve["provider_id"],
                "failure_code": "LINEAGE_MISMATCH",
                "detail": "historical_model_available_at_and_decision_at_unproven",
            })
            continue
        if actual is None:
            continue
        grouped[key[0]].append({**curve, "truth": actual})
    candidates: list[dict[str, Any]] = []
    shortfalls: list[dict[str, Any]] = []
    for station in sorted(timezones):
        rows = grouped.get(station, [])
        # Exact chronology is the station-day contract.  Keep the earliest
        # 28 closed days so later backfills cannot silently change a cohort.
        rows = sorted(rows, key=lambda item: item["event_date"])
        if len(rows) < MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS:
            station_missing = [
                item for item in missing_truth if item["station_id"] == station
            ]
            mismatch_count = sum(
                item["failure_code"] == "TIMEZONE_MISMATCH"
                for item in station_missing
            )
            model_lineage_gap_count = sum(
                item["station_id"] == station for item in model_lineage_gaps
            )
            failure_code = (
                "LINEAGE_MISMATCH"
                if not rows and model_lineage_gap_count
                else
                "TIMEZONE_MISMATCH"
                if not rows and mismatch_count
                else "OOS_SAMPLE_SHORT"
            )
            shortfalls.append({
                "station_id": station,
                "model_id": next(iter(models)),
                "provider_id": next(iter(provider_ids)),
                "source_class": SOURCE_ROLE,
                "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                "settlement_timezone": timezones[station],
                "lead_bucket": "d0",
                "run_cycle": None,
                "run_cycle_scope": "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE",
                "state": "BLOCKED",
                "failure_code": failure_code,
                "available_closed_historical_days": len(rows),
                "required_train_days": MIN_TRAIN_DAYS,
                "required_holdout_days": MIN_HOLDOUT_DAYS,
                "missing_days": MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS - len(rows),
                "timezone_mismatch_scope_count": mismatch_count,
                "data_missing_scope_count": sum(
                    item["failure_code"] == "DATA_MISSING"
                    for item in station_missing
                ),
                "model_lineage_gap_scope_count": model_lineage_gap_count,
            })
            continue
        cohort = rows[: MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS]
        train, holdout = cohort[:MIN_TRAIN_DAYS], cohort[MIN_TRAIN_DAYS:]
        cohort_rows: list[dict[str, Any]] = []
        for index, item in enumerate(cohort):
            case_identity = {
                "event_date": item["event_date"],
                "partition": "train" if index < MIN_TRAIN_DAYS else "holdout",
                "forecast_daily_max_c": item["forecast_daily_max_c"],
                "truth_value_c": item["truth"]["official_tmax_c"],
                "model_input_content_sha256": item["raw_message_sha256"],
                "source_receipt_sha256": item["receipt_sha256"],
                "truth_day_bounds_content_sha256": item["truth"]["content_sha256"],
                "truth_bounds_version": item["truth"]["bounds_version"],
                "truth_observation_revision_vector_sha256": item["truth"][
                    "observation_revision_vector_sha256"
                ],
                "truth_max_observation_fact_sha256": item["truth"][
                    "max_observation_fact_sha256"
                ],
                "truth_coverage_receipt_sha256": item["truth"][
                    "coverage_receipt_sha256"
                ],
                "truth_canonical_committed_at_utc": item["truth"][
                    "canonical_committed_at_utc"
                ],
                "station_day_end_utc": item["truth"]["station_day_end_utc"],
            }
            cohort_rows.append(
                {
                    **case_identity,
                    "case_contract_sha256": content_sha256(case_identity),
                }
            )
        train_case_set_sha256 = content_sha256(
            [item["case_contract_sha256"] for item in cohort_rows[:MIN_TRAIN_DAYS]]
        )
        holdout_case_set_sha256 = content_sha256(
            [item["case_contract_sha256"] for item in cohort_rows[MIN_TRAIN_DAYS:]]
        )
        cohort_scope_identity = {
            "station_id": station,
            "model_id": next(iter(models)),
            "provider_id": next(iter(provider_ids)),
            "transport_provider_id": TRANSPORT_PROVIDER_ID,
            "source_role": SOURCE_ROLE,
            "settlement_timezone": cohort[0]["target_timezone"],
            "lead_bucket": "d0",
            # Open-Meteo's historical daily archive does not expose the
            # upstream native run cycle.  Preserve that absence as null;
            # never manufacture a native-looking cycle label.
            "run_cycle": None,
            "run_cycle_scope": "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE",
            "season_bin": _season(cohort[0]["event_date"]),
            "cohort_rows": cohort_rows,
        }
        train_bias = sum(
            item["truth"]["official_tmax_c"] - item["forecast_daily_max_c"]
            for item in train
        ) / len(train)
        train_climate = sum(item["truth"]["official_tmax_c"] for item in train) / len(train)
        predictions: list[dict[str, Any]] = []
        model_errors: list[float] = []
        climate_errors: list[float] = []
        contract_ids: list[str] = []
        for item in holdout:
            actual = item["truth"]["official_tmax_c"]
            calibrated = item["forecast_daily_max_c"] + train_bias
            model_error = calibrated - actual
            climate_error = train_climate - actual
            model_errors.append(model_error)
            climate_errors.append(climate_error)
            contract_identity = {
                "contract_version": "deterministic_open_meteo_historical_oos_v2",
                "station_id": station,
                "event_date": item["event_date"],
                "target_timezone": item["target_timezone"],
                "provider_id": item["provider_id"],
                "transport_provider_id": TRANSPORT_PROVIDER_ID,
                "source_role": SOURCE_ROLE,
                "source_raw_sha256": item["raw_message_sha256"],
                "source_receipt_sha256": item["receipt_sha256"],
                "truth_day_bounds_content_sha256": item["truth"]["content_sha256"],
                "truth_bounds_version": item["truth"]["bounds_version"],
                "truth_observation_revision_vector_sha256": item["truth"][
                    "observation_revision_vector_sha256"
                ],
                "truth_max_observation_fact_sha256": item["truth"][
                    "max_observation_fact_sha256"
                ],
                "truth_coverage_receipt_sha256": item["truth"][
                    "coverage_receipt_sha256"
                ],
                "truth_canonical_committed_at_utc": item["truth"][
                    "canonical_committed_at_utc"
                ],
                "station_day_end_utc": item["truth"]["station_day_end_utc"],
                "receipt_class": HISTORICAL_RECEIPT_CLASS,
                "temporal_scope": TEMPORAL_SCOPE,
                "future_event_count": 0,
                "historical_oos_eligible": True,
            }
            contract_id = "det-om-hist-oos-" + content_sha256(contract_identity)[:40]
            contract_ids.append(contract_id)
            predictions.append({
                "schema": CANDIDATE_SCHEMA + "_prediction_v1",
                "event_date": item["event_date"],
                "decision_at_utc": item["reconstructed_at_utc"],
                "forecast_daily_max_c": item["forecast_daily_max_c"],
                "calibrated_daily_max_c": calibrated,
                "actual_tmax_c": actual,
                "error_c": model_error,
                "member_run_content_shas": [item["raw_message_sha256"]],
                "historical_oos_contract_id": contract_id,
                "receipt_class": HISTORICAL_RECEIPT_CLASS,
                "temporal_scope": TEMPORAL_SCOPE,
            })
        candidate_identity = {
            "schema": CANDIDATE_SCHEMA,
            "state": "PRODUCTION_NO_LIVE_OOS_PASS" if _rmse(model_errors) <= _rmse(climate_errors) + 1e-12 else "OOS_REJECTED",
            "station_id": station,
            "model_id": next(iter(models)),
            "provider_id": next(iter(provider_ids)),
            "transport_provider_id": TRANSPORT_PROVIDER_ID,
            "source_role": SOURCE_ROLE,
            "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
            "formal_p0_eligible": False,
            "native_arrival_index_eligible": False,
            "p1_eligible": False,
            "oos_event_temporal_scope": TEMPORAL_SCOPE,
            "future_event_count": 0,
            "season_bin": _season(cohort[0]["event_date"]),
            "settlement_timezone": cohort[0]["target_timezone"],
            "lead_bucket": "d0",
            "run_cycle": None,
            "run_cycle_scope": "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE",
            "min_train_days": MIN_TRAIN_DAYS,
            "min_holdout_days": MIN_HOLDOUT_DAYS,
            "train_start_date": train[0]["event_date"],
            "train_end_date": train[-1]["event_date"],
            "holdout_start_date": holdout[0]["event_date"],
            "holdout_end_date": holdout[-1]["event_date"],
            "train_n": len(train),
            "holdout_n": len(holdout),
            "cohort_rows": cohort_rows,
            "train_case_set_sha256": train_case_set_sha256,
            "holdout_case_set_sha256": holdout_case_set_sha256,
            "strict_oos_cohort_sha256": content_sha256(cohort_scope_identity),
            "cohort_capture_receipt_sha256s": sorted(
                {item["receipt_sha256"] for item in cohort}
            ),
            "forecast_extraction_version": (
                "bayes_basket_target_historical_aggregator_rebuild_evidence_v1"
            ),
            "train_bias_c": train_bias,
            "train_climatology_tmax_c": train_climate,
            "model_metrics": {
                "mae_c": sum(abs(value) for value in model_errors) / len(model_errors),
                "rmse_c": _rmse(model_errors),
                "mean_error_c": sum(model_errors) / len(model_errors),
            },
            "climatology_baseline_metrics": {
                "mae_c": sum(abs(value) for value in climate_errors) / len(climate_errors),
                "rmse_c": _rmse(climate_errors),
                "mean_error_c": sum(climate_errors) / len(climate_errors),
            },
            "historical_oos_contract_ids": contract_ids,
            "predictions": predictions,
        }
        candidates.append({**candidate_identity, "content_sha256": content_sha256(candidate_identity)})
    identity = {
        "schema": SCHEMA,
        "as_of_utc": _stamp(as_of),
        "provider_id": next(iter(provider_ids)),
        "model_id": next(iter(models)),
        "transport_provider_id": TRANSPORT_PROVIDER_ID,
        "source_role": SOURCE_ROLE,
        "capture_receipt_sha256s": sorted(set(capture_shas)),
        "station_count": len(timezones),
        "curve_count": len(by_scope),
        "truth_count": len(truth),
        "truth_source": "canonical_ssot_fact_v12_awc_observation_contract_v1",
        "legacy_truth_projection_read": False,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "shortfalls": shortfalls,
        "missing_truth_scopes": missing_truth,
        "model_lineage_gaps": model_lineage_gaps,
        "oos_policy": {
            "temporal_scope": TEMPORAL_SCOPE,
            "future_event_count": 0,
            "min_train_days": MIN_TRAIN_DAYS,
            "min_holdout_days": MIN_HOLDOUT_DAYS,
            "native_arrival_index_written": False,
            "p1_authority_written": False,
            "formal_live_authority_written": False,
            "production_database_written": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "state": "READY_CANDIDATES" if candidates else "PENDING_STRICT_OOS_SAMPLES",
        "formal_p0_eligible": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "CANDIDATE_SCHEMA",
    "HISTORICAL_RECEIPT_CLASS",
    "MIN_HOLDOUT_DAYS",
    "MIN_TRAIN_DAYS",
    "SCHEMA",
    "SOURCE_ROLE",
    "TEMPORAL_SCOPE",
    "build_deterministic_republisher_oos",
]
