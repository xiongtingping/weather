"""Derive conservative NWP regime fields from direct physical model fields.

Temperature is deliberately excluded from this module. Regime labels use only
direct provider physical fields carried by immutable official GRIB snapshots.
They are forecast context, never observations or settlement facts.
"""

from __future__ import annotations

from collections import Counter
import math
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "official_nwp_physical_regime_v2"
PHYSICAL_INPUT_FIELDS = (
    "relative_humidity_850_pct",
    "relative_humidity_700_pct",
    "u_wind_850_mps",
    "v_wind_850_mps",
    "u_wind_700_mps",
    "v_wind_700_mps",
    "geopotential_height_500_m",
    "geopotential_height_850_m",
    "total_cloud_cover_pct",
    "total_precipitation_mm",
    "total_precipitation_accumulation_hours",
    "convective_precipitation_mm",
    "convective_precipitation_accumulation_hours",
    "convective_available_potential_energy_j_per_kg",
    "boundary_layer_height_m",
    "boundary_layer_gust_mps",
    "boundary_layer_momentum_transfer_coefficient",
    "eastward_turbulent_surface_stress_n_m2_s",
    "northward_turbulent_surface_stress_n_m2_s",
)
FORBIDDEN_CLASSIFICATION_FIELDS = (
    "temperature_2m_c",
    "temperature_850_c",
    "temperature_700_c",
)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _wind_speed(features: Mapping[str, Any], level: str) -> float | None:
    u = _finite(features.get(f"u_wind_{level}_mps"))
    v = _finite(features.get(f"v_wind_{level}_mps"))
    if u is None or v is None:
        return None
    return math.hypot(u, v)


def derive_official_nwp_physical_regime(features: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one official forecast point without consuming temperature."""

    values = dict(features) if isinstance(features, Mapping) else {}
    rh850 = _finite(values.get("relative_humidity_850_pct"))
    rh700 = _finite(values.get("relative_humidity_700_pct"))
    wind850 = _wind_speed(values, "850")
    wind700 = _wind_speed(values, "700")
    cloud = _finite(values.get("total_cloud_cover_pct"))
    precip = _finite(values.get("total_precipitation_mm"))
    precip_hours = _finite(values.get("total_precipitation_accumulation_hours"))
    convective_precip = _finite(values.get("convective_precipitation_mm"))
    convective_precip_hours = _finite(values.get("convective_precipitation_accumulation_hours"))
    cape = _finite(values.get("convective_available_potential_energy_j_per_kg"))
    convective_base = _finite(values.get("convective_cloud_base_msl_m"))
    convective_top = _finite(values.get("convective_cloud_top_msl_m"))
    pbl_height = _finite(values.get("boundary_layer_height_m"))
    pbl_gust = _finite(values.get("boundary_layer_gust_mps"))
    pbl_transfer = _finite(values.get("boundary_layer_momentum_transfer_coefficient"))
    humidity = [item for item in (rh850, rh700) if item is not None]
    winds = [item for item in (wind850, wind700) if item is not None]
    evidence = {
        key: value
        for key, value in {
            "relative_humidity_850_pct": rh850,
            "relative_humidity_700_pct": rh700,
            "wind_speed_850_mps": wind850,
            "wind_speed_700_mps": wind700,
            "geopotential_height_500_m": _finite(values.get("geopotential_height_500_m")),
            "geopotential_height_850_m": _finite(values.get("geopotential_height_850_m")),
            "total_cloud_cover_pct": cloud,
            "total_precipitation_mm": precip,
            "total_precipitation_accumulation_hours": precip_hours,
            "convective_precipitation_mm": convective_precip,
            "convective_precipitation_accumulation_hours": convective_precip_hours,
            "convective_available_potential_energy_j_per_kg": cape,
            "convective_cloud_base_msl_m": convective_base,
            "convective_cloud_top_msl_m": convective_top,
            "boundary_layer_height_m": pbl_height,
            "boundary_layer_gust_mps": pbl_gust,
            "boundary_layer_momentum_transfer_coefficient": pbl_transfer,
            "eastward_turbulent_surface_stress_n_m2_s": _finite(values.get("eastward_turbulent_surface_stress_n_m2_s")),
            "northward_turbulent_surface_stress_n_m2_s": _finite(values.get("northward_turbulent_surface_stress_n_m2_s")),
        }.items()
        if value is not None
    }
    humidity_mean = sum(humidity) / len(humidity) if humidity else None
    wind_mean = sum(winds) / len(winds) if winds else None
    wind_max = max(winds) if winds else None

    label = "unknown"
    state = "insufficient_physical_fields"
    confidence = 0.0
    basis = "upper_air_humidity_and_wind_missing"
    if len(humidity) >= 2 and rh850 is not None and rh700 is not None and rh850 >= 75.0 and rh700 >= 75.0:
        label = "moist_column_cloud_favorable"
        state = "available"
        confidence = 0.78
        basis = "rh850_and_rh700_both_at_least_75pct"
    elif wind_max is not None and wind_max >= 8.0:
        label = "well_ventilated"
        state = "available"
        confidence = 0.76
        basis = "upper_air_wind_max_at_least_8mps"
    elif wind_mean is not None and wind_mean >= 4.0:
        label = "moderate_ventilation"
        state = "available"
        confidence = 0.68
        basis = "mean_850_700_wind_at_least_4mps"
    elif humidity_mean is not None and humidity_mean <= 45.0 and winds:
        label = "dry_air_background"
        state = "available"
        confidence = 0.64
        basis = "mean_850_700_relative_humidity_at_most_45pct"
    elif wind_mean is not None and wind_mean < 2.5 and humidity_mean is not None:
        label = "weak_ventilation_background"
        state = "available"
        confidence = 0.60
        basis = "mean_850_700_wind_below_2_5mps_with_humidity_present"
    elif wind_mean is not None and humidity_mean is not None:
        label = "mixed_physical_background"
        state = "available"
        confidence = 0.50
        basis = "upper_air_humidity_and_wind_present_without_strong_regime_gate"

    if cloud is None:
        cloud_regime = "unknown_direct_cloud_field_missing"
    elif cloud >= 70.0:
        cloud_regime = "forecast_overcast_direct"
    elif cloud <= 30.0:
        cloud_regime = "forecast_low_cloud_fraction_direct"
    else:
        cloud_regime = "forecast_partial_cloud_direct"
    if precip is None or precip_hours is None or precip_hours <= 0.0:
        precipitation_regime = "unknown_accumulation_window_missing"
    elif precip <= 0.01:
        precipitation_regime = "no_accumulated_precipitation_since_run"
    else:
        precipitation_regime = "accumulated_precipitation_present_window_not_point_localized"
    convective_geometry = convective_base is not None and convective_top is not None and convective_top > convective_base > 0.0
    if convective_precip is not None and convective_precip_hours is not None and convective_precip_hours > 0.0:
        convection_regime = "accumulated_convective_precipitation_present" if convective_precip > 0.01 else "no_accumulated_convective_precipitation_since_run"
    elif convective_geometry:
        convection_regime = "forecast_convective_cloud_geometry_direct"
    elif cape is not None:
        convection_regime = "convective_environment_favorable_not_occurrence" if cape >= 500.0 else "low_convective_energy_direct"
    else:
        convection_regime = "unknown_direct_convection_field_missing"
    if pbl_height is not None:
        boundary_layer_regime = "direct_boundary_layer_height_available"
    elif any(value is not None for value in (pbl_gust, pbl_transfer, evidence.get("eastward_turbulent_surface_stress_n_m2_s"), evidence.get("northward_turbulent_surface_stress_n_m2_s"))):
        boundary_layer_regime = "direct_boundary_layer_momentum_fields_available"
    else:
        boundary_layer_regime = "unknown_direct_boundary_layer_field_missing"

    return {
        "schema": SCHEMA_VERSION,
        "state": state,
        "basin_regime": label,
        "basin_regime_confidence": confidence,
        "basin_regime_source": "official_nwp_direct_physical_fields",
        "classification_basis": basis,
        "physical_evidence": evidence,
        "input_field_count": len(evidence),
        "cloud_regime": cloud_regime,
        "precipitation_regime": precipitation_regime,
        "convection_regime": convection_regime,
        "boundary_layer_regime": boundary_layer_regime,
        "cloud_regime_semantics": "direct_model_forecast_not_observed_weather",
        "boundary": {
            "temperature_fields_consumed": False,
            "temperature_curve_weather_type_inference_forbidden": True,
            "direct_physical_fields_only": True,
            "forecast_not_observation": True,
            "not_settlement_actual": True,
        },
    }


def aggregate_official_nwp_physical_regime(point_regimes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate exact point classifications; unknown remains explicit."""

    rows = [dict(row) for row in point_regimes if isinstance(row, Mapping)]
    labels = [str(row.get("basin_regime") or "unknown") for row in rows]
    known = [label for label in labels if label != "unknown"]
    counts = Counter(labels)
    if not known:
        label, confidence, state = "unknown", 0.0, "insufficient_physical_fields"
    else:
        known_counts = Counter(known)
        label, count = sorted(known_counts.items(), key=lambda item: (-item[1], item[0]))[0]
        confidences = [
            float(row.get("basin_regime_confidence") or 0.0)
            for row in rows
            if str(row.get("basin_regime") or "unknown") == label
        ]
        coverage = len(known) / len(rows) if rows else 0.0
        dominance = count / len(known)
        confidence = min(0.85, (sum(confidences) / len(confidences)) * coverage * (0.5 + 0.5 * dominance))
        state = "available"
    cloud_values = [_finite(_mapping(row.get("physical_evidence")).get("total_cloud_cover_pct")) for row in rows]
    cloud_values = [value for value in cloud_values if value is not None]
    cape_values = [_finite(_mapping(row.get("physical_evidence")).get("convective_available_potential_energy_j_per_kg")) for row in rows]
    cape_values = [value for value in cape_values if value is not None]

    def interval_increment(field: str, hours_field: str) -> tuple[float, int, bool]:
        total, count, invalid = 0.0, 0, False

        # Accumulation counters reset at every provider/run.  The old
        # implementation differentiated the concatenated multi-provider list,
        # so the last point of ECMWF could be subtracted from the first point
        # of GFS (or vice versa).  That manufactured precipitation increments
        # and let a source with a reset counter become a wet regime.
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            provider = str(row.get("_provider_id") or "__single_input__")
            groups.setdefault(provider, []).append(row)
        for group_rows in groups.values():
            ordered = sorted(
                group_rows,
                key=lambda row: str(row.get("_valid_time_utc") or ""),
            )
            previous_amount: float | None = None
            previous_hours: float | None = None
            for row in ordered:
                evidence = _mapping(row.get("physical_evidence"))
                amount, hours = _finite(evidence.get(field)), _finite(evidence.get(hours_field))
                if amount is None or hours is None:
                    continue
                if previous_amount is not None and previous_hours is not None and hours > previous_hours:
                    delta = amount - previous_amount
                    if delta < -0.01:
                        invalid = True
                    else:
                        total += max(0.0, delta)
                        count += 1
                previous_amount, previous_hours = amount, hours
        return total, count, invalid

    precipitation_increment, precipitation_interval_count, precipitation_invalid = interval_increment("total_precipitation_mm", "total_precipitation_accumulation_hours")
    convective_increment, convective_interval_count, convective_invalid = interval_increment("convective_precipitation_mm", "convective_precipitation_accumulation_hours")
    direct_convective_geometry_count = sum(str(row.get("convection_regime") or "") == "forecast_convective_cloud_geometry_direct" for row in rows)
    cloudy_share = sum(value >= 70.0 for value in cloud_values) / len(cloud_values) if cloud_values else 0.0
    clear_share = sum(value <= 30.0 for value in cloud_values) / len(cloud_values) if cloud_values else 0.0
    low_cape_share = sum(value < 200.0 for value in cape_values) / len(cape_values) if cape_values else 0.0
    if (convective_interval_count and convective_increment >= 0.1 and not convective_invalid) or direct_convective_geometry_count:
        forecast_weather_regime = "convection_or_showers"
    elif precipitation_interval_count and precipitation_increment >= 0.1 and not precipitation_invalid:
        forecast_weather_regime = "wet_precipitation"
    elif cloud_values and cloudy_share >= 0.5:
        forecast_weather_regime = "cloudy_stable"
    elif cloud_values and clear_share >= 0.5 and precipitation_interval_count and precipitation_increment <= 0.01 and cape_values and low_cape_share >= 0.5:
        forecast_weather_regime = "clear_dry"
    else:
        forecast_weather_regime = "unknown_forecast_regime"
    return {
        "schema": "official_nwp_daily_physical_regime_v2",
        "state": state,
        "basin_regime": label,
        "basin_regime_confidence": confidence,
        "basin_regime_source": "official_nwp_hourly_curve.physical_regime",
        "point_count": len(rows),
        "known_point_count": len(known),
        "unknown_point_count": len(rows) - len(known),
        "known_point_share": len(known) / len(rows) if rows else 0.0,
        "label_counts": dict(sorted(counts.items())),
        "forecast_weather_regime": forecast_weather_regime,
        "forecast_weather_regime_origin": "official_nwp_direct_physical_fields_v2",
        "direct_cloud_point_count": len(cloud_values),
        "direct_cape_point_count": len(cape_values),
        "target_window_precipitation_increment_mm": precipitation_increment if precipitation_interval_count and not precipitation_invalid else None,
        "target_window_precipitation_interval_count": precipitation_interval_count,
        "target_window_convective_precipitation_increment_mm": convective_increment if convective_interval_count and not convective_invalid else None,
        "target_window_convective_precipitation_interval_count": convective_interval_count,
        "accumulation_sequence_invalid": precipitation_invalid or convective_invalid,
        "boundary": {
            "temperature_fields_consumed": False,
            "temperature_curve_weather_type_inference_forbidden": True,
            "forecast_not_observation": True,
        },
    }


__all__ = [
    "FORBIDDEN_CLASSIFICATION_FIELDS",
    "PHYSICAL_INPUT_FIELDS",
    "SCHEMA_VERSION",
    "aggregate_official_nwp_physical_regime",
    "derive_official_nwp_physical_regime",
]
