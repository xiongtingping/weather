from __future__ import annotations

import json

import pytest

from reported_x_successor_checkpoint_runtime import (
    CHECKPOINT_SCHEMA,
    PUBLIC_SSOT_SCHEMA,
    build_successor_checkpoint_surfaces,
    build_successor_public_ssot_surfaces,
    build_successor_resolution_bridge,
    materialize_successor_checkpoint_columns,
    upgrade_initial_candidate,
    upgrade_physical_only_strong_candidate,
    upgrade_weak_revision_candidate,
    validate_successor_checkpoint_row,
)


def _event(event_id: str, report_type: str, temperature_c: float) -> dict:
    return {
        "canonical_observation_event_id": event_id,
        "report_type": report_type,
        "valid_time": "2026-07-15T00:30:00Z",
        "parsed_observation": {"temperature_c": temperature_c},
    }


def _checkpoint(observed_bound=30.5) -> dict:
    return {
        "posterior_checkpoint_id": "pchk-1",
        "joint_posterior_sha256": "a" * 64,
        "observed_max_lower_bound_c": observed_bound,
        "joint_posterior": [
            {"temperature_c": 30.75, "state": "terminated", "probability": 0.7},
            {"temperature_c": 31.75, "state": "warming", "probability": 0.3},
        ],
    }


def _row() -> dict:
    columns = materialize_successor_checkpoint_columns(
        _checkpoint(),
        canonical_events=[
            _event("metar-31", "METAR", 31.0),
            _event("speci-34", "SPECI", 34.0),
        ],
    )
    assert columns["ok"] is True
    return columns["columns"]


def test_p0_projects_to_integer_x_without_observed_bound() -> None:
    result = build_successor_checkpoint_surfaces(
        _checkpoint(observed_bound=None),
        canonical_events=[],
    )
    assert result["ok"] is True
    assert result["reported_x_lower_bound_c"] is None
    assert set(result["reported_physical_x_distribution"]) == {"31", "32"}
    assert result["reported_x_termination"]["state"] == "na"


def test_incremental_and_weak_successor_share_same_integer_projection() -> None:
    events = [
        _event("metar-31", "METAR", 31.0),
        _event("speci-34", "SPECI", 34.0),
    ]
    strong = build_successor_checkpoint_surfaces(_checkpoint(), canonical_events=events)
    weak_revision = build_successor_checkpoint_surfaces(
        {
            **_checkpoint(),
            "joint_posterior": [
                {"temperature_c": 30.75, "state": "terminated", "probability": 0.2},
                {"temperature_c": 31.75, "state": "warming", "probability": 0.8},
            ],
        },
        canonical_events=events,
    )
    assert strong["reported_x_lower_bound_c"] == 31
    assert weak_revision["reported_x_lower_bound_c"] == 31
    assert strong["reported_physical_x_distribution"] == {"31": 0.7, "32": 0.3}
    assert weak_revision["reported_physical_x_distribution"] == {"31": 0.2, "32": 0.8}


def test_checkpoint_columns_validate_and_public_ssot_exposes_only_x() -> None:
    row = _row()
    assert row["checkpoint_probability_schema"] == CHECKPOINT_SCHEMA
    assert validate_successor_checkpoint_row(row)["ok"] is True
    public = build_successor_public_ssot_surfaces(row)
    assert public["schema"] == PUBLIC_SSOT_SCHEMA
    assert public["reported_physical_x_distribution"] == {"31": 0.7, "32": 0.3}
    assert public["latent_probability_public"] is False
    assert "latent_scheduled_slot_high_distribution" not in public


def test_bridge_consumes_integer_x_not_latent_z() -> None:
    result = build_successor_resolution_bridge(
        _row(),
        resolution_contract={
            "resolution_contract_id": "resolution-1",
            "resolution_source_id": "wu-zuuu",
            "resolution_local_date": "2026-07-15",
            "settlement_unit": "C",
            "bridge_closed": False,
            "validated": False,
        },
    )
    assert result["ok"] is True
    assert result["input_probability_field"] == "reported_physical_x_distribution"
    assert result["continuous_latent_distribution_consumed"] is False


def test_validation_rejects_fractional_public_x_support() -> None:
    row = _row()
    row["reported_physical_x_distribution_json"] = json.dumps({"31.25": 1.0})
    result = validate_successor_checkpoint_row(row)
    assert result["ok"] is False
    assert "reported_x_support_not_integer" in result["issues"]


def _resolution() -> dict:
    return {
        "resolution_contract_id": "resolution-1",
        "resolution_source_id": "wu-zuuu",
        "resolution_local_date": "2026-07-15",
        "settlement_unit": "C",
        "bridge_closed": False,
        "validated": False,
    }


def test_initial_candidate_upgrade_uses_reported_x_bridge() -> None:
    candidate = {
        "ok": True,
        "checkpoint": {**_checkpoint(observed_bound=None), "checkpoint_kind": "initial"},
        "resolution_bridge": {},
    }
    result = upgrade_initial_candidate(candidate, resolution_contract=_resolution())
    assert result["ok"] is True
    assert "physical_high_distribution" not in result["checkpoint"]
    assert result["checkpoint"]["reported_physical_x_distribution"] == {
        "31": 0.7,
        "32": 0.3,
    }
    assert result["resolution_bridge"]["input_probability_field"] == (
        "reported_physical_x_distribution"
    )


def test_physical_only_strong_upgrade_replaces_continuous_termination_surface() -> None:
    events = [
        _event("metar-31", "METAR", 31.0),
        _event("speci-34", "SPECI", 34.0),
    ]
    checkpoint = _checkpoint()
    candidate = {
        "ok": True,
        "state": "physical_only_candidate_ready",
        "checkpoint": checkpoint,
        "checkpoint_row": {
            "joint_posterior_json": json.dumps(checkpoint["joint_posterior"]),
            "observed_max_lower_bound_c": 30.5,
            "physical_high_distribution_json": json.dumps(
                {"30.75": 0.7, "31.75": 0.3}
            ),
            "settlement_scalar_distribution_json": "[]",
            "market_bucket_distribution_json": "{}",
        },
        "strong_observation_update": {
            "canonical_observation_event_id": "speci-34",
            "model_artifact_sha256": "c" * 64,
            "previous_checkpoint_id": "pchk-previous",
            "likelihood_update_kind": "cumulative_evidence_replacement_delta_log_lr_v1",
            "raw_log_score_sha256": "d" * 64,
            "previous_cumulative_raw_log_score_sha256": "",
            "conditional_innovation_log_score_sha256": "e" * 64,
        },
        "strong_update_row": {
            "raw_log_score_sha256": "d" * 64,
            "path_auxiliary_json": "{}",
        },
    }
    result = upgrade_physical_only_strong_candidate(
        candidate,
        canonical_events=events,
        resolution_contract=_resolution(),
    )
    assert result["ok"] is True
    assert result["checkpoint_row"]["physical_high_distribution_json"] == "{}"
    assert json.loads(
        result["checkpoint_row"]["reported_physical_x_distribution_json"]
    ) == {"31": 0.7, "32": 0.3}
    termination = result["strong_observation_update"]["termination_risk"]
    assert termination["schema"] == "termination_risk_marginal_v2_reported_x"
    assert termination["probability_no_material_new_daily_high"] == 0.7
    assert termination["updates_posterior"] is False
    identity = json.loads(result["strong_update_row"]["path_auxiliary_json"])[
        "strong_likelihood_update_identity"
    ]
    assert identity["likelihood_update_kind"] == "cumulative_evidence_replacement_delta_log_lr_v1"
    assert identity["conditional_innovation_log_score_sha256"] == "e" * 64


def test_strong_upgrade_refuses_old_closed_bridge_realization_candidate() -> None:
    result = upgrade_physical_only_strong_candidate(
        {"ok": True, "state": "candidate_ready"},
        canonical_events=[],
        resolution_contract={**_resolution(), "bridge_closed": True, "validated": True},
    )
    assert result["ok"] is False
    assert result["block_reasons"] == [
        "successor_strong_candidate_must_be_physical_only"
    ]


def test_weak_revision_upgrade_calculates_delta_on_reported_x() -> None:
    predecessor = _row()
    revised_checkpoint = {
        "joint_posterior_json": json.dumps(
            [
                {"temperature_c": 30.75, "state": "terminated", "probability": 0.2},
                {"temperature_c": 31.75, "state": "warming", "probability": 0.8},
            ]
        ),
        "observed_max_lower_bound_c": 30.5,
        "physical_high_distribution_json": json.dumps({"30.75": 0.2, "31.75": 0.8}),
    }
    result = upgrade_weak_revision_candidate(
        {
            "ok": True,
            "state": "candidate_ready",
            "checkpoint_row": revised_checkpoint,
            "update_row": {},
        },
        previous_checkpoint_row=predecessor,
        canonical_events=[_event("metar-31", "METAR", 31.0)],
    )
    assert result["ok"] is True
    delta = json.loads(result["update_row"]["posterior_delta_json"])
    assert delta["31"] == pytest.approx(-0.5)
    assert delta["32"] == pytest.approx(0.5)
    assert result["checkpoint_row"]["physical_high_distribution_json"] == "{}"
