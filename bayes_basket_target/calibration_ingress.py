"""Bounded, append-only ingress for target-owned calibration base facts.

Historical replay is retained in the same SSOT but carries an explicit
``NO_LIVE_REPLAY_CANDIDATE`` authority.  It can support reviewed no-live
calibration later, but can never be mistaken for native strict-as-of evidence
or satisfy formal promotion by itself.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any, Mapping, Sequence

from .activation import assert_target_database_owner
from .contracts import (
    MODEL_IDS,
    MIN_FORMAL_ENSEMBLE_MODELS,
    ContractError,
    content_sha256,
)
from .et_history import assert_event_day_closed
from .model_sources import (
    HISTORICAL_REPLAY_ONLY_PROVIDERS,
    model_id_for_provider,
    provider_role,
    provider_source_class,
)
from .resource_bounds import assert_connection_target_capacity, assert_process_memory_budget
from .ssot import production_ssot_schema_status


SCHEMA = "bayes_basket_target_calibration_evidence_v1"
INGRESS_SCHEMA = "bayes_basket_target_calibration_base_ingress_v1"
FORMAL_NATIVE = "FORMAL_NATIVE_ELIGIBLE"
NO_LIVE_REPLAY = "NO_LIVE_REPLAY_CANDIDATE"
TEMPORARY_AWC_LABEL = "TEMPORARY_AWC_CALIBRATION_LABEL"
MAX_STATIONS = 512
MAX_TRUTH_ROWS = 20_000
MAX_MODEL_ROWS = 60_000
MAX_RESIDUAL_ROWS = 60_000


def _rows(packet: Mapping[str, Any], key: str, *, limit: int) -> list[dict[str, Any]]:
    value = packet.get(key)
    if not isinstance(value, list):
        raise ContractError(f"{key}_list_required")
    if len(value) > limit:
        raise ContractError(f"{key}_row_limit_exceeded")
    if any(not isinstance(item, Mapping) for item in value):
        raise ContractError(f"{key}_row_invalid")
    return [dict(item) for item in value]


def _optional_rows(
    packet: Mapping[str, Any],
    key: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if key not in packet:
        return []
    return _rows(packet, key, limit=limit)


def _text(row: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = str(row.get(key) or "").strip()
    if not value and not allow_empty:
        raise ContractError(f"{key}_missing")
    return value


def _sha(row: Mapping[str, Any], key: str) -> str:
    value = _text(row, key).lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ContractError(f"{key}_invalid")
    return value


def _verify_content_address(row: Mapping[str, Any], *, label: str) -> None:
    supplied = _sha(row, "content_sha256")
    identity = {key: value for key, value in row.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError(f"{label}_content_sha256_mismatch")


def _insert_once(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: Sequence[str],
    values: Sequence[Any],
    primary_key: str,
    primary_value: str,
) -> bool:
    existing = conn.execute(
        f"SELECT {','.join(columns)} FROM {table} WHERE {primary_key}=?",
        (primary_value,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != tuple(values):
            raise ContractError(f"{table}_content_conflict")
        return False
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        tuple(values),
    )
    return True


def _model_authority(row: Mapping[str, Any]) -> str:
    timestamps_complete = bool(row.get("timestamps_complete"))
    aggregator = bool(row.get("aggregator_or_republisher"))
    published = _text(row, "published_at_utc", allow_empty=True)
    received = _text(row, "received_at_utc", allow_empty=True)
    prospective = bool(row.get("prospective_strict_asof_eligible"))
    if timestamps_complete and not aggregator and published and received and prospective:
        return FORMAL_NATIVE
    return NO_LIVE_REPLAY


def ingest_calibration_base_facts(
    conn: sqlite3.Connection,
    packet: Mapping[str, Any],
    *,
    commit: bool = False,
) -> dict[str, Any]:
    """Validate and atomically append canonical truth/model/residual facts."""

    if not isinstance(packet, Mapping) or packet.get("schema") != SCHEMA:
        raise ContractError("calibration_evidence_schema_invalid")
    temporal = packet.get("temporal_boundary")
    checks = packet.get("checks")
    boundary = packet.get("boundary")
    if (
        not isinstance(temporal, Mapping)
        or temporal.get("closed_settlement_event_days_only") is not True
    ):
        raise ContractError("calibration_closed_settlement_boundary_required")
    if not isinstance(checks, Mapping):
        raise ContractError("calibration_truth_checks_missing")
    canonical_truth = checks.get("truth_source_canonical_ingress") is True
    temporary_awc_truth = (
        checks.get("temporary_awc_calibration_label_explicit") is True
    )
    if not canonical_truth and not temporary_awc_truth:
        raise ContractError("calibration_truth_authority_required")
    if not isinstance(boundary, Mapping) or boundary.get("calls_order_gateway") is not False:
        raise ContractError("calibration_gateway_boundary_invalid")
    if boundary.get("places_orders") is not False or boundary.get("live_order_authorized") is not False:
        raise ContractError("calibration_live_boundary_invalid")
    if temporary_awc_truth and any(
        boundary.get(key) is not False
        for key in (
            "enters_realtime_truth",
            "enters_p1",
            "enters_hard_no",
        )
    ):
        raise ContractError("temporary_awc_calibration_boundary_invalid")

    stations = _rows(packet, "station_registry", limit=MAX_STATIONS)
    truths = _rows(packet, "truth_evidence", limit=MAX_TRUTH_ROWS)
    truth_references = _optional_rows(
        packet,
        "truth_references",
        limit=MAX_TRUTH_ROWS,
    )
    models = _rows(packet, "calibration_model_rows", limit=MAX_MODEL_ROWS)
    model_references = _optional_rows(
        packet,
        "model_references",
        limit=MAX_MODEL_ROWS,
    )
    residuals = _rows(packet, "residual_evidence", limit=MAX_RESIDUAL_ROWS)
    if (
        not stations
        or not (truths or truth_references)
        or not (models or model_references)
        or not residuals
    ):
        raise ContractError("calibration_base_facts_incomplete")
    assert_process_memory_budget()

    as_of_utc = _text(packet, "as_of_utc")
    station_ids: set[str] = set()
    station_timezones: dict[str, str] = {}
    for row in stations:
        _verify_content_address(row, label="station_registry")
        station_id = _text(row, "station_id")
        if station_id in station_ids:
            raise ContractError("calibration_station_duplicate")
        station_ids.add(station_id)
        station_timezones[station_id] = _text(row, "timezone_name")
        if _text(row, "source_lane").upper() not in {"AMSC", "AWC"}:
            raise ContractError("calibration_station_source_lane_invalid")
        if _text(row, "source_role") != "CANONICAL_AMSC_AWC_INGRESS":
            raise ContractError("calibration_station_source_role_invalid")

    truth_by_id: dict[str, dict[str, Any]] = {}
    for row in truths:
        _verify_content_address(row, label="truth")
        truth_id = _text(row, "truth_id")
        if truth_id in truth_by_id:
            raise ContractError("calibration_truth_duplicate")
        truth_by_id[truth_id] = row
        station_id = _text(row, "station_id")
        if station_id not in station_ids:
            raise ContractError("calibration_truth_station_missing")
        target_timezone = _text(row, "target_timezone")
        if target_timezone != station_timezones[station_id]:
            raise ContractError("calibration_truth_settlement_timezone_mismatch")
        assert_event_day_closed(
            _text(row, "local_date"),
            as_of_utc=as_of_utc,
            target_timezone=target_timezone,
        )
        source_role = _text(row, "source_role")
        if source_role not in {
            "CANONICAL_AMSC_AWC_INGRESS",
            TEMPORARY_AWC_LABEL,
        }:
            raise ContractError("calibration_truth_source_role_invalid")
        source_lane = _text(row, "source_lane").upper()
        if source_lane not in {"AMSC", "AWC"}:
            raise ContractError("calibration_truth_source_lane_invalid")
        if source_role == TEMPORARY_AWC_LABEL:
            if not temporary_awc_truth or source_lane != "AWC":
                raise ContractError(
                    "temporary_awc_calibration_truth_scope_invalid"
                )

    for reference in truth_references:
        reference_identity = {
            str(key): value
            for key, value in reference.items()
            if key != "reference_sha256"
        }
        if (
            _sha(reference, "reference_sha256")
            != content_sha256(reference_identity)
        ):
            raise ContractError("calibration_truth_reference_sha_mismatch")
        truth_id = _text(reference, "truth_id")
        if truth_id in truth_by_id:
            raise ContractError("calibration_truth_reference_duplicate")
        existing = conn.execute(
            "SELECT truth_id,station_id,local_date,official_tmax_c,"
            "source_lane,settlement_definition,revision_no,qc_status,"
            "source_role,authority_scope,content_sha256 "
            "FROM calibration_station_daily_high_truth "
            "WHERE truth_id=? LIMIT 2",
            (truth_id,),
        ).fetchall()
        if len(existing) != 1:
            raise ContractError("calibration_truth_reference_missing")
        row = existing[0]
        expected = (
            truth_id,
            _text(reference, "station_id"),
            _text(reference, "local_date"),
            float(reference["official_tmax_c"]),
            _text(reference, "source_lane").upper(),
            _text(reference, "settlement_definition"),
            int(reference.get("revision_no", 0)),
            _text(reference, "qc_status").upper(),
            _text(reference, "source_role"),
            _text(reference, "authority_scope"),
            _sha(reference, "content_sha256"),
        )
        if tuple(row) != expected:
            raise ContractError("calibration_truth_reference_content_mismatch")
        station_id = expected[1]
        if station_id not in station_ids:
            raise ContractError("calibration_truth_reference_station_missing")
        assert_event_day_closed(
            expected[2],
            as_of_utc=as_of_utc,
            target_timezone=station_timezones[station_id],
        )
        if expected[7] != "ACCEPTED":
            raise ContractError("calibration_truth_reference_qc_invalid")
        if expected[8] not in {
            "CANONICAL_AMSC_AWC_INGRESS",
            TEMPORARY_AWC_LABEL,
        }:
            raise ContractError("calibration_truth_reference_role_invalid")
        if (
            expected[8] == TEMPORARY_AWC_LABEL
            and (
                not temporary_awc_truth
                or expected[4] != "AWC"
                or expected[9] != NO_LIVE_REPLAY
            )
        ):
            raise ContractError(
                "temporary_awc_calibration_truth_reference_invalid"
            )
        truth_by_id[truth_id] = {
            "truth_id": truth_id,
            "station_id": station_id,
            "local_date": expected[2],
            "official_tmax_c": expected[3],
            "source_lane": expected[4],
            "settlement_definition": expected[5],
            "revision_no": expected[6],
            "qc_status": expected[7],
            "source_role": expected[8],
            "authority_scope": expected[9],
            "content_sha256": expected[10],
            "target_timezone": station_timezones[station_id],
        }

    model_by_id: dict[str, dict[str, Any]] = {}
    scope_models: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for row in models:
        _verify_content_address(row, label="model_run")
        model_run_id = _text(row, "model_run_id")
        if model_run_id in model_by_id:
            raise ContractError("calibration_model_run_duplicate")
        model_by_id[model_run_id] = row
        station_id = _text(row, "station_id")
        target_date = _text(row, "target_local_date")
        if station_id not in station_ids:
            raise ContractError("calibration_model_station_missing")
        target_timezone = _text(row, "target_timezone")
        if target_timezone != station_timezones[station_id]:
            raise ContractError("calibration_model_settlement_timezone_mismatch")
        assert_event_day_closed(
            target_date,
            as_of_utc=as_of_utc,
            target_timezone=target_timezone,
        )
        model_id = _text(row, "model_id").upper()
        if model_id not in MODEL_IDS:
            raise ContractError("calibration_model_id_invalid")
        provider_id = _text(row, "provider_id").lower()
        if model_id_for_provider(provider_id) != model_id:
            raise ContractError("calibration_model_provider_mismatch")
        if provider_role(provider_id) != _text(row, "provider_role").lower():
            raise ContractError("calibration_provider_role_mismatch")
        expected_source_class = provider_source_class(provider_id)
        if _text(row, "source_class").lower() != expected_source_class:
            raise ContractError("calibration_provider_source_class_mismatch")
        is_aggregator = bool(row.get("aggregator_or_republisher"))
        if is_aggregator != (expected_source_class == "aggregator_republisher"):
            raise ContractError("calibration_aggregator_flag_mismatch")
        if is_aggregator and bool(row.get("prospective_strict_asof_eligible")):
            raise ContractError("calibration_aggregator_native_arrival_forbidden")
        if provider_id in HISTORICAL_REPLAY_ONLY_PROVIDERS:
            if (
                _text(row, "asof_status").upper() != "HISTORICAL_REPLAY_ONLY"
                or bool(row.get("timestamps_complete"))
                or bool(row.get("prospective_strict_asof_eligible"))
                or not bool(row.get("historical_replay_eligible"))
            ):
                raise ContractError(
                    "calibration_historical_transport_scope_invalid"
                )
        # Models publish on different clocks.  Candidate availability is
        # therefore grouped by target/maturity scope, never by a fabricated
        # common run initialization.
        scope_models[(
            station_id,
            target_date,
            _text(row, "lead_bucket"),
            _text(row, "maturity_key"),
        )].add(model_id)
    for reference in model_references:
        reference_identity = {
            str(key): value
            for key, value in reference.items()
            if key != "reference_sha256"
        }
        if (
            _sha(reference, "reference_sha256")
            != content_sha256(reference_identity)
        ):
            raise ContractError("calibration_model_reference_sha_mismatch")
        model_run_id = _text(reference, "model_run_id")
        if model_run_id in model_by_id:
            raise ContractError("calibration_model_reference_duplicate")
        cursor = conn.execute(
            "SELECT * FROM calibration_historical_asof_model_run "
            "WHERE model_run_id=? LIMIT 2",
            (model_run_id,),
        )
        existing = cursor.fetchall()
        if len(existing) != 1:
            raise ContractError("calibration_model_reference_missing")
        column_names = tuple(item[0] for item in cursor.description or ())
        db_row = dict(zip(column_names, tuple(existing[0])))
        for key in (
            "model_run_id",
            "model_id",
            "station_id",
            "target_local_date",
            "run_init_utc",
            "lead_bucket",
            "maturity_key",
            "run_cycle",
            "raw_tmax_c",
            "settlement_definition",
            "authority_scope",
            "content_sha256",
        ):
            expected = reference.get(key)
            actual = db_row.get(key)
            if key == "raw_tmax_c":
                if float(expected) != float(actual):
                    raise ContractError(
                        "calibration_model_reference_content_mismatch"
                    )
            elif str(expected) != str(actual):
                raise ContractError(
                    "calibration_model_reference_content_mismatch"
                )
        station_id = str(db_row["station_id"])
        if station_id not in station_ids:
            raise ContractError("calibration_model_reference_station_missing")
        assert_event_day_closed(
            str(db_row["target_local_date"]),
            as_of_utc=as_of_utc,
            target_timezone=station_timezones[station_id],
        )
        if (
            str(db_row["model_id"]) not in MODEL_IDS
            or str(db_row["authority_scope"])
            not in {FORMAL_NATIVE, NO_LIVE_REPLAY}
        ):
            raise ContractError("calibration_model_reference_authority_invalid")
        db_row["target_timezone"] = station_timezones[station_id]
        model_by_id[model_run_id] = db_row
        scope_models[(
            station_id,
            str(db_row["target_local_date"]),
            str(db_row["lead_bucket"]),
            str(db_row["maturity_key"]),
        )].add(str(db_row["model_id"]))

    evaluable_multi_model_scope_count = sum(
        len(models_in_scope) >= MIN_FORMAL_ENSEMBLE_MODELS
        for models_in_scope in scope_models.values()
    )
    single_model_scope_count = (
        len(scope_models) - evaluable_multi_model_scope_count
    )
    if evaluable_multi_model_scope_count == 0:
        raise ContractError("calibration_multi_model_scope_required")
    if single_model_scope_count:
        raise ContractError("calibration_single_model_scope_forbidden")

    residual_ids: set[str] = set()
    for row in residuals:
        _verify_content_address(row, label="residual")
        residual_id = _text(row, "residual_id")
        if residual_id in residual_ids:
            raise ContractError("calibration_residual_duplicate")
        residual_ids.add(residual_id)
        truth = truth_by_id.get(_text(row, "truth_id"))
        model = model_by_id.get(_text(row, "model_run_id"))
        if truth is None or model is None:
            raise ContractError("calibration_residual_lineage_missing")
        if (
            _text(row, "station_id") != _text(truth, "station_id")
            or _text(row, "station_id") != _text(model, "station_id")
            or _text(row, "model_id").upper() != _text(model, "model_id").upper()
            or _text(truth, "target_timezone") != _text(model, "target_timezone")
        ):
            raise ContractError("calibration_residual_scope_mismatch")

    status = production_ssot_schema_status(conn)
    if not status.get("ok") or not status.get("calibration_base_schema_ok"):
        raise ContractError("production_calibration_base_schema_incomplete")
    if commit:
        assert_connection_target_capacity(conn)
        assert_target_database_owner(conn)

    counts = {"stations": 0, "truths": 0, "model_runs": 0, "residuals": 0}
    savepoint = "calibration_base_ingress"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for row in stations:
            columns = (
                "station_id", "icao_code", "city_label", "timezone_name",
                "source_lane", "settlement_definition", "registry_version",
                "source_role", "content_sha256",
            )
            values = (
                _text(row, "station_id"), _text(row, "icao_code"),
                _text(row, "city_label", allow_empty=True), _text(row, "timezone_name"),
                _text(row, "source_lane").upper(), _text(row, "settlement_definition"),
                _text(row, "registry_version"), _text(row, "source_role"),
                _sha(row, "content_sha256"),
            )
            counts["stations"] += _insert_once(
                conn, table="calibration_station_registry", columns=columns, values=values,
                primary_key="station_id", primary_value=values[0],
            )
        for row in truths:
            authority = (
                NO_LIVE_REPLAY
                if _text(row, "source_role") == TEMPORARY_AWC_LABEL
                else (
                    FORMAL_NATIVE
                    if bool(row.get("timestamps_complete"))
                    and _text(row, "received_at_utc", allow_empty=True)
                    else NO_LIVE_REPLAY
                )
            )
            columns = (
                "truth_id", "station_id", "local_date", "official_tmax_c",
                "source_lane", "settlement_definition", "revision_no",
                "published_at_utc", "received_at_utc", "canonical_committed_at_utc",
                "timestamps_complete", "qc_status", "source_role",
                "source_payload_sha256", "observation_ids_json", "authority_scope",
                "content_sha256",
            )
            values = (
                _text(row, "truth_id"), _text(row, "station_id"), _text(row, "local_date"),
                float(row["official_tmax_c"]), _text(row, "source_lane").upper(),
                _text(row, "settlement_definition"), int(row.get("revision_no", 0)),
                row.get("published_at_utc"), row.get("received_at_utc"),
                row.get("canonical_committed_at_utc"), int(bool(row.get("timestamps_complete"))),
                _text(row, "qc_status").upper(), _text(row, "source_role"),
                _sha(row, "source_payload_sha256"),
                json.dumps(row.get("observation_ids") or [], sort_keys=True, separators=(",", ":")),
                authority, _sha(row, "content_sha256"),
            )
            counts["truths"] += _insert_once(
                conn, table="calibration_station_daily_high_truth", columns=columns, values=values,
                primary_key="truth_id", primary_value=values[0],
            )
        for row in models:
            authority = _model_authority(row)
            columns = (
                "model_run_id", "model_id", "station_id", "target_local_date",
                "run_init_utc", "published_at_utc", "received_at_utc",
                "timestamps_complete", "lead_bucket", "maturity_key", "run_cycle",
                "raw_tmax_c", "settlement_definition", "provider_id", "provider_role",
                "source_class", "aggregator_or_republisher", "open_meteo",
                "source_url", "raw_object_ref", "asof_status", "capture_modes_json",
                "historical_replay_eligible", "prospective_strict_asof_eligible",
                "authority_scope", "source_object_sha256", "content_sha256",
            )
            values = (
                _text(row, "model_run_id"), _text(row, "model_id").upper(),
                _text(row, "station_id"), _text(row, "target_local_date"),
                _text(row, "run_init_utc"), row.get("published_at_utc"),
                row.get("received_at_utc"), int(bool(row.get("timestamps_complete"))),
                _text(row, "lead_bucket"), _text(row, "maturity_key"),
                _text(row, "run_cycle"), float(row["raw_tmax_c"]),
                _text(row, "settlement_definition"), _text(row, "provider_id"),
                _text(row, "provider_role"), _text(row, "source_class"),
                int(bool(row.get("aggregator_or_republisher"))),
                int(bool(row.get("open_meteo"))),
                _text(row, "source_url", allow_empty=True),
                _text(row, "raw_object_ref", allow_empty=True), _text(row, "asof_status"),
                json.dumps(row.get("capture_modes") or [], sort_keys=True, separators=(",", ":")),
                int(bool(row.get("historical_replay_eligible"))),
                int(bool(row.get("prospective_strict_asof_eligible"))),
                authority, _sha(row, "source_object_sha256"),
                _sha(row, "content_sha256"),
            )
            counts["model_runs"] += _insert_once(
                conn, table="calibration_historical_asof_model_run",
                columns=columns, values=values, primary_key="model_run_id",
                primary_value=values[0],
            )
        for row in residuals:
            model = model_by_id[_text(row, "model_run_id")]
            truth = truth_by_id[_text(row, "truth_id")]
            authority = (
                NO_LIVE_REPLAY
                if _text(truth, "source_role") == TEMPORARY_AWC_LABEL
                else _model_authority(model)
            )
            columns = (
                "residual_id", "truth_id", "model_run_id", "station_id", "model_id",
                "season_bin", "lead_bucket", "maturity_key", "run_cycle",
                "e_y_minus_f_raw_c", "paired_asof_utc", "qc_status",
                "historical_replay_eligible", "prospective_strict_asof_eligible",
                "authority_scope", "pair_sha256", "content_sha256",
            )
            values = (
                _text(row, "residual_id"), _text(row, "truth_id"),
                _text(row, "model_run_id"), _text(row, "station_id"),
                _text(row, "model_id").upper(), _text(row, "season_bin"),
                _text(row, "lead_bucket"), _text(row, "maturity_key"),
                _text(row, "run_cycle"), float(row["e_y_minus_f_raw_c"]),
                _text(row, "paired_asof_utc"), _text(row, "qc_status").upper(),
                int(bool(row.get("historical_replay_eligible"))),
                int(bool(row.get("prospective_strict_asof_eligible"))),
                authority, _sha(row, "pair_sha256"), _sha(row, "content_sha256"),
            )
            counts["residuals"] += _insert_once(
                conn, table="calibration_model_station_residual",
                columns=columns, values=values, primary_key="residual_id",
                primary_value=values[0],
            )
        if commit:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    return {
        "schema": INGRESS_SCHEMA,
        "state": "COMMITTED" if commit else "VALIDATED_NO_WRITE",
        "counts": counts if commit else {key: 0 for key in counts},
        "validated_counts": {
            "stations": len(stations), "truths": len(truths),
            "truth_references": len(truth_references),
            "model_runs": len(models),
            "model_references": len(model_references),
            "residuals": len(residuals),
        },
        "formal_native_model_rows": sum(_model_authority(row) == FORMAL_NATIVE for row in models),
        "no_live_replay_model_rows": sum(_model_authority(row) == NO_LIVE_REPLAY for row in models),
        "temporary_awc_truth_rows": sum(
            _text(row, "source_role") == TEMPORARY_AWC_LABEL
            for row in truths
        ),
        "evaluable_multi_model_scope_count": evaluable_multi_model_scope_count,
        "single_model_scope_count": single_model_scope_count,
        "candidate_model_pool": list(MODEL_IDS),
        "live_order_authorized": False,
        "calls_order_gateway": False,
    }


__all__ = [
    "FORMAL_NATIVE",
    "INGRESS_SCHEMA",
    "NO_LIVE_REPLAY",
    "TEMPORARY_AWC_LABEL",
    "ingest_calibration_base_facts",
]
