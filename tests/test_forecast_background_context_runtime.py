import pytest

from forecast_background_context_runtime import (
    REGIMES,
    build_forecast_background_context,
    validate_forecast_background_context,
)


def test_disagreement_is_preserved_as_weighted_vector_not_mixed_zero() -> None:
    context = build_forecast_background_context(
        forecast_evidence_snapshot_id="forev-1",
        decision_as_of="2026-07-15T03:00:00Z",
        source_revisions=[
            {"source_id": "gfs", "source_revision_id": "g1", "forecast_weather_regime": "clear_dry"},
            {"source_id": "taf", "source_revision_id": "t1", "forecast_weather_regime": "convection_or_showers"},
        ],
        source_expert_trace=[
            {"source_id": "gfs", "source_revision_id": "g1", "information_weight": 0.75},
            {"source_id": "taf", "source_revision_id": "t1", "information_weight": 0.25},
        ],
        missing_source_ids=["ecmwf"],
    )
    validated = validate_forecast_background_context(context)
    assert validated["regime_probability_vector"]["clear_dry"] == pytest.approx(0.75)
    assert validated["regime_probability_vector"]["convection_or_showers"] == pytest.approx(0.25)
    assert validated["unknown_probability"] == pytest.approx(0.0)
    assert validated["transition_control_active"] is False
    assert validated["changes_probability"] is False


def test_unknown_sources_are_neutral_and_vector_contract_is_fixed() -> None:
    context = build_forecast_background_context(
        forecast_evidence_snapshot_id="forev-2",
        decision_as_of="2026-07-15T03:30:00Z",
        source_revisions=[
            {"source_id": "gfs", "source_revision_id": "g2", "forecast_weather_regime": "unknown_forecast_regime"},
        ],
        source_expert_trace=[
            {"source_id": "gfs", "source_revision_id": "g2", "information_weight": 1.0},
        ],
    )
    validated = validate_forecast_background_context(context)
    assert set(validated["regime_probability_vector"]) == set(REGIMES)
    assert sum(validated["regime_probability_vector"].values()) == 0.0
    assert validated["unknown_probability"] == 1.0
    assert validated["state"] == "unknown_neutral"
