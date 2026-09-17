from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from bayes_basket_target.contracts import (
    FORMAL_ENSEMBLE_METHOD_VERSION,
    content_sha256,
    model_set_hash,
)
from bayes_basket_target.ssot import apply_target_ssot_schema
from scripts.repair_bayes_basket_target_invalid_single_model_activation_no_live import (
    ACTIVATION_COLUMNS,
    repair_invalid_single_model_activations,
)


AS_OF = "2026-07-15T12:00:00Z"


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "target.db"
    with sqlite3.connect(path) as conn:
        apply_target_ssot_schema(conn)
    return path


def _selection_evidence(
    station: str,
    models: tuple[str, ...],
    weights: dict[str, float],
) -> dict[str, object]:
    identity: dict[str, object] = {
        "selection_evidence_id": f"selection-{station.lower()}",
        "station_id": station,
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "method_version": FORMAL_ENSEMBLE_METHOD_VERSION,
        "train_start_date": "2026-01-01",
        "train_end_date": "2026-05-31",
        "recent_start_date": "2026-05-01",
        "recent_end_date": "2026-05-31",
        "holdout_start_date": "2026-06-01",
        "holdout_end_date": "2026-06-30",
        "candidate_scores": [
            {"models": list(models), "objective": 0.25}
        ],
        "selected_models": list(models),
        "selected_weights": weights,
        "oos_metrics": {
            "brier_score": 0.12,
            "nll": 0.34,
            "crps": 0.56,
        },
        "residual_covariance": {
            model: {
                other: (1.0 if model == other else 0.2)
                for other in models
            }
            for model in models
        },
        "residual_correlation": {
            model: {
                other: (1.0 if model == other else 0.2)
                for other in models
            }
            for model in models
        },
        "effective_sample_n": 80.0,
        "previous_ensemble_artifact_sha256": None,
        "max_weight_delta": 0.2,
        "actual_max_weight_delta": 0.1,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _artifact(
    station: str,
    artifact_id: str,
    models: tuple[str, ...],
    weights: dict[str, float],
    *,
    evidence_sha256: str | None,
    legacy_hash_contract: bool = False,
) -> dict[str, object]:
    mask = model_set_hash(models)
    identity: dict[str, object] = {
        "ensemble_artifact_id": artifact_id,
        "station_id": station,
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "candidate_models": list(models),
        "supported_masks": {mask: weights},
        "residual_covariance": {
            model: {
                other: (1.0 if model == other else 0.2)
                for other in models
            }
            for model in models
        },
        "correlation_penalty": 0.05,
        "selection_evidence_sha256": evidence_sha256,
        "previous_ensemble_artifact_sha256": None,
        "weight_method": (
            FORMAL_ENSEMBLE_METHOD_VERSION
            if evidence_sha256 is not None
            else None
        ),
        "effective_sample_n": 80.0 if evidence_sha256 is not None else None,
        "max_weight_delta": 0.2 if evidence_sha256 is not None else None,
        "train_start_date": (
            "2026-01-01" if evidence_sha256 is not None else None
        ),
        "train_end_date": (
            "2026-05-31" if evidence_sha256 is not None else None
        ),
        "recent_start_date": (
            "2026-05-01" if evidence_sha256 is not None else None
        ),
        "recent_end_date": (
            "2026-05-31" if evidence_sha256 is not None else None
        ),
        "holdout_start_date": (
            "2026-06-01" if evidence_sha256 is not None else None
        ),
        "holdout_end_date": (
            "2026-06-30" if evidence_sha256 is not None else None
        ),
        "oos_metrics": (
            {"brier_score": 0.12, "nll": 0.34, "crps": 0.56}
            if evidence_sha256 is not None
            else {}
        ),
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "promoted": False,
    }
    hash_identity = (
        {
            key: identity[key]
            for key in (
                "ensemble_artifact_id",
                "station_id",
                "season_bin",
                "lead_bucket",
                "run_cycle",
                "settlement_definition",
                "candidate_models",
                "supported_masks",
                "residual_covariance",
                "correlation_penalty",
                "valid_from_utc",
                "valid_until_utc",
                "promoted",
            )
        }
        if legacy_hash_contract
        else identity
    )
    stored = {**identity, "content_sha256": content_sha256(hash_identity)}
    if legacy_hash_contract:
        # Simulate _upgrade_calibration_ensemble_governance: every column
        # added after this row was created remains SQL NULL.
        stored["oos_metrics"] = None
    return stored


def _activation(
    station: str,
    artifact: dict[str, object],
    generation: int,
) -> dict[str, object]:
    models = tuple(artifact["candidate_models"])
    identity: dict[str, object] = {
        "artifact_kind": "MODEL_ENSEMBLE",
        "artifact_id": artifact["ensemble_artifact_id"],
        "station_id": station,
        "model_id_or_star": "*",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "available_model_set_hash_or_star": model_set_hash(models),
        "generation": generation,
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "activated_at_utc": f"2026-07-{generation:02d}T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    activation_id = f"activation-{station.lower()}-{generation}"
    return {
        "activation_id": activation_id,
        **identity,
        "content_sha256": content_sha256(
            {"activation_id": activation_id, **identity}
        ),
    }


def _insert_evidence(conn: sqlite3.Connection, row: dict[str, object]) -> None:
    conn.execute(
        """
        INSERT INTO calibration_ensemble_selection_evidence(
          selection_evidence_id,station_id,season_bin,lead_bucket,run_cycle,
          settlement_definition,method_version,train_start_date,train_end_date,
          recent_start_date,recent_end_date,holdout_start_date,holdout_end_date,
          candidate_scores_json,selected_models_json,selected_weights_json,
          oos_metrics_json,residual_covariance_json,residual_correlation_json,
          effective_sample_n,previous_ensemble_artifact_sha256,max_weight_delta,
          actual_max_weight_delta,content_sha256
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["selection_evidence_id"],
            row["station_id"],
            row["season_bin"],
            row["lead_bucket"],
            row["run_cycle"],
            row["settlement_definition"],
            row["method_version"],
            row["train_start_date"],
            row["train_end_date"],
            row["recent_start_date"],
            row["recent_end_date"],
            row["holdout_start_date"],
            row["holdout_end_date"],
            json.dumps(row["candidate_scores"], sort_keys=True),
            json.dumps(row["selected_models"], sort_keys=True),
            json.dumps(row["selected_weights"], sort_keys=True),
            json.dumps(row["oos_metrics"], sort_keys=True),
            json.dumps(row["residual_covariance"], sort_keys=True),
            json.dumps(row["residual_correlation"], sort_keys=True),
            row["effective_sample_n"],
            row["previous_ensemble_artifact_sha256"],
            row["max_weight_delta"],
            row["actual_max_weight_delta"],
            row["content_sha256"],
        ),
    )


def _insert_artifact(conn: sqlite3.Connection, row: dict[str, object]) -> None:
    conn.execute(
        """
        INSERT INTO calibration_ensemble_artifact(
          ensemble_artifact_id,station_id,season_bin,lead_bucket,run_cycle,
          settlement_definition,candidate_models_json,supported_masks_json,
          residual_covariance_json,correlation_penalty,
          selection_evidence_sha256,previous_ensemble_artifact_sha256,
          weight_method,effective_sample_n,max_weight_delta,train_start_date,
          train_end_date,recent_start_date,recent_end_date,holdout_start_date,
          holdout_end_date,oos_metrics_json,valid_from_utc,valid_until_utc,
          content_sha256
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["ensemble_artifact_id"],
            row["station_id"],
            row["season_bin"],
            row["lead_bucket"],
            row["run_cycle"],
            row["settlement_definition"],
            json.dumps(row["candidate_models"], sort_keys=True),
            json.dumps(row["supported_masks"], sort_keys=True),
            json.dumps(row["residual_covariance"], sort_keys=True),
            row["correlation_penalty"],
            row["selection_evidence_sha256"],
            row["previous_ensemble_artifact_sha256"],
            row["weight_method"],
            row["effective_sample_n"],
            row["max_weight_delta"],
            row["train_start_date"],
            row["train_end_date"],
            row["recent_start_date"],
            row["recent_end_date"],
            row["holdout_start_date"],
            row["holdout_end_date"],
            (
                None
                if row["oos_metrics"] is None
                else json.dumps(row["oos_metrics"], sort_keys=True)
            ),
            row["valid_from_utc"],
            row["valid_until_utc"],
            row["content_sha256"],
        ),
    )


def _insert_activation(
    conn: sqlite3.Connection,
    row: dict[str, object],
) -> None:
    conn.execute(
        "INSERT INTO calibration_activation("
        + ",".join(ACTIVATION_COLUMNS)
        + ") VALUES("
        + ",".join("?" for _ in ACTIVATION_COLUMNS)
        + ")",
        tuple(row[column] for column in ACTIVATION_COLUMNS),
    )


def _seed_station(
    conn: sqlite3.Connection,
    station: str,
    *,
    include_predecessor: bool = True,
) -> tuple[dict[str, object] | None, dict[str, object]]:
    valid = None
    if include_predecessor:
        evidence = _selection_evidence(
            station, ("ECMWF", "GFS"), {"ECMWF": 0.65, "GFS": 0.35}
        )
        valid = _artifact(
            station,
            f"valid-{station.lower()}",
            ("ECMWF", "GFS"),
            {"ECMWF": 0.65, "GFS": 0.35},
            evidence_sha256=str(evidence["content_sha256"]),
        )
        _insert_evidence(conn, evidence)
        _insert_artifact(conn, valid)
        _insert_activation(conn, _activation(station, valid, 1))
    invalid = _artifact(
        station,
        f"invalid-{station.lower()}",
        ("ECMWF",),
        {"ECMWF": 1.0},
        evidence_sha256=None,
        legacy_hash_contract=True,
    )
    _insert_artifact(conn, invalid)
    _insert_activation(conn, _activation(station, invalid, 2))
    return valid, invalid


def _activation_count(path: Path, station: str) -> int:
    with sqlite3.connect(path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM calibration_activation WHERE station_id=?",
                (station,),
            ).fetchone()[0]
        )


def test_no_legal_predecessor_is_zero_write(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as conn:
        _seed_station(conn, "KAUS", include_predecessor=False)
    result = repair_invalid_single_model_activations(
        db_path=database,
        confirm_production_no_live=True,
        as_of_utc=AS_OF,
    )
    assert result["rows_written"] == 0
    assert result["skipped"][0]["reason"] == "no_valid_predecessor"
    assert _activation_count(database, "KAUS") == 1


def test_formally_promoted_predecessor_is_forbidden(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as conn:
        valid, _ = _seed_station(conn, "KAUS")
        assert valid is not None
        conn.execute(
            """
            INSERT INTO calibration_artifact_promotion(
              promotion_decision_id,artifact_kind,artifact_id,decision_seq,
              decision,gate_version,decided_at_utc,receipt_sha256,
              content_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "promotion-kaus",
                "MODEL_ENSEMBLE",
                valid["ensemble_artifact_id"],
                1,
                "PROMOTED",
                "gate-v1",
                AS_OF,
                "a" * 64,
                "b" * 64,
            ),
        )
    result = repair_invalid_single_model_activations(
        db_path=database,
        confirm_production_no_live=True,
        as_of_utc=AS_OF,
    )
    assert result["rows_written"] == 0
    assert result["skipped"][0]["reason"] == "no_valid_predecessor"
    assert _activation_count(database, "KAUS") == 2


def test_legacy_multimodel_predecessor_without_strict_oos_is_not_repairable(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as conn:
        legacy_multi = _artifact(
            "KAUS",
            "legacy-multi-kaus",
            ("ECMWF", "GFS"),
            {"ECMWF": 0.65, "GFS": 0.35},
            evidence_sha256=None,
            legacy_hash_contract=True,
        )
        _insert_artifact(conn, legacy_multi)
        _insert_activation(conn, _activation("KAUS", legacy_multi, 1))
        invalid = _artifact(
            "KAUS",
            "invalid-kaus",
            ("ECMWF",),
            {"ECMWF": 1.0},
            evidence_sha256=None,
            legacy_hash_contract=True,
        )
        _insert_artifact(conn, invalid)
        _insert_activation(conn, _activation("KAUS", invalid, 2))

    result = repair_invalid_single_model_activations(
        db_path=database,
        confirm_production_no_live=True,
        as_of_utc=AS_OF,
    )
    assert result["rows_written"] == 0
    assert result["skipped"][0]["reason"] == "no_valid_predecessor"
    assert _activation_count(database, "KAUS") == 2


def test_injected_failure_rolls_back_every_append(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as conn:
        _seed_station(conn, "KAUS")
        _seed_station(conn, "KBOS")
    with pytest.raises(RuntimeError, match="repair_injected_failure"):
        repair_invalid_single_model_activations(
            db_path=database,
            confirm_production_no_live=True,
            as_of_utc=AS_OF,
            _fail_after_insert=1,
        )
    assert _activation_count(database, "KAUS") == 2
    assert _activation_count(database, "KBOS") == 2


def test_legacy_contract_hash_still_rejects_weight_tampering(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as conn:
        evidence = _selection_evidence(
            "KAUS", ("ECMWF", "GFS"), {"ECMWF": 0.65, "GFS": 0.35}
        )
        valid = _artifact(
            "KAUS",
            "valid-kaus",
            ("ECMWF", "GFS"),
            {"ECMWF": 0.65, "GFS": 0.35},
            evidence_sha256=str(evidence["content_sha256"]),
        )
        _insert_evidence(conn, evidence)
        _insert_artifact(conn, valid)
        _insert_activation(conn, _activation("KAUS", valid, 1))

        invalid = _artifact(
            "KAUS",
            "invalid-kaus",
            ("ECMWF",),
            {"ECMWF": 1.0},
            evidence_sha256=None,
            legacy_hash_contract=True,
        )
        # Preserve the original hash while changing the persisted payload.
        invalid["supported_masks"] = {
            model_set_hash(("ECMWF",)): {"ECMWF": 0.75}
        }
        _insert_artifact(conn, invalid)
        _insert_activation(conn, _activation("KAUS", invalid, 2))

    with pytest.raises(
        ValueError,
        match="legacy_ensemble_artifact_hash_unverifiable_not_repairable",
    ):
        repair_invalid_single_model_activations(
            db_path=database,
            confirm_production_no_live=True,
            as_of_utc=AS_OF,
        )
    assert _activation_count(database, "KAUS") == 2
