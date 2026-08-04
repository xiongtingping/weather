"""Concrete closed-history builders for the d0/d2 model scopes.

The builder registry deliberately owns one scope at a time.  This module is
the implementation behind the two scopes that have retained, independent
historical inputs today:

* ``d2 × global_deterministic`` reads only ``PREVIOUS_DAY2`` five-model rows;
* ``d0 × global_ensemble`` reads only governed historical ensemble members.

Both paths are read-only and produce a canonical contract.  They never fetch
forecast data, mutate old calibration projections, create a native-arrival
claim, or touch P1/order state.  A caller that wants to persist the result
must use :func:`bayes_basket_target.model_builders.ingest_built_scope`, which
in turn uses the canonical ingress as the only write path.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .canonical_contract import (
    CONTRACT_VERSION,
    CanonicalContract,
    SCHEMA as CANONICAL_CONTRACT_SCHEMA,
)
from .contracts import ContractError, MODEL_IDS, content_sha256, model_set_hash
from .native_ensemble_oos import (
    HISTORICAL_OOS_TEMPORAL_SCOPE,
    NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS,
    NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS,
    _load_cases,
    _load_systems,
    build_native_ensemble_oos,
)
from .resource_bounds import load_bounded_json
from .settlement_time import settlement_day_window


ROOT = Path(__file__).resolve().parents[1]
D2_LEAD_BUCKET = "d2"
D2_RUN_CYCLE = "PREVIOUS_DAY2"
D0_LEAD_BUCKET = "d0"
D0_RUN_CYCLE = "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
EXPECTED_D2_MODELS = tuple(sorted(MODEL_IDS))
# This is a fixed, closed historical split.  It is intentionally not advanced
# from ``as_of`` and never includes a forward observation.
D2_TRAIN_DATES = tuple(f"2026-07-{day:02d}" for day in range(1, 21))
D2_HOLDOUT_DATES = tuple(f"2026-07-{day:02d}" for day in range(22, 30))
D2_ALL_DATES = D2_TRAIN_DATES + D2_HOLDOUT_DATES
SCOPE_BUILD_SCHEMA = "bayes_basket_target_closed_historical_scope_build_v1"


def _utc(value: Any, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _finite(value: Any, code: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(code) from exc
    if not math.isfinite(result):
        raise ContractError(code)
    return result


def _normalise_stations(stations: Iterable[str]) -> tuple[str, ...]:
    values = tuple(sorted({str(value or "").strip().upper() for value in stations if str(value or "").strip()}))
    if not values:
        raise ContractError("historical_scope_builder_stations_missing")
    return values


def _read_only_connection(db: Path | str) -> sqlite3.Connection:
    database = Path(db).expanduser().resolve(strict=True)
    if database.is_symlink():
        raise ContractError("historical_scope_builder_database_symlink_forbidden")
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _station_timezone(conn: sqlite3.Connection, station: str) -> str:
    row = conn.execute(
        "SELECT settlement_timezone,market_registry_timezone_count "
        "FROM calibration_station_market_timezone_binding WHERE station_id=?",
        (station,),
    ).fetchone()
    if row is None or int(row[1] or 0) != 1:
        raise ContractError("historical_scope_builder_timezone_binding_missing")
    timezone_name = str(row[0] or "").strip()
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ContractError("historical_scope_builder_timezone_invalid") from exc
    return timezone_name


def _closed_awc_truth(
    conn: sqlite3.Connection,
    *,
    station: str,
    event_date: str,
    timezone_name: str,
    as_of: datetime,
) -> dict[str, Any] | None:
    """Return the current accepted AWC revision only after the local day ends."""

    rows = conn.execute(
        "SELECT truth_id,station_id,local_date,settlement_timezone,"
        "official_tmax_c,source_lane,revision_no,canonical_committed_at_utc,"
        "source_payload_sha256,authority_scope,source_role,content_sha256 "
        "FROM calibration_station_daily_high_truth WHERE station_id=? "
        "AND local_date=? AND source_lane='AWC' AND qc_status='ACCEPTED' "
        "AND authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') "
        "ORDER BY revision_no DESC,canonical_committed_at_utc DESC,truth_id DESC",
        (station, event_date),
    ).fetchall()
    if not rows:
        return None
    day_end = _utc(
        settlement_day_window(event_date, timezone_name)["target_day_end_utc"],
        "historical_scope_builder_day_end_invalid",
    )
    for row in rows:
        if str(row[3] or "").strip() != timezone_name:
            continue
        committed_text = str(row[7] or "").strip()
        if not committed_text:
            continue
        try:
            committed = _utc(committed_text, "historical_scope_builder_truth_timestamp_invalid")
        except ContractError:
            continue
        if committed < day_end or committed > as_of:
            continue
        source_payload_sha = str(row[8] or "").strip().lower()
        truth_sha = str(row[11] or "").strip().lower()
        if len(source_payload_sha) != 64 or len(truth_sha) != 64:
            continue
        return {
            "truth_id": str(row[0]),
            "station_id": str(row[1]).upper(),
            "event_date": str(row[2]),
            "settlement_timezone": str(row[3]),
            "truth_value": _finite(row[4], "historical_scope_builder_truth_value_invalid"),
            "source_lane": str(row[5]).upper(),
            "truth_revision_no": int(row[6] or 0),
            "canonical_committed_at_utc": _stamp(committed),
            "truth_content_sha256": source_payload_sha,
            "authority_scope": str(row[9]),
            "source_role": str(row[10]),
            "row_content_sha256": truth_sha,
            "station_day_end_utc": _stamp(day_end),
        }
    return None


def _canonical_truth_fact(truth: Mapping[str, Any], timezone_name: str) -> dict[str, Any]:
    # Preserve the exact persisted source-role/authority pair.  The historical
    # projection name maps only to the canonical no-live name; a formal AWC
    # row remains formal.  Any other pair or timezone fails closed.
    source_role = str(truth.get("source_role") or "")
    source_authority = str(truth.get("authority_scope") or "")
    authority = {
        (
            "AWC_HISTORICAL_REPLAY_INGRESS",
            "NO_LIVE_REPLAY_CANDIDATE",
        ): AUTHORITY_SCOPE,
        (
            "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1",
            "FORMAL_NATIVE_ELIGIBLE",
        ): "FORMAL_NATIVE_ELIGIBLE",
    }.get((source_role, source_authority))
    if authority is None or str(truth.get("settlement_timezone") or "") != timezone_name:
        raise ContractError("historical_scope_builder_truth_authority_invalid")
    identity = {
        "truth_id": str(truth["truth_id"]),
        "station_id": str(truth["station_id"]).upper(),
        "event_date": str(truth["event_date"]),
        "truth_revision_no": int(truth["truth_revision_no"]),
        "truth_value": float(truth["truth_value"]),
        "truth_content_sha256": str(truth["truth_content_sha256"]),
        "canonical_committed_at_utc": str(truth["canonical_committed_at_utc"]),
        "station_day_end_utc": str(truth["station_day_end_utc"]),
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": source_role,
        "authority_scope": authority,
        "settlement_timezone": timezone_name,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _canonical_activation(
    *,
    activation_id: str,
    station_id: str,
    model_family: str,
    lead_bucket: str,
    run_cycle: str,
    candidate_models: Sequence[str],
    selected_models: Sequence[str],
    selected_weights: Mapping[str, float],
    available_model_set_hash: str,
    selection_evidence_sha256: str,
    artifact_content_sha256: str,
    generation: int,
) -> dict[str, Any]:
    selected = tuple(sorted({str(value).upper() for value in selected_models}))
    candidates = tuple(sorted({str(value).upper() for value in candidate_models}))
    weights = {str(key).upper(): float(value) for key, value in selected_weights.items()}
    identity = {
        "activation_id": activation_id,
        "station_id": station_id,
        "model_family": model_family,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "candidate_models": list(candidates),
        "selected_models": list(selected),
        "selected_weights": dict(sorted(weights.items())),
        "available_model_set_hash": available_model_set_hash,
        "selection_evidence_sha256": selection_evidence_sha256,
        "artifact_content_sha256": artifact_content_sha256,
        "activation_generation": int(generation),
        "authority_scope": AUTHORITY_SCOPE,
        "oos_role": "historical_strict_oos",
    }
    return {
        **identity,
        "activation_content_sha256": content_sha256(identity),
    }


def _contract(
    *,
    as_of_utc: str,
    builder_id: str,
    model_facts: Sequence[Mapping[str, Any]],
    truth_facts: Sequence[Mapping[str, Any]],
    residual_facts: Sequence[Mapping[str, Any]],
    activation_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    identity = {
        "schema": CANONICAL_CONTRACT_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": as_of_utc,
        "builder_id": builder_id,
        "model_facts": list(model_facts),
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": list(truth_facts),
        "residual_facts": list(residual_facts),
        "activation_facts": list(activation_facts),
    }
    packet = {**identity, "content_sha256": content_sha256(identity)}
    # Validate before returning it to the registry so malformed scopes fail at
    # the builder boundary rather than later in a production ingress attempt.
    CanonicalContract.from_mapping(packet)
    return packet


def _legacy_reference_truth(truth: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "truth_id": str(truth["truth_id"]),
        "station_id": str(truth["station_id"]),
        "local_date": str(truth["event_date"]),
        "official_tmax_c": float(truth["truth_value"]),
        "source_lane": "AWC",
        "revision_no": int(truth["truth_revision_no"]),
        "authority_scope": str(truth["authority_scope"]),
        "content_sha256": str(truth["row_content_sha256"]),
    }


def _d2_station_legacy_rows(
    conn: sqlite3.Connection,
    *,
    station: str,
    as_of: datetime,
) -> tuple[dict[str, list[dict[str, Any]]] | None, dict[str, Any]]:
    """Collect d2 inputs without looking at any d0/d1 row."""

    status: dict[str, Any] = {
        "station_id": station,
        "state": "BLOCKED",
        "blockers": [],
        "lead_bucket": D2_LEAD_BUCKET,
        "run_cycle": D2_RUN_CYCLE,
    }
    timezone_name = _station_timezone(conn, station)
    truth_by_date: dict[str, dict[str, Any]] = {}
    for event_date in D2_ALL_DATES:
        truth = _closed_awc_truth(
            conn,
            station=station,
            event_date=event_date,
            timezone_name=timezone_name,
            as_of=as_of,
        )
        if truth is not None:
            truth_by_date[event_date] = truth
    missing_truth = [day for day in D2_ALL_DATES if day not in truth_by_date]
    if missing_truth:
        status.update({
            "timezone": timezone_name,
            "missing_closed_awc_truth": missing_truth,
        })
        status["blockers"].append("CLOSED_AWC_TRUTH_MISSING_OR_UNVERIFIED")
        return None, status

    rows = conn.execute(
        "SELECT model_run_id,model_id,station_id,target_local_date,run_init_utc,"
        "lead_bucket,run_cycle,raw_tmax_c,source_class,aggregator_or_republisher,"
        "open_meteo,asof_status,historical_replay_eligible,"
        "prospective_strict_asof_eligible,authority_scope "
        "FROM calibration_historical_asof_model_run WHERE station_id=? "
        "AND lead_bucket=? AND run_cycle=? AND target_local_date IN ("
        + ",".join("?" for _ in D2_ALL_DATES)
        + ") ORDER BY target_local_date,model_id",
        (station, D2_LEAD_BUCKET, D2_RUN_CYCLE, *D2_ALL_DATES),
    ).fetchall()
    by_scope: dict[tuple[str, str], sqlite3.Row] = {}
    source_classes: set[str] = set()
    for row in rows:
        model = str(row[1]).upper()
        event_date = str(row[3])
        key = (event_date, model)
        if key in by_scope:
            status["blockers"].append("DUPLICATE_D2_MODEL_SCOPE")
            return None, status
        if model not in EXPECTED_D2_MODELS:
            status["blockers"].append("UNEXPECTED_D2_MODEL")
            return None, status
        expected_init = datetime.combine(
            date.fromisoformat(event_date) - timedelta(days=2),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        if _utc(row[4], "historical_scope_builder_d2_run_init_invalid") != expected_init:
            status["blockers"].append("D2_RUN_INIT_NOT_PREVIOUS_DAY2")
            return None, status
        # The retained five-model rows are governed republisher history.  A
        # native row is accepted only if its canonical source label is already
        # exact; no legacy alias is renamed here.
        source_class = str(row[8])
        if source_class not in {"aggregator_republisher", "native_official"}:
            status["blockers"].append("D2_MODEL_SOURCE_CLASS_UNSUPPORTED")
            return None, status
        source_classes.add(source_class)
        if int(row[12] or 0) != 1 or int(row[13] or 0) != 0:
            status["blockers"].append("D2_MODEL_STRICT_OOS_LINEAGE_INVALID")
            return None, status
        if source_class == "aggregator_republisher" and (
            int(row[9] or 0) != 1
            or int(row[10] or 0) != 1
            or str(row[11]) != "HISTORICAL_REPLAY_ONLY"
            or str(row[14]) != "NO_LIVE_REPLAY_CANDIDATE"
        ):
            status["blockers"].append("D2_REPUBLISHER_LINEAGE_INVALID")
            return None, status
        by_scope[key] = row
    missing_models = [
        (event_date, model)
        for event_date in D2_ALL_DATES
        for model in EXPECTED_D2_MODELS
        if (event_date, model) not in by_scope
    ]
    if missing_models:
        status.update({"timezone": timezone_name, "missing_model_scopes": missing_models})
        status["blockers"].append("EXACT_D2_MODEL_SCOPE_MISSING")
        return None, status
    if len(source_classes) != 1:
        status["blockers"].append("D2_MODEL_SOURCE_LINEAGE_MIXED")
        return None, status

    truth_references = [_legacy_reference_truth(truth_by_date[day]) for day in D2_ALL_DATES]
    model_references: list[dict[str, Any]] = []
    residual_evidence: list[dict[str, Any]] = []
    for event_date in D2_ALL_DATES:
        truth = truth_by_date[event_date]
        for model in EXPECTED_D2_MODELS:
            row = by_scope[(event_date, model)]
            model_references.append({
                "model_run_id": str(row[0]),
                "model_id": model,
                "station_id": station,
                "target_local_date": event_date,
                "run_init_utc": str(row[4]),
                "lead_bucket": D2_LEAD_BUCKET,
                "run_cycle": D2_RUN_CYCLE,
            })
            residual_evidence.append({
                "residual_id": (
                    f"canonical-d2-residual-{station}-{event_date}-{model}-"
                    f"{str(truth['truth_id'])[-16:]}"
                ),
                "truth_id": str(truth["truth_id"]),
                "model_run_id": str(row[0]),
                "station_id": station,
                "model_id": model,
                "lead_bucket": D2_LEAD_BUCKET,
                "run_cycle": D2_RUN_CYCLE,
                "e_y_minus_f_raw_c": (
                    float(truth["truth_value"])
                    - _finite(row[7], "historical_scope_builder_d2_forecast_invalid")
                ),
                "paired_asof_utc": _stamp(
                    _utc(row[4], "historical_scope_builder_d2_run_init_invalid")
                ),
            })
    status.update({
        "state": "READY",
        "timezone": timezone_name,
        "train_count": len(D2_TRAIN_DATES),
        "holdout_count": len(D2_HOLDOUT_DATES),
        "model_count": len(model_references),
        "truth_count": len(truth_references),
        "source_class": next(iter(source_classes)),
    })
    return {
        "truth_references": truth_references,
        "model_references": model_references,
        "residual_evidence": residual_evidence,
    }, status


def _d2_activation(
    *,
    station: str,
    generation: int,
    model_facts: Sequence[Mapping[str, Any]],
    truth_facts: Sequence[Mapping[str, Any]],
    residual_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    relevant_models = [
        fact for fact in model_facts
        if fact["station_id"] == station
        and fact["lead_bucket"] == D2_LEAD_BUCKET
        and fact["run_cycle"] == D2_RUN_CYCLE
    ]
    relevant_truths = [fact for fact in truth_facts if fact["station_id"] == station]
    relevant_residuals = [
        fact for fact in residual_facts
        if fact["station_id"] == station
        and fact["lead_bucket"] == D2_LEAD_BUCKET
        and fact["run_cycle"] == D2_RUN_CYCLE
    ]
    selection_identity = {
        "schema": "d2_global_deterministic_fixed_five_selection_v1",
        "station_id": station,
        "lead_bucket": D2_LEAD_BUCKET,
        "run_cycle": D2_RUN_CYCLE,
        "train_dates": list(D2_TRAIN_DATES),
        "holdout_dates": list(D2_HOLDOUT_DATES),
        "candidate_models": list(EXPECTED_D2_MODELS),
        "selected_models": list(EXPECTED_D2_MODELS),
        "selection_rule": "PRE_REGISTERED_FIXED_FIVE_NO_HOLDOUT_TUNING",
        "model_fact_content_sha256s": sorted(str(fact["content_sha256"]) for fact in relevant_models),
        "truth_fact_content_sha256s": sorted(str(fact["content_sha256"]) for fact in relevant_truths),
    }
    selection_sha = content_sha256(selection_identity)
    artifact_identity = {
        "schema": "d2_global_deterministic_historical_artifact_v1",
        "selection_evidence_sha256": selection_sha,
        "model_fact_content_sha256s": sorted(str(fact["content_sha256"]) for fact in relevant_models),
        "residual_fact_content_sha256s": sorted(str(fact["content_sha256"]) for fact in relevant_residuals),
        "train_count": len(D2_TRAIN_DATES),
        "holdout_count": len(D2_HOLDOUT_DATES),
        "authority_scope": AUTHORITY_SCOPE,
    }
    artifact_sha = content_sha256(artifact_identity)
    activation_id = (
        f"canonical-d2-global-deterministic-{station}-{artifact_sha[:24]}-g{generation}"
    )
    return _canonical_activation(
        activation_id=activation_id,
        station_id=station,
        model_family="global_deterministic",
        lead_bucket=D2_LEAD_BUCKET,
        run_cycle=D2_RUN_CYCLE,
        candidate_models=EXPECTED_D2_MODELS,
        selected_models=EXPECTED_D2_MODELS,
        selected_weights={model: 1.0 / len(EXPECTED_D2_MODELS) for model in EXPECTED_D2_MODELS},
        available_model_set_hash=model_set_hash(EXPECTED_D2_MODELS),
        selection_evidence_sha256=selection_sha,
        artifact_content_sha256=artifact_sha,
        generation=generation,
    )


def build_d2_global_deterministic_from_connection(
    conn: sqlite3.Connection,
    *,
    stations: Sequence[str],
    as_of_utc: str,
    generation: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build the d2 deterministic canonical packet from an already-open DB."""

    if int(generation) < 1:
        raise ContractError("historical_scope_builder_generation_invalid")
    as_of = _utc(as_of_utc, "historical_scope_builder_as_of_invalid")
    station_ids = _normalise_stations(stations)
    legacy_truths: list[dict[str, Any]] = []
    legacy_models: list[dict[str, Any]] = []
    legacy_residuals: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    ready_stations: list[str] = []
    for station in station_ids:
        try:
            part, status = _d2_station_legacy_rows(conn, station=station, as_of=as_of)
        except Exception as exc:
            part = None
            status = {
                "station_id": station,
                "state": "BLOCKED",
                "lead_bucket": D2_LEAD_BUCKET,
                "run_cycle": D2_RUN_CYCLE,
                "blockers": [f"BUILD_ERROR:{type(exc).__name__}:{exc}"],
            }
        statuses.append(status)
        if part is None:
            continue
        ready_stations.append(station)
        legacy_truths.extend(part["truth_references"])
        legacy_models.extend(part["model_references"])
        legacy_residuals.extend(part["residual_evidence"])
    receipt_identity: dict[str, Any] = {
        "schema": SCOPE_BUILD_SCHEMA,
        "builder_id": "d2.global_deterministic.historical_strict.v1",
        "as_of_utc": _stamp(as_of),
        "lead_bucket": D2_LEAD_BUCKET,
        "run_cycle": D2_RUN_CYCLE,
        "model_family": "global_deterministic",
        "station_ids_requested": list(station_ids),
        "station_ids_ready": ready_stations,
        "station_ids_blocked": [item["station_id"] for item in statuses if item.get("state") != "READY"],
        "station_statuses": statuses,
        "train_dates": list(D2_TRAIN_DATES),
        "holdout_dates": list(D2_HOLDOUT_DATES),
        "train_count": len(D2_TRAIN_DATES),
        "holdout_count": len(D2_HOLDOUT_DATES),
        "network_fetches": 0,
        "writes_target_database": False,
        "writes_production_database": False,
        "native_arrival_index_written": False,
        "p1_authority_written": False,
        "formal_live_authority_written": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "future_data_is_not_oos": True,
    }
    if not ready_stations:
        return None, {**receipt_identity, "state": "BLOCKED", "content_sha256": content_sha256(receipt_identity)}
    receipt_identity["retired_legacy_input_counts"] = {
        "truth_references": len(legacy_truths),
        "model_references": len(legacy_models),
        "residual_evidence": len(legacy_residuals),
    }
    blocked_statuses = [
        {
            **status,
            "state": "BLOCKED",
            "blockers": sorted(
                set(status.get("blockers") or [])
                | {"LEGACY_PROJECTION_RETIRED"}
            ),
        }
        for status in statuses
    ]
    receipt_identity.update({
        "state": "BLOCKED",
        "station_ids_ready": [],
        "station_ids_blocked": list(station_ids),
        "station_statuses": blocked_statuses,
        "blockers": ["LEGACY_PROJECTION_RETIRED"],
    })
    return None, {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }


def build_d2_global_deterministic(
    *,
    db: Path | str,
    stations: Sequence[str],
    as_of_utc: str,
    generation: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    with _read_only_connection(db) as conn:
        return build_d2_global_deterministic_from_connection(
            conn,
            stations=stations,
            as_of_utc=as_of_utc,
            generation=generation,
        )


def _global_ensemble_system_ids() -> tuple[str, ...]:
    """Read the registered global Open-Meteo systems without naming regionals."""

    config_path = ROOT / "config/bayes_basket_target/station_regional_model_pool_v1.json"
    config = load_bounded_json(config_path, limit=1024 * 1024)
    if not isinstance(config, Mapping):
        raise ContractError("historical_scope_builder_global_pool_invalid")
    ids: set[str] = set()
    for section in (
        "global_ensemble_aggregator_capture_models",
        "global_aggregator_capture_models",
    ):
        rows = config.get(section)
        if not isinstance(rows, Mapping):
            continue
        for key, value in rows.items():
            if not isinstance(value, Mapping):
                continue
            if str(value.get("geographic_scope") or "") != "global":
                continue
            system_id = str(value.get("system_id") or "").strip().upper()
            if system_id:
                ids.add(system_id)
    if len(ids) < 2:
        raise ContractError("historical_scope_builder_global_ensemble_pool_short")
    return tuple(sorted(ids))


def _d0_model_fact(
    *,
    station: str,
    system_id: str,
    provider_id: str,
    event_date: str,
    timezone_name: str,
    mean: float,
    member_set_sha256: str,
    native_run_init_utc: str,
    receipt_at_utc: str,
) -> dict[str, Any]:
    # ``native_run_init_utc`` comes from the retained member rows.  It is
    # deliberately not derived from the Open-Meteo receipt timestamp.
    identity = {
        "station_id": station,
        "model_id": "",
        "ensemble_system_id": system_id,
        "model_family": "global_ensemble",
        "provider_id": provider_id,
        "source_class": "aggregator_republisher",
        "event_date": event_date,
        "run_init_utc": native_run_init_utc,
        "lead_bucket": D0_LEAD_BUCKET,
        "run_cycle": D0_RUN_CYCLE,
        "settlement_timezone": timezone_name,
        "forecast_value": mean,
        "raw_object_sha256": member_set_sha256,
        "oos_role": "historical_strict_oos",
        "closed_day": True,
        "strict_asof": True,
        "native_arrival_indexed": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "published_at_utc": "",
        "received_at_utc": receipt_at_utc,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _d0_case_native_run_init_utc(
    case: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Validate one selected d0 member set's upstream run initialization.

    A republisher receipt proves retrieval time only.  The canonical d0 key
    needs the independently retained upstream run initialization for every
    member, with one normalized value across the complete member set.
    """

    members = case.get("member_native_run_init_utcs")
    values = case.get("values")
    if (
        not isinstance(members, Sequence)
        or isinstance(members, (str, bytes))
        or not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or not members
        or len(members) != len(values)
    ):
        return None, "MEMBER_RUN_INIT_SET_MISSING_OR_CARDINALITY_INVALID"
    normalized: list[str] = []
    for raw in members:
        if not isinstance(raw, str) or not raw.strip():
            return None, "MEMBER_RUN_INIT_MISSING"
        try:
            normalized.append(
                _stamp(
                    _utc(
                        raw,
                        "historical_scope_builder_d0_native_run_init_invalid",
                    )
                )
            )
        except ContractError:
            return None, "MEMBER_RUN_INIT_NOT_UTC"
    if len(set(normalized)) != 1:
        return None, "MEMBER_RUN_INIT_INCONSISTENT"
    return normalized[0], None


def _d0_residual_fact(
    *,
    station: str,
    system_id: str,
    event_date: str,
    truth_id: str,
    model_fact_sha: str,
    residual: float,
    receipt_at_utc: str,
) -> dict[str, Any]:
    identity = {
        "residual_id": f"canonical-d0-ensemble-residual-{station}-{system_id}-{event_date}-{truth_id[-16:]}",
        "truth_id": truth_id,
        "model_fact_content_sha256": model_fact_sha,
        "station_id": station,
        "model_id": system_id,
        "event_date": event_date,
        "lead_bucket": D0_LEAD_BUCKET,
        "run_cycle": D0_RUN_CYCLE,
        "residual_value": residual,
        "paired_asof_utc": receipt_at_utc,
        "oos_role": "historical_strict_oos",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _d0_candidate_activation(
    *,
    candidate: Mapping[str, Any],
    generation: int,
    model_facts: Sequence[Mapping[str, Any]],
    residual_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    station = str(candidate["station_id"]).upper()
    systems = tuple(sorted(str(value).upper() for value in candidate["selected_system_ids"]))
    selection_identity = {
        "schema": "d0_global_ensemble_historical_selection_v1",
        "station_id": station,
        "lead_bucket": D0_LEAD_BUCKET,
        "run_cycle": D0_RUN_CYCLE,
        "selected_system_ids": list(systems),
        "system_weights": {
            str(key).upper(): float(value)
            for key, value in candidate["system_weights"].items()
        },
        "independence_family_ids": sorted(str(value) for value in candidate["independence_family_ids"]),
        "train_start_date": str(candidate["train_start_date"]),
        "train_end_date": str(candidate["train_end_date"]),
        "holdout_start_date": str(candidate["holdout_start_date"]),
        "holdout_end_date": str(candidate["holdout_end_date"]),
        "threshold_profile": str(candidate["threshold_profile"]),
        "model_fact_content_sha256s": sorted(
            str(fact["content_sha256"])
            for fact in model_facts
            if fact["station_id"] == station
            and fact["ensemble_system_id"] in systems
        ),
    }
    selection_sha = content_sha256(selection_identity)
    artifact_identity = {
        "schema": "d0_global_ensemble_historical_artifact_v1",
        "selection_evidence_sha256": selection_sha,
        "residual_fact_content_sha256s": sorted(
            str(fact["content_sha256"])
            for fact in residual_facts
            if fact["station_id"] == station and fact["model_id"] in systems
        ),
        "oos_temporal_scope": HISTORICAL_OOS_TEMPORAL_SCOPE,
        "future_event_count": 0,
        "authority_scope": AUTHORITY_SCOPE,
    }
    artifact_sha = content_sha256(artifact_identity)
    return _canonical_activation(
        activation_id=(
            f"canonical-d0-global-ensemble-{station}-{artifact_sha[:24]}-g{generation}"
        ),
        station_id=station,
        model_family="global_ensemble",
        lead_bucket=D0_LEAD_BUCKET,
        run_cycle=D0_RUN_CYCLE,
        candidate_models=systems,
        selected_models=systems,
        selected_weights={str(key).upper(): float(value) for key, value in candidate["system_weights"].items()},
        available_model_set_hash=str(candidate["available_system_set_hash"]),
        selection_evidence_sha256=selection_sha,
        artifact_content_sha256=artifact_sha,
        generation=generation,
    )


def _d0_station_contract_parts(
    conn: sqlite3.Connection,
    *,
    station: str,
    as_of_utc: str,
    generation: int,
    system_ids: Sequence[str],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    timezone_name = _station_timezone(conn, station)
    report = build_native_ensemble_oos(
        conn,
        station_id=station,
        as_of_utc=as_of_utc,
        min_train_days=NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS,
        min_holdout_days=NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS,
        system_mask=tuple(system_ids),
    )
    candidates = [
        candidate for candidate in report.get("cross_candidates") or []
        if str(candidate.get("lead_bucket")) == D0_LEAD_BUCKET
        and str(candidate.get("state")) == "PRODUCTION_NO_LIVE_OOS_PASS"
    ]
    status: dict[str, Any] = {
        "station_id": station,
        "lead_bucket": D0_LEAD_BUCKET,
        "run_cycle": D0_RUN_CYCLE,
        "model_family": "global_ensemble",
        "state": "BLOCKED",
        "blockers": [],
        "train_count": NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS,
        "holdout_count": NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS,
    }
    if not candidates:
        status["blockers"].append("D0_GLOBAL_ENSEMBLE_STRICT_OOS_NOT_READY")
        status["readiness"] = [
            item for item in report.get("readiness") or []
            if str(item.get("lead_bucket")) == D0_LEAD_BUCKET
            and str(item.get("ensemble_system_id")) in set(system_ids)
        ]
        return None, status
    # Canonical activation has no season field.  A second passing candidate in
    # its same station/lead/run scope would be ambiguous rather than an excuse
    # to silently pick by OOS score.
    if len(candidates) != 1:
        status["blockers"].append("D0_CANONICAL_ACTIVATION_SCOPE_AMBIGUOUS")
        return None, status
    candidate = candidates[0]
    selected = tuple(sorted(str(value).upper() for value in candidate["selected_system_ids"]))
    transports = {
        str(candidate["system_transport_provider_ids"].get(system) or "")
        for system in selected
    }
    cycles = {
        str(candidate["run_cycle_by_system"].get(system) or "")
        for system in selected
    }
    if (
        not bool(candidate.get("republisher_fallback_used"))
        or transports != {"OPEN_METEO"}
        or cycles != {D0_RUN_CYCLE}
        or str(candidate.get("run_cycle")) != D0_RUN_CYCLE
    ):
        status["blockers"].append("D0_REPUBLISHER_LINEAGE_OR_CYCLE_INVALID")
        return None, status
    systems = _load_systems(conn)
    cases_by_scope = _load_cases(conn, station_id=station, as_of_utc=as_of_utc)
    model_facts: list[dict[str, Any]] = []
    truth_by_id: dict[str, dict[str, Any]] = {}
    residual_facts: list[dict[str, Any]] = []
    for system in selected:
        evaluation = candidate["system_evaluations"].get(system)
        if not isinstance(evaluation, Mapping):
            status["blockers"].append("D0_SYSTEM_EVALUATION_MISSING")
            return None, status
        scope = (
            system,
            str(candidate["season_bin"]),
            D0_LEAD_BUCKET,
            D0_RUN_CYCLE,
        )
        cases = cases_by_scope.get(scope)
        if not cases:
            status["blockers"].append("D0_EXACT_HISTORICAL_MEMBER_SCOPE_MISSING")
            return None, status
        start = str(evaluation["train_start_date"])
        end = str(evaluation["holdout_end_date"])
        selected_cases = [
            item for item in cases
            if start <= str(item["event_date"]) <= end
        ]
        expected_case_count = int(evaluation["train_n"]) + int(evaluation["holdout_n"])
        if len(selected_cases) != expected_case_count:
            status["blockers"].append("D0_COHORT_CARDINALITY_MISMATCH")
            return None, status
        for case in selected_cases:
            event_date = str(case["event_date"])
            native_run_init_utc, run_init_reason = _d0_case_native_run_init_utc(case)
            if native_run_init_utc is None:
                status["blockers"].append("D0_UPSTREAM_RUN_INIT_UNPROVEN")
                status.setdefault("upstream_run_init_failures", []).append({
                    "system_id": system,
                    "event_date": event_date,
                    "reason": run_init_reason,
                })
                return None, status
            truth = _closed_awc_truth(
                conn,
                station=station,
                event_date=event_date,
                timezone_name=timezone_name,
                as_of=_utc(as_of_utc, "historical_scope_builder_as_of_invalid"),
            )
            if truth is None or abs(float(truth["truth_value"]) - float(case["actual_tmax_c"])) > 1e-9:
                status["blockers"].append("D0_TRUTH_REVISION_SCOPE_MISMATCH")
                return None, status
            raw_hash = str(case["member_set_sha256"])
            receipt_at = _stamp(
                _utc(case["aggregator_snapshot_at_utc"], "historical_scope_builder_d0_receipt_invalid")
            )
            values = [_finite(value, "historical_scope_builder_d0_member_value_invalid") for value in case["values"]]
            if not values:
                status["blockers"].append("D0_MEMBER_SET_EMPTY")
                return None, status
            fact = _d0_model_fact(
                station=station,
                system_id=system,
                provider_id=str((systems.get(system) or {}).get("provider_id") or ""),
                event_date=event_date,
                timezone_name=timezone_name,
                mean=sum(values) / len(values),
                member_set_sha256=raw_hash,
                native_run_init_utc=native_run_init_utc,
                receipt_at_utc=receipt_at,
            )
            if not fact["provider_id"]:
                status["blockers"].append("D0_SYSTEM_PROVIDER_MISSING")
                return None, status
            model_facts.append(fact)
            truth_fact = _canonical_truth_fact(truth, timezone_name)
            truth_by_id[truth_fact["truth_id"]] = truth_fact
            residual_facts.append(_d0_residual_fact(
                station=station,
                system_id=system,
                event_date=event_date,
                truth_id=str(truth["truth_id"]),
                model_fact_sha=str(fact["content_sha256"]),
                residual=float(truth["truth_value"]) - float(fact["forecast_value"]),
                receipt_at_utc=receipt_at,
            ))
    activation = _d0_candidate_activation(
        candidate=candidate,
        generation=generation,
        model_facts=model_facts,
        residual_facts=residual_facts,
    )
    status.update({
        "state": "READY",
        "source_class": "aggregator_republisher",
        "selected_system_ids": list(selected),
        "independence_family_ids": list(candidate["independence_family_ids"]),
        "train_count": int(candidate["min_train_days"]),
        "holdout_count": int(candidate["min_holdout_days"]),
        "threshold_profile": str(candidate["threshold_profile"]),
    })
    return {
        "model_facts": model_facts,
        "truth_facts": list(truth_by_id.values()),
        "residual_facts": residual_facts,
        "activation_facts": [activation],
    }, status


def build_d0_global_ensemble_from_connection(
    conn: sqlite3.Connection,
    *,
    stations: Sequence[str],
    as_of_utc: str,
    generation: int,
    system_ids: Sequence[str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build no-live d0 global ensemble facts from only closed historical data."""

    if int(generation) < 1:
        raise ContractError("historical_scope_builder_generation_invalid")
    _utc(as_of_utc, "historical_scope_builder_as_of_invalid")
    station_ids = _normalise_stations(stations)
    systems = tuple(sorted({str(value).upper() for value in (system_ids or _global_ensemble_system_ids())}))
    if len(systems) < 2:
        raise ContractError("historical_scope_builder_global_ensemble_pool_short")
    model_facts: list[dict[str, Any]] = []
    truth_by_id: dict[str, dict[str, Any]] = {}
    residual_facts: list[dict[str, Any]] = []
    activation_facts: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    ready_stations: list[str] = []
    for station in station_ids:
        try:
            part, status = _d0_station_contract_parts(
                conn,
                station=station,
                as_of_utc=as_of_utc,
                generation=int(generation),
                system_ids=systems,
            )
        except Exception as exc:
            part = None
            status = {
                "station_id": station,
                "state": "BLOCKED",
                "lead_bucket": D0_LEAD_BUCKET,
                "run_cycle": D0_RUN_CYCLE,
                "blockers": [f"BUILD_ERROR:{type(exc).__name__}:{exc}"],
            }
        statuses.append(status)
        if part is None:
            continue
        ready_stations.append(station)
        model_facts.extend(part["model_facts"])
        for fact in part["truth_facts"]:
            truth_by_id[str(fact["truth_id"])] = fact
        residual_facts.extend(part["residual_facts"])
        activation_facts.extend(part["activation_facts"])
    as_of = _stamp(_utc(as_of_utc, "historical_scope_builder_as_of_invalid"))
    receipt_identity: dict[str, Any] = {
        "schema": SCOPE_BUILD_SCHEMA,
        "builder_id": "d0.global_ensemble.historical_strict.v1",
        "as_of_utc": as_of,
        "lead_bucket": D0_LEAD_BUCKET,
        "run_cycle": D0_RUN_CYCLE,
        "model_family": "global_ensemble",
        "station_ids_requested": list(station_ids),
        "station_ids_ready": ready_stations,
        "station_ids_blocked": [item["station_id"] for item in statuses if item.get("state") != "READY"],
        "station_statuses": statuses,
        "global_system_ids": list(systems),
        "train_count": NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS,
        "holdout_count": NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS,
        "threshold_profile": "PRODUCTION_NO_LIVE_BOOTSTRAP_2_TRAIN_1_HISTORICAL_HOLDOUT",
        "network_fetches": 0,
        "writes_target_database": False,
        "writes_production_database": False,
        "native_arrival_index_written": False,
        "p1_authority_written": False,
        "formal_live_authority_written": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "future_data_is_not_oos": True,
    }
    if not ready_stations:
        return None, {**receipt_identity, "state": "BLOCKED", "content_sha256": content_sha256(receipt_identity)}
    packet = _contract(
        as_of_utc=as_of,
        builder_id="d0.global_ensemble.historical_strict.v1",
        model_facts=sorted(model_facts, key=lambda item: item["scope_key"] if "scope_key" in item else (item["station_id"], item["ensemble_system_id"], item["event_date"])),
        truth_facts=sorted(truth_by_id.values(), key=lambda item: (item["station_id"], item["event_date"], item["truth_revision_no"])),
        residual_facts=sorted(residual_facts, key=lambda item: (item["station_id"], item["model_id"], item["event_date"])),
        activation_facts=sorted(activation_facts, key=lambda item: item["station_id"]),
    )
    receipt_identity.update({
        "state": "BUILT",
        "canonical_contract_content_sha256": packet["content_sha256"],
        "validated_counts": {
            "model": len(packet["model_facts"]),
            "truth": len(packet["truth_facts"]),
            "residual": len(packet["residual_facts"]),
            "activation": len(packet["activation_facts"]),
        },
    })
    return packet, {**receipt_identity, "content_sha256": content_sha256(receipt_identity)}


def build_d0_global_ensemble(
    *,
    db: Path | str,
    stations: Sequence[str],
    as_of_utc: str,
    generation: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    with _read_only_connection(db) as conn:
        return build_d0_global_ensemble_from_connection(
            conn,
            stations=stations,
            as_of_utc=as_of_utc,
            generation=generation,
        )


__all__ = [
    "D0_LEAD_BUCKET",
    "D0_RUN_CYCLE",
    "D2_ALL_DATES",
    "D2_HOLDOUT_DATES",
    "D2_LEAD_BUCKET",
    "D2_RUN_CYCLE",
    "D2_TRAIN_DATES",
    "build_d0_global_ensemble",
    "build_d0_global_ensemble_from_connection",
    "build_d2_global_deterministic",
    "build_d2_global_deterministic_from_connection",
]
