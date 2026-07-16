"""Fit a shadow-only initial physical latent-state prior from first events.

The ``no_prior_observation`` trajectory feature is an explicit missing-history
marker, not a latent state.  This module therefore learns P(Z0 | X) only from
first canonical-event replay labels that were independently created after the
physical day closed.  It never invents warming/cooling mass for unobserved
temperature cells.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Any, Mapping, Sequence

from physical_latent_state_contract import LATENT_STATES


SCHEMA_VERSION = "physical_latent_state_prior_v1"
TRAINING_SCOPE = "physical_x"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _nearest(value: float, grid: Sequence[float], tolerance: float) -> float | None:
    candidate = min(grid, key=lambda item: abs(item - value))
    return candidate if abs(candidate - value) <= tolerance else None


def _normalize_bins(value: Sequence[Mapping[str, Any]] | None, grid: Sequence[float], tolerance: float) -> tuple[list[dict[str, Any]], list[str]]:
    """Return explicit bins; legacy absence means one bin per grid cell."""

    if value is None:
        bins: list[dict[str, Any]] = []
        for index, temperature in enumerate(grid):
            lower = temperature - tolerance if index == 0 else (grid[index - 1] + temperature) / 2.0
            upper = temperature + tolerance if index == len(grid) - 1 else (temperature + grid[index + 1]) / 2.0
            bins.append({"label": f"{temperature:g}", "lower_inclusive": lower, "upper_exclusive": upper})
        return bins, []
    bins: list[dict[str, Any]] = []
    issues: list[str] = []
    for raw in value:
        row = raw if isinstance(raw, Mapping) else {}
        label = _text(row.get("label"))
        lower = _finite(row.get("lower_inclusive"))
        upper = _finite(row.get("upper_exclusive"))
        if not label or lower is None or upper is None or lower >= upper:
            issues.append("physical_latent_prior_temperature_bin_invalid")
            continue
        bins.append({"label": label, "lower_inclusive": lower, "upper_exclusive": upper})
    labels = [row["label"] for row in bins]
    if len(labels) != len(set(labels)):
        issues.append("physical_latent_prior_temperature_bin_label_duplicate")
    ordered = sorted(bins, key=lambda row: (row["lower_inclusive"], row["upper_exclusive"], row["label"]))
    if any(left["upper_exclusive"] > right["lower_inclusive"] for left, right in zip(ordered, ordered[1:])):
        issues.append("physical_latent_prior_temperature_bins_overlap")
    for temperature in grid:
        if sum(1 for row in bins if row["lower_inclusive"] <= temperature < row["upper_exclusive"]) != 1:
            issues.append("physical_latent_prior_temperature_grid_not_exactly_covered_by_bins")
            break
    return bins, sorted(set(issues))


def _bin_for(value: float, bins: Sequence[Mapping[str, Any]]) -> str:
    for row in bins:
        if float(row["lower_inclusive"]) <= value < float(row["upper_exclusive"]):
            return _text(row.get("label"))
    return ""


def build_physical_latent_state_prior(
    *,
    physical_training_samples: Sequence[Mapping[str, Any]],
    latent_replay_labels: Sequence[Mapping[str, Any]],
    temperature_grid_c: Sequence[Any],
    temperature_bins: Sequence[Mapping[str, Any]] | None = None,
    minimum_samples_per_temperature: int = 10,
    temperature_grid_tolerance_c: float = 0.26,
) -> dict[str, Any]:
    """Return P(Z0|X) only when every physical cell has observed coverage."""

    grid = sorted({_finite(value) for value in temperature_grid_c if _finite(value) is not None})
    tolerance = _finite(temperature_grid_tolerance_c)
    if len(grid) < 2 or tolerance is None or tolerance <= 0.0 or int(minimum_samples_per_temperature) < 1:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["physical_latent_prior_configuration_invalid"],
            "read_only": True,
            "shadow_only": True,
        }
    bins, bin_issues = _normalize_bins(temperature_bins, grid, tolerance)
    if bin_issues:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": bin_issues,
            "read_only": True,
            "shadow_only": True,
        }
    replay_by_id: dict[str, Mapping[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for raw in latent_replay_labels:
        row = raw if isinstance(raw, Mapping) else {}
        sample_id = _text(row.get("training_sample_id"))
        if not sample_id:
            continue
        if sample_id in replay_by_id:
            duplicate_ids.add(sample_id)
        replay_by_id[sample_id] = row
    counts: dict[str, dict[str, int]] = {str(row["label"]): defaultdict(int) for row in bins}
    used: list[dict[str, Any]] = []
    rejected: dict[str, int] = defaultdict(int)
    for raw in physical_training_samples:
        sample = raw if isinstance(raw, Mapping) else {}
        sample_id = _text(sample.get("training_sample_id"))
        labels = sample.get("labels") if isinstance(sample.get("labels"), Mapping) else {}
        sequence = sample.get("canonical_sequence_event_ids")
        if sample.get("physical_auxiliary_eligible") is not True or sample.get("quality_grade") not in {"A", "B"}:
            rejected["sample_not_physical_x_eligible"] += 1
            continue
        if not isinstance(sequence, list) or len(sequence) != 1:
            rejected["sample_not_first_canonical_event"] += 1
            continue
        target = _finite(labels.get("final_physical_high_c"))
        bin_label = _bin_for(target, bins) if target is not None else ""
        if not bin_label:
            rejected["sample_final_x_outside_temperature_bins"] += 1
            continue
        replay = replay_by_id.get(sample_id)
        if sample_id in duplicate_ids or replay is None:
            rejected["sample_replay_label_missing_or_ambiguous"] += 1
            continue
        if _text(replay.get("training_scope")) != TRAINING_SCOPE:
            rejected["sample_replay_training_scope_invalid"] += 1
            continue
        state = _text(replay.get("z_pre"))
        if state not in LATENT_STATES:
            rejected["sample_replay_initial_state_invalid"] += 1
            continue
        counts[bin_label][state] += 1
        used.append({"training_sample_id": sample_id, "temperature_bin": bin_label, "z0": state, "latent_replay_sha256": _text(replay.get("latent_replay_sha256"))})
    undercovered = {
        label: sum(counts[label].values())
        for label in sorted(counts)
        if sum(counts[label].values()) < int(minimum_samples_per_temperature)
    }
    if undercovered:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "insufficient_history",
            "block_reasons": ["physical_latent_prior_temperature_cell_coverage_insufficient"],
            "diagnostics": {
                "minimum_samples_per_temperature": int(minimum_samples_per_temperature),
                "undercovered_temperature_bins": undercovered,
                "used_first_event_count": len(used),
                "rejected_counts": dict(sorted(rejected.items())),
            },
            "read_only": True,
            "shadow_only": True,
            "boundary": {
                "no_prior_observation_is_not_latent_state": True,
                "uniform_or_handwritten_latent_prior_forbidden": True,
                "writes_posterior": False,
                "places_orders": False,
            },
        }
    by_bin = {
        label: {
            state: counts[label].get(state, 0) / sum(counts[label].values())
            for state in LATENT_STATES
            if counts[label].get(state, 0) > 0
        }
        for label in sorted(counts)
    }
    prior = {f"{temperature:g}": dict(by_bin[_bin_for(temperature, bins)]) for temperature in grid}
    core = {
        "schema": SCHEMA_VERSION,
        "training_scope": TRAINING_SCOPE,
        "temperature_grid_c": grid,
        "temperature_bins": bins,
        "minimum_samples_per_temperature": int(minimum_samples_per_temperature),
        "temperature_grid_tolerance_c": tolerance,
        "latent_state_prior_by_temperature": prior,
        "latent_state_prior_by_temperature_bin": by_bin,
        "used_first_event_rows": used,
    }
    artifact_sha = _sha(core)
    return {
        **core,
        "artifact_sha256": artifact_sha,
        "ok": True,
        "state": "available_shadow_only",
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "no_prior_observation_is_not_latent_state": True,
            "uniform_or_handwritten_latent_prior_forbidden": True,
            "temperature_bins_are_explicit_and_empirical": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def build_neutral_physical_latent_state_operational_baseline(
    *, temperature_grid_c: Sequence[Any]
) -> dict[str, Any]:
    """Return a governed zero-information Z0 baseline for active runtime.

    The uniform measure is not empirical evidence and never changes marginal
    P0(X). It only keeps the latent state space total until an approved replay
    artifact replaces it.
    """

    grid = sorted({_finite(value) for value in temperature_grid_c if _finite(value) is not None})
    if len(grid) < 2:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["physical_latent_operational_baseline_grid_invalid"],
            "read_only": True,
            "shadow_only": True,
        }
    probability = 1.0 / len(LATENT_STATES)
    state_measure = {state: probability for state in LATENT_STATES}
    prior = {f"{temperature:g}": dict(state_measure) for temperature in grid}
    core = {
        "schema": SCHEMA_VERSION,
        "training_scope": TRAINING_SCOPE,
        "approval_kind": "operational_precalibration_neutral_baseline",
        "approval_state": "approved",
        "temperature_grid_c": grid,
        "latent_state_prior_by_temperature": prior,
        "neutral_state_measure": state_measure,
        "replacement_policy": "only_approved_empirical_physical_latent_state_prior_may_replace",
    }
    artifact_sha = _sha(core)
    return {
        **core,
        "artifact_sha256": artifact_sha,
        "ok": True,
        "state": "available_shadow_only",
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "not_empirical_evidence": True,
            "does_not_change_marginal_p0_x": True,
            "strict_asof_oos_not_live_gate": True,
            "no_prior_observation_is_not_latent_state": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "TRAINING_SCOPE",
    "build_neutral_physical_latent_state_operational_baseline",
    "build_physical_latent_state_prior",
]
