from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.same_day_climatology import (
    SameDayClimatologyArtifact,
    build_same_day_climatology_artifact,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _sample(
    truth_id: str,
    local_date: str,
    temperature: float,
    *,
    role: str = "SAME_DAY_CORE",
    truth_sha: str = SHA_A,
) -> dict[str, object]:
    return {
        "truth_id": truth_id,
        "station_id": "EDDM",
        "local_date": local_date,
        "official_tmax_c": temperature,
        "settled_at_utc": f"{local_date}T22:00:00Z",
        "settlement_closed": True,
        "source_lane": "AWC",
        "settlement_definition": "daily_high_c",
        "settlement_timezone": "Europe/Berlin",
        "sample_role": role,
        "trend_adjustment_c": 0.1,
        "station_regime_adjustment_c": -0.1,
        "weight": 1.0 if role == "SAME_DAY_CORE" else 0.25,
        "truth_content_sha256": truth_sha,
    }


def _artifact(
    *,
    samples: list[dict[str, object]] | None = None,
):
    return build_same_day_climatology_artifact(
        station_id="EDDM",
        settlement_timezone="Europe/Berlin",
        settlement_definition="daily_high_c",
        target_year=2026,
        calendar_day="07-30",
        truth_samples=samples
        or [
            _sample("truth-2022", "2022-07-30", 33.0, truth_sha=SHA_A),
            _sample("truth-2023", "2023-07-30", 35.0, truth_sha=SHA_B),
            _sample(
                "truth-2024-adjacent",
                "2024-07-29",
                34.0,
                role="ADJACENT_DAY_SHRINKAGE",
                truth_sha=SHA_C,
            ),
        ],
        bucket_centers_c={
            "33": 33.0,
            "34": 34.0,
            "35": 35.0,
            "36": 36.0,
        },
        kde_bandwidth_c=1.0,
        activation_id="same-day-activation-1",
        built_at_utc="2026-07-29T00:00:00Z",
        valid_from_utc="2026-01-01T00:00:00Z",
        valid_until_utc="2027-01-01T00:00:00Z",
    )


def test_builds_content_addressed_immutable_artifact_deterministically() -> None:
    samples = [
        _sample("truth-2022", "2022-07-30", 33.0, truth_sha=SHA_A),
        _sample("truth-2023", "2023-07-30", 35.0, truth_sha=SHA_B),
        _sample(
            "truth-adjacent",
            "2024-07-29",
            34.0,
            role="ADJACENT_DAY_SHRINKAGE",
            truth_sha=SHA_C,
        ),
    ]
    first = _artifact(samples=samples)
    second = _artifact(samples=list(reversed(samples)))

    assert first.content_sha256 == second.content_sha256
    assert first.artifact_id == second.artifact_id
    assert first.same_day_truth_ids == ("truth-2022", "truth-2023")
    assert first.adjacent_truth_ids == ("truth-adjacent",)
    assert sum(first.bucket_probabilities.values()) == pytest.approx(1.0)
    with pytest.raises(TypeError):
        first.bucket_probabilities["33"] = 0.5  # type: ignore[index]
    assert SameDayClimatologyArtifact.from_mapping(
        first.to_dict()
    ).content_sha256 == first.content_sha256


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda sample: sample.update(local_date="2026-07-30"),
            "same_day_target_year_or_future_truth_leakage",
        ),
        (
            lambda sample: sample.update(settlement_closed=False),
            "same_day_truth_not_settled",
        ),
        (
            lambda sample: sample.update(settled_at_utc="2026-01-01T00:00:00Z"),
            "same_day_future_settlement_leakage",
        ),
        (
            lambda sample: sample.update(local_date="2023-07-29"),
            "same_day_core_calendar_day_mismatch",
        ),
        (
            lambda sample: sample.update(
                sample_role="ADJACENT_DAY_SHRINKAGE",
                local_date="2023-06-01",
            ),
            "same_day_adjacent_sample_out_of_window",
        ),
        (
            lambda sample: sample.update(settlement_timezone="UTC"),
            "same_day_truth_timezone_mismatch",
        ),
        (
            lambda sample: sample.update(model_run_id="forbidden"),
            "same_day_truth_sample_forbidden_historical_input:model_run_id",
        ),
    ],
)
def test_rejects_leakage_mislabelling_and_historical_model_mix(
    mutation, error: str
) -> None:
    sample = _sample("truth-2023", "2023-07-30", 35.0)
    mutation(sample)
    with pytest.raises(ContractError, match=error):
        _artifact(samples=[sample])


def test_rejects_duplicate_truth_and_content_hash_tamper() -> None:
    duplicate = [
        _sample("truth-duplicate", "2022-07-30", 33.0),
        _sample("truth-duplicate", "2023-07-30", 35.0, truth_sha=SHA_B),
    ]
    with pytest.raises(ContractError, match="same_day_duplicate_truth_id"):
        _artifact(samples=duplicate)

    raw = deepcopy(_artifact().to_dict())
    raw["center_c"] = float(raw["center_c"]) + 0.5
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    raw["content_sha256"] = content_sha256(identity)
    with pytest.raises(ContractError, match="same_day_center_mismatch"):
        SameDayClimatologyArtifact.from_mapping(raw)


def test_rejects_unlabelled_adjacent_truth_and_non_monotonic_bucket_map() -> None:
    with pytest.raises(ContractError, match="same_day_core_calendar_day_mismatch"):
        _artifact(samples=[_sample("wrong-role", "2023-07-29", 34.0)])

    with pytest.raises(
        ContractError, match="same_day_bucket_centers_not_strictly_increasing"
    ):
        build_same_day_climatology_artifact(
            station_id="EDDM",
            settlement_timezone="Europe/Berlin",
            settlement_definition="daily_high_c",
            target_year=2026,
            calendar_day="07-30",
            truth_samples=[_sample("truth-2023", "2023-07-30", 35.0)],
            bucket_centers_c={"35": 35.0, "34": 34.0},
            kde_bandwidth_c=1.0,
            activation_id="activation",
            built_at_utc="2026-07-29T00:00:00Z",
            valid_from_utc="2026-01-01T00:00:00Z",
            valid_until_utc="2027-01-01T00:00:00Z",
        )
