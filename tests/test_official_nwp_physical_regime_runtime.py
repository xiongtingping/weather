from __future__ import annotations

import pytest

from official_nwp_physical_regime_runtime import (
    aggregate_official_nwp_physical_regime,
    derive_official_nwp_physical_regime,
)


def test_temperature_fields_alone_never_create_weather_regime() -> None:
    cold = derive_official_nwp_physical_regime({"temperature_2m_c": 20.0, "temperature_850_c": 10.0})
    hot = derive_official_nwp_physical_regime({"temperature_2m_c": 40.0, "temperature_850_c": 30.0})

    assert cold["basin_regime"] == "unknown"
    assert hot["basin_regime"] == "unknown"
    assert cold["boundary"]["temperature_fields_consumed"] is False
    assert hot["boundary"]["temperature_curve_weather_type_inference_forbidden"] is True


def test_direct_humidity_and_wind_fields_create_conservative_regimes() -> None:
    moist = derive_official_nwp_physical_regime({
        "relative_humidity_850_pct": 80.0,
        "relative_humidity_700_pct": 90.0,
        "u_wind_850_mps": 1.0,
        "v_wind_850_mps": 1.0,
        "u_wind_700_mps": 2.0,
        "v_wind_700_mps": 1.0,
    })
    ventilated = derive_official_nwp_physical_regime({
        "relative_humidity_850_pct": 55.0,
        "relative_humidity_700_pct": 50.0,
        "u_wind_850_mps": 8.0,
        "v_wind_850_mps": 1.0,
        "u_wind_700_mps": 9.0,
        "v_wind_700_mps": 1.0,
    })

    assert moist["basin_regime"] == "moist_column_cloud_favorable"
    assert moist["cloud_regime_semantics"] == "direct_model_forecast_not_observed_weather"
    assert moist["precipitation_regime"].startswith("unknown_")
    assert ventilated["basin_regime"] == "well_ventilated"

    daily = aggregate_official_nwp_physical_regime([moist, moist, ventilated])
    assert daily["basin_regime"] == "moist_column_cloud_favorable"
    assert daily["known_point_share"] == 1.0


def test_accumulated_precipitation_is_differenced_across_target_curve() -> None:
    points = [
        derive_official_nwp_physical_regime({"total_cloud_cover_pct": 80, "total_precipitation_mm": 2.0, "total_precipitation_accumulation_hours": 12, "convective_available_potential_energy_j_per_kg": 100}),
        derive_official_nwp_physical_regime({"total_cloud_cover_pct": 85, "total_precipitation_mm": 2.0, "total_precipitation_accumulation_hours": 13, "convective_available_potential_energy_j_per_kg": 100}),
        derive_official_nwp_physical_regime({"total_cloud_cover_pct": 90, "total_precipitation_mm": 2.7, "total_precipitation_accumulation_hours": 14, "convective_available_potential_energy_j_per_kg": 100}),
    ]
    daily = aggregate_official_nwp_physical_regime(points)
    assert daily["target_window_precipitation_increment_mm"] == pytest.approx(0.7)
    assert daily["forecast_weather_regime"] == "wet_precipitation"


def test_cape_alone_never_claims_convection_occurrence() -> None:
    point = derive_official_nwp_physical_regime({
        "total_cloud_cover_pct": 40,
        "total_precipitation_mm": 0,
        "total_precipitation_accumulation_hours": 4,
        "convective_available_potential_energy_j_per_kg": 1800,
    })
    daily = aggregate_official_nwp_physical_regime([point, point])
    assert point["convection_regime"] == "convective_environment_favorable_not_occurrence"
    assert daily["forecast_weather_regime"] != "convection_or_showers"


def test_accumulation_differencing_is_isolated_per_provider() -> None:
    ecmwf_0 = derive_official_nwp_physical_regime({
        "total_precipitation_mm": 0.0,
        "total_precipitation_accumulation_hours": 6,
        "total_cloud_cover_pct": 5,
    })
    ecmwf_1 = derive_official_nwp_physical_regime({
        "total_precipitation_mm": 0.0,
        "total_precipitation_accumulation_hours": 9,
        "total_cloud_cover_pct": 5,
    })
    gfs_0 = derive_official_nwp_physical_regime({
        "total_precipitation_mm": 0.0,
        "total_precipitation_accumulation_hours": 1,
        "total_cloud_cover_pct": 5,
    })
    # This is a reset to zero in a second provider, not negative rainfall in
    # the first provider's curve.
    gfs_1 = derive_official_nwp_physical_regime({
        "total_precipitation_mm": 0.0,
        "total_precipitation_accumulation_hours": 4,
        "total_cloud_cover_pct": 5,
    })
    for row, provider, valid in (
        (ecmwf_0, "ecmwf", "2026-07-21T06:00:00Z"),
        (ecmwf_1, "ecmwf", "2026-07-21T09:00:00Z"),
        (gfs_0, "gfs", "2026-07-21T01:00:00Z"),
        (gfs_1, "gfs", "2026-07-21T04:00:00Z"),
    ):
        row["_provider_id"] = provider
        row["_valid_time_utc"] = valid

    daily = aggregate_official_nwp_physical_regime([ecmwf_0, ecmwf_1, gfs_0, gfs_1])

    assert daily["target_window_precipitation_increment_mm"] == 0.0
    assert daily["accumulation_sequence_invalid"] is False
