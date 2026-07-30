#!/usr/bin/env python3
"""Append a no-live ensemble activation that restores a valid multi-model predecessor.

The repair is intentionally narrow and append-only.  It never changes an
ensemble artifact, its weights, promotion evidence, execution state, or any
order/gateway table.  Dry-run is the default; the confirmation flag is the
only write authority.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration import validate_formal_ensemble_artifact  # noqa: E402
from bayes_basket_target.contracts import (  # noqa: E402
    EPS,
    CalibrationActivation,
    ContractError,
    ModelEnsembleArtifact,
    ModelEnsembleSelectionEvidence,
    content_sha256,
    timestamp_in_window,
    timestamp_lte,
)
from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    resolve_target_database,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    assert_process_memory_budget,
    assert_target_db_capacity,
)


SCHEMA = (
    "bayes_basket_target_invalid_single_model_activation_no_live_repair_v1"
)
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
CONFIRMATION_FLAG = "--confirm-production-no-live"
MAX_ACTIVATION_ROWS = 20_000
MAX_JSON_CELL_BYTES = 2 * 1024 * 1024
PROJECTED_WRITE_BYTES = 64 * 1024

ACTIVATION_COLUMNS = (
    "activation_id",
    "artifact_kind",
    "artifact_id",
    "station_id",
    "model_id_or_star",
    "season_bin",
    "lead_bucket",
    "run_cycle",
    "settlement_definition",
    "available_model_set_hash_or_star",
    "generation",
    "valid_from_utc",
    "valid_until_utc",
    "activated_at_utc",
    "authority_scope",
    "content_sha256",
)
SCOPE_COLUMNS = (
    "station_id",
    "season_bin",
    "lead_bucket",
    "run_cycle",
    "settlement_definition",
)
ENSEMBLE_COLUMNS = (
    "ensemble_artifact_id",
    "station_id",
    "season_bin",
    "lead_bucket",
    "run_cycle",
    "settlement_definition",
    "candidate_models_json",
    "supported_masks_json",
    "residual_covariance_json",
    "correlation_penalty",
    "selection_evidence_sha256",
    "previous_ensemble_artifact_sha256",
    "weight_method",
    "effective_sample_n",
    "max_weight_delta",
    "train_start_date",
    "train_end_date",
    "recent_start_date",
    "recent_end_date",
    "holdout_start_date",
    "holdout_end_date",
    "oos_metrics_json",
    "valid_from_utc",
    "valid_until_utc",
    "content_sha256",
)
ENSEMBLE_GOVERNANCE_COLUMNS = (
    "selection_evidence_sha256",
    "previous_ensemble_artifact_sha256",
    "weight_method",
    "effective_sample_n",
    "max_weight_delta",
    "train_start_date",
    "train_end_date",
    "recent_start_date",
    "recent_end_date",
    "holdout_start_date",
    "holdout_end_date",
    "oos_metrics_json",
)
EVIDENCE_COLUMNS = (
    "selection_evidence_id",
    "station_id",
    "season_bin",
    "lead_bucket",
    "run_cycle",
    "settlement_definition",
    "method_version",
    "train_start_date",
    "train_end_date",
    "recent_start_date",
    "recent_end_date",
    "holdout_start_date",
    "holdout_end_date",
    "candidate_scores_json",
    "selected_models_json",
    "selected_weights_json",
    "oos_metrics_json",
    "residual_covariance_json",
    "residual_correlation_json",
    "effective_sample_n",
    "previous_ensemble_artifact_sha256",
    "max_weight_delta",
    "actual_max_weight_delta",
    "content_sha256",
)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _row_mapping(row: sqlite3.Row, columns: Sequence[str]) -> dict[str, Any]:
    return {column: row[column] for column in columns}


def _bounded_json(value: Any, name: str) -> Any:
    if not isinstance(value, str):
        raise ContractError(f"{name}_json_text_required")
    raw = value.encode("utf-8")
    if len(raw) > min(MAX_JSON_CELL_BYTES, MAX_JSON_INPUT_BYTES):
        raise ContractError(f"{name}_json_bytes_exceeded")
    try:
        result = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(f"{name}_json_invalid") from exc
    return result


def _quick_check(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA quick_check").fetchall()
    if len(rows) != 1 or str(rows[0][0]) != "ok":
        raise RuntimeError("target_database_quick_check_failed")


def _require_schema(conn: sqlite3.Connection) -> None:
    required = {
        "calibration_activation": set(ACTIVATION_COLUMNS),
        "calibration_ensemble_artifact": set(ENSEMBLE_COLUMNS),
        "calibration_ensemble_selection_evidence": set(EVIDENCE_COLUMNS),
        "calibration_artifact_promotion": {
            "artifact_kind",
            "artifact_id",
            "decision",
        },
    }
    for table, columns in required.items():
        actual = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not columns.issubset(actual):
            raise RuntimeError(f"target_schema_missing:{table}")


def _activation_from_row(row: sqlite3.Row) -> CalibrationActivation:
    raw = _row_mapping(row, ACTIVATION_COLUMNS)
    identity = dict(raw)
    stored = str(identity.pop("content_sha256") or "").strip().lower()
    if content_sha256(identity) != stored:
        raise ContractError("calibration_activation_content_sha256_mismatch")
    return CalibrationActivation.from_mapping(raw)


def _artifact_raw(conn: sqlite3.Connection, artifact_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT " + ",".join(ENSEMBLE_COLUMNS)
        + " FROM calibration_ensemble_artifact WHERE ensemble_artifact_id=?",
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise ContractError("ensemble_artifact_missing")
    raw = _row_mapping(row, ENSEMBLE_COLUMNS)
    governance_values = {
        column: raw.get(column) for column in ENSEMBLE_GOVERNANCE_COLUMNS
    }
    raw["_governance_columns_all_null"] = all(
        value is None for value in governance_values.values()
    )
    for source, target in (
        ("candidate_models_json", "candidate_models"),
        ("supported_masks_json", "supported_masks"),
        ("residual_covariance_json", "residual_covariance"),
        ("oos_metrics_json", "oos_metrics"),
    ):
        value = raw.pop(source)
        raw[target] = {} if value is None and target == "oos_metrics" else _bounded_json(
            value, source
        )
    raw["promoted"] = False
    return raw


def _artifact_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ensemble_artifact_id": raw["ensemble_artifact_id"],
        "station_id": raw["station_id"],
        "season_bin": raw["season_bin"],
        "lead_bucket": raw["lead_bucket"],
        "run_cycle": raw["run_cycle"],
        "settlement_definition": raw["settlement_definition"],
        "candidate_models": raw["candidate_models"],
        "supported_masks": raw["supported_masks"],
        "residual_covariance": raw["residual_covariance"],
        "correlation_penalty": raw["correlation_penalty"],
        "selection_evidence_sha256": raw["selection_evidence_sha256"],
        "previous_ensemble_artifact_sha256": raw[
            "previous_ensemble_artifact_sha256"
        ],
        "weight_method": raw["weight_method"],
        "effective_sample_n": raw["effective_sample_n"],
        "max_weight_delta": raw["max_weight_delta"],
        "train_start_date": raw["train_start_date"],
        "train_end_date": raw["train_end_date"],
        "recent_start_date": raw["recent_start_date"],
        "recent_end_date": raw["recent_end_date"],
        "holdout_start_date": raw["holdout_start_date"],
        "holdout_end_date": raw["holdout_end_date"],
        "oos_metrics": raw["oos_metrics"],
        "valid_from_utc": raw["valid_from_utc"],
        "valid_until_utc": raw["valid_until_utc"],
        "promoted": False,
    }


def _legacy_artifact_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return the pre-governance ensemble identity.

    ``_upgrade_calibration_ensemble_governance`` added nullable columns to
    existing rows without changing their immutable content hash.  Those
    columns therefore cannot be inserted into a legacy row's hash identity.
    ``promoted`` was already part of the original artifact contract and is
    reconstructed from the explicitly no-live/unpromoted repair boundary.
    """

    return {
        "ensemble_artifact_id": raw["ensemble_artifact_id"],
        "station_id": raw["station_id"],
        "season_bin": raw["season_bin"],
        "lead_bucket": raw["lead_bucket"],
        "run_cycle": raw["run_cycle"],
        "settlement_definition": raw["settlement_definition"],
        "candidate_models": raw["candidate_models"],
        "supported_masks": raw["supported_masks"],
        "residual_covariance": raw["residual_covariance"],
        "correlation_penalty": raw["correlation_penalty"],
        "valid_from_utc": raw["valid_from_utc"],
        "valid_until_utc": raw["valid_until_utc"],
        "promoted": False,
    }


def _validate_artifact_hash(raw: Mapping[str, Any]) -> str:
    stored = str(raw.get("content_sha256") or "").strip().lower()
    # Governed artifacts have one authoritative current contract and are
    # always verified through the production ModelEnsembleArtifact loader.
    if not bool(raw.get("_governance_columns_all_null")):
        try:
            artifact = ModelEnsembleArtifact.from_mapping(raw)
            validate_formal_ensemble_artifact(artifact)
        except ContractError as exc:
            raise ContractError(
                "governed_ensemble_artifact_not_production_loadable_"
                "not_repairable"
            ) from exc
        return "governed_v4"

    # A NULL-governance row may be either a genuine pre-migration row or an
    # ungoverned row emitted after the additive migration.  Accept only an
    # exact hash under one of those two known immutable contracts.
    if content_sha256(_legacy_artifact_identity(raw)) == stored:
        return "legacy_pre_governance_v1"
    if content_sha256(_artifact_identity(raw)) == stored:
        return "current_ungoverned_v4"
    raise ContractError(
        "legacy_ensemble_artifact_hash_unverifiable_not_repairable"
    )


def _is_single_model_illegal(
    raw: Mapping[str, Any],
    *,
    available_hash: str,
) -> bool:
    candidates = {
        str(model).strip().upper()
        for model in (raw.get("candidate_models") or [])
    }
    masks = raw.get("supported_masks")
    if not isinstance(masks, Mapping):
        return False
    weights = masks.get(available_hash)
    positive_members = (
        {
            str(model).strip().upper()
            for model, weight in weights.items()
            if isinstance(weight, (int, float)) and float(weight) > EPS
        }
        if isinstance(weights, Mapping)
        else set()
    )
    return len(candidates) == 1 or len(positive_members) == 1


def _has_formal_promotion(
    conn: sqlite3.Connection,
    artifact_id: str,
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM calibration_artifact_promotion "
            "WHERE artifact_kind='MODEL_ENSEMBLE' AND artifact_id=? "
            "AND decision='PROMOTED' LIMIT 1",
            (artifact_id,),
        ).fetchone()
        is not None
    )


def _selection_evidence(
    conn: sqlite3.Connection,
    evidence_sha256: str,
) -> ModelEnsembleSelectionEvidence:
    row = conn.execute(
        "SELECT " + ",".join(EVIDENCE_COLUMNS)
        + " FROM calibration_ensemble_selection_evidence "
        "WHERE content_sha256=?",
        (evidence_sha256,),
    ).fetchone()
    if row is None:
        raise ContractError("ensemble_selection_evidence_missing")
    raw = _row_mapping(row, EVIDENCE_COLUMNS)
    for source, target in (
        ("candidate_scores_json", "candidate_scores"),
        ("selected_models_json", "selected_models"),
        ("selected_weights_json", "selected_weights"),
        ("oos_metrics_json", "oos_metrics"),
        ("residual_covariance_json", "residual_covariance"),
        ("residual_correlation_json", "residual_correlation"),
    ):
        raw[target] = _bounded_json(raw.pop(source), source)
    return ModelEnsembleSelectionEvidence.from_mapping(raw)


def _validated_predecessor(
    conn: sqlite3.Connection,
    activation: CalibrationActivation,
    *,
    as_of_utc: str,
) -> ModelEnsembleArtifact:
    if activation.authority_scope != AUTHORITY_SCOPE:
        raise ContractError("repair_predecessor_not_no_live")
    if not timestamp_lte(activation.activated_at_utc, as_of_utc) or not (
        timestamp_in_window(
            as_of_utc,
            activation.valid_from_utc,
            activation.valid_until_utc,
        )
    ):
        raise ContractError("repair_predecessor_activation_not_active")
    if _has_formal_promotion(conn, activation.artifact_id):
        raise ContractError("repair_formal_promotion_forbidden")
    raw = _artifact_raw(conn, activation.artifact_id)
    artifact = ModelEnsembleArtifact.from_mapping(raw)
    validate_formal_ensemble_artifact(artifact)
    if not timestamp_in_window(
        as_of_utc,
        artifact.valid_from_utc,
        artifact.valid_until_utc,
    ):
        raise ContractError("repair_predecessor_artifact_not_active")
    if (
        artifact.station_id,
        artifact.season_bin,
        artifact.lead_bucket,
        artifact.run_cycle,
        artifact.settlement_definition,
    ) != (
        activation.station_id,
        activation.season_bin,
        activation.lead_bucket,
        activation.run_cycle,
        activation.settlement_definition,
    ):
        raise ContractError("repair_predecessor_artifact_scope_mismatch")
    weights = artifact.supported_masks.get(
        activation.available_model_set_hash_or_star
    )
    if not isinstance(weights, Mapping) or sum(
        1 for value in weights.values() if float(value) > EPS
    ) < 2:
        raise ContractError("repair_predecessor_activation_mask_invalid")
    evidence = _selection_evidence(
        conn, str(artifact.selection_evidence_sha256 or "")
    )
    if (
        evidence.station_id,
        evidence.season_bin,
        evidence.lead_bucket,
        evidence.run_cycle,
        evidence.settlement_definition,
        evidence.selected_models,
    ) != (
        artifact.station_id,
        artifact.season_bin,
        artifact.lead_bucket,
        artifact.run_cycle,
        artifact.settlement_definition,
        artifact.candidate_models,
    ):
        raise ContractError("ensemble_selection_evidence_binding_invalid")
    full_mask = next(
        (
            mask_weights
            for mask_weights in artifact.supported_masks.values()
            if set(mask_weights) == set(artifact.candidate_models)
        ),
        None,
    )
    if full_mask is None or any(
        abs(float(full_mask[model]) - float(evidence.selected_weights[model]))
        > EPS
        for model in artifact.candidate_models
    ):
        raise ContractError("ensemble_selection_weights_binding_invalid")
    return artifact


def _scope_key(activation: CalibrationActivation) -> tuple[str, ...]:
    return tuple(str(getattr(activation, key)) for key in SCOPE_COLUMNS)


def _build_repairs(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    station_ids: frozenset[str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    where = "WHERE artifact_kind='MODEL_ENSEMBLE'"
    parameters: tuple[Any, ...] = ()
    if station_ids:
        placeholders = ",".join("?" for _ in station_ids)
        where += f" AND station_id IN ({placeholders})"
        parameters = tuple(sorted(station_ids))
    count = int(
        conn.execute(
            f"SELECT COUNT(*) FROM calibration_activation {where}",
            parameters,
        ).fetchone()[0]
    )
    if count > MAX_ACTIVATION_ROWS:
        raise RuntimeError("repair_activation_row_budget_exceeded")
    rows = conn.execute(
        "SELECT " + ",".join(ACTIVATION_COLUMNS)
        + f" FROM calibration_activation {where} "
        "ORDER BY station_id,season_bin,lead_bucket,run_cycle,"
        "settlement_definition,generation DESC,activation_id",
        parameters,
    ).fetchall()
    assert_process_memory_budget(projected_bytes=count * 2048)
    groups: dict[tuple[str, ...], list[CalibrationActivation]] = {}
    for row in rows:
        activation = _activation_from_row(row)
        groups.setdefault(_scope_key(activation), []).append(activation)

    repairs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    # SQLite's historical UNIQUE key omits settlement_definition.  Allocate
    # repair generations above that wider key and advance it per repair so
    # two settlement scopes can never collide during one transaction.
    generation_ceilings: dict[tuple[str, str, str, str], int] = {}
    for activations in groups.values():
        for activation in activations:
            key = (
                activation.station_id,
                activation.season_bin,
                activation.lead_bucket,
                activation.run_cycle,
            )
            generation_ceilings[key] = max(
                generation_ceilings.get(key, 0),
                activation.generation,
            )
    for scope, activations in sorted(groups.items()):
        max_generation = max(item.generation for item in activations)
        latest = [
            item for item in activations if item.generation == max_generation
        ]
        if len(latest) != 1:
            skipped.append(
                {"scope": list(scope), "reason": "latest_activation_ambiguous"}
            )
            continue
        current = latest[0]
        if current.authority_scope != AUTHORITY_SCOPE:
            skipped.append(
                {"scope": list(scope), "reason": "latest_not_no_live"}
            )
            continue
        if _has_formal_promotion(conn, current.artifact_id):
            skipped.append(
                {"scope": list(scope), "reason": "formal_promotion_forbidden"}
            )
            continue
        if not timestamp_lte(current.activated_at_utc, as_of_utc) or not (
            timestamp_in_window(
                as_of_utc,
                current.valid_from_utc,
                current.valid_until_utc,
            )
        ):
            skipped.append(
                {"scope": list(scope), "reason": "latest_not_active"}
            )
            continue
        current_raw = _artifact_raw(conn, current.artifact_id)
        current_hash_contract = _validate_artifact_hash(current_raw)
        if not _is_single_model_illegal(
            current_raw,
            available_hash=current.available_model_set_hash_or_star,
        ):
            continue

        predecessors: list[
            tuple[int, int, str, CalibrationActivation, ModelEnsembleArtifact]
        ] = []
        for candidate in activations:
            if (
                candidate.generation >= current.generation
                or candidate.authority_scope != AUTHORITY_SCOPE
            ):
                continue
            try:
                artifact = _validated_predecessor(
                    conn, candidate, as_of_utc=as_of_utc
                )
            except (ContractError, ValueError):
                continue
            weights = artifact.supported_masks[
                candidate.available_model_set_hash_or_star
            ]
            predecessors.append(
                (
                    candidate.generation,
                    len(weights),
                    candidate.activation_id,
                    candidate,
                    artifact,
                )
            )
        if not predecessors:
            skipped.append(
                {"scope": list(scope), "reason": "no_valid_predecessor"}
            )
            continue
        _, _, _, predecessor, artifact = max(predecessors)
        generation_key = (
            predecessor.station_id,
            predecessor.season_bin,
            predecessor.lead_bucket,
            predecessor.run_cycle,
        )
        next_generation = generation_ceilings[generation_key] + 1
        generation_ceilings[generation_key] = next_generation
        identity = {
            "artifact_kind": "MODEL_ENSEMBLE",
            "artifact_id": artifact.ensemble_artifact_id,
            "station_id": predecessor.station_id,
            "model_id_or_star": "*",
            "season_bin": predecessor.season_bin,
            "lead_bucket": predecessor.lead_bucket,
            "run_cycle": predecessor.run_cycle,
            "settlement_definition": predecessor.settlement_definition,
            "available_model_set_hash_or_star": (
                predecessor.available_model_set_hash_or_star
            ),
            "generation": next_generation,
            "valid_from_utc": predecessor.valid_from_utc,
            "valid_until_utc": predecessor.valid_until_utc,
            "activated_at_utc": as_of_utc,
            "authority_scope": AUTHORITY_SCOPE,
        }
        activation_id = "repair-no-live-ens-" + content_sha256(identity)[:32]
        repair = {
            "activation_id": activation_id,
            **identity,
            "content_sha256": content_sha256(
                {"activation_id": activation_id, **identity}
            ),
            "replaces_activation_id": current.activation_id,
            "restores_activation_id": predecessor.activation_id,
            "artifact_content_sha256": artifact.content_sha256,
            "replaced_artifact_hash_contract": current_hash_contract,
        }
        CalibrationActivation.from_mapping(
            {key: repair[key] for key in ACTIVATION_COLUMNS}
        )
        repairs.append(repair)
    return repairs, skipped


def repair_invalid_single_model_activations(
    *,
    db_path: Path,
    confirm_production_no_live: bool = False,
    station_ids: Sequence[str] | None = None,
    as_of_utc: str | None = None,
    _fail_after_insert: int | None = None,
) -> dict[str, Any]:
    requested = Path(db_path).expanduser()
    if requested.is_symlink():
        raise RuntimeError("target_database_symlink_forbidden")
    database = resolve_target_database(requested)
    if not database.is_file():
        raise RuntimeError("target_database_path_invalid")
    timestamp = as_of_utc or _utc_now()
    normalized_stations = (
        frozenset(
            station.strip().upper()
            for station in station_ids
            if station and station.strip()
        )
        if station_ids
        else None
    )
    assert_process_memory_budget()
    assert_target_db_capacity(
        database,
        projected_bytes=(
            PROJECTED_WRITE_BYTES if confirm_production_no_live else 0
        ),
    )
    uri = f"file:{database}?mode={'rw' if confirm_production_no_live else 'ro'}"
    with sqlite3.connect(
        uri,
        uri=True,
        timeout=60.0,
        isolation_level=None,
    ) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        if not confirm_production_no_live:
            conn.execute("PRAGMA query_only=ON")
        _require_schema(conn)
        _quick_check(conn)
        repairs, skipped = _build_repairs(
            conn,
            as_of_utc=timestamp,
            station_ids=normalized_stations,
        )
        rows_written = 0
        if confirm_production_no_live and repairs:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-read under the writer lock; no dry-run result is trusted
                # across a concurrent append.
                repairs, skipped = _build_repairs(
                    conn,
                    as_of_utc=timestamp,
                    station_ids=normalized_stations,
                )
                for index, repair in enumerate(repairs, start=1):
                    values = tuple(repair[key] for key in ACTIVATION_COLUMNS)
                    conn.execute(
                        "INSERT INTO calibration_activation("
                        + ",".join(ACTIVATION_COLUMNS)
                        + ") VALUES("
                        + ",".join("?" for _ in ACTIVATION_COLUMNS)
                        + ")",
                        values,
                    )
                    rows_written += 1
                    if (
                        _fail_after_insert is not None
                        and index >= _fail_after_insert
                    ):
                        raise RuntimeError("repair_injected_failure")
                _quick_check(conn)
                assert_process_memory_budget()
                assert_target_db_capacity(database)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    result = {
        "schema": SCHEMA,
        "state": (
            "DRY_RUN"
            if not confirm_production_no_live
            else ("APPLIED" if rows_written else "UNCHANGED_ZERO_WRITE")
        ),
        "database": str(database),
        "as_of_utc": timestamp,
        "station_ids": (
            sorted(normalized_stations) if normalized_stations else ["*"]
        ),
        "candidate_count": len(repairs),
        "rows_written": rows_written,
        "repairs": repairs,
        "skipped": skipped,
        "authority_scope": AUTHORITY_SCOPE,
        "formal_promoted": False,
        "artifacts_modified": 0,
        "artifact_weights_modified": 0,
        "orders_submitted": 0,
        "calls_order_gateway": False,
        "writes_order_state": False,
        "live_submit_authorized": False,
        "services_restarted": False,
    }
    return {**result, "receipt_sha256": content_sha256(result)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--station-id",
        action="append",
        default=[],
        help="optional repeatable station filter; omitted means all stations",
    )
    parser.add_argument("--as-of-utc")
    parser.add_argument(
        CONFIRMATION_FLAG,
        action="store_true",
        help="append repairs under PRODUCTION_NO_LIVE_ONLY",
    )
    args = parser.parse_args()
    result = repair_invalid_single_model_activations(
        db_path=args.db,
        confirm_production_no_live=bool(args.confirm_production_no_live),
        station_ids=args.station_id,
        as_of_utc=args.as_of_utc,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
