"""Deterministic METAR/SPECI physical and temperature-path features.

The module is deliberately pure: the same canonical raw prefix produces the
same feature vector in live P1 compilation and strict-as-of OOS replay.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .future_temperature_curve import (
    RootFutureTemperatureCurve,
    compile_future_curve_features,
)


PATH_STATE_SCHEMA = "bayes_basket_metar_conditional_path_state_v1"
PHYSICAL_STATE_SCHEMA = "bayes_basket_metar_physical_state_v1"
MAX_RAW_METAR_BYTES = 16_384
PATH_MODEL_COEFFICIENT_KEYS = frozenset({
    "base_distribution_mean",
    "root_expected_remaining_warming",
    "momentum_c_per_hour",
    "acceleration_c_per_hour2",
    "drawdown_from_high_c",
    "dewpoint_depression_c",
    "cloud_cover_fraction",
    "precipitation_present",
    "wind_speed_mps",
    "remaining_hours",
})
HOURLY_CURVE_PATH_MODEL_COEFFICIENT_KEYS = frozenset({
    *PATH_MODEL_COEFFICIENT_KEYS,
    "predicted_peak_remaining_warming_c",
    "minutes_to_predicted_peak",
    "next_curve_delta_from_high_c",
    "future_curve_slope_c_per_hour",
})

# ``XX`` and ``//`` are both ICAO spellings of "dewpoint not available", and
# only the first was accepted.  One report in 866 -- OEJN 2026-08-18T01:00Z,
# ``32/// Q1003`` -- used the slash form, and because the parser raises
# rather than skipping, that single report took OEJN's whole trajectory
# training packet down with it: the station stopped emitting artifacts after
# 08-17 and sat at generation 1 while the rest of the fleet reached a median
# of 73.  No other station has produced one, which is why it read as a
# station-specific evidence gap rather than as a parser that could not spell.
_TEMP_DEW = re.compile(r"(?<![A-Z0-9])(M?\d{2})/(M?\d{2}|XX|//)(?![A-Z0-9])")
_WIND = re.compile(
    r"(?<![A-Z0-9])(?:\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)(?![A-Z0-9])"
)
_PRESSURE = re.compile(r"(?<![A-Z0-9])(?:Q(\d{4})|A(\d{4}))(?![A-Z0-9])")
# The convective suffixes are part of the cloud group, not a separate token:
# ``FEW050CB`` and ``SCT025TCU`` are one group each.  Without them the negative
# lookahead rejected the whole group, the report parsed as having no cloud
# layer at all, and ``complete`` went false -- on exactly the reports where the
# sky matters most.  Measured 2026-08-16 over 24622 August reports: 516 groups
# recovered (399 CB, 128 TCU), and because ``complete`` is judged on the last
# report of the prefix, one such report voided its whole training fold.  WMKK
# lost 24% of its reports this way, ZBAA 8%, MPMG 11% -- the convective
# stations, which is the wrong tail to be blind in.
#
# ``///`` in the height slot is an AUTO station reporting a layer it cannot
# measure the base of.  The amount -- which is the only part
# ``cloud_cover_fraction`` reads -- is still known, so the group is usable and
# refusing it discarded information the report actually carries.  EDDM's AUTO
# reports are the bulk of the residual: ``FEW///TCU``, ``SCT///CB``.
_CLOUD = re.compile(
    r"(?<![A-Z0-9])(FEW|SCT|BKN|OVC|VV)(?:\d{3}|///)(?:CB|TCU)?(?![A-Z0-9])"
)
_WEATHER = re.compile(
    r"^(?:[-+]|VC)?(?:MI|PR|BC|DR|BL|SH|TS|FZ)?"
    r"(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP)(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP)?$"
)


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_invalid")
    return result


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _signed_temperature(token: str) -> float:
    return float(-int(token[1:]) if token.startswith("M") else int(token))


@dataclass(frozen=True)
class MetarPhysicalState:
    dewpoint_c: float | None
    wind_speed_mps: float | None
    wind_gust_mps: float | None
    cloud_cover_fraction: float | None
    precipitation_present: bool
    pressure_hpa: float | None
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PHYSICAL_STATE_SCHEMA,
            "dewpoint_c": self.dewpoint_c,
            "wind_speed_mps": self.wind_speed_mps,
            "wind_gust_mps": self.wind_gust_mps,
            "cloud_cover_fraction": self.cloud_cover_fraction,
            "precipitation_present": self.precipitation_present,
            "pressure_hpa": self.pressure_hpa,
            "complete": self.complete,
        }


def parse_metar_physical_state(
    raw_text: str,
    *,
    expected_temperature_c: float,
) -> MetarPhysicalState:
    """Parse the bounded physical subset needed by the path model."""

    raw = str(raw_text or "").strip().upper()
    if not raw or len(raw.encode("utf-8")) > MAX_RAW_METAR_BYTES:
        raise ContractError("metar_path_raw_text_invalid")
    body = raw.split(" RMK ", 1)[0]
    temp_match = _TEMP_DEW.search(body)
    if temp_match is None:
        raise ContractError("metar_path_temperature_dewpoint_missing")
    parsed_temperature = _signed_temperature(temp_match.group(1))
    if abs(parsed_temperature - _finite(
        "metar_path_expected_temperature_c", expected_temperature_c
    )) > 0.51:
        raise ContractError("metar_path_temperature_mismatch")
    dewpoint = (
        None
        if temp_match.group(2) in {"XX", "//"}
        else _signed_temperature(temp_match.group(2))
    )

    wind_match = _WIND.search(body)
    wind_speed = None
    wind_gust = None
    if wind_match is not None:
        factor = 0.514444 if wind_match.group(3) == "KT" else 1.0
        wind_speed = float(wind_match.group(1)) * factor
        if wind_match.group(2) is not None:
            wind_gust = float(wind_match.group(2)) * factor

    cloud_tokens = _CLOUD.findall(body)
    cloud_fraction = None
    if cloud_tokens:
        fractions = {"FEW": 0.25, "SCT": 0.50, "BKN": 0.875, "OVC": 1.0, "VV": 1.0}
        cloud_fraction = max(fractions[token] for token in cloud_tokens)
    elif any(token in body.split() for token in ("CAVOK", "NCD", "NSC", "SKC", "CLR")):
        cloud_fraction = 0.0

    pressure_match = _PRESSURE.search(body)
    pressure = None
    if pressure_match is not None:
        if pressure_match.group(1) is not None:
            pressure = float(pressure_match.group(1))
        else:
            pressure = float(pressure_match.group(2)) / 100.0 * 33.8638866667

    precipitation = any(_WEATHER.fullmatch(token) for token in body.split())
    complete = all(
        value is not None
        for value in (dewpoint, wind_speed, cloud_fraction, pressure)
    )
    return MetarPhysicalState(
        dewpoint_c=dewpoint,
        wind_speed_mps=wind_speed,
        wind_gust_mps=wind_gust,
        cloud_cover_fraction=cloud_fraction,
        precipitation_present=precipitation,
        pressure_hpa=pressure,
        complete=complete,
    )


@dataclass(frozen=True)
class ConditionalMetarPathState:
    as_of_utc: str
    minutes_remaining_in_settlement_day: float
    running_high_c: float
    current_temperature_c: float
    drawdown_from_high_c: float
    momentum_c_per_hour: float
    acceleration_c_per_hour2: float
    dewpoint_depression_c: float
    cloud_cover_fraction: float
    precipitation_present: bool
    wind_speed_mps: float
    wind_gust_mps: float
    pressure_hpa: float
    root_expected_tmax_c: float
    root_expected_remaining_warming_c: float
    future_curve_sha256: str | None
    future_curve_complete: bool
    predicted_peak_temperature_c: float
    minutes_to_predicted_peak: float
    predicted_peak_remaining_warming_c: float
    next_curve_delta_from_high_c: float
    future_curve_slope_c_per_hour: float
    observation_count: int
    complete: bool
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ConditionalMetarPathState":
        expected = {
            "schema", "as_of_utc", "minutes_remaining_in_settlement_day",
            "running_high_c", "current_temperature_c",
            "drawdown_from_high_c", "momentum_c_per_hour",
            "acceleration_c_per_hour2", "dewpoint_depression_c",
            "cloud_cover_fraction", "precipitation_present",
            "wind_speed_mps", "wind_gust_mps", "pressure_hpa",
            "root_expected_tmax_c", "root_expected_remaining_warming_c",
            "future_curve_sha256", "future_curve_complete",
            "predicted_peak_temperature_c", "minutes_to_predicted_peak",
            "predicted_peak_remaining_warming_c",
            "next_curve_delta_from_high_c", "future_curve_slope_c_per_hour",
            "observation_count", "complete", "content_sha256",
        }
        if set(raw) != expected or raw.get("schema") != PATH_STATE_SCHEMA:
            raise ContractError("metar_path_state_schema_invalid")
        body = {key: raw[key] for key in raw if key != "content_sha256"}
        if str(raw.get("content_sha256") or "").lower() != content_sha256(body):
            raise ContractError("metar_path_state_content_sha256_mismatch")
        observation_count = raw.get("observation_count")
        if isinstance(observation_count, bool) or not isinstance(observation_count, int) or observation_count < 1:
            raise ContractError("metar_path_observation_count_invalid")
        if any(
            not isinstance(raw.get(key), bool)
            for key in ("complete", "precipitation_present", "future_curve_complete")
        ):
            raise ContractError("metar_path_state_boolean_invalid")
        curve_sha = str(raw.get("future_curve_sha256") or "").strip().lower()
        if curve_sha and (
            len(curve_sha) != 64
            or any(char not in "0123456789abcdef" for char in curve_sha)
        ):
            raise ContractError("metar_path_future_curve_sha256_invalid")
        return cls(
            as_of_utc=_utc("metar_path_as_of_utc", raw["as_of_utc"]).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            minutes_remaining_in_settlement_day=_finite("metar_path_minutes_remaining", raw["minutes_remaining_in_settlement_day"]),
            running_high_c=_finite("metar_path_running_high_c", raw["running_high_c"]),
            current_temperature_c=_finite("metar_path_current_temperature_c", raw["current_temperature_c"]),
            drawdown_from_high_c=_finite("metar_path_drawdown", raw["drawdown_from_high_c"]),
            momentum_c_per_hour=_finite("metar_path_momentum", raw["momentum_c_per_hour"]),
            acceleration_c_per_hour2=_finite("metar_path_acceleration", raw["acceleration_c_per_hour2"]),
            dewpoint_depression_c=_finite("metar_path_dewpoint_depression", raw["dewpoint_depression_c"]),
            cloud_cover_fraction=_finite("metar_path_cloud_cover", raw["cloud_cover_fraction"]),
            precipitation_present=raw["precipitation_present"],
            wind_speed_mps=_finite("metar_path_wind_speed", raw["wind_speed_mps"]),
            wind_gust_mps=_finite("metar_path_wind_gust", raw["wind_gust_mps"]),
            pressure_hpa=_finite("metar_path_pressure", raw["pressure_hpa"]),
            root_expected_tmax_c=_finite("metar_path_root_expected_tmax", raw["root_expected_tmax_c"]),
            root_expected_remaining_warming_c=_finite("metar_path_root_expected_remaining", raw["root_expected_remaining_warming_c"]),
            future_curve_sha256=curve_sha or None,
            future_curve_complete=raw["future_curve_complete"],
            predicted_peak_temperature_c=_finite("metar_path_predicted_peak_temperature", raw["predicted_peak_temperature_c"]),
            minutes_to_predicted_peak=_finite("metar_path_minutes_to_predicted_peak", raw["minutes_to_predicted_peak"]),
            predicted_peak_remaining_warming_c=_finite("metar_path_predicted_peak_remaining", raw["predicted_peak_remaining_warming_c"]),
            next_curve_delta_from_high_c=_finite("metar_path_next_curve_delta", raw["next_curve_delta_from_high_c"]),
            future_curve_slope_c_per_hour=_finite("metar_path_future_curve_slope", raw["future_curve_slope_c_per_hour"]),
            observation_count=observation_count,
            complete=raw["complete"],
            content_sha256=str(raw["content_sha256"]).lower(),
        )

    def to_dict(self, *, include_content_sha256: bool = True) -> dict[str, Any]:
        result = {
            "schema": PATH_STATE_SCHEMA,
            "as_of_utc": self.as_of_utc,
            "minutes_remaining_in_settlement_day": self.minutes_remaining_in_settlement_day,
            "running_high_c": self.running_high_c,
            "current_temperature_c": self.current_temperature_c,
            "drawdown_from_high_c": self.drawdown_from_high_c,
            "momentum_c_per_hour": self.momentum_c_per_hour,
            "acceleration_c_per_hour2": self.acceleration_c_per_hour2,
            "dewpoint_depression_c": self.dewpoint_depression_c,
            "cloud_cover_fraction": self.cloud_cover_fraction,
            "precipitation_present": self.precipitation_present,
            "wind_speed_mps": self.wind_speed_mps,
            "wind_gust_mps": self.wind_gust_mps,
            "pressure_hpa": self.pressure_hpa,
            "root_expected_tmax_c": self.root_expected_tmax_c,
            "root_expected_remaining_warming_c": self.root_expected_remaining_warming_c,
            "future_curve_sha256": self.future_curve_sha256,
            "future_curve_complete": self.future_curve_complete,
            "predicted_peak_temperature_c": self.predicted_peak_temperature_c,
            "minutes_to_predicted_peak": self.minutes_to_predicted_peak,
            "predicted_peak_remaining_warming_c": self.predicted_peak_remaining_warming_c,
            "next_curve_delta_from_high_c": self.next_curve_delta_from_high_c,
            "future_curve_slope_c_per_hour": self.future_curve_slope_c_per_hour,
            "observation_count": self.observation_count,
            "complete": self.complete,
        }
        if include_content_sha256:
            result["content_sha256"] = self.content_sha256
        return result


def compile_conditional_metar_path_state(
    observations: Sequence[Mapping[str, Any]],
    *,
    as_of_utc: str,
    settlement_timezone: str,
    root_probabilities: Mapping[str, Any],
    bucket_centers_c: Mapping[str, Any],
    future_curve: RootFutureTemperatureCurve | Mapping[str, Any] | None = None,
    root_checkpoint_id: str | None = None,
    root_probability_sha256: str | None = None,
    station_id: str | None = None,
    event_date: str | None = None,
) -> ConditionalMetarPathState:
    """Compile a full-prefix state; incomplete paths are explicit, never imputed."""

    if not observations:
        raise ContractError("metar_path_observation_sequence_empty")
    if set(root_probabilities) != set(bucket_centers_c):
        raise ContractError("metar_path_root_geometry_mismatch")
    rows = sorted(
        (dict(row) for row in observations),
        key=lambda row: (
            _utc("metar_path_observed_at_utc", row.get("observed_at_utc")),
            str(row.get("observation_id") or ""),
        ),
    )
    if len(rows) > 512:
        raise ContractError("metar_path_observation_resource_limit_exceeded")
    temperatures = [
        _finite("metar_path_temperature_c", row.get("temperature_c"))
        for row in rows
    ]
    times = [
        _utc("metar_path_observed_at_utc", row.get("observed_at_utc"))
        for row in rows
    ]
    states = []
    for row, temperature in zip(rows, temperatures):
        raw_text = str(row.get("raw_text") or "").strip()
        if not raw_text:
            states.append(None)
            continue
        states.append(parse_metar_physical_state(
            raw_text,
            expected_temperature_c=temperature,
        ))
    latest = states[-1]
    segment_slopes: list[float] = []
    for left_t, right_t, left_temp, right_temp in zip(
        times, times[1:], temperatures, temperatures[1:]
    ):
        hours = (right_t - left_t).total_seconds() / 3600.0
        if hours <= 0.0:
            raise ContractError("metar_path_observation_time_rewind")
        segment_slopes.append((right_temp - left_temp) / hours)
    momentum = segment_slopes[-1] if segment_slopes else 0.0
    if len(segment_slopes) >= 2:
        hours = (times[-1] - times[-2]).total_seconds() / 3600.0
        acceleration = (segment_slopes[-1] - segment_slopes[-2]) / hours
    else:
        acceleration = 0.0

    decision = _utc("metar_path_as_of_utc", as_of_utc)
    local = decision.astimezone(ZoneInfo(settlement_timezone))
    next_midnight = datetime.combine(
        local.date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=local.tzinfo,
    )
    minutes_remaining = max(0.0, (next_midnight - local).total_seconds() / 60.0)
    running_high = max(temperatures)
    expected_tmax = sum(
        _finite("metar_path_root_probability", probability)
        * _finite("metar_path_bucket_center_c", bucket_centers_c[bucket])
        for bucket, probability in root_probabilities.items()
    )
    physical_complete = latest is not None and latest.complete
    complete = physical_complete and len(rows) >= 3
    curve_features: dict[str, Any] | None = None
    if future_curve is not None:
        curve = (
            future_curve
            if isinstance(future_curve, RootFutureTemperatureCurve)
            else RootFutureTemperatureCurve.from_mapping(future_curve)
        )
        if (
            not root_checkpoint_id
            or not root_probability_sha256
            or curve.root_checkpoint_id != root_checkpoint_id
            or curve.root_probability_sha256 != root_probability_sha256
            or (station_id and curve.station_id != station_id.strip().upper())
            or (event_date and curve.event_date != event_date)
        ):
            raise ContractError("metar_path_future_curve_root_scope_mismatch")
        curve_features = compile_future_curve_features(
            curve,
            as_of_utc=as_of_utc,
            running_high_c=running_high,
        )
    dewpoint_depression = (
        temperatures[-1] - float(latest.dewpoint_c)
        if latest is not None and latest.dewpoint_c is not None
        else 0.0
    )
    body = {
        "schema": PATH_STATE_SCHEMA,
        "as_of_utc": decision.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "minutes_remaining_in_settlement_day": minutes_remaining,
        "running_high_c": running_high,
        "current_temperature_c": temperatures[-1],
        "drawdown_from_high_c": running_high - temperatures[-1],
        "momentum_c_per_hour": momentum,
        "acceleration_c_per_hour2": acceleration,
        "dewpoint_depression_c": dewpoint_depression,
        "cloud_cover_fraction": float(latest.cloud_cover_fraction) if physical_complete else 0.0,
        "precipitation_present": bool(latest.precipitation_present) if latest is not None else False,
        "wind_speed_mps": float(latest.wind_speed_mps) if physical_complete else 0.0,
        "wind_gust_mps": float(latest.wind_gust_mps or latest.wind_speed_mps) if physical_complete else 0.0,
        "pressure_hpa": float(latest.pressure_hpa) if physical_complete else 0.0,
        "root_expected_tmax_c": expected_tmax,
        "root_expected_remaining_warming_c": max(0.0, expected_tmax - running_high),
        "future_curve_sha256": (
            None if curve_features is None else curve_features["curve_sha256"]
        ),
        "future_curve_complete": curve_features is not None,
        "predicted_peak_temperature_c": (
            0.0 if curve_features is None else curve_features["predicted_peak_temperature_c"]
        ),
        "minutes_to_predicted_peak": (
            0.0 if curve_features is None else curve_features["minutes_to_predicted_peak"]
        ),
        "predicted_peak_remaining_warming_c": (
            0.0 if curve_features is None else curve_features["predicted_peak_remaining_warming_c"]
        ),
        "next_curve_delta_from_high_c": (
            0.0 if curve_features is None else curve_features["next_curve_delta_from_high_c"]
        ),
        "future_curve_slope_c_per_hour": (
            0.0 if curve_features is None else curve_features["future_curve_slope_c_per_hour"]
        ),
        "observation_count": len(rows),
        "complete": complete,
    }
    return ConditionalMetarPathState(
        **{key: value for key, value in body.items() if key != "schema"},
        content_sha256=content_sha256(body),
    )


def condition_remaining_warming_distribution(
    base_distribution: Mapping[float, float],
    path_state: ConditionalMetarPathState,
    *,
    coefficients: Mapping[str, Any],
    sigma_c: float,
) -> dict[float, float]:
    """Condition a learned remaining-warming prior on the current path.

    Coefficients are artifact-owned and must be evaluated by the same dated
    OOS folds before promotion.  This function performs no coefficient fitting.
    """

    if not path_state.complete:
        raise ContractError("metar_path_state_incomplete")
    parsed_base = {
        _finite("metar_path_increment_c", key): _finite(
            "metar_path_increment_probability", value
        )
        for key, value in base_distribution.items()
    }
    if (
        not parsed_base
        or any(value < 0.0 for value in parsed_base.values())
        or abs(sum(parsed_base.values()) - 1.0) > 1e-8
    ):
        raise ContractError("metar_path_base_distribution_invalid")
    coefficient_keys = set(coefficients)
    if (
        coefficient_keys != PATH_MODEL_COEFFICIENT_KEYS
        and coefficient_keys != HOURLY_CURVE_PATH_MODEL_COEFFICIENT_KEYS
    ):
        raise ContractError("metar_path_model_coefficients_invalid")
    beta = {
        key: _finite(f"metar_path_coefficient_{key}", coefficients[key])
        for key in coefficient_keys
    }
    sigma = _finite("metar_path_sigma_c", sigma_c)
    if sigma <= 0.0:
        raise ContractError("metar_path_sigma_c_invalid")
    base_mean = sum(key * value for key, value in parsed_base.items())
    target = (
        beta["base_distribution_mean"] * base_mean
        + beta["root_expected_remaining_warming"]
        * path_state.root_expected_remaining_warming_c
        + beta["momentum_c_per_hour"] * path_state.momentum_c_per_hour
        + beta["acceleration_c_per_hour2"]
        * path_state.acceleration_c_per_hour2
        + beta["drawdown_from_high_c"] * path_state.drawdown_from_high_c
        + beta["dewpoint_depression_c"] * path_state.dewpoint_depression_c
        + beta["cloud_cover_fraction"] * path_state.cloud_cover_fraction
        + beta["precipitation_present"]
        * float(path_state.precipitation_present)
        + beta["wind_speed_mps"] * path_state.wind_speed_mps
        + beta["remaining_hours"]
        * (path_state.minutes_remaining_in_settlement_day / 60.0)
    )
    if coefficient_keys == HOURLY_CURVE_PATH_MODEL_COEFFICIENT_KEYS:
        if not path_state.future_curve_complete:
            raise ContractError("metar_path_future_curve_incomplete")
        target += (
            beta["predicted_peak_remaining_warming_c"]
            * path_state.predicted_peak_remaining_warming_c
            + beta["minutes_to_predicted_peak"]
            * (path_state.minutes_to_predicted_peak / 60.0)
            + beta["next_curve_delta_from_high_c"]
            * path_state.next_curve_delta_from_high_c
            + beta["future_curve_slope_c_per_hour"]
            * path_state.future_curve_slope_c_per_hour
        )
    target = max(min(parsed_base), min(max(parsed_base), target))
    weighted = {
        increment: probability
        * math.exp(-0.5 * ((increment - target) / sigma) ** 2)
        for increment, probability in parsed_base.items()
    }
    total = sum(weighted.values())
    if total <= 0.0:
        raise ContractError("metar_path_conditioned_distribution_zero_mass")
    return {
        increment: value / total for increment, value in weighted.items()
    }


__all__ = [
    "ConditionalMetarPathState",
    "MetarPhysicalState",
    "PATH_STATE_SCHEMA",
    "PATH_MODEL_COEFFICIENT_KEYS",
    "HOURLY_CURVE_PATH_MODEL_COEFFICIENT_KEYS",
    "PHYSICAL_STATE_SCHEMA",
    "compile_conditional_metar_path_state",
    "condition_remaining_warming_distribution",
    "parse_metar_physical_state",
]
