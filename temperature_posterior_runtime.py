"""Continuous temperature posterior engine.

The engine models settlement daily maximum temperature as one continuous
variable, then integrates that posterior into market buckets. It is intentionally
independent of the legacy additive evidence pool.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


EPS = 1e-12
VERSION = "continuous_temperature_posterior_v1"


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _clamp01(value: Any) -> Optional[float]:
    num = _to_float(value)
    if num is None:
        return None
    return max(0.0, min(1.0, float(num)))


def _normalize(values: Sequence[float]) -> List[float]:
    vals = [max(0.0, float(v)) for v in values]
    total = sum(vals)
    if total <= EPS:
        if not vals:
            return []
        return [1.0 / len(vals)] * len(vals)
    return [v / total for v in vals]


def _normalize_unit(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"°C", "C", "CELSIUS"}:
        return "C"
    if raw in {"°F", "F", "FAHRENHEIT"}:
        return "F"
    return raw


def _label_unit(label: str) -> str:
    raw = str(label or "")
    if "°F" in raw or re.search(r"\bF\b", raw, flags=re.I):
        return "F"
    if "°C" in raw or re.search(r"\bC\b", raw, flags=re.I):
        return "C"
    return ""


def temperature_bucket_bounds(label: str, rounding_rule: Optional[str] = None) -> Optional[Tuple[float, float]]:
    raw = str(label or "").strip().replace("–", "-")
    if not raw:
        return None
    num = r"(-?\d+(?:\.\d+)?)"
    unit = r"([A-Za-z°/%]+)?"
    unit_norm = _label_unit(raw)
    whole_degrees = str(rounding_rule or "").strip().lower() == "whole_degrees" and unit_norm in {"C", "F"}
    rounded_degrees = str(rounding_rule or "").strip().lower() == "rounded_degrees" and unit_norm in {"C", "F"}

    def eq_bounds(value: float) -> Tuple[float, float]:
        if whole_degrees or rounded_degrees:
            return float(value) - 0.5, float(value) + 0.5
        return float(value) - 0.5, float(value) + 0.5

    match = re.match(rf"^{num}\s*-\s*{num}\s*{unit}$", raw)
    if not match:
        match = re.match(rf"^{num}\s+to\s+{num}\s*{unit}$", raw, flags=re.I)
    if match:
        low = min(float(match.group(1)), float(match.group(2)))
        high = max(float(match.group(1)), float(match.group(2)))
        if rounded_degrees or whole_degrees:
            return low - 0.5, high + 0.5
        return low, high

    match = re.match(rf"^{num}\s*{unit}\s+or\s+below$", raw, flags=re.I)
    if match:
        k = float(match.group(1))
        return float("-inf"), k + 0.5 if (rounded_degrees or whole_degrees) else k

    match = re.match(rf"^{num}\s*{unit}\s+or\s+(higher|above|more)$", raw, flags=re.I)
    if match:
        k = float(match.group(1))
        return k - 0.5 if (rounded_degrees or whole_degrees) else k, float("inf")

    match = re.match(rf"^{num}\s*{unit}$", raw)
    if match:
        return eq_bounds(float(match.group(1)))
    return None


def _normal_cdf(x: float, mean: float, sigma: float) -> float:
    if math.isinf(x):
        return 0.0 if x < 0.0 else 1.0
    z = (float(x) - float(mean)) / max(EPS, float(sigma))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _normal_pdf(x: float, mean: float, sigma: float) -> float:
    s = max(EPS, float(sigma))
    z = (float(x) - float(mean)) / s
    return math.exp(-0.5 * z * z) / (s * math.sqrt(2.0 * math.pi))


def _bucket_center(bounds: Tuple[float, float], fallback_low: float, fallback_high: float) -> float:
    low, high = bounds
    if math.isinf(low) and math.isinf(high):
        return (fallback_low + fallback_high) / 2.0
    if math.isinf(low):
        return min(fallback_high, high - 1.0)
    if math.isinf(high):
        return max(fallback_low, low + 1.0)
    return (low + high) / 2.0


def _finite_bounds(bounds: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    finite = [v for pair in bounds for v in pair if not math.isinf(v)]
    if not finite:
        return 0.0, 1.0
    low = min(finite)
    high = max(finite)
    if abs(high - low) <= EPS:
        return low - 1.0, high + 1.0
    return low, high


def _infer_normal_from_bucket_probs(
    labels: Sequence[str],
    probs: Sequence[float],
    rounding_rule: Optional[str],
) -> Optional[Tuple[float, float]]:
    if len(labels) != len(probs) or not labels:
        return None
    bounds_raw = [temperature_bucket_bounds(label, rounding_rule) for label in labels]
    if any(b is None for b in bounds_raw):
        return None
    bounds = [b for b in bounds_raw if b is not None]
    fallback_low, fallback_high = _finite_bounds(bounds)
    centers = [_bucket_center(b, fallback_low, fallback_high) for b in bounds]
    normalized = _normalize([float(v) for v in probs])
    mean = sum(p * c for p, c in zip(normalized, centers))
    variance = sum(p * (c - mean) ** 2 for p, c in zip(normalized, centers))
    avg_width_terms = [
        min(2.0, max(0.25, high - low))
        for low, high in bounds
        if not math.isinf(low) and not math.isinf(high)
    ]
    avg_width = sum(avg_width_terms) / len(avg_width_terms) if avg_width_terms else 1.0
    sigma = math.sqrt(max(0.18 ** 2, variance + (avg_width / math.sqrt(12.0)) ** 2))
    return float(mean), float(sigma)


def _component_mean_sigma(
    component: Mapping[str, Any],
    labels: Sequence[str],
    rounding_rule: Optional[str],
) -> Optional[Tuple[float, float]]:
    mean = _to_float(
        component.get("mean_c")
        if component.get("mean_c") is not None
        else component.get("mu_c")
        if component.get("mu_c") is not None
        else component.get("peak_c")
        if component.get("peak_c") is not None
        else component.get("target_peak_c")
    )
    sigma = _to_float(component.get("sigma_c") if component.get("sigma_c") is not None else component.get("std_c"))
    if mean is not None and sigma is not None and sigma > 0:
        return float(mean), max(0.05, float(sigma))

    probs = component.get("bucket_probs")
    if probs is None:
        probs = component.get("probabilities")
    if isinstance(probs, Sequence) and not isinstance(probs, (str, bytes)):
        nums = [_to_float(v) for v in probs]
        if len(nums) == len(labels) and all(v is not None for v in nums):
            inferred = _infer_normal_from_bucket_probs(labels, [float(v or 0.0) for v in nums], rounding_rule)
            if inferred is not None:
                inf_mean, inf_sigma = inferred
                return (
                    float(mean) if mean is not None else inf_mean,
                    max(0.05, float(sigma) if sigma is not None and sigma > 0 else inf_sigma),
                )
    return None


def _component_precision(component: Mapping[str, Any], sigma: float) -> float:
    explicit = _to_float(component.get("precision"))
    if explicit is not None and explicit > 0:
        precision = explicit
    else:
        precision = 1.0 / max(0.05, float(sigma)) ** 2
    confidence = _clamp01(component.get("confidence"))
    if confidence is not None:
        precision *= max(0.0, float(confidence))
    scale = _to_float(component.get("scale"))
    if scale is not None and scale >= 0:
        precision *= float(scale)
    return max(0.0, float(precision))


def _prepare_components(
    labels: Sequence[str],
    rounding_rule: Optional[str],
    source_components: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    prepared: List[Dict[str, Any]] = []
    for idx, raw in enumerate(source_components or []):
        if not isinstance(raw, Mapping):
            continue
        if raw.get("enabled") is False:
            continue
        inferred = _component_mean_sigma(raw, labels, rounding_rule)
        if inferred is None:
            continue
        mean, sigma = inferred
        precision = _component_precision(raw, sigma)
        if precision <= EPS:
            continue
        source_id = str(raw.get("source_id") or raw.get("id") or raw.get("source") or f"source_{idx}")
        group = str(raw.get("group") or raw.get("source_group") or source_id)
        role = str(raw.get("role") or "likelihood")
        prepared.append(
            {
                "source_id": source_id,
                "group": group,
                "role": role,
                "mean_c": float(mean),
                "sigma_c": float(sigma),
                "precision": float(precision),
                "raw": dict(raw),
            }
        )
    return prepared


def _combine_groups(
    components: Sequence[Mapping[str, Any]],
    *,
    group_rho: float,
    precision_cap: float,
) -> Tuple[float, float, List[Dict[str, Any]]]:
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for component in components:
        groups.setdefault(str(component.get("group") or component.get("source_id")), []).append(component)

    group_rows: List[Dict[str, Any]] = []
    for group_id, members in groups.items():
        precisions = [max(0.0, float(member.get("precision") or 0.0)) for member in members]
        sum_precision = sum(precisions)
        if sum_precision <= EPS:
            continue
        raw_mean = sum(float(member.get("mean_c") or 0.0) * p for member, p in zip(members, precisions)) / sum_precision
        max_precision = max(precisions)
        eff_precision = max_precision + max(0.0, min(1.0, group_rho)) * (sum_precision - max_precision)
        eff_precision = min(max(0.0, float(precision_cap)), max(0.0, eff_precision))
        if eff_precision <= EPS:
            continue
        group_rows.append(
            {
                "group": group_id,
                "mean_c": float(raw_mean),
                "raw_precision": float(sum_precision),
                "effective_precision": float(eff_precision),
                "member_count": len(members),
                "members": [dict(member) for member in members],
            }
        )

    total_precision = sum(float(row["effective_precision"]) for row in group_rows)
    if total_precision <= EPS:
        raise ValueError("no_effective_precision")
    posterior_mean = (
        sum(float(row["mean_c"]) * float(row["effective_precision"]) for row in group_rows) / total_precision
    )
    posterior_sigma = math.sqrt(1.0 / total_precision)
    return float(posterior_mean), max(0.05, float(posterior_sigma)), group_rows


def _round_grid_value(value: float) -> float:
    return round(float(value) + 0.0, 3)


def _grid_range(
    bounds: Sequence[Tuple[float, float]],
    mean: float,
    sigma: float,
    observed_lower: Optional[float],
) -> Tuple[float, float]:
    finite_low, finite_high = _finite_bounds(bounds)
    low = min(finite_low - 2.0, mean - 5.0 * sigma)
    high = max(finite_high + 2.0, mean + 5.0 * sigma)
    if observed_lower is not None:
        low = min(low, float(observed_lower) - 1.0)
        high = max(high, float(observed_lower) + 3.0)
    if high <= low:
        high = low + 1.0
    return float(low), float(high)


def _truncated_cdf(x: float, mean: float, sigma: float, lower: Optional[float]) -> float:
    if lower is None:
        return _normal_cdf(x, mean, sigma)
    if x < lower:
        return 0.0
    base = _normal_cdf(float(lower), mean, sigma)
    denom = max(EPS, 1.0 - base)
    return max(0.0, min(1.0, (_normal_cdf(x, mean, sigma) - base) / denom))


def _truncated_pdf(x: float, mean: float, sigma: float, lower: Optional[float]) -> float:
    if lower is not None and x < lower:
        return 0.0
    if lower is None:
        return _normal_pdf(x, mean, sigma)
    denom = max(EPS, 1.0 - _normal_cdf(float(lower), mean, sigma))
    return _normal_pdf(x, mean, sigma) / denom


def _truncated_bucket_probability(
    bounds: Tuple[float, float],
    *,
    mean: float,
    sigma: float,
    lower: Optional[float],
) -> float:
    low, high = bounds
    if lower is not None:
        low = max(low, float(lower))
    if high <= low:
        return 0.0
    return max(0.0, _truncated_cdf(high, mean, sigma, lower) - _truncated_cdf(low, mean, sigma, lower))


def _active_likelihood_updates(updates: Optional[Iterable[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
    prepared: List[Dict[str, Any]] = []
    for idx, raw in enumerate(updates or []):
        if not isinstance(raw, Mapping):
            continue
        if raw.get("enabled") is False:
            continue
        kind = str(raw.get("kind") or raw.get("type") or "").strip()
        if not kind and (raw.get("probabilities") is not None or raw.get("bucket_probs") is not None):
            kind = "bucket_likelihood"
        if kind not in {"bucket_likelihood", "trajectory_reachability", "normal_likelihood"}:
            continue
        strength = _to_float(raw.get("strength") if raw.get("strength") is not None else raw.get("weight"))
        if strength is None:
            strength = 1.0
        strength = max(0.0, min(3.0, float(strength)))
        if strength <= EPS:
            continue
        precision = _to_float(raw.get("precision"))
        if precision is None:
            precision = strength
        source_id = str(raw.get("source_id") or raw.get("id") or raw.get("source") or f"likelihood_update_{idx}")
        prepared.append(
            {
                **dict(raw),
                "kind": kind,
                "strength": float(strength),
                "precision": max(0.0, float(precision)),
                "source_id": source_id,
                "group": str(raw.get("group") or raw.get("source_group") or source_id),
                "role": str(raw.get("role") or "likelihood_update"),
            }
        )
    return prepared


def _bucket_index_for_temp(temp_c: float, bounds: Sequence[Tuple[float, float]]) -> Optional[int]:
    for idx, (low, high) in enumerate(bounds):
        if float(temp_c) >= low and float(temp_c) < high:
            return idx
        if idx == len(bounds) - 1 and not math.isinf(high) and abs(float(temp_c) - high) <= 1e-9:
            return idx
    return None


def _bucket_likelihood_factor(
    update: Mapping[str, Any],
    temp_c: float,
    labels: Sequence[str],
    bounds: Sequence[Tuple[float, float]],
    rounding_rule: Optional[str],
) -> float:
    raw_probs = update.get("probabilities")
    if raw_probs is None:
        raw_probs = update.get("bucket_probs")
    if not isinstance(raw_probs, Sequence) or isinstance(raw_probs, (str, bytes)):
        return 1.0
    probs = [_to_float(value) for value in raw_probs]
    if any(value is None for value in probs):
        return 1.0
    update_labels_raw = update.get("labels") or update.get("outcomes")
    update_labels = (
        [str(label or "") for label in update_labels_raw]
        if isinstance(update_labels_raw, Sequence) and not isinstance(update_labels_raw, (str, bytes))
        else list(labels)
    )
    if len(update_labels) != len(probs):
        if len(labels) != len(probs):
            return 1.0
        update_labels = list(labels)
        update_bounds = list(bounds)
    elif list(update_labels) == list(labels):
        update_bounds = list(bounds)
    else:
        bounds_raw = [temperature_bucket_bounds(label, rounding_rule) for label in update_labels]
        if any(bound is None for bound in bounds_raw):
            return 1.0
        update_bounds = [bound for bound in bounds_raw if bound is not None]
    idx = _bucket_index_for_temp(float(temp_c), update_bounds)
    if idx is None or idx >= len(probs):
        return 1.0
    normalized = _normalize([float(value or 0.0) for value in probs])
    return max(EPS, float(normalized[idx]))


def _trajectory_reachability_factor(update: Mapping[str, Any], temp_c: float) -> float:
    observed = _to_float(update.get("observed_temp_c"))
    observed_max = _to_float(update.get("observed_today_max_c"))
    anchors = [value for value in (observed, observed_max) if value is not None]
    if not anchors:
        return 1.0
    anchor = max(float(value) for value in anchors)
    rate = _to_float(update.get("rate_c_per_hour"))
    if rate is None:
        rate = 0.0
    expected_rate = max(0.0, float(rate))
    hours = _to_float(update.get("hours_to_peak_window"))
    if hours is None:
        hours = _to_float(update.get("hours_to_peak"))
    if hours is None:
        hours = 1.0
    min_hours = max(0.05, _to_float(update.get("min_projection_hours")) or 0.25)
    max_hours = max(min_hours, _to_float(update.get("max_projection_hours")) or 3.5)
    effective_hours = max(min_hours, min(max_hours, float(hours)))
    required_rate = max(0.0, float(temp_c) - anchor) / effective_hours
    rate_sigma = max(0.05, _to_float(update.get("rate_sigma_c_per_hour")) or 0.35)
    excess_rate = max(0.0, float(required_rate) - expected_rate)
    return max(EPS, math.exp(-0.5 * (excess_rate / rate_sigma) ** 2))


def _normal_likelihood_factor(update: Mapping[str, Any], temp_c: float) -> float:
    mean = _to_float(update.get("mean_c") if update.get("mean_c") is not None else update.get("mu_c"))
    sigma = _to_float(update.get("sigma_c") if update.get("sigma_c") is not None else update.get("std_c"))
    if mean is None or sigma is None or sigma <= 0:
        return 1.0
    return max(EPS, _normal_pdf(float(temp_c), float(mean), max(0.05, float(sigma))))


def _likelihood_factor(
    update: Mapping[str, Any],
    temp_c: float,
    labels: Sequence[str],
    bounds: Sequence[Tuple[float, float]],
    rounding_rule: Optional[str],
) -> float:
    kind = str(update.get("kind") or "").strip()
    if kind == "trajectory_reachability":
        base = _trajectory_reachability_factor(update, temp_c)
    elif kind == "normal_likelihood":
        base = _normal_likelihood_factor(update, temp_c)
    else:
        base = _bucket_likelihood_factor(update, temp_c, labels, bounds, rounding_rule)
    strength = max(0.0, min(3.0, _to_float(update.get("strength")) or 1.0))
    return max(EPS, float(base)) ** strength


def _likelihood_update_summaries(updates: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    for update in updates:
        summaries.append(
            {
                "source_id": str(update.get("source_id") or ""),
                "group": str(update.get("group") or ""),
                "role": str(update.get("role") or ""),
                "kind": str(update.get("kind") or ""),
                "strength": float(_to_float(update.get("strength")) or 0.0),
                "effective_precision": float(_to_float(update.get("precision")) or 0.0),
                "source": update.get("source"),
            }
        )
    return summaries


def _grid_quantile_from_masses(q: float, grid: Sequence[Mapping[str, float]], masses: Sequence[float]) -> float:
    target = max(0.0, min(1.0, float(q)))
    cumulative = 0.0
    last_temp = float(grid[-1]["temp_c"]) if grid else 0.0
    for row, mass in zip(grid, masses):
        cumulative += float(mass)
        last_temp = float(row["temp_c"])
        if cumulative >= target:
            return last_temp
    return last_temp


def _quantile(p: float, mean: float, sigma: float, lower: Optional[float], low: float, high: float) -> float:
    target = max(0.0, min(1.0, float(p)))
    left = low
    right = high
    for _ in range(80):
        mid = (left + right) / 2.0
        if _truncated_cdf(mid, mean, sigma, lower) < target:
            left = mid
        else:
            right = mid
    return (left + right) / 2.0


def _find_lock_bucket_index(
    labels: Sequence[str],
    bounds: Sequence[Tuple[float, float]],
    *,
    hard_lock_bucket: Optional[str],
    hard_lock_temp_c: Optional[float],
) -> Optional[int]:
    if hard_lock_bucket:
        wanted = str(hard_lock_bucket).strip()
        for idx, label in enumerate(labels):
            if str(label).strip() == wanted:
                return idx
    if hard_lock_temp_c is not None:
        temp = float(hard_lock_temp_c)
        for idx, (low, high) in enumerate(bounds):
            if temp >= low and temp < high:
                return idx
    return None


def build_temperature_posterior_distribution(
    labels: Sequence[str],
    *,
    rounding_rule: Optional[str] = None,
    source_components: Optional[Iterable[Mapping[str, Any]]] = None,
    likelihood_updates: Optional[Iterable[Mapping[str, Any]]] = None,
    observed_today_max_c: Optional[float] = None,
    hard_lock_bucket: Optional[str] = None,
    hard_lock_temp_c: Optional[float] = None,
    event_key: Optional[Mapping[str, Any]] = None,
    as_of: Optional[str] = None,
    group_rho: float = 0.35,
    precision_cap: float = 8.0,
    grid_step_c: float = 0.1,
    cdf_step_c: float = 0.5,
) -> Dict[str, Any]:
    """Build a continuous posterior and bucket probabilities.

    Returns a serializable fact-layer object. The function fails closed only when
    there are no labels; otherwise it uses a conservative default component if
    no calibrated component is available.
    """

    labels_list = [str(label or "") for label in labels]
    if not labels_list:
        return {
            "version": VERSION,
            "target_variable": "settlement_daily_max_c",
            "status": "no_labels",
            "bucket_probabilities": [],
            "grid": [],
            "cdf_values": [],
            "quantiles_c": {},
            "quantile_grid": [],
            "source_components": {},
            "likelihood_updates": {"applied": False, "method": "no_labels", "updates": []},
            "calibration": {},
            "observation_constraints": {},
        }
    bounds_raw = [temperature_bucket_bounds(label, rounding_rule) for label in labels_list]
    if any(bound is None for bound in bounds_raw):
        return {
            "version": VERSION,
            "target_variable": "settlement_daily_max_c",
            "status": "unparseable_bucket_bounds",
            "labels": labels_list,
            "bucket_probabilities": [],
            "grid": [],
            "cdf_values": [],
            "quantiles_c": {},
            "quantile_grid": [],
            "source_components": {},
            "likelihood_updates": {"applied": False, "method": "unparseable_bucket_bounds", "updates": []},
            "calibration": {},
            "observation_constraints": {"rounding_rule": rounding_rule},
        }
    bounds = [bound for bound in bounds_raw if bound is not None]
    finite_low, finite_high = _finite_bounds(bounds)
    observed_lower = _to_float(observed_today_max_c)

    prepared = _prepare_components(labels_list, rounding_rule, source_components or [])
    if not prepared:
        conservative_mean = (finite_low + finite_high) / 2.0
        conservative_sigma = max(1.5, (finite_high - finite_low) / 2.0)
        prepared = [
            {
                "source_id": "conservative_default",
                "group": "conservative_default",
                "role": "conservative_prior",
                "mean_c": float(conservative_mean),
                "sigma_c": float(conservative_sigma),
                "precision": 1.0 / conservative_sigma**2,
                "raw": {"source_id": "conservative_default", "reason": "no_calibrated_components"},
            }
        ]
    mean, sigma, group_rows = _combine_groups(prepared, group_rho=group_rho, precision_cap=precision_cap)

    lock_idx = _find_lock_bucket_index(
        labels_list,
        bounds,
        hard_lock_bucket=hard_lock_bucket,
        hard_lock_temp_c=_to_float(hard_lock_temp_c),
    )
    hard_locked = lock_idx is not None
    lower = observed_lower
    grid_low, grid_high = _grid_range(bounds, mean, sigma, lower)
    active_updates = _active_likelihood_updates(likelihood_updates)
    likelihood_updates_meta: Dict[str, Any] = {
        "applied": False,
        "method": "none",
        "updates": _likelihood_update_summaries(active_updates),
    }

    if hard_locked:
        probs = [1.0 if idx == lock_idx else 0.0 for idx in range(len(labels_list))]
        locked_temp = _to_float(hard_lock_temp_c)
        if locked_temp is None:
            locked_temp = _bucket_center(bounds[lock_idx], finite_low, finite_high)
        mean = float(locked_temp)
        sigma = 0.0
        grid = [{"temp_c": _round_grid_value(mean), "pdf": 1.0}]
        cdf_values = [
            {"temp_c": _round_grid_value(mean - 0.001), "cdf": 0.0},
            {"temp_c": _round_grid_value(mean), "cdf": 1.0},
        ]
        quantile_points = [0.05, 0.10, 0.50, 0.90, 0.95]
        quantile_grid = [{"q": q, "temp_c": _round_grid_value(mean)} for q in quantile_points]
        if active_updates:
            likelihood_updates_meta["method"] = "overridden_by_hard_source_lock"
    else:
        grid_step = max(0.01, float(grid_step_c))
        grid_count = min(2000, max(2, int(math.ceil((grid_high - grid_low) / grid_step)) + 1))
        grid_temps = []
        for idx in range(grid_count):
            temp = grid_low + idx * grid_step
            if temp > grid_high + EPS:
                break
            grid_temps.append(temp)
        if active_updates:
            raw_values: List[float] = []
            for temp in grid_temps:
                base_pdf = _truncated_pdf(temp, mean, sigma, lower)
                factor = 1.0
                for update in active_updates:
                    factor *= _likelihood_factor(update, temp, labels_list, bounds, rounding_rule)
                raw_values.append(max(0.0, float(base_pdf) * float(factor)))
            area = sum(raw_values) * grid_step
            if area > EPS:
                masses = [float(value) * grid_step / area for value in raw_values]
                pdf_values = [float(value) / area for value in raw_values]
                grid = [
                    {"temp_c": _round_grid_value(temp), "pdf": float(pdf)}
                    for temp, pdf in zip(grid_temps, pdf_values)
                ]
                probs_raw = []
                for bound in bounds:
                    mass = sum(
                        float(mass)
                        for temp, mass in zip(grid_temps, masses)
                        if _bucket_index_for_temp(temp, [bound]) == 0
                    )
                    probs_raw.append(mass)
                probs = _normalize(probs_raw)
                mean = sum(float(temp) * float(mass) for temp, mass in zip(grid_temps, masses))
                variance = sum(float(mass) * (float(temp) - float(mean)) ** 2 for temp, mass in zip(grid_temps, masses))
                sigma = max(0.05, math.sqrt(max(0.0, variance)))
                cdf_step = max(0.05, float(cdf_step_c))
                cdf_count = min(500, max(2, int(math.ceil((grid_high - grid_low) / cdf_step)) + 1))
                cdf_values = []
                for idx in range(cdf_count):
                    temp = grid_low + idx * cdf_step
                    if temp > grid_high + EPS:
                        break
                    cdf = sum(float(mass) for raw_temp, mass in zip(grid_temps, masses) if raw_temp <= temp)
                    cdf_values.append({"temp_c": _round_grid_value(temp), "cdf": max(0.0, min(1.0, float(cdf)))})
                quantile_points = [0.05, 0.10, 0.50, 0.90, 0.95]
                quantile_grid = [
                    {"q": q, "temp_c": _round_grid_value(_grid_quantile_from_masses(q, grid, masses))}
                    for q in quantile_points
                ]
                likelihood_updates_meta["applied"] = True
                likelihood_updates_meta["method"] = "base_density_times_likelihood_updates_on_grid"
            else:
                active_updates = []
                likelihood_updates_meta["method"] = "updates_zeroed_density_fallback_to_base"
        if not active_updates:
            probs_raw = [
                _truncated_bucket_probability(bound, mean=mean, sigma=sigma, lower=lower)
                for bound in bounds
            ]
            probs = _normalize(probs_raw)
            grid = [
                {
                    "temp_c": _round_grid_value(temp),
                    "pdf": float(_truncated_pdf(temp, mean, sigma, lower)),
                }
                for temp in grid_temps
            ]
            cdf_step = max(0.05, float(cdf_step_c))
            cdf_count = min(500, max(2, int(math.ceil((grid_high - grid_low) / cdf_step)) + 1))
            cdf_values = []
            for idx in range(cdf_count):
                temp = grid_low + idx * cdf_step
                if temp > grid_high + EPS:
                    break
                cdf_values.append(
                    {
                        "temp_c": _round_grid_value(temp),
                        "cdf": float(_truncated_cdf(temp, mean, sigma, lower)),
                    }
                )
            quantile_points = [0.05, 0.10, 0.50, 0.90, 0.95]
            quantile_grid = [
                {
                    "q": q,
                    "temp_c": _round_grid_value(_quantile(q, mean, sigma, lower, grid_low, grid_high)),
                }
                for q in quantile_points
            ]

    quantiles_c = {
        "p05": quantile_grid[0]["temp_c"] if len(quantile_grid) > 0 else None,
        "p10": quantile_grid[1]["temp_c"] if len(quantile_grid) > 1 else None,
        "p50": quantile_grid[2]["temp_c"] if len(quantile_grid) > 2 else None,
        "p90": quantile_grid[3]["temp_c"] if len(quantile_grid) > 3 else None,
        "p95": quantile_grid[4]["temp_c"] if len(quantile_grid) > 4 else None,
    }
    bucket_probabilities = [
        {
            "outcome": label,
            "probability": float(prob),
            "lower_c": None if math.isinf(bounds[idx][0]) else float(bounds[idx][0]),
            "upper_c": None if math.isinf(bounds[idx][1]) else float(bounds[idx][1]),
        }
        for idx, (label, prob) in enumerate(zip(labels_list, probs))
    ]

    d0_violations = [
        row["outcome"]
        for row in bucket_probabilities
        if observed_lower is not None
        and row["probability"] > 1e-9
        and row["upper_c"] is not None
        and float(row["upper_c"]) <= float(observed_lower)
    ]
    return {
        "version": VERSION,
        "target_variable": "settlement_daily_max_c",
        "status": "hard_source_locked" if hard_locked else "ok",
        "event_key": dict(event_key or {}),
        "as_of": as_of,
        "mean_c": float(mean),
        "sigma_c": float(sigma),
        "grid": grid,
        "cdf_values": cdf_values,
        "quantiles_c": quantiles_c,
        "quantile_grid": quantile_grid,
        "bucket_probabilities": bucket_probabilities,
        "source_components": {
            "components": [dict(component) for component in prepared],
            "groups": group_rows,
            "group_rho": float(group_rho),
            "precision_cap": float(precision_cap),
        },
        "likelihood_updates": likelihood_updates_meta,
        "calibration": {
            "method": "precision_weighted_gaussian_with_group_shrinkage",
            "hierarchical_fallback": "component_supplied",
            "group_precision_rule": "max_precision_plus_rho_times_remaining_capped",
        },
        "observation_constraints": {
            "observed_today_max_c": observed_lower,
            "d0_lower_bound_applied": observed_lower is not None and not hard_locked,
            "d0_violation_count": len(d0_violations),
            "d0_violation_buckets": d0_violations,
            "hard_source_locked": bool(hard_locked),
            "hard_lock_bucket": labels_list[lock_idx] if hard_locked and lock_idx is not None else None,
            "hard_lock_temp_c": _to_float(hard_lock_temp_c),
        },
    }


def bucket_probabilities_by_label(posterior: Mapping[str, Any]) -> Dict[str, float]:
    rows = posterior.get("bucket_probabilities")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return {}
    out: Dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("outcome") or "")
        prob = _clamp01(row.get("probability"))
        if label and prob is not None:
            out[label] = float(prob)
    return out
