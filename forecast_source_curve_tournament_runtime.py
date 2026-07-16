"""Research-only prequential AMSC curve reliability diagnostic.

This legacy experiment may describe which forecast curve happened to track D0
observations, but its output is forbidden from changing the sole joint weak
likelihood, an approved bias state, the posterior, sizing, or orders. D0 AMSC
evidence belongs to the strong likelihood and may only create an explanatory
forecast-observation interaction trace there. Forecasts published after an
observation's valid time are excluded.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "forecast_source_curve_tournament_state_v1"
EPS = 1e-12


def _float(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def _time(value: Any) -> datetime | None:
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


def _canonical_sha(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _huber(value: float, delta: float = 1.0) -> float:
    absolute = abs(float(value))
    boundary = max(EPS, float(delta))
    return 0.5 * absolute * absolute if absolute <= boundary else boundary * (absolute - 0.5 * boundary)


def _project_bounded_simplex(raw: Sequence[float], lower: Sequence[float], upper: Sequence[float]) -> list[float]:
    """Euclidean projection onto sum(x)=1 with elementwise bounds."""

    if not raw:
        return []
    low = [max(0.0, float(value)) for value in lower]
    high = [min(1.0, max(low[index], float(value))) for index, value in enumerate(upper)]
    if sum(low) > 1.0 + 1e-9 or sum(high) < 1.0 - 1e-9:
        raise ValueError("curve_share_change_bounds_infeasible")
    values = [float(value) for value in raw]
    left = min(value - high[index] for index, value in enumerate(values)) - 1.0
    right = max(value - low[index] for index, value in enumerate(values)) + 1.0
    for _ in range(100):
        midpoint = (left + right) / 2.0
        projected = [min(high[index], max(low[index], value - midpoint)) for index, value in enumerate(values)]
        if sum(projected) > 1.0:
            left = midpoint
        else:
            right = midpoint
    projected = [min(high[index], max(low[index], value - right)) for index, value in enumerate(values)]
    total = sum(projected)
    if abs(total - 1.0) > 1e-8:
        free = [index for index in range(len(values)) if low[index] + 1e-10 < projected[index] < high[index] - 1e-10]
        if free:
            correction = (1.0 - total) / len(free)
            for index in free:
                projected[index] += correction
    return projected


def _select_prequential_forecasts(
    observations: Sequence[Mapping[str, Any]],
    forecasts: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], Mapping[str, Any]], dict[str, int]]:
    selected: dict[tuple[str, str], Mapping[str, Any]] = {}
    exclusions: dict[str, int] = defaultdict(int)
    valid_observation_times = {str(row.get("valid_time") or "") for row in observations}
    for row in forecasts:
        source = str(row.get("source_id") or "")
        valid_time_text = str(row.get("valid_time") or "")
        valid_time = _time(valid_time_text)
        available_at = _time(row.get("as_of_eligible_at") or row.get("available_at") or row.get("issued_at"))
        temperature = _float(row.get("temperature_c"))
        if not source or valid_time is None or available_at is None or temperature is None:
            exclusions["forecast_contract_incomplete"] += 1
            continue
        if valid_time_text not in valid_observation_times:
            exclusions["forecast_without_matching_observation"] += 1
            continue
        if available_at > valid_time:
            exclusions["forecast_published_after_observation_valid_time"] += 1
            continue
        key = (source, valid_time_text)
        previous = selected.get(key)
        previous_available = _time(previous.get("as_of_eligible_at") or previous.get("available_at") or previous.get("issued_at")) if previous else None
        if previous is None or (previous_available is not None and available_at > previous_available):
            selected[key] = row
    return selected, dict(sorted(exclusions.items()))


def evaluate_curve_reliability(
    *,
    base_weights: Mapping[str, Any],
    provider_families: Mapping[str, str],
    observations: Sequence[Mapping[str, Any]],
    forecasts: Sequence[Mapping[str, Any]],
    previous_effective_weights: Mapping[str, Any] | None = None,
    consumed_state_ids: Sequence[str] = (),
    ewma_alpha: float = 0.35,
    level_scale_c: float = 1.5,
    slope_scale_c: float = 0.8,
    level_coefficient: float = 0.55,
    slope_coefficient: float = 0.30,
    new_high_coefficient: float = 0.15,
    eta: float = 0.7,
    minimum_member_share: float = 0.05,
    max_downshift_per_observation: float = 0.20,
    max_recovery_per_observation: float = 0.10,
    all_source_bad_reliability: float = 0.35,
) -> dict[str, Any]:
    cleaned_base = {str(source): max(0.0, float(value)) for source, value in base_weights.items() if _float(value) is not None}
    total_base = sum(cleaned_base.values())
    if total_base <= EPS:
        return {"schema": SCHEMA_VERSION, "state": "unavailable", "state_reason": "base_weights_empty"}
    cleaned_base = {source: value / total_base for source, value in cleaned_base.items()}
    observation_rows = sorted(
        (dict(row) for row in observations if _time(row.get("valid_time")) is not None and _float(row.get("temperature_c")) is not None),
        key=lambda row: _time(row.get("valid_time")),
    )
    selected, exclusions = _select_prequential_forecasts(observation_rows, forecasts)
    ewma: dict[str, dict[str, float]] = {}
    source_updates: dict[str, int] = defaultdict(int)
    missing_counts: dict[str, int] = defaultdict(int)
    prior_observation_temperature: float | None = None
    observed_high: float | None = None
    prior_predictions: dict[str, float] = {}
    predicted_highs: dict[str, float] = {}
    loss_trace: list[dict[str, Any]] = []
    alpha = min(1.0, max(0.0, float(ewma_alpha)))
    coefficients = (max(0.0, level_coefficient), max(0.0, slope_coefficient), max(0.0, new_high_coefficient))
    coefficient_total = sum(coefficients) or 1.0
    coefficients = tuple(value / coefficient_total for value in coefficients)
    for observation in observation_rows:
        valid_time = str(observation.get("valid_time") or "")
        observed = float(observation["temperature_c"])
        observed_new_high = observed_high is None or observed > observed_high
        for source in cleaned_base:
            forecast = selected.get((source, valid_time))
            if forecast is None:
                missing_counts[source] += 1
                continue
            predicted = float(forecast["temperature_c"])
            level_loss = _huber((observed - predicted) / max(EPS, float(level_scale_c)))
            if prior_observation_temperature is not None and source in prior_predictions:
                observed_slope = observed - prior_observation_temperature
                predicted_slope = predicted - prior_predictions[source]
                slope_loss = _huber((observed_slope - predicted_slope) / max(EPS, float(slope_scale_c)))
            else:
                slope_loss = 0.0
            explicit_new_high_probability = _float(forecast.get("predicted_new_high_probability"))
            if explicit_new_high_probability is None:
                predicted_new_high = source not in predicted_highs or predicted > predicted_highs[source]
                explicit_new_high_probability = 1.0 if predicted_new_high else 0.0
            probability = min(1.0, max(0.0, explicit_new_high_probability))
            new_high_loss = (probability - float(observed_new_high)) ** 2
            state = ewma.setdefault(source, {"level": 0.0, "slope": 0.0, "new_high": 0.0})
            for name, value in (("level", level_loss), ("slope", slope_loss), ("new_high", new_high_loss)):
                state[name] = alpha * value + (1.0 - alpha) * state[name]
            source_updates[source] += 1
            prior_predictions[source] = predicted
            predicted_highs[source] = max(predicted_highs.get(source, predicted), predicted)
            loss_trace.append(
                {
                    "source_id": source,
                    "observation_valid_time": valid_time,
                    "forecast_snapshot_id": str(forecast.get("forecast_snapshot_id") or forecast.get("response_sha256") or ""),
                    "level_loss": level_loss,
                    "slope_loss": slope_loss,
                    "new_high_loss": new_high_loss,
                }
            )
        prior_observation_temperature = observed
        observed_high = max(observed_high if observed_high is not None else observed, observed)

    curve_losses: dict[str, float | None] = {}
    reliabilities: dict[str, float | None] = {}
    for source in cleaned_base:
        state = ewma.get(source)
        if not state:
            curve_losses[source] = None
            reliabilities[source] = None
            continue
        loss = coefficients[0] * state["level"] + coefficients[1] * state["slope"] + coefficients[2] * state["new_high"]
        curve_losses[source] = loss
        reliabilities[source] = math.exp(-max(0.0, float(eta)) * loss)

    by_family: dict[str, list[str]] = defaultdict(list)
    for source in cleaned_base:
        by_family[str(provider_families.get(source) or source)].append(source)
    previous = {
        source: max(0.0, float((previous_effective_weights or {}).get(source, cleaned_base[source])))
        for source in cleaned_base
    }
    family_totals_before: dict[str, float] = {}
    family_reliabilities: dict[str, float] = {}
    family_has_reliability: dict[str, bool] = {}
    previous_family_totals: dict[str, float] = {}
    for family, members in sorted(by_family.items()):
        family_total = sum(cleaned_base[source] for source in members)
        family_totals_before[family] = family_total
        previous_family_totals[family] = sum(previous[source] for source in members)
        available_members = [source for source in members if reliabilities[source] is not None]
        family_has_reliability[family] = bool(available_members)
        if not available_members:
            family_reliabilities[family] = 1.0
        else:
            denominator = sum(cleaned_base[source] for source in available_members)
            family_reliabilities[family] = (
                sum(cleaned_base[source] * float(reliabilities[source]) for source in available_members) / denominator
                if denominator > EPS
                else 1.0
            )
    families = sorted(by_family)
    raw_family = [family_totals_before[family] * family_reliabilities[family] for family in families]
    raw_family_total = sum(raw_family)
    raw_family_shares = (
        [value / raw_family_total for value in raw_family]
        if raw_family_total > EPS
        else [family_totals_before[family] for family in families]
    )
    previous_family_total = sum(previous_family_totals.values())
    previous_family_shares = (
        [previous_family_totals[family] / previous_family_total for family in families]
        if previous_family_total > EPS
        else [family_totals_before[family] for family in families]
    )
    family_count = len(families)
    family_floor = min(max(0.0, float(minimum_member_share)), 1.0 / max(family_count, 1))
    family_lower = [max(family_floor, share - max(0.0, float(max_downshift_per_observation))) for share in previous_family_shares]
    family_upper = [min(1.0, share + max(0.0, float(max_recovery_per_observation))) for share in previous_family_shares]
    for index, family in enumerate(families):
        if not family_has_reliability[family]:
            family_lower[index] = previous_family_shares[index]
            family_upper[index] = previous_family_shares[index]
    if sum(family_lower) > 1.0:
        family_lower = [family_floor for _family in families]
    if sum(family_upper) < 1.0:
        family_upper = [1.0 for _family in families]
    projected_family_shares = _project_bounded_simplex(raw_family_shares, family_lower, family_upper)
    family_totals_after = dict(zip(families, projected_family_shares))

    output_weights: dict[str, float] = {}
    for family, members in sorted(by_family.items()):
        base_family_total = family_totals_before[family]
        target_family_total = family_totals_after[family]
        base_shares = [cleaned_base[source] / base_family_total for source in members]
        prior_member_total = sum(previous[source] for source in members)
        previous_shares = [previous[source] / prior_member_total for source in members] if prior_member_total > EPS else base_shares
        raw = [
            base_shares[index] * (float(reliabilities[source]) if reliabilities[source] is not None else 1.0)
            for index, source in enumerate(members)
        ]
        raw_total = sum(raw)
        raw_shares = [value / raw_total for value in raw] if raw_total > EPS else base_shares
        count = len(members)
        floor = min(max(0.0, float(minimum_member_share)), 1.0 / count)
        lower = [max(floor, share - max(0.0, float(max_downshift_per_observation))) for share in previous_shares]
        upper = [min(1.0, share + max(0.0, float(max_recovery_per_observation))) for share in previous_shares]
        for index, source in enumerate(members):
            if reliabilities[source] is None:
                lower[index] = previous_shares[index]
                upper[index] = previous_shares[index]
        if sum(lower) > 1.0:
            lower = [floor for _source in members]
        if sum(upper) < 1.0:
            upper = [1.0 for _source in members]
        shares = _project_bounded_simplex(raw_shares, lower, upper)
        for source, share in zip(members, shares):
            output_weights[source] = target_family_total * share

    available_reliabilities = [float(value) for value in reliabilities.values() if value is not None]
    all_sources_bad = bool(available_reliabilities) and max(available_reliabilities) < float(all_source_bad_reliability)
    uniform_shrinkage = min(1.0, max(0.0, 1.0 - max(available_reliabilities))) if all_sources_bad else 0.0
    state_basis = {
        "base_weights": cleaned_base,
        "observations": [{"valid_time": row.get("valid_time"), "temperature_c": row.get("temperature_c")} for row in observation_rows],
        "loss_trace": loss_trace,
        "output_weights": output_weights,
    }
    state_id = _canonical_sha(state_basis)
    already_consumed = state_id in {str(value) for value in consumed_state_ids}
    return {
        "schema": SCHEMA_VERSION,
        "state": "unavailable" if already_consumed else "available",
        "state_reason": "curve_state_already_consumed" if already_consumed else "",
        "curve_state_id": state_id,
        "base_weights": cleaned_base,
        "effective_weights": output_weights if not already_consumed else cleaned_base,
        "curve_losses": curve_losses,
        "reliability_multipliers": reliabilities,
        "source_update_counts": dict(sorted(source_updates.items())),
        "source_missing_counts": dict(sorted(missing_counts.items())),
        "prequential_excluded_counts": exclusions,
        "family_precision_before": family_totals_before,
        "family_precision_after": family_totals_after if not already_consumed else family_totals_before,
        "family_reliability_multipliers": family_reliabilities,
        "all_sources_bad": all_sources_bad,
        "weak_layer_uniform_shrinkage": uniform_shrinkage,
        "loss_trace": loss_trace,
        "contract": {
            "research_only": True,
            "production_weight_input": False,
            "writes_joint_weak_likelihood": False,
            "writes_bias_state": False,
            "writes_posterior_or_order": False,
            "forecast_source_promoted_to_fact": False,
            "amsc_curve_reliability_consumed_once": not already_consumed,
            "curve_reweighting_changes_total_weak_precision": False,
            "curve_reweighting_changes_family_shares": True,
            "provider_family_dedup_preserved": True,
            "curve_reweighting_creates_observation_likelihood": False,
            "metar_strong_likelihood_owner_unchanged": True,
            "hard_lock_created": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "evaluate_curve_reliability"]
