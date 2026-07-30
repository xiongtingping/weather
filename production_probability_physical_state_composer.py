"""Physical-state composer adapter inside the preserved v6 online skeleton.

This module prepares pure checkpoint extensions.  It does not write a database,
publish SSOT, create a decision or place an order.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo
from zuuu_metar_reporting_schedule import (
    SCHEDULE_ID as ZUUU_SCHEDULE_ID,
    expected_regular_metar_slots_utc,
    is_settlement_metar_slot_utc,
)

from identifiable_physical_state_bayes.adapters import (
    canonical_observation_from_event,
    drivers_from_weak_likelihood,
    initialize_physical_posterior,
    scenario_weights_from_weak_likelihood,
)
from identifiable_physical_state_bayes.contracts import (
    SCHEMA_VERSION as PHYSICAL_STATE_SCHEMA,
    ReportingSlot,
)
from identifiable_physical_state_bayes.core import (
    build_joint_observation_likelihood,
    canonical_sha256,
    condition_pre_event_state,
    observation_likelihood_sha256,
    pre_event_prediction_sha256,
    predict_to_event,
)
from identifiable_physical_state_bayes.reporting import project_future_paths
from identifiable_physical_state_bayes.revision import (
    replace_future_forcing_generation,
)
from identifiable_physical_state_bayes.serialization import (
    posterior_from_payload,
    posterior_payload,
    posterior_sha256,
)


SCHEMA_VERSION = "production_probability_identifiable_physical_composer_v1"
COMPOSER_SCHEMA = "identifiable_physical_state_bayes_v1_pre_event_predict_update"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("physical_composer_time_must_be_timezone_aware")
    return parsed


def normalize_physical_predecessor_checkpoint(
    previous_checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an event-safe predecessor without rewriting persisted history.

    Weak forecast revisions may have advanced the physical reference clock to
    their wall-clock ``decision_as_of`` while the last canonical observation
    remains earlier.  That reference is not an observation and must not make
    the next real observation look out of order.  For event composition, the
    predecessor therefore ends at the last consumed canonical event whenever
    the two timestamps are inconsistent.  The original checkpoint remains
    immutable; the successor emitted by the event compiler carries the
    canonical event time as its new reference.
    """

    previous = dict(previous_checkpoint)
    physical_reference = _text(previous.get("physical_state_reference_valid_at"))
    last_event = _text(previous.get("last_event_valid_at"))
    if not physical_reference or not last_event:
        return previous
    if _time(physical_reference) > _time(last_event):
        previous["physical_state_reference_valid_at"] = last_event
    return previous


def _parse(value: Any, default: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    try:
        return json.loads(_text(value))
    except (TypeError, ValueError):
        return default


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _target_day(target_identity: str) -> str:
    day = _text(target_identity).rsplit(":", 1)[-1]
    datetime.fromisoformat(day)
    return day


def build_remaining_reporting_slots(
    *,
    target_identity: str,
    state_reference_valid_at: str,
    reporting_operator_artifact: Mapping[str, Any],
    include_reference: bool,
) -> tuple[ReportingSlot, ...]:
    timezone_name = _text(reporting_operator_artifact.get("timezone"))
    schedule = reporting_operator_artifact.get("scheduled_local_times")
    policy = reporting_operator_artifact.get("slot_policy")
    schedule_id = _text(reporting_operator_artifact.get("reporting_schedule_id"))
    if not timezone_name or not isinstance(policy, Mapping):
        raise ValueError("physical_reporting_schedule_surface_missing")
    timezone = ZoneInfo(timezone_name)
    reference = datetime.fromisoformat(_text(state_reference_valid_at).replace("Z", "+00:00"))
    if reference.tzinfo is None:
        raise ValueError("physical_reporting_reference_timezone_missing")
    day = _target_day(target_identity)
    result = []
    if isinstance(schedule, Sequence):
        raw_slots = [
            (
                _text(local_time),
                datetime.fromisoformat(f"{day}T{_text(local_time)}")
                .replace(tzinfo=timezone)
                .astimezone(ZoneInfo("UTC")),
            )
            for local_time in schedule
        ]
    elif schedule_id == ZUUU_SCHEDULE_ID:
        raw_slots = [
            (
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                .astimezone(timezone)
                .time()
                .isoformat(),
                datetime.fromisoformat(value.replace("Z", "+00:00")),
            )
            for value in expected_regular_metar_slots_utc(day)
        ]
    else:
        raise ValueError("physical_reporting_schedule_identity_invalid")
    for local_time, valid in raw_slots:
        if valid < reference or (valid == reference and not include_reference):
            continue
        hour_policy = policy.get(_text(local_time))
        row = dict(hour_policy) if isinstance(hour_policy, Mapping) else dict(policy.get("default") or {})
        # The AMSC :30 report remains a reference/acquisition observation.  It
        # must stay in the future path so the model can use it, but it cannot
        # become a contract/settlement slot or raise the hard fact boundary.
        is_contract_slot = bool(row.get("is_contract_slot", True))
        if schedule_id == ZUUU_SCHEDULE_ID:
            is_contract_slot = is_settlement_metar_slot_utc(day, valid.isoformat().replace("+00:00", "Z"))
        result.append(
            ReportingSlot(
                valid_at=valid.isoformat().replace("+00:00", "Z"),
                is_contract_slot=is_contract_slot,
                encoding_step_c=float(row.get("encoding_step_c", 1.0)),
                report_probability=float(row.get("report_probability", 1.0)),
                quality_acceptance_probability=float(row.get("quality_acceptance_probability", 1.0)),
                missing_reason=_text(row.get("missing_reason")),
            )
        )
    return tuple(result)


def _future_drivers(
    *,
    weak_likelihood: Mapping[str, Any],
    scenario_ids: Sequence[str],
    slots: Sequence[ReportingSlot],
    state_model_artifact: Mapping[str, Any],
) -> dict[str, tuple[Any, ...]]:
    result = {scenario: [] for scenario in scenario_ids}
    for slot in slots:
        at_slot = drivers_from_weak_likelihood(
            weak_likelihood=weak_likelihood,
            valid_at=slot.valid_at,
            scenario_ids=scenario_ids,
            state_model_artifact=state_model_artifact,
        )
        for scenario in scenario_ids:
            result[scenario].append(at_slot[scenario])
    return {scenario: tuple(rows) for scenario, rows in result.items()}


def compile_physical_p0_extension(
    *,
    target_identity: str,
    state_reference_valid_at: str,
    prior_context: Mapping[str, Any],
    weak_likelihood: Mapping[str, Any],
    state_model_artifact: Mapping[str, Any],
    reporting_operator_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    posterior, initialization_lineage = initialize_physical_posterior(
        prior_context=prior_context,
        weak_likelihood=weak_likelihood,
        state_reference_valid_at=state_reference_valid_at,
        state_model_artifact=state_model_artifact,
    )
    slots = build_remaining_reporting_slots(
        target_identity=target_identity,
        state_reference_valid_at=state_reference_valid_at,
        reporting_operator_artifact=reporting_operator_artifact,
        include_reference=True,
    )
    scenarios = tuple(sorted({item.forcing_scenario_id for item in posterior.components}))
    projection = project_future_paths(
        posterior=posterior,
        state_reference_valid_at=state_reference_valid_at,
        reporting_slots=slots,
        drivers_by_scenario=_future_drivers(
            weak_likelihood=weak_likelihood,
            scenario_ids=scenarios,
            slots=slots,
            state_model_artifact=state_model_artifact,
        ),
        observed_contract_max_c=None,
        state_model_artifact=state_model_artifact,
        reporting_operator_artifact=reporting_operator_artifact,
    )
    payload = posterior_payload(posterior)
    projection_payload = asdict(projection)
    return {
        "schema": SCHEMA_VERSION,
        "physical_composer_schema": COMPOSER_SCHEMA,
        "physical_state_posterior_schema": PHYSICAL_STATE_SCHEMA,
        "physical_state_posterior_json": _json(payload),
        "physical_state_posterior_sha256": posterior_sha256(payload),
        "physical_path_projection_json": _json(projection_payload),
        "physical_path_projection_sha256": projection.path_projection_sha256,
        "physical_state_model_artifact_sha256": _text(state_model_artifact.get("artifact_sha256")),
        "physical_reporting_operator_sha256": projection.reporting_operator_sha256,
        "physical_driver_generation_sha256": initialization_lineage["driver_generation_sha256"],
        "reported_physical_x_distribution": dict(projection.reported_x_distribution),
        "initialization_lineage": initialization_lineage,
        "places_orders": False,
    }


def compose_physical_canonical_event_extension(
    *,
    previous_checkpoint: Mapping[str, Any],
    canonical_event: Mapping[str, Any],
    weak_likelihood: Mapping[str, Any],
    state_model_artifact: Mapping[str, Any],
    observation_artifact: Mapping[str, Any],
    reporting_operator_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    previous_checkpoint = normalize_physical_predecessor_checkpoint(previous_checkpoint)
    previous_payload = _parse(previous_checkpoint.get("physical_state_posterior_json"), {})
    if not isinstance(previous_payload, Mapping) or not previous_payload:
        raise ValueError("physical_composer_previous_state_missing")
    if posterior_sha256(previous_payload) != _text(previous_checkpoint.get("physical_state_posterior_sha256")):
        raise ValueError("physical_composer_previous_state_hash_mismatch")
    posterior = posterior_from_payload(previous_payload)
    observation = canonical_observation_from_event(canonical_event)
    scenario_ids = tuple(sorted({item.forcing_scenario_id for item in posterior.components}))
    drivers = drivers_from_weak_likelihood(
        weak_likelihood=weak_likelihood,
        valid_at=observation.event_valid_at,
        scenario_ids=scenario_ids,
        state_model_artifact=state_model_artifact,
    )
    prediction = predict_to_event(
        previous_posterior=posterior,
        previous_checkpoint_sha256=_text(previous_checkpoint.get("physical_state_posterior_sha256")),
        previous_valid_at=(
            _text(previous_checkpoint.get("physical_state_reference_valid_at"))
            or _text(previous_checkpoint.get("last_event_valid_at"))
        ),
        canonical_event_id=observation.canonical_event_id,
        event_valid_at=observation.event_valid_at,
        drivers=drivers,
        state_model_artifact=state_model_artifact,
    )
    likelihood = build_joint_observation_likelihood(
        prediction=prediction,
        observation=observation,
        observation_artifact=observation_artifact,
    )
    conditioned = condition_pre_event_state(
        prediction=prediction,
        likelihood=likelihood,
        observation=observation,
        previous_observed_contract_max_c=(
            int(previous_checkpoint["reported_x_lower_bound_c"])
            if previous_checkpoint.get("reported_x_lower_bound_c") is not None
            else None
        ),
    )
    slots = build_remaining_reporting_slots(
        target_identity=_text(previous_checkpoint.get("target_identity")),
        state_reference_valid_at=observation.event_valid_at,
        reporting_operator_artifact=reporting_operator_artifact,
        include_reference=False,
    )
    projection = project_future_paths(
        posterior=conditioned.posterior,
        state_reference_valid_at=observation.event_valid_at,
        reporting_slots=slots,
        drivers_by_scenario=_future_drivers(
            weak_likelihood=weak_likelihood,
            scenario_ids=scenario_ids,
            slots=slots,
            state_model_artifact=state_model_artifact,
        ),
        observed_contract_max_c=conditioned.observed_contract_max_c,
        state_model_artifact=state_model_artifact,
        reporting_operator_artifact=reporting_operator_artifact,
    )
    state_payload = posterior_payload(conditioned.posterior)
    prediction_payload = asdict(prediction)
    likelihood_payload = asdict(likelihood)
    conditioned_payload = asdict(conditioned)
    projection_payload = asdict(projection)
    return {
        "schema": SCHEMA_VERSION,
        "physical_composer_schema": COMPOSER_SCHEMA,
        "physical_pre_event_state_json": _json(prediction_payload),
        "physical_pre_event_state_sha256": pre_event_prediction_sha256(prediction),
        "physical_observation_likelihood_json": _json(likelihood_payload),
        "physical_observation_likelihood_sha256": observation_likelihood_sha256(likelihood),
        "physical_post_event_state_json": _json(conditioned_payload),
        "physical_post_event_state_sha256": canonical_sha256(conditioned_payload),
        "physical_current_event_consumption_count": likelihood.current_event_consumption_count,
        "physical_state_posterior_schema": PHYSICAL_STATE_SCHEMA,
        "physical_state_posterior_json": _json(state_payload),
        "physical_state_posterior_sha256": posterior_sha256(state_payload),
        "physical_path_projection_json": _json(projection_payload),
        "physical_path_projection_sha256": projection.path_projection_sha256,
        "physical_state_model_artifact_sha256": prediction.state_model_artifact_sha256,
        "physical_observation_artifact_sha256": likelihood.observation_artifact_sha256,
        "physical_reporting_operator_sha256": projection.reporting_operator_sha256,
        "physical_driver_generation_sha256": prediction.forcing_generation_sha256,
        "reported_physical_x_distribution": dict(projection.reported_x_distribution),
        "reported_x_lower_bound_c": conditioned.observed_contract_max_c,
        "last_event_valid_at": observation.event_valid_at,
        "invariant_report": dict(conditioned.invariant_report),
        "current_observation_consumed_once": True,
        "nwp_hourly_path_likelihood_reapplied": False,
        "places_orders": False,
    }


def compile_physical_weak_revision_extension(
    *,
    previous_checkpoint: Mapping[str, Any],
    previous_weak_likelihood: Mapping[str, Any],
    revised_weak_likelihood: Mapping[str, Any],
    decision_as_of: str,
    state_model_artifact: Mapping[str, Any],
    reporting_operator_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Advance with old drivers, then replace future forcing at ``as_of``.

    A forecast generation is a weak exogenous driver, never an observation
    likelihood.  The state is first advanced from its checkpoint reference to
    the revision's effective time using the superseded generation.  Only then
    is the driver-scenario dimension rebound and the remaining frozen reporting
    slots projected with the revised generation.
    """

    previous_payload = _parse(previous_checkpoint.get("physical_state_posterior_json"), {})
    if not isinstance(previous_payload, Mapping) or not previous_payload:
        raise ValueError("physical_weak_revision_previous_state_missing")
    previous_state_sha = _text(previous_checkpoint.get("physical_state_posterior_sha256"))
    if posterior_sha256(previous_payload) != previous_state_sha:
        raise ValueError("physical_weak_revision_previous_state_hash_mismatch")
    posterior = posterior_from_payload(previous_payload)
    old_generation = _text(
        _parse(previous_weak_likelihood.get("forecast_scenario_paths"), {}).get("sha256")
    )
    if (
        len(old_generation) != 64
        or old_generation
        != (
            _text(previous_checkpoint.get("physical_driver_generation_sha256"))
            or _text(previous_checkpoint.get("forcing_generation_sha256"))
        )
    ):
        raise ValueError("physical_weak_revision_previous_driver_generation_mismatch")
    reference = (
        _text(previous_checkpoint.get("physical_state_reference_valid_at"))
        or _text(previous_checkpoint.get("last_event_valid_at"))
    )
    if not reference or not _text(decision_as_of):
        raise ValueError("physical_weak_revision_reference_time_missing")
    # A D+1 checkpoint is anchored at the target-day start, which can be later
    # than the wall-clock time at which a new forecast revision is received.
    # A forcing replacement must never run the state transition backwards.  In
    # that case the revised forcing becomes effective at the existing target
    # reference instant and the advance is deliberately a zero-duration
    # no-op; current-day revisions still use the actual decision time.
    reference_time = _time(reference)
    decision_time = _time(_text(decision_as_of))
    effective_from = (
        reference
        if decision_time < reference_time
        else _text(decision_as_of)
    )
    old_scenarios = tuple(sorted({item.forcing_scenario_id for item in posterior.components}))
    old_drivers = drivers_from_weak_likelihood(
        weak_likelihood=previous_weak_likelihood,
        valid_at=effective_from,
        scenario_ids=old_scenarios,
        state_model_artifact=state_model_artifact,
    )
    advance_identity = "weak-forcing-effective-" + canonical_sha256(
        {
            "previous_physical_state_posterior_sha256": previous_state_sha,
            "previous_forcing_generation_sha256": old_generation,
            "effective_from": effective_from,
        }
    )
    advance = predict_to_event(
        previous_posterior=posterior,
        previous_checkpoint_sha256=previous_state_sha,
        previous_valid_at=reference,
        canonical_event_id=advance_identity,
        event_valid_at=effective_from,
        drivers=old_drivers,
        state_model_artifact=state_model_artifact,
    )
    weights, weight_method, new_generation = scenario_weights_from_weak_likelihood(
        weak_likelihood=revised_weak_likelihood
    )
    rebound, rebind_audit = replace_future_forcing_generation(
        posterior_at_effective_time=advance.posterior,
        revised_scenario_weights=weights,
        revised_forcing_generation_sha256=new_generation,
        effective_from=effective_from,
    )
    slots = build_remaining_reporting_slots(
        target_identity=_text(previous_checkpoint.get("target_identity")),
        state_reference_valid_at=effective_from,
        reporting_operator_artifact=reporting_operator_artifact,
        include_reference=False,
    )
    scenarios = tuple(sorted({item.forcing_scenario_id for item in rebound.components}))
    projection = project_future_paths(
        posterior=rebound,
        state_reference_valid_at=effective_from,
        reporting_slots=slots,
        drivers_by_scenario=_future_drivers(
            weak_likelihood=revised_weak_likelihood,
            scenario_ids=scenarios,
            slots=slots,
            state_model_artifact=state_model_artifact,
        ),
        observed_contract_max_c=(
            int(previous_checkpoint["reported_x_lower_bound_c"])
            if previous_checkpoint.get("reported_x_lower_bound_c") is not None
            else None
        ),
        state_model_artifact=state_model_artifact,
        reporting_operator_artifact=reporting_operator_artifact,
    )
    if not projection.reported_x_distribution:
        raise ValueError("physical_weak_revision_reported_x_projection_empty")
    state_payload = posterior_payload(rebound)
    advance_payload = asdict(advance)
    projection_payload = asdict(projection)
    control_lineage = {
        "scenario_weight_method": weight_method,
        "scenario_weights": weights,
        "revised_forcing_generation_sha256": new_generation,
    }
    return {
        "schema": SCHEMA_VERSION,
        "physical_composer_schema": COMPOSER_SCHEMA,
        "physical_state_posterior_schema": PHYSICAL_STATE_SCHEMA,
        "physical_state_posterior_json": _json(state_payload),
        "physical_state_posterior_sha256": posterior_sha256(state_payload),
        "physical_weak_advance_json": _json(advance_payload),
        "physical_weak_advance_sha256": pre_event_prediction_sha256(advance),
        "physical_forcing_rebind_json": _json(rebind_audit),
        "physical_forcing_rebind_sha256": canonical_sha256(rebind_audit),
        "physical_path_projection_json": _json(projection_payload),
        "physical_path_projection_sha256": projection.path_projection_sha256,
        "physical_state_model_artifact_sha256": _text(
            state_model_artifact.get("artifact_sha256")
        ),
        "physical_reporting_operator_sha256": projection.reporting_operator_sha256,
        "physical_driver_generation_sha256": new_generation,
        "physical_scenario_control_lineage_json": _json(control_lineage),
        "physical_scenario_control_lineage_sha256": canonical_sha256(control_lineage),
        "physical_state_reference_valid_at": effective_from,
        "reported_physical_x_distribution": dict(projection.reported_x_distribution),
        "reported_x_lower_bound_c": previous_checkpoint.get("reported_x_lower_bound_c"),
        "forecast_revision_used_as_likelihood": False,
        "current_observation_consumed": False,
        "past_state_transitions_replayed": False,
        "state_advanced_with_superseded_driver_to_effective_time": True,
        "future_forcing_replaced": True,
        "places_orders": False,
    }


__all__ = [
    "COMPOSER_SCHEMA",
    "SCHEMA_VERSION",
    "build_remaining_reporting_slots",
    "compile_physical_weak_revision_extension",
    "compile_physical_p0_extension",
    "normalize_physical_predecessor_checkpoint",
    "compose_physical_canonical_event_extension",
]
