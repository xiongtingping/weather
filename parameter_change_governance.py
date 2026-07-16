from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from lineage_identity_runtime import lineage_id_from_parts


SCHEMA_VERSION = "parameter_change_governance_v1"
REQUEST_SCHEMA = "parameter_change_request_v1"
STATE_SCHEMA = "parameter_change_state_v1"
ROLLING_SCHEMA = "daily_calibration_rolling_summary_v1"

PARAMETER_CHANGE_STATES: tuple[str, ...] = (
    "proposed",
    "reviewed",
    "approved",
    "applied",
    "measured",
    "kept",
    "rolled_back",
)

STATE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "proposed": ("reviewed",),
    "reviewed": ("approved",),
    "approved": ("applied",),
    "applied": ("measured",),
    "measured": ("kept", "rolled_back"),
    "kept": (),
    "rolled_back": (),
}

IMPACT_LAYER_CHANGE_TYPES: dict[str, str] = {
    "prior": "prior_parameter_review",
    "forecast_weak": "forecast_weak_parameter_review",
    "forecast_source_weight_artifact": "forecast_source_weight_artifact_review",
    "metar_strong": "metar_strong_parameter_review",
    "posterior": "posterior_calibration_parameter_review",
    "phase_weight": "phase_weight_schedule_review",
    "source_latency": "source_latency_policy_review",
    "source_acquisition": "source_acquisition_policy_review",
    "fact_write": "fact_lineage_policy_review",
    "decision": "decision_threshold_review",
    "execution": "execution_slo_policy_review",
}


def parameter_change_governance_contract() -> dict[str, Any]:
    return {
        "schema": "parameter_change_governance_contract_v1",
        "read_only": True,
        "runtime_schema": SCHEMA_VERSION,
        "owner": "parameter_change_governance",
        "state_machine": {
            "states": list(PARAMETER_CHANGE_STATES),
            "transitions": {state: list(next_states) for state, next_states in STATE_TRANSITIONS.items()},
            "default_new_request_state": "proposed",
            "apply_allowed_in_state": "approved",
            "terminal_states": ["kept", "rolled_back"],
        },
        "required_bindings": [
            "calibration_candidate_id",
            "review_id",
            "daily_calibration_record_id",
            "impact_layer",
            "expected_improvement_metric",
            "rollback_plan",
            "measurement_plan",
            "artifact_sha256",
            "parent_artifact_sha256",
            "evaluation_manifest_sha256",
        ],
        "impact_layers": dict(IMPACT_LAYER_CHANGE_TYPES),
        "apply_gates": [
            "manual_review_completed",
            "explicit_user_approval",
            "rollback_plan_present",
            "parameter_diff_recorded",
            "fresh_parameter_snapshot_captured",
            "post_apply_measurement_windows_registered",
        ],
        "measurement_windows_days": [7, 30, 90],
        "forecast_source_weight_artifact_policy": {
            "source_data_policy": "external_only",
            "minimum_production_proposal_event_days": 80,
            "primary_metrics": ["bucket_nll", "bucket_rps"],
            "max_single_promotion_weight_change_percentage_points": 5.0,
            "runtime_apply_requires_approved_state": True,
            "rollback_must_not_require_retraining": True,
        },
        "write_boundary": {
            "daily_review_can_write_candidates_only_after_confirmed_review_schema": True,
            "parameter_change_events_require_separate_allow_parameter_event_apply": True,
            "runtime_requests_never_change_parameters": True,
        },
        "guards": {
            "writes_archive_db": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "changes_parameters": False,
            "historical_backfill": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _date_only(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10]


def _parse_date(value: Any) -> date | None:
    text = _date_only(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _mean(values: Sequence[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _counter_dict(values: Sequence[Any]) -> dict[str, int]:
    counts = Counter(str(value or "").strip() or "unknown" for value in values)
    return dict(sorted(counts.items()))


def _metrics_from_record(record: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(record.get("calibration_metrics"))


def _state_payload(current_state: str) -> dict[str, Any]:
    state = current_state if current_state in PARAMETER_CHANGE_STATES else "proposed"
    next_states = list(STATE_TRANSITIONS[state])
    block_reasons: list[str] = []
    if state != "approved":
        block_reasons.append("state_not_approved")
    block_reasons.append("explicit_user_confirmation_required")
    return {
        "schema": STATE_SCHEMA,
        "state": state,
        "allowed_states": list(PARAMETER_CHANGE_STATES),
        "allowed_next_states": next_states,
        "can_apply": False,
        "can_write_parameters": False,
        "apply_block_reasons": block_reasons,
        "requires_manual_review": state == "proposed",
        "requires_manual_approval": state in {"proposed", "reviewed"},
        "next_boundary": "manual_review_then_explicit_approval_required",
    }


def build_rolling_calibration_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    as_of_date: str = "",
    windows: Sequence[int] = (7, 30, 90),
) -> dict[str, Any]:
    dated_records: list[tuple[date | None, Mapping[str, Any]]] = [
        (_parse_date(record.get("event_date") or record.get("run_date")), _as_mapping(record))
        for record in records
        if isinstance(record, Mapping)
    ]
    available_dates = [record_date for record_date, _record in dated_records if record_date is not None]
    as_of = _parse_date(as_of_date) or (max(available_dates) if available_dates else date.today())
    window_payloads: dict[str, dict[str, Any]] = {}
    for window in windows:
        days = max(1, int(window))
        start = as_of - timedelta(days=days - 1)
        in_window = [
            record
            for record_date, record in dated_records
            if record_date is None or (start <= record_date <= as_of)
        ]
        metrics_rows = [_metrics_from_record(record) for record in in_window]
        brier_values = [
            value
            for value in (_float_or_none(metrics.get("posterior_brier")) for metrics in metrics_rows)
            if value is not None
        ]
        log_loss_values = [
            value
            for value in (_float_or_none(metrics.get("posterior_log_loss")) for metrics in metrics_rows)
            if value is not None
        ]
        target_hits = [metrics.get("target_hit") for metrics in metrics_rows if metrics.get("target_hit") is not None]
        target_hit_count = sum(1 for value in target_hits if bool(value))
        first_bad_links = [
            _first_text(
                _as_mapping(record.get("error_attribution")).get("primary_link"),
                metrics.get("first_bad_link"),
                "unknown",
            )
            for record, metrics in zip(in_window, metrics_rows)
        ]
        execution_misses = [
            _first_text(metrics.get("execution_miss_reason"))
            for metrics in metrics_rows
            if _first_text(metrics.get("execution_miss_reason"))
        ]
        station_ids = [
            _first_text(_as_mapping(record.get("strata")).get("station_id"), "unknown")
            for record in in_window
        ]
        phase_values = [
            _first_text(_as_mapping(record.get("strata")).get("phase"), "unknown")
            for record in in_window
        ]
        window_payloads[f"{days}d"] = {
            "window_days": days,
            "as_of_date": as_of.isoformat(),
            "window_start_date": start.isoformat(),
            "record_count": len(in_window),
            "settled_record_count": len(target_hits),
            "target_hit_count": target_hit_count,
            "target_hit_rate": None if not target_hits else target_hit_count / len(target_hits),
            "mean_posterior_brier": _mean(brier_values),
            "mean_posterior_log_loss": _mean(log_loss_values),
            "first_bad_link_counts": _counter_dict(first_bad_links),
            "execution_miss_count": len(execution_misses),
            "execution_miss_reason_counts": _counter_dict(execution_misses),
            "station_counts": _counter_dict(station_ids),
            "phase_counts": _counter_dict(phase_values),
        }
    return {
        "schema": ROLLING_SCHEMA,
        "read_only": True,
        "as_of_date": as_of.isoformat(),
        "window_keys": list(window_payloads.keys()),
        "windows": window_payloads,
        "record_count": len(dated_records),
        "history_source": "provided_daily_calibration_records",
        "coverage_status": "partial_until_daily_weather_reviews_table_persisted",
        "guards": {
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "changes_parameters": False,
            "historical_backfill": False,
        },
    }


def build_parameter_change_request(
    *,
    candidate: Mapping[str, Any],
    daily_calibration_record: Mapping[str, Any],
    rolling_calibration_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_map = _as_mapping(candidate)
    record = _as_mapping(daily_calibration_record)
    calibration_candidate_id = _first_text(candidate_map.get("calibration_candidate_id"))
    review_id = _first_text(candidate_map.get("review_id"), record.get("review_id"))
    impact_layer = _first_text(candidate_map.get("impact_layer"), "unknown")
    change_type = IMPACT_LAYER_CHANGE_TYPES.get(impact_layer, f"{impact_layer}_manual_review")
    expected_metric = _first_text(candidate_map.get("expected_improvement_metric"), "manual_review_outcome")
    request_id = lineage_id_from_parts(
        "parameter_change_request_id",
        calibration_candidate_id,
        review_id,
        impact_layer,
        expected_metric,
    )
    return {
        "schema": REQUEST_SCHEMA,
        "parameter_change_request_id": request_id,
        "calibration_candidate_id": calibration_candidate_id,
        "review_id": review_id,
        "daily_calibration_record_id": _first_text(record.get("daily_calibration_record_id")),
        "state": "proposed",
        "change_type": change_type,
        "impact_layer": impact_layer,
        "expected_improvement_metric": expected_metric,
        "trigger_sample": dict(_as_mapping(candidate_map.get("trigger_sample"))),
        "proposed_change": {
            "candidate_type": _first_text(candidate_map.get("candidate_type")),
            "target_bucket": candidate_map.get("target_bucket"),
            "settlement_actual": candidate_map.get("settlement_actual"),
            "requires_historical_recompute": bool(candidate_map.get("requires_historical_recompute")),
            "proposal_json": dict(_as_mapping(candidate_map.get("proposal_json"))),
        },
        "risk_controls": {
            "possible_side_effects": list(_as_sequence(candidate_map.get("possible_side_effects"))),
            "single_day_candidate_may_overfit": True,
            "requires_impact_review": True,
            "requires_rollback_plan_before_approval": True,
        },
        "rollback_plan": {
            "required_before_approval": True,
            "default_action": "restore_previous_parameter_snapshot",
            "rollback_trigger": "7_30_90_measurement_regresses_or_side_effect_detected",
        },
        "measurement_plan": {
            "windows_days": [7, 30, 90],
            "primary_metric": expected_metric,
            "rolling_summary_schema": _first_text(_as_mapping(rolling_calibration_summary).get("schema"), ROLLING_SCHEMA),
            "requires_measured_state_before_kept_or_rolled_back": True,
        },
        "governance_state": _state_payload("proposed"),
        "guards": {
            "writes_archive_db": False,
            "writes_parameter_change_events": False,
            "changes_parameters": False,
            "historical_backfill": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "next_boundary": "manual_review_required_before_reviewed_state",
    }


def build_parameter_change_state(
    *,
    calibration_candidates: Sequence[Mapping[str, Any]],
    daily_calibration_record: Mapping[str, Any],
    rolling_calibration_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidates = [dict(_as_mapping(candidate)) for candidate in calibration_candidates if isinstance(candidate, Mapping)]
    requests = [
        build_parameter_change_request(
            candidate=candidate,
            daily_calibration_record=daily_calibration_record,
            rolling_calibration_summary=rolling_calibration_summary,
        )
        for candidate in candidates
    ]
    return {
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "state": "no_candidates" if not requests else "proposed",
        "request_count": len(requests),
        "requests": requests,
        "rolling_calibration_summary": dict(_as_mapping(rolling_calibration_summary)),
        "governance_state": _state_payload("proposed" if requests else "kept"),
        "guards": {
            "writes_archive_db": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "changes_parameters": False,
            "historical_backfill": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "invariants": {
            "candidate_cannot_apply_without_reviewed": True,
            "candidate_cannot_apply_without_approved": True,
            "applied_requires_calibration_candidate_id": True,
            "applied_requires_rollback_plan": True,
            "measured_requires_7_30_90_windows": True,
        },
        "next_boundary": (
            "manual_review_required_before_parameter_change"
            if requests
            else "wait_for_bias_correction_candidate"
        ),
    }
