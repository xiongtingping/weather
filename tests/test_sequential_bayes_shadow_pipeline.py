from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from canonical_observation_runtime import canonicalize_observations
from canonical_observation_writer import persist_canonical_observation_proposal
from dynamic_three_leg_shadow_decision_writer import (
    build_isolated_dynamic_three_leg_shadow_decision_candidate,
    claim_isolated_posterior_commit_outbox,
    commit_isolated_dynamic_three_leg_shadow_decisions,
    initialize_isolated_dynamic_three_leg_shadow_decision_schema,
    run_isolated_dynamic_three_leg_shadow_decision_cycle,
)
from observation_fast_lane_shadow_orchestrator import run_observation_fast_lane_shadow
from realization_state_projection_runtime import POLICY_SCHEMA_VERSION
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_filter_shadow_runtime import replay_canonical_sequence
from sequential_bayes_schema_contract import apply_forward_schema
from sequential_bayes_shadow_pipeline import (
    build_canonical_event_transaction_b_candidate,
    load_canonical_observation_event,
    run_canonical_event_shadow_update,
    run_canonical_event_public_ssot_shadow_update,
)
from production_sequential_bayes_candidate_compiler import (
    compile_production_transaction_b_candidate,
)
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError
from settlement_window_runtime import build_settlement_window_state, load_approved_settlement_window_policy
from top_yes_wave_reentry_runtime import build_top_yes_wave_reentry_state
from source_observation_runtime import build_source_observation
from tests.strong_observation_artifact_fixtures import approved_strong_artifact


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"
PREVIOUS_CHECKPOINT_ID = "pchk_pipeline_previous_123456"
PREVIOUS_RUN_ID = "post_pipeline_previous_123456"
NOW = "2026-07-03T07:00:00Z"


def _settlement_window(decision_as_of: str = "2026-07-03T07:00:01Z") -> dict:
    return build_settlement_window_state(
        resolution_local_date="2026-07-03",
        decision_as_of=decision_as_of,
        policy=load_approved_settlement_window_policy(),
    )


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _previous_checkpoint() -> dict:
    joint = [
        {"temperature_c": 29.0, "state": "warming", "probability": 0.5},
        {"temperature_c": 30.0, "state": "warming", "probability": 0.5},
    ]
    return {
        "posterior_checkpoint_id": PREVIOUS_CHECKPOINT_ID,
        "posterior_run_id": PREVIOUS_RUN_ID,
        "target_identity": TARGET,
        "event_date": "2026-07-03",
        "joint_posterior": joint,
        "joint_posterior_sha256": _hash(joint),
        "observed_max_lower_bound_c": 28.5,
        "consumed_event_ids": [],
        "source_event_ids": ["srcevt_pipeline_previous_123456"],
    }


def _seed_previous_checkpoint(conn: sqlite3.Connection, previous: dict) -> None:
    conn.execute(
        """
        INSERT INTO posterior_checkpoints(
            posterior_checkpoint_id, target_identity, posterior_run_id,
            previous_checkpoint_id, checkpoint_kind, forecast_evidence_snapshot_id,
            prior_context_id, resolution_contract_id, last_strong_update_id,
            last_canonical_event_id, canonical_sequence_sha256, temperature_grid_id,
            state_grid_id, joint_posterior_json, joint_posterior_sha256,
            physical_high_distribution_json, settlement_scalar_distribution_json,
            market_bucket_distribution_json, latent_post_state_json,
            trajectory_feature_state_json, source_reliability_state_json,
            observed_max_lower_bound_c, consumed_event_ids_json,
            consumed_ancestor_sha256, model_artifact_sha256, replay_generation,
            supersedes_checkpoint_id, state, created_at, committed_at
        ) VALUES (?, ?, ?, '', 'initial', 'forecast-initial', 'prior-initial',
                  'resolution-v1', '', '', 'ancestor-initial', 'temperature-grid',
                  'state-grid', ?, ?, ?, '{}', '{}', '{}', '{}', ?, ?, '[]',
                  'ancestor-initial', 'artifact-initial', 0, '', 'shadow_ready',
                  '2026-07-03T06:00:00Z', '2026-07-03T06:00:00Z')
        """,
        (
            previous["posterior_checkpoint_id"],
            previous["target_identity"],
            previous["posterior_run_id"],
            _json(previous["joint_posterior"]),
            previous["joint_posterior_sha256"],
            _json({"29": 0.5, "30": 0.5}),
            _json({"warming": 1.0}),
            _json({}),
        ),
    )


def _canonical_event_id(conn: sqlite3.Connection) -> str:
    amsc = build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:30:00Z",
        fetched_at="2026-07-03T06:59:59Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref="fixture://amsc",
        raw_payload={"source": "amsc"},
        payload={"report_type": "METAR"},
    ).as_dict()
    awc = build_source_observation(
        source_id="zuuu_metar_raw_awc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:30:00Z",
        fetched_at="2026-07-03T07:00:00Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref="fixture://awc",
        raw_payload={"source": "awc"},
        payload={"report_type": "METAR"},
    ).as_dict()
    proposal = canonicalize_observations(
        [awc, amsc],
        target_identity=TARGET,
        event_date="2026-07-03",
        decision_as_of=NOW,
    )
    persisted = persist_canonical_observation_proposal(conn, proposal)
    assert persisted["inserted_events"] == 1
    assert persisted["inserted_relations"] == 2
    return str(proposal["events"][0]["canonical_observation_event_id"])


def _artifact() -> dict:
    return approved_strong_artifact(
        model_artifact_sha256="strong-artifact-pipeline",
        raw_log_score_by_temperature_and_pre_state={
            "29|warming": -0.4,
            "30|warming": 0.4,
        },
    )


def _resolution_contract(*, closed: bool = True) -> dict:
    return {
        "resolution_contract_id": "resolution-v1",
        "resolution_source_id": "official-source",
        "resolution_local_date": "2026-07-03",
        "settlement_unit": "C",
        "bridge_closed": closed,
        "validated": closed,
        "physical_to_settlement_kernel": {
            "29": [{"settlement_value_c": 29.0, "probability": 1.0}],
            "30": [{"settlement_value_c": 30.0, "probability": 1.0}],
        },
        "bucket_contract": {
            "29C": {"lower_inclusive": 28.5, "upper_exclusive": 29.5},
            "30C": {"lower_inclusive": 29.5, "upper_exclusive": 30.5},
        },
    }


def _realization_policy() -> dict:
    return {
        "schema": POLICY_SCHEMA_VERSION,
        "artifact_sha256": "realization-policy-pipeline",
        "approval_state": "approved",
        "credible_probability_floor": 0.05,
        "strong_support_log_probability_ratio_floor": 0.0,
        "strong_exclusion_probability_ceiling": 0.01,
        "strong_exclusion_log_probability_ratio_ceiling": -1.0,
    }


def _decision_market() -> dict:
    return {
        "market_snapshot_id": "market_pipeline_1234567890abcdef",
        "buckets": {
            "29C": {"yes_ask": 0.25, "no_ask": 0.75, "yes_token_id": "yes-29", "no_token_id": "no-29"},
            "30C": {
                "yes_ask": 0.65,
                "no_ask": 0.35,
                "yes_quote_id": "quote_pipeline_1234567890abcdef",
                "yes_token_id": "token_pipeline_1234567890abcdef",
                "no_token_id": "no-30",
                "yes_tick_size": "0.001",
                "yes_fee_schedule_id": "fee_pipeline_1234567890abcdef",
            },
        },
    }


def _decision_capital() -> dict:
    return {
        "capital_snapshot_id": "capital_pipeline_1234567890abcdef",
        "risk_snapshot_id": "risk_pipeline_1234567890abcdef",
        "capital_epoch_id": "capital_epoch_pipeline_1234567890abcdef",
        "risk_reservation_id": "risk_reservation_pipeline_1234567890abcdef",
        "available_cash_usdc": 100.0,
        "notional_by_leg": {
            "top_yes_wave": 100.0,
            "second_yes": 5.0,
            "strongly_excluded_no": 5.0,
        },
    }


def _fast_lane_topology(**overrides) -> dict:
    value = {
        "release_delay_ms": 0,
        "synchronous_forecast_refresh_on_hot_path": False,
        "generic_task_queue_on_hot_path": False,
        "resident_canonical_and_inference_worker": True,
        "transaction_b_atomic_before_intent": True,
        "posterior_commit_outbox_durable_before_intent": True,
        "decision_reads_committed_ssot": True,
        "signer_connection_pool_resident": True,
    }
    value.update(overrides)
    return value


def _commit_clock(*timestamps: str):
    values = iter(timestamps)
    return lambda: next(values)


def _fast_lane_posterior_inputs(event_id: str) -> dict:
    return {
        "public_slug": TARGET,
        "public_event_date": "2026-07-03",
        "public_fact_as_of": "2026-07-03T06:59:59.400Z",
        "shadow_only": True,
        "previous_checkpoint": _previous_checkpoint(),
        "canonical_observation_event_id": event_id,
        "forecast_evidence_snapshot_id": "forecast-current",
        "prior_context_id": "prior-current",
        "approved_lr_artifact": _artifact(),
        "resolution_contract": _resolution_contract(),
        "realization_policy": _realization_policy(),
        "temperature_quantization_c": 1.0,
        "fact_snapshot_id": "factsnap_pipeline_current_123456",
        "correlation_id": "corr_pipeline_current_123456",
        "outbox_event_id": "outbox_pipeline_current_123456",
        "decision_as_of": "2026-07-03T06:59:59.400Z",
        "deadline_at": "2026-07-03T07:00:03Z",
        "started_at": "2026-07-03T06:59:59.000Z",
        "completed_at": "2026-07-03T06:59:59.400Z",
        "source_reliability_before": {"AMSC": {"state": "unknown"}},
        "source_reliability_after": {"AMSC": {"state": "unknown"}},
        "path_auxiliary": {"shadow_only": True},
    }


def _fast_lane_decision_inputs() -> dict:
    return {
        "outbox_event_id": "outbox_pipeline_current_123456",
        "lease_owner": "shadow-fast-lane-worker-1",
        "claimed_at": "2026-07-03T06:59:59.700Z",
        "lease_until": "2026-07-03T07:00:29.700Z",
        "slug": TARGET,
        "event_date": "2026-07-03",
        "market_snapshot": _decision_market(),
        "capital_plan": _decision_capital(),
        "settlement_window_policy": load_approved_settlement_window_policy(),
        "strategy_artifact_sha256": "strategy-artifact-pipeline-immutable",
    }


def _run(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    shadow_only: bool = True,
    resident_fast_lane_prewarm_dependency_identity: str = "",
    source_reliability_after: dict | None = None,
    resolution_contract: dict | None = None,
) -> dict:
    return run_canonical_event_shadow_update(
        conn,
        shadow_only=shadow_only,
        previous_checkpoint=_previous_checkpoint(),
        canonical_observation_event_id=event_id,
        forecast_evidence_snapshot_id="forecast-current",
        prior_context_id="prior-current",
        approved_lr_artifact=_artifact(),
        resolution_contract=resolution_contract or _resolution_contract(),
        realization_policy=_realization_policy(),
        temperature_quantization_c=1.0,
        fact_snapshot_id="factsnap_pipeline_current_123456",
        correlation_id="corr_pipeline_current_123456",
        outbox_event_id="outbox_pipeline_current_123456",
        decision_as_of=NOW,
        deadline_at="2026-07-03T07:00:03Z",
        started_at="2026-07-03T06:59:59Z",
        completed_at=NOW,
        source_reliability_before={"AMSC": {"state": "unknown"}},
        source_reliability_after=source_reliability_after or {"AMSC": {"state": "unknown"}},
        path_auxiliary={"shadow_only": True},
        resident_fast_lane_prewarm_dependency_identity=resident_fast_lane_prewarm_dependency_identity,
    )


def _run_public_ssot(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    resident_fast_lane_prewarm_dependency_identity: str = "",
    resolution_contract: dict | None = None,
) -> dict:
    return run_canonical_event_public_ssot_shadow_update(
        conn,
        public_slug=TARGET,
        public_event_date="2026-07-03",
        public_fact_as_of=NOW,
        shadow_only=True,
        previous_checkpoint=_previous_checkpoint(),
        canonical_observation_event_id=event_id,
        forecast_evidence_snapshot_id="forecast-current",
        prior_context_id="prior-current",
        approved_lr_artifact=_artifact(),
        resolution_contract=resolution_contract or _resolution_contract(),
        realization_policy=_realization_policy(),
        temperature_quantization_c=1.0,
        fact_snapshot_id="factsnap_pipeline_current_123456",
        correlation_id="corr_pipeline_current_123456",
        outbox_event_id="outbox_pipeline_current_123456",
        decision_as_of=NOW,
        deadline_at="2026-07-03T07:00:03Z",
        started_at="2026-07-03T06:59:59Z",
        completed_at=NOW,
        source_reliability_before={"AMSC": {"state": "unknown"}},
        source_reliability_after={"AMSC": {"state": "unknown"}},
        path_auxiliary={"shadow_only": True},
        resident_fast_lane_prewarm_dependency_identity=resident_fast_lane_prewarm_dependency_identity,
    )


def test_production_compiler_reuses_canonical_candidate_math_without_shadow_marker() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)
    inputs = {
        "previous_checkpoint": _previous_checkpoint(),
        "canonical_observation_event_id": event_id,
        "forecast_evidence_snapshot_id": "forecast-current",
        "prior_context_id": "prior-current",
        "approved_lr_artifact": _artifact(),
        "resolution_contract": _resolution_contract(),
        "realization_policy": _realization_policy(),
        "temperature_quantization_c": 1.0,
        "fact_snapshot_id": "factsnap-production-compiler",
        "correlation_id": "corr-production-compiler",
        "outbox_event_id": "outbox-production-compiler",
        "decision_as_of": NOW,
        "deadline_at": "2026-07-03T07:00:03Z",
        "started_at": "2026-07-03T06:59:59Z",
        "completed_at": NOW,
        "source_reliability_before": {"AMSC": {"state": "unknown"}},
        "source_reliability_after": {"AMSC": {"state": "unknown"}},
        "path_auxiliary": {"runtime": "production"},
    }
    try:
        shared = build_canonical_event_transaction_b_candidate(
            conn,
            candidate_mode="production",
            **inputs,
        )
        compiled = compile_production_transaction_b_candidate(conn, **inputs)
        assert shared["state"] == compiled["state"] == "candidate_ready"
        assert compiled["checkpoint_row"]["state"] == "production_ready"
        assert {
            key: value for key, value in shared["checkpoint_row"].items() if key != "state"
        } == {
            key: value for key, value in compiled["checkpoint_row"].items() if key != "state"
        }
        assert compiled["candidate_mode"] == "production"
        assert compiled["shadow_only"] is False
        production_realization = json.loads(compiled["realization_state_row"]["state_json"])
        assert production_realization["candidate_mode"] == "production"
        assert production_realization["shadow_only"] is False
        assert compiled["places_orders"] is False
        assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
    finally:
        conn.close()


def test_canonical_to_shadow_transaction_b_matches_same_cutoff_full_replay() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)

    result = _run(conn, event_id)

    assert result["ok"] is True
    assert result["state"] == "shadow_committed"
    assert result["shadow_only"] is True
    assert result["transaction_b"]["idempotent"] is False
    assert result["realization_projection"]["current_report_bucket"] == ""
    assert result["realization_projection"]["current_report_bucket_na_reason"] == "physical_x_must_not_be_directly_mapped_to_market_bucket_b"
    assert result["realization_projection"]["strong_physical_evidence"]["top_bucket"] == "30C"
    assert "trajectory_no_weather_eligible" not in result["realization_projection"]
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 1
    assert conn.execute("SELECT selected_source_id FROM canonical_observation_events").fetchone()[0] == "zuuu_metar_raw_amsc"
    assert conn.execute("SELECT COUNT(*) FROM canonical_observation_event_sources WHERE relation='confirmation'").fetchone()[0] == 1
    assert event_id not in result["strong_observation_update"]["strong_evidence_lineage"]["pre_event_trajectory_event_ids"]
    stored_trajectory = json.loads(
        conn.execute("SELECT trajectory_feature_state_json FROM posterior_checkpoints WHERE posterior_checkpoint_id=?", (result["checkpoint"]["posterior_checkpoint_id"],)).fetchone()[0]
    )
    assert stored_trajectory["temporal_role"] == "post_event"
    assert stored_trajectory["latest_canonical_observation_event_id"] == event_id

    event = load_canonical_observation_event(conn, canonical_observation_event_id=event_id)["canonical_observation_event"]
    replay = replay_canonical_sequence(
        initial_checkpoint=_previous_checkpoint(),
        canonical_events=[event],
        forecast_evidence_snapshot_id="forecast-current",
        prior_context_id="prior-current",
        approved_lr_artifact=_artifact(),
        decision_as_of=NOW,
        deadline_at="2026-07-03T07:00:03Z",
        fact_snapshot_id_prefix="factsnap_replay",
        resolution_contract_id="resolution-v1",
    )
    assert replay["ok"] is True
    assert replay["checkpoint"]["joint_posterior_sha256"] == result["checkpoint"]["joint_posterior_sha256"]
    assert replay["checkpoint"]["consumed_event_ids"] == result["checkpoint"]["consumed_event_ids"]
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('event_fact_history', 'event_fact_latest')"
    ).fetchall()

    retry = _run(conn, event_id)
    assert retry["ok"] is True
    assert retry["transaction_b"]["idempotent"] is True


def test_pipeline_blocks_before_transaction_b_when_l3_policy_is_not_approved() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)
    policy = _realization_policy()
    policy["approval_state"] = "draft"

    result = run_canonical_event_shadow_update(
        conn,
        shadow_only=True,
        previous_checkpoint=_previous_checkpoint(),
        canonical_observation_event_id=event_id,
        forecast_evidence_snapshot_id="forecast-current",
        prior_context_id="prior-current",
        approved_lr_artifact=_artifact(),
        resolution_contract=_resolution_contract(),
        realization_policy=policy,
        temperature_quantization_c=1.0,
        fact_snapshot_id="factsnap_pipeline_current_123456",
        correlation_id="corr_pipeline_current_123456",
        outbox_event_id="outbox_pipeline_current_123456",
        decision_as_of=NOW,
        deadline_at="2026-07-03T07:00:03Z",
        started_at="2026-07-03T06:59:59Z",
        completed_at=NOW,
        source_reliability_before={"AMSC": {"state": "unknown"}},
        source_reliability_after={"AMSC": {"state": "unknown"}},
        path_auxiliary={"shadow_only": True},
    )

    assert result["ok"] is False
    assert result["reason"] == "realization_state_projection_blocked"
    assert "realization_policy_identity_or_approval_invalid" in result["realization_projection"]["block_reasons"]
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0


def test_unclosed_bridge_commits_physical_posterior_but_creates_no_market_realization_or_outbox() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)

    result = _run(conn, event_id, resolution_contract=_resolution_contract(closed=False))

    assert result["ok"] is True
    assert result["state"] == "shadow_committed_physical_only"
    assert result["market_bucket_probabilities_na_reason"] == "x_to_s_to_b_bridge_unavailable"
    assert result["peak_timing_state"]["state"] in {"available", "na"}
    assert result["peak_timing_state"]["does_not_update_physical_x"] is True
    assert result["transaction_b"]["market_bucket_projection_state"] == "na"
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    row = conn.execute("SELECT market_bucket_distribution_json FROM posterior_checkpoints ORDER BY committed_at DESC LIMIT 1").fetchone()
    assert row is not None and row[0] == "{}"
    auxiliary = json.loads(
        conn.execute("SELECT path_auxiliary_json FROM strong_observation_updates").fetchone()[0]
    )
    assert auxiliary["peak_timing_state"]["state"] in {"available", "na"}


def test_pipeline_rejects_caller_injected_source_reliability_post_state_before_any_write() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)

    result = _run(
        conn,
        event_id,
        source_reliability_after={"AMSC": {"state": "verified"}},
    )

    assert result["ok"] is False
    assert result["reason"] == "external_source_reliability_post_state_update_not_allowed"
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0


def test_canonical_to_isolated_public_ssot_reuses_same_l3_candidate_and_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)

    result = _run_public_ssot(conn, event_id)

    assert result["ok"] is True
    assert result["state"] == "public_ssot_shadow_committed"
    assert result["public_ssot_shadow_only"] is True
    assert result["realization_projection"]["current_report_bucket"] == ""
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 2
    state_json = json.loads(conn.execute("SELECT state_json FROM realization_states").fetchone()[0])
    assert state_json["schema"] == "realization_state_l3_v2"
    assert state_json["projection"]["strong_observation_update_id"] == result["strong_observation_update"]["strong_observation_update_id"]
    fact = json.loads(conn.execute("SELECT fact_json FROM event_fact_history").fetchone()[0])
    assert fact["posterior_ssot_fact"]["realization_state_id"] == result["realization_state_row"]["realization_state_id"]
    assert conn.execute("SELECT COUNT(*) FROM event_fact_latest").fetchone()[0] == 1

    retried = _run_public_ssot(conn, event_id)
    assert retried["transaction_b"]["idempotent"] is True
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1


def test_unclosed_bridge_still_commits_physical_x_public_ssot_atomically_without_trade_rows() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)

    result = _run_public_ssot(
        conn,
        event_id,
        resolution_contract=_resolution_contract(closed=False),
    )

    assert result["ok"] is True
    assert result["state"] == "public_ssot_shadow_physical_only_committed"
    assert result["transaction_b"]["physical_only"] is True
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_fact_latest").fetchone()[0] == 1
    fact = json.loads(conn.execute("SELECT fact_json FROM event_fact_history").fetchone()[0])
    ssot = fact["posterior_ssot_fact"]
    assert ssot["state"] == "physical_only_market_projection_na"
    assert ssot["physical_high_distribution"]
    assert ssot["market_bucket_probabilities"] == {}
    assert ssot["market_bucket_probabilities_na_reason"] == "x_to_s_to_b_bridge_unavailable"
    assert ssot["realization_state_na_reason"] == "market_bucket_projection_unavailable"
    assert ssot["peak_timing_state"]["state"] in {"available", "na"}
    assert ssot["peak_timing_state"]["does_not_update_physical_x"] is True

    retry = _run_public_ssot(
        conn,
        event_id,
        resolution_contract=_resolution_contract(closed=False),
    )
    assert retry["transaction_b"]["idempotent"] is True
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1


def test_resident_prewarm_dependency_is_durable_lineage_not_bayesian_evidence() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)
    identity = "a" * 64

    result = _run_public_ssot(
        conn,
        event_id,
        resident_fast_lane_prewarm_dependency_identity=identity,
    )

    assert result["ok"] is True
    update_auxiliary = json.loads(
        conn.execute("SELECT path_auxiliary_json FROM strong_observation_updates").fetchone()[0]
    )
    public_fact = json.loads(
        conn.execute("SELECT fact_json FROM event_fact_history").fetchone()[0]
    )
    assert update_auxiliary["resident_fast_lane_prewarm_dependency_identity"] == identity
    assert (
        public_fact["posterior_ssot_fact"]["runtime_dependency_lineage"]
        ["resident_fast_lane_prewarm_dependency_identity"]
        == identity
    )
    assert "resident_fast_lane_prewarm_dependency_identity" not in result["strong_observation_update"]

    retry = _run_public_ssot(
        conn,
        event_id,
        resident_fast_lane_prewarm_dependency_identity=identity,
    )
    assert retry["transaction_b"]["idempotent"] is True
    with pytest.raises(ShadowTransactionIntegrityError, match="runtime_dependency_binding_mismatch"):
        _run_public_ssot(
            conn,
            event_id,
            resident_fast_lane_prewarm_dependency_identity="b" * 64,
        )


def test_private_transaction_b_rejects_retry_with_changed_resident_dependency_identity() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)

    assert _run(
        conn,
        event_id,
        resident_fast_lane_prewarm_dependency_identity="d" * 64,
    )["ok"] is True
    with pytest.raises(ShadowTransactionIntegrityError, match="runtime_dependency_binding_mismatch"):
        _run(
            conn,
            event_id,
            resident_fast_lane_prewarm_dependency_identity="e" * 64,
        )


def test_public_ssot_pipeline_refuses_nonisolated_db_before_any_p1_or_public_write() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)

    with pytest.raises(ShadowTransactionIntegrityError, match="public_ssot_shadow_manifest_missing"):
        _run_public_ssot(conn, event_id)

    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 0
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_fact_history'").fetchone()


def test_isolated_outbox_to_dynamic_three_leg_shadow_decision_is_atomic_and_never_live_submits() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)
    public = _run_public_ssot(conn, event_id)
    assert public["ok"] is True

    result = run_isolated_dynamic_three_leg_shadow_decision_cycle(
        conn,
        outbox_event_id="outbox_pipeline_current_123456",
        lease_owner="shadow-decision-worker-1",
        claimed_at="2026-07-03T07:00:01Z",
        lease_until="2026-07-03T07:00:30Z",
        slug=TARGET,
        event_date="2026-07-03",
        market_snapshot=_decision_market(),
        capital_plan=_decision_capital(),
        settlement_window_policy=load_approved_settlement_window_policy(),
        strategy_artifact_sha256="strategy-artifact-pipeline-immutable",
    )

    assert result["ok"] is True
    assert result["idempotent"] is False
    assert result["live_submission_authorized"] is False
    assert conn.execute("SELECT COUNT(*) FROM trading_plans").fetchone()[0] == 1
    # At 15:00 resolution-local time the independent NO settlement window is
    # still closed.  The physically excluded bucket remains unavailable as a
    # YES, while only the reachable top YES is executable.
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM decision_intents").fetchone()[0] == 1
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_alpha_cycles'").fetchone()
    assert [tuple(row) for row in conn.execute("SELECT DISTINCT will_order FROM auto_trade_decisions").fetchall()] == [(0,)]
    assert [tuple(row) for row in conn.execute("SELECT DISTINCT live_submit_authorized FROM decision_intents").fetchall()] == [(0,)]
    assert conn.execute("SELECT state FROM posterior_commit_outbox").fetchone()[0] == "completed"

    retry = run_isolated_dynamic_three_leg_shadow_decision_cycle(
        conn,
        outbox_event_id="outbox_pipeline_current_123456",
        lease_owner="shadow-decision-worker-2",
        claimed_at="2026-07-03T07:00:02Z",
        lease_until="2026-07-03T07:00:31Z",
        slug=TARGET,
        event_date="2026-07-03",
        market_snapshot=_decision_market(),
        capital_plan=_decision_capital(),
        settlement_window_policy=load_approved_settlement_window_policy(),
        strategy_artifact_sha256="strategy-artifact-pipeline-immutable",
    )
    assert retry["idempotent"] is True
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1


def test_shadow_decision_failure_rolls_back_plan_decisions_intents_cycle_and_outbox_completion() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)
    assert _run_public_ssot(conn, event_id)["ok"] is True
    claimed = claim_isolated_posterior_commit_outbox(
        conn,
        outbox_event_id="outbox_pipeline_current_123456",
        lease_owner="shadow-decision-worker-1",
        claimed_at="2026-07-03T07:00:01Z",
        lease_until="2026-07-03T07:00:30Z",
    )
    candidate = build_isolated_dynamic_three_leg_shadow_decision_candidate(
        conn,
        outbox_event_id="outbox_pipeline_current_123456",
        lease_owner="shadow-decision-worker-1",
        claim_generation=claimed["claim_generation"],
        slug=TARGET,
        event_date="2026-07-03",
        market_snapshot=_decision_market(),
        capital_plan=_decision_capital(),
        settlement_window_state=_settlement_window(),
        top_yes_wave_reentry_state=build_top_yes_wave_reentry_state(
            posterior_run_id=claimed["posterior_run_id"],
            completed_cycles=[],
        ),
        strategy_artifact_sha256="strategy-artifact-pipeline-immutable",
    )
    assert candidate["ok"] is True

    with pytest.raises(RuntimeError, match="dynamic_three_leg_shadow_fault_after_intents"):
        commit_isolated_dynamic_three_leg_shadow_decisions(
            conn,
            candidate=candidate,
            decision_as_of="2026-07-03T07:00:01Z",
            fault_after="intents",
        )

    assert conn.execute("SELECT COUNT(*) FROM trading_plans").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM decision_intents").fetchone()[0] == 0
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_alpha_cycles'").fetchone()
    assert conn.execute("SELECT state FROM posterior_commit_outbox").fetchone()[0] == "claimed"


def test_shadow_decision_schema_refuses_nonisolated_database_before_creating_decision_tables() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)

    with pytest.raises(ShadowTransactionIntegrityError, match="dynamic_three_leg_shadow_manifest_missing"):
        initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)

    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trading_plans'").fetchone()
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_trade_decisions'").fetchone()


def test_fast_lane_shadow_orchestrator_uses_p8_then_p9_within_slo_and_never_places_order() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)

    posterior_inputs = _fast_lane_posterior_inputs(event_id)
    posterior_inputs["resident_fast_lane_prewarm_dependency_identity"] = "c" * 64
    result = run_observation_fast_lane_shadow(
        conn,
        first_actionable_seen_at="2026-07-03T06:59:59.000Z",
        canonicalized_at="2026-07-03T06:59:59.050Z",
        topology=_fast_lane_topology(),
        posterior_transaction_inputs=posterior_inputs,
        decision_cycle_inputs=_fast_lane_decision_inputs(),
        commit_timestamp_observer=_commit_clock(
            "2026-07-03T06:59:59.100Z",
            "2026-07-03T06:59:59.400Z",
            "2026-07-03T06:59:59.500Z",
            "2026-07-03T06:59:59.700Z",
        ),
    )

    assert result["ok"] is True
    assert result["state"] == "shadow_committed_within_slo"
    assert result["places_orders"] is False
    assert result["posterior"]["public_ssot_shadow_only"] is True
    assert result["decision"]["live_submission_authorized"] is False
    assert result["outbox_dispatch"]["boundary"]["dedicated_posterior_outbox_only"] is True
    assert result["outbox_dispatch"]["boundary"]["generic_task_queue"] is False
    assert result["fast_lane"]["total_elapsed_ms"] == 700.0
    assert result["telemetry"]["state"] == "appended"
    assert conn.execute("SELECT COUNT(*) FROM observation_fast_lane_latency_packets").fetchone()[0] == 1
    assert result["latency_timestamp_sources"]["caller_completed_at_not_used_for_latency"] is True
    assert result["latency_timestamp_sources"]["caller_claimed_at_not_used_for_latency"] is True
    assert conn.execute("SELECT COUNT(*) FROM decision_intents WHERE live_submit_authorized=1").fetchone()[0] == 0
    fact = json.loads(conn.execute("SELECT fact_json FROM event_fact_history").fetchone()[0])
    assert (
        fact["posterior_ssot_fact"]["runtime_dependency_lineage"]
        ["resident_fast_lane_prewarm_dependency_identity"]
        == "c" * 64
    )
    assert fact["posterior_ssot_fact"]["committed_at"] == "2026-07-03T06:59:59.100Z"
    assert conn.execute("SELECT created_at FROM auto_trade_decisions LIMIT 1").fetchone()[0] == "2026-07-03T06:59:59.500Z"


def test_fast_lane_bad_topology_blocks_before_any_shadow_posterior_or_decision_write() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)

    result = run_observation_fast_lane_shadow(
        conn,
        first_actionable_seen_at="2026-07-03T06:59:59.000Z",
        canonicalized_at="2026-07-03T06:59:59.050Z",
        topology=_fast_lane_topology(release_delay_ms=35000),
        posterior_transaction_inputs=_fast_lane_posterior_inputs(event_id),
        decision_cycle_inputs=_fast_lane_decision_inputs(),
    )

    assert result["ok"] is False
    assert result["reason"] == "fast_lane_topology_blocked_before_write"
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0


def test_fast_lane_uses_observed_t1_deadline_before_p8_not_caller_logical_timestamps() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)

    result = run_observation_fast_lane_shadow(
        conn,
        first_actionable_seen_at="2026-07-03T06:59:59.000Z",
        canonicalized_at="2026-07-03T06:59:59.050Z",
        topology=_fast_lane_topology(),
        posterior_transaction_inputs=_fast_lane_posterior_inputs(event_id),
        decision_cycle_inputs=_fast_lane_decision_inputs(),
        commit_timestamp_observer=_commit_clock("2026-07-03T07:00:03.001Z"),
    )

    assert result["ok"] is False
    assert result["reason"] == "fast_lane_observation_hard_deadline_or_clock_invalid_before_transaction_b"
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observation_fast_lane_latency_packets'"
    ).fetchone()


def test_pipeline_requires_explicit_shadow_opt_in_before_any_write() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    event_id = _canonical_event_id(conn)

    with pytest.raises(ValueError, match="shadow_only_required"):
        _run(conn, event_id, shadow_only=False)

    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 0
