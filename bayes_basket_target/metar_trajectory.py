"""Pure-domain METAR/SPECI trajectory likelihood compiler.

The compiler owns no runtime state and performs no I/O.  Every P1 proof is
recomputed from one immutable P0-M/P0 root and the complete canonical
observation prefix available at ``as_of_utc``.  It therefore cannot compound
an earlier P1 or revive a bucket eliminated by the observed running high.
"""

from __future__ import annotations

import math
import json
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
    timestamp_in_window,
    timestamp_lt,
)
from .event_contracts import CanonicalObservationEvent
from .metar_path_state import (
    HOURLY_CURVE_PATH_MODEL_COEFFICIENT_KEYS,
    PATH_MODEL_COEFFICIENT_KEYS,
    PATH_STATE_SCHEMA,
    compile_conditional_metar_path_state,
    condition_remaining_warming_distribution,
)


METAR_TRAJECTORY_ARTIFACT_SCHEMA = (
    "bayes_basket_metar_trajectory_likelihood_artifact_v1"
)
METAR_TRAJECTORY_PROOF_SCHEMA = "bayes_basket_metar_trajectory_update_proof_v1"
METAR_TRAJECTORY_OBSERVATION_SCHEMA = (
    "bayes_basket_metar_trajectory_observation_v1"
)
METAR_TRAJECTORY_METHOD_VERSION = (
    "canonical_full_root_recompute_remaining_warming_v1"
)
METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION = (
    "canonical_conditional_remaining_path_v2"
)
METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION = (
    "canonical_conditional_hourly_future_path_v3"
)
METAR_TRAJECTORY_AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
MAX_METAR_TRAJECTORY_OBSERVATIONS = 512
MAX_METAR_TRAJECTORY_BUCKETS = 256
MAX_METAR_TRAJECTORY_FEATURES = 32
MAX_METAR_TRAJECTORY_PROOF_TEXT_BYTES = 131_072
# How much evidence a trajectory artifact must carry before it may govern.
#
# This was 20, chosen when the assumption was that folds would arrive roughly
# daily.  They do not: a fold needs a complete observation prefix *and* a root
# curve for the same day, and the two overlap on well under half of days, so
# 20 sat about five weeks out with the layer contributing nothing in the
# meantime -- and contributing nothing is not neutral.  It leaves P1 as the
# model's own tail renormalised, which is exactly the shape that fails on days
# the model got wrong (ZUUU 2026-08-12: 76% of P0 mass at or below 37C against
# an observed 38C).
#
# Lowering it does not lower the standard.  The gate's real quality test is
# ``stable_oos_gain`` -- the candidate must beat the no-correction baseline on
# Brier, NLL and CRPS simultaneously, out of sample -- and that test does not
# get easier with fewer folds; a weak artifact still loses on all three.  What
# the sample floor buys is protection against a lucky win on a tiny sample,
# and that is bought instead by ramping applied strength with evidence: see
# ``maturity_strength_for_sample_n``.  A newly gated artifact nudges; a
# seasoned one moves.
MINIMUM_EFFECTIVE_SAMPLE_N = 5.0
# Where the ramp reaches the artifact's full configured strength.
MATURITY_FULL_SAMPLE_N = 30.0
_PROMOTED_STATES = {"FORMAL_PROMOTED", "PRODUCTION_NO_LIVE_PROMOTED"}


def maturity_strength_for_sample_n(
    effective_sample_n: float,
    *,
    full_strength: float,
) -> float:
    """Scale configured maturity by ``sqrt(n / n_full)``, capped at full.

    Square root because the correction's standard error shrinks as
    ``1/sqrt(n)``: the strength we are entitled to apply tracks how well the
    correction is pinned down, not how many rows we happened to collect.  At
    the five-fold floor this is about 0.41 of configured strength, so the
    first live artifacts tilt the posterior rather than reshape it.
    """

    if full_strength <= 0.0:
        return 0.0
    if effective_sample_n <= 0.0:
        return 0.0
    ratio = min(1.0, effective_sample_n / MATURITY_FULL_SAMPLE_N)
    return full_strength * math.sqrt(ratio)
_LOCAL_TIME_BUCKETS = {
    "NIGHT": range(0, 6),
    "MORNING": range(6, 12),
    "AFTERNOON": range(12, 18),
    "EVENING": range(18, 24),
}
_EPS = 1e-12


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


def _range(name: str, value: Any, lower: float, upper: float) -> float:
    result = _finite(name, value)
    if result < lower or result > upper:
        raise ContractError(f"{name}_out_of_range")
    return result


def _positive(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result <= 0.0:
        raise ContractError(f"{name}_must_be_positive")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _parsed(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _timezone(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        ZoneInfo(result)
    except ZoneInfoNotFoundError as exc:
        raise ContractError(f"{name}_invalid") from exc
    return result


def _strict_fields(
    raw: Mapping[str, Any],
    *,
    allowed: set[str],
    contract: str,
) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ContractError(
            f"{contract}_unknown_fields:"
            + ",".join(sorted(str(item) for item in unknown))
        )


def _immutable_mapping(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(raw))


def _bounded_text_sequence(
    name: str,
    raw: Any,
    *,
    limit: int,
) -> tuple[str, ...]:
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes, bytearray))
    ):
        raise ContractError(f"{name}_invalid")
    if len(raw) > limit:
        raise ContractError(f"{name}_resource_limit_exceeded")
    values = tuple(_text(name, item) for item in raw)
    if sum(len(item.encode("utf-8")) for item in values) > (
        MAX_METAR_TRAJECTORY_PROOF_TEXT_BYTES
    ):
        raise ContractError(f"{name}_resource_limit_exceeded")
    return values


def _probability_mapping(name: str, raw: Any) -> Mapping[float, float]:
    if not isinstance(raw, Mapping) or not raw:
        raise ContractError(f"{name}_missing")
    if len(raw) > MAX_METAR_TRAJECTORY_BUCKETS:
        raise ContractError(f"{name}_resource_limit_exceeded")
    parsed: dict[float, float] = {}
    for key, value in raw.items():
        increment = _range(f"{name}_increment", key, 0.0, math.inf)
        if increment in parsed:
            raise ContractError(f"{name}_increment_duplicate")
        parsed[increment] = _range(f"{name}_probability", value, 0.0, 1.0)
    if 0.0 not in parsed:
        raise ContractError(f"{name}_zero_increment_missing")
    if abs(sum(parsed.values()) - 1.0) > 1e-9:
        raise ContractError(f"{name}_not_normalized")
    return MappingProxyType(dict(sorted(parsed.items())))


def _probability_to_dict(values: Mapping[float, float]) -> dict[str, float]:
    return {format(key, ".12g"): values[key] for key in sorted(values)}


def _artifact_hash_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in raw.items()
        if key not in {"artifact_id", "content_sha256"}
    }


@dataclass(frozen=True)
class MetarTrajectoryLikelihoodArtifact:
    artifact_id: str
    activation_id: str
    station_id: str
    source_lane: str
    season_bin: str
    local_time_bucket: str
    maturity_bin: str
    settlement_definition: str
    settlement_timezone: str
    feature_definitions: Mapping[str, str]
    missing_observation_policy: str
    expected_interval_minutes: int
    long_interval_minutes: int
    max_missing_intervals: int
    missing_interval_strength_factor: float
    long_interval_strength_factor: float
    path_feature_schema: str
    path_model_coefficients: Mapping[str, float]
    path_model_sigma_c: float | None
    remaining_warming_distribution: Mapping[float, float]
    hard_exclusion_baseline_distribution: Mapping[float, float]
    base_strength: float
    maturity_strength: float
    likelihood_ratio_floor: float
    likelihood_ratio_ceiling: float
    sample_n: int
    effective_sample_n: float
    oos_metrics: Mapping[str, float]
    tail_calibration_error: float
    trained_through_utc: str
    valid_from_utc: str
    valid_until_utc: str
    promotion_state: str
    authority_scope: str
    activated: bool
    method_version: str
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "MetarTrajectoryLikelihoodArtifact":
        allowed = {
            "schema", "artifact_id", "activation_id", "station_id",
            "source_lane", "season_bin", "local_time_bucket", "maturity_bin",
            "settlement_definition", "settlement_timezone",
            "feature_definitions", "missing_observation_policy",
            "expected_interval_minutes", "long_interval_minutes",
            "max_missing_intervals", "missing_interval_strength_factor",
            "long_interval_strength_factor", "path_feature_schema",
            "path_model_coefficients", "path_model_sigma_c",
            "remaining_warming_distribution",
            "hard_exclusion_baseline_distribution", "base_strength",
            "maturity_strength", "likelihood_ratio_floor",
            "likelihood_ratio_ceiling", "sample_n", "effective_sample_n",
            "oos_metrics", "tail_calibration_error", "trained_through_utc",
            "valid_from_utc", "valid_until_utc", "promotion_state",
            "authority_scope", "activated", "method_version", "content_sha256",
        }
        _strict_fields(raw, allowed=allowed, contract="metar_trajectory_artifact")
        if raw.get("schema") not in {None, METAR_TRAJECTORY_ARTIFACT_SCHEMA}:
            raise ContractError("metar_trajectory_artifact_schema_invalid")
        source_lane = _text(
            "metar_trajectory_source_lane", raw.get("source_lane")
        ).lower()
        if source_lane not in {"amsc", "awc", "asos_1min", "madis_hfmetar"}:
            raise ContractError("metar_trajectory_source_lane_invalid")
        local_time_bucket = _text(
            "metar_trajectory_local_time_bucket", raw.get("local_time_bucket")
        ).upper()
        if local_time_bucket not in _LOCAL_TIME_BUCKETS:
            raise ContractError("metar_trajectory_local_time_bucket_invalid")
        features_raw = raw.get("feature_definitions")
        if not isinstance(features_raw, Mapping) or not features_raw:
            raise ContractError("metar_trajectory_feature_definitions_missing")
        if len(features_raw) > MAX_METAR_TRAJECTORY_FEATURES:
            raise ContractError(
                "metar_trajectory_feature_definitions_resource_limit_exceeded"
            )
        feature_definitions = MappingProxyType({
            str(key): _text("metar_trajectory_feature_definition", value)
            for key, value in sorted(features_raw.items())
        })
        missing_policy = _text(
            "metar_trajectory_missing_observation_policy",
            raw.get("missing_observation_policy"),
        ).upper()
        if missing_policy != "DEGRADE_STRENGTH":
            raise ContractError("metar_trajectory_missing_policy_invalid")
        try:
            expected_interval = int(raw.get("expected_interval_minutes"))
            long_interval = int(raw.get("long_interval_minutes"))
            max_missing = int(raw.get("max_missing_intervals"))
            sample_n = int(raw.get("sample_n"))
        except (TypeError, ValueError) as exc:
            raise ContractError("metar_trajectory_integer_field_invalid") from exc
        if expected_interval < 1:
            raise ContractError("metar_trajectory_expected_interval_invalid")
        if long_interval < expected_interval:
            raise ContractError("metar_trajectory_long_interval_invalid")
        if max_missing < 0:
            raise ContractError("metar_trajectory_max_missing_invalid")
        if sample_n < 1:
            raise ContractError("metar_trajectory_sample_n_invalid")
        method_version = _text(
            "metar_trajectory_method_version", raw.get("method_version")
        )
        coefficients_raw = raw.get("path_model_coefficients") or {}
        if not isinstance(coefficients_raw, Mapping):
            raise ContractError("metar_trajectory_path_coefficients_invalid")
        path_coefficients = MappingProxyType({
            str(key): _finite(
                f"metar_trajectory_path_coefficient_{key}", value
            )
            for key, value in sorted(coefficients_raw.items())
        })
        path_feature_schema = str(raw.get("path_feature_schema") or "").strip()
        path_sigma_raw = raw.get("path_model_sigma_c")
        path_sigma = (
            None
            if path_sigma_raw in (None, "")
            else _positive("metar_trajectory_path_model_sigma_c", path_sigma_raw)
        )
        if method_version == METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION:
            if (
                path_feature_schema != PATH_STATE_SCHEMA
                or set(path_coefficients) != PATH_MODEL_COEFFICIENT_KEYS
                or path_sigma is None
            ):
                raise ContractError("metar_trajectory_conditional_path_model_incomplete")
        elif method_version == METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION:
            if (
                path_feature_schema != PATH_STATE_SCHEMA
                or set(path_coefficients)
                != HOURLY_CURVE_PATH_MODEL_COEFFICIENT_KEYS
                or path_sigma is None
            ):
                raise ContractError(
                    "metar_trajectory_conditional_hourly_curve_model_incomplete"
                )
        elif method_version == METAR_TRAJECTORY_METHOD_VERSION:
            if path_feature_schema or path_coefficients or path_sigma is not None:
                raise ContractError("metar_trajectory_v1_path_model_forbidden")
        else:
            raise ContractError("metar_trajectory_method_version_invalid")
        metrics_raw = raw.get("oos_metrics")
        if not isinstance(metrics_raw, Mapping):
            raise ContractError("metar_trajectory_oos_metrics_missing")
        metrics = MappingProxyType({
            name: _range(
                f"metar_trajectory_oos_{name}",
                metrics_raw.get(name),
                0.0,
                math.inf,
            )
            for name in ("brier_score", "nll", "crps")
        })
        floor = _positive(
            "metar_trajectory_likelihood_ratio_floor",
            raw.get("likelihood_ratio_floor"),
        )
        ceiling = _positive(
            "metar_trajectory_likelihood_ratio_ceiling",
            raw.get("likelihood_ratio_ceiling"),
        )
        if not floor <= 1.0 <= ceiling or floor > ceiling:
            raise ContractError("metar_trajectory_likelihood_ratio_bounds_invalid")
        trained = _timestamp(
            "metar_trajectory_trained_through_utc",
            raw.get("trained_through_utc"),
        )
        valid_from = _timestamp(
            "metar_trajectory_valid_from_utc", raw.get("valid_from_utc")
        )
        valid_until = _timestamp(
            "metar_trajectory_valid_until_utc", raw.get("valid_until_utc")
        )
        if not timestamp_lt(valid_from, valid_until):
            raise ContractError("metar_trajectory_artifact_valid_window_invalid")
        effective_n = _positive(
            "metar_trajectory_effective_sample_n", raw.get("effective_sample_n")
        )
        if effective_n > sample_n + _EPS:
            raise ContractError("metar_trajectory_effective_sample_n_invalid")
        value = cls(
            artifact_id=_text(
                "metar_trajectory_artifact_id", raw.get("artifact_id")
            ),
            activation_id=_text(
                "metar_trajectory_activation_id", raw.get("activation_id")
            ),
            station_id=_text(
                "metar_trajectory_station_id", raw.get("station_id")
            ).upper(),
            source_lane=source_lane,
            season_bin=_text(
                "metar_trajectory_season_bin", raw.get("season_bin")
            ),
            local_time_bucket=local_time_bucket,
            maturity_bin=_text(
                "metar_trajectory_maturity_bin", raw.get("maturity_bin")
            ),
            settlement_definition=_text(
                "metar_trajectory_settlement_definition",
                raw.get("settlement_definition"),
            ),
            settlement_timezone=_timezone(
                "metar_trajectory_settlement_timezone",
                raw.get("settlement_timezone"),
            ),
            feature_definitions=feature_definitions,
            missing_observation_policy=missing_policy,
            expected_interval_minutes=expected_interval,
            long_interval_minutes=long_interval,
            max_missing_intervals=max_missing,
            missing_interval_strength_factor=_range(
                "metar_trajectory_missing_interval_strength_factor",
                raw.get("missing_interval_strength_factor"),
                0.0,
                1.0,
            ),
            long_interval_strength_factor=_range(
                "metar_trajectory_long_interval_strength_factor",
                raw.get("long_interval_strength_factor"),
                0.0,
                1.0,
            ),
            path_feature_schema=path_feature_schema,
            path_model_coefficients=path_coefficients,
            path_model_sigma_c=path_sigma,
            remaining_warming_distribution=_probability_mapping(
                "metar_trajectory_remaining_warming_distribution",
                raw.get("remaining_warming_distribution"),
            ),
            hard_exclusion_baseline_distribution=_probability_mapping(
                "metar_trajectory_hard_exclusion_baseline_distribution",
                raw.get("hard_exclusion_baseline_distribution"),
            ),
            base_strength=_range(
                "metar_trajectory_base_strength",
                raw.get("base_strength"),
                0.0,
                1.0,
            ),
            maturity_strength=_range(
                "metar_trajectory_maturity_strength",
                raw.get("maturity_strength"),
                0.0,
                1.0,
            ),
            likelihood_ratio_floor=floor,
            likelihood_ratio_ceiling=ceiling,
            sample_n=sample_n,
            effective_sample_n=effective_n,
            oos_metrics=metrics,
            tail_calibration_error=_range(
                "metar_trajectory_tail_calibration_error",
                raw.get("tail_calibration_error"),
                0.0,
                1.0,
            ),
            trained_through_utc=trained,
            valid_from_utc=valid_from,
            valid_until_utc=valid_until,
            promotion_state=_text(
                "metar_trajectory_promotion_state", raw.get("promotion_state")
            ).upper(),
            authority_scope=_text(
                "metar_trajectory_authority_scope", raw.get("authority_scope")
            ).upper(),
            activated=raw.get("activated") is True,
            method_version=method_version,
            content_sha256=_sha(
                "metar_trajectory_content_sha256", raw.get("content_sha256")
            ),
        )
        if value.authority_scope != METAR_TRAJECTORY_AUTHORITY_SCOPE:
            raise ContractError("metar_trajectory_authority_scope_invalid")
        identity = _artifact_hash_identity(
            value.to_dict(include_content_sha256=False)
        )
        expected_sha = content_sha256(identity)
        if value.content_sha256 != expected_sha:
            raise ContractError("metar_trajectory_artifact_content_sha256_mismatch")
        if value.artifact_id != "metar-trajectory-" + expected_sha[:32]:
            raise ContractError("metar_trajectory_artifact_id_not_content_addressed")
        return value

    def to_dict(self, *, include_content_sha256: bool = True) -> dict[str, Any]:
        result = {
            "schema": METAR_TRAJECTORY_ARTIFACT_SCHEMA,
            "artifact_id": self.artifact_id,
            "activation_id": self.activation_id,
            "station_id": self.station_id,
            "source_lane": self.source_lane,
            "season_bin": self.season_bin,
            "local_time_bucket": self.local_time_bucket,
            "maturity_bin": self.maturity_bin,
            "settlement_definition": self.settlement_definition,
            "settlement_timezone": self.settlement_timezone,
            "feature_definitions": dict(self.feature_definitions),
            "missing_observation_policy": self.missing_observation_policy,
            "expected_interval_minutes": self.expected_interval_minutes,
            "long_interval_minutes": self.long_interval_minutes,
            "max_missing_intervals": self.max_missing_intervals,
            "missing_interval_strength_factor": (
                self.missing_interval_strength_factor
            ),
            "long_interval_strength_factor": self.long_interval_strength_factor,
            "path_feature_schema": self.path_feature_schema,
            "path_model_coefficients": dict(self.path_model_coefficients),
            "path_model_sigma_c": self.path_model_sigma_c,
            "remaining_warming_distribution": _probability_to_dict(
                self.remaining_warming_distribution
            ),
            "hard_exclusion_baseline_distribution": _probability_to_dict(
                self.hard_exclusion_baseline_distribution
            ),
            "base_strength": self.base_strength,
            "maturity_strength": self.maturity_strength,
            "likelihood_ratio_floor": self.likelihood_ratio_floor,
            "likelihood_ratio_ceiling": self.likelihood_ratio_ceiling,
            "sample_n": self.sample_n,
            "effective_sample_n": self.effective_sample_n,
            "oos_metrics": dict(self.oos_metrics),
            "tail_calibration_error": self.tail_calibration_error,
            "trained_through_utc": self.trained_through_utc,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "promotion_state": self.promotion_state,
            "authority_scope": self.authority_scope,
            "activated": self.activated,
            "method_version": self.method_version,
        }
        if include_content_sha256:
            result["content_sha256"] = self.content_sha256
        return result


def build_metar_trajectory_likelihood_artifact(
    payload: Mapping[str, Any],
) -> MetarTrajectoryLikelihoodArtifact:
    """Seal an artifact with a content-derived ID and SHA."""

    raw = dict(payload)
    if raw.get("schema") not in {
        None, METAR_TRAJECTORY_ARTIFACT_SCHEMA
    }:
        raise ContractError("metar_trajectory_artifact_schema_invalid")
    raw.pop("artifact_id", None)
    raw.pop("content_sha256", None)
    normalized = {
        "schema": METAR_TRAJECTORY_ARTIFACT_SCHEMA,
        "activation_id": _text(
            "metar_trajectory_activation_id", raw.get("activation_id")
        ),
        "station_id": _text(
            "metar_trajectory_station_id", raw.get("station_id")
        ).upper(),
        "source_lane": _text(
            "metar_trajectory_source_lane", raw.get("source_lane")
        ).lower(),
        "season_bin": _text(
            "metar_trajectory_season_bin", raw.get("season_bin")
        ),
        "local_time_bucket": _text(
            "metar_trajectory_local_time_bucket",
            raw.get("local_time_bucket"),
        ).upper(),
        "maturity_bin": _text(
            "metar_trajectory_maturity_bin", raw.get("maturity_bin")
        ),
        "settlement_definition": _text(
            "metar_trajectory_settlement_definition",
            raw.get("settlement_definition"),
        ),
        "settlement_timezone": _timezone(
            "metar_trajectory_settlement_timezone",
            raw.get("settlement_timezone"),
        ),
        "feature_definitions": {
            str(key): _text("metar_trajectory_feature_definition", value)
            for key, value in sorted(
                dict(raw.get("feature_definitions") or {}).items()
            )
        },
        "missing_observation_policy": _text(
            "metar_trajectory_missing_observation_policy",
            raw.get("missing_observation_policy"),
        ).upper(),
        "expected_interval_minutes": int(raw.get("expected_interval_minutes")),
        "long_interval_minutes": int(raw.get("long_interval_minutes")),
        "max_missing_intervals": int(raw.get("max_missing_intervals")),
        "missing_interval_strength_factor": _finite(
            "metar_trajectory_missing_interval_strength_factor",
            raw.get("missing_interval_strength_factor"),
        ),
        "long_interval_strength_factor": _finite(
            "metar_trajectory_long_interval_strength_factor",
            raw.get("long_interval_strength_factor"),
        ),
        "path_feature_schema": str(raw.get("path_feature_schema") or "").strip(),
        "path_model_coefficients": {
            str(key): _finite(
                f"metar_trajectory_path_coefficient_{key}", value
            )
            for key, value in sorted(
                dict(raw.get("path_model_coefficients") or {}).items()
            )
        },
        "path_model_sigma_c": (
            None
            if raw.get("path_model_sigma_c") in (None, "")
            else _finite(
                "metar_trajectory_path_model_sigma_c",
                raw.get("path_model_sigma_c"),
            )
        ),
        "remaining_warming_distribution": _probability_to_dict(
            _probability_mapping(
                "metar_trajectory_remaining_warming_distribution",
                raw.get("remaining_warming_distribution"),
            )
        ),
        "hard_exclusion_baseline_distribution": _probability_to_dict(
            _probability_mapping(
                "metar_trajectory_hard_exclusion_baseline_distribution",
                raw.get("hard_exclusion_baseline_distribution"),
            )
        ),
        "base_strength": _finite(
            "metar_trajectory_base_strength", raw.get("base_strength")
        ),
        "maturity_strength": _finite(
            "metar_trajectory_maturity_strength", raw.get("maturity_strength")
        ),
        "likelihood_ratio_floor": _finite(
            "metar_trajectory_likelihood_ratio_floor",
            raw.get("likelihood_ratio_floor"),
        ),
        "likelihood_ratio_ceiling": _finite(
            "metar_trajectory_likelihood_ratio_ceiling",
            raw.get("likelihood_ratio_ceiling"),
        ),
        "sample_n": int(raw.get("sample_n")),
        "effective_sample_n": _finite(
            "metar_trajectory_effective_sample_n",
            raw.get("effective_sample_n"),
        ),
        "oos_metrics": {
            name: _finite(
                f"metar_trajectory_oos_{name}",
                dict(raw.get("oos_metrics") or {}).get(name),
            )
            for name in ("brier_score", "nll", "crps")
        },
        "tail_calibration_error": _finite(
            "metar_trajectory_tail_calibration_error",
            raw.get("tail_calibration_error"),
        ),
        "trained_through_utc": _timestamp(
            "metar_trajectory_trained_through_utc",
            raw.get("trained_through_utc"),
        ),
        "valid_from_utc": _timestamp(
            "metar_trajectory_valid_from_utc", raw.get("valid_from_utc")
        ),
        "valid_until_utc": _timestamp(
            "metar_trajectory_valid_until_utc", raw.get("valid_until_utc")
        ),
        "promotion_state": _text(
            "metar_trajectory_promotion_state", raw.get("promotion_state")
        ).upper(),
        "authority_scope": _text(
            "metar_trajectory_authority_scope", raw.get("authority_scope")
        ).upper(),
        "activated": raw.get("activated") is True,
        "method_version": _text(
            "metar_trajectory_method_version", raw.get("method_version")
        ),
    }
    unknown = set(raw) - set(normalized)
    if unknown:
        raise ContractError(
            "metar_trajectory_artifact_unknown_fields:"
            + ",".join(sorted(unknown))
        )
    digest = content_sha256(normalized)
    return MetarTrajectoryLikelihoodArtifact.from_mapping({
        **normalized,
        "artifact_id": "metar-trajectory-" + digest[:32],
        "content_sha256": digest,
    })


@dataclass(frozen=True)
class MetarTrajectoryObservation:
    event: CanonicalObservationEvent
    canonical_committed_at_utc: str
    temperature_c: float
    raw_text: str | None = None

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "MetarTrajectoryObservation":
        allowed = {
            "schema",
            "event",
            "canonical_committed_at_utc",
            "temperature_c",
            "raw_text",
        }
        _strict_fields(raw, allowed=allowed, contract="metar_trajectory_observation")
        if raw.get("schema") not in {None, METAR_TRAJECTORY_OBSERVATION_SCHEMA}:
            raise ContractError("metar_trajectory_observation_schema_invalid")
        event_raw = raw.get("event")
        if not isinstance(event_raw, Mapping):
            raise ContractError("metar_trajectory_observation_event_missing")
        event = CanonicalObservationEvent.from_mapping(event_raw)
        committed_at = _timestamp(
            "metar_trajectory_canonical_committed_at_utc",
            raw.get("canonical_committed_at_utc"),
        )
        if _parsed(committed_at) < _parsed(event.received_at_utc):
            raise ContractError(
                "metar_trajectory_commit_before_receive"
            )
        return cls(
            event=event,
            canonical_committed_at_utc=committed_at,
            temperature_c=_finite(
                "metar_trajectory_temperature_c", raw.get("temperature_c")
            ),
            raw_text=(
                str(raw.get("raw_text") or "").strip() or None
            ),
        )

    @property
    def content_sha256(self) -> str:
        return content_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": METAR_TRAJECTORY_OBSERVATION_SCHEMA,
            "event": self.event.to_dict(),
            "canonical_committed_at_utc": (
                self.canonical_committed_at_utc
            ),
            "temperature_c": self.temperature_c,
            "raw_text": self.raw_text,
        }


def canonicalize_metar_trajectory_observations(
    observations: Iterable[
        MetarTrajectoryObservation | Mapping[str, Any]
    ],
    *,
    as_of_utc: str,
    station_id: str,
    event_date: str,
    source_lane: str,
    settlement_timezone: str,
) -> tuple[MetarTrajectoryObservation, ...]:
    """Deduplicate exact retries and produce the strict event-time sequence."""

    as_of = _timestamp("metar_trajectory_as_of_utc", as_of_utc)
    expected_station = _text(
        "metar_trajectory_expected_station_id", station_id
    ).upper()
    expected_lane = _text(
        "metar_trajectory_expected_source_lane", source_lane
    ).lower()
    timezone_name = _timezone(
        "metar_trajectory_expected_settlement_timezone", settlement_timezone
    )
    timezone = ZoneInfo(timezone_name)
    by_id: dict[str, MetarTrajectoryObservation] = {}
    for index, item in enumerate(observations, start=1):
        if index > MAX_METAR_TRAJECTORY_OBSERVATIONS:
            raise ContractError(
                "metar_trajectory_observation_resource_limit_exceeded"
            )
        value = (
            item
            if isinstance(item, MetarTrajectoryObservation)
            else MetarTrajectoryObservation.from_mapping(item)
        )
        event = value.event
        if _parsed(event.received_at_utc) > _parsed(as_of):
            continue
        if (
            event.station_id != expected_station
            or event.event_date != event_date
        ):
            raise ContractError("metar_trajectory_observation_scope_mismatch")
        if event.source_lane != expected_lane:
            raise ContractError("metar_trajectory_source_lane_mismatch")
        if event.qc_state != "ACCEPTED" or event.supersedes_observation_id:
            raise ContractError("metar_trajectory_noncanonical_revision_forbidden")
        if event.observation_id in by_id:
            if by_id[event.observation_id].content_sha256 != value.content_sha256:
                raise ContractError("metar_trajectory_duplicate_id_conflict")
            continue
        if _parsed(event.observed_at_utc) > _parsed(as_of):
            raise ContractError("metar_trajectory_future_observation_forbidden")
        local_date = _parsed(event.observed_at_utc).astimezone(timezone).date()
        if local_date.isoformat() != event_date:
            raise ContractError(
                "metar_trajectory_observation_settlement_day_mismatch"
            )
        by_id[event.observation_id] = value
    if not by_id:
        raise ContractError("metar_trajectory_observation_sequence_empty")
    ordered = tuple(sorted(
        by_id.values(),
        key=lambda item: (
            _parsed(item.event.observed_at_utc),
            item.event.sequence,
            item.event.observation_id,
        ),
    ))
    seen_slots: set[tuple[str, str]] = set()
    for item in ordered:
        slot = (item.event.observed_at_utc, item.event.report_type)
        if slot in seen_slots:
            raise ContractError("metar_trajectory_observation_slot_conflict")
        seen_slots.add(slot)
    return ordered


def canonical_observation_sequence_sha256(
    observations: Iterable[MetarTrajectoryObservation],
) -> str:
    identity = [
        {
            "observation_id": item.event.observation_id,
            "observation_sha256": item.content_sha256,
            "observed_at_utc": item.event.observed_at_utc,
            "received_at_utc": item.event.received_at_utc,
            "sequence": item.event.sequence,
        }
        for item in observations
    ]
    return content_sha256(identity)


@dataclass(frozen=True)
class MetarTrajectoryUpdateProof:
    proof_id: str
    artifact_id: str
    artifact_content_sha256: str
    activation_id: str
    prediction_root_stage: str
    prediction_root_checkpoint_id: str
    prediction_root_content_sha256: str
    station_id: str
    event_date: str
    settlement_timezone: str
    source_lane: str
    maturity_bin: str
    as_of_utc: str
    canonical_observation_ids: tuple[str, ...]
    canonical_observation_sequence_sha256: str
    running_high_c: float
    hard_bound_c: float
    hard_excluded_buckets: tuple[str, ...]
    hard_bound_mask_by_bucket: Mapping[str, float]
    likelihood_ratio_by_bucket: Mapping[str, float]
    applied_strength: float
    inferred_missing_intervals: int
    long_interval_count: int
    degradation_reasons: tuple[str, ...]
    posterior_probabilities: Mapping[str, float]
    center_bucket: str
    model_center_bucket: str
    model_center_c: float
    direction_skew: float
    direction: str
    positive_core_buckets: tuple[str, ...]
    conditional_path_state_sha256: str | None
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "MetarTrajectoryUpdateProof":
        allowed = {
            "schema", "proof_id", "artifact_id", "artifact_content_sha256",
            "activation_id", "prediction_root_stage",
            "prediction_root_checkpoint_id", "prediction_root_content_sha256",
            "station_id", "event_date", "settlement_timezone", "source_lane",
            "maturity_bin", "as_of_utc", "canonical_observation_ids",
            "canonical_observation_sequence_sha256", "running_high_c",
            "hard_bound_c", "hard_excluded_buckets",
            "hard_bound_mask_by_bucket",
            "likelihood_ratio_by_bucket", "applied_strength",
            "inferred_missing_intervals", "long_interval_count",
            "degradation_reasons", "posterior_probabilities",
            "center_bucket", "model_center_bucket", "model_center_c",
            "direction_skew", "direction",
            "positive_core_buckets", "content_sha256",
            "conditional_path_state_sha256",
        }
        _strict_fields(raw, allowed=allowed, contract="metar_trajectory_proof")
        if raw.get("schema") not in {None, METAR_TRAJECTORY_PROOF_SCHEMA}:
            raise ContractError("metar_trajectory_proof_schema_invalid")
        root_stage = _text(
            "metar_trajectory_prediction_root_stage",
            raw.get("prediction_root_stage"),
        )
        if root_stage not in {"P0", "P0-M"}:
            raise ContractError("metar_trajectory_prediction_root_stage_invalid")
        ids = _bounded_text_sequence(
            "metar_trajectory_observation_ids",
            raw.get("canonical_observation_ids"),
            limit=MAX_METAR_TRAJECTORY_OBSERVATIONS,
        )
        if not ids or len(ids) != len(set(ids)):
            raise ContractError("metar_trajectory_observation_ids_invalid")
        ratios_raw = raw.get("likelihood_ratio_by_bucket")
        hard_mask_raw = raw.get("hard_bound_mask_by_bucket")
        posterior_raw = raw.get("posterior_probabilities")
        if (
            not isinstance(ratios_raw, Mapping)
            or not isinstance(hard_mask_raw, Mapping)
            or not isinstance(posterior_raw, Mapping)
        ):
            raise ContractError("metar_trajectory_probability_vectors_missing")
        if (
            len(ratios_raw) > MAX_METAR_TRAJECTORY_BUCKETS
            or len(hard_mask_raw) > MAX_METAR_TRAJECTORY_BUCKETS
            or len(posterior_raw) > MAX_METAR_TRAJECTORY_BUCKETS
        ):
            raise ContractError(
                "metar_trajectory_probability_vectors_resource_limit_exceeded"
            )
        ratios = MappingProxyType({
            str(bucket): _positive("metar_trajectory_likelihood_ratio", ratio)
            for bucket, ratio in ratios_raw.items()
        })
        hard_mask = MappingProxyType({
            str(bucket): _range(
                "metar_trajectory_hard_bound_mask",
                value,
                0.0,
                1.0,
            )
            for bucket, value in hard_mask_raw.items()
        })
        if any(value not in {0.0, 1.0} for value in hard_mask.values()):
            raise ContractError("metar_trajectory_hard_bound_mask_not_binary")
        posterior = MappingProxyType({
            str(bucket): _range(
                "metar_trajectory_posterior_probability",
                probability,
                0.0,
                1.0,
            )
            for bucket, probability in posterior_raw.items()
        })
        if set(ratios) != set(hard_mask) or set(ratios) != set(posterior):
            raise ContractError("metar_trajectory_bucket_support_mismatch")
        if abs(sum(posterior.values()) - 1.0) > 1e-9:
            raise ContractError("metar_trajectory_posterior_not_normalized")
        try:
            missing = int(raw.get("inferred_missing_intervals"))
            long_count = int(raw.get("long_interval_count"))
        except (TypeError, ValueError) as exc:
            raise ContractError("metar_trajectory_interval_count_invalid") from exc
        if missing < 0 or long_count < 0:
            raise ContractError("metar_trajectory_interval_count_invalid")
        direction = _text(
            "metar_trajectory_direction", raw.get("direction")
        ).upper()
        if direction not in {"UP", "DOWN", "NEUTRAL"}:
            raise ContractError("metar_trajectory_direction_invalid")
        value = cls(
            proof_id=_text("metar_trajectory_proof_id", raw.get("proof_id")),
            artifact_id=_text(
                "metar_trajectory_artifact_id", raw.get("artifact_id")
            ),
            artifact_content_sha256=_sha(
                "metar_trajectory_artifact_content_sha256",
                raw.get("artifact_content_sha256"),
            ),
            activation_id=_text(
                "metar_trajectory_activation_id", raw.get("activation_id")
            ),
            prediction_root_stage=root_stage,
            prediction_root_checkpoint_id=_text(
                "metar_trajectory_prediction_root_checkpoint_id",
                raw.get("prediction_root_checkpoint_id"),
            ),
            prediction_root_content_sha256=_sha(
                "metar_trajectory_prediction_root_content_sha256",
                raw.get("prediction_root_content_sha256"),
            ),
            station_id=_text(
                "metar_trajectory_station_id", raw.get("station_id")
            ).upper(),
            event_date=_text(
                "metar_trajectory_event_date", raw.get("event_date")
            ),
            settlement_timezone=_timezone(
                "metar_trajectory_settlement_timezone",
                raw.get("settlement_timezone"),
            ),
            source_lane=_text(
                "metar_trajectory_source_lane", raw.get("source_lane")
            ).lower(),
            maturity_bin=_text(
                "metar_trajectory_maturity_bin", raw.get("maturity_bin")
            ),
            as_of_utc=_timestamp(
                "metar_trajectory_as_of_utc", raw.get("as_of_utc")
            ),
            canonical_observation_ids=ids,
            canonical_observation_sequence_sha256=_sha(
                "metar_trajectory_observation_sequence_sha256",
                raw.get("canonical_observation_sequence_sha256"),
            ),
            running_high_c=_finite(
                "metar_trajectory_running_high_c", raw.get("running_high_c")
            ),
            hard_bound_c=_finite(
                "metar_trajectory_hard_bound_c", raw.get("hard_bound_c")
            ),
            hard_excluded_buckets=tuple(
                _bounded_text_sequence(
                    "metar_trajectory_hard_excluded_buckets",
                    raw.get("hard_excluded_buckets") or (),
                    limit=MAX_METAR_TRAJECTORY_BUCKETS,
                )
            ),
            hard_bound_mask_by_bucket=hard_mask,
            likelihood_ratio_by_bucket=ratios,
            applied_strength=_range(
                "metar_trajectory_applied_strength",
                raw.get("applied_strength"),
                0.0,
                1.0,
            ),
            inferred_missing_intervals=missing,
            long_interval_count=long_count,
            degradation_reasons=tuple(
                _bounded_text_sequence(
                    "metar_trajectory_degradation_reasons",
                    raw.get("degradation_reasons") or (),
                    limit=MAX_METAR_TRAJECTORY_OBSERVATIONS,
                )
            ),
            posterior_probabilities=posterior,
            center_bucket=_text(
                "metar_trajectory_center_bucket", raw.get("center_bucket")
            ),
            model_center_bucket=_text(
                "metar_trajectory_model_center_bucket",
                raw.get("model_center_bucket"),
            ),
            model_center_c=_finite(
                "metar_trajectory_model_center_c", raw.get("model_center_c")
            ),
            direction_skew=_range(
                "metar_trajectory_direction_skew",
                raw.get("direction_skew"),
                -1.0,
                1.0,
            ),
            direction=direction,
            positive_core_buckets=tuple(
                _bounded_text_sequence(
                    "metar_trajectory_positive_core_buckets",
                    raw.get("positive_core_buckets") or (),
                    limit=MAX_METAR_TRAJECTORY_BUCKETS,
                )
            ),
            conditional_path_state_sha256=(
                None
                if raw.get("conditional_path_state_sha256") in (None, "")
                else _sha(
                    "metar_trajectory_conditional_path_state_sha256",
                    raw.get("conditional_path_state_sha256"),
                )
            ),
            content_sha256=_sha(
                "metar_trajectory_proof_content_sha256",
                raw.get("content_sha256"),
            ),
        )
        if abs(value.running_high_c - value.hard_bound_c) > _EPS:
            raise ContractError("metar_trajectory_hard_bound_running_high_mismatch")
        excluded = set(value.hard_excluded_buckets)
        if (
            excluded
            != {
                bucket
                for bucket, mask in value.hard_bound_mask_by_bucket.items()
                if mask == 0.0
            }
            or any(
                value.posterior_probabilities[bucket] != 0.0
                for bucket in excluded
            )
        ):
            raise ContractError(
                "metar_trajectory_hard_bound_mask_mismatch"
            )
        body = value.to_dict(include_content_sha256=False)
        digest = content_sha256({
            key: item for key, item in body.items() if key != "proof_id"
        })
        if value.proof_id != "metar-proof-" + digest[:32]:
            raise ContractError("metar_trajectory_proof_id_not_content_addressed")
        if value.content_sha256 != content_sha256(body):
            raise ContractError("metar_trajectory_proof_content_sha256_mismatch")
        return value

    def to_dict(self, *, include_content_sha256: bool = True) -> dict[str, Any]:
        result = {
            "schema": METAR_TRAJECTORY_PROOF_SCHEMA,
            "proof_id": self.proof_id,
            "artifact_id": self.artifact_id,
            "artifact_content_sha256": self.artifact_content_sha256,
            "activation_id": self.activation_id,
            "prediction_root_stage": self.prediction_root_stage,
            "prediction_root_checkpoint_id": self.prediction_root_checkpoint_id,
            "prediction_root_content_sha256": (
                self.prediction_root_content_sha256
            ),
            "station_id": self.station_id,
            "event_date": self.event_date,
            "settlement_timezone": self.settlement_timezone,
            "source_lane": self.source_lane,
            "maturity_bin": self.maturity_bin,
            "as_of_utc": self.as_of_utc,
            "canonical_observation_ids": list(self.canonical_observation_ids),
            "canonical_observation_sequence_sha256": (
                self.canonical_observation_sequence_sha256
            ),
            "running_high_c": self.running_high_c,
            "hard_bound_c": self.hard_bound_c,
            "hard_excluded_buckets": list(self.hard_excluded_buckets),
            "hard_bound_mask_by_bucket": dict(
                self.hard_bound_mask_by_bucket
            ),
            "likelihood_ratio_by_bucket": dict(self.likelihood_ratio_by_bucket),
            "applied_strength": self.applied_strength,
            "inferred_missing_intervals": self.inferred_missing_intervals,
            "long_interval_count": self.long_interval_count,
            "degradation_reasons": list(self.degradation_reasons),
            "posterior_probabilities": dict(self.posterior_probabilities),
            "center_bucket": self.center_bucket,
            "model_center_bucket": self.model_center_bucket,
            "model_center_c": self.model_center_c,
            "direction_skew": self.direction_skew,
            "direction": self.direction,
            "positive_core_buckets": list(self.positive_core_buckets),
            "conditional_path_state_sha256": (
                self.conditional_path_state_sha256
            ),
        }
        if include_content_sha256:
            result["content_sha256"] = self.content_sha256
        return result


def _nearest_distribution_probability(
    distribution: Mapping[float, float],
    increment_c: float,
) -> float:
    key = min(
        distribution,
        key=lambda candidate: (abs(candidate - increment_c), candidate),
    )
    return distribution[key]


def _interval_diagnostics(
    observations: tuple[MetarTrajectoryObservation, ...],
    artifact: MetarTrajectoryLikelihoodArtifact,
) -> tuple[int, int]:
    missing = 0
    long_count = 0
    for left, right in zip(observations, observations[1:]):
        minutes = (
            _parsed(right.event.observed_at_utc)
            - _parsed(left.event.observed_at_utc)
        ).total_seconds() / 60.0
        if minutes < 0:
            raise ContractError("metar_trajectory_observation_time_rewind")
        if minutes > artifact.long_interval_minutes + _EPS:
            long_count += 1
        missing += max(
            0,
            int(math.floor((minutes + _EPS) / artifact.expected_interval_minutes))
            - 1,
        )
    return missing, long_count


def _local_time_bucket(as_of_utc: str, timezone_name: str) -> str:
    hour = _parsed(as_of_utc).astimezone(ZoneInfo(timezone_name)).hour
    return next(
        name for name, hours in _LOCAL_TIME_BUCKETS.items() if hour in hours
    )


def compile_metar_trajectory_update_proof(
    prediction_root: ProbabilityEnvelope | Mapping[str, Any],
    *,
    observations: Iterable[
        MetarTrajectoryObservation | Mapping[str, Any]
    ],
    artifact: MetarTrajectoryLikelihoodArtifact | Mapping[str, Any],
    bucket_centers_c: Mapping[str, Any],
    bucket_upper_bounds_c: Mapping[str, Any],
    maturity_bin: str,
    as_of_utc: str,
) -> MetarTrajectoryUpdateProof:
    """Compile P1 trajectory evidence from the immutable prediction root."""

    root = (
        prediction_root
        if isinstance(prediction_root, ProbabilityEnvelope)
        else ProbabilityEnvelope.from_mapping(prediction_root)
    )
    governed = (
        artifact
        if isinstance(artifact, MetarTrajectoryLikelihoodArtifact)
        else MetarTrajectoryLikelihoodArtifact.from_mapping(artifact)
    )
    as_of = _timestamp("metar_trajectory_as_of_utc", as_of_utc)
    maturity = _text("metar_trajectory_maturity_bin", maturity_bin)
    if root.stage not in {"P0", "P0-M"}:
        raise ContractError("metar_trajectory_prediction_root_stage_invalid")
    if (
        root.station_id != governed.station_id
        or root.settlement_timezone != governed.settlement_timezone
    ):
        raise ContractError("metar_trajectory_prediction_root_scope_mismatch")
    for key, expected in {
        "season_bin": governed.season_bin,
        "settlement_definition": governed.settlement_definition,
    }.items():
        actual = str(root.lineage.get(key) or "")
        if not actual:
            raise ContractError(f"metar_trajectory_prediction_root_{key}_missing")
        if actual != expected:
            raise ContractError(
                f"metar_trajectory_prediction_root_{key}_mismatch"
            )
    if maturity != governed.maturity_bin:
        raise ContractError("metar_trajectory_maturity_bin_mismatch")
    if _parsed(as_of) < _parsed(root.decision_at_utc):
        raise ContractError("metar_trajectory_prediction_root_future_asof")
    if not timestamp_in_window(
        as_of, governed.valid_from_utc, governed.valid_until_utc
    ):
        raise ContractError("metar_trajectory_artifact_not_active_asof")
    if governed.local_time_bucket != _local_time_bucket(
        as_of, governed.settlement_timezone
    ):
        raise ContractError("metar_trajectory_local_time_bucket_mismatch")
    ordered = canonicalize_metar_trajectory_observations(
        observations,
        as_of_utc=as_of,
        station_id=root.station_id,
        event_date=root.event_date,
        source_lane=governed.source_lane,
        settlement_timezone=root.settlement_timezone,
    )
    if not timestamp_lt(
        governed.trained_through_utc, ordered[0].event.observed_at_utc
    ):
        raise ContractError("metar_trajectory_artifact_training_leakage")
    if (
        not isinstance(bucket_centers_c, Mapping)
        or set(bucket_centers_c) != set(root.probabilities)
        or not isinstance(bucket_upper_bounds_c, Mapping)
        or set(bucket_upper_bounds_c) != set(root.probabilities)
    ):
        raise ContractError("metar_trajectory_bucket_geometry_support_mismatch")
    if len(root.probabilities) > MAX_METAR_TRAJECTORY_BUCKETS:
        raise ContractError("metar_trajectory_bucket_resource_limit_exceeded")
    centers = {
        str(bucket): _finite("metar_trajectory_bucket_center_c", value)
        for bucket, value in bucket_centers_c.items()
    }
    upper_bounds = {
        str(bucket): _finite("metar_trajectory_bucket_upper_bound_c", value)
        for bucket, value in bucket_upper_bounds_c.items()
    }
    ordered_buckets = sorted(
        root.probabilities, key=lambda bucket: root.bucket_ordinals[bucket]
    )
    if (
        any(
            centers[left] >= centers[right]
            or upper_bounds[left] >= upper_bounds[right]
            for left, right in zip(ordered_buckets, ordered_buckets[1:])
        )
        or any(
            centers[bucket] > upper_bounds[bucket]
            for bucket in ordered_buckets
        )
    ):
        raise ContractError("metar_trajectory_bucket_geometry_invalid")
    running_high = max(item.temperature_c for item in ordered)
    conditional_path_state = None
    governed_remaining_distribution = governed.remaining_warming_distribution
    if (
        governed.method_version in {
            METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION,
            METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
        }
    ):
        future_curve = None
        if (
            governed.method_version
            == METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
        ):
            future_curve_raw = root.lineage.get(
                "conditional_future_temperature_curve_json"
            )
            if not future_curve_raw:
                raise ContractError(
                    "metar_trajectory_root_future_curve_missing"
                )
            try:
                future_curve = json.loads(str(future_curve_raw))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ContractError(
                    "metar_trajectory_root_future_curve_invalid"
                ) from exc
            declared_curve_sha = str(
                root.lineage.get(
                    "conditional_future_temperature_curve_sha256"
                )
                or ""
            ).strip().lower()
            if (
                len(declared_curve_sha) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in declared_curve_sha
                )
                or not isinstance(future_curve, Mapping)
                or str(future_curve.get("content_sha256") or "").lower()
                != declared_curve_sha
            ):
                raise ContractError(
                    "metar_trajectory_root_future_curve_sha256_mismatch"
                )
            curve_root_stage = str(
                root.lineage.get(
                    "conditional_future_temperature_curve_root_stage"
                )
                or root.stage
            )
            curve_root_checkpoint_id = str(
                root.lineage.get(
                    "conditional_future_temperature_curve_root_checkpoint_id"
                )
                or root.checkpoint_id
            )
            curve_root_probability_sha256 = str(
                root.lineage.get(
                    "conditional_future_temperature_curve_root_probability_sha256"
                )
                or root.posterior_sha256
            )
            direct_binding = (
                curve_root_stage == root.stage
                and curve_root_checkpoint_id == root.checkpoint_id
                and curve_root_probability_sha256 == root.posterior_sha256
            )
            p0_ancestor_binding = (
                root.stage == "P0-M"
                and curve_root_stage == "P0"
                and curve_root_checkpoint_id
                == str(root.lineage.get("p0_root_checkpoint_id") or "")
                and curve_root_probability_sha256
                == str(root.lineage.get("p0_root_posterior_sha256") or "")
            )
            if not direct_binding and not p0_ancestor_binding:
                raise ContractError(
                    "metar_trajectory_root_future_curve_binding_invalid"
                )
        conditional_path_state = compile_conditional_metar_path_state(
            [
                {
                    "observation_id": item.event.observation_id,
                    "observed_at_utc": item.event.observed_at_utc,
                    "temperature_c": item.temperature_c,
                    "raw_text": item.raw_text,
                }
                for item in ordered
            ],
            as_of_utc=as_of,
            settlement_timezone=root.settlement_timezone,
            root_probabilities=root.probabilities,
            bucket_centers_c=centers,
            future_curve=future_curve,
            root_checkpoint_id=(
                curve_root_checkpoint_id
                if future_curve is not None
                else root.checkpoint_id
            ),
            root_probability_sha256=(
                curve_root_probability_sha256
                if future_curve is not None
                else root.posterior_sha256
            ),
            station_id=root.station_id,
            event_date=root.event_date,
        )
        governed_remaining_distribution = (
            condition_remaining_warming_distribution(
                governed.remaining_warming_distribution,
                conditional_path_state,
                coefficients=governed.path_model_coefficients,
                sigma_c=float(governed.path_model_sigma_c),
            )
        )
    # A bucket is dead exactly when the running high has reached its upper
    # bound: for half-open interval uppers ``[lo, hi)`` the settled max being
    # ``>= hi`` puts it outside the bucket, and for first-killing-value
    # thresholds ``>= threshold`` is the definition.  One ``<=`` predicate is
    # therefore correct for both shapes callers supply.  The previous strict
    # ``<`` was wrong at the boundary for both -- it only looked right on the
    # native path because half-integer interval edges never equal an integer
    # report.  On the threshold path the boundary is hit routinely: LFPB
    # 2026-07-31 carried a running high of 29 against a "28°C" threshold of
    # 29.0, and the provably dead bucket kept 2.6% of the posterior.
    #
    # The eps admits float noise in converted thresholds (an F-market bucket's
    # (U-30.6)/1.8 lands within 1e-15 of its exact value); at 0.5-degree
    # physical granularity it can never revive a genuinely live bucket.
    excluded = tuple(
        bucket for bucket in ordered_buckets
        if upper_bounds[bucket] <= running_high + _EPS
    )
    survivors = [bucket for bucket in ordered_buckets if bucket not in excluded]
    if not survivors or sum(root.probabilities[bucket] for bucket in survivors) <= 0:
        raise ContractError("metar_trajectory_hard_bound_exhausts_support")
    missing, long_count = _interval_diagnostics(ordered, governed)
    reasons: list[str] = []
    promoted = (
        governed.activated
        and governed.promotion_state in _PROMOTED_STATES
    )
    strength = governed.base_strength * governed.maturity_strength
    if not promoted:
        strength = 0.0
        reasons.append("ARTIFACT_NOT_PROMOTED")
    if missing:
        strength *= governed.missing_interval_strength_factor ** missing
        reasons.append("MISSING_INTERVALS")
    if long_count:
        strength *= governed.long_interval_strength_factor ** long_count
        reasons.append("LONG_INTERVALS")
    if missing > governed.max_missing_intervals:
        strength = 0.0
        reasons.append("MISSING_INTERVAL_LIMIT_EXCEEDED")
    if strength <= _EPS:
        strength = 0.0
        ratios = {bucket: 1.0 for bucket in ordered_buckets}
    else:
        ratios = {}
        for bucket in ordered_buckets:
            increment = max(0.0, centers[bucket] - running_high)
            numerator = _nearest_distribution_probability(
                governed_remaining_distribution, increment
            )
            denominator = _nearest_distribution_probability(
                governed.hard_exclusion_baseline_distribution, increment
            )
            if denominator <= 0.0:
                raw_ratio = governed.likelihood_ratio_ceiling
            else:
                raw_ratio = numerator / denominator
            bounded = max(
                governed.likelihood_ratio_floor,
                min(governed.likelihood_ratio_ceiling, raw_ratio),
            )
            ratios[bucket] = math.exp(strength * math.log(bounded))
    soft_weighted = {
        bucket: root.probabilities[bucket] * ratios[bucket]
        for bucket in ordered_buckets
    }
    hard_bound_mask = {
        bucket: 0.0 if bucket in excluded else 1.0
        for bucket in ordered_buckets
    }
    weighted = {
        bucket: soft_weighted[bucket] * hard_bound_mask[bucket]
        for bucket in ordered_buckets
    }
    total = sum(weighted.values())
    if total <= 0.0:
        raise ContractError("metar_trajectory_posterior_zero_mass")
    posterior = {bucket: weighted[bucket] / total for bucket in ordered_buckets}
    if any(posterior[bucket] != 0.0 for bucket in excluded):
        raise ContractError("metar_trajectory_hard_exclusion_revived")
    center = min(
        posterior,
        key=lambda bucket: (
            -posterior[bucket],
            root.bucket_ordinals[bucket],
            bucket,
        ),
    )
    center_ordinal = root.bucket_ordinals[center]
    # Basket anchor for this stage: where the observed path actually points.
    # The posterior argmax above still drives skew/direction, but it is the
    # wrong anchor for the basket -- an open-ended edge bucket spans an
    # unbounded interval and absorbs the whole upper tail, so it can win the
    # argmax over 1°C buckets without the path pointing anywhere near it.
    # P1's own point estimate is the posterior's probability-weighted centre
    # (user ruling, 2026-08-15).  It used to be the running high plus the
    # trajectory layer's expected remaining warming, and that reads correctly
    # only once the trajectory layer is strong: early in the local day it
    # contributes almost nothing, so the anchor collapsed onto the observed
    # high and said, in effect, that the day would not warm further.
    # Measured 2026-08-15: KHOU at 08:55 local anchored at 29C against a P0
    # forecast of 35.6C, KLAX at 07:37 anchored at 21C against 24.2C, and
    # seven of nine P1 seats sat three or more buckets away from their own
    # heaviest bucket -- dragged, by charter 49's containment rule, into cold
    # buckets the market prices at a tenth of a cent.
    #
    # The mean is taken over the posterior that is actually being served, so
    # the observed path is already in it: hard-excluded buckets carry zero
    # mass and cannot pull the anchor down, and the likelihood ratios have
    # already moved the rest.  An open-ended edge bucket contributes its
    # declared centre rather than an unbounded one, which is the same
    # convention every other consumer of this geometry uses.
    posterior_mass = sum(float(value) for value in posterior.values())
    if posterior_mass <= 0.0:
        raise ContractError("metar_trajectory_posterior_zero_mass")
    model_center_c = sum(
        float(posterior[bucket]) * float(centers[bucket])
        for bucket in ordered_buckets
    ) / posterior_mass
    model_center_bucket = next(
        (
            bucket
            for bucket in ordered_buckets
            if model_center_c < upper_bounds[bucket]
        ),
        ordered_buckets[-1],
    )
    # A hard-excluded bucket is one the observed high has already ruled out; it
    # can never be the anchor even if the expected path rounds into it.
    if model_center_bucket in excluded:
        model_center_bucket = next(
            (
                bucket
                for bucket in ordered_buckets
                if bucket not in excluded
            ),
            center,
        )
    skew = (
        sum(
            posterior[bucket] for bucket in ordered_buckets
            if root.bucket_ordinals[bucket] > center_ordinal
        )
        - sum(
            posterior[bucket] for bucket in ordered_buckets
            if root.bucket_ordinals[bucket] < center_ordinal
        )
    )
    direction = "UP" if skew > 1e-9 else "DOWN" if skew < -1e-9 else "NEUTRAL"
    sequence_sha = canonical_observation_sequence_sha256(ordered)
    body = {
        "schema": METAR_TRAJECTORY_PROOF_SCHEMA,
        "artifact_id": governed.artifact_id,
        "artifact_content_sha256": governed.content_sha256,
        "activation_id": governed.activation_id,
        "prediction_root_stage": root.stage,
        "prediction_root_checkpoint_id": root.checkpoint_id,
        "prediction_root_content_sha256": root.content_sha256,
        "station_id": root.station_id,
        "event_date": root.event_date,
        "settlement_timezone": root.settlement_timezone,
        "source_lane": governed.source_lane,
        "maturity_bin": maturity,
        "as_of_utc": as_of,
        "canonical_observation_ids": [
            item.event.observation_id for item in ordered
        ],
        "canonical_observation_sequence_sha256": sequence_sha,
        "running_high_c": running_high,
        "hard_bound_c": running_high,
        "hard_excluded_buckets": list(excluded),
        "hard_bound_mask_by_bucket": hard_bound_mask,
        "likelihood_ratio_by_bucket": ratios,
        "applied_strength": strength,
        "inferred_missing_intervals": missing,
        "long_interval_count": long_count,
        "degradation_reasons": reasons,
        "posterior_probabilities": posterior,
        "center_bucket": center,
        "model_center_bucket": model_center_bucket,
        "model_center_c": model_center_c,
        "direction_skew": skew,
        "direction": direction,
        "positive_core_buckets": [
            bucket for bucket in ordered_buckets if posterior[bucket] > 0.0
        ],
        "conditional_path_state_sha256": (
            None
            if conditional_path_state is None
            else conditional_path_state.content_sha256
        ),
    }
    digest = content_sha256(body)
    proof_body = {"proof_id": "metar-proof-" + digest[:32], **body}
    return MetarTrajectoryUpdateProof.from_mapping({
        **proof_body,
        "content_sha256": content_sha256(proof_body),
    })


def validate_metar_trajectory_update_proof(
    proof: MetarTrajectoryUpdateProof | Mapping[str, Any],
    *,
    prediction_root: ProbabilityEnvelope | Mapping[str, Any],
    observations: Iterable[
        MetarTrajectoryObservation | Mapping[str, Any]
    ],
    artifact: MetarTrajectoryLikelihoodArtifact | Mapping[str, Any],
    bucket_centers_c: Mapping[str, Any],
    bucket_upper_bounds_c: Mapping[str, Any],
) -> MetarTrajectoryUpdateProof:
    """Recompile from authoritative inputs and require byte-stable identity."""

    value = (
        proof
        if isinstance(proof, MetarTrajectoryUpdateProof)
        else MetarTrajectoryUpdateProof.from_mapping(proof)
    )
    expected = compile_metar_trajectory_update_proof(
        prediction_root,
        observations=observations,
        artifact=artifact,
        bucket_centers_c=bucket_centers_c,
        bucket_upper_bounds_c=bucket_upper_bounds_c,
        maturity_bin=value.maturity_bin,
        as_of_utc=value.as_of_utc,
    )
    if value.to_dict() != expected.to_dict():
        raise ContractError("metar_trajectory_proof_replay_mismatch")
    return value


__all__ = [
    "METAR_TRAJECTORY_ARTIFACT_SCHEMA",
    "METAR_TRAJECTORY_AUTHORITY_SCOPE",
    "METAR_TRAJECTORY_CONDITIONAL_PATH_METHOD_VERSION",
    "METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION",
    "METAR_TRAJECTORY_METHOD_VERSION",
    "METAR_TRAJECTORY_OBSERVATION_SCHEMA",
    "METAR_TRAJECTORY_PROOF_SCHEMA",
    "MAX_METAR_TRAJECTORY_BUCKETS",
    "MAX_METAR_TRAJECTORY_FEATURES",
    "MAX_METAR_TRAJECTORY_OBSERVATIONS",
    "MetarTrajectoryLikelihoodArtifact",
    "MetarTrajectoryObservation",
    "MetarTrajectoryUpdateProof",
    "build_metar_trajectory_likelihood_artifact",
    "canonical_observation_sequence_sha256",
    "canonicalize_metar_trajectory_observations",
    "compile_metar_trajectory_update_proof",
    "validate_metar_trajectory_update_proof",
]
