"""Forecast weak likelihood runtime for hierarchical temperature posterior.

This read-only module turns model, TAF, WU, and TWC forecast observations into
one weak likelihood distribution. It applies source-group shrinkage and
precision caps so duplicated forecast channels do not become hard evidence.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from forecast_source_contextual_weight_model import (
    SCHEMA_VERSION as WEIGHT_ARTIFACT_SCHEMA_VERSION,
    mix_source_distributions,
    predict_contextual_weights,
)

from source_observation_runtime import (
    PRIMARY_TAF_SOURCE_ID,
    REFERENCE_TAF_SOURCE_IDS,
    SOURCE_CLASSES,
    SourceObservation,
    source_defaults,
)


SCHEMA_VERSION = "forecast_weak_likelihood_runtime_v1"
CONTRACT_SCHEMA_VERSION = "forecast_weak_likelihood_contract_v1"
EPS = 1e-12


@dataclass(frozen=True)
class ForecastGroupConfig:
    group: str
    default_sigma_c: float
    default_confidence: float
    precision_cap: float
    shrinkage_strength: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "default_sigma_c": self.default_sigma_c,
            "default_confidence": self.default_confidence,
            "precision_cap": self.precision_cap,
            "shrinkage_strength": self.shrinkage_strength,
        }


DEFAULT_GROUP_CONFIG: dict[str, ForecastGroupConfig] = {
    "model": ForecastGroupConfig("model", default_sigma_c=1.15, default_confidence=0.70, precision_cap=1.25, shrinkage_strength=1.00),
    "taf": ForecastGroupConfig("taf", default_sigma_c=0.90, default_confidence=0.80, precision_cap=1.50, shrinkage_strength=0.01),
    "wu_forecast": ForecastGroupConfig("wu_forecast", default_sigma_c=1.05, default_confidence=0.55, precision_cap=0.70, shrinkage_strength=1.25),
    "twc_forecast": ForecastGroupConfig("twc_forecast", default_sigma_c=1.05, default_confidence=0.55, precision_cap=0.70, shrinkage_strength=1.25),
    "other_forecast": ForecastGroupConfig("other_forecast", default_sigma_c=1.30, default_confidence=0.40, precision_cap=0.45, shrinkage_strength=1.50),
}


def _weight_artifact_state(artifact: Mapping[str, Any] | None) -> tuple[str, list[str]]:
    if not isinstance(artifact, Mapping) or not artifact:
        return "not_configured", []
    issues: list[str] = []
    if artifact.get("schema") != WEIGHT_ARTIFACT_SCHEMA_VERSION:
        issues.append("weight_artifact_schema_invalid")
    if artifact.get("source_data_policy") != "external_only":
        issues.append("weight_artifact_not_external_only")
    if not str(artifact.get("artifact_sha256") or ""):
        issues.append("weight_artifact_sha256_missing")
    if not isinstance(artifact.get("provider_families"), Mapping):
        issues.append("weight_artifact_provider_families_missing")
    if issues:
        return "invalid", issues
    approved = bool(
        artifact.get("production_ready") is True
        and artifact.get("runtime_integrated") is True
        and str(artifact.get("approval_state") or "") == "approved"
    )
    return ("approved" if approved else "shadow_only"), []


def _weight_artifact_lane(
    *,
    artifact: Mapping[str, Any] | None,
    curve_state: Mapping[str, Any] | None,
    source_trace: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
    labels: Sequence[str],
) -> dict[str, Any]:
    state, issues = _weight_artifact_state(artifact)
    output: dict[str, Any] = {
        "schema": "forecast_source_weight_runtime_lane_v1",
        "state": state,
        "issues": issues,
        "artifact_sha256": str((artifact or {}).get("artifact_sha256") or ""),
        "approval_state": str((artifact or {}).get("approval_state") or ""),
        "candidate_distribution": {},
        "base_weights": {},
        "effective_weights": {},
        "curve_state_id": "",
        "curve_applied": False,
        "provider_family_dedup_enabled": False,
        "amsc_curve_reliability_consumed_once": False,
        "apply_to_runtime": False,
        "na_reason": "weight_artifact_not_configured" if state == "not_configured" else "",
    }
    if state in {"not_configured", "invalid"}:
        if state == "invalid":
            output["na_reason"] = "weight_artifact_contract_failed"
        return output
    distributions = {
        str(row.get("source_id") or ""): dict(row.get("distribution") or {})
        for row in source_trace
        if row.get("included") and row.get("source_id") and isinstance(row.get("distribution"), Mapping)
    }
    if not distributions:
        output["state"] = "unavailable"
        output["na_reason"] = "weight_artifact_sources_unavailable"
        return output
    try:
        base_weights = predict_contextual_weights(
            artifact or {},
            context,
            available_entities=sorted(distributions),
        )
    except (TypeError, ValueError) as exc:
        output["state"] = "invalid"
        output["issues"] = [f"weight_artifact_prediction_failed:{type(exc).__name__}"]
        output["na_reason"] = "weight_artifact_prediction_failed"
        return output
    # Retired production behaviour: D0 AMSC curve matching may not mutate a
    # forecast-only weak distribution.  Observation/forecast interaction is
    # owned by the strong likelihood and composer.  Keep only a visible
    # rejection trace while this superseded runtime remains available to
    # offline comparison tools.
    effective_weights = dict(base_weights)
    curve_supplied = isinstance(curve_state, Mapping) and bool(curve_state)
    curve_applied = False
    candidate = mix_source_distributions(distributions, effective_weights)
    uniform_shrinkage = 0.0
    if not candidate or any(label not in candidate for label in labels):
        output["state"] = "unavailable"
        output["na_reason"] = "weight_artifact_candidate_distribution_missing"
        return output
    output.update(
        {
            "base_weights": base_weights,
            "effective_weights": effective_weights,
            "candidate_distribution": {label: float(candidate[label]) for label in labels},
            "curve_state_id": str((curve_state or {}).get("curve_state_id") or "") if curve_applied else "",
            "curve_applied": curve_applied,
            "curve_state_rejected": curve_supplied,
            "curve_state_rejected_reason": "d0_curve_weight_override_retired_strong_likelihood_owns_observation_interaction" if curve_supplied else None,
            "weak_layer_uniform_shrinkage": uniform_shrinkage,
            "provider_family_dedup_enabled": True,
            "amsc_curve_reliability_consumed_once": curve_applied,
            "apply_to_runtime": state == "approved",
            "na_reason": None,
        }
    )
    return output


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _clean_label(value: Any) -> str:
    return str(value or "").strip()


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize(values: Sequence[float]) -> list[float]:
    cleaned = [max(0.0, float(v)) for v in values]
    total = sum(cleaned)
    if total <= EPS:
        return [1.0 / len(cleaned) for _ in cleaned] if cleaned else []
    return [v / total for v in cleaned]


def _weighted_peak_moments(items: Sequence[Mapping[str, Any]]) -> tuple[float | None, float | None]:
    usable: list[tuple[float, float, float]] = []
    for item in items:
        trace = item.get("trace") if isinstance(item.get("trace"), Mapping) else {}
        peak = _to_float(trace.get("forecast_peak_c"))
        sigma = _to_float(trace.get("sigma_c"))
        precision = _to_float(item.get("precision"))
        if peak is None or sigma is None or precision is None or precision <= EPS:
            continue
        usable.append((peak, max(0.05, sigma), precision))
    total_precision = sum(item[2] for item in usable)
    if total_precision <= EPS:
        return None, None
    mean = sum(peak * precision for peak, _sigma, precision in usable) / total_precision
    variance = sum(
        precision * (sigma**2 + (peak - mean) ** 2)
        for peak, sigma, precision in usable
    ) / total_precision
    return float(mean), math.sqrt(max(0.05**2, variance))


def _round_float(value: float | None, ndigits: int = 12) -> float | None:
    if value is None:
        return None
    return round(float(value), ndigits)


def _coerce_source_row(row: Any) -> dict[str, Any]:
    if isinstance(row, SourceObservation):
        return row.as_dict(include_payload=True)
    if isinstance(row, Mapping):
        return dict(row)
    return {}


def _source_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _first_float(row: Mapping[str, Any], payload: Mapping[str, Any], *fields: str) -> float | None:
    for field in fields:
        parsed = _to_float(row.get(field))
        if parsed is not None:
            return parsed
        parsed = _to_float(payload.get(field))
        if parsed is not None:
            return parsed
    return None


def _first_text(row: Mapping[str, Any], payload: Mapping[str, Any], *fields: str) -> str:
    for field in fields:
        value = str(row.get(field) or "").strip()
        if value:
            return value
        value = str(payload.get(field) or "").strip()
        if value:
            return value
    return ""


def _source_class(row: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    source_class = _first_text(row, payload, "source_class")
    if source_class:
        return source_class
    source_id = _first_text(row, payload, "source_id", "source")
    return str(source_defaults(source_id).get("source_class") or SOURCE_CLASSES["unknown"])


def _source_group(row: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    explicit = _first_text(row, payload, "forecast_group", "source_group", "group")
    if explicit:
        return explicit
    source_id = _first_text(row, payload, "source_id", "source")
    role = _first_text(row, payload, "role")
    haystack = f"{source_id} {role}".lower()
    if any(token in haystack for token in ("ecmwf", "gfs", "icon", "meteoblue", "model")):
        return "model"
    if "taf" in haystack:
        return "taf"
    if "wu" in haystack or "wunderground" in haystack:
        return "wu_forecast"
    if "twc" in haystack or "weather_com" in haystack or "weather.com" in haystack:
        return "twc_forecast"
    return "other_forecast"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _hard_observation_fields_present(row: Mapping[str, Any], payload: Mapping[str, Any]) -> list[str]:
    fields: list[str] = []
    for field in (
        "temperature_c",
        "observed_today_max_c",
        "hard_lock",
        "hard_source_direct",
        "hard_source_direct_ssot_write",
        "settlement_actual",
        "official_final_result",
    ):
        if row.get(field) not in (None, "") or payload.get(field) not in (None, ""):
            fields.append(field)
    return fields


def _bucket_bounds(label: str, rounding_rule: str = "rounded_degrees") -> tuple[float, float] | None:
    raw = str(label or "").strip().replace("–", "-")
    if not raw:
        return None
    num = r"(-?\d+(?:\.\d+)?)"
    rounded = str(rounding_rule or "").strip().lower() in {"rounded_degrees", "whole_degrees"}
    match = re.match(rf"^{num}\s*-\s*{num}", raw)
    if not match:
        match = re.match(rf"^{num}\s+to\s+{num}", raw, flags=re.I)
    if match:
        low = min(float(match.group(1)), float(match.group(2)))
        high = max(float(match.group(1)), float(match.group(2)))
        return (low - 0.5, high + 0.5) if rounded else (low, high)
    match = re.match(rf"^{num}\s*(?:°?[CF])?\s+or\s+below$", raw, flags=re.I)
    if match:
        high = float(match.group(1))
        return float("-inf"), high + 0.5 if rounded else high
    match = re.match(rf"^{num}\s*(?:°?[CF])?\s+or\s+(?:higher|above|more)$", raw, flags=re.I)
    if match:
        low = float(match.group(1))
        return low - 0.5 if rounded else low, float("inf")
    match = re.match(rf"^{num}\s*(?:°?[CF])?$", raw, flags=re.I)
    if match:
        center = float(match.group(1))
        return center - 0.5, center + 0.5
    return None


def _normal_cdf(x: float, mean: float, sigma: float) -> float:
    if math.isinf(x):
        return 0.0 if x < 0.0 else 1.0
    z = (float(x) - float(mean)) / max(EPS, float(sigma))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _normal_bucket_probs(labels: Sequence[str], *, peak_c: float, sigma_c: float, rounding_rule: str) -> list[float]:
    probs: list[float] = []
    fallback = 1.0 / len(labels) if labels else 0.0
    for label in labels:
        bounds = _bucket_bounds(label, rounding_rule=rounding_rule)
        if bounds is None:
            probs.append(fallback)
            continue
        low, high = bounds
        probs.append(max(0.0, _normal_cdf(high, peak_c, sigma_c) - _normal_cdf(low, peak_c, sigma_c)))
    return _normalize(probs)


def _explicit_bucket_distribution(
    labels: Sequence[str],
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[float] | None:
    raw = row.get("distribution")
    if not isinstance(raw, Mapping):
        raw = payload.get("distribution")
    if not isinstance(raw, Mapping):
        raw = row.get("bucket_probabilities")
    if not isinstance(raw, Mapping):
        raw = payload.get("bucket_probabilities")
    if not isinstance(raw, Mapping):
        return None
    values: list[float] = []
    matched = 0
    for label in labels:
        parsed = _to_float(raw.get(label))
        if parsed is None:
            values.append(0.0)
            continue
        values.append(max(0.0, float(parsed)))
        matched += 1
    if matched <= 0 or sum(values) <= EPS:
        return None
    return _normalize(values)


def _group_config(group: str, overrides: Mapping[str, Any] | None = None) -> ForecastGroupConfig:
    base = DEFAULT_GROUP_CONFIG.get(group, DEFAULT_GROUP_CONFIG["other_forecast"])
    cfg = (overrides or {}).get(group) if isinstance(overrides, Mapping) else None
    if not isinstance(cfg, Mapping):
        return base
    return ForecastGroupConfig(
        group=group,
        default_sigma_c=max(0.05, float(_to_float(cfg.get("default_sigma_c")) or base.default_sigma_c)),
        default_confidence=max(0.0, min(1.0, float(_to_float(cfg.get("default_confidence")) or base.default_confidence))),
        precision_cap=max(0.0, float(_to_float(cfg.get("precision_cap")) or base.precision_cap)),
        shrinkage_strength=max(0.0, float(_to_float(cfg.get("shrinkage_strength")) or base.shrinkage_strength)),
    )


def _source_precision(confidence: float, sigma_c: float, explicit_precision: float | None = None) -> float:
    if explicit_precision is not None:
        return max(0.0, float(explicit_precision))
    return max(0.0, float(confidence)) / max(0.05, float(sigma_c)) ** 2


def _distribution_map(labels: Sequence[str], values: Sequence[float]) -> dict[str, float]:
    return {label: float(_round_float(float(value)) or 0.0) for label, value in zip(labels, values)}


def build_forecast_weak_likelihood(
    *,
    buckets: Sequence[Any],
    source_observations: Iterable[Any] | None = None,
    rounding_rule: str = "rounded_degrees",
    event_date: str = "",
    phase: str = "",
    as_of: Any = None,
    group_config: Mapping[str, Any] | None = None,
    weight_artifact: Mapping[str, Any] | None = None,
    weight_context: Mapping[str, Any] | None = None,
    curve_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    labels = [_clean_label(item.get("bucket") if isinstance(item, Mapping) else item) for item in (buckets or [])]
    labels = [label for label in labels if label]
    na_reasons: list[str] = []
    source_trace: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    uniform = [1.0 / len(labels) for _ in labels] if labels else []

    if not labels:
        na_reasons.append("bucket_labels_missing")

    source_rows = [_coerce_source_row(raw_row) for raw_row in (source_observations or [])]
    as_of_dt = _parse_datetime(as_of)
    primary_taf_available = False
    for row in source_rows:
        payload = _source_payload(row)
        source_id = _first_text(row, payload, "source_id", "source")
        if source_id != PRIMARY_TAF_SOURCE_ID:
            continue
        source_class = _source_class(row, payload)
        hard_eligible = _truthy(row.get("hard_eligible")) or _truthy(payload.get("hard_eligible"))
        forecast_peak_c = _first_float(row, payload, "forecast_peak_c", "peak_c", "value_c", "taf_forecast_peak_c")
        source_event_date = _first_text(row, payload, "event_date", "target_event_date")[:10]
        event_date_aligned = not event_date or not source_event_date or source_event_date == str(event_date)[:10]
        primary_fetched_dt = _parse_datetime(_first_text(row, payload, "fetched_at", "response_completed_at", "parsed_at"))
        as_of_aligned = as_of_dt is None or primary_fetched_dt is None or (primary_fetched_dt - as_of_dt).total_seconds() <= 60.0
        if source_class == SOURCE_CLASSES["forecast_weak"] and not hard_eligible and forecast_peak_c is not None and event_date_aligned and as_of_aligned:
            primary_taf_available = True
            break

    for row in source_rows:
        payload = _source_payload(row)
        source_id = _first_text(row, payload, "source_id", "source")
        source_event_id = _first_text(row, payload, "source_event_id")
        source_observation_id = _first_text(row, payload, "source_observation_id")
        role = _first_text(row, payload, "role")
        source_class = _source_class(row, payload)
        group = _source_group(row, payload)
        source_event_date = _first_text(row, payload, "event_date", "target_event_date")[:10]
        valid_time = _first_text(row, payload, "valid_time", "valid_time_local", "forecast_valid_time", "taf_forecast_peak_at")
        fetched_at = _first_text(row, payload, "fetched_at", "response_completed_at", "parsed_at")
        hard_eligible = _truthy(row.get("hard_eligible")) or _truthy(payload.get("hard_eligible"))
        hard_fields = _hard_observation_fields_present(row, payload)
        trace_row = {
            "source_event_id": source_event_id,
            "source_observation_id": source_observation_id,
            "source_id": source_id,
            "source_class": source_class,
            "role": role,
            "group": group,
            "event_date": source_event_date,
            "valid_time": valid_time,
            "fetched_at": fetched_at,
            "hard_eligible": hard_eligible,
            "hard_observation_fields_present": hard_fields,
            "hard_observation_fields_ignored": bool(hard_fields),
            "included": False,
            "exclusion_reason": "",
        }
        if source_class != SOURCE_CLASSES["forecast_weak"]:
            trace_row["exclusion_reason"] = "source_class_not_forecast_weak"
            source_trace.append(trace_row)
            continue
        if event_date and source_event_date and source_event_date != str(event_date)[:10]:
            trace_row["exclusion_reason"] = "forecast_event_date_mismatch"
            source_trace.append(trace_row)
            continue
        fetched_dt = _parse_datetime(fetched_at)
        if as_of_dt is not None and fetched_dt is not None and (fetched_dt - as_of_dt).total_seconds() > 60.0:
            trace_row["exclusion_reason"] = "forecast_future_fetch_time"
            source_trace.append(trace_row)
            na_reasons.append("forecast_future_fetch_time_excluded")
            continue
        if hard_eligible:
            trace_row["exclusion_reason"] = "forecast_source_marked_hard_eligible"
            source_trace.append(trace_row)
            continue
        if primary_taf_available and source_id in REFERENCE_TAF_SOURCE_IDS:
            trace_row["exclusion_reason"] = "taf_reference_suppressed_by_amsc_primary"
            trace_row["reference_suppressed_by"] = PRIMARY_TAF_SOURCE_ID
            source_trace.append(trace_row)
            continue
        forecast_peak_c = _first_float(
            row,
            payload,
            "forecast_peak_c",
            "peak_c",
            "value_c",
            "wu_forecast_peak_c",
            "twc_forecast_peak_c",
            "weather_com_forecast_peak_c",
            "taf_forecast_peak_c",
            "ecmwf_model_peak_c",
            "gfs_model_peak_c",
            "icon_model_peak_c",
            "meteoblue_model_peak_c",
        )
        explicit_distribution = _explicit_bucket_distribution(labels, row, payload)
        if forecast_peak_c is None and explicit_distribution is None:
            trace_row["exclusion_reason"] = "forecast_peak_c_missing"
            source_trace.append(trace_row)
            continue
        cfg = _group_config(group, group_config)
        sigma_c = _first_float(row, payload, "sigma_c", "forecast_sigma_c", "std_c")
        if sigma_c is None or sigma_c <= 0:
            sigma_c = cfg.default_sigma_c
        confidence = _first_float(row, payload, "confidence", "forecast_confidence")
        if confidence is None:
            confidence = cfg.default_confidence
        confidence = max(0.0, min(1.0, float(confidence)))
        explicit_precision = _first_float(row, payload, "precision", "forecast_precision")
        precision = _source_precision(confidence, sigma_c, explicit_precision)
        distribution = (
            explicit_distribution
            if explicit_distribution is not None
            else _normal_bucket_probs(
                labels,
                peak_c=float(forecast_peak_c),
                sigma_c=float(sigma_c),
                rounding_rule=rounding_rule,
            )
        )
        trace_row.update(
            {
                "included": True,
                "forecast_peak_c": float(forecast_peak_c) if forecast_peak_c is not None else None,
                "distribution_source": "explicit_bucket_distribution" if explicit_distribution is not None else "forecast_peak_normal",
                "sigma_c": float(sigma_c),
                "confidence": confidence,
                "precision_raw": precision,
                "distribution": _distribution_map(labels, distribution),
            }
        )
        source_trace.append(trace_row)
        groups.setdefault(group, []).append({"trace": trace_row, "distribution": distribution, "precision": precision})

    if not groups:
        if labels:
            na_reasons.append("forecast_weak_sources_missing_or_unusable")
        likelihood = uniform
        group_trace: list[dict[str, Any]] = []
        total_raw_precision = 0.0
        total_capped_precision = 0.0
    else:
        log_scores = [0.0 for _ in labels]
        group_trace = []
        total_raw_precision = 0.0
        total_capped_precision = 0.0
        for group, items in sorted(groups.items()):
            cfg = _group_config(group, group_config)
            raw_precision = sum(float(item["precision"]) for item in items)
            capped_precision = min(raw_precision, cfg.precision_cap)
            total_raw_precision += raw_precision
            total_capped_precision += capped_precision
            if raw_precision <= EPS:
                group_distribution = list(uniform)
            else:
                group_distribution = [
                    sum(float(item["precision"]) * item["distribution"][idx] for item in items) / raw_precision
                    for idx in range(len(labels))
                ]
                group_distribution = _normalize(group_distribution)
            shrinkage_alpha = (
                capped_precision / (capped_precision + cfg.shrinkage_strength)
                if capped_precision > EPS or cfg.shrinkage_strength > EPS
                else 0.0
            )
            shrunk_distribution = _normalize(
                [
                    shrinkage_alpha * group_distribution[idx] + (1.0 - shrinkage_alpha) * uniform[idx]
                    for idx in range(len(labels))
                ]
            )
            peak_mean_c, sigma_effective_c = _weighted_peak_moments(items)
            for idx, value in enumerate(shrunk_distribution):
                log_scores[idx] += capped_precision * math.log(max(EPS, value))
            group_trace.append(
                {
                    "group": group,
                    "source_count": len(items),
                    "included_source_event_ids": [
                        str(item["trace"].get("source_event_id") or "") for item in items if str(item["trace"].get("source_event_id") or "")
                    ],
                    "raw_precision": _round_float(raw_precision),
                    "capped_precision": _round_float(capped_precision),
                    "precision_cap": cfg.precision_cap,
                    "cap_hit": bool(raw_precision > cfg.precision_cap + EPS),
                    "shrinkage_alpha": _round_float(shrinkage_alpha),
                    "forecast_peak_mean_c": _round_float(peak_mean_c),
                    "forecast_sigma_effective_c": _round_float(sigma_effective_c),
                    "config": cfg.as_dict(),
                    "distribution": _distribution_map(labels, group_distribution),
                    "shrunk_distribution": _distribution_map(labels, shrunk_distribution),
                }
            )
        max_log = max(log_scores) if log_scores else 0.0
        likelihood = _normalize([math.exp(score - max_log) for score in log_scores])

    contextual_weight_lane = _weight_artifact_lane(
        artifact=weight_artifact,
        curve_state=curve_state,
        source_trace=source_trace,
        context={
            "event_date_local": str(event_date or "")[:10],
            "lead_hours": (weight_context or {}).get("lead_hours"),
            "context": (
                dict((weight_context or {}).get("context") or {})
                if isinstance((weight_context or {}).get("context"), Mapping)
                else {}
            ),
        },
        labels=labels,
    )
    champion_likelihood = list(likelihood)
    if contextual_weight_lane.get("apply_to_runtime") is True:
        likelihood = _normalize(
            [float(contextual_weight_lane["candidate_distribution"].get(label) or 0.0) for label in labels]
        )
    candidate_values = [
        float(contextual_weight_lane.get("candidate_distribution", {}).get(label) or 0.0)
        for label in labels
    ]
    candidate_tv_distance = (
        0.5 * sum(abs(left - right) for left, right in zip(champion_likelihood, candidate_values))
        if candidate_values and sum(candidate_values) > EPS
        else None
    )

    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(labels),
        "read_only": True,
        "production_entry_enabled": False,
        "guards": _guards(),
        "event_date": str(event_date or ""),
        "phase": str(phase or ""),
        "rounding_rule": str(rounding_rule or ""),
        "formula": (
            "approved_contextual_provider_family_mixture"
            if contextual_weight_lane.get("apply_to_runtime") is True
            else "symmetric_group_precision_geometric_pool_with_group_precision_caps"
        ),
        "forecast_weak_likelihood": [
            {"bucket": label, "forecast_weak_likelihood": _round_float(value)} for label, value in zip(labels, likelihood)
        ],
        "forecast_weak_likelihood_map": _distribution_map(labels, likelihood),
        "normalization": {
            "bucket_count": len(labels),
            "forecast_weak_likelihood_sum": _round_float(sum(likelihood)),
        },
        "source_trace": source_trace,
        "source_group_trace": group_trace,
        "forecast_group_pooling": {
            "schema": "forecast_group_precision_pool_v1",
            "state": "available" if groups else "not_available",
            "pooling_mode": "symmetric_group_precision_geometric_pool" if groups else "neutral_uniform",
            "included_groups": sorted(groups),
            "source_group_count": len(groups),
            "legacy_model_taf_special_case_retired": True,
        },
        "contextual_weight_lane": {
            **contextual_weight_lane,
            "champion_distribution": _distribution_map(labels, champion_likelihood),
            "candidate_tv_distance": _round_float(candidate_tv_distance),
            "curve_reweighting_changes_total_weak_precision": False,
        },
        "precision_cap": {
            "total_raw_precision": _round_float(total_raw_precision),
            "total_capped_precision": _round_float(total_capped_precision),
            "total_cap_hit": bool(total_raw_precision > total_capped_precision + EPS),
        },
        "na_reasons": sorted(set(reason for reason in na_reasons if reason)),
        "boundary": {
            "role": "prediction_weak_likelihood",
            "weak_sources_never_hard_lock": True,
            "diagnostic_current_sources_excluded": True,
            "hard_observation_sources_excluded": True,
            "market_sources_excluded": True,
            "source_group_shrinkage_enabled": True,
            "precision_cap_enabled": True,
            "legacy_model_taf_special_case_retired": True,
            "d0_observation_trajectory_not_consumed_in_weak_runtime": True,
            "provider_family_dedup_enabled": bool(contextual_weight_lane.get("provider_family_dedup_enabled")),
            "amsc_curve_reliability_consumed_once": bool(contextual_weight_lane.get("amsc_curve_reliability_consumed_once")),
            "runtime_requests_never_change_parameters": True,
        },
        "next_boundary": "sequential_remaining_warming_bayes_runtime_no_standalone_production_entry",
    }


def build_forecast_weak_likelihood_contract(forecast_output: Mapping[str, Any] | None) -> dict[str, Any]:
    output = forecast_output if isinstance(forecast_output, Mapping) else {}
    issues: list[str] = []
    if output.get("schema") != SCHEMA_VERSION:
        issues.append("forecast_weak_likelihood_schema_invalid")
    likelihood_map = output.get("forecast_weak_likelihood_map")
    if not isinstance(likelihood_map, Mapping) or not likelihood_map:
        issues.append("forecast_weak_likelihood_map_missing")
        likelihood_sum = None
    else:
        likelihood_sum = 0.0
        for value in likelihood_map.values():
            parsed = _to_float(value)
            if parsed is None:
                issues.append("forecast_weak_likelihood_contains_non_numeric_probability")
                likelihood_sum = None
                break
            likelihood_sum += max(0.0, float(parsed))
        if likelihood_sum is not None and abs(float(likelihood_sum) - 1.0) > 1e-6:
            issues.append("forecast_weak_likelihood_not_normalized")
    boundary = output.get("boundary") if isinstance(output.get("boundary"), Mapping) else {}
    for key in (
        "weak_sources_never_hard_lock",
        "diagnostic_current_sources_excluded",
        "hard_observation_sources_excluded",
        "market_sources_excluded",
        "source_group_shrinkage_enabled",
        "precision_cap_enabled",
        "legacy_model_taf_special_case_retired",
        "d0_observation_trajectory_not_consumed_in_weak_runtime",
    ):
        if boundary.get(key) is not True:
            issues.append(f"boundary_{key}_missing")
    source_trace = output.get("source_trace") if isinstance(output.get("source_trace"), list) else []
    included_sources = [row for row in source_trace if isinstance(row, Mapping) and row.get("included")]
    for row in included_sources:
        if row.get("source_class") != SOURCE_CLASSES["forecast_weak"]:
            issues.append("included_source_not_forecast_weak")
        if row.get("hard_eligible") is True:
            issues.append("included_source_marked_hard_eligible")
        if row.get("hard_observation_fields_present") and row.get("hard_observation_fields_ignored") is not True:
            issues.append("included_hard_observation_fields_not_marked_ignored")
    for row in source_trace:
        if not isinstance(row, Mapping) or row.get("included"):
            continue
        source_class = str(row.get("source_class") or "")
        if source_class in {
            SOURCE_CLASSES["hard_observation"],
            SOURCE_CLASSES["diagnostic_current"],
            SOURCE_CLASSES["market"],
            SOURCE_CLASSES["settlement"],
        } and row.get("exclusion_reason") != "source_class_not_forecast_weak":
            issues.append("non_forecast_source_exclusion_reason_invalid")
    group_trace = output.get("source_group_trace") if isinstance(output.get("source_group_trace"), list) else []
    if included_sources and not group_trace:
        issues.append("source_group_trace_missing_for_included_sources")
    for group in group_trace:
        if not isinstance(group, Mapping):
            continue
        capped = _to_float(group.get("capped_precision"))
        cap = _to_float(group.get("precision_cap"))
        if capped is None:
            issues.append("group_capped_precision_missing")
        if cap is None:
            issues.append("group_precision_cap_missing")
        if capped is not None and cap is not None and capped > cap + 1e-9:
            issues.append("group_capped_precision_exceeds_cap")
        if "shrinkage_alpha" not in group:
            issues.append("group_shrinkage_alpha_missing")
    pooling = output.get("forecast_group_pooling") if isinstance(output.get("forecast_group_pooling"), Mapping) else {}
    if pooling.get("schema") != "forecast_group_precision_pool_v1":
        issues.append("forecast_group_pooling_trace_missing")
    if pooling.get("legacy_model_taf_special_case_retired") is not True:
        issues.append("legacy_model_taf_special_case_retirement_missing")
    precision = output.get("precision_cap") if isinstance(output.get("precision_cap"), Mapping) else {}
    raw_total = _to_float(precision.get("total_raw_precision"))
    capped_total = _to_float(precision.get("total_capped_precision"))
    if raw_total is not None and capped_total is not None and capped_total > raw_total + 1e-9:
        issues.append("total_capped_precision_exceeds_raw_precision")
    return {
        "schema": CONTRACT_SCHEMA_VERSION,
        "ok": not issues,
        "state": "ready" if not issues else "blocked",
        "issues": sorted(set(issues)),
        "included_source_count": len(included_sources),
        "excluded_source_count": len(source_trace) - len(included_sources),
        "source_group_count": len(group_trace),
        "forecast_weak_likelihood_sum": _round_float(likelihood_sum),
        "total_raw_precision": raw_total,
        "total_capped_precision": capped_total,
        "boundary": dict(boundary),
    }


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "DEFAULT_GROUP_CONFIG",
    "SCHEMA_VERSION",
    "build_forecast_weak_likelihood",
    "build_forecast_weak_likelihood_contract",
]
