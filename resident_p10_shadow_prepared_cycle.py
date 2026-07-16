"""Durable, immutable prepared-cycle contract for the resident P10 shadow host.

The P10 hot path must not accept an arbitrary caller-owned dictionary as its
forecast/prior/LR/market context.  Preparation is a separate, explicit
shadow-only phase: it freezes a fully validated cycle, writes an append-only
record to the isolated shadow DB, and the resident host later loads it only by
its content-derived ID.  This module never fetches, refreshes, trains, signs,
or submits anything.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Mapping

from observation_fast_lane_contract import validate_observation_fast_lane_topology
from resident_fast_lane_prewarm_contract import (
    validate_resident_fast_lane_prewarm,
    validate_resident_market_projection_prewarm,
)
from strong_observation_lr_artifact_contract import validate_strong_observation_lr_runtime_artifact
from strong_observation_lr_runtime_registry import (
    assert_active_strong_lr_artifact_binding,
    initialize_strong_lr_runtime_registry_schema,
)
from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)


SCHEMA_VERSION = "resident_p10_shadow_prepared_cycle_v1"
PREPARED_CYCLE_SCHEMA = "resident_p10_shadow_prepared_cycle_v1"
PREPARED_CYCLE_STATE = "prepared_shadow_only"
PREPARED_CYCLE_TABLE = "resident_p10_prepared_cycles_shadow"
DEFAULT_TOPOLOGY = {
    "release_delay_ms": 0,
    "synchronous_forecast_refresh_on_hot_path": False,
    "generic_task_queue_on_hot_path": False,
    "resident_canonical_and_inference_worker": True,
    "transaction_b_atomic_before_intent": True,
    "posterior_commit_outbox_durable_before_intent": True,
    "decision_reads_committed_ssot": True,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _forbidden_execution_keys(value: Any, *, path: str = "") -> list[str]:
    """Find L5/secret fields that must never be frozen into a P10 cycle."""

    forbidden: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = _text(raw_key).lower()
            child_path = f"{path}.{raw_key}" if path else _text(raw_key)
            if any(token in key for token in ("signer", "clob", "private_key", "credential", "access_token")):
                forbidden.append(child_path)
            forbidden.extend(_forbidden_execution_keys(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            forbidden.extend(_forbidden_execution_keys(child, path=f"{path}[{index}]"))
    return forbidden


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _frozen_payload(cycle: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in _mapping(cycle).items()
        if key not in {"prepared_cycle_id", "prepared_cycle_sha256"}
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_isolated_preparation_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ValueError("resident_p10_prepared_cycle_manifest_missing")
    manifest = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if manifest is None or _text(manifest[0]) != SHADOW_MANIFEST_STATE:
        raise ValueError("resident_p10_prepared_cycle_manifest_invalid")
    if not _table_exists(conn, "posterior_checkpoints"):
        raise ValueError("resident_p10_prepared_cycle_checkpoint_table_missing")


def _checkpoint_persisted(conn: sqlite3.Connection, checkpoint: Mapping[str, Any]) -> bool:
    row = conn.execute(
        """
        SELECT posterior_run_id,joint_posterior_sha256,target_identity
        FROM posterior_checkpoints WHERE posterior_checkpoint_id=?
        """,
        (_text(checkpoint.get("posterior_checkpoint_id")),),
    ).fetchone()
    return bool(
        row
        and _text(row[0]) == _text(checkpoint.get("posterior_run_id"))
        and _text(row[1]) == _text(checkpoint.get("joint_posterior_sha256"))
        and _text(row[2]) == _text(checkpoint.get("target_identity"))
    )


def validate_resident_p10_shadow_cycle(prepared_cycle: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the frozen P10 dependency, posterior, and decision bindings.

    It is deliberately pure: validation does not create a dependency, synthesize
    a clock, or repair a stale prewarm entry.
    """

    cycle = _mapping(prepared_cycle)
    posterior = _mapping(cycle.get("posterior_context"))
    decision = _mapping(cycle.get("decision_context"))
    previous = _mapping(cycle.get("previous_checkpoint"))
    resolution_contract = _mapping(posterior.get("resolution_contract"))
    market_projection_required = bool(
        resolution_contract.get("bridge_closed") is True
        and resolution_contract.get("validated") is True
    )
    prewarm = validate_resident_fast_lane_prewarm(
        cycle.get("prewarmed_components"),
        decision_as_of=cycle.get("prepared_at") if _parse_datetime(cycle.get("prepared_at")) is not None else "",
    )
    topology = validate_observation_fast_lane_topology(
        cycle.get("topology") or DEFAULT_TOPOLOGY,
        require_execution_readiness=False,
    )
    blockers: list[str] = []
    required_cycle = ("target_identity", "event_date", "public_slug")
    required_posterior = (
        "forecast_evidence_snapshot_id",
        "prior_context_id",
        "approved_lr_artifact",
        "resolution_contract",
        "realization_policy",
        "temperature_quantization_c",
        "source_reliability_before",
        "source_reliability_after",
        "path_auxiliary",
    )
    required_decision = (
        "lease_owner",
        "market_snapshot",
        "capital_plan",
        "strategy_artifact_sha256",
        "top_yes_wave_entry_plan",
    )
    required_previous = (
        "posterior_checkpoint_id",
        "posterior_run_id",
        "target_identity",
        "event_date",
        "joint_posterior",
        "joint_posterior_sha256",
    )
    for key in required_cycle:
        if not _text(cycle.get(key)):
            blockers.append(f"resident_p10_cycle_{key}_missing")
    for key in required_posterior:
        value = posterior.get(key)
        if value is None or value == "":
            blockers.append(f"resident_p10_posterior_{key}_missing")
    if market_projection_required:
        for key in required_decision:
            value = decision.get(key)
            if value is None or value == "":
                blockers.append(f"resident_p10_decision_{key}_missing")
    for key in required_previous:
        value = previous.get(key)
        if value is None or value == "":
            blockers.append(f"resident_p10_previous_{key}_missing")
    if previous and _text(previous.get("target_identity")) != _text(cycle.get("target_identity")):
        blockers.append("resident_p10_previous_target_identity_mismatch")
    if previous and _text(previous.get("event_date")) != _text(cycle.get("event_date")):
        blockers.append("resident_p10_previous_event_date_mismatch")
    components = _mapping(cycle.get("prewarmed_components"))
    if "resident_signer_connection_pool" in components:
        blockers.append("resident_p10_execution_component_forbidden")
    if "signer_connection_pool_resident" in _mapping(cycle.get("topology")):
        blockers.append("resident_p10_execution_topology_field_forbidden")
    for field_path in _forbidden_execution_keys(cycle):
        blockers.append(f"resident_p10_execution_or_secret_field_forbidden:{field_path}")
    forecast = _mapping(components.get("forecast_snapshot"))
    prior = _mapping(components.get("prior_context"))
    strong = _mapping(components.get("strong_lr_artifact"))
    market = _mapping(components.get("market_context"))
    market_prewarm = (
        validate_resident_market_projection_prewarm(
            components,
            decision_as_of=cycle.get("prepared_at") if _parse_datetime(cycle.get("prepared_at")) is not None else "",
        )
        if market_projection_required
        else {
            "ok": True,
            "state": "not_applicable",
            "blockers": [],
            "purpose": "market_projection_and_decision",
            "na_reason": "x_to_s_to_b_bridge_unavailable",
        }
    )
    artifact = _mapping(posterior.get("approved_lr_artifact"))
    if _mapping(posterior.get("source_reliability_after")) != _mapping(posterior.get("source_reliability_before")):
        blockers.append("resident_p10_external_source_reliability_post_state_update_not_allowed")
    market_snapshot = _mapping(decision.get("market_snapshot"))
    if forecast and _text(forecast.get("forecast_snapshot_id")) != _text(posterior.get("forecast_evidence_snapshot_id")):
        blockers.append("resident_p10_forecast_prewarm_posterior_binding_mismatch")
    if prior and _text(prior.get("prior_context_id")) != _text(posterior.get("prior_context_id")):
        blockers.append("resident_p10_prior_prewarm_posterior_binding_mismatch")
    if strong and _text(strong.get("strong_lr_artifact_sha256")) != _text(artifact.get("model_artifact_sha256")):
        blockers.append("resident_p10_strong_artifact_prewarm_posterior_binding_mismatch")
    # P10 validates the score surface that it actually runs.  The offline
    # strict-as-of/OOS release packet is intentionally not a runtime trade
    # gate and is checked only when promoting a new artifact version.
    strong_artifact_contract = validate_strong_observation_lr_runtime_artifact(artifact)
    if strong_artifact_contract.get("ok") is not True:
        blockers.extend(
            f"resident_p10_strong_artifact_contract:{issue}"
            for issue in strong_artifact_contract.get("issues") or ["invalid"]
        )
    if market and _text(market.get("market_snapshot_id")) != _text(market_snapshot.get("market_snapshot_id")):
        blockers.append("resident_p10_market_prewarm_decision_binding_mismatch")

    if _text(cycle.get("schema")) != PREPARED_CYCLE_SCHEMA:
        blockers.append("resident_p10_prepared_cycle_schema_invalid")
    if _text(cycle.get("activation_state")) != PREPARED_CYCLE_STATE:
        blockers.append("resident_p10_prepared_cycle_activation_state_invalid")
    if _parse_datetime(cycle.get("prepared_at")) is None:
        blockers.append("resident_p10_prepared_cycle_prepared_at_invalid")
    if not _text(cycle.get("prepared_by")):
        blockers.append("resident_p10_prepared_cycle_prepared_by_missing")
    dependency_identity = _text(prewarm.get("dependency_identity_sha256"))
    if _text(cycle.get("runtime_dependency_identity_sha256")) != dependency_identity:
        blockers.append("resident_p10_prepared_cycle_dependency_identity_mismatch")
    payload_sha256 = _sha256(_frozen_payload(cycle))
    if _text(cycle.get("prepared_cycle_sha256")) != payload_sha256:
        blockers.append("resident_p10_prepared_cycle_sha256_mismatch")
    expected_cycle_id = f"p10pc_{payload_sha256[:24]}"
    if _text(cycle.get("prepared_cycle_id")) != expected_cycle_id:
        blockers.append("resident_p10_prepared_cycle_id_mismatch")
    all_blockers = sorted(set(
        blockers
        + list(prewarm.get("blockers") or [])
        + list(market_prewarm.get("blockers") or [])
        + list(topology.get("blockers") or [])
    ))
    return {
        "schema": SCHEMA_VERSION,
        "ok": not all_blockers,
        "state": "ready" if not all_blockers else "blocked",
        "blockers": all_blockers,
        "prepared_cycle_id": _text(cycle.get("prepared_cycle_id")),
        "prepared_cycle_sha256": _text(cycle.get("prepared_cycle_sha256")),
        "prewarm": prewarm,
        "market_projection_prewarm": market_prewarm,
        "market_projection_required": market_projection_required,
        "strong_artifact_contract": strong_artifact_contract,
        "topology": topology,
        "boundary": {
            "network_acquisition": False,
            "synchronous_forecast_refresh": False,
            "generic_task_queue": False,
            "production_archive_write": False,
            "places_orders": False,
            "market_context_cannot_block_physical_posterior": True,
            "freezes_caller_owned_input": True,
        },
    }


def freeze_resident_p10_shadow_cycle(
    cycle_input: Mapping[str, Any],
    *,
    prepared_at: str,
    prepared_by: str,
) -> dict[str, Any]:
    """Freeze a caller input into a content-addressed shadow prepared cycle.

    ``prepared_at`` is intentionally required from an external observer.  This
    contract never fabricates a preparation timestamp or fetches a dependency.
    The result is not usable by a host until it is persisted in an isolated DB.
    """

    cycle = json.loads(_canonical_json(_mapping(cycle_input)))
    cycle.update(
        {
            "schema": PREPARED_CYCLE_SCHEMA,
            "activation_state": PREPARED_CYCLE_STATE,
            "prepared_at": _text(prepared_at),
            "prepared_by": _text(prepared_by),
            # The topology is execution-critical input too.  Persist the
            # resolved default rather than allowing a later code default to
            # silently alter a cycle that was already frozen.
            "topology": _mapping(cycle.get("topology")) or dict(DEFAULT_TOPOLOGY),
            "runtime_dependency_identity_sha256": _text(
                validate_resident_fast_lane_prewarm(cycle.get("prewarmed_components")).get(
                    "dependency_identity_sha256"
                )
            ),
        }
    )
    payload_sha256 = _sha256(_frozen_payload(cycle))
    cycle["prepared_cycle_id"] = f"p10pc_{payload_sha256[:24]}"
    cycle["prepared_cycle_sha256"] = payload_sha256
    validation = validate_resident_p10_shadow_cycle(cycle)
    return {
        "schema": SCHEMA_VERSION,
        "ok": validation.get("ok") is True,
        "state": "frozen" if validation.get("ok") is True else "blocked",
        "blockers": list(validation.get("blockers") or []),
        "prepared_cycle": cycle,
        "validation": validation,
        "shadow_only": True,
        "places_orders": False,
        "boundary": {
            "loads_or_refreshes_dependencies": False,
            "synthesizes_prepared_at": False,
            "production_archive_write": False,
            "clob": False,
        },
    }


def initialize_resident_p10_shadow_prepared_cycle_schema(conn: sqlite3.Connection) -> None:
    """Create the append-only prepared-cycle registry in an isolated DB."""

    _assert_isolated_preparation_db(conn)
    initialize_strong_lr_runtime_registry_schema(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PREPARED_CYCLE_TABLE} (
            prepared_cycle_id TEXT PRIMARY KEY,
            target_identity TEXT NOT NULL,
            event_date TEXT NOT NULL,
            public_slug TEXT NOT NULL,
            activation_state TEXT NOT NULL CHECK(activation_state='{PREPARED_CYCLE_STATE}'),
            prepared_at TEXT NOT NULL,
            prepared_by TEXT NOT NULL,
            prepared_cycle_sha256 TEXT NOT NULL UNIQUE,
            runtime_dependency_identity_sha256 TEXT NOT NULL,
            cycle_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(target_identity, event_date, prepared_cycle_sha256)
        )
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {PREPARED_CYCLE_TABLE}_no_update
        BEFORE UPDATE ON {PREPARED_CYCLE_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'resident_p10_prepared_cycle_append_only');
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {PREPARED_CYCLE_TABLE}_no_delete
        BEFORE DELETE ON {PREPARED_CYCLE_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'resident_p10_prepared_cycle_append_only');
        END
        """
    )


def persist_resident_p10_shadow_cycle(
    conn: sqlite3.Connection,
    *,
    prepared_cycle: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one frozen cycle after proving its prior checkpoint is durable."""

    _assert_isolated_preparation_db(conn)
    if not _table_exists(conn, PREPARED_CYCLE_TABLE):
        raise ValueError("resident_p10_prepared_cycle_registry_missing")
    cycle = _mapping(prepared_cycle)
    validation = validate_resident_p10_shadow_cycle(cycle)
    if validation.get("ok") is not True:
        raise ValueError("resident_p10_prepared_cycle_invalid:" + ",".join(validation.get("blockers") or []))
    if not _checkpoint_persisted(conn, _mapping(cycle.get("previous_checkpoint"))):
        raise ValueError("resident_p10_prepared_cycle_previous_checkpoint_not_persisted")
    artifact = _mapping(_mapping(cycle.get("posterior_context")).get("approved_lr_artifact"))
    assert_active_strong_lr_artifact_binding(
        conn,
        target_identity=_text(cycle.get("target_identity")),
        artifact_sha256=_text(artifact.get("model_artifact_sha256")),
        as_of=_text(cycle.get("prepared_at")),
    )
    row_values = {
        "prepared_cycle_id": _text(cycle.get("prepared_cycle_id")),
        "target_identity": _text(cycle.get("target_identity")),
        "event_date": _text(cycle.get("event_date")),
        "public_slug": _text(cycle.get("public_slug")),
        "activation_state": _text(cycle.get("activation_state")),
        "prepared_at": _text(cycle.get("prepared_at")),
        "prepared_by": _text(cycle.get("prepared_by")),
        "prepared_cycle_sha256": _text(cycle.get("prepared_cycle_sha256")),
        "runtime_dependency_identity_sha256": _text(cycle.get("runtime_dependency_identity_sha256")),
        "cycle_json": _canonical_json(cycle),
        "created_at": _text(cycle.get("prepared_at")),
    }
    existing = conn.execute(
        f"SELECT prepared_cycle_sha256,cycle_json FROM {PREPARED_CYCLE_TABLE} WHERE prepared_cycle_id=?",
        (row_values["prepared_cycle_id"],),
    ).fetchone()
    if existing is not None:
        if _text(existing[0]) != row_values["prepared_cycle_sha256"] or _text(existing[1]) != row_values["cycle_json"]:
            raise ValueError("resident_p10_prepared_cycle_append_only_conflict")
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "already_persisted",
            "prepared_cycle_id": row_values["prepared_cycle_id"],
            "prepared_cycle_sha256": row_values["prepared_cycle_sha256"],
            "shadow_only": True,
            "places_orders": False,
        }
    fields = list(row_values)
    conn.execute(
        f"INSERT INTO {PREPARED_CYCLE_TABLE}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row_values[field] for field in fields),
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "persisted",
        "prepared_cycle_id": row_values["prepared_cycle_id"],
        "prepared_cycle_sha256": row_values["prepared_cycle_sha256"],
        "shadow_only": True,
        "places_orders": False,
    }


def load_resident_p10_shadow_cycle(
    conn: sqlite3.Connection,
    *,
    prepared_cycle_id: str,
) -> dict[str, Any]:
    """Load one exact frozen cycle by ID; malformed registry data fails closed."""

    _assert_isolated_preparation_db(conn)
    if not _table_exists(conn, PREPARED_CYCLE_TABLE):
        raise ValueError("resident_p10_prepared_cycle_registry_missing")
    cycle_id = _text(prepared_cycle_id)
    row = conn.execute(
        f"""
        SELECT prepared_cycle_id,prepared_cycle_sha256,cycle_json,target_identity,event_date,
               public_slug,activation_state,runtime_dependency_identity_sha256
        FROM {PREPARED_CYCLE_TABLE} WHERE prepared_cycle_id=?
        """,
        (cycle_id,),
    ).fetchone()
    if row is None:
        raise ValueError("resident_p10_prepared_cycle_missing")
    try:
        parsed = json.loads(_text(row[2]))
    except (TypeError, ValueError) as exc:
        raise ValueError("resident_p10_prepared_cycle_json_invalid") from exc
    cycle = _mapping(parsed)
    validation = validate_resident_p10_shadow_cycle(cycle)
    registry_matches = (
        _text(row[0]) == _text(cycle.get("prepared_cycle_id")) == cycle_id
        and _text(row[1]) == _text(cycle.get("prepared_cycle_sha256"))
        and _text(row[3]) == _text(cycle.get("target_identity"))
        and _text(row[4]) == _text(cycle.get("event_date"))
        and _text(row[5]) == _text(cycle.get("public_slug"))
        and _text(row[6]) == _text(cycle.get("activation_state"))
        and _text(row[7]) == _text(cycle.get("runtime_dependency_identity_sha256"))
        and _canonical_json(cycle) == _text(row[2])
    )
    if validation.get("ok") is not True or not registry_matches:
        reasons = list(validation.get("blockers") or [])
        if not registry_matches:
            reasons.append("resident_p10_prepared_cycle_registry_binding_mismatch")
        raise ValueError("resident_p10_prepared_cycle_load_invalid:" + ",".join(sorted(set(reasons))))
    artifact = _mapping(_mapping(cycle.get("posterior_context")).get("approved_lr_artifact"))
    assert_active_strong_lr_artifact_binding(
        conn,
        target_identity=_text(cycle.get("target_identity")),
        artifact_sha256=_text(artifact.get("model_artifact_sha256")),
    )
    return cycle


__all__ = [
    "DEFAULT_TOPOLOGY",
    "PREPARED_CYCLE_SCHEMA",
    "PREPARED_CYCLE_STATE",
    "PREPARED_CYCLE_TABLE",
    "SCHEMA_VERSION",
    "freeze_resident_p10_shadow_cycle",
    "initialize_resident_p10_shadow_prepared_cycle_schema",
    "load_resident_p10_shadow_cycle",
    "persist_resident_p10_shadow_cycle",
    "validate_resident_p10_shadow_cycle",
]
