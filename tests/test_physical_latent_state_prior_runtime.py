from __future__ import annotations

from physical_latent_state_prior_runtime import (
    build_neutral_physical_latent_state_operational_baseline,
    build_physical_latent_state_prior,
)


def _sample(identifier: str, temperature: float, state: str, *, first: bool = True) -> tuple[dict, dict]:
    sample = {
        "training_sample_id": identifier,
        "quality_grade": "B",
        "physical_auxiliary_eligible": True,
        "canonical_sequence_event_ids": [identifier] if first else ["previous", identifier],
        "labels": {"final_physical_high_c": temperature},
    }
    replay = {
        "training_sample_id": identifier,
        "training_scope": "physical_x",
        "z_pre": state,
        "latent_replay_sha256": f"replay-{identifier}",
    }
    return sample, replay


def test_latent_p0_is_empirical_first_event_distribution_not_handwritten_uniform() -> None:
    samples, labels = zip(
        _sample("a", 30.0, "warming"),
        _sample("b", 30.0, "warming"),
        _sample("c", 31.0, "temporary_platform"),
        _sample("d", 31.0, "temporary_platform"),
    )
    result = build_physical_latent_state_prior(
        physical_training_samples=list(samples),
        latent_replay_labels=list(labels),
        temperature_grid_c=[30.0, 31.0],
        minimum_samples_per_temperature=2,
    )

    assert result["ok"] is True
    assert result["latent_state_prior_by_temperature"] == {
        "30": {"warming": 1.0},
        "31": {"temporary_platform": 1.0},
    }
    assert result["boundary"]["no_prior_observation_is_not_latent_state"] is True


def test_latent_p0_rejects_nonfirst_or_undercovered_cells_instead_of_filling_them() -> None:
    first, replay = _sample("a", 30.0, "warming")
    nonfirst, nonfirst_replay = _sample("b", 31.0, "warming", first=False)
    result = build_physical_latent_state_prior(
        physical_training_samples=[first, nonfirst],
        latent_replay_labels=[replay, nonfirst_replay],
        temperature_grid_c=[30.0, 31.0],
        minimum_samples_per_temperature=1,
    )

    assert result["ok"] is False
    assert result["state"] == "insufficient_history"
    assert result["diagnostics"]["undercovered_temperature_bins"] == {"31": 0}
    assert result["diagnostics"]["rejected_counts"]["sample_not_first_canonical_event"] == 1


def test_latent_p0_uses_explicit_empirical_temperature_bins_then_maps_to_grid() -> None:
    samples, labels = zip(
        _sample("a", 30.1, "warming"),
        _sample("b", 30.4, "warming"),
        _sample("c", 31.1, "temporary_platform"),
        _sample("d", 31.4, "temporary_platform"),
    )
    result = build_physical_latent_state_prior(
        physical_training_samples=list(samples),
        latent_replay_labels=list(labels),
        temperature_grid_c=[30.0, 30.5, 31.0, 31.5],
        temperature_bins=[
            {"label": "30s", "lower_inclusive": 29.75, "upper_exclusive": 30.75},
            {"label": "31s", "lower_inclusive": 30.75, "upper_exclusive": 31.75},
        ],
        minimum_samples_per_temperature=2,
    )

    assert result["ok"] is True
    assert result["latent_state_prior_by_temperature"]["30"] == {"warming": 1.0}
    assert result["latent_state_prior_by_temperature"]["31"] == {"temporary_platform": 1.0}
    assert result["boundary"]["temperature_bins_are_explicit_and_empirical"] is True


def test_latent_p0_rejects_overlapping_bins_instead_of_selecting_one_by_order() -> None:
    sample, label = _sample("a", 30.0, "warming")
    result = build_physical_latent_state_prior(
        physical_training_samples=[sample],
        latent_replay_labels=[label],
        temperature_grid_c=[30.0, 31.0],
        temperature_bins=[
            {"label": "left", "lower_inclusive": 29.0, "upper_exclusive": 30.6},
            {"label": "right", "lower_inclusive": 30.5, "upper_exclusive": 31.5},
        ],
        minimum_samples_per_temperature=1,
    )

    assert result["ok"] is False
    assert "physical_latent_prior_temperature_bins_overlap" in result["block_reasons"]


def test_neutral_operational_latent_baseline_is_explicit_and_does_not_claim_training() -> None:
    result = build_neutral_physical_latent_state_operational_baseline(
        temperature_grid_c=[30.0, 31.0]
    )

    assert result["ok"] is True
    assert result["approval_kind"] == "operational_precalibration_neutral_baseline"
    assert result["boundary"]["not_empirical_evidence"] is True
    assert sum(result["latent_state_prior_by_temperature"]["30"].values()) == 1.0
