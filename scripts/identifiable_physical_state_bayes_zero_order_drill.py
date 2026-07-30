#!/usr/bin/env python3
"""Exercise the physical composer through P0, P1, weak revision and basket P(B).

The drill uses an in-memory clone of the production schema and in-memory
approval copies of candidate model artifacts.  It never edits activation,
archive.db, strategy state, orders or CLOB.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from identifiable_physical_state_bayes.core import canonical_sha256
from basket_strategy.probability_adapter import extract_probability_projection
from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    IDENTIFIABLE_PHYSICAL_FORMULA_ID,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION as ACTIVATION_SCHEMA,
    STRONG_UPDATE_SCHEMA,
    activation_sha256,
    validate_v6_activation,
)
from production_probability_v6_event_runtime import compose_v6_canonical_event
from production_probability_v6_initial_writer import (
    commit_v6_initial_posterior,
    materialize_v6_initial_public_fact_row,
)
from production_probability_v6_market_inputs_runtime import prewarm_v6_market_inputs
from production_probability_v6_p0_input_runtime import build_v6_production_p0_inputs
from production_probability_v6_p0_runtime import compile_v6_p0_candidate
from production_probability_v6_public_ssot_writer import (
    V6TransactionConflict,
    V6TransactionIntegrityError,
    _insert,
)
from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256
from production_probability_v6_transaction_commit_runtime import (
    commit_v6_candidate_transaction,
)
from production_probability_v6_weak_revision_runtime import (
    compile_v6_weak_revision_candidate,
)
from production_probability_v6_weak_revision_writer import (
    commit_v6_weak_revision_transaction,
    materialize_v6_weak_revision_public_fact_row,
)
from scripts.production_probability_v6_zero_order_drill import _clone_schema
from zuuu_metar_reporting_schedule import expected_regular_metar_slots_utc


SCHEMA_VERSION = "identifiable_physical_state_bayes_zero_order_drill_v1"
DAY = "2026-07-17"
TARGET = f"physical_weather_day_v1:ZUUU:{DAY}"


def _json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _sha(value) -> str:
    return canonical_sha256(value)


def _approved_artifact(relative: str) -> dict:
    value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    value.pop("artifact_sha256", None)
    value.update(
        {
            "state": "approved",
            "production_runtime_eligible": True,
            "shadow_only": False,
        }
    )
    value["artifact_sha256"] = canonical_sha256(value)
    return value


def _activation(
    state_model: dict,
    observation: dict,
    reporting: dict,
    *,
    prior_model_artifact_sha256: str,
) -> dict:
    value = {
        "schema": ACTIVATION_SCHEMA,
        "state": "production_enabled",
        "bayes_formula_id": IDENTIFIABLE_PHYSICAL_FORMULA_ID,
        "composer_schema": IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "v5_fallback_forbidden": True,
        "pre_event_predict_does_not_consume_current_observation": True,
        "no_post_emission_state_transition": True,
        "nwp_hourly_curve_not_reapplied_as_independent_likelihood": True,
        "runtime_manifest_sha256": v6_runtime_manifest_sha256(),
        "prior_model_artifact_sha256": prior_model_artifact_sha256,
        "weak_likelihood_artifact_sha256": "3" * 64,
        "physical_state_model_artifact_sha256": state_model["artifact_sha256"],
        "physical_observation_artifact_sha256": observation["artifact_sha256"],
        "physical_reporting_operator_sha256": reporting["artifact_sha256"],
        "physical_state_vector": [
            "latent_temperature",
            "specific_humidity",
            "net_temperature_tendency",
            "surface_wetness_memory",
            "cold_pool_memory",
            "regime_state",
        ],
        "frozen_reporting_operator_enabled": True,
        "current_event_joint_consumption_count": 1,
        "production_transaction_b_authorized": True,
        "places_orders": False,
        "authorizes_clob": False,
    }
    value["activation_contract_sha256"] = activation_sha256(value)
    return validate_v6_activation(value)


def _weak(*, generation: str, warm_weight: float, warm_shift_c: float) -> dict:
    slots = expected_regular_metar_slots_utc(DAY)
    start, end = slots[0], slots[-1]
    return {
        "artifact_sha256": "3" * 64,
        "forecast_scenario_paths": {
            "sha256": generation,
            "paths": [
                {
                    "scenario_id": "cool",
                    "source_mode_probability": 1.0 - warm_weight,
                    "forecast_weather_regime": "wet_precipitation",
                    "hourly_curve": [
                        {
                            "valid_time_utc": start,
                            "temperature_c": 27.0 + warm_shift_c,
                            "dewpoint_c": 24.0,
                            "precipitation_probability": 0.65,
                            "cloud_suppression_probability": 0.8,
                        },
                        {
                            "valid_time_utc": end,
                            "temperature_c": 29.0 + warm_shift_c,
                            "dewpoint_c": 24.0,
                            "precipitation_probability": 0.35,
                            "cloud_suppression_probability": 0.6,
                        },
                    ],
                },
                {
                    "scenario_id": "warm",
                    "source_mode_probability": warm_weight,
                    "forecast_weather_regime": "clear_dry",
                    "hourly_curve": [
                        {
                            "valid_time_utc": start,
                            "temperature_c": 28.0 + warm_shift_c,
                            "dewpoint_c": 21.0,
                            "precipitation_probability": 0.05,
                            "cloud_suppression_probability": 0.15,
                        },
                        {
                            "valid_time_utc": end,
                            "temperature_c": 35.0 + warm_shift_c,
                            "dewpoint_c": 21.0,
                            "precipitation_probability": 0.05,
                            "cloud_suppression_probability": 0.15,
                        },
                    ],
                },
            ],
        },
        "physical_scenario_prior_weights": {
            "cool": 1.0 - warm_weight,
            "warm": warm_weight,
        },
        "candidate_scenario_responsibilities": {},
    }


def _snapshot(*, identity: str, weak: dict, decision_as_of: str) -> dict:
    source_revision = f"taf-{identity}"
    paths = weak["forecast_scenario_paths"]
    return {
        "forecast_evidence_snapshot_id": identity,
        "target_identity": TARGET,
        "event_date": DAY,
        "decision_as_of": decision_as_of,
        "weak_likelihood_artifact_sha256": "3" * 64,
        "weak_likelihood_sha256": _sha(
            {"identity": identity, "generation": paths["sha256"]}
        ),
        "source_set_key": "physical-zero-order",
        "source_revision_ids_json": _json([source_revision]),
        "source_revisions_json": _json(
            [
                {
                    "source_id": "zuuu_taf_forecast_amsc",
                    "source_revision_id": source_revision,
                    "available_at": decision_as_of,
                }
            ]
        ),
        "temperature_grid_json": "[]",
        "likelihood_by_x_json": "{}",
        "forecast_peak_timing_prior_json": "{}",
        "forecast_background_context_json": "{}",
        "source_expert_trace_json": "[]",
        "forecast_multimodality_trace_json": "{}",
        "weak_model_diagnostics_json": "{}",
        "snapshot_json": "{}",
        "snapshot_sha256": _sha({"snapshot": identity}),
        "state": "active",
        "created_at": decision_as_of,
        "forecast_scenario_paths_json": _json(paths),
        "forecast_scenario_paths_sha256": paths["sha256"],
        "candidate_scenario_responsibilities_json": "{}",
        "candidate_scenario_responsibilities_sha256": _sha({}),
        "prior_context_audit_json": "{}",
    }


def _event(valid_at: str) -> dict:
    return {
        "canonical_observation_event_id": "physical-zero-order-event",
        "canonical_observation_key": f"ZUUU|METAR|{valid_at}",
        "target_identity": TARGET,
        "station_id": "ZUUU",
        "report_type": "METAR",
        "valid_time": valid_at,
        "revision_no": 0,
        "normalized_physical_sha256": "e" * 64,
        "selected_source_observation_id": "source-physical-zero-order",
        "selected_source_id": "zuuu_metar_raw_amsc",
        "selection_role": "primary",
        "selection_policy_version": "physical-zero-order-v1",
        "first_transport_seen_at": valid_at,
        "first_actionable_seen_at": valid_at,
        "accepted_at": valid_at,
        "state": "accepted_primary",
        "supersedes_event_id": None,
        "conflict_group_id": None,
        "parsed_observation_json": _json(
            {
                "temperature_c": 30.0,
                "dewpoint_c": 23.0,
                "cloud_text": "BKN020",
                "weather_text": "-RA",
                "qnh_hpa": 1000.0,
            }
        ),
        "quality_flags_json": "[]",
        "created_at": valid_at,
    }


def main() -> int:
    state_model = _approved_artifact(
        "config/artifacts/identifiable_physical_state_model_v1.candidate.json"
    )
    observation = _approved_artifact(
        "config/artifacts/identifiable_physical_observation_v1.candidate.json"
    )
    reporting = _approved_artifact(
        "config/artifacts/amsc_frozen_reporting_operator_v1.candidate.json"
    )
    market_inputs = prewarm_v6_market_inputs(
        event_date=DAY,
        root=ROOT,
        strategy_projection_role="basket_probability_only",
    )
    if market_inputs.get("market_projection_available") is not True:
        raise RuntimeError("physical_zero_order_market_inputs_unavailable")
    physical_contract = market_inputs["physical_resolution_contract"]
    slug = str(
        market_inputs["market_context_result"]["market_context"].get("slug") or ""
    )
    old_weak = _weak(generation="a" * 64, warm_weight=0.55, warm_shift_c=0.0)
    new_weak = _weak(generation="b" * 64, warm_weight=0.7, warm_shift_c=0.5)
    slots = expected_regular_metar_slots_utc(DAY)
    state_reference = slots[0]
    production_p0_inputs = build_v6_production_p0_inputs(
        event_date=DAY,
        decision_as_of=state_reference,
    )
    if production_p0_inputs.get("ok") is not True:
        raise RuntimeError("physical_zero_order_production_p0_inputs_unavailable")
    physical_prior = production_p0_inputs.get("prior_context") or {}
    if (
        physical_prior.get("schema") != "full_physical_p0_prior_context_v1"
        or physical_prior.get("state") != "available"
        or physical_prior.get("production_runtime_eligible") is not True
        or physical_prior.get("shadow_only") is not False
    ):
        raise RuntimeError("physical_zero_order_production_prior_not_bound")
    prior_model_sha = str(physical_prior.get("prior_model_artifact_sha256") or "")
    activation = _activation(
        state_model,
        observation,
        reporting,
        prior_model_artifact_sha256=prior_model_sha,
    )
    event_valid_at = slots[4]
    revision_as_of = slots[4].replace(":00:00Z", ":30:00Z")
    old_snapshot = _snapshot(
        identity="physical-zero-order-weak-old",
        weak=old_weak,
        decision_as_of=state_reference,
    )
    new_snapshot = _snapshot(
        identity="physical-zero-order-weak-new",
        weak=new_weak,
        decision_as_of=revision_as_of,
    )
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity=TARGET,
        event_date=DAY,
        decision_as_of=state_reference,
        state_reference_valid_at=state_reference,
        prior_context_id=str(physical_prior.get("prior_context_id") or ""),
        prior_model_artifact_sha256=prior_model_sha,
        forecast_evidence_snapshot_id=old_snapshot["forecast_evidence_snapshot_id"],
        weak_likelihood_artifact_sha256="3" * 64,
        resolution_contract_id=physical_contract["resolution_contract_id"],
        forcing_generation_sha256="a" * 64,
        initial_joint=(),
        candidate_scenario_responsibilities={},
        replay_generation=0,
        prior_context=physical_prior,
        weak_likelihood=old_weak,
        physical_state_model_artifact=state_model,
        physical_reporting_operator_artifact=reporting,
    )
    p0_public = materialize_v6_initial_public_fact_row(
        activation=activation,
        checkpoint_row=p0["checkpoint_row"],
        fact_snapshot_id="physical-zero-order-p0-fact",
        slug=slug,
        event_date=DAY,
        fact_as_of=state_reference,
        correlation_id="physical-zero-order-p0",
        market_inputs=market_inputs,
    )
    conn = _clone_schema(ROOT / "data/archive.db")
    order_tables = (
        "auto_orders",
        "posterior_commit_outbox",
        "posterior_outbox_execution_contracts",
    )
    counts_before = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in order_tables
    }
    commit_v6_initial_posterior(
        conn,
        activation=activation,
        p0_candidate=p0,
        weak_snapshot_row=old_snapshot,
        public_fact_row=p0_public,
    )
    conn.row_factory = None
    predecessor = dict(
        zip(
            [row[1] for row in conn.execute("PRAGMA table_info(posterior_checkpoints)")],
            conn.execute(
                "SELECT * FROM posterior_checkpoints ORDER BY rowid DESC LIMIT 1"
            ).fetchone(),
        )
    )
    event = _event(event_valid_at)
    event["is_contract_report_slot"] = True
    event["parsed_observation"] = json.loads(event["parsed_observation_json"])
    candidate = compose_v6_canonical_event(
        activation=activation,
        previous_checkpoint=predecessor,
        canonical_event=event,
        pre_event_history=[],
        weak_likelihood=old_weak,
        transition_artifact={},
        observation_artifact={},
        decision_as_of=event_valid_at,
        completed_at=event_valid_at,
        physical_state_model_artifact=state_model,
        physical_observation_artifact=observation,
        physical_reporting_operator_artifact=reporting,
    )
    _insert(
        conn,
        "canonical_observation_events",
        {key: value for key, value in event.items() if key not in {"parsed_observation", "is_contract_report_slot"}},
    )
    conn.commit()
    p1_commit = commit_v6_candidate_transaction(
        conn,
        activation=activation,
        previous_checkpoint=predecessor,
        candidate=candidate,
        canonical_observation_event=event,
        market_inputs=market_inputs,
        fact_snapshot_id="physical-zero-order-p1-fact",
        slug=slug,
        event_date=DAY,
        fact_as_of=event_valid_at,
        correlation_id="physical-zero-order-p1",
    )
    columns = [row[1] for row in conn.execute("PRAGMA table_info(posterior_checkpoints)")]
    p1_checkpoint = dict(
        zip(
            columns,
            conn.execute(
                "SELECT * FROM posterior_checkpoints ORDER BY rowid DESC LIMIT 1"
            ).fetchone(),
        )
    )
    weak_candidate = compile_v6_weak_revision_candidate(
        activation=activation,
        previous_checkpoint=p1_checkpoint,
        previous_snapshot=old_snapshot,
        revised_snapshot=new_snapshot,
        trigger_source_id="zuuu_taf_forecast_amsc",
        trigger_source_revision_id="taf-physical-zero-order-weak-new",
        revision_kind="amendment",
        decision_as_of=revision_as_of,
        previous_weak_likelihood=old_weak,
        revised_weak_likelihood=new_weak,
        physical_state_model_artifact=state_model,
        physical_reporting_operator_artifact=reporting,
    )
    latest_fact_json = conn.execute(
        "SELECT h.fact_json FROM event_fact_latest l "
        "JOIN event_fact_history h ON h.id=l.fact_id WHERE l.slug=? AND l.event_date=?",
        (slug, DAY),
    ).fetchone()[0]
    weak_public = materialize_v6_weak_revision_public_fact_row(
        activation=activation,
        previous_fact_json=latest_fact_json,
        checkpoint_row=weak_candidate["checkpoint_row"],
        update_row=weak_candidate["update_row"],
        fact_snapshot_id="physical-zero-order-weak-fact",
        slug=slug,
        event_date=DAY,
        fact_as_of=revision_as_of,
        market_inputs=market_inputs,
    )
    weak_commit = commit_v6_weak_revision_transaction(
        conn,
        activation=activation,
        candidate=weak_candidate,
        public_fact_row=weak_public,
    )
    weak_retry = commit_v6_weak_revision_transaction(
        conn,
        activation=activation,
        candidate=weak_candidate,
        public_fact_row=weak_public,
    )
    cas_conflict = False
    try:
        commit_v6_candidate_transaction(
            conn,
            activation=activation,
            previous_checkpoint=predecessor,
            candidate=candidate,
            canonical_observation_event=event,
            market_inputs=market_inputs,
            fact_snapshot_id="physical-zero-order-stale-fact",
            slug=slug,
            event_date=DAY,
            fact_as_of=event_valid_at,
            correlation_id="physical-zero-order-stale",
        )
    except (V6TransactionConflict, V6TransactionIntegrityError):
        cas_conflict = True
    counts_after = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in order_tables
    }
    latest_ssot = json.loads(
        conn.execute(
            "SELECT h.fact_json FROM event_fact_latest l "
            "JOIN event_fact_history h ON h.id=l.fact_id WHERE l.slug=? AND l.event_date=?",
            (slug, DAY),
        ).fetchone()[0]
    )["posterior_ssot_fact"]
    probabilities = latest_ssot.get("market_bucket_probabilities") or {}
    basket_projection = extract_probability_projection(
        {"probability_projection": latest_ssot}
    )
    gates = {
        "p0_committed": True,
        "production_bound_physical_temperature_prior": True,
        "p1_transaction_b_committed": p1_commit.get("ok") is True,
        "weak_driver_revision_committed": weak_commit.get("ok") is True,
        "weak_revision_idempotent_retry": weak_retry.get("idempotent") is True,
        "stale_checkpoint_cas_fail_closed": cas_conflict,
        "basket_probability_materialized": bool(probabilities)
        and abs(sum(float(value) for value in probabilities.values()) - 1.0) <= 1e-8,
        "basket_projection_only": latest_ssot.get("strategy_projection_role")
        == "basket_probability_only",
        "basket_adapter_consumes_latest_as_p1": basket_projection.get("status")
        == "available"
        and basket_projection.get("stage") == "P1",
        "no_realization_state": not str(latest_ssot.get("realization_state_id") or ""),
        "no_order_or_outbox_rows_created": counts_before == counts_after,
        "current_observation_consumed_once": candidate.get(
            "current_observation_consumed_once"
        )
        is True,
        "weak_revision_not_likelihood": weak_candidate.get(
            "forecast_revision_used_as_likelihood"
        )
        is False,
    }
    result = {
        "schema": SCHEMA_VERSION,
        "ok": all(gates.values()),
        "state": (
            "identifiable_physical_zero_order_drill_accepted"
            if all(gates.values())
            else "identifiable_physical_zero_order_drill_blocked"
        ),
        "activation_contract_sha256": activation["activation_contract_sha256"],
        "runtime_manifest_sha256": activation["runtime_manifest_sha256"],
        "candidate_artifacts_promoted_in_memory_only": True,
        "writes_archive_db": False,
        "uses_in_memory_clone_of_production_schema": True,
        "gates": gates,
        "order_counts_before": counts_before,
        "order_counts_after": counts_after,
        "places_orders": False,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
