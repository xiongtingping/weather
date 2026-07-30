"""Pure-domain compiler for the historical same-calendar-day climate anchor.

The artifact compiled here is deliberately independent from historical model
runs.  It consumes only settled station-day maximum-temperature truth from
years strictly before the target year.  Adjacent calendar days are allowed
only as explicitly labelled shrinkage observations.

This module performs no I/O and has no production or execution side effects.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError, content_sha256


SAME_DAY_CLIMATOLOGY_SCHEMA = "bayes_basket_same_day_climatology_artifact_v1"
SAME_DAY_CLIMATOLOGY_METHOD = "settled_truth_same_day_kde_v1"
SAME_DAY_SAMPLE_ROLES = ("SAME_DAY_CORE", "ADJACENT_DAY_SHRINKAGE")
_FORBIDDEN_FIELD_FRAGMENTS = (
    "model",
    "nwp",
    "forecast",
    "taf",
    "metar",
    "speci",
    "order",
    "price",
    "inventory",
)
_EPS = 1e-12


def _strict_fields(
    raw: Mapping[str, Any], *, allowed: set[str], contract: str
) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ContractError(
            f"{contract}_unknown_fields:"
            + ",".join(sorted(str(field) for field in unknown))
        )


def _reject_forbidden_fields(raw: Mapping[str, Any], *, contract: str) -> None:
    for field in raw:
        normalized = str(field).strip().lower()
        if any(fragment in normalized for fragment in _FORBIDDEN_FIELD_FRAGMENTS):
            raise ContractError(f"{contract}_forbidden_historical_input:{field}")


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


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


def _positive(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result <= 0.0:
        raise ContractError(f"{name}_must_be_positive")
    return result


def _nonnegative(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result < 0.0:
        raise ContractError(f"{name}_must_be_nonnegative")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ContractError(f"{name}_invalid")
    return result


def _iso_date(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _timezone(value: Any) -> str:
    result = _text("settlement_timezone", value)
    try:
        ZoneInfo(result)
    except ZoneInfoNotFoundError as exc:
        raise ContractError("settlement_timezone_invalid") from exc
    return result


def _calendar_day(value: Any) -> str:
    result = _text("calendar_day", value)
    try:
        parsed = date.fromisoformat(f"2000-{result}")
    except ValueError as exc:
        raise ContractError("calendar_day_invalid") from exc
    return parsed.strftime("%m-%d")


def _month_day(local_date: str) -> str:
    parsed = date.fromisoformat(local_date)
    return parsed.strftime("%m-%d")


def _circular_day_distance(left: str, right: str) -> int:
    # Leap-safe synthetic year.  A 366-day cycle makes 02-29 representable.
    left_date = date.fromisoformat(f"2000-{left}")
    right_date = date.fromisoformat(f"2000-{right}")
    delta = abs((left_date - right_date).days)
    return min(delta, 366 - delta)


@dataclass(frozen=True)
class SameDayTruthSample:
    truth_id: str
    station_id: str
    local_date: str
    official_tmax_c: float
    settled_at_utc: str
    source_lane: str
    settlement_definition: str
    settlement_timezone: str
    sample_role: str
    trend_adjustment_c: float
    station_regime_adjustment_c: float
    adjusted_tmax_c: float
    weight: float
    truth_content_sha256: str

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        station_id: str,
        settlement_timezone: str,
        settlement_definition: str,
        calendar_day: str,
        target_year: int,
        max_adjacent_day_distance: int,
    ) -> "SameDayTruthSample":
        if not isinstance(raw, Mapping):
            raise ContractError("same_day_truth_sample_invalid")
        _reject_forbidden_fields(raw, contract="same_day_truth_sample")
        _strict_fields(
            raw,
            allowed={
                "truth_id",
                "station_id",
                "local_date",
                "official_tmax_c",
                "settled_at_utc",
                "settlement_closed",
                "source_lane",
                "settlement_definition",
                "settlement_timezone",
                "sample_role",
                "trend_adjustment_c",
                "station_regime_adjustment_c",
                "adjusted_tmax_c",
                "weight",
                "truth_content_sha256",
            },
            contract="same_day_truth_sample",
        )
        local_date = _iso_date("truth_local_date", raw.get("local_date"))
        parsed_date = date.fromisoformat(local_date)
        if parsed_date.year >= target_year:
            raise ContractError("same_day_target_year_or_future_truth_leakage")
        if raw.get("settlement_closed") is not True:
            raise ContractError("same_day_truth_not_settled")
        role = _text("sample_role", raw.get("sample_role")).upper()
        if role not in SAME_DAY_SAMPLE_ROLES:
            raise ContractError("same_day_sample_role_invalid")
        distance = _circular_day_distance(_month_day(local_date), calendar_day)
        if role == "SAME_DAY_CORE" and distance != 0:
            raise ContractError("same_day_core_calendar_day_mismatch")
        if role == "ADJACENT_DAY_SHRINKAGE" and not (
            1 <= distance <= max_adjacent_day_distance
        ):
            raise ContractError("same_day_adjacent_sample_out_of_window")
        sample_station = _text("truth_station_id", raw.get("station_id")).upper()
        if sample_station != station_id:
            raise ContractError("same_day_truth_station_mismatch")
        sample_timezone = _timezone(raw.get("settlement_timezone"))
        if sample_timezone != settlement_timezone:
            raise ContractError("same_day_truth_timezone_mismatch")
        sample_definition = _text(
            "truth_settlement_definition", raw.get("settlement_definition")
        )
        if sample_definition != settlement_definition:
            raise ContractError("same_day_truth_settlement_definition_mismatch")
        official_tmax = _finite("official_tmax_c", raw.get("official_tmax_c"))
        trend_adjustment = _finite(
            "trend_adjustment_c", raw.get("trend_adjustment_c", 0.0)
        )
        regime_adjustment = _finite(
            "station_regime_adjustment_c",
            raw.get("station_regime_adjustment_c", 0.0),
        )
        adjusted_tmax = official_tmax + trend_adjustment + regime_adjustment
        if "adjusted_tmax_c" in raw and abs(
            _finite("adjusted_tmax_c", raw.get("adjusted_tmax_c")) - adjusted_tmax
        ) > 1e-9:
            raise ContractError("same_day_adjusted_tmax_mismatch")
        settled_at = _timestamp("settled_at_utc", raw.get("settled_at_utc"))
        target_year_start = datetime(
            target_year, 1, 1, tzinfo=ZoneInfo(settlement_timezone)
        )
        if datetime.fromisoformat(
            settled_at.replace("Z", "+00:00")
        ) >= target_year_start:
            raise ContractError("same_day_future_settlement_leakage")
        return cls(
            truth_id=_text("truth_id", raw.get("truth_id")),
            station_id=sample_station,
            local_date=local_date,
            official_tmax_c=official_tmax,
            settled_at_utc=settled_at,
            source_lane=_text("source_lane", raw.get("source_lane")).upper(),
            settlement_definition=sample_definition,
            settlement_timezone=sample_timezone,
            sample_role=role,
            trend_adjustment_c=trend_adjustment,
            station_regime_adjustment_c=regime_adjustment,
            adjusted_tmax_c=adjusted_tmax,
            weight=_positive("sample_weight", raw.get("weight", 1.0)),
            truth_content_sha256=_sha(
                "truth_content_sha256", raw.get("truth_content_sha256")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "truth_id": self.truth_id,
            "station_id": self.station_id,
            "local_date": self.local_date,
            "official_tmax_c": self.official_tmax_c,
            "settled_at_utc": self.settled_at_utc,
            "settlement_closed": True,
            "source_lane": self.source_lane,
            "settlement_definition": self.settlement_definition,
            "settlement_timezone": self.settlement_timezone,
            "sample_role": self.sample_role,
            "trend_adjustment_c": self.trend_adjustment_c,
            "station_regime_adjustment_c": self.station_regime_adjustment_c,
            "adjusted_tmax_c": self.adjusted_tmax_c,
            "weight": self.weight,
            "truth_content_sha256": self.truth_content_sha256,
        }


@dataclass(frozen=True)
class SameDayClimatologyArtifact:
    artifact_id: str
    activation_id: str
    station_id: str
    settlement_timezone: str
    settlement_definition: str
    target_year: int
    calendar_day: str
    method_version: str
    truth_samples: tuple[SameDayTruthSample, ...]
    same_day_truth_ids: tuple[str, ...]
    adjacent_truth_ids: tuple[str, ...]
    max_adjacent_day_distance: int
    kde_bandwidth_c: float
    effective_sample_n: float
    center_c: float
    dispersion_c: float
    bucket_centers_c: Mapping[str, float]
    bucket_probabilities: Mapping[str, float]
    built_at_utc: str
    valid_from_utc: str
    valid_until_utc: str
    activated: bool
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SameDayClimatologyArtifact":
        if not isinstance(raw, Mapping):
            raise ContractError("same_day_climatology_artifact_invalid")
        _strict_fields(
            raw,
            allowed={
                "schema",
                "artifact_id",
                "activation_id",
                "station_id",
                "settlement_timezone",
                "settlement_definition",
                "target_year",
                "calendar_day",
                "method_version",
                "truth_samples",
                "same_day_truth_ids",
                "adjacent_truth_ids",
                "max_adjacent_day_distance",
                "kde_bandwidth_c",
                "effective_sample_n",
                "center_c",
                "dispersion_c",
                "bucket_centers_c",
                "bucket_probabilities",
                "built_at_utc",
                "valid_from_utc",
                "valid_until_utc",
                "activated",
                "content_sha256",
            },
            contract="same_day_climatology_artifact",
        )
        if raw.get("schema") != SAME_DAY_CLIMATOLOGY_SCHEMA:
            raise ContractError("same_day_climatology_schema_invalid")
        target_year = int(raw.get("target_year", 0))
        if target_year < 1900:
            raise ContractError("same_day_target_year_invalid")
        max_adjacent = int(raw.get("max_adjacent_day_distance", -1))
        if max_adjacent < 0 or max_adjacent > 31:
            raise ContractError("same_day_max_adjacent_day_distance_invalid")
        station_id = _text("station_id", raw.get("station_id")).upper()
        timezone = _timezone(raw.get("settlement_timezone"))
        definition = _text(
            "settlement_definition", raw.get("settlement_definition")
        )
        calendar_day = _calendar_day(raw.get("calendar_day"))
        samples_raw = raw.get("truth_samples")
        if not isinstance(samples_raw, (list, tuple)) or not samples_raw:
            raise ContractError("same_day_truth_samples_missing")
        samples = tuple(
            SameDayTruthSample.from_mapping(
                item,
                station_id=station_id,
                settlement_timezone=timezone,
                settlement_definition=definition,
                calendar_day=calendar_day,
                target_year=target_year,
                max_adjacent_day_distance=max_adjacent,
            )
            for item in samples_raw
        )
        centers = _probability_mapping(
            "bucket_centers_c", raw.get("bucket_centers_c"), probabilities=False
        )
        probabilities = _probability_mapping(
            "bucket_probabilities", raw.get("bucket_probabilities"), probabilities=True
        )
        value = cls(
            artifact_id=_text("artifact_id", raw.get("artifact_id")),
            activation_id=_text("activation_id", raw.get("activation_id")),
            station_id=station_id,
            settlement_timezone=timezone,
            settlement_definition=definition,
            target_year=target_year,
            calendar_day=calendar_day,
            method_version=_text("method_version", raw.get("method_version")),
            truth_samples=samples,
            same_day_truth_ids=tuple(
                _text("same_day_truth_id", item)
                for item in raw.get("same_day_truth_ids", ())
            ),
            adjacent_truth_ids=tuple(
                _text("adjacent_truth_id", item)
                for item in raw.get("adjacent_truth_ids", ())
            ),
            max_adjacent_day_distance=max_adjacent,
            kde_bandwidth_c=_positive(
                "kde_bandwidth_c", raw.get("kde_bandwidth_c")
            ),
            effective_sample_n=_positive(
                "effective_sample_n", raw.get("effective_sample_n")
            ),
            center_c=_finite("center_c", raw.get("center_c")),
            dispersion_c=_nonnegative("dispersion_c", raw.get("dispersion_c")),
            bucket_centers_c=MappingProxyType(centers),
            bucket_probabilities=MappingProxyType(probabilities),
            built_at_utc=_timestamp("built_at_utc", raw.get("built_at_utc")),
            valid_from_utc=_timestamp("valid_from_utc", raw.get("valid_from_utc")),
            valid_until_utc=_timestamp("valid_until_utc", raw.get("valid_until_utc")),
            activated=raw.get("activated") is True,
            content_sha256=_sha("content_sha256", raw.get("content_sha256")),
        )
        value._validate()
        return value

    def _validate(self) -> None:
        if self.method_version != SAME_DAY_CLIMATOLOGY_METHOD:
            raise ContractError("same_day_climatology_method_invalid")
        if not self.activated:
            raise ContractError("same_day_climatology_not_activated")
        if not _timestamp_lt(self.valid_from_utc, self.valid_until_utc):
            raise ContractError("same_day_climatology_valid_window_invalid")
        truth_ids = tuple(sample.truth_id for sample in self.truth_samples)
        if len(set(truth_ids)) != len(truth_ids):
            raise ContractError("same_day_duplicate_truth_id")
        expected_core = tuple(
            sample.truth_id
            for sample in self.truth_samples
            if sample.sample_role == "SAME_DAY_CORE"
        )
        expected_adjacent = tuple(
            sample.truth_id
            for sample in self.truth_samples
            if sample.sample_role == "ADJACENT_DAY_SHRINKAGE"
        )
        if not expected_core:
            raise ContractError("same_day_core_samples_missing")
        if (
            self.same_day_truth_ids != expected_core
            or self.adjacent_truth_ids != expected_adjacent
        ):
            raise ContractError("same_day_truth_id_partition_mismatch")
        if set(self.bucket_centers_c) != set(self.bucket_probabilities):
            raise ContractError("same_day_bucket_mapping_mismatch")
        ordered = list(self.bucket_centers_c.values())
        if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
            raise ContractError("same_day_bucket_centers_not_strictly_increasing")
        if self.effective_sample_n > len(self.truth_samples) + _EPS:
            raise ContractError("same_day_effective_sample_n_invalid")
        expected_center = sum(
            self.bucket_centers_c[bucket] * probability
            for bucket, probability in self.bucket_probabilities.items()
        )
        expected_dispersion = math.sqrt(
            sum(
                probability
                * (self.bucket_centers_c[bucket] - expected_center) ** 2
                for bucket, probability in self.bucket_probabilities.items()
            )
        )
        if abs(expected_center - self.center_c) > 1e-9:
            raise ContractError("same_day_center_mismatch")
        if abs(expected_dispersion - self.dispersion_c) > 1e-9:
            raise ContractError("same_day_dispersion_mismatch")
        if content_sha256(self.to_dict(include_content_sha256=False)) != self.content_sha256:
            raise ContractError("same_day_climatology_content_sha256_mismatch")
        identity_without_id = self.to_dict(include_content_sha256=False)
        identity_without_id.pop("artifact_id")
        expected_artifact_id = (
            "same-day-clim-" + content_sha256(identity_without_id)[:32]
        )
        if self.artifact_id != expected_artifact_id:
            raise ContractError("same_day_climatology_artifact_id_mismatch")

    def to_dict(self, *, include_content_sha256: bool = True) -> dict[str, Any]:
        result = {
            "schema": SAME_DAY_CLIMATOLOGY_SCHEMA,
            "artifact_id": self.artifact_id,
            "activation_id": self.activation_id,
            "station_id": self.station_id,
            "settlement_timezone": self.settlement_timezone,
            "settlement_definition": self.settlement_definition,
            "target_year": self.target_year,
            "calendar_day": self.calendar_day,
            "method_version": self.method_version,
            "truth_samples": [sample.to_dict() for sample in self.truth_samples],
            "same_day_truth_ids": list(self.same_day_truth_ids),
            "adjacent_truth_ids": list(self.adjacent_truth_ids),
            "max_adjacent_day_distance": self.max_adjacent_day_distance,
            "kde_bandwidth_c": self.kde_bandwidth_c,
            "effective_sample_n": self.effective_sample_n,
            "center_c": self.center_c,
            "dispersion_c": self.dispersion_c,
            "bucket_centers_c": dict(self.bucket_centers_c),
            "bucket_probabilities": dict(self.bucket_probabilities),
            "built_at_utc": self.built_at_utc,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "activated": self.activated,
        }
        if include_content_sha256:
            result["content_sha256"] = self.content_sha256
        return result


def _timestamp_lt(left: str, right: str) -> bool:
    return datetime.fromisoformat(left.replace("Z", "+00:00")) < datetime.fromisoformat(
        right.replace("Z", "+00:00")
    )


def _probability_mapping(
    name: str, raw: Any, *, probabilities: bool
) -> dict[str, float]:
    if not isinstance(raw, Mapping) or not raw:
        raise ContractError(f"{name}_missing")
    result: dict[str, float] = {}
    for bucket, value in raw.items():
        key = _text(f"{name}_bucket", bucket)
        number = _finite(f"{name}_{key}", value)
        if probabilities and not 0.0 <= number <= 1.0:
            raise ContractError(f"{name}_out_of_range")
        result[key] = number
    if probabilities and abs(sum(result.values()) - 1.0) > 1e-9:
        raise ContractError(f"{name}_not_normalized")
    return result


def _kde_bucket_probabilities(
    samples: Iterable[SameDayTruthSample],
    *,
    bucket_centers_c: Mapping[str, float],
    bandwidth_c: float,
) -> dict[str, float]:
    log_weights: dict[str, float] = {}
    sample_list = tuple(samples)
    for bucket, center in bucket_centers_c.items():
        kernel_sum = sum(
            sample.weight
            * math.exp(
                max(
                    -700.0,
                    -0.5 * ((center - sample.adjusted_tmax_c) / bandwidth_c) ** 2,
                )
            )
            for sample in sample_list
        )
        log_weights[bucket] = max(kernel_sum, _EPS)
    total = sum(log_weights.values())
    return {bucket: value / total for bucket, value in log_weights.items()}


def build_same_day_climatology_artifact(
    *,
    station_id: str,
    settlement_timezone: str,
    settlement_definition: str,
    target_year: int,
    calendar_day: str,
    truth_samples: Iterable[Mapping[str, Any]],
    bucket_centers_c: Mapping[str, float],
    kde_bandwidth_c: float,
    activation_id: str,
    built_at_utc: str,
    valid_from_utc: str,
    valid_until_utc: str,
    max_adjacent_day_distance: int = 7,
    activated: bool = True,
) -> SameDayClimatologyArtifact:
    """Compile a deterministic, content-addressed weak climatology anchor."""

    station = _text("station_id", station_id).upper()
    timezone = _timezone(settlement_timezone)
    definition = _text("settlement_definition", settlement_definition)
    day = _calendar_day(calendar_day)
    year = int(target_year)
    if year < 1900:
        raise ContractError("same_day_target_year_invalid")
    adjacent_limit = int(max_adjacent_day_distance)
    if adjacent_limit < 0 or adjacent_limit > 31:
        raise ContractError("same_day_max_adjacent_day_distance_invalid")
    parsed_samples = tuple(
        SameDayTruthSample.from_mapping(
            sample,
            station_id=station,
            settlement_timezone=timezone,
            settlement_definition=definition,
            calendar_day=day,
            target_year=year,
            max_adjacent_day_distance=adjacent_limit,
        )
        for sample in truth_samples
    )
    if not parsed_samples:
        raise ContractError("same_day_truth_samples_missing")
    parsed_samples = tuple(
        sorted(parsed_samples, key=lambda sample: (sample.local_date, sample.truth_id))
    )
    if not any(sample.sample_role == "SAME_DAY_CORE" for sample in parsed_samples):
        raise ContractError("same_day_core_samples_missing")
    if len({sample.truth_id for sample in parsed_samples}) != len(parsed_samples):
        raise ContractError("same_day_duplicate_truth_id")
    centers = _probability_mapping(
        "bucket_centers_c", bucket_centers_c, probabilities=False
    )
    ordered_centers = list(centers.values())
    if ordered_centers != sorted(ordered_centers) or len(set(ordered_centers)) != len(
        ordered_centers
    ):
        raise ContractError("same_day_bucket_centers_not_strictly_increasing")
    bandwidth = _positive("kde_bandwidth_c", kde_bandwidth_c)
    probabilities = _kde_bucket_probabilities(
        parsed_samples, bucket_centers_c=centers, bandwidth_c=bandwidth
    )
    center = sum(centers[bucket] * probabilities[bucket] for bucket in centers)
    dispersion = math.sqrt(
        sum(
            probabilities[bucket] * (centers[bucket] - center) ** 2
            for bucket in centers
        )
    )
    weights = [sample.weight for sample in parsed_samples]
    effective_n = sum(weights) ** 2 / sum(weight**2 for weight in weights)
    base = {
        "schema": SAME_DAY_CLIMATOLOGY_SCHEMA,
        "activation_id": _text("activation_id", activation_id),
        "station_id": station,
        "settlement_timezone": timezone,
        "settlement_definition": definition,
        "target_year": year,
        "calendar_day": day,
        "method_version": SAME_DAY_CLIMATOLOGY_METHOD,
        "truth_samples": [sample.to_dict() for sample in parsed_samples],
        "same_day_truth_ids": [
            sample.truth_id
            for sample in parsed_samples
            if sample.sample_role == "SAME_DAY_CORE"
        ],
        "adjacent_truth_ids": [
            sample.truth_id
            for sample in parsed_samples
            if sample.sample_role == "ADJACENT_DAY_SHRINKAGE"
        ],
        "max_adjacent_day_distance": adjacent_limit,
        "kde_bandwidth_c": bandwidth,
        "effective_sample_n": effective_n,
        "center_c": center,
        "dispersion_c": dispersion,
        "bucket_centers_c": centers,
        "bucket_probabilities": probabilities,
        "built_at_utc": _timestamp("built_at_utc", built_at_utc),
        "valid_from_utc": _timestamp("valid_from_utc", valid_from_utc),
        "valid_until_utc": _timestamp("valid_until_utc", valid_until_utc),
        "activated": activated is True,
    }
    artifact_id = "same-day-clim-" + content_sha256(base)[:32]
    identity = {**base, "artifact_id": artifact_id}
    identity["content_sha256"] = content_sha256(identity)
    return SameDayClimatologyArtifact.from_mapping(identity)


compile_same_day_climatology_artifact = build_same_day_climatology_artifact
validate_same_day_climatology_artifact = SameDayClimatologyArtifact.from_mapping


__all__ = [
    "SAME_DAY_CLIMATOLOGY_METHOD",
    "SAME_DAY_CLIMATOLOGY_SCHEMA",
    "SAME_DAY_SAMPLE_ROLES",
    "SameDayClimatologyArtifact",
    "SameDayTruthSample",
    "build_same_day_climatology_artifact",
    "compile_same_day_climatology_artifact",
    "validate_same_day_climatology_artifact",
]
