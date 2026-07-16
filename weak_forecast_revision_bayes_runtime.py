"""Bayes-coherent replacement update for a newly visible weak forecast revision."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "weak_forecast_revision_log_lr_v1"


def apply_weak_revision_ratio(
    current_joint_posterior: Mapping[str, float],
    *,
    previous_weak_log_likelihood_by_x: Mapping[str, float],
    revised_weak_log_likelihood_by_x: Mapping[str, float],
) -> dict[str, Any]:
    """Replace old weak evidence while retaining all intervening strong evidence.

    Joint keys use ``<x>|<latent_state>``.  The revision delta depends only on
    physical maximum X and therefore leaves the conditional latent-state mix
    within each X unchanged before normalization.
    """

    previous = {str(key): float(value) for key, value in previous_weak_log_likelihood_by_x.items()}
    revised = {str(key): float(value) for key, value in revised_weak_log_likelihood_by_x.items()}
    if not previous or set(previous) != set(revised):
        raise ValueError("weak_revision_x_support_mismatch")
    rows = {str(key): float(value) for key, value in current_joint_posterior.items()}
    if not rows or any(value < 0 or not math.isfinite(value) for value in rows.values()):
        raise ValueError("current_joint_posterior_invalid")
    total = sum(rows.values())
    if total <= 0:
        raise ValueError("current_joint_posterior_zero_mass")
    log_weights: dict[str, float] = {}
    delta_by_x = {x: revised[x] - previous[x] for x in previous}
    for key, probability in rows.items():
        x = key.split("|", 1)[0]
        if x not in delta_by_x:
            raise ValueError("joint_posterior_x_outside_weak_support")
        log_weights[key] = (-math.inf if probability == 0 else math.log(probability / total)) + delta_by_x[x]
    finite = [value for value in log_weights.values() if math.isfinite(value)]
    if not finite:
        raise ValueError("weak_revision_eliminated_all_mass")
    anchor = max(finite)
    weights = {key: (0.0 if not math.isfinite(value) else math.exp(value - anchor)) for key, value in log_weights.items()}
    normalizer = sum(weights.values())
    posterior = {key: value / normalizer for key, value in weights.items()}
    return {
        "schema": SCHEMA_VERSION,
        "posterior_joint": posterior,
        "weak_revision_log_lr_by_x": delta_by_x,
        "semantics": "replace_previous_weak_revision_not_multiply_as_independent",
        "retains_prior_strong_updates": True,
    }


def apply_weak_revision_ratio_to_joint_rows(
    current_joint_posterior: Iterable[Mapping[str, Any]],
    *,
    previous_weak_likelihood_by_x: Mapping[str, float],
    revised_weak_likelihood_by_x: Mapping[str, float],
) -> dict[str, Any]:
    """Apply a replacement update to the production joint-row shape.

    Production checkpoints store rows with ``temperature_c``, ``state`` and
    ``probability``.  Likelihood inputs are positive (possibly unnormalised)
    values; only their ratio matters, so normalising either weak surface is
    deliberately unnecessary.
    """

    rows = [dict(row) for row in current_joint_posterior if isinstance(row, Mapping)]
    if not rows:
        raise ValueError("current_joint_posterior_rows_missing")
    previous: dict[str, float] = {}
    revised: dict[str, float] = {}
    for source, destination, label in (
        (previous_weak_likelihood_by_x, previous, "previous"),
        (revised_weak_likelihood_by_x, revised, "revised"),
    ):
        for key, value in source.items():
            numeric = float(value)
            if not math.isfinite(numeric) or numeric <= 0.0:
                raise ValueError(f"{label}_weak_likelihood_nonpositive_or_invalid")
            destination[str(key)] = numeric
    if not previous or set(previous) != set(revised):
        raise ValueError("weak_revision_x_support_mismatch")
    keyed: dict[str, float] = {}
    row_by_key: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        temperature = float(row.get("temperature_c"))
        state = str(row.get("state") or "").strip()
        probability = float(row.get("probability"))
        if not math.isfinite(temperature) or not state or not math.isfinite(probability) or probability < 0.0:
            raise ValueError("current_joint_posterior_row_invalid")
        key = f"{temperature:g}|{state}"
        if key in keyed:
            raise ValueError("current_joint_posterior_row_identity_duplicate")
        keyed[key] = probability
        row_by_key[key] = {**row, "_ordinal": index}
    log_previous = {key: math.log(value) for key, value in previous.items()}
    log_revised = {key: math.log(value) for key, value in revised.items()}
    applied = apply_weak_revision_ratio(
        keyed,
        previous_weak_log_likelihood_by_x=log_previous,
        revised_weak_log_likelihood_by_x=log_revised,
    )
    posterior_rows: list[dict[str, Any]] = []
    for key, probability in applied["posterior_joint"].items():
        source = row_by_key[key]
        posterior_rows.append(
            {
                key: value
                for key, value in {
                    **source,
                    "probability": probability,
                }.items()
                if key != "_ordinal"
            }
        )
    posterior_rows.sort(key=lambda row: row_by_key[f"{float(row['temperature_c']):g}|{row['state']}"]["_ordinal"])
    return {
        **applied,
        "posterior_joint": posterior_rows,
        "previous_weak_likelihood_by_x": previous,
        "revised_weak_likelihood_by_x": revised,
    }


__all__ = [
    "SCHEMA_VERSION",
    "apply_weak_revision_ratio",
    "apply_weak_revision_ratio_to_joint_rows",
]
