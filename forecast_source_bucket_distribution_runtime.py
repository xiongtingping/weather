"""Convert external forecast sources into the exact market bucket grid."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "forecast_source_bucket_distribution_v1"
EPS = 1e-12


def _float(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def convert_temperature(value: float, *, from_unit: str, to_unit: str) -> float:
    source = str(from_unit or "C").upper()
    target = str(to_unit or "C").upper()
    if source == target:
        return float(value)
    if source == "F" and target == "C":
        return (float(value) - 32.0) * 5.0 / 9.0
    if source == "C" and target == "F":
        return float(value) * 9.0 / 5.0 + 32.0
    raise ValueError("unsupported_temperature_unit_conversion")


def _bucket_rows(market_event: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in market_event.get("bucket_schema") or []:
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("label") or "").strip()
        if not label:
            continue
        rows.append(
            {
                "label": label,
                "lower": _float(raw.get("lower")),
                "upper": _float(raw.get("upper")),
                "lower_inclusive": bool(raw.get("lower_inclusive", True)),
                "upper_inclusive": bool(raw.get("upper_inclusive", False)),
                "unit": str(raw.get("unit") or market_event.get("temperature_unit") or "C").upper(),
            }
        )
    rows.sort(key=lambda row: float("-inf") if row["lower"] is None else float(row["lower"]))
    return rows


def bucket_index_for_temperature(market_event: Mapping[str, Any], value: float, *, unit: str = "C") -> int | None:
    rows = _bucket_rows(market_event)
    target_unit = str(market_event.get("temperature_unit") or (rows[0]["unit"] if rows else "C")).upper()
    converted = convert_temperature(float(value), from_unit=unit, to_unit=target_unit)
    for index, row in enumerate(rows):
        low = row["lower"]
        high = row["upper"]
        low_ok = low is None or converted > low or (row["lower_inclusive"] and converted == low)
        high_ok = high is None or converted < high or (row["upper_inclusive"] and converted == high)
        if low_ok and high_ok:
            return index
    return None


def _normalize(values: Sequence[float]) -> list[float]:
    cleaned = [max(0.0, float(value)) for value in values]
    total = sum(cleaned)
    if total <= EPS:
        return [1.0 / len(cleaned) for _ in cleaned] if cleaned else []
    return [value / total for value in cleaned]


def member_peak_distribution(
    market_event: Mapping[str, Any],
    member_daily_peaks: Sequence[Any],
    *,
    member_unit: str = "C",
    prior_strength: float = 0.25,
) -> dict[str, Any]:
    rows = _bucket_rows(market_event)
    counts = [max(0.0, float(prior_strength)) for _ in rows]
    valid = 0
    outside = 0
    for raw_value in member_daily_peaks:
        value = _float(raw_value)
        if value is None:
            continue
        index = bucket_index_for_temperature(market_event, value, unit=member_unit)
        if index is None:
            outside += 1
            continue
        counts[index] += 1.0
        valid += 1
    probabilities = _normalize(counts)
    return {
        "schema": SCHEMA_VERSION,
        "state": "available" if valid else "unavailable",
        "state_reason": "" if valid else "member_daily_peaks_missing_or_outside_bucket_grid",
        "distribution_source": "member_daily_peak_frequency_with_symmetric_prior",
        "bucket_probabilities": {row["label"]: probability for row, probability in zip(rows, probabilities)},
        "member_count_input": len(member_daily_peaks),
        "member_count_valid": valid,
        "member_count_outside_grid": outside,
        "prior_strength_per_bucket": max(0.0, float(prior_strength)),
    }


def _normal_cdf(value: float | None, mean: float, sigma: float) -> float:
    if value is None:
        return 0.0
    z = (float(value) - mean) / max(0.05, float(sigma))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def deterministic_peak_distribution(
    market_event: Mapping[str, Any],
    forecast_peak: float,
    *,
    forecast_unit: str = "C",
    bias: float = 0.0,
    residual_sigma: float = 1.5,
) -> dict[str, Any]:
    rows = _bucket_rows(market_event)
    target_unit = str(market_event.get("temperature_unit") or (rows[0]["unit"] if rows else "C")).upper()
    peak = convert_temperature(float(forecast_peak), from_unit=forecast_unit, to_unit=target_unit) + float(bias)
    sigma = max(0.05, float(residual_sigma))
    probabilities = []
    for row in rows:
        low_cdf = 0.0 if row["lower"] is None else _normal_cdf(row["lower"], peak, sigma)
        high_cdf = 1.0 if row["upper"] is None else _normal_cdf(row["upper"], peak, sigma)
        probabilities.append(max(0.0, high_cdf - low_cdf))
    probabilities = _normalize(probabilities)
    return {
        "schema": SCHEMA_VERSION,
        "state": "available" if rows else "unavailable",
        "state_reason": "" if rows else "market_bucket_schema_missing",
        "distribution_source": "bias_corrected_peak_normal_residual",
        "forecast_peak_canonical_unit": peak,
        "forecast_unit": target_unit,
        "bias": float(bias),
        "residual_sigma": sigma,
        "bucket_probabilities": {row["label"]: probability for row, probability in zip(rows, probabilities)},
    }


def explicit_bucket_distribution(
    market_event: Mapping[str, Any],
    values: Mapping[str, Any],
) -> dict[str, Any]:
    rows = _bucket_rows(market_event)
    probabilities = []
    matched = 0
    for row in rows:
        value = _float(values.get(row["label"]))
        probabilities.append(max(0.0, value or 0.0))
        matched += int(value is not None)
    probabilities = _normalize(probabilities)
    return {
        "schema": SCHEMA_VERSION,
        "state": "available" if matched and sum(probabilities) > 0 else "unavailable",
        "state_reason": "" if matched else "explicit_bucket_distribution_does_not_match_market_grid",
        "distribution_source": "explicit_bucket_distribution",
        "bucket_probabilities": {row["label"]: probability for row, probability in zip(rows, probabilities)},
        "matched_bucket_count": matched,
    }


def build_forecast_source_bucket_distribution(
    market_event: Mapping[str, Any],
    source_forecast: Mapping[str, Any],
) -> dict[str, Any]:
    explicit = source_forecast.get("bucket_probabilities")
    if isinstance(explicit, Mapping):
        return explicit_bucket_distribution(market_event, explicit)
    members = source_forecast.get("member_daily_peaks_c")
    if isinstance(members, Sequence) and not isinstance(members, (str, bytes, bytearray)):
        return member_peak_distribution(
            market_event,
            members,
            member_unit=str(source_forecast.get("temperature_unit") or "C"),
            prior_strength=float(source_forecast.get("member_prior_strength") or 0.25),
        )
    peak = _float(source_forecast.get("forecast_peak_c"))
    if peak is None:
        return {
            "schema": SCHEMA_VERSION,
            "state": "unavailable",
            "state_reason": "forecast_distribution_members_and_peak_missing",
            "bucket_probabilities": {},
        }
    return deterministic_peak_distribution(
        market_event,
        peak,
        forecast_unit=str(source_forecast.get("temperature_unit") or "C"),
        bias=float(_float(source_forecast.get("bias")) or 0.0),
        residual_sigma=float(_float(source_forecast.get("residual_sigma")) or 1.5),
    )


__all__ = [
    "SCHEMA_VERSION",
    "bucket_index_for_temperature",
    "build_forecast_source_bucket_distribution",
    "convert_temperature",
    "deterministic_peak_distribution",
    "explicit_bucket_distribution",
    "member_peak_distribution",
]

