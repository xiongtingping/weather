"""Shadow-only joint forecast weak likelihood with no model-vs-TAF special case.

Every forecast source is an expert.  Historical, context-specific error and
correlation parameters are supplied by one immutable approved artifact.  D0
observations belong to the strong layer and are deliberately rejected here.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from joint_weak_bias_calibration_runtime import SCHEMA_VERSION as BIAS_CALIBRATION_SCHEMA_VERSION, select_joint_weak_bias_state
from weak_likelihood_source_set_runtime import (
    SOURCE_UNIVERSE,
    SURFACE_SCHEMA_VERSION,
    baseline_template_issues,
    select_source_set_surface,
    source_set_key,
)


SCHEMA_VERSION = "joint_forecast_weak_likelihood_v1"
ARTIFACT_SCHEMA_VERSION = "joint_forecast_weak_likelihood_artifact_v3"
EXPERT_SURFACE_SCHEMA_VERSION = "source_expert_continuous_mixture_v1"
MULTIMODALITY_SCHEMA_VERSION = "forecast_multimodality_trace_v1"
SOURCE_MODES_SCHEMA_VERSION = "forecast_source_modes_v1"
EPSILON = 1e-300


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def canonical_artifact_sha256(artifact: Mapping[str, Any]) -> str:
    """Hash exactly the immutable artifact material, never its self-reference."""

    return _sha256({key: value for key, value in _mapping(artifact).items() if key != "artifact_sha256"})


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _normalize(values: Sequence[float]) -> list[float]:
    nonnegative = [max(0.0, float(value)) for value in values]
    total = sum(nonnegative)
    return [value / total for value in nonnegative] if total > 0.0 else []


def _normal_grid_distribution(grid: Sequence[float], *, mean: float, sigma: float) -> list[float]:
    safe_sigma = max(0.05, sigma)
    values = [math.exp(-0.5 * ((temperature - mean) / safe_sigma) ** 2) for temperature in grid]
    return _normalize(values)


def _source_modes(row: Mapping[str, Any], *, peak: float, forecast_sigma: float | None) -> list[dict[str, Any]]:
    """Validate source-native modes without inventing meteorological branches.

    Older immutable revisions contain only one peak.  They map losslessly to a
    singleton mode; this is a schema normalization, not a model fallback or an
    artificial multimodal split.
    """

    raw_modes = row.get("source_modes") if isinstance(row.get("source_modes"), list) else []
    if not raw_modes:
        return [{
            "mode_id": "primary",
            "mode_probability": 1.0,
            "forecast_peak_c": peak,
            "forecast_sigma_c": forecast_sigma,
            "forecast_peak_valid_time": _text(row.get("forecast_peak_valid_time")),
            "hourly_curve": [dict(point) for point in row.get("hourly_curve") or [] if isinstance(point, Mapping)],
            "physical_regime": dict(_mapping(row.get("physical_regime"))),
            "forecast_weather_regime": _text(row.get("forecast_weather_regime")),
            "mode_origin": "singleton_from_immutable_peak_revision",
        }]
    modes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_modes):
        mode = _mapping(raw)
        mode_id = _text(mode.get("mode_id"))
        mode_peak = _finite(mode.get("forecast_peak_c"))
        probability = _finite(mode.get("mode_probability"))
        if not mode_id or mode_id in seen or mode_peak is None or probability is None or probability <= 0.0:
            raise ValueError(f"forecast_source_mode_invalid:{index}")
        seen.add(mode_id)
        modes.append({
            "mode_id": mode_id,
            "mode_probability": float(probability),
            "forecast_peak_c": float(mode_peak),
            "forecast_sigma_c": _finite(mode.get("forecast_sigma_c")),
            "forecast_peak_valid_time": _text(mode.get("forecast_peak_valid_time")),
            "hourly_curve": [dict(point) for point in mode.get("hourly_curve") or [] if isinstance(point, Mapping)],
            "physical_regime": dict(_mapping(mode.get("physical_regime"))),
            "forecast_weather_regime": _text(mode.get("forecast_weather_regime")),
            "mode_origin": _text(mode.get("mode_origin")) or "source_declared",
        })
    normalized = _normalize([float(mode["mode_probability"]) for mode in modes])
    for mode, probability in zip(modes, normalized):
        mode["mode_probability"] = probability
    return modes


def _context_key(context: Mapping[str, Any]) -> str:
    return "|".join(
        f"{key}={_text(context.get(key))}"
        for key in ("season", "weather_regime", "lead_bucket")
    )


def _forbidden_surface(value: Any, *, path: str = "") -> list[str]:
    forbidden_tokens = ("market", "price", "order", "position", "pnl", "edge", "lcb", "observation", "metar")
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = _text(key).lower()
            child_path = f"{path}.{key_text}" if path else key_text
            if any(token in key_text for token in forbidden_tokens):
                found.append(child_path)
            found.extend(_forbidden_surface(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden_surface(child, path=f"{path}[{index}]"))
    return found


def validate_joint_forecast_weak_artifact(artifact: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate only structural/artifact boundary requirements, never fit data."""

    value = _mapping(artifact)
    issues: list[str] = []
    if value.get("schema") != ARTIFACT_SCHEMA_VERSION:
        issues.append("joint_weak_artifact_schema_invalid")
    if not _text(value.get("artifact_sha256")):
        issues.append("joint_weak_artifact_sha256_missing")
    elif _text(value.get("artifact_sha256")) != canonical_artifact_sha256(value):
        issues.append("joint_weak_artifact_sha256_mismatch")
    if _text(value.get("approval_state")) != "approved":
        issues.append("joint_weak_artifact_not_approved")
    if _text(value.get("source_data_policy")) != "forecast_only_asof":
        issues.append("joint_weak_artifact_source_data_policy_invalid")
    bias_correction = _mapping(value.get("bias_correction"))
    if bias_correction.get("schema") not in {BIAS_CALIBRATION_SCHEMA_VERSION, "joint_weak_bias_calibration_v1"}:
        issues.append("joint_weak_artifact_bias_correction_missing_or_invalid")
    elif _text(bias_correction.get("residual_definition")) not in {
        "forecast_peak_c_minus_physical_x_final_c",
        "interval_censored_forecast_error_against_exact_x_or_settlement_bucket",
    }:
        issues.append("joint_weak_artifact_bias_residual_definition_invalid")
    bias_training_days = int(_finite(bias_correction.get("training_event_day_count")) or 0)
    if bias_training_days > 0:
        if _text(bias_correction.get("state_asof_policy")) != "training_event_date_max_strictly_before_scored_event_date":
            issues.append("joint_weak_artifact_bias_asof_policy_invalid")
        try:
            date.fromisoformat(_text(bias_correction.get("training_event_date_max")))
        except ValueError:
            issues.append("joint_weak_artifact_bias_training_event_date_max_invalid")
    catalog = _mapping(value.get("source_catalog"))
    if not catalog:
        issues.append("joint_weak_artifact_source_catalog_missing")
    for source_id, config in catalog.items():
        if not _text(source_id) or not _text(_mapping(config).get("provider_family")):
            issues.append("joint_weak_artifact_source_family_missing")
            break
    surfaces_by_context = _mapping(value.get("source_set_surfaces"))
    template_issues = baseline_template_issues(artifact=value)
    has_exact_baseline_template = bool(value.get("exact_surface_baseline_template")) and not template_issues
    if template_issues and value.get("exact_surface_baseline_template") is not None:
        issues.extend(template_issues)
    if not surfaces_by_context and not has_exact_baseline_template:
        issues.append("joint_weak_artifact_source_set_surfaces_missing")
    for context_key, surfaces in surfaces_by_context.items():
        if not _text(context_key) or not _mapping(surfaces):
            issues.append("joint_weak_artifact_source_set_surface_context_missing")
            break
        for source_set, parameters in _mapping(surfaces).items():
            params = _mapping(parameters)
            if source_set != source_set_key(params.get("available_source_ids") or []):
                issues.append("joint_weak_artifact_source_set_surface_key_invalid")
                break
            available = list(params.get("available_source_ids") or [])
            if params.get("schema") != SURFACE_SCHEMA_VERSION or not isinstance(params.get("source_base_weights"), Mapping):
                issues.append("joint_weak_artifact_source_set_surface_weights_missing")
                break
            if not isinstance(params.get("source_error_calibration"), Mapping) or not isinstance(params.get("source_correlation"), Mapping):
                issues.append("joint_weak_artifact_source_set_surface_error_or_correlation_missing")
                break
            if _finite(params.get("joint_information_scale")) is None or _finite(params.get("information_confidence")) is None:
                issues.append("joint_weak_artifact_source_set_surface_information_missing")
                break
            if not available and (_finite(params.get("joint_information_scale")) != 0.0 or _finite(params.get("information_confidence")) != 0.0):
                issues.append("joint_weak_artifact_empty_source_set_surface_not_neutral")
                break
    if _forbidden_surface(value.get("source_set_surfaces")):
        issues.append("joint_weak_artifact_contains_forbidden_observation_or_market_surface")
    if _forbidden_surface(value.get("exact_surface_baseline_template")):
        issues.append("joint_weak_artifact_baseline_template_contains_forbidden_observation_or_market_surface")
    return {
        "schema": ARTIFACT_SCHEMA_VERSION,
        "ok": not issues,
        "state": "ready" if not issues else "blocked",
        "issues": sorted(set(issues)),
        "artifact_sha256": _text(value.get("artifact_sha256")),
        "computed_artifact_sha256": canonical_artifact_sha256(value),
        "read_only": True,
    }


def _source_set_from_experts(experts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    available = [source for source in SOURCE_UNIVERSE if source in {_text(row.get("source_id")) for row in experts}]
    return {
        "ok": True,
        "source_set_key": source_set_key(available),
        "available_source_ids": available,
        "missing_source_ids": [source for source in SOURCE_UNIVERSE if source not in available],
    }


def _latest_visible_experts(
    source_revisions: Iterable[Mapping[str, Any]],
    *,
    event_date: str,
    decision_as_of: str,
    source_catalog: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    as_of = _parse_time(decision_as_of)
    trace: list[dict[str, Any]] = []
    issues: list[str] = []
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    if as_of is None:
        return [], trace, ["decision_as_of_missing_or_invalid"]
    for raw in source_revisions:
        row = dict(raw) if isinstance(raw, Mapping) else {}
        source_id = _text(row.get("source_id"))
        revision_id = _text(row.get("source_revision_id"))
        available_at = _text(row.get("available_at"))
        available = _parse_time(available_at)
        event_day = _text(row.get("event_date"))[:10]
        trace_row = {
            "source_id": source_id,
            "source_revision_id": revision_id,
            "available_at": available_at,
            "event_date": event_day,
            "included": False,
            "exclusion_reason": "",
        }
        if _text(row.get("source_class")) != "forecast_weak_source":
            trace_row["exclusion_reason"] = "source_class_not_forecast_weak"
            trace.append(trace_row)
            if bool(row.get("hard_eligible")) or _text(row.get("source_class")) == "hard_observation_candidate":
                issues.append("forecast_weak_hard_or_nonforecast_source_injected")
            continue
        forbidden_fields = sorted(
            key
            for key, value in row.items()
            if value not in (None, "", [], {})
            and any(
                token in str(key).lower()
                for token in (
                    "current_temperature",
                    "observed_today",
                    "metar",
                    "market",
                    "order",
                    "position",
                    "pnl",
                    "edge",
                )
            )
        )
        if forbidden_fields:
            trace_row["exclusion_reason"] = "forecast_revision_contains_forbidden_observation_or_market_input"
            trace_row["forbidden_input_fields"] = forbidden_fields
            trace.append(trace_row)
            issues.append("forecast_weak_forbidden_observation_or_market_input_seen")
            continue
        if bool(row.get("hard_eligible")):
            trace_row["exclusion_reason"] = "forecast_source_marked_hard_eligible"
            trace.append(trace_row)
            continue
        if not source_id or not revision_id or available is None:
            trace_row["exclusion_reason"] = "forecast_source_identity_or_available_at_missing"
            trace.append(trace_row)
            continue
        if event_day != _text(event_date)[:10]:
            trace_row["exclusion_reason"] = "forecast_event_date_mismatch"
            trace.append(trace_row)
            continue
        if available > as_of:
            trace_row["exclusion_reason"] = "forecast_revision_after_asof"
            trace.append(trace_row)
            continue
        if source_id not in source_catalog:
            trace_row["exclusion_reason"] = "source_not_in_approved_artifact_catalog"
            trace.append(trace_row)
            continue
        previous = latest.get(source_id)
        if previous is None or available > previous[0] or (available == previous[0] and revision_id > previous[1]["source_revision_id"]):
            if previous is not None:
                previous[1]["trace"]["included"] = False
                previous[1]["trace"]["exclusion_reason"] = "source_revision_superseded_by_latest_visible"
            entry = {"row": row, "trace": trace_row, "source_revision_id": revision_id}
            latest[source_id] = (available, entry)
            trace.append(trace_row)
        else:
            trace_row["exclusion_reason"] = "source_revision_superseded_by_latest_visible"
            trace.append(trace_row)
    experts: list[dict[str, Any]] = []
    for source_id, (_available, entry) in sorted(latest.items()):
        row = entry["row"]
        peak = _finite(row.get("forecast_peak_c"))
        forecast_sigma = _finite(row.get("forecast_sigma_c"))
        if peak is None:
            entry["trace"]["exclusion_reason"] = "forecast_peak_c_missing"
            continue
        entry["trace"]["included"] = True
        hourly_curve = row.get("hourly_curve") if isinstance(row.get("hourly_curve"), list) else []
        try:
            source_modes = _source_modes(row, peak=peak, forecast_sigma=forecast_sigma)
        except ValueError:
            entry["trace"]["included"] = False
            entry["trace"]["exclusion_reason"] = "forecast_source_modes_invalid"
            issues.append("forecast_source_modes_invalid")
            continue
        experts.append({
            "source_id": source_id,
            "source_revision_id": entry["source_revision_id"],
            "forecast_peak_c": peak,
            "forecast_sigma_c": forecast_sigma,
            "hourly_curve": [dict(point) for point in hourly_curve if isinstance(point, Mapping)],
            "source_modes_schema": SOURCE_MODES_SCHEMA_VERSION,
            "source_modes": source_modes,
            "trace": entry["trace"],
        })
    return experts, trace, issues


def _effective_information_weights(
    experts: Sequence[Mapping[str, Any]],
    *,
    source_catalog: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, Any], list[str]]:
    base_weights = _mapping(parameters.get("source_base_weights"))
    family_caps = _mapping(parameters.get("family_information_caps"))
    weights: dict[str, float] = {}
    family_members: dict[str, list[str]] = {}
    issues: list[str] = []
    for expert in experts:
        source_id = _text(expert.get("source_id"))
        weight = _finite(base_weights.get(source_id))
        family = _text(_mapping(source_catalog.get(source_id)).get("provider_family"))
        if weight is None or weight < 0.0 or not family:
            issues.append("source_base_weight_or_family_missing_for_available_expert")
            continue
        weights[source_id] = weight
        family_members.setdefault(family, []).append(source_id)
    if not weights:
        return {}, {}, sorted(set(issues + ["effective_source_weights_missing"]))
    family_trace: dict[str, Any] = {}
    for family, members in family_members.items():
        raw = sum(weights[source_id] for source_id in members)
        cap = _finite(family_caps.get(family))
        if cap is None:
            issues.append("family_information_cap_missing")
            continue
        if cap < 0.0:
            issues.append("family_information_cap_invalid")
            continue
        scale = min(1.0, cap / raw) if raw > 0.0 else 0.0
        for source_id in members:
            weights[source_id] *= scale
        family_trace[family] = {
            "source_ids": sorted(members),
            "raw_information_weight": raw,
            "cap": cap,
            "effective_information_weight": sum(weights[source_id] for source_id in members),
            "cap_hit": scale < 1.0 - 1e-12,
        }
    if issues:
        return {}, family_trace, sorted(set(issues))
    if sum(weights.values()) <= 0.0:
        return {}, family_trace, ["effective_source_weights_zero"]
    return weights, family_trace, []


def _correlation_discount(
    weights: Mapping[str, float],
    correlation: Mapping[str, Any],
) -> tuple[float | None, list[str]]:
    source_ids = sorted(weights)
    total = sum(float(weights[source_id]) for source_id in source_ids)
    if total <= 0.0:
        return None, ["effective_source_weights_zero"]
    normalized = {source_id: float(weights[source_id]) / total for source_id in source_ids}
    quadratic = 0.0
    independent = sum(weight**2 for weight in normalized.values())
    issues: list[str] = []
    for left in source_ids:
        row = _mapping(correlation.get(left))
        for right in source_ids:
            value = _finite(row.get(right))
            # Anti-correlated calibrated residuals are legal.  They increase
            # diversification rather than invalidating an otherwise approved
            # surface; the final discount remains capped at one below.
            if value is None or value < -1.0 or value > 1.0:
                issues.append("source_correlation_matrix_missing_or_invalid")
                continue
            quadratic += normalized[left] * normalized[right] * value
    if issues or quadratic <= 0.0:
        return None, sorted(set(issues or ["source_correlation_quadratic_invalid"]))
    return min(1.0, max(0.0, independent / quadratic)), []


def _multimodality_trace(
    grid: Sequence[float],
    mixture: Sequence[float],
    *,
    source_weights: Mapping[str, float],
    source_catalog: Mapping[str, Any],
    source_distributions: Mapping[str, Sequence[float]],
) -> dict[str, Any]:
    modes: list[dict[str, Any]] = []
    maximum_mass = max((float(value) for value in mixture), default=0.0)
    for index, value in enumerate(mixture):
        left = mixture[index - 1] if index > 0 else -1.0
        right = mixture[index + 1] if index + 1 < len(mixture) else -1.0
        if value >= left and value >= right and (value > left or value > right):
            contributions = {
                source_id: max(0.0, float(source_weights.get(source_id) or 0.0))
                * max(0.0, float(distribution[index]))
                for source_id, distribution in source_distributions.items()
                if index < len(distribution)
            }
            contribution_total = sum(contributions.values())
            contribution_shares = {
                source_id: contribution / contribution_total
                for source_id, contribution in contributions.items()
                if contribution_total > 0.0 and contribution > 0.0
            }
            # A source supports this particular peak only when it contributes
            # materially at the peak location. Listing every available source
            # on every mode would erase the disagreement this trace preserves.
            supporting_source_ids = sorted(
                source_id
                for source_id, share in contribution_shares.items()
                if share >= 0.10
            )
            # Tempering can leave machine-precision plateaus at a grid edge.
            # They are not meteorological modes when no source contributes
            # meaningful local density.
            if (
                contribution_total <= EPSILON
                or not contribution_shares
                or float(value) < maximum_mass * 1e-8
            ):
                continue
            modes.append(
                {
                    "temperature_c": grid[index],
                    "local_mass": value,
                    "supporting_source_ids": supporting_source_ids,
                    "supporting_source_families": sorted(
                        {
                            _text(_mapping(source_catalog.get(source_id)).get("provider_family"))
                            for source_id in supporting_source_ids
                        }
                    ),
                    "source_contribution_shares": {
                        source_id: contribution_shares[source_id]
                        for source_id in sorted(contribution_shares)
                    },
                }
            )
    modes.sort(key=lambda row: (-float(row["local_mass"]), float(row["temperature_c"])))
    return {
        "schema": MULTIMODALITY_SCHEMA_VERSION,
        "continuous_distribution_sha256": _sha256(list(zip(grid, mixture))),
        "mode_count": len(modes),
        "modes": modes,
        "multimodality_state": "multimodal" if len(modes) >= 2 else "unimodal" if modes else "flat_or_unresolved",
        "diagnostic_only": True,
        "changes_probability": False,
        "changes_source_weights": False,
        "creates_strategy_signal": False,
    }


def _reported_integer_c(temperature_c: float) -> int:
    return int(math.floor(float(temperature_c) + 0.5))


def _source_weight_attribution(
    *,
    grid: Sequence[float],
    likelihood: Sequence[float],
    source_distributions: Mapping[str, Sequence[float]],
    source_mode_distributions: Mapping[str, Mapping[str, Any]],
    weights: Mapping[str, float],
    source_catalog: Mapping[str, Any],
    parameters: Mapping[str, Any],
    family_trace: Mapping[str, Any],
    correlation_discount: float,
    joint_information_scale: float,
) -> dict[str, Any]:
    """Freeze the exact source-weight chain and integer-X local ownership.

    Correlation discount is a property of the complete source set in the
    approved surface, not an independently fitted per-source multiplier.  A
    source's final information index therefore allocates that joint tempered
    information by its normalized family-capped mixture share.
    """

    base_weights = _mapping(parameters.get("source_base_weights"))
    total_capped = sum(float(value) for value in weights.values())
    normalized = {
        source_id: float(weight) / total_capped
        for source_id, weight in weights.items()
    }
    applied_scale = float(correlation_discount) * float(joint_information_scale)
    source_rows: list[dict[str, Any]] = []
    for source_id in sorted(weights):
        family = _text(_mapping(source_catalog.get(source_id)).get("provider_family"))
        family_row = _mapping(family_trace.get(family))
        raw_family = float(_finite(family_row.get("raw_information_weight")) or 0.0)
        effective_family = float(_finite(family_row.get("effective_information_weight")) or 0.0)
        source_rows.append(
            {
                "source_id": source_id,
                "provider_family": family,
                "base_information_weight": float(base_weights[source_id]),
                "family_information_cap": float(family_row.get("cap")),
                "family_raw_information_weight": raw_family,
                "family_cap_scale": effective_family / raw_family if raw_family > 0.0 else 0.0,
                "family_capped_information_weight": float(weights[source_id]),
                "normalized_information_share": normalized[source_id],
                "correlation_discount": float(correlation_discount),
                "joint_information_scale": float(joint_information_scale),
                "final_information_index": normalized[source_id] * applied_scale,
            }
        )

    buckets: dict[int, dict[str, Any]] = {}
    for index, temperature in enumerate(grid):
        bucket = _reported_integer_c(temperature)
        row = buckets.setdefault(
            bucket,
            {
                "reported_x_c": bucket,
                "weak_mixture_mass": 0.0,
                "weak_likelihood_mass": 0.0,
                "source_contribution_mass": {source_id: 0.0 for source_id in sorted(weights)},
                "mode_contribution_mass": {scenario_id: 0.0 for scenario_id in sorted(source_mode_distributions)},
            },
        )
        row["weak_likelihood_mass"] += float(likelihood[index])
        for source_id, distribution in source_distributions.items():
            contribution = normalized[source_id] * float(distribution[index])
            row["source_contribution_mass"][source_id] += contribution
            row["weak_mixture_mass"] += contribution
        for scenario_id, mode in source_mode_distributions.items():
            source_id = _text(mode.get("source_id"))
            distribution = mode.get("distribution") or []
            if source_id in normalized and index < len(distribution):
                row["mode_contribution_mass"][scenario_id] += (
                    normalized[source_id]
                    * float(mode.get("mode_probability") or 0.0)
                    * float(distribution[index])
                )
    bucket_rows: list[dict[str, Any]] = []
    for bucket in sorted(buckets):
        row = buckets[bucket]
        total = float(row["weak_mixture_mass"])
        responsibilities = {
            source_id: float(value) / total
            for source_id, value in row["source_contribution_mass"].items()
            if total > 0.0
        }
        dominant = max(responsibilities, key=responsibilities.get) if responsibilities else ""
        mode_responsibilities = {
            scenario_id: float(value) / total
            for scenario_id, value in row["mode_contribution_mass"].items()
            if total > 0.0 and float(value) > 0.0
        }
        bucket_rows.append(
            {
                "reported_x_c": bucket,
                "weak_mixture_mass": total,
                "weak_likelihood_mass": float(row["weak_likelihood_mass"]),
                "local_responsibility_by_source": responsibilities,
                "local_responsibility_by_mode": mode_responsibilities,
                "dominant_source_id": dominant,
                "dominant_scenario_id": (
                    max(mode_responsibilities, key=mode_responsibilities.get)
                    if mode_responsibilities else ""
                ),
            }
        )
    return {
        "schema": "weak_source_weight_attribution_v1",
        "source_rows": source_rows,
        "bucket_rows": bucket_rows,
        "base_information_weight_total": sum(float(base_weights[source_id]) for source_id in weights),
        "family_capped_information_weight_total": total_capped,
        "normalized_information_share_total": sum(normalized.values()),
        "correlation_discount": float(correlation_discount),
        "joint_information_scale": float(joint_information_scale),
        "applied_information_scale": applied_scale,
        "final_information_index_total": sum(row["final_information_index"] for row in source_rows),
        "correlation_discount_scope": "joint_source_set_global",
        "local_responsibility_semantics": "integer_reported_x_integral_of_same_calibrated_source_mixture",
        "frontend_recompute_allowed": False,
    }


def build_joint_forecast_weak_likelihood(
    *,
    temperature_grid_c: Sequence[Any],
    source_revisions: Iterable[Mapping[str, Any]],
    artifact: Mapping[str, Any],
    forecast_context: Mapping[str, Any],
    event_date: str,
    decision_as_of: str,
) -> dict[str, Any]:
    """Build the sole target weak likelihood from immutable forecast evidence.

    The result is shadow-only.  It blocks rather than fabricating a uniform
    likelihood whenever the artifact/context/evidence closure is incomplete.
    """

    grid = sorted({_finite(value) for value in temperature_grid_c if _finite(value) is not None})
    issues: list[str] = []
    if len(grid) < 2:
        issues.append("temperature_grid_missing_or_too_small")
    artifact_contract = validate_joint_forecast_weak_artifact(artifact)
    issues.extend(artifact_contract["issues"])
    bias_correction = _mapping(_mapping(artifact).get("bias_correction"))
    bias_training_days = int(_finite(bias_correction.get("training_event_day_count")) or 0)
    if bias_training_days > 0:
        try:
            scored_event_date = date.fromisoformat(_text(event_date)[:10])
            bias_training_event_date_max = date.fromisoformat(
                _text(bias_correction.get("training_event_date_max"))
            )
        except ValueError:
            issues.append("bias_correction_event_date_order_unverifiable")
        else:
            if bias_training_event_date_max >= scored_event_date:
                issues.append("bias_correction_not_strictly_prior_to_scored_event")
    context = _mapping(forecast_context)
    allowed_context_fields = {"season", "weather_regime", "lead_bucket", "forecast_evidence_snapshot_id"}
    unknown_context_fields = sorted(set(context) - allowed_context_fields)
    if unknown_context_fields:
        issues.append(f"forecast_context_contains_nonforecast_or_unknown_field:{','.join(unknown_context_fields)}")
    context_key = _context_key(context)
    if issues:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(issues)),
            "artifact_sha256": _text(_mapping(artifact).get("artifact_sha256")),
            "context_key": context_key,
            "read_only": True,
            "shadow_only": True,
            "market_data_consumed": False,
            "observation_data_consumed": False,
        }
    catalog = _mapping(_mapping(artifact).get("source_catalog"))
    experts, source_trace, expert_issues = _latest_visible_experts(
        source_revisions,
        event_date=event_date,
        decision_as_of=decision_as_of,
        source_catalog=catalog,
    )
    source_set = _source_set_from_experts(experts)
    surface_selection = select_source_set_surface(
        artifact=artifact,
        source_set=source_set,
        context_key=context_key,
    )
    parameters = _mapping(surface_selection.get("surface"))
    if surface_selection.get("ok") is not True:
        issues.extend(surface_selection.get("issues") or [])
    empty_source_set = not experts and source_set.get("source_set_key") == "empty"
    if empty_source_set:
        weights, family_trace, weight_issues = {}, {}, []
        correlation_discount, correlation_issues = 1.0, []
    else:
        weights, family_trace, weight_issues = _effective_information_weights(
            experts,
            source_catalog=catalog,
            parameters=parameters,
        )
        correlation_discount, correlation_issues = _correlation_discount(
            weights,
            _mapping(parameters.get("source_correlation")),
        ) if weights else (None, ["effective_source_weights_missing"])
    issues.extend(expert_issues)
    issues.extend(weight_issues)
    issues.extend(correlation_issues)
    information_scale = _finite(parameters.get("joint_information_scale"))
    if information_scale is None or (information_scale < 0.0 if empty_source_set else information_scale <= 0.0):
        issues.append("joint_information_scale_invalid")
    if issues:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(issues)),
            "artifact_sha256": _text(_mapping(artifact).get("artifact_sha256")),
            "context_key": context_key,
            "source_trace": source_trace,
            "family_trace": family_trace,
            "weak_source_set": source_set,
            "weak_source_surface_selection": surface_selection,
            "read_only": True,
            "shadow_only": True,
            "market_data_consumed": False,
            "observation_data_consumed": False,
        }
    calibration = _mapping(parameters.get("source_error_calibration"))
    bias_correction = _mapping(_mapping(artifact).get("bias_correction"))
    expert_rows: list[dict[str, Any]] = []
    # The joint weak surface is an explicit mixture of calibrated source
    # scenarios.  Do not use a product/log pool here: under material source
    # disagreement it invents a high-density middle temperature that no
    # forecast scenario actually supports and destroys legitimate modes.
    mixture = [0.0 for _temperature in grid]
    mixture_total_weight = sum(weights.values())
    source_distributions: dict[str, list[float]] = {}
    source_mode_distributions: dict[str, dict[str, Any]] = {}
    for expert in experts:
        source_id = _text(expert.get("source_id"))
        if source_id not in weights:
            continue
        source_calibration = _mapping(calibration.get(source_id))
        residual_distribution = _mapping(source_calibration.get("residual_distribution"))
        residual_scale_c = _finite(residual_distribution.get("scale_c"))
        error_sigma_c = residual_scale_c
        if error_sigma_c is None:
            # v3 surfaces created before the explicit residual-distribution
            # field used error_sigma_c as the Gaussian residual scale.  This
            # is the same model parameter, not a second model/fallback path.
            error_sigma_c = _finite(source_calibration.get("error_sigma_c"))
        if error_sigma_c is None or error_sigma_c <= 0.0:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": ["source_error_calibration_missing_or_invalid"],
                "artifact_sha256": _text(_mapping(artifact).get("artifact_sha256")),
                "context_key": context_key,
                "source_trace": source_trace,
                "read_only": True,
                "shadow_only": True,
                "market_data_consumed": False,
                "observation_data_consumed": False,
            }
        bias_state = select_joint_weak_bias_state(
            calibration=bias_correction,
            source_id=source_id,
            season=_text(context.get("season")),
            weather_regime=_text(context.get("weather_regime")),
            lead_bucket=_text(context.get("lead_bucket")),
            decision_as_of=decision_as_of,
        )
        bias_c = _finite(bias_state.get("bias_c"))
        if bias_c is None:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": ["hierarchical_bias_state_missing_or_invalid"],
                "artifact_sha256": _text(_mapping(artifact).get("artifact_sha256")),
                "context_key": context_key,
                "source_trace": source_trace,
                "read_only": True,
                "shadow_only": True,
                "market_data_consumed": False,
                "observation_data_consumed": False,
            }
        raw_sigma = _finite(expert.get("forecast_sigma_c"))
        calibrated_modes: list[dict[str, Any]] = []
        distribution = [0.0 for _temperature in grid]
        for mode in expert.get("source_modes") or []:
            mode_probability = float(mode["mode_probability"])
            mode_raw_sigma = _finite(mode.get("forecast_sigma_c"))
            effective_raw_sigma = mode_raw_sigma if mode_raw_sigma is not None else raw_sigma
            mode_sigma = math.sqrt((effective_raw_sigma or 0.0) ** 2 + error_sigma_c**2)
            # b=E(forecast-X): subtract positive hot bias, raise negative cold bias.
            mode_mean = float(mode["forecast_peak_c"]) - bias_c
            mode_distribution = _normal_grid_distribution(grid, mean=mode_mean, sigma=mode_sigma)
            for index, probability in enumerate(mode_distribution):
                distribution[index] += mode_probability * probability
            mode_id = _text(mode.get("mode_id"))
            calibrated_modes.append({
                "mode_id": mode_id,
                "scenario_id": f"{_text(expert.get('source_revision_id'))}#{mode_id}",
                "mode_probability": mode_probability,
                "mode_origin": _text(mode.get("mode_origin")),
                "forecast_peak_c": float(mode["forecast_peak_c"]),
                "forecast_peak_valid_time": _text(mode.get("forecast_peak_valid_time")),
                "forecast_sigma_c": effective_raw_sigma,
                "calibration_bias_c": bias_c,
                "calibrated_mean_c": mode_mean,
                "calibrated_sigma_c": mode_sigma,
                "forecast_weather_regime": _text(mode.get("forecast_weather_regime")),
                "physical_regime": dict(_mapping(mode.get("physical_regime"))),
                "hourly_curve": [dict(point) for point in mode.get("hourly_curve") or [] if isinstance(point, Mapping)],
                "continuous_distribution": [
                    {"temperature_c": temperature, "probability": probability}
                    for temperature, probability in zip(grid, mode_distribution)
                ],
            })
            source_mode_distributions[f"{_text(expert.get('source_revision_id'))}#{mode_id}"] = {
                "source_id": source_id,
                "source_revision_id": _text(expert.get("source_revision_id")),
                "source_mode_id": mode_id,
                "mode_probability": mode_probability,
                "distribution": mode_distribution,
            }
        distribution = _normalize(distribution)
        source_distributions[source_id] = distribution
        information_weight = float(weights[source_id])
        for index, probability in enumerate(distribution):
            mixture[index] += (information_weight / mixture_total_weight) * probability
        expert_rows.append(
            {
                "source_id": source_id,
                "source_revision_id": _text(expert.get("source_revision_id")),
                "provider_family": _text(_mapping(catalog.get(source_id)).get("provider_family")),
                "forecast_peak_c": float(expert["forecast_peak_c"]),
                "calibration_bias_c": bias_c,
                "bias_state_id": _text(bias_state.get("bias_state_id")),
                "bias_level": _text(bias_state.get("bias_level")),
                "bias_sample_count": int(bias_state.get("bias_sample_count") or 0),
                "bias_fallback": bias_state.get("bias_fallback"),
                "calibrated_mean_c": sum(
                    float(mode["mode_probability"]) * float(mode["calibrated_mean_c"])
                    for mode in calibrated_modes
                ),
                "forecast_sigma_c": raw_sigma,
                "error_sigma_c": error_sigma_c,
                "residual_distribution": residual_distribution or {
                    "schema": "conditional_source_residual_distribution_v1",
                    "family": "gaussian_scale_from_v3_error_sigma_field",
                    "location_c": 0.0,
                    "scale_c": error_sigma_c,
                    "learned_from_closed_physical_x": bool(source_calibration.get("sample_count")),
                },
                "calibrated_sigma_c": math.sqrt(max(0.0, sum(
                    float(mode["mode_probability"])
                    * (
                        float(mode["calibrated_sigma_c"]) ** 2
                        + float(mode["calibrated_mean_c"]) ** 2
                    )
                    for mode in calibrated_modes
                ) - sum(
                    float(mode["mode_probability"]) * float(mode["calibrated_mean_c"])
                    for mode in calibrated_modes
                ) ** 2)),
                "source_modes_schema": SOURCE_MODES_SCHEMA_VERSION,
                "source_mode_count": len(calibrated_modes),
                "source_modes": calibrated_modes,
                "information_weight": information_weight,
                "hourly_curve_sha256": _sha256(expert.get("hourly_curve") or []),
                "future_forcing_available": bool(expert.get("hourly_curve")),
                "continuous_distribution": [
                    {"temperature_c": temperature, "probability": probability}
                    for temperature, probability in zip(grid, distribution)
                ],
            }
        )
    applied_scale = float(information_scale) * float(correlation_discount)
    if empty_source_set:
        likelihood = [1.0 / len(grid) for _item in grid]
        mixture = list(likelihood)
    else:
        mixture = _normalize(mixture)
        # Modes are emergent, never mandated.  Bias-corrected centres that are
        # close relative to their fitted residual scales can merge naturally;
        # materially separated scenarios remain distinct.  Correlation changes
        # joint information without inventing an unsupported midpoint.
        likelihood = _normalize([max(EPSILON, probability) ** applied_scale for probability in mixture])
    trace = _multimodality_trace(
        grid,
        likelihood,
        source_weights=weights,
        source_catalog=catalog,
        source_distributions=source_distributions,
    )
    source_weight_attribution = _source_weight_attribution(
        grid=grid,
        likelihood=likelihood,
        source_distributions=source_distributions,
        source_mode_distributions=source_mode_distributions,
        weights=weights,
        source_catalog=catalog,
        parameters=parameters,
        family_trace=family_trace,
        correlation_discount=float(correlation_discount),
        joint_information_scale=float(information_scale),
    ) if not empty_source_set else {
        "schema": "weak_source_weight_attribution_v1",
        "source_rows": [],
        "bucket_rows": [],
        "correlation_discount": 1.0,
        "joint_information_scale": float(information_scale),
        "applied_information_scale": 0.0,
        "final_information_index_total": 0.0,
        "correlation_discount_scope": "joint_source_set_global",
        "frontend_recompute_allowed": False,
        "na_reason": "neutral_empty_source_set",
    }
    attribution_by_source = {
        row["source_id"]: row for row in source_weight_attribution["source_rows"]
    }
    for row in expert_rows:
        chain = attribution_by_source.get(_text(row.get("source_id")), {})
        row.update({key: value for key, value in chain.items() if key not in {"source_id", "provider_family"}})
    scenario_paths = [
        {
            "scenario_id": _text(mode.get("scenario_id")),
            "source_id": _text(row.get("source_id")),
            "source_revision_id": _text(row.get("source_revision_id")),
            "source_mode_id": _text(mode.get("mode_id")),
            "source_mode_probability": float(mode.get("mode_probability") or 0.0),
            "mode_origin": _text(mode.get("mode_origin")),
            "forecast_weather_regime": _text(mode.get("forecast_weather_regime")),
            "physical_regime": dict(_mapping(mode.get("physical_regime"))),
            "hourly_curve": list(mode.get("hourly_curve") or []),
            "hourly_curve_sha256": _sha256(mode.get("hourly_curve") or []),
            "future_forcing_available": bool(mode.get("hourly_curve")),
        }
        for row in expert_rows
        for mode in row.get("source_modes") or []
    ]
    candidate_responsibilities: list[dict[str, Any]] = []
    if not empty_source_set:
        for index, temperature in enumerate(grid):
            numerators: dict[str, float] = {}
            scenario_rows: list[tuple[Mapping[str, Any], Mapping[str, Any], float]] = []
            for row in expert_rows:
                source_id = _text(row.get("source_id"))
                for mode in row.get("source_modes") or []:
                    continuous = mode.get("continuous_distribution") or []
                    mode_mass = (
                        float(_mapping(continuous[index]).get("probability") or 0.0)
                        if index < len(continuous)
                        else 0.0
                    )
                    numerator = (
                        max(0.0, float(weights[source_id]))
                        * max(0.0, float(mode.get("mode_probability") or 0.0))
                        * max(0.0, mode_mass)
                    )
                    scenario_id = _text(mode.get("scenario_id"))
                    numerators[scenario_id] = numerator
                    scenario_rows.append((row, mode, numerator))
            denominator = sum(numerators.values())
            if denominator <= 0.0:
                continue
            candidate_responsibilities.append(
                {
                    "continuous_y_c": temperature,
                    "scenarios": [
                        {
                            "scenario_id": _text(mode.get("scenario_id")),
                            "source_id": _text(row.get("source_id")),
                            "source_revision_id": _text(row.get("source_revision_id")),
                            "source_mode_id": _text(mode.get("mode_id")),
                            "responsibility": numerator / denominator,
                        }
                        for row, mode, numerator in scenario_rows
                    ],
                }
            )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "read_only": True,
        "shadow_only": True,
        "market_data_consumed": False,
        "observation_data_consumed": False,
        "artifact_sha256": _text(_mapping(artifact).get("artifact_sha256")),
        "context_key": context_key,
        "weak_source_set": source_set,
        "weak_source_surface_selection": surface_selection,
        "forecast_evidence_snapshot_id": _text(context.get("forecast_evidence_snapshot_id")),
        "formula": "neutral_weak_likelihood_empty_source_set" if empty_source_set else "correlation_aware_tempered_mixture_of_context_calibrated_forecast_experts",
        "temperature_grid_c": grid,
        "joint_forecast_weak_likelihood": [
            {"temperature_c": temperature, "likelihood": probability}
            for temperature, probability in zip(grid, likelihood)
        ],
        "joint_forecast_weak_likelihood_sha256": _sha256(list(zip(grid, likelihood))),
        "source_expert_continuous_mixture": {
            "schema": EXPERT_SURFACE_SCHEMA_VERSION,
            "temperature_grid_c": grid,
            "probabilities": mixture,
            "sha256": _sha256(list(zip(grid, mixture))),
            "diagnostic_only": False,
            "changes_probability": False,
            "changes_source_weights": False,
        },
        "forecast_multimodality_trace": trace,
        "source_weight_attribution": source_weight_attribution,
        "source_expert_trace": expert_rows,
        "forecast_scenario_paths": {
            "schema": "weak_forecast_scenario_paths_v1",
            "paths": scenario_paths,
            "sha256": _sha256(scenario_paths),
            "nwp_or_taf_path_is_transition_control_only": True,
            "changes_weak_likelihood": False,
        },
        "candidate_scenario_responsibilities": {
            "schema": "weak_candidate_scenario_responsibilities_v1",
            "rows": candidate_responsibilities,
            "sha256": _sha256(candidate_responsibilities),
            "posterior_scenario_dimension_added": False,
        },
        "source_trace": source_trace,
        "family_trace": family_trace,
        "correlation_discount": correlation_discount,
        "joint_information_scale": information_scale,
        "applied_information_scale": applied_scale,
        "weak_information_confidence": surface_selection.get("confidence_cap"),
        "weak_evidence_state": "neutral_empty_source_set" if empty_source_set else "forecast_experts_available",
        "mode_formation_policy": parameters.get("mode_formation_policy") or "emergent_from_bias_corrected_distance_residual_overlap_and_source_correlation",
        "force_multimodality": False,
        "force_unimodality": False,
        "boundary": {
            "all_forecasts_are_weak_evidence": True,
            "model_taf_special_case_absent": True,
            "d0_observation_or_trajectory_not_consumed": True,
            "market_or_trading_input_not_consumed": True,
            "weak_distribution_preserves_material_scenarios_but_allows_evidence_based_mode_merging": True,
            "forecast_multimodality_trace_describes_output_distribution": True,
            "runtime_does_not_train_or_change_parameters": True,
            "unsupported_source_combination_fails_closed": True,
            "empty_source_set_is_neutral_not_blocking": True,
            "candidate_scenario_responsibility_derived_from_same_weak_mixture": True,
            "hourly_paths_are_transition_controls_not_second_likelihood": True,
        },
    }


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "canonical_artifact_sha256",
    "EXPERT_SURFACE_SCHEMA_VERSION",
    "MULTIMODALITY_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "build_joint_forecast_weak_likelihood",
    "validate_joint_forecast_weak_artifact",
]
