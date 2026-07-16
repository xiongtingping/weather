"""Canonical source-set contract for the one weak-likelihood main model.

This is a model-input boundary, not a fallback or a hand-tuned weighting
layer.  It makes every absent forecast source explicit so an approved artifact
can select the exact calibration/error/correlation surface for the available
combination.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "weak_likelihood_source_set_v1"
SURFACE_SCHEMA_VERSION = "weak_likelihood_source_set_surface_v1"
SOURCE_UNIVERSE = ("ecmwf", "icon", "gfs", "jma", "taf", "wu", "twc")
BASELINE_TEMPLATE_SCHEMA = "joint_weak_exact_surface_baseline_template_v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _ids(value: Any) -> list[str]:
    rows = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else []
    return [source for source in SOURCE_UNIVERSE if source in {str(item).strip() for item in rows}]


def source_set_key(available_source_ids: Sequence[str]) -> str:
    ordered = _ids(available_source_ids)
    return "+".join(ordered) if ordered else "empty"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _baseline_template_contract(template: Mapping[str, Any] | None, catalog: Mapping[str, Any]) -> list[str]:
    """Validate a compact, deterministic exact-surface baseline template.

    It is intentionally not a nearest-context fallback.  For each supplied
    context and availability mask the template produces one exact surface with
    a content-addressed identity derived from the artifact SHA and the full
    source-set mask.  This keeps live operation available before the first
    closed-X training cohort without reusing any legacy model coefficients.
    """

    value = _mapping(template)
    issues: list[str] = []
    if value.get("schema") != BASELINE_TEMPLATE_SCHEMA:
        return ["joint_weak_baseline_template_schema_invalid"]
    if value.get("surface_generation") != "deterministic_exact_context_and_source_set_v1":
        issues.append("joint_weak_baseline_template_generation_policy_invalid")
    if value.get("no_nearest_context_or_source_set_fallback") is not True:
        issues.append("joint_weak_baseline_template_no_fallback_guard_missing")
    weights = _mapping(value.get("source_base_weights"))
    errors = _mapping(value.get("source_error_sigma_c"))
    families = {_mapping(catalog.get(source)).get("provider_family") for source in SOURCE_UNIVERSE}
    caps = _mapping(value.get("family_information_caps"))
    correlations = _mapping(value.get("family_pair_correlation"))
    for source in SOURCE_UNIVERSE:
        if _number(weights.get(source)) is None or _number(weights.get(source)) <= 0.0:
            issues.append("joint_weak_baseline_template_source_weight_missing_or_invalid")
            break
        if _number(errors.get(source)) is None or _number(errors.get(source)) <= 0.0:
            issues.append("joint_weak_baseline_template_source_error_missing_or_invalid")
            break
    for family in families:
        if not str(family or "").strip() or _number(caps.get(str(family))) is None:
            issues.append("joint_weak_baseline_template_family_cap_missing_or_invalid")
            break
    clean_families = sorted(str(family) for family in families if str(family or "").strip())
    for index, left in enumerate(clean_families):
        for right in clean_families[index:]:
            key = "|".join(sorted((left, right)))
            correlation = _number(correlations.get(key))
            if correlation is None or correlation < 0.0 or correlation > 1.0:
                issues.append("joint_weak_baseline_template_family_correlation_missing_or_invalid")
                break
        if issues:
            break
    for field in ("base_joint_information_scale", "base_information_confidence", "maximum_joint_information_scale"):
        value_number = _number(value.get(field))
        if value_number is None or value_number < 0.0 or value_number > 1.0:
            issues.append(f"joint_weak_baseline_template_{field}_invalid")
    return sorted(set(issues))


def baseline_template_issues(*, artifact: Mapping[str, Any] | None) -> list[str]:
    value = _mapping(artifact)
    return _baseline_template_contract(
        _mapping(value.get("exact_surface_baseline_template")),
        _mapping(value.get("source_catalog")),
    )


def _family_pair_correlation(template: Mapping[str, Any], left: str, right: str) -> float:
    key = "|".join(sorted((left, right)))
    value = _number(_mapping(template.get("family_pair_correlation")).get(key))
    # The template contract has already made this impossible.  Keep this
    # defensive guard local so a corrupted artifact cannot create a hidden
    # default correlation.
    if value is None:
        raise ValueError("joint_weak_baseline_template_family_correlation_missing")
    return float(value)


def _baseline_exact_surface(
    *, artifact: Mapping[str, Any], source_set: Mapping[str, Any], context_key: str
) -> dict[str, Any]:
    value = _mapping(artifact)
    template = _mapping(value.get("exact_surface_baseline_template"))
    catalog = _mapping(value.get("source_catalog"))
    available = _ids(source_set.get("available_source_ids"))
    missing = [source for source in SOURCE_UNIVERSE if source not in available]
    if not available:
        return {
            "schema": SURFACE_SCHEMA_VERSION,
            "available_source_ids": [],
            "missing_source_ids": missing,
            "source_base_weights": {},
            "source_error_calibration": {},
            "source_correlation": {},
            "family_information_caps": _mapping(template.get("family_information_caps")),
            "joint_information_scale": 0.0,
            "information_confidence": 0.0,
            "surface_origin": "approved_precalibration_baseline_exact_empty_source_set",
            "surface_identity": f"{value.get('artifact_sha256') or ''}|{context_key}|empty",
            "training_event_day_count": 0,
        }
    weights = _mapping(template.get("source_base_weights"))
    errors = _mapping(template.get("source_error_sigma_c"))
    source_families = {
        source: str(_mapping(catalog.get(source)).get("provider_family") or "")
        for source in available
    }
    source_correlation = {
        left: {
            right: 1.0 if left == right else _family_pair_correlation(template, source_families[left], source_families[right])
            for right in available
        }
        for left in available
    }
    family_count = len(set(source_families.values()))
    source_count_factor = min(1.0, len(available) / 3.0)
    family_diversity_factor = min(1.0, 0.60 + 0.20 * family_count)
    base_scale = float(_number(template.get("base_joint_information_scale")) or 0.0)
    max_scale = float(_number(template.get("maximum_joint_information_scale")) or 0.0)
    scale = min(max_scale, base_scale * source_count_factor * family_diversity_factor)
    base_confidence = float(_number(template.get("base_information_confidence")) or 0.0)
    confidence = min(base_confidence, base_confidence * source_count_factor * family_diversity_factor)
    return {
        "schema": SURFACE_SCHEMA_VERSION,
        "available_source_ids": available,
        "missing_source_ids": missing,
        "source_base_weights": {source: float(weights[source]) for source in available},
        "source_error_calibration": {
            source: {
                "bias_c": 0.0,
                "error_sigma_c": float(errors[source]),
                "residual_distribution": {
                    "schema": "conditional_source_residual_distribution_v1",
                    "family": str(
                        template.get("source_residual_distribution_family")
                        or "gaussian_scale_fallback"
                    ),
                    "location_c": 0.0,
                    "scale_c": float(errors[source]),
                    "learned_from_closed_physical_x": False,
                    "state": "approved_precalibration_fallback",
                },
                "sample_count": 0,
                "bias_mode": "explicit_zero_precalibration_baseline",
            }
            for source in available
        },
        "family_information_caps": _mapping(template.get("family_information_caps")),
        "source_correlation": source_correlation,
        "joint_information_scale": scale,
        "information_confidence": confidence,
        "surface_origin": "approved_precalibration_baseline_exact_generated",
        "surface_identity": (
            f"{value.get('artifact_sha256') or ''}|{context_key}|{source_set_key(available)}"
        ),
        "training_event_day_count": 0,
        "missingness_effect": "lower_information_scale_and_confidence_not_source_imputation",
        "mode_formation_policy": str(
            template.get("mode_formation_policy")
            or "emergent_from_bias_corrected_distance_residual_overlap_and_source_correlation"
        ),
        "force_multimodality": False,
        "force_unimodality": False,
    }


def build_weak_likelihood_source_set(
    *,
    official_nwp_generation: Mapping[str, Any] | None,
    independent_source_availability: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge official NWP and TAF/WU/TWC availability without inventing data."""

    nwp = _mapping(official_nwp_generation)
    independent = _mapping(independent_source_availability)
    issues: list[str] = []
    nwp_available: list[str] = []
    nwp_missing: list[str] = ["ecmwf", "icon", "gfs", "jma"]
    if nwp:
        if nwp.get("schema") != "official_nwp_multimodel_live_bundle_v1" or nwp.get("state") != "available":
            issues.append("official_nwp_generation_unavailable_or_invalid")
        else:
            nwp_available = _ids(nwp.get("available_provider_ids"))
            nwp_missing = _ids(nwp.get("missing_provider_ids"))
            expected_nwp_missing = [source for source in ("ecmwf", "icon", "gfs", "jma") if source not in nwp_available]
            if nwp_missing != expected_nwp_missing:
                issues.append("official_nwp_generation_missingness_mask_invalid")
    independent_available = _ids(independent.get("available_source_ids"))
    independent_missing = _ids(independent.get("missing_source_ids"))
    independent_available = [source for source in independent_available if source in {"taf", "wu", "twc"}]
    independent_missing = [source for source in independent_missing if source in {"taf", "wu", "twc"}]
    overlap = set(independent_available) & set(independent_missing)
    if overlap:
        issues.append("independent_weak_source_missingness_overlap")
    available = _ids([*nwp_available, *independent_available])
    missing = [source for source in SOURCE_UNIVERSE if source not in available]
    # The independent caller may explicitly call a source missing, but its
    # absence is already represented in the full-universe mask; no default
    # confidence or backfill is applied here.
    return {
        "schema": SCHEMA_VERSION,
        "ok": not issues,
        "state": "available" if not issues else "blocked",
        "issues": sorted(set(issues)),
        "source_universe": list(SOURCE_UNIVERSE),
        "available_source_ids": available,
        "missing_source_ids": missing,
        "source_set_key": source_set_key(available),
        "nwp_available_source_ids": nwp_available,
        "nwp_missing_source_ids": nwp_missing,
        "independent_available_source_ids": independent_available,
        "independent_declared_missing_source_ids": independent_missing,
        "manual_confidence_or_weight": None,
        "boundary": {
            "missing_source_is_explicit_model_input": True,
            "uses_stale_or_substitute_source": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def select_source_set_surface(
    *, artifact: Mapping[str, Any] | None, source_set: Mapping[str, Any] | None, context_key: str
) -> dict[str, Any]:
    """Select only an exact approved combination surface; never fall back."""

    value = _mapping(artifact)
    state = _mapping(source_set)
    key = str(state.get("source_set_key") or "")
    surfaces = _mapping(_mapping(value.get("source_set_surfaces")).get(str(context_key or "")))
    surface = _mapping(surfaces.get(key))
    baseline_template = _mapping(value.get("exact_surface_baseline_template"))
    baseline_generated = False
    issues: list[str] = []
    if state.get("ok") is not True:
        issues.append("weak_source_set_invalid")
    if not key:
        issues.append("weak_source_set_key_missing")
    if surface.get("schema") != SURFACE_SCHEMA_VERSION:
        template_issues = _baseline_template_contract(baseline_template, _mapping(value.get("source_catalog")))
        if not template_issues:
            surface = _baseline_exact_surface(
                artifact=value,
                source_set=state,
                context_key=str(context_key or ""),
            )
            baseline_generated = True
        else:
            issues.extend(template_issues or ["weak_source_set_surface_missing_or_invalid"])
    if surface.get("schema") != SURFACE_SCHEMA_VERSION:
        issues.append("weak_source_set_surface_missing_or_invalid")
    if list(surface.get("available_source_ids") or []) != list(state.get("available_source_ids") or []):
        issues.append("weak_source_set_surface_available_source_mismatch")
    if list(surface.get("missing_source_ids") or []) != list(state.get("missing_source_ids") or []):
        issues.append("weak_source_set_surface_missing_source_mismatch")
    for field in ("information_confidence", "joint_information_scale", "source_error_calibration", "source_correlation"):
        if field not in surface:
            issues.append(f"weak_source_set_surface_{field}_missing")
    confidence = surface.get("information_confidence")
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = -1.0
    if confidence_value < 0.0 or confidence_value > 1.0:
        issues.append("weak_source_set_surface_information_confidence_invalid")
    return {
        "schema": "weak_likelihood_source_set_surface_selection_v1",
        "ok": not issues,
        "state": "available" if not issues else "blocked",
        "issues": sorted(set(issues)),
        "context_key": str(context_key or ""),
        "source_set_key": key,
        "surface": surface if not issues else {},
        "surface_origin": str(surface.get("surface_origin") or "explicit_artifact_surface") if not issues else "",
        "baseline_exact_surface_generated": baseline_generated if not issues else False,
        "confidence_cap": confidence_value if not issues else None,
        "boundary": {
            "exact_combination_only": True,
            "no_generic_fallback_surface": True,
            "baseline_template_is_exact_generated_surface_not_nearest_fallback": baseline_generated,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "SOURCE_UNIVERSE",
    "SURFACE_SCHEMA_VERSION",
    "BASELINE_TEMPLATE_SCHEMA",
    "baseline_template_issues",
    "build_weak_likelihood_source_set",
    "select_source_set_surface",
    "source_set_key",
]
