from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.future_temperature_curve import (
    RootFutureTemperatureCurve,
    build_root_future_temperature_curve,
    compile_future_curve_features,
)


def _curve() -> RootFutureTemperatureCurve:
    return build_root_future_temperature_curve({
        "station_id": "RKPK",
        "event_date": "2026-08-01",
        "settlement_timezone": "Asia/Seoul",
        "root_checkpoint_id": "p0m-root",
        "root_probability_sha256": "1" * 64,
        "available_at_utc": "2026-08-01T05:00:00Z",
        "source_snapshot_sha256": "2" * 64,
        "independence_family_ids": ["NOAA", "ECMWF"],
        "points": [
            {"valid_time_utc": "2026-08-01T06:00:00Z", "temperature_c": 29.0},
            {"valid_time_utc": "2026-08-01T07:00:00Z", "temperature_c": 31.0},
            {"valid_time_utc": "2026-08-01T08:00:00Z", "temperature_c": 33.0},
            {"valid_time_utc": "2026-08-01T09:00:00Z", "temperature_c": 32.0},
        ],
    })


def test_curve_is_content_addressed_and_compiles_future_peak_features() -> None:
    curve = _curve()
    assert RootFutureTemperatureCurve.from_mapping(curve.to_dict()) == curve
    features = compile_future_curve_features(
        curve,
        as_of_utc="2026-08-01T05:30:00Z",
        running_high_c=30.0,
    )
    assert features["future_point_count"] == 4
    assert features["predicted_peak_temperature_c"] == 33.0
    assert features["predicted_peak_time_utc"] == "2026-08-01T08:00:00.000Z"
    assert features["minutes_to_predicted_peak"] == 150.0
    assert features["predicted_peak_remaining_warming_c"] == 3.0


def test_curve_rejects_one_family_hash_drift_and_future_arrival() -> None:
    payload = _curve().to_dict()
    payload["independence_family_ids"] = ["ECMWF"]
    payload.pop("content_sha256")
    with pytest.raises(ContractError, match="independent_families_insufficient"):
        build_root_future_temperature_curve(payload)

    tampered = deepcopy(_curve().to_dict())
    tampered["points"][0]["temperature_c"] = 99.0
    with pytest.raises(ContractError, match="content_sha256_mismatch"):
        RootFutureTemperatureCurve.from_mapping(tampered)

    with pytest.raises(ContractError, match="not_strict_asof"):
        compile_future_curve_features(
            _curve(),
            as_of_utc="2026-08-01T04:59:59Z",
            running_high_c=30.0,
        )


def test_curve_rejects_wrong_local_day_and_large_gap() -> None:
    payload = _curve().to_dict()
    payload.pop("content_sha256")
    payload["points"][-1]["valid_time_utc"] = "2026-08-02T09:00:00Z"
    with pytest.raises(ContractError, match="gap_invalid|event_date_mismatch"):
        build_root_future_temperature_curve(payload)
