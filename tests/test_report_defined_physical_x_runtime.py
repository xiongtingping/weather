from __future__ import annotations

import pytest

from report_defined_physical_x_runtime import (
    advance_reported_x_lower_bound,
    build_report_defined_checkpoint_projection,
    build_reported_x_termination_marginal,
    project_latent_high_to_reported_x,
    project_joint_latent_high_to_reported_x,
    round_to_reported_c,
    scheduled_metar_reported_max,
)


def _event(event_id: str, report_type: str, temperature_c: float) -> dict:
    return {
        "canonical_observation_event_id": event_id,
        "report_type": report_type,
        "valid_time": "2026-07-15T00:30:00Z",
        "parsed_observation": {"temperature_c": temperature_c},
    }


def test_metar_reported_max_is_exact_and_speci_does_not_raise_x_bound() -> None:
    result = scheduled_metar_reported_max(
        [_event("metar-30", "METAR", 30.0), _event("speci-33", "SPECI", 33.0)]
    )

    assert result["ok"] is True
    assert result["reported_x_lower_bound_c"] == 30
    assert result["ignored_speci_event_ids"] == ["speci-33"]


def test_incremental_bound_advances_on_metar_and_not_on_speci() -> None:
    metar = advance_reported_x_lower_bound(30, _event("metar-31", "METAR", 31.0))
    speci = advance_reported_x_lower_bound(
        metar["reported_x_lower_bound_c"], _event("speci-34", "SPECI", 34.0)
    )

    assert metar["reported_x_lower_bound_c"] == 31
    assert speci["reported_x_lower_bound_c"] == 31
    assert speci["state"] == "unchanged_speci_soft_evidence_only"


def test_report_projection_has_integer_support_and_masks_below_observed_metar() -> None:
    result = project_latent_high_to_reported_x(
        {"30.5": 0.20, "30.75": 0.25, "31": 0.30, "31.25": 0.15, "31.75": 0.10},
        reported_x_lower_bound_c=31,
    )

    assert result["ok"] is True
    assert set(result["reported_x_distribution"]) == {"31", "32"}
    assert sum(result["reported_x_distribution"].values()) == pytest.approx(1.0)
    assert all(float(value) >= 31 for value in result["reported_x_distribution"])


def test_joint_state_is_marginalized_once_before_report_projection() -> None:
    result = project_joint_latent_high_to_reported_x(
        [
            {"temperature_c": 30.75, "state": "warming", "probability": 0.3},
            {"temperature_c": 30.75, "state": "cooling", "probability": 0.2},
            {"temperature_c": 31.75, "state": "warming", "probability": 0.5},
        ],
        reported_x_lower_bound_c=31,
    )

    assert result["reported_x_distribution"] == {"31": 0.5, "32": 0.5}


def test_reported_x_termination_is_a_read_only_marginal() -> None:
    result = build_reported_x_termination_marginal(
        {"31": 0.8, "32": 0.18, "33": 0.02},
        reported_x_lower_bound_c=31,
    )

    assert result["probability_no_future_reported_new_high"] == pytest.approx(0.8)
    assert result["probability_future_reported_new_high"] == pytest.approx(0.2)
    assert result["updates_posterior"] is False


def test_metar_half_degree_interval_is_not_used_as_final_x_hard_bound() -> None:
    result = scheduled_metar_reported_max([_event("metar-31", "METAR", 31.0)])
    assert result["reported_x_lower_bound_c"] == 31
    projected = project_latent_high_to_reported_x(
        {"30.5": 0.4, "31": 0.6},
        reported_x_lower_bound_c=result["reported_x_lower_bound_c"],
    )
    assert min(map(int, projected["reported_x_distribution"])) == 31


@pytest.mark.parametrize(
    ("latent", "reported"),
    [(30.49, 30), (30.5, 31), (31.49, 31), (31.5, 32), (-0.5, -1)],
)
def test_rounding_is_deterministic_at_grid_boundaries(latent: float, reported: int) -> None:
    assert round_to_reported_c(latent) == reported


def test_checkpoint_projection_separates_latent_z_from_public_reported_x() -> None:
    checkpoint = {
        "posterior_checkpoint_id": "pchk-1",
        "joint_posterior_sha256": "joint-1",
        "observed_max_lower_bound_c": 30.5,
        "joint_posterior": [
            {"temperature_c": 30.75, "state": "terminated", "probability": 0.6},
            {"temperature_c": 31.75, "state": "warming", "probability": 0.4},
        ],
    }
    result = build_report_defined_checkpoint_projection(
        checkpoint,
        canonical_events=[
            _event("metar-31", "METAR", 31.0),
            _event("speci-34", "SPECI", 34.0),
        ],
    )

    assert result["ok"] is True
    assert result["latent_scheduled_slot_high_distribution"] == {
        "30.75": 0.6,
        "31.75": 0.4,
    }
    assert result["reported_physical_x_distribution"] == {
        "31": pytest.approx(0.6),
        "32": pytest.approx(0.4),
    }
    assert result["latent_observed_lower_bound_c"] == 30.5
    assert result["reported_x_lower_bound_c"] == 31
    assert result["resolution_bridge_input_field"] == "reported_physical_x_distribution"
    assert result["ambiguous_physical_high_distribution_retired"] is True
    assert result["reported_x_termination"]["updates_posterior"] is False


def test_checkpoint_projection_rejects_speci_contaminated_hard_bound() -> None:
    result = build_report_defined_checkpoint_projection(
        {
            "observed_max_lower_bound_c": 33.5,
            "joint_posterior": [
                {"temperature_c": 34.0, "state": "warming", "probability": 1.0}
            ],
        },
        canonical_events=[
            _event("metar-31", "METAR", 31.0),
            _event("speci-34", "SPECI", 34.0),
        ],
    )

    assert result["ok"] is False
    assert result["block_reasons"] == [
        "latent_hard_bound_not_derived_from_regular_metar_only"
    ]
