"""Build and persist the P0 prior×weak initial posterior in isolated shadow.

P0 is the only legal predecessor of the first D0 strong-observation update:

    p0(X, Z) = Normalize(p_prior(X) * L_forecast_weak(X) * p_prior(Z | X))

It deliberately consumes no observation, market, strategy, order, or legacy
v3.1 surface.  P0 always establishes the physical maximum X.  A closed
X→S→B bridge is required only to expose market-bucket B probabilities; before
then the checkpoint carries an explicit B-NA projection rather than blocking X.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from lineage_identity_runtime import build_fact_snapshot_id, build_posterior_checkpoint_id, build_posterior_run_id
from resolution_bridge_runtime import PHYSICAL_ONLY_STATE, build_physical_only_resolution_bridge, build_resolution_bridge
from sequential_bayes_public_ssot_shadow_writer import SHADOW_MANIFEST_STATE, SHADOW_MANIFEST_TABLE
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError, materialize_checkpoint_row
from strong_observation_lr_artifact_contract import validate_strong_observation_lr_runtime_artifact
from strong_observation_lr_runtime_registry import assert_active_strong_lr_artifact_binding
from temperature_trajectory_feature_runtime import build_empty_pre_event_temperature_trajectory_feature_state


SCHEMA_VERSION = "initial_posterior_checkpoint_shadow_v1"
PRODUCTION_SCHEMA_VERSION = "production_initial_posterior_checkpoint_v1"
INITIAL_LINEAGE_TABLE = "initial_posterior_lineage_shadow"
INITIAL_LINEAGE_SCHEMA = "initial_posterior_lineage_shadow_v1"
PRODUCTION_INITIAL_LINEAGE_SCHEMA = "production_initial_posterior_lineage_v1"
INITIAL_LINEAGE_REQUIRED_COLUMNS = (
    "posterior_checkpoint_id",
    "target_identity",
    "posterior_run_id",
    "prior_context_id",
    "prior_artifact_sha256",
    "weak_likelihood_artifact_sha256",
    "weak_likelihood_sha256",
    "forecast_evidence_snapshot_id",
    "strong_lr_artifact_sha256",
    "source_reliability_initial_state_sha256",
    "resolution_contract_id",
    "initial_evidence_sha256",
    "lineage_json",
    "created_at",
)
CHECKPOINT_SCHEMA_VERSION = "posterior_checkpoint_shadow_v2"
PRODUCTION_CHECKPOINT_SCHEMA_VERSION = "posterior_checkpoint_v2"
EPSILON = 1e-12


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_isolated_shadow_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ShadowTransactionIntegrityError("initial_checkpoint_shadow_manifest_missing")
    manifest = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if manifest is None or _text(manifest[0]) != SHADOW_MANIFEST_STATE:
        raise ShadowTransactionIntegrityError("initial_checkpoint_shadow_manifest_invalid")
    if not _table_exists(conn, "posterior_checkpoints"):
        raise ShadowTransactionIntegrityError("initial_checkpoint_shadow_checkpoint_table_missing")


def _forbidden_surface(value: Any, *, path: str = "") -> list[str]:
    forbidden = ("market", "price", "order", "position", "pnl", "edge", "lcb", "metar", "observation", "strategy")
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = _text(key).lower()
            child_path = f"{path}.{key_text}" if path else key_text
            if any(token in key_text for token in forbidden):
                found.append(child_path)
            found.extend(_forbidden_surface(child, path=child_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            found.extend(_forbidden_surface(child, path=f"{path}[{index}]"))
    return found


def _forbidden_source_reliability_surface(value: Any, *, path: str = "", depth: int = 0) -> list[str]:
    """Reject current-event/execution data without rejecting source IDs.

    A source-reliability pre-state is keyed by a source identity such as
    ``zuuu_metar_raw_amsc``.  Its key is an identity, not a current METAR
    payload; only nested state fields may be inspected for physical/event or
    market contamination.  At depth zero, obvious pseudo-source keys remain
    invalid so callers cannot smuggle a whole current observation as a source.
    """

    forbidden = (
        "market", "price", "order", "position", "pnl", "edge", "lcb",
        "current_observation", "current_report", "temperature", "dewpoint",
        "cloud", "weather", "trajectory", "momentum",
    )
    root_pseudo_source = (
        "current_observation", "current_report", "observation", "trajectory", "momentum",
        "temperature", "dewpoint", "cloud", "weather", "market", "price", "order", "position", "pnl", "edge", "lcb",
    )
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = _text(key).lower()
            child_path = f"{path}.{key_text}" if path else key_text
            if (depth == 0 and (key_text in {"metar", "speci"} or any(token in key_text for token in root_pseudo_source))) or (
                depth > 0 and any(token in key_text for token in forbidden)
            ):
                found.append(child_path)
            found.extend(_forbidden_source_reliability_surface(child, path=child_path, depth=depth + 1))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            found.extend(_forbidden_source_reliability_surface(child, path=f"{path}[{index}]", depth=depth + 1))
    return found


def _normalized_distribution(rows: Any, *, temperature_key: str, mass_key: str, prefix: str) -> tuple[list[tuple[float, float]], list[str]]:
    values = rows if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)) else []
    parsed: list[tuple[float, float]] = []
    issues: list[str] = []
    seen: set[float] = set()
    for value in values:
        row = _mapping(value)
        temperature = _finite(row.get(temperature_key))
        mass = _finite(row.get(mass_key))
        if temperature is None or mass is None or mass < 0.0 or temperature in seen:
            issues.append(f"{prefix}_row_invalid")
            continue
        seen.add(temperature)
        parsed.append((temperature, mass))
    total = sum(mass for _temperature, mass in parsed)
    if not parsed or abs(total - 1.0) > 1e-8:
        issues.append(f"{prefix}_not_normalized")
    return sorted(parsed), sorted(set(issues))


def _prior_components(prior_context: Mapping[str, Any]) -> tuple[list[tuple[float, float]], dict[float, dict[str, float]], list[str]]:
    context = _mapping(prior_context)
    issues: list[str] = []
    if not _text(context.get("prior_context_id")):
        issues.append("initial_prior_context_id_missing")
    if not _text(context.get("artifact_sha256")):
        issues.append("initial_prior_artifact_sha256_missing")
    # Governance metadata such as ``boundary.places_orders=false`` is not a
    # model input. Inspect only the prior's modelling payload; otherwise an
    # explicit safety declaration paradoxically blocks every governed prior.
    modelling_context = {
        key: value
        for key, value in context.items()
        if key not in {"boundary", "read_only", "shadow_only", "ok", "state"}
    }
    if _forbidden_surface(modelling_context):
        issues.append("initial_prior_context_contains_forbidden_runtime_surface")
    continuous = _mapping(context.get("continuous_distribution"))
    if _text(continuous.get("schema")) != "continuous_temperature_climate_prior_v1":
        issues.append("initial_prior_continuous_schema_invalid")
    prior_rows, prior_issues = _normalized_distribution(
        continuous.get("grid"), temperature_key="temp_c", mass_key="mass", prefix="initial_prior_continuous_distribution"
    )
    issues.extend(prior_issues)
    by_temperature = _mapping(context.get("latent_state_prior_by_temperature"))
    latent: dict[float, dict[str, float]] = {}
    for temperature, _mass in prior_rows:
        raw = _mapping(by_temperature.get(f"{temperature:g}") or by_temperature.get(str(temperature)))
        state_values: dict[str, float] = {}
        for state, probability in raw.items():
            parsed = _finite(probability)
            if not _text(state) or parsed is None or parsed < 0.0:
                issues.append("initial_prior_latent_state_row_invalid")
                continue
            state_values[_text(state)] = parsed
        if not state_values or abs(sum(state_values.values()) - 1.0) > 1e-8:
            issues.append("initial_prior_latent_state_not_normalized")
        else:
            latent[temperature] = dict(sorted(state_values.items()))
    if set(latent) != {temperature for temperature, _mass in prior_rows}:
        issues.append("initial_prior_latent_state_grid_mismatch")
    return prior_rows, latent, sorted(set(issues))


def _weak_components(weak_likelihood: Mapping[str, Any]) -> tuple[list[tuple[float, float]], list[str]]:
    weak = _mapping(weak_likelihood)
    issues: list[str] = []
    if weak.get("ok") is not True or _text(weak.get("state")) != "available":
        issues.append("initial_weak_likelihood_not_available")
    if _text(weak.get("schema")) != "joint_forecast_weak_likelihood_v1":
        issues.append("initial_weak_likelihood_schema_invalid")
    if not _text(weak.get("artifact_sha256")):
        issues.append("initial_weak_artifact_sha256_missing")
    if not _text(weak.get("forecast_evidence_snapshot_id")):
        issues.append("initial_forecast_evidence_snapshot_id_missing")
    timing_prior = _mapping(weak.get("forecast_peak_timing_prior"))
    if timing_prior:
        if _text(timing_prior.get("schema")) != "forecast_peak_timing_prior_v1":
            issues.append("initial_forecast_peak_timing_prior_schema_invalid")
        if _text(timing_prior.get("forecast_evidence_snapshot_id")) != _text(weak.get("forecast_evidence_snapshot_id")):
            issues.append("initial_forecast_peak_timing_prior_snapshot_mismatch")
    if weak.get("market_data_consumed") is not False or weak.get("observation_data_consumed") is not False:
        issues.append("initial_weak_likelihood_runtime_boundary_invalid")
    rows, row_issues = _normalized_distribution(
        weak.get("joint_forecast_weak_likelihood"),
        temperature_key="temperature_c",
        mass_key="likelihood",
        prefix="initial_weak_likelihood",
    )
    issues.extend(row_issues)
    grid = [_finite(value) for value in weak.get("temperature_grid_c") or []]
    if any(value is None for value in grid) or sorted(set(value for value in grid if value is not None)) != [temperature for temperature, _mass in rows]:
        issues.append("initial_weak_likelihood_grid_mismatch")
    if _forbidden_surface({"context": weak.get("forecast_context") or {}}):
        issues.append("initial_weak_likelihood_context_contains_forbidden_runtime_surface")
    return rows, sorted(set(issues))


def _blocked(*reasons: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "consumes_d0_observation": False,
            "consumes_market_or_strategy": False,
            "uses_legacy_v31_output": False,
            "writes_archive_db": False,
            "places_orders": False,
        },
    }


def _apply_initial_mode(value: Mapping[str, Any], *, initial_mode: str) -> dict[str, Any]:
    """Apply the execution boundary marker without changing Bayes evidence.

    The mathematical builder is shared, but production and shadow identities
    are deliberately distinct.  A production P0 can therefore never be
    copied from the shadow database or mistaken for shadow authority.
    """

    mode = _text(initial_mode)
    if mode not in {"shadow", "production"}:
        raise ValueError("initial_posterior_mode_must_be_shadow_or_production")
    result = dict(value)
    production = mode == "production"
    result["schema"] = PRODUCTION_SCHEMA_VERSION if production else SCHEMA_VERSION
    result["initial_mode"] = mode
    result["shadow_only"] = not production
    if result.get("ok") is True:
        checkpoint = dict(_mapping(result.get("checkpoint")))
        checkpoint["schema"] = (
            PRODUCTION_CHECKPOINT_SCHEMA_VERSION if production else CHECKPOINT_SCHEMA_VERSION
        )
        checkpoint["state"] = "production_ready" if production else "shadow_ready"
        result["checkpoint"] = checkpoint
        result["state"] = "production_initial_checkpoint_ready" if production else "initial_checkpoint_ready"
    boundary = dict(_mapping(result.get("boundary")))
    boundary["production_archive_write"] = False
    boundary["shadow_database_authority"] = not production
    result["boundary"] = boundary
    return result


def build_initial_posterior_checkpoint_for_mode(
    *,
    initial_mode: str,
    target_identity: str,
    event_date: str,
    station_id: str,
    decision_as_of: str,
    prior_context: Mapping[str, Any],
    joint_weak_likelihood: Mapping[str, Any],
    active_strong_lr_artifact: Mapping[str, Any],
    resolution_contract: Mapping[str, Any],
    source_reliability_initial_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Build P0 from prior and weak forecast evidence, before any D0 event."""

    mode = _text(initial_mode)
    if mode not in {"shadow", "production"}:
        raise ValueError("initial_posterior_mode_must_be_shadow_or_production")
    issues: list[str] = []
    target = _text(target_identity)
    day = _text(event_date)[:10]
    station = _text(station_id).upper()
    as_of = _parse_time(decision_as_of)
    if not target:
        issues.append("initial_target_identity_missing")
    if len(day) != 10:
        issues.append("initial_event_date_invalid")
    if not station:
        issues.append("initial_station_id_missing")
    if as_of is None:
        issues.append("initial_decision_as_of_invalid")
    if not isinstance(source_reliability_initial_state, Mapping):
        issues.append("initial_source_reliability_state_invalid")
    elif _forbidden_source_reliability_surface(source_reliability_initial_state):
        issues.append("initial_source_reliability_contains_forbidden_runtime_surface")
    prior_rows, latent_by_temperature, prior_issues = _prior_components(prior_context)
    weak_rows, weak_issues = _weak_components(joint_weak_likelihood)
    issues.extend(prior_issues)
    issues.extend(weak_issues)
    if [temperature for temperature, _mass in prior_rows] != [temperature for temperature, _mass in weak_rows]:
        issues.append("initial_prior_weak_temperature_grid_mismatch")
    strong_contract = validate_strong_observation_lr_runtime_artifact(active_strong_lr_artifact)
    if strong_contract.get("ok") is not True:
        issues.extend(f"initial_strong_artifact_contract:{item}" for item in strong_contract.get("issues") or ["invalid"])
    strong_artifact_sha = _text(_mapping(active_strong_lr_artifact).get("model_artifact_sha256"))
    if not strong_artifact_sha:
        issues.append("initial_strong_artifact_sha256_missing")
    initial_states = {state for state_by_temperature in latent_by_temperature.values() for state in state_by_temperature}
    artifact_transitions = _mapping(_mapping(active_strong_lr_artifact).get("post_state_transition"))
    unsupported_initial_states = sorted(state for state in initial_states if not _mapping(artifact_transitions.get(state)))
    if unsupported_initial_states:
        issues.append("initial_strong_artifact_transition_missing_initial_state")
    if issues:
        return _apply_initial_mode(_blocked(*issues), initial_mode=mode)

    combined: list[dict[str, Any]] = []
    for (temperature, prior_mass), (_weak_temperature, likelihood) in zip(prior_rows, weak_rows):
        for state, state_probability in latent_by_temperature[temperature].items():
            combined.append(
                {
                    "temperature_c": temperature,
                    "state": state,
                    "probability": prior_mass * likelihood * state_probability,
                }
            )
    total = sum(float(row["probability"]) for row in combined)
    if total <= EPSILON or not math.isfinite(total):
        return _apply_initial_mode(
            _blocked("initial_prior_weak_product_zero_or_non_finite"), initial_mode=mode
        )
    joint = [
        {**row, "probability": float(row["probability"]) / total}
        for row in sorted(combined, key=lambda row: (float(row["temperature_c"]), _text(row["state"])))
    ]
    joint_hash = _sha256(joint)
    physical_distribution: dict[str, float] = {}
    state_distribution: dict[str, float] = {}
    for row in joint:
        temperature_key = f"{float(row['temperature_c']):g}"
        physical_distribution[temperature_key] = physical_distribution.get(temperature_key, 0.0) + float(row["probability"])
        state_distribution[_text(row["state"])] = state_distribution.get(_text(row["state"]), 0.0) + float(row["probability"])
    contract = _mapping(resolution_contract)
    if contract.get("bridge_closed") is True and contract.get("validated") is True:
        bridge = build_resolution_bridge(
            physical_high_distribution=physical_distribution,
            resolution_contract=resolution_contract,
        )
        if bridge.get("ok") is not True or bridge.get("state") != "available" or bridge.get("bridge_closed") is not True:
            return _apply_initial_mode(
                _blocked(*(bridge.get("block_reasons") or ["initial_closed_resolution_bridge_unavailable"])),
                initial_mode=mode,
            )
    else:
        bridge = build_physical_only_resolution_bridge(
            physical_high_distribution=physical_distribution,
            resolution_contract=resolution_contract,
        )
        if bridge.get("ok") is not True or bridge.get("state") != PHYSICAL_ONLY_STATE:
            return _apply_initial_mode(
                _blocked(*(bridge.get("block_reasons") or ["initial_physical_only_resolution_projection_unavailable"])),
                initial_mode=mode,
            )
    contract_id = _text(bridge.get("resolution_contract_id"))
    if not contract_id:
        return _apply_initial_mode(_blocked("initial_resolution_contract_id_missing"), initial_mode=mode)
    trajectory = build_empty_pre_event_temperature_trajectory_feature_state(
        target_identity=target,
        station_id=station,
        decision_as_of=_text(decision_as_of),
    )
    if trajectory.get("ok") is not True:
        return _apply_initial_mode(
            _blocked(*(trajectory.get("block_reasons") or ["initial_empty_trajectory_unavailable"])),
            initial_mode=mode,
        )
    lineage = {
        "schema": PRODUCTION_INITIAL_LINEAGE_SCHEMA if mode == "production" else INITIAL_LINEAGE_SCHEMA,
        "prior_context_id": _text(prior_context.get("prior_context_id")),
        "prior_artifact_sha256": _text(prior_context.get("artifact_sha256")),
        "prior_model_artifact_sha256": _text(prior_context.get("prior_model_artifact_sha256")),
        "prior_distribution_sha256": _sha256(prior_rows),
        "weak_likelihood_artifact_sha256": _text(joint_weak_likelihood.get("artifact_sha256")),
        "weak_likelihood_sha256": _text(joint_weak_likelihood.get("joint_forecast_weak_likelihood_sha256")) or _sha256(weak_rows),
        "forecast_evidence_snapshot_id": _text(joint_weak_likelihood.get("forecast_evidence_snapshot_id")),
        "forecast_peak_timing_prior": dict(
            _mapping(joint_weak_likelihood.get("forecast_peak_timing_prior"))
        ),
        "strong_lr_artifact_sha256": strong_artifact_sha,
        "source_reliability_initial_state_sha256": _sha256(dict(source_reliability_initial_state)),
        "resolution_contract_id": contract_id,
        "resolution_contract_sha256": _text(bridge.get("contract_sha256")),
        "market_bucket_projection_state": _text(bridge.get("state")),
        "market_bucket_probabilities_na_reason": _text(bridge.get("market_bucket_probabilities_na_reason")),
        "joint_posterior_sha256": joint_hash,
        "decision_as_of": _text(decision_as_of),
        "canonical_event_count": 0,
    }
    evidence_sha = _sha256(lineage)
    fact_snapshot_id = build_fact_snapshot_id(
        slug=target,
        event_date=day,
        fact_as_of=_text(decision_as_of),
        source_event_ids=(),
        identity_salt={"initial_evidence_sha256": evidence_sha},
    )
    posterior_run_id = build_posterior_run_id(
        slug=target,
        event_date=day,
        fact_snapshot_id=fact_snapshot_id,
        source_event_ids=(),
        posterior_hash=joint_hash,
        phase=f"{mode}_initial_prior_weak",
    )
    checkpoint_id = build_posterior_checkpoint_id(
        target_identity=target,
        posterior_run_id=posterior_run_id,
        replay_generation=0,
        joint_posterior_sha256=joint_hash,
    )
    checkpoint = {
        "schema": CHECKPOINT_SCHEMA_VERSION,
        "ok": True,
        "posterior_checkpoint_id": checkpoint_id,
        "target_identity": target,
        "event_date": day,
        "posterior_run_id": posterior_run_id,
        "previous_posterior_run_id": "",
        "previous_checkpoint_id": "",
        "checkpoint_kind": "initial",
        "last_strong_update_id": "",
        "last_canonical_event_id": "",
        "joint_posterior": joint,
        "joint_posterior_sha256": joint_hash,
        "physical_high_distribution": dict(sorted(physical_distribution.items(), key=lambda item: float(item[0]))),
        "market_bucket_projection_state": _text(bridge.get("state")),
        "market_bucket_probabilities_na_reason": _text(bridge.get("market_bucket_probabilities_na_reason")),
        "latent_post_state_distribution": dict(sorted(state_distribution.items())),
        "observed_max_lower_bound_c": None,
        "consumed_event_ids": [],
        "source_event_ids": [],
        "consumed_ancestor_sha256": _sha256([]),
        "forecast_evidence_snapshot_id": lineage["forecast_evidence_snapshot_id"],
        "prior_context_id": lineage["prior_context_id"],
        "resolution_contract_id": contract_id,
        "model_artifact_sha256": strong_artifact_sha,
        "replay_generation": 0,
        "state": "shadow_ready",
        "created_at": _text(decision_as_of),
        "committed_at": _text(decision_as_of),
        "initial_evidence_snapshot_id": fact_snapshot_id,
        "initial_evidence_sha256": evidence_sha,
    }
    return _apply_initial_mode({
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "initial_checkpoint_ready",
        "checkpoint": checkpoint,
        "resolution_bridge": bridge,
        "trajectory_feature_state": trajectory,
        "source_reliability_state": dict(source_reliability_initial_state),
        "initial_lineage": {**lineage, "initial_evidence_sha256": evidence_sha, "initial_evidence_snapshot_id": fact_snapshot_id},
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "formula": "normalize(prior_temperature_mass*joint_forecast_weak_likelihood*prior_latent_state_given_temperature)",
            "consumes_d0_observation": False,
            "consumes_market_or_strategy": False,
            "uses_legacy_v31_output": False,
            "strict_asof_oos_runtime_gate": False,
            "writes_archive_db": False,
            "places_orders": False,
        },
    }, initial_mode=mode)


def build_initial_posterior_checkpoint(
    *,
    target_identity: str,
    event_date: str,
    station_id: str,
    decision_as_of: str,
    prior_context: Mapping[str, Any],
    joint_weak_likelihood: Mapping[str, Any],
    active_strong_lr_artifact: Mapping[str, Any],
    resolution_contract: Mapping[str, Any],
    source_reliability_initial_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Backward-compatible shadow P0 entrypoint."""

    return build_initial_posterior_checkpoint_for_mode(
        initial_mode="shadow",
        target_identity=target_identity,
        event_date=event_date,
        station_id=station_id,
        decision_as_of=decision_as_of,
        prior_context=prior_context,
        joint_weak_likelihood=joint_weak_likelihood,
        active_strong_lr_artifact=active_strong_lr_artifact,
        resolution_contract=resolution_contract,
        source_reliability_initial_state=source_reliability_initial_state,
    )


def initialize_initial_posterior_lineage_shadow_schema(conn: sqlite3.Connection) -> None:
    """Create only the isolated append-only lineage table; no runtime row."""

    _assert_isolated_shadow_db(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {INITIAL_LINEAGE_TABLE} (
            posterior_checkpoint_id TEXT PRIMARY KEY,
            target_identity TEXT NOT NULL,
            posterior_run_id TEXT NOT NULL UNIQUE,
            prior_context_id TEXT NOT NULL,
            prior_artifact_sha256 TEXT NOT NULL,
            weak_likelihood_artifact_sha256 TEXT NOT NULL,
            weak_likelihood_sha256 TEXT NOT NULL,
            forecast_evidence_snapshot_id TEXT NOT NULL,
            strong_lr_artifact_sha256 TEXT NOT NULL,
            source_reliability_initial_state_sha256 TEXT NOT NULL,
            resolution_contract_id TEXT NOT NULL,
            initial_evidence_sha256 TEXT NOT NULL UNIQUE,
            lineage_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    columns = {
        _text(row[1])
        for row in conn.execute(f"PRAGMA table_info({INITIAL_LINEAGE_TABLE})").fetchall()
    }
    if "source_reliability_initial_state_sha256" not in columns:
        # This table exists only in the isolated shadow database.  The
        # additive column is needed to make the first strong-LR pre-state part
        # of P0 identity even for a schema initialized one revision earlier.
        conn.execute(
            f"ALTER TABLE {INITIAL_LINEAGE_TABLE} "
            "ADD COLUMN source_reliability_initial_state_sha256 TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {INITIAL_LINEAGE_TABLE}_no_update
        BEFORE UPDATE ON {INITIAL_LINEAGE_TABLE}
        BEGIN SELECT RAISE(ABORT, 'initial_posterior_lineage_append_only'); END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {INITIAL_LINEAGE_TABLE}_no_delete
        BEFORE DELETE ON {INITIAL_LINEAGE_TABLE}
        BEGIN SELECT RAISE(ABORT, 'initial_posterior_lineage_append_only'); END
        """
    )


def persist_initial_posterior_checkpoint_shadow(
    conn: sqlite3.Connection,
    *,
    initial_checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically append P0 plus lineage, refusing predecessor forks."""

    _assert_isolated_shadow_db(conn)
    if not _table_exists(conn, INITIAL_LINEAGE_TABLE):
        raise ShadowTransactionIntegrityError("initial_checkpoint_lineage_table_missing")
    value = _mapping(initial_checkpoint)
    if value.get("schema") != SCHEMA_VERSION or value.get("ok") is not True:
        raise ValueError("initial_checkpoint_not_ready")
    checkpoint = _mapping(value.get("checkpoint"))
    lineage = _mapping(value.get("initial_lineage"))
    bridge = _mapping(value.get("resolution_bridge"))
    trajectory = _mapping(value.get("trajectory_feature_state"))
    reliability = _mapping(value.get("source_reliability_state"))
    if _text(checkpoint.get("checkpoint_kind")) != "initial" or checkpoint.get("consumed_event_ids") != []:
        raise ValueError("initial_checkpoint_kind_or_consumed_events_invalid")
    if _text(lineage.get("joint_posterior_sha256")) != _text(checkpoint.get("joint_posterior_sha256")):
        raise ValueError("initial_checkpoint_lineage_posterior_binding_mismatch")
    if _text(lineage.get("resolution_contract_id")) != _text(checkpoint.get("resolution_contract_id")):
        raise ValueError("initial_checkpoint_lineage_resolution_binding_mismatch")
    if _text(lineage.get("strong_lr_artifact_sha256")) != _text(checkpoint.get("model_artifact_sha256")):
        raise ValueError("initial_checkpoint_lineage_strong_artifact_binding_mismatch")
    if _text(lineage.get("source_reliability_initial_state_sha256")) != _sha256(reliability):
        raise ValueError("initial_checkpoint_lineage_source_reliability_binding_mismatch")
    assert_active_strong_lr_artifact_binding(
        conn,
        target_identity=_text(checkpoint.get("target_identity")),
        artifact_sha256=_text(checkpoint.get("model_artifact_sha256")),
        as_of=_text(checkpoint.get("created_at")),
    )
    checkpoint_row = materialize_checkpoint_row(
        checkpoint,
        resolution_bridge=bridge,
        trajectory_feature_state=trajectory,
        source_reliability_state=reliability,
    )
    target = _text(checkpoint_row.get("target_identity"))
    checkpoint_id = _text(checkpoint_row.get("posterior_checkpoint_id"))
    existing_target = conn.execute(
        "SELECT posterior_checkpoint_id FROM posterior_checkpoints WHERE target_identity=? ORDER BY replay_generation,rowid",
        (target,),
    ).fetchall()
    if existing_target and any(_text(row[0]) != checkpoint_id for row in existing_target):
        raise ShadowTransactionIntegrityError("initial_checkpoint_target_already_has_different_checkpoint")
    existing_checkpoint = conn.execute(
        "SELECT posterior_run_id,joint_posterior_sha256 FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
        (checkpoint_id,),
    ).fetchone()
    existing_lineage = conn.execute(
        f"SELECT lineage_json FROM {INITIAL_LINEAGE_TABLE} WHERE posterior_checkpoint_id=?",
        (checkpoint_id,),
    ).fetchone()
    if existing_checkpoint is not None or existing_lineage is not None:
        if existing_checkpoint is None or existing_lineage is None:
            raise ShadowTransactionIntegrityError("initial_checkpoint_partial_state_detected")
        if (
            _text(existing_checkpoint[0]) != _text(checkpoint_row.get("posterior_run_id"))
            or _text(existing_checkpoint[1]) != _text(checkpoint_row.get("joint_posterior_sha256"))
            or _text(existing_lineage[0]) != _canonical_json(lineage)
        ):
            raise ShadowTransactionIntegrityError("initial_checkpoint_existing_identity_or_lineage_drift")
        return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": True, "shadow_only": True, "posterior_checkpoint_id": checkpoint_id}
    lineage_row = {
        "posterior_checkpoint_id": checkpoint_id,
        "target_identity": target,
        "posterior_run_id": _text(checkpoint_row.get("posterior_run_id")),
        "prior_context_id": _text(lineage.get("prior_context_id")),
        "prior_artifact_sha256": _text(lineage.get("prior_artifact_sha256")),
        "weak_likelihood_artifact_sha256": _text(lineage.get("weak_likelihood_artifact_sha256")),
        "weak_likelihood_sha256": _text(lineage.get("weak_likelihood_sha256")),
        "forecast_evidence_snapshot_id": _text(lineage.get("forecast_evidence_snapshot_id")),
        "strong_lr_artifact_sha256": _text(lineage.get("strong_lr_artifact_sha256")),
        "source_reliability_initial_state_sha256": _text(lineage.get("source_reliability_initial_state_sha256")),
        "resolution_contract_id": _text(lineage.get("resolution_contract_id")),
        "initial_evidence_sha256": _text(lineage.get("initial_evidence_sha256")),
        "lineage_json": _canonical_json(lineage),
        "created_at": _text(checkpoint_row.get("committed_at")),
    }
    if any(not _text(value) for value in lineage_row.values()):
        raise ValueError("initial_checkpoint_lineage_fields_missing")
    conn.execute("SAVEPOINT initial_posterior_checkpoint_shadow")
    try:
        fields = list(checkpoint_row)
        conn.execute(
            f"INSERT INTO posterior_checkpoints({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            tuple(checkpoint_row[field] for field in fields),
        )
        fields = list(lineage_row)
        conn.execute(
            f"INSERT INTO {INITIAL_LINEAGE_TABLE}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            tuple(lineage_row[field] for field in fields),
        )
    except Exception:
        conn.execute("ROLLBACK TO initial_posterior_checkpoint_shadow")
        conn.execute("RELEASE initial_posterior_checkpoint_shadow")
        raise
    conn.execute("RELEASE initial_posterior_checkpoint_shadow")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "idempotent": False,
        "shadow_only": True,
        "posterior_checkpoint_id": checkpoint_id,
        "posterior_run_id": _text(checkpoint_row.get("posterior_run_id")),
        "initial_evidence_sha256": _text(lineage.get("initial_evidence_sha256")),
        "boundary": {"writes_strong_update": False, "writes_realization": False, "writes_outbox": False, "places_orders": False},
    }


__all__ = [
    "INITIAL_LINEAGE_REQUIRED_COLUMNS",
    "INITIAL_LINEAGE_TABLE",
    "SCHEMA_VERSION",
    "build_initial_posterior_checkpoint",
    "build_initial_posterior_checkpoint_for_mode",
    "initialize_initial_posterior_lineage_shadow_schema",
    "persist_initial_posterior_checkpoint_shadow",
]
