from __future__ import annotations

import pytest

from physical_state_bayes_v6.observation_adapter import build_canonical_emission_input


def _event(event_id: str, valid_time: str, temperature: float, *, report_type: str = "METAR", cloud: str = "FEW040", weather: str = "", visibility: float | None = None):
    return {
        "canonical_observation_event_id": event_id,
        "valid_time": valid_time,
        "report_type": report_type,
        "parsed_observation": {
            "temperature_c": temperature,
            "dewpoint_c": 23.0,
            "cloud_text": cloud,
            "weather_text": weather,
            "visibility_m": visibility,
        },
    }


def test_adapter_uses_current_event_once_against_strict_pre_event_history() -> None:
    current = _event(
        "event-current",
        "2026-07-15T11:30:00Z",
        36.0,
        cloud="FEW040TCU FEW040",
    )
    previous = _event("event-previous", "2026-07-15T11:00:00Z", 37.0)

    result = build_canonical_emission_input(
        canonical_event=current,
        pre_event_history=[previous],
    )

    assert result.features.current_step_rate_c_per_hour == -2.0
    assert result.features.current_step_rate_interval_c_per_hour == (-4.0, 0.0)
    covariance = result.features.continuous_measurement_covariance
    assert covariance[0] == pytest.approx((2.0 / 3.0, 1.0 / 6.0))
    assert covariance[1] == pytest.approx((1.0 / 6.0, 1.0 / 6.0))
    assert result.features.dewpoint_depression_c == 13.0
    assert result.features.dewpoint_depression_interval_c == (12.0, 14.0)
    assert result.cb_present is False
    assert result.tcu_present is True
    assert result.convective_evidence_kind == "tcu"
    assert result.cloud_suppressed is False
    assert result.features.weather_signature == "cloud=clear_or_few|weather=convective|convective=1"
    assert result.is_regular_metar is True


def test_adapter_rejects_current_event_inside_pre_event_history() -> None:
    current = _event("event-current", "2026-07-15T11:30:00Z", 36.0)
    with pytest.raises(ValueError, match="current_event_in_pre_event_history"):
        build_canonical_emission_input(
            canonical_event=current,
            pre_event_history=[current],
        )


def test_first_event_has_missing_step_rate_instead_of_zero_fill() -> None:
    result = build_canonical_emission_input(
        canonical_event=_event(
            "event-speci",
            "2026-07-15T05:15:00Z",
            31.0,
            report_type="SPECI",
            cloud="BKN020",
            weather="-RA",
        ),
        pre_event_history=[],
    )

    assert result.features.current_step_rate_c_per_hour is None
    assert result.features.current_step_rate_interval_c_per_hour is None
    assert result.features.continuous_measurement_covariance[0][0] == 0.0
    assert result.features.continuous_measurement_covariance[1][1] == pytest.approx(1.0 / 6.0)
    assert result.cloud_suppressed is True
    assert result.precipitation_present is True
    assert result.is_regular_metar is False


def test_adapter_carries_fog_and_low_visibility_into_physical_feature_vector() -> None:
    result = build_canonical_emission_input(
        canonical_event=_event(
            "event-fog", "2026-07-16T22:00:00Z", 24.0, weather="BR", visibility=1200.0
        ),
        pre_event_history=[],
    )

    assert result.features.fog_or_mist_present is True
    assert result.features.visibility_m == 1200.0
