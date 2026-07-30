"""Append-only authority for supplemental P0/P1 probability evidence.

This store deliberately stays outside the basket/execution ledger.  It owns
only immutable climatology, historical-meta and METAR-trajectory artifacts,
their promotion/activation receipts, and the exact update proofs consumed by
P0 or P1 materialization.
"""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime
from typing import Any

from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_probability_evidence_authority_v1"
ARTIFACT_KINDS = (
    "SAME_DAY_CLIMATOLOGY",
    "P0_HISTORICAL_META",
    "METAR_TRAJECTORY_LIKELIHOOD",
)
PROOF_KINDS = (
    "P0_HISTORICAL_TILT",
    "METAR_TRAJECTORY_UPDATE",
)
TABLES = (
    "basket_probability_evidence_artifact",
    "basket_probability_evidence_promotion",
    "basket_probability_evidence_activation",
    "basket_probability_update_proof",
)
MAX_EVIDENCE_JSON_BYTES = 1_048_576


def same_day_climatology_scope_key(
    event_date: str, settlement_definition: str
) -> str:
    parsed = datetime.strptime(_text("event_date", event_date), "%Y-%m-%d")
    return (
        f"{parsed.year:04d}|{parsed.month:02d}-{parsed.day:02d}|"
        f"{_text('settlement_definition', settlement_definition)}"
    )


def p0_historical_meta_scope_key(
    *,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    settlement_definition: str,
    settlement_timezone: str,
) -> str:
    return "|".join((
        _text("season_bin", season_bin),
        _text("lead_bucket", lead_bucket),
        _text("run_cycle", run_cycle),
        _text("settlement_definition", settlement_definition),
        _text("settlement_timezone", settlement_timezone),
    ))


def metar_trajectory_scope_key(
    *,
    source_lane: str,
    season_bin: str,
    local_time_bucket: str,
    maturity_bin: str,
    settlement_definition: str,
) -> str:
    return "|".join((
        _text("source_lane", source_lane).lower(),
        _text("season_bin", season_bin),
        _text("local_time_bucket", local_time_bucket).upper(),
        _text("maturity_bin", maturity_bin),
        _text("settlement_definition", settlement_definition),
    ))

DDL = """
CREATE TABLE IF NOT EXISTS basket_probability_evidence_artifact (
  artifact_id TEXT PRIMARY KEY,
  artifact_kind TEXT NOT NULL CHECK(artifact_kind IN (
    'SAME_DAY_CLIMATOLOGY',
    'P0_HISTORICAL_META',
    'METAR_TRAJECTORY_LIKELIHOOD'
  )),
  station_id TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  trained_through_utc TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  oos_gate_passed INTEGER NOT NULL CHECK(oos_gate_passed IN (0,1)),
  authority_scope TEXT NOT NULL CHECK(authority_scope IN (
    'PRODUCTION_NO_LIVE_CANDIDATE','FORMAL_PROMOTED'
  )),
  artifact_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  CHECK(
    authority_scope<>'FORMAL_PROMOTED' OR oos_gate_passed=1
  ),
  UNIQUE(artifact_kind,station_id,scope_key,content_sha256)
);

CREATE INDEX IF NOT EXISTS basket_probability_evidence_artifact_scope_idx
ON basket_probability_evidence_artifact(
  artifact_kind,station_id,scope_key,valid_from_utc,valid_until_utc
);

CREATE TABLE IF NOT EXISTS basket_probability_evidence_promotion (
  promotion_id TEXT PRIMARY KEY,
  artifact_kind TEXT NOT NULL CHECK(artifact_kind IN (
    'SAME_DAY_CLIMATOLOGY',
    'P0_HISTORICAL_META',
    'METAR_TRAJECTORY_LIKELIHOOD'
  )),
  artifact_id TEXT NOT NULL,
  decision_seq INTEGER NOT NULL CHECK(decision_seq>=1),
  decision TEXT NOT NULL CHECK(decision IN (
    'PROMOTED','REJECTED','SUPERSEDED'
  )),
  gate_version TEXT NOT NULL,
  metrics_sha256 TEXT NOT NULL CHECK(length(metrics_sha256)=64),
  decided_at_utc TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(artifact_kind,artifact_id,gate_version,decision_seq)
);

CREATE TABLE IF NOT EXISTS basket_probability_evidence_activation (
  activation_id TEXT PRIMARY KEY,
  artifact_kind TEXT NOT NULL CHECK(artifact_kind IN (
    'SAME_DAY_CLIMATOLOGY',
    'P0_HISTORICAL_META',
    'METAR_TRAJECTORY_LIKELIHOOD'
  )),
  artifact_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  activated_at_utc TEXT NOT NULL,
  authority_scope TEXT NOT NULL CHECK(authority_scope IN (
    'PRODUCTION_NO_LIVE_CANDIDATE','FORMAL_PROMOTED'
  )),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(artifact_kind,station_id,scope_key,generation)
);

CREATE INDEX IF NOT EXISTS basket_probability_evidence_activation_scope_idx
ON basket_probability_evidence_activation(
  artifact_kind,station_id,scope_key,valid_from_utc,valid_until_utc,generation
);

CREATE TABLE IF NOT EXISTS basket_probability_update_proof (
  proof_id TEXT PRIMARY KEY,
  proof_kind TEXT NOT NULL CHECK(proof_kind IN (
    'P0_HISTORICAL_TILT','METAR_TRAJECTORY_UPDATE'
  )),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  prediction_root_checkpoint_id TEXT NOT NULL,
  artifact_id_or_none TEXT NOT NULL,
  activation_id_or_none TEXT NOT NULL,
  canonical_sequence_sha256_or_none TEXT NOT NULL,
  decision_at_utc TEXT NOT NULL,
  proof_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(
    proof_kind,station_id,event_date,prediction_root_checkpoint_id,
    decision_at_utc,content_sha256
  )
);

CREATE INDEX IF NOT EXISTS basket_probability_update_proof_scope_idx
ON basket_probability_update_proof(
  proof_kind,station_id,event_date,decision_at_utc
);

CREATE TRIGGER IF NOT EXISTS basket_probability_evidence_artifact_no_update
BEFORE UPDATE ON basket_probability_evidence_artifact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_probability_evidence_artifact_no_delete
BEFORE DELETE ON basket_probability_evidence_artifact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_probability_evidence_promotion_no_update
BEFORE UPDATE ON basket_probability_evidence_promotion
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_probability_evidence_promotion_no_delete
BEFORE DELETE ON basket_probability_evidence_promotion
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_probability_evidence_activation_no_update
BEFORE UPDATE ON basket_probability_evidence_activation
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_probability_evidence_activation_no_delete
BEFORE DELETE ON basket_probability_evidence_activation
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_probability_update_proof_no_update
BEFORE UPDATE ON basket_probability_update_proof
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_probability_update_proof_no_delete
BEFORE DELETE ON basket_probability_update_proof
BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


def _execute_transactional(
    conn: sqlite3.Connection,
    script: str,
) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise RuntimeError("probability_evidence_ddl_incomplete")


def probability_evidence_schema_status(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = sorted(set(TABLES) - present)
    return {
        "schema": SCHEMA,
        "ok": not missing,
        "tables": list(TABLES),
        "missing_tables": missing,
        "calls_order_gateway": False,
        "places_orders": False,
    }


def apply_probability_evidence_schema(
    conn: sqlite3.Connection,
    *,
    commit: bool = False,
) -> dict[str, Any]:
    """Install only new append-only tables; never backfill or promote."""

    savepoint = f"probability_evidence_schema_{id(conn):x}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        _execute_transactional(conn, DDL)
        result = probability_evidence_schema_status(conn)
        if result["ok"] is not True:
            raise RuntimeError(
                "probability_evidence_schema_incomplete:"
                + ",".join(result["missing_tables"])
            )
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    if commit:
        conn.commit()
    return {
        **result,
        "rows_written": 0,
        "performs_legacy_backfill": False,
        "promotes_artifacts": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise ContractError(f"{name}_invalid")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _json_object(name: str, value: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(value, dict):
        raise ContractError(f"{name}_not_object")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > MAX_EVIDENCE_JSON_BYTES:
        raise ContractError(f"{name}_resource_limit_exceeded")
    return dict(value), encoded


def _artifact_scope(
    artifact_kind: str,
    artifact: Any,
) -> tuple[str, str, str, str, str, str | None]:
    """Return exact station/timezone/scope/window/training bindings."""

    if artifact_kind == "SAME_DAY_CLIMATOLOGY":
        scope_key = (
            f"{artifact.target_year:04d}|{artifact.calendar_day}|"
            f"{artifact.settlement_definition}"
        )
        trained_through = None
    elif artifact_kind == "P0_HISTORICAL_META":
        scope_key = p0_historical_meta_scope_key(
            season_bin=artifact.season_bin,
            lead_bucket=artifact.lead_bucket,
            run_cycle=artifact.run_cycle,
            settlement_definition=artifact.settlement_definition,
            settlement_timezone=artifact.settlement_timezone,
        )
        trained_through = artifact.trained_through_utc
    else:
        scope_key = metar_trajectory_scope_key(
            source_lane=artifact.source_lane,
            season_bin=artifact.season_bin,
            local_time_bucket=artifact.local_time_bucket,
            maturity_bin=artifact.maturity_bin,
            settlement_definition=artifact.settlement_definition,
        )
        trained_through = artifact.trained_through_utc
    return (
        artifact.station_id,
        artifact.settlement_timezone,
        scope_key,
        artifact.valid_from_utc,
        artifact.valid_until_utc,
        trained_through,
    )


def insert_probability_evidence_artifact(
    conn: sqlite3.Connection,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Append one validated domain artifact without promotion side effects."""

    artifact_kind = _text("artifact_kind", row.get("artifact_kind")).upper()
    if artifact_kind not in ARTIFACT_KINDS:
        raise ContractError("probability_evidence_artifact_kind_invalid")
    artifact, artifact_json = _json_object(
        "artifact_json", row.get("artifact")
    )
    if artifact_kind == "SAME_DAY_CLIMATOLOGY":
        from .same_day_climatology import SameDayClimatologyArtifact

        validated = SameDayClimatologyArtifact.from_mapping(artifact)
    elif artifact_kind == "P0_HISTORICAL_META":
        from .p0_historical_meta import (
            P0HistoricalMetaCombinationArtifact,
        )

        validated = P0HistoricalMetaCombinationArtifact.from_mapping(
            artifact
        )
    else:
        from .metar_trajectory import MetarTrajectoryLikelihoodArtifact

        validated = MetarTrajectoryLikelihoodArtifact.from_mapping(artifact)
    if validated.artifact_id != _text(
        "artifact_id", row.get("artifact_id")
    ):
        raise ContractError(
            "probability_evidence_artifact_identity_mismatch"
        )
    artifact_sha = _sha(
        "artifact_content_sha256",
        artifact.get("content_sha256"),
    )
    supplied_sha = _sha("content_sha256", row.get("content_sha256"))
    if supplied_sha != artifact_sha:
        raise ContractError(
            "probability_evidence_artifact_content_sha256_mismatch"
        )
    authority_scope = _text(
        "authority_scope", row.get("authority_scope")
    ).upper()
    oos_passed = row.get("oos_gate_passed") is True
    if authority_scope == "FORMAL_PROMOTED" and not oos_passed:
        raise ContractError(
            "probability_evidence_formal_promotion_requires_oos"
        )
    (
        expected_station,
        expected_timezone,
        expected_scope,
        expected_valid_from,
        expected_valid_until,
        expected_trained_through,
    ) = _artifact_scope(artifact_kind, validated)
    if _text("station_id", row.get("station_id")).upper() != expected_station:
        raise ContractError("probability_evidence_artifact_station_mismatch")
    if (
        _text("settlement_timezone", row.get("settlement_timezone"))
        != expected_timezone
    ):
        raise ContractError("probability_evidence_artifact_timezone_mismatch")
    if _text("scope_key", row.get("scope_key")) != expected_scope:
        raise ContractError("probability_evidence_artifact_scope_mismatch")
    if (
        _timestamp("valid_from_utc", row.get("valid_from_utc"))
        != expected_valid_from
        or _timestamp("valid_until_utc", row.get("valid_until_utc"))
        != expected_valid_until
    ):
        raise ContractError(
            "probability_evidence_artifact_valid_window_mismatch"
        )
    if (
        expected_trained_through is not None
        and _timestamp(
            "trained_through_utc", row.get("trained_through_utc")
        )
        != expected_trained_through
    ):
        raise ContractError(
            "probability_evidence_artifact_training_window_mismatch"
        )
    values = (
        _text("artifact_id", row.get("artifact_id")),
        artifact_kind,
        _text("station_id", row.get("station_id")).upper(),
        _text("settlement_timezone", row.get("settlement_timezone")),
        _text("scope_key", row.get("scope_key")),
        _timestamp("trained_through_utc", row.get("trained_through_utc")),
        _timestamp("valid_from_utc", row.get("valid_from_utc")),
        _timestamp("valid_until_utc", row.get("valid_until_utc")),
        1 if oos_passed else 0,
        authority_scope,
        artifact_json,
        supplied_sha,
    )
    existing = conn.execute(
        "SELECT content_sha256 FROM basket_probability_evidence_artifact "
        "WHERE artifact_id=?",
        (values[0],),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != supplied_sha:
            raise ContractError(
                "probability_evidence_artifact_id_content_conflict"
            )
        return {"state": "ALREADY_COMMITTED", "wrote": False}
    conn.execute(
        "INSERT INTO basket_probability_evidence_artifact VALUES"
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    return {"state": "COMMITTED", "wrote": True}


def insert_probability_evidence_promotion(
    conn: sqlite3.Connection,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Append one OOS promotion decision with deterministic identity."""

    kind = _text("artifact_kind", row.get("artifact_kind")).upper()
    if kind not in ARTIFACT_KINDS:
        raise ContractError("probability_evidence_artifact_kind_invalid")
    artifact_id = _text("artifact_id", row.get("artifact_id"))
    artifact = conn.execute(
        "SELECT oos_gate_passed FROM basket_probability_evidence_artifact "
        "WHERE artifact_id=? AND artifact_kind=?",
        (artifact_id, kind),
    ).fetchone()
    if artifact is None:
        raise ContractError("probability_evidence_promotion_artifact_missing")
    try:
        decision_seq = int(row.get("decision_seq"))
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "probability_evidence_promotion_sequence_invalid"
        ) from exc
    if decision_seq < 1:
        raise ContractError(
            "probability_evidence_promotion_sequence_invalid"
        )
    decision = _text("decision", row.get("decision")).upper()
    if decision not in {"PROMOTED", "REJECTED", "SUPERSEDED"}:
        raise ContractError("probability_evidence_promotion_decision_invalid")
    if decision == "PROMOTED" and int(artifact[0]) != 1:
        raise ContractError(
            "probability_evidence_formal_promotion_requires_oos"
        )
    identity = {
        "promotion_id": _text("promotion_id", row.get("promotion_id")),
        "artifact_kind": kind,
        "artifact_id": artifact_id,
        "decision_seq": decision_seq,
        "decision": decision,
        "gate_version": _text("gate_version", row.get("gate_version")),
        "metrics_sha256": _sha(
            "metrics_sha256", row.get("metrics_sha256")
        ),
        "decided_at_utc": _timestamp(
            "decided_at_utc", row.get("decided_at_utc")
        ),
        "receipt_sha256": _sha(
            "receipt_sha256", row.get("receipt_sha256")
        ),
    }
    supplied_sha = _sha("content_sha256", row.get("content_sha256"))
    if supplied_sha != content_sha256(identity):
        raise ContractError(
            "probability_evidence_promotion_content_sha256_mismatch"
        )
    latest = conn.execute(
        "SELECT decision_seq FROM basket_probability_evidence_promotion "
        "WHERE artifact_kind=? AND artifact_id=? "
        "ORDER BY decision_seq DESC LIMIT 1",
        (kind, artifact_id),
    ).fetchone()
    if latest is not None and decision_seq <= int(latest[0]):
        existing = conn.execute(
            "SELECT content_sha256 FROM "
            "basket_probability_evidence_promotion WHERE promotion_id=?",
            (identity["promotion_id"],),
        ).fetchone()
        if existing is not None and str(existing[0]) == supplied_sha:
            return {"state": "ALREADY_COMMITTED", "wrote": False}
        raise ContractError(
            "probability_evidence_promotion_sequence_not_monotone"
        )
    conn.execute(
        "INSERT INTO basket_probability_evidence_promotion VALUES"
        "(?,?,?,?,?,?,?,?,?,?)",
        (
            identity["promotion_id"],
            kind,
            artifact_id,
            decision_seq,
            decision,
            identity["gate_version"],
            identity["metrics_sha256"],
            identity["decided_at_utc"],
            identity["receipt_sha256"],
            supplied_sha,
        ),
    )
    return {"state": "COMMITTED", "wrote": True}


def insert_probability_update_proof(
    conn: sqlite3.Connection,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Append one exact P0/P1 update proof; equal replay is idempotent."""

    proof_kind = _text("proof_kind", row.get("proof_kind")).upper()
    if proof_kind not in PROOF_KINDS:
        raise ContractError("probability_update_proof_kind_invalid")
    proof, proof_json = _json_object("proof_json", row.get("proof"))
    if proof_kind == "P0_HISTORICAL_TILT":
        from .p0_historical_meta import P0HistoricalTiltProof

        validated_proof = P0HistoricalTiltProof.from_mapping(proof)
    else:
        from .metar_trajectory import MetarTrajectoryUpdateProof

        validated_proof = MetarTrajectoryUpdateProof.from_mapping(proof)
        exact_bindings = {
            "station_id": validated_proof.station_id,
            "event_date": validated_proof.event_date,
            "settlement_timezone": (
                validated_proof.settlement_timezone
            ),
            "prediction_root_checkpoint_id": (
                validated_proof.prediction_root_checkpoint_id
            ),
            "artifact_id_or_none": validated_proof.artifact_id,
            "activation_id_or_none": validated_proof.activation_id,
            "canonical_sequence_sha256_or_none": (
                validated_proof.canonical_observation_sequence_sha256
            ),
            "decision_at_utc": validated_proof.as_of_utc,
        }
        for field, expected in exact_bindings.items():
            actual = str(row.get(field) or "").strip()
            if field == "station_id":
                actual = actual.upper()
            if actual != str(expected):
                raise ContractError(
                    f"probability_update_proof_{field}_binding_mismatch"
                )
    if validated_proof.proof_id != _text(
        "proof_id", row.get("proof_id")
    ):
        raise ContractError(
            "probability_update_proof_identity_mismatch"
        )
    proof_sha = _sha("proof_content_sha256", proof.get("content_sha256"))
    supplied_sha = _sha("content_sha256", row.get("content_sha256"))
    if proof_sha != supplied_sha:
        raise ContractError(
            "probability_update_proof_content_sha256_mismatch"
        )
    values = (
        _text("proof_id", row.get("proof_id")),
        proof_kind,
        _text("station_id", row.get("station_id")).upper(),
        _text("event_date", row.get("event_date")),
        _text("settlement_timezone", row.get("settlement_timezone")),
        _text(
            "prediction_root_checkpoint_id",
            row.get("prediction_root_checkpoint_id"),
        ),
        _text(
            "artifact_id_or_none",
            row.get("artifact_id_or_none", "NONE"),
        ),
        _text(
            "activation_id_or_none",
            row.get("activation_id_or_none", "NONE"),
        ),
        _text(
            "canonical_sequence_sha256_or_none",
            row.get("canonical_sequence_sha256_or_none", "NONE"),
        ),
        _timestamp("decision_at_utc", row.get("decision_at_utc")),
        proof_json,
        supplied_sha,
    )
    existing = conn.execute(
        "SELECT content_sha256 FROM basket_probability_update_proof "
        "WHERE proof_id=?",
        (values[0],),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != supplied_sha:
            raise ContractError(
                "probability_update_proof_id_content_conflict"
            )
        return {"state": "ALREADY_COMMITTED", "wrote": False}
    conn.execute(
        "INSERT INTO basket_probability_update_proof VALUES"
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    return {"state": "COMMITTED", "wrote": True}


def insert_probability_evidence_activation(
    conn: sqlite3.Connection,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Append an explicit pointer; never promotes the referenced artifact."""

    kind = _text("artifact_kind", row.get("artifact_kind")).upper()
    if kind not in ARTIFACT_KINDS:
        raise ContractError("probability_evidence_artifact_kind_invalid")
    artifact_id = _text("artifact_id", row.get("artifact_id"))
    artifact = conn.execute(
        "SELECT authority_scope,oos_gate_passed,station_id,scope_key,"
        "valid_from_utc,valid_until_utc,artifact_json FROM "
        "basket_probability_evidence_artifact "
        "WHERE artifact_id=? AND artifact_kind=?",
        (artifact_id, kind),
    ).fetchone()
    if artifact is None:
        raise ContractError("probability_evidence_activation_artifact_missing")
    authority_scope = _text(
        "authority_scope", row.get("authority_scope")
    ).upper()
    if authority_scope != str(artifact[0]):
        raise ContractError(
            "probability_evidence_activation_authority_mismatch"
        )
    if authority_scope == "FORMAL_PROMOTED" and int(artifact[1]) != 1:
        raise ContractError(
            "probability_evidence_formal_promotion_requires_oos"
        )
    station_id = _text("station_id", row.get("station_id")).upper()
    scope_key = _text("scope_key", row.get("scope_key"))
    valid_from = _timestamp("valid_from_utc", row.get("valid_from_utc"))
    valid_until = _timestamp("valid_until_utc", row.get("valid_until_utc"))
    if station_id != str(artifact[2]):
        raise ContractError(
            "probability_evidence_activation_station_mismatch"
        )
    if scope_key != str(artifact[3]):
        raise ContractError(
            "probability_evidence_activation_scope_mismatch"
        )
    if valid_from != str(artifact[4]) or valid_until != str(artifact[5]):
        raise ContractError(
            "probability_evidence_activation_valid_window_mismatch"
        )
    governed = json.loads(str(artifact[6]))
    activation_id = _text("activation_id", row.get("activation_id"))
    if activation_id != _text(
        "artifact_activation_id", governed.get("activation_id")
    ):
        raise ContractError(
            "probability_evidence_activation_identity_mismatch"
        )
    if authority_scope == "FORMAL_PROMOTED":
        latest_promotion = conn.execute(
            "SELECT decision FROM basket_probability_evidence_promotion "
            "WHERE artifact_kind=? AND artifact_id=? "
            "ORDER BY decision_seq DESC LIMIT 1",
            (kind, artifact_id),
        ).fetchone()
        if (
            latest_promotion is None
            or str(latest_promotion[0]) != "PROMOTED"
        ):
            raise ContractError(
                "probability_evidence_formal_promotion_receipt_missing"
            )
    try:
        generation = int(row.get("generation"))
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "probability_evidence_activation_generation_invalid"
        ) from exc
    if generation < 1:
        raise ContractError(
            "probability_evidence_activation_generation_invalid"
        )
    identity = {
        "activation_id": activation_id,
        "artifact_kind": kind,
        "artifact_id": artifact_id,
        "station_id": station_id,
        "scope_key": scope_key,
        "generation": generation,
        "valid_from_utc": valid_from,
        "valid_until_utc": valid_until,
        "activated_at_utc": _timestamp(
            "activated_at_utc", row.get("activated_at_utc")
        ),
        "authority_scope": authority_scope,
    }
    supplied_sha = _sha("content_sha256", row.get("content_sha256"))
    if supplied_sha != content_sha256(identity):
        raise ContractError(
            "probability_evidence_activation_content_sha256_mismatch"
        )
    values = (
        activation_id,
        kind,
        artifact_id,
        station_id,
        scope_key,
        generation,
        valid_from,
        valid_until,
        identity["activated_at_utc"],
        authority_scope,
        supplied_sha,
    )
    existing = conn.execute(
        "SELECT content_sha256 FROM "
        "basket_probability_evidence_activation WHERE activation_id=?",
        (values[0],),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != values[-1]:
            raise ContractError(
                "probability_evidence_activation_id_content_conflict"
            )
        return {"state": "ALREADY_COMMITTED", "wrote": False}
    conn.execute(
        "INSERT INTO basket_probability_evidence_activation VALUES"
        "(?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    return {"state": "COMMITTED", "wrote": True}


def resolve_active_probability_evidence_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_kind: str,
    station_id: str,
    scope_key: str,
    as_of_utc: str,
    required_authority_scope: str,
) -> dict[str, Any] | None:
    """Resolve exactly one latest valid activation or fail on ambiguity."""

    kind = _text("artifact_kind", artifact_kind).upper()
    if kind not in ARTIFACT_KINDS:
        raise ContractError("probability_evidence_artifact_kind_invalid")
    as_of = _timestamp("as_of_utc", as_of_utc)
    rows = conn.execute(
        """
        SELECT a.generation,a.activation_id,a.artifact_id,
               a.authority_scope,e.artifact_json,e.content_sha256,
               e.oos_gate_passed,a.content_sha256
        FROM basket_probability_evidence_activation AS a
        JOIN basket_probability_evidence_artifact AS e
          ON e.artifact_id=a.artifact_id
         AND e.artifact_kind=a.artifact_kind
        WHERE a.artifact_kind=? AND a.station_id=? AND a.scope_key=?
          AND a.valid_from_utc<=? AND ?<a.valid_until_utc
          AND a.authority_scope=?
        ORDER BY a.generation DESC,a.activation_id DESC
        """,
        (
            kind,
            _text("station_id", station_id).upper(),
            _text("scope_key", scope_key),
            as_of,
            as_of,
            _text(
                "required_authority_scope", required_authority_scope
            ).upper(),
        ),
    ).fetchall()
    if not rows:
        return None
    highest = int(rows[0][0])
    winners = [row for row in rows if int(row[0]) == highest]
    if len(winners) != 1:
        raise ContractError(
            "probability_evidence_activation_generation_ambiguous"
        )
    row = winners[0]
    if (
        str(row[3]) == "FORMAL_PROMOTED"
        and int(row[6]) != 1
    ):
        raise ContractError(
            "probability_evidence_activation_oos_contract_invalid"
        )
    if str(row[3]) == "FORMAL_PROMOTED":
        latest_promotion = conn.execute(
            "SELECT decision FROM basket_probability_evidence_promotion "
            "WHERE artifact_kind=? AND artifact_id=? "
            "ORDER BY decision_seq DESC LIMIT 1",
            (kind, str(row[2])),
        ).fetchone()
        if (
            latest_promotion is None
            or str(latest_promotion[0]) != "PROMOTED"
        ):
            raise ContractError(
                "probability_evidence_formal_activation_not_currently_promoted"
            )
    return {
        "generation": int(row[0]),
        "activation_id": str(row[1]),
        "artifact_id": str(row[2]),
        "authority_scope": str(row[3]),
        "artifact": json.loads(str(row[4])),
        "content_sha256": str(row[5]),
        "oos_gate_passed": bool(row[6]),
        "activation_content_sha256": str(row[7]),
    }


__all__ = [
    "ARTIFACT_KINDS",
    "DDL",
    "PROOF_KINDS",
    "SCHEMA",
    "TABLES",
    "apply_probability_evidence_schema",
    "insert_probability_evidence_artifact",
    "insert_probability_evidence_activation",
    "insert_probability_evidence_promotion",
    "insert_probability_update_proof",
    "probability_evidence_schema_status",
    "p0_historical_meta_scope_key",
    "metar_trajectory_scope_key",
    "resolve_active_probability_evidence_artifact",
    "same_day_climatology_scope_key",
]
