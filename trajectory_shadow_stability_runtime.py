"""Read-only stability diagnostics for sequential daily-high shadow candidates."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "zuuu_daily_high_shadow_stability_audit_v1"
POLICY_VERSION = "zuuu_daily_high_shadow_stability_policy_v1"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probability_map(candidate: Mapping[str, Any]) -> dict[str, float]:
    row = _mapping(_mapping(candidate.get("metar_strong_likelihood_candidate")).get("likelihood_map"))
    values: dict[str, float] = {}
    for key, value in row.items():
        parsed = _number(value)
        if parsed is not None:
            values[str(key)] = max(0.0, parsed)
    total = sum(values.values())
    return {key: value / total for key, value in values.items()} if total > 0 else {}


def _top_bucket(probabilities: Mapping[str, float]) -> str:
    return max(probabilities, key=probabilities.get) if probabilities else ""


def _candidate_violations(candidate: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    snapshot = _mapping(candidate.get("canonical_observation_snapshot"))
    cutoff = str(snapshot.get("as_of") or candidate.get("as_of") or "")
    for point in snapshot.get("selected_points") or []:
        row = _mapping(point)
        valid = str(row.get("verified_metar_valid_time") or "")
        first_seen = str(row.get("first_seen_at") or "")
        if cutoff and valid and valid > cutoff:
            issues.append("future_observation_leak")
        if cutoff and first_seen and first_seen > cutoff:
            issues.append("late_arrival_observation_leak")
    remaining = _mapping(candidate.get("remaining_daily_high_distribution"))
    hard_floor = _mapping(remaining.get("hard_floor"))
    probabilities = _probability_map(candidate)
    for bucket, mask in hard_floor.items():
        if _number(mask) == 0.0 and probabilities.get(str(bucket), 0.0) > 1e-9:
            issues.append(f"hard_floor_violation:{bucket}")
    if candidate.get("candidate_only") is not True:
        issues.append("candidate_only_guard_missing")
    if candidate.get("production_applied") is not False:
        issues.append("production_applied_guard_invalid")
    return sorted(set(issues))


def build_shadow_stability_audit(candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Measure changes between as-of candidate outputs without judging skill."""

    rows = [dict(value) for value in candidates if isinstance(value, Mapping)]
    rows.sort(key=lambda value: str(value.get("as_of") or _mapping(value.get("canonical_observation_snapshot")).get("as_of") or ""))
    transitions: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    state_switch_count = 0
    top_bucket_switch_count = 0
    previous_probs: dict[str, float] | None = None
    previous_state = ""
    previous_top = ""
    for index, candidate in enumerate(rows):
        probabilities = _probability_map(candidate)
        snapshot = _mapping(candidate.get("canonical_observation_snapshot"))
        trajectory = _mapping(candidate.get("temperature_trajectory_posterior"))
        state = str(trajectory.get("state") or "")
        top = _top_bucket(probabilities)
        candidate_issues = _candidate_violations(candidate)
        for issue in candidate_issues:
            violations.append({"index": index, "as_of": candidate.get("as_of") or snapshot.get("as_of"), "issue": issue})
        if previous_probs is not None:
            labels = sorted(set(previous_probs) | set(probabilities))
            total_variation = 0.5 * sum(abs(probabilities.get(label, 0.0) - previous_probs.get(label, 0.0)) for label in labels)
            max_delta = max((abs(probabilities.get(label, 0.0) - previous_probs.get(label, 0.0)) for label in labels), default=0.0)
            state_switched = bool(state and previous_state and state != previous_state)
            top_switched = bool(top and previous_top and top != previous_top)
            state_switch_count += int(state_switched)
            top_bucket_switch_count += int(top_switched)
            transitions.append(
                {
                    "from_as_of": rows[index - 1].get("as_of") or _mapping(rows[index - 1].get("canonical_observation_snapshot")).get("as_of"),
                    "to_as_of": candidate.get("as_of") or snapshot.get("as_of"),
                    "total_variation_distance": total_variation,
                    "max_bucket_probability_shift": max_delta,
                    "from_trajectory_state": previous_state,
                    "to_trajectory_state": state,
                    "trajectory_state_switched": state_switched,
                    "from_top_bucket": previous_top,
                    "to_top_bucket": top,
                    "top_bucket_switched": top_switched,
                }
            )
        previous_probs = probabilities
        previous_state = state
        previous_top = top
    max_tv = max((float(row["total_variation_distance"]) for row in transitions), default=0.0)
    max_shift = max((float(row["max_bucket_probability_shift"]) for row in transitions), default=0.0)
    return {
        "schema": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "candidate_count": len(rows),
        "transition_count": len(transitions),
        "transitions": transitions,
        "max_total_variation_distance": max_tv,
        "max_bucket_probability_shift": max_shift,
        "top_bucket_switch_count": top_bucket_switch_count,
        "trajectory_state_switch_count": state_switch_count,
        "violations": violations,
        "ok": not violations,
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "changes_parameters": False,
        },
    }


__all__ = ["POLICY_VERSION", "SCHEMA_VERSION", "build_shadow_stability_audit"]
