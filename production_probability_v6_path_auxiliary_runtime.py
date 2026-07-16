"""V6 post-update auxiliary state owned by the unique probability composer.

The values produced here never update ``p(X)``.  They bind the already
committed strong update to the canonical post-event trajectory and expose the
forecast-supported peak clock needed by the second-YES execution window.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from peak_timing_state_runtime import (
    build_peak_timing_state,
    validate_peak_timing_state_contract,
)
from temperature_trajectory_feature_runtime import (
    build_temperature_trajectory_feature_state,
)


SCHEMA_VERSION = "production_probability_v6_path_auxiliary_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _parse(value: Any, default: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    try:
        return json.loads(_text(value))
    except (TypeError, ValueError):
        return default


def enrich_v6_candidate_with_post_event_auxiliary(
    *,
    candidate: Mapping[str, Any],
    canonical_events_through_current: Sequence[Mapping[str, Any]],
    forecast_peak_timing_prior: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Attach trajectory and peak timing without changing posterior identity."""

    value = dict(_mapping(candidate))
    update = dict(_mapping(value.get("strong_update_row")))
    checkpoint = dict(_mapping(value.get("checkpoint_row")))
    event_id = _text(update.get("canonical_observation_event_id"))
    update_id = _text(update.get("strong_observation_update_id"))
    if (
        value.get("ok") is not True
        or not event_id
        or not update_id
        or _text(checkpoint.get("last_canonical_event_id")) != event_id
        or _text(checkpoint.get("last_strong_update_id")) != update_id
    ):
        raise ValueError("v6_path_auxiliary_candidate_lineage_invalid")

    decision_as_of = _text(update.get("completed_at") or update.get("decision_as_of"))
    trajectory = build_temperature_trajectory_feature_state(
        canonical_events=canonical_events_through_current,
        decision_as_of=decision_as_of,
        temperature_quantization_c=1.0,
        temporal_role="post_event",
    )
    if trajectory.get("ok") is not True:
        raise ValueError("v6_path_auxiliary_post_event_trajectory_invalid")

    auxiliary = dict(_mapping(_parse(update.get("path_auxiliary_json"), {})))
    termination = dict(_mapping(auxiliary.get("termination_risk")))
    update_view = {
        "ok": True,
        "strong_observation_update_id": update_id,
        "canonical_observation_event_id": event_id,
        "forecast_evidence_snapshot_id": _text(
            update.get("forecast_evidence_snapshot_id")
        ),
        "termination_risk": termination,
    }
    current_event = next(
        (
            dict(_mapping(row))
            for row in reversed(tuple(canonical_events_through_current))
            if _text(_mapping(row).get("canonical_observation_event_id")) == event_id
        ),
        None,
    )
    if current_event is None:
        raise ValueError("v6_path_auxiliary_current_event_missing")
    timing = build_peak_timing_state(
        forecast_peak_timing_prior=forecast_peak_timing_prior,
        strong_observation_update=update_view,
        post_event_trajectory=trajectory,
        canonical_observation_event=current_event,
    )
    _surface, timing_issues = validate_peak_timing_state_contract(
        timing,
        expected_event_id=event_id,
        expected_update_id=update_id,
    )
    if timing_issues:
        raise ValueError(
            "v6_path_auxiliary_peak_timing_invalid:" + ",".join(timing_issues)
        )

    auxiliary.update(
        {
            "schema": SCHEMA_VERSION,
            "temperature_trajectory_feature_state": trajectory,
            "peak_timing_state": timing,
            "peak_timing_does_not_update_physical_x": True,
        }
    )
    update["path_auxiliary_json"] = _json(auxiliary)
    checkpoint["trajectory_feature_state_json"] = _json(
        {
            "duration_clocks": _parse(checkpoint.get("duration_clocks_json"), {}),
            "temperature_trajectory_feature_state": trajectory,
            "peak_timing_state": timing,
        }
    )
    value["strong_update_row"] = update
    value["checkpoint_row"] = checkpoint
    value["peak_timing_state"] = timing
    value["temperature_trajectory_feature_state"] = trajectory
    value["reported_physical_x_unchanged"] = True
    return value


def inject_v6_peak_timing_into_public_fact(
    *, public_fact_row: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Publish the composer-owned timing state from the same update bundle."""

    public = dict(_mapping(public_fact_row))
    fact = dict(_mapping(_parse(public.get("fact_json"), {})))
    ssot = dict(_mapping(fact.get("posterior_ssot_fact")))
    update = _mapping(_mapping(candidate).get("strong_update_row"))
    checkpoint = _mapping(_mapping(candidate).get("checkpoint_row"))
    auxiliary = _mapping(_parse(update.get("path_auxiliary_json"), {}))
    timing = dict(_mapping(auxiliary.get("peak_timing_state")))
    _surface, issues = validate_peak_timing_state_contract(
        timing,
        expected_event_id=_text(checkpoint.get("last_canonical_event_id")),
        expected_update_id=_text(checkpoint.get("last_strong_update_id")),
    )
    if issues:
        raise ValueError("v6_public_peak_timing_invalid:" + ",".join(issues))
    if (
        _text(public.get("posterior_run_id"))
        != _text(checkpoint.get("posterior_run_id"))
        or _text(ssot.get("posterior_checkpoint_id"))
        != _text(checkpoint.get("posterior_checkpoint_id"))
    ):
        raise ValueError("v6_public_peak_timing_checkpoint_binding_mismatch")
    ssot["peak_timing_state"] = timing
    ssot["temperature_trajectory_feature_state"] = dict(
        _mapping(auxiliary.get("temperature_trajectory_feature_state"))
    )
    fact["posterior_ssot_fact"] = ssot
    public["fact_json"] = _json(fact)
    return public


__all__ = [
    "SCHEMA_VERSION",
    "enrich_v6_candidate_with_post_event_auxiliary",
    "inject_v6_peak_timing_into_public_fact",
]
