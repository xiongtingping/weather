"""Append-only SSOT registry for the one active strong-LR runtime artifact.

The registry separates runtime activation from offline promotion evidence.
Strict-as-of/OOS may approve a new candidate, but every P0/P8 process loads
the exact active artifact identity from this isolated registry. Callers may
not smuggle an arbitrary scorer through a prepared-cycle payload.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from sequential_bayes_public_ssot_shadow_writer import SHADOW_MANIFEST_STATE, SHADOW_MANIFEST_TABLE
from strong_observation_lr_artifact_contract import validate_strong_observation_lr_runtime_artifact


SCHEMA_VERSION = "strong_observation_lr_runtime_registry_v1"
ACTIVATION_ATTESTATION_SCHEMA_VERSION = "strong_lr_runtime_activation_attestation_v1"
PROMOTION_GATE_SCHEMA_VERSION = "strong_observation_artifact_promotion_gate_v1"
ARTIFACT_TABLE = "strong_lr_runtime_artifacts_shadow"
ACTIVATION_TABLE = "strong_lr_runtime_artifact_activations_shadow"
ACTIVATION_BASES = frozenset({"approved_runtime_baseline", "promoted_candidate"})
ARTIFACT_REQUIRED_COLUMNS = frozenset({
    "artifact_sha256",
    "artifact_schema",
    "artifact_json",
    "artifact_json_sha256",
    "registered_at",
    "registered_by",
    "registration_basis",
    "registration_ref",
})
ACTIVATION_REQUIRED_COLUMNS = frozenset({
    "activation_id",
    "target_identity",
    "artifact_sha256",
    "activated_at",
    "activated_by",
    "activation_basis",
    "activation_ref",
    "activation_evidence_json",
    "activation_evidence_sha256",
    "previous_activation_id",
    "activation_sha256",
})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_text(value: Any) -> str:
    parsed = _time(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed is not None else ""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_isolated(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ValueError("strong_lr_registry_shadow_manifest_missing")
    row = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ValueError("strong_lr_registry_shadow_manifest_invalid")


def initialize_strong_lr_runtime_registry_schema(conn: sqlite3.Connection) -> None:
    """Create append-only artifact and activation-event tables."""

    _assert_isolated(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ARTIFACT_TABLE} (
            artifact_sha256 TEXT PRIMARY KEY,
            artifact_schema TEXT NOT NULL,
            artifact_json TEXT NOT NULL,
            artifact_json_sha256 TEXT NOT NULL UNIQUE,
            registered_at TEXT NOT NULL,
            registered_by TEXT NOT NULL,
            registration_basis TEXT NOT NULL,
            registration_ref TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ACTIVATION_TABLE} (
            activation_id TEXT PRIMARY KEY,
            target_identity TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            activated_at TEXT NOT NULL,
            activated_by TEXT NOT NULL,
            activation_basis TEXT NOT NULL,
            activation_ref TEXT NOT NULL,
            activation_evidence_json TEXT NOT NULL,
            activation_evidence_sha256 TEXT NOT NULL,
            previous_activation_id TEXT NOT NULL,
            activation_sha256 TEXT NOT NULL UNIQUE,
            FOREIGN KEY(artifact_sha256) REFERENCES {ARTIFACT_TABLE}(artifact_sha256)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{ACTIVATION_TABLE}_target_time "
        f"ON {ACTIVATION_TABLE}(target_identity,activated_at,activation_id)"
    )
    activation_columns = {
        _text(row[1]) for row in conn.execute(f"PRAGMA table_info({ACTIVATION_TABLE})").fetchall()
    }
    if "activation_evidence_json" not in activation_columns:
        conn.execute(
            f"ALTER TABLE {ACTIVATION_TABLE} ADD COLUMN activation_evidence_json TEXT NOT NULL DEFAULT ''"
        )
    if "activation_evidence_sha256" not in activation_columns:
        conn.execute(
            f"ALTER TABLE {ACTIVATION_TABLE} ADD COLUMN activation_evidence_sha256 TEXT NOT NULL DEFAULT ''"
        )
    for table in (ARTIFACT_TABLE, ACTIVATION_TABLE):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, '{table}_append_only'); END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, '{table}_append_only'); END
            """
        )


def register_strong_lr_runtime_artifact(
    conn: sqlite3.Connection,
    *,
    artifact: Mapping[str, Any],
    registered_at: str,
    registered_by: str,
    registration_basis: str,
    registration_ref: str,
) -> dict[str, Any]:
    """Register one structurally valid immutable scorer, without activation."""

    _assert_isolated(conn)
    if not _table_exists(conn, ARTIFACT_TABLE):
        raise ValueError("strong_lr_registry_schema_missing")
    value = dict(_mapping(artifact))
    validation = validate_strong_observation_lr_runtime_artifact(value)
    artifact_sha = _text(value.get("model_artifact_sha256"))
    if validation.get("ok") is not True:
        raise ValueError("strong_lr_registry_artifact_invalid:" + ",".join(validation.get("issues") or []))
    if _time(registered_at) is None or not _text(registered_by) or not _text(registration_basis) or not _text(registration_ref):
        raise ValueError("strong_lr_registry_registration_identity_invalid")
    artifact_json = _json(value)
    json_sha = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
    existing = conn.execute(
        f"SELECT artifact_json_sha256,artifact_json FROM {ARTIFACT_TABLE} WHERE artifact_sha256=?",
        (artifact_sha,),
    ).fetchone()
    if existing is not None:
        if _text(existing[0]) != json_sha or _text(existing[1]) != artifact_json:
            raise ValueError("strong_lr_registry_artifact_identity_conflict")
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "already_registered", "artifact_sha256": artifact_sha}
    conn.execute(
        f"INSERT INTO {ARTIFACT_TABLE} VALUES(?,?,?,?,?,?,?,?)",
        (
            artifact_sha,
            _text(value.get("schema")),
            artifact_json,
            json_sha,
            _utc_text(registered_at),
            _text(registered_by),
            _text(registration_basis),
            _text(registration_ref),
        ),
    )
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "registered", "artifact_sha256": artifact_sha}


def _validate_activation_evidence(
    *,
    evidence: Mapping[str, Any],
    target_identity: str,
    artifact_sha256: str,
    activation_basis: str,
    activation_ref: str,
    artifact: Mapping[str, Any],
    activated_at: str,
) -> dict[str, Any]:
    value = dict(_mapping(evidence))
    if _text(value.get("schema")) != ACTIVATION_ATTESTATION_SCHEMA_VERSION:
        raise ValueError("strong_lr_registry_activation_attestation_schema_invalid")
    if (
        _text(value.get("target_identity")) != target_identity
        or _text(value.get("artifact_sha256")) != artifact_sha256
        or _text(value.get("activation_basis")) != activation_basis
        or _text(value.get("activation_ref")) != activation_ref
        or _time(value.get("attested_at")) is None
        or _time(value.get("attested_at")) > _time(activated_at)
        or not _text(value.get("attested_by"))
    ):
        raise ValueError("strong_lr_registry_activation_attestation_identity_invalid")
    if activation_basis == "promoted_candidate":
        gate = _mapping(value.get("promotion_gate"))
        gate_without_sha = {key: item for key, item in gate.items() if key != "promotion_gate_sha256"}
        artifact_value = _mapping(artifact)
        evaluator = _mapping(artifact_value.get("feature_evaluator"))
        if (
            _text(gate.get("schema")) != PROMOTION_GATE_SCHEMA_VERSION
            or gate.get("ok") is not True
            or _text(gate.get("state")) != "eligible_for_registry_activation"
            or _text(gate.get("candidate_artifact_sha256")) != artifact_sha256
            or not _text(gate.get("promotion_gate_sha256"))
            or _text(gate.get("promotion_gate_sha256")) != _sha(gate_without_sha)
            or not _text(gate.get("training_manifest_sha256"))
            or _text(gate.get("training_manifest_sha256"))
            != _text(artifact_value.get("training_manifest_sha256"))
            or not _text(gate.get("oos_evaluation_sha256"))
            or _text(gate.get("oos_evaluation_sha256"))
            != _text(artifact_value.get("oos_evaluation_sha256"))
            or not _text(gate.get("feature_ablation_sha256"))
            or _text(gate.get("feature_ablation_sha256"))
            != _text(evaluator.get("oos_feature_ablation_sha256"))
            or gate.get("read_only") is not True
            or gate.get("offline_only") is not True
        ):
            raise ValueError("strong_lr_registry_promotion_gate_invalid")
    elif activation_basis == "approved_runtime_baseline":
        approval = _mapping(value.get("baseline_approval"))
        if (
            _text(approval.get("approval_kind")) != "operational_precalibration_baseline"
            or _text(approval.get("approval_state")) != "approved"
            or approval.get("not_trained_or_oos_promoted") is not True
            or not _text(approval.get("risk_and_scope_statement"))
        ):
            raise ValueError("strong_lr_registry_runtime_baseline_approval_invalid")
    else:
        raise ValueError("strong_lr_registry_activation_basis_invalid")
    return value


def activate_strong_lr_runtime_artifact(
    conn: sqlite3.Connection,
    *,
    target_identity: str,
    artifact_sha256: str,
    activated_at: str,
    activated_by: str,
    activation_basis: str,
    activation_ref: str,
    activation_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one activation event; latest as-of event is the sole active model."""

    _assert_isolated(conn)
    target = _text(target_identity)
    artifact_sha = _text(artifact_sha256)
    basis = _text(activation_basis)
    activated_at_utc = _utc_text(activated_at)
    if not target or _time(activated_at) is None or not _text(activated_by) or basis not in ACTIVATION_BASES or not _text(activation_ref):
        raise ValueError("strong_lr_registry_activation_identity_invalid")
    artifact_row = conn.execute(
        f"SELECT artifact_json FROM {ARTIFACT_TABLE} WHERE artifact_sha256=?", (artifact_sha,)
    ).fetchone()
    if artifact_row is None:
        raise ValueError("strong_lr_registry_activation_artifact_missing")
    try:
        registered_artifact = json.loads(_text(artifact_row[0]))
    except (TypeError, ValueError) as exc:
        raise ValueError("strong_lr_registry_activation_artifact_json_invalid") from exc
    evidence = _validate_activation_evidence(
        evidence=activation_evidence,
        target_identity=target,
        artifact_sha256=artifact_sha,
        activation_basis=basis,
        activation_ref=_text(activation_ref),
        artifact=_mapping(registered_artifact),
        activated_at=activated_at_utc,
    )
    evidence_json = _json(evidence)
    evidence_sha = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
    same_time = conn.execute(
        f"SELECT activation_id,artifact_sha256,activated_by,activation_basis,activation_ref,"
        "activation_evidence_sha256,previous_activation_id,activation_sha256 "
        f"FROM {ACTIVATION_TABLE} WHERE target_identity=? AND activated_at=?",
        (target, activated_at_utc),
    ).fetchall()
    for row in same_time:
        stored_core = {
            "target_identity": target,
            "artifact_sha256": _text(row[1]),
            "activated_at": activated_at_utc,
            "activated_by": _text(row[2]),
            "activation_basis": _text(row[3]),
            "activation_ref": _text(row[4]),
            "activation_evidence_sha256": _text(row[5]),
            "previous_activation_id": _text(row[6]),
        }
        requested_identity = (
            artifact_sha,
            _text(activated_by),
            basis,
            _text(activation_ref),
            evidence_sha,
        )
        stored_identity = (
            stored_core["artifact_sha256"],
            stored_core["activated_by"],
            stored_core["activation_basis"],
            stored_core["activation_ref"],
            stored_core["activation_evidence_sha256"],
        )
        if requested_identity == stored_identity and _sha(stored_core) == _text(row[7]):
            return {
                "schema": SCHEMA_VERSION,
                "ok": True,
                "state": "already_active_event",
                "activation_id": _text(row[0]),
                **stored_core,
            }
    if same_time:
        raise ValueError("strong_lr_registry_activation_time_collision")
    previous = conn.execute(
        f"SELECT activation_id,activated_at FROM {ACTIVATION_TABLE} WHERE target_identity=? "
        "ORDER BY activated_at DESC,activation_id DESC LIMIT 1",
        (target,),
    ).fetchone()
    if previous is not None and _time(activated_at_utc) <= _time(previous[1]):
        raise ValueError("strong_lr_registry_activation_time_regression")
    core = {
        "target_identity": target,
        "artifact_sha256": artifact_sha,
        "activated_at": activated_at_utc,
        "activated_by": _text(activated_by),
        "activation_basis": basis,
        "activation_ref": _text(activation_ref),
        "activation_evidence_sha256": evidence_sha,
        "previous_activation_id": _text(previous[0]) if previous else "",
    }
    activation_sha = _sha(core)
    activation_id = f"stronglract_{activation_sha[:24]}"
    existing = conn.execute(
        f"SELECT activation_sha256 FROM {ACTIVATION_TABLE} WHERE activation_id=?",
        (activation_id,),
    ).fetchone()
    if existing is not None:
        if _text(existing[0]) != activation_sha:
            raise ValueError("strong_lr_registry_activation_identity_conflict")
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "already_active_event", "activation_id": activation_id, **core}
    conn.execute(
        f"INSERT INTO {ACTIVATION_TABLE} "
        "(activation_id,target_identity,artifact_sha256,activated_at,activated_by,activation_basis,activation_ref,"
        "activation_evidence_json,activation_evidence_sha256,previous_activation_id,activation_sha256) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            activation_id,
            target,
            artifact_sha,
            activated_at_utc,
            _text(activated_by),
            basis,
            _text(activation_ref),
            evidence_json,
            evidence_sha,
            core["previous_activation_id"],
            activation_sha,
        ),
    )
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "activated", "activation_id": activation_id, **core}


def load_active_strong_lr_runtime_artifact(
    conn: sqlite3.Connection,
    *,
    target_identity: str,
    as_of: str = "",
) -> dict[str, Any]:
    """Load and revalidate the sole latest active artifact for a target."""

    _assert_isolated(conn)
    target = _text(target_identity)
    cutoff = _time(as_of) if _text(as_of) else None
    if not target or (_text(as_of) and cutoff is None):
        raise ValueError("strong_lr_registry_load_identity_or_asof_invalid")
    params: tuple[Any, ...]
    where = "a.target_identity=?"
    params = (target,)
    if cutoff is not None:
        where += " AND a.activated_at<=?"
        params = (target, cutoff.isoformat().replace("+00:00", "Z"))
    row = conn.execute(
        f"""
        SELECT a.activation_id,a.artifact_sha256,a.activated_at,a.activated_by,a.activation_basis,a.activation_ref,
               a.activation_evidence_json,a.activation_evidence_sha256,
               a.previous_activation_id,a.activation_sha256,
               r.artifact_json,r.artifact_json_sha256
        FROM {ACTIVATION_TABLE} a
        JOIN {ARTIFACT_TABLE} r ON r.artifact_sha256=a.artifact_sha256
        WHERE {where}
        ORDER BY a.activated_at DESC,a.activation_id DESC LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        raise ValueError("strong_lr_registry_active_artifact_missing")
    try:
        artifact = json.loads(_text(row[10]))
        activation_evidence = json.loads(_text(row[6]))
    except (TypeError, ValueError) as exc:
        raise ValueError("strong_lr_registry_active_artifact_json_invalid") from exc
    try:
        _validate_activation_evidence(
            evidence=_mapping(activation_evidence),
            target_identity=target,
            artifact_sha256=_text(row[1]),
            activation_basis=_text(row[4]),
            activation_ref=_text(row[5]),
            artifact=_mapping(artifact),
            activated_at=_text(row[2]),
        )
    except ValueError as exc:
        raise ValueError("strong_lr_registry_active_activation_evidence_invalid") from exc
    if (
        hashlib.sha256(_text(row[10]).encode("utf-8")).hexdigest() != _text(row[11])
        or hashlib.sha256(_text(row[6]).encode("utf-8")).hexdigest() != _text(row[7])
        or _text(_mapping(artifact).get("model_artifact_sha256")) != _text(row[1])
        or validate_strong_observation_lr_runtime_artifact(_mapping(artifact)).get("ok") is not True
    ):
        raise ValueError("strong_lr_registry_active_artifact_integrity_invalid")
    activation_core = {
        "target_identity": target,
        "artifact_sha256": _text(row[1]),
        "activated_at": _text(row[2]),
        "activated_by": _text(row[3]),
        "activation_basis": _text(row[4]),
        "activation_ref": _text(row[5]),
        "activation_evidence_sha256": _text(row[7]),
        "previous_activation_id": _text(row[8]),
    }
    if _sha(activation_core) != _text(row[9]):
        raise ValueError("strong_lr_registry_active_activation_integrity_invalid")
    expected_previous = conn.execute(
        f"SELECT activation_id FROM {ACTIVATION_TABLE} "
        "WHERE target_identity=? AND activated_at<? "
        "ORDER BY activated_at DESC,activation_id DESC LIMIT 1",
        (target, _text(row[2])),
    ).fetchone()
    expected_previous_id = _text(expected_previous[0]) if expected_previous else ""
    if expected_previous_id != _text(row[8]):
        raise ValueError("strong_lr_registry_active_activation_predecessor_invalid")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "active",
        "target_identity": target,
        "activation_id": _text(row[0]),
        "artifact_sha256": _text(row[1]),
        "activated_at": _text(row[2]),
        "activation_basis": _text(row[4]),
        "activation_ref": _text(row[5]),
        "activation_evidence_sha256": _text(row[7]),
        "activation_evidence": dict(_mapping(activation_evidence)),
        "artifact": dict(_mapping(artifact)),
    }


def assert_active_strong_lr_artifact_binding(
    conn: sqlite3.Connection, *, target_identity: str, artifact_sha256: str, as_of: str = ""
) -> dict[str, Any]:
    active = load_active_strong_lr_runtime_artifact(conn, target_identity=target_identity, as_of=as_of)
    if _text(active.get("artifact_sha256")) != _text(artifact_sha256):
        raise ValueError("strong_lr_registry_active_artifact_binding_mismatch")
    return active


__all__ = [
    "ACTIVATION_BASES",
    "ACTIVATION_ATTESTATION_SCHEMA_VERSION",
    "ACTIVATION_REQUIRED_COLUMNS",
    "ACTIVATION_TABLE",
    "ARTIFACT_REQUIRED_COLUMNS",
    "ARTIFACT_TABLE",
    "SCHEMA_VERSION",
    "activate_strong_lr_runtime_artifact",
    "assert_active_strong_lr_artifact_binding",
    "initialize_strong_lr_runtime_registry_schema",
    "load_active_strong_lr_runtime_artifact",
    "register_strong_lr_runtime_artifact",
]
