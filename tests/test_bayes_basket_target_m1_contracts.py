from __future__ import annotations

import pytest

from bayes_basket_target import (
    CanonicalObservationEvent,
    ContractError,
    ForecastRevision,
    ProbabilityEnvelope,
    StageProbabilityBiasContract,
    validate_canonical_observation_sequence,
    validate_forecast_revision_chain,
)
from bayes_basket_target.basket_kernel.contracts import TargetBasketPolicy
from bayes_basket_target.contracts import PROBABILITY_MAX_STALE_SECONDS


SHA = "a" * 64


def _envelope() -> ProbabilityEnvelope:
    return ProbabilityEnvelope.from_mapping({
        "schema": "bayes_basket_probability_envelope_v1",
        "stage": "P0",
        "checkpoint_id": "p0m-contract",
        "station_id": "EDDM",
        "event_date": "2026-07-26",
        "settlement_timezone": "Europe/Berlin",
        "decision_at_utc": "2026-07-25T12:00:00Z",
        "fresh_until_utc": "2026-07-26T12:00:00Z",
        "bucket_ordinals": {"28": 28, "29": 29, "30": 30, "31": 31},
        "probabilities": {"28": 0.10, "29": 0.40, "30": 0.35, "31": 0.15},
        "posterior_version": "p0m-v1",
        "posterior_sha256": SHA,
        "bridge_sha256": "b" * 64,
        "lineage": {"ensemble_artifact_id": "ensemble-1"},
    })


def test_p1_serves_past_its_cadence_but_only_until_the_day_has_moved() -> None:
    base = _envelope().to_dict()
    p1 = ProbabilityEnvelope.from_mapping({
        **base,
        "stage": "P1",
        "checkpoint_id": "p1-cadence",
        "decision_at_utc": "2026-07-26T07:00:00Z",
        "fresh_until_utc": "2026-07-26T08:00:00Z",
    })
    assert PROBABILITY_MAX_STALE_SECONDS == 3 * 60 * 60

    assert p1.is_fresh("2026-07-26T07:30:00Z") is True
    assert p1.is_servable("2026-07-26T07:30:00Z") is True
    # The case that stood ZUUU down for an event day: minutes past an hourly
    # roll, with nothing newer in existence.
    assert p1.is_fresh("2026-07-26T08:13:00Z") is False
    assert p1.is_servable("2026-07-26T08:13:00Z") is True
    assert p1.is_servable("2026-07-26T11:00:00Z") is True
    assert p1.is_servable("2026-07-26T11:00:01Z") is False
    # A checkpoint is never usable before it was decided.
    assert p1.is_servable("2026-07-26T06:59:59Z") is False

    # The allowance is P1's alone.  A lapsed P0/P0-M means a newer model run
    # or TAF exists to recompile from, so serving the old one is a choice, not
    # the only option.
    for stage in ("P0", "P0-M"):
        lapsed = ProbabilityEnvelope.from_mapping({
            **base,
            "stage": stage,
            "checkpoint_id": f"{stage.lower()}-cadence",
            "decision_at_utc": "2026-07-26T07:00:00Z",
            "fresh_until_utc": "2026-07-26T08:00:00Z",
        })
        assert lapsed.is_servable("2026-07-26T08:13:00Z") is False


def _policy() -> TargetBasketPolicy:
    return TargetBasketPolicy.from_mapping({
        "clip_max_notional_usdc": 10.0,
        "center_lead_min_probability": 0.01,
        "direction_skew_deadband": 0.01,
        "direction_entropy_max": 1.0,
        "direction_min_side_mass": 0.01,
    })


def _forecast(
    revision_id: str,
    *,
    kind: str,
    sequence: int,
    supersedes: str | None,
    received_at: str,
) -> ForecastRevision:
    return ForecastRevision.from_mapping({
        "schema": "bayes_basket_forecast_revision_v1",
        "revision_id": revision_id,
        "station_id": "EDDM",
        "event_date": "2026-07-26",
        "revision_kind": kind,
        "sequence": sequence,
        "issued_at_utc": "2026-07-25T10:00:00Z",
        "received_at_utc": received_at,
        "valid_from_utc": "2026-07-25T12:00:00Z",
        "valid_until_utc": "2026-07-26T12:00:00Z",
        "source_object_sha256": f"{sequence:064x}",
        "supersedes_revision_id": supersedes,
    })


def _observation(
    observation_id: str,
    *,
    sequence: int,
    received_at: str,
    supersedes: str | None = None,
) -> CanonicalObservationEvent:
    return CanonicalObservationEvent.from_mapping({
        "schema": "bayes_basket_canonical_observation_event_v2",
        "observation_id": observation_id,
        "station_id": "EDDM",
        "event_date": "2026-07-26",
        "settlement_timezone": "Europe/Berlin",
        "source_lane": "awc",
        "report_type": "METAR",
        "sequence": sequence,
        "observed_at_utc": "2026-07-26T10:00:00Z",
        "received_at_utc": received_at,
        "raw_sha256": f"{sequence:064x}",
        "qc_state": "REVIEW_REQUIRED" if supersedes else "ACCEPTED",
        "supersedes_observation_id": supersedes,
    })


def test_probability_envelope_is_full_normalized_strict_and_content_addressed() -> None:
    envelope = _envelope()
    assert sum(envelope.probabilities.values()) == pytest.approx(1.0)
    assert envelope.is_fresh("2026-07-26T00:00:00Z") is True
    assert len(envelope.content_sha256) == 64

    with pytest.raises(ContractError, match="unknown_fields"):
        ProbabilityEnvelope.from_mapping({**envelope.to_dict(), "weather_direction": "UP"})
    with pytest.raises(ContractError, match="not_normalized"):
        ProbabilityEnvelope.from_mapping({
            **envelope.to_dict(),
            "probabilities": {"28": 0.1, "29": 0.4, "30": 0.3, "31": 0.1},
        })


def test_forecast_revision_chain_replaces_each_predecessor_exactly_once() -> None:
    taf = _forecast(
        "taf-1",
        kind="TAF",
        sequence=1,
        supersedes=None,
        received_at="2026-07-25T10:01:00Z",
    )
    amd = _forecast(
        "taf-amd-2",
        kind="AMD",
        sequence=2,
        supersedes="taf-1",
        received_at="2026-07-25T11:01:00Z",
    )
    cor = _forecast(
        "taf-cor-3",
        kind="COR",
        sequence=3,
        supersedes="taf-amd-2",
        received_at="2026-07-25T12:01:00Z",
    )
    assert validate_forecast_revision_chain([cor, taf, amd]) == (taf, amd, cor)

    duplicate_child = _forecast(
        "taf-cor-other",
        kind="COR",
        sequence=3,
        supersedes="taf-1",
        received_at="2026-07-25T12:02:00Z",
    )
    with pytest.raises(ContractError, match="sequence_gap_or_duplicate|not_linear"):
        validate_forecast_revision_chain([taf, amd, cor, duplicate_child])
    with pytest.raises(ContractError, match="unknown_fields"):
        ForecastRevision.from_mapping({**taf.to_dict(), "temperature": 30})


def test_canonical_observation_sequence_rejects_duplicate_rewind_and_unreviewed_revision() -> None:
    first = _observation("metar-1", sequence=1, received_at="2026-07-26T10:01:00Z")
    second = _observation("metar-2", sequence=2, received_at="2026-07-26T11:01:00Z")
    assert validate_canonical_observation_sequence([second, first]) == (first, second)

    with pytest.raises(ContractError, match="id_duplicate"):
        validate_canonical_observation_sequence([first, first])
    rewind = _observation("metar-2", sequence=2, received_at="2026-07-26T10:00:30Z")
    with pytest.raises(ContractError, match="arrival_rewind"):
        validate_canonical_observation_sequence([first, rewind])
    with pytest.raises(ContractError, match="revision_requires_review"):
        CanonicalObservationEvent.from_mapping({
            **second.to_dict(),
            "supersedes_observation_id": "metar-1",
            "qc_state": "ACCEPTED",
        })


def test_canonical_correction_is_review_only_and_linear() -> None:
    first = _observation("metar-1", sequence=1, received_at="2026-07-26T10:01:00Z")
    correction = _observation(
        "metar-cor-2",
        sequence=2,
        received_at="2026-07-26T10:02:00Z",
        supersedes="metar-1",
    )
    assert validate_canonical_observation_sequence([first, correction]) == (first, correction)
    assert correction.qc_state == "REVIEW_REQUIRED"


def test_canonical_observation_timezone_is_required_identity() -> None:
    row = _observation(
        "metar-timezone", sequence=1,
        received_at="2026-07-26T10:01:00Z",
    )
    missing = row.to_dict()
    missing.pop("settlement_timezone")
    with pytest.raises(ContractError, match="settlement_timezone_missing"):
        CanonicalObservationEvent.from_mapping(missing)
    legacy = row.to_dict()
    legacy["schema"] = "bayes_basket_canonical_observation_event_v1"
    with pytest.raises(ContractError, match="schema_invalid"):
        CanonicalObservationEvent.from_mapping(legacy)
    conflict = row.to_dict()
    conflict.update({
        "observation_id": "metar-timezone-2",
        "sequence": 2,
        "settlement_timezone": "UTC",
    })
    with pytest.raises(ContractError, match="scope_mixed"):
        validate_canonical_observation_sequence(
            [row, CanonicalObservationEvent.from_mapping(conflict)]
        )


def test_stage_probability_bias_has_one_probability_only_direction_and_replays() -> None:
    envelope = _envelope()
    first = StageProbabilityBiasContract.materialize(
        envelope,
        _policy(),
        at_utc="2026-07-26T00:00:00Z",
    )
    second = StageProbabilityBiasContract.materialize(
        envelope,
        _policy(),
        at_utc="2026-07-26T00:00:00Z",
    )

    assert first == second
    assert first.direction == "UP"
    assert first.direction_skew == pytest.approx(0.40)
    assert first.content_sha256 == second.content_sha256
    with pytest.raises(ContractError, match="unknown_fields"):
        StageProbabilityBiasContract.from_mapping({
            **first.to_dict(),
            "market_anchor_direction": "DOWN",
        })


@pytest.mark.parametrize("stage", ("P0", "P0-M", "P1"))
@pytest.mark.parametrize(
    ("case", "probabilities", "policy_overrides", "at_utc", "expected"),
    (
        (
            "strong",
            {"28": 0.10, "29": 0.40, "30": 0.35, "31": 0.15},
            {},
            "2026-07-26T00:00:00Z",
            "UP",
        ),
        (
            "weak_skew",
            {"28": 0.24, "29": 0.50, "30": 0.16, "31": 0.10},
            {"direction_skew_deadband": 0.05},
            "2026-07-26T00:00:00Z",
            "NEUTRAL",
        ),
        (
            "high_entropy",
            {"28": 0.25, "29": 0.27, "30": 0.26, "31": 0.22},
            {"direction_entropy_max": 0.90},
            "2026-07-26T00:00:00Z",
            "NEUTRAL",
        ),
        (
            "low_direction_mass",
            {"28": 0.05, "29": 0.85, "30": 0.06, "31": 0.04},
            {
                "direction_skew_deadband": 0.01,
                "direction_min_side_mass": 0.20,
            },
            "2026-07-26T00:00:00Z",
            "NEUTRAL",
        ),
        (
            "stale",
            {"28": 0.10, "29": 0.40, "30": 0.35, "31": 0.15},
            {},
            "2026-07-26T12:00:00Z",
            "NEUTRAL",
        ),
    ),
)
def test_all_probability_stages_have_identical_confidence_gates(
    stage: str,
    case: str,
    probabilities: dict[str, float],
    policy_overrides: dict[str, float],
    at_utc: str,
    expected: str,
) -> None:
    raw = _envelope().to_dict()
    raw.update({
        "stage": stage,
        "checkpoint_id": f"{stage}-{case}",
        "probabilities": probabilities,
        "posterior_version": f"{stage}-{case}-v1",
        "posterior_sha256": (
            f"{len(stage) + len(case):064x}"[-64:]
        ),
    })
    policy = TargetBasketPolicy.from_mapping({
        "clip_max_notional_usdc": 10.0,
        "center_lead_min_probability": 0.01,
        "direction_skew_deadband": 0.01,
        "direction_entropy_max": 1.0,
        "direction_min_side_mass": 0.01,
        **policy_overrides,
    })

    result = StageProbabilityBiasContract.materialize(
        ProbabilityEnvelope.from_mapping(raw),
        policy,
        at_utc=at_utc,
    )

    assert result.stage == stage
    assert result.direction == expected
