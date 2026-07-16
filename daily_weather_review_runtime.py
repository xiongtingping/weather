from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from lineage_identity_runtime import lineage_id_from_parts
from parameter_change_governance import build_parameter_change_state, build_rolling_calibration_summary
from weather_bias_calibration_runtime import (
    build_calibration_metrics,
    build_daily_calibration_record,
    build_error_attribution,
)


SCHEMA_VERSION = "daily_weather_review_runtime_v1"
REVIEW_JSON_SCHEMA = "daily_weather_review_json_v1"
CALIBRATION_CANDIDATE_SCHEMA = "daily_weather_review_calibration_candidate_v1"
CHAIN_NODE_TRACE_SCHEMA = "daily_review_chain_node_trace_v1"
CHAIN_NODE_SCHEMA = "daily_review_chain_node_v1"
CHAIN_NODE_ORDER = (
    "acquisition",
    "parsing",
    "fact_write",
    "prior",
    "weak",
    "phase_weight",
    "strong",
    "posterior",
    "decision",
    "execution",
    "settlement",
)


def daily_review_input_contract() -> dict[str, Any]:
    return {
        "schema": "daily_weather_review_input_contract_v1",
        "read_only": True,
        "runtime_schema": SCHEMA_VERSION,
        "required_input_layers": [
            {
                "layer": "source",
                "required_fields": ["source_observations_or_source_acquisition_trace", "source_latency_trace"],
                "missing_policy": "first_bad_link=source_acquisition",
            },
            {
                "layer": "fact",
                "required_fields": ["fact_snapshot_id", "slug", "event_date", "fact_as_of"],
                "missing_policy": "first_bad_link=fact_snapshot",
            },
            {
                "layer": "posterior",
                "required_fields": [
                    "posterior_ssot_fact",
                    "posterior_ssot_fact.target_bucket",
                    "posterior_ssot_fact.posterior_run_id",
                ],
                "missing_policy": "first_bad_link=posterior",
            },
            {
                "layer": "decision",
                "required_fields": ["auto_trade_decisions.will_order", "decided_state", "reason_codes_json"],
                "missing_policy": "decision_summary.blocked_or_skip_count",
            },
            {
                "layer": "execution",
                "required_fields": ["auto_orders.order_id_or_order_status", "signal_to_order_timing"],
                "missing_policy": "first_bad_link=execution_when_will_order_without_submitted_order",
            },
            {
                "layer": "settlement",
                "required_fields": ["settlement_actual"],
                "missing_policy": "first_bad_link=settlement",
            },
            {
                "layer": "audit_attachments",
                "required_fields": ["ui_snapshot_lineage", "ssot_chain_replay"],
                "missing_policy": "attachment_blocker_maps_to_fact_write_or_execution",
            },
        ],
        "first_bad_link_order": [
            "source_acquisition",
            "fact_write",
            "fact_snapshot",
            "posterior",
            "decision",
            "execution",
            "settlement",
            "posterior_calibration",
            "daily_review",
            "none",
        ],
        "output_records": [
            "daily_weather_review",
            "daily_calibration_record",
            "bias_correction_candidates",
            "parameter_change_state",
            "rolling_calibration_summary",
        ],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_daily_weather_reviews": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _append_unique(out: list[str], raw: Any) -> None:
    text = str(raw or "").strip()
    if text and text not in out:
        out.append(text)


def _posterior_ssot_fact(fact_snapshot: Mapping[str, Any], posterior: Mapping[str, Any] | None) -> tuple[str, dict[str, Any]]:
    fact = _as_mapping(fact_snapshot)
    post = _as_mapping(posterior)
    for source_name, source in (("fact_snapshot", fact), ("posterior", post)):
        ssot = _as_mapping(source.get("posterior_ssot_fact"))
        if ssot:
            return source_name, dict(ssot)
        if str(source.get("schema") or "") == "bayes_posterior_ssot_fact_v1":
            return source_name, dict(source)
    return "", {}


def _posterior_lineage_state(
    fact_snapshot: Mapping[str, Any],
    posterior: Mapping[str, Any] | None,
    *,
    target_bucket: str,
    target_probability: float | None,
) -> dict[str, Any]:
    fact = _as_mapping(fact_snapshot)
    post = _as_mapping(posterior)
    lineage = _as_mapping(fact.get("lineage_state"))
    posterior_runtime = _as_mapping(fact.get("posterior_runtime")) or _as_mapping(post.get("posterior_runtime"))
    ssot_source, ssot = _posterior_ssot_fact(fact, post)
    fact_snapshot_id = _first_text(
        fact.get("fact_snapshot_id"),
        lineage.get("fact_snapshot_id"),
        ssot.get("fact_snapshot_id"),
    )
    posterior_run_id = _first_text(
        ssot.get("posterior_run_id"),
        fact.get("posterior_run_id"),
        lineage.get("posterior_run_id"),
        posterior_runtime.get("posterior_run_id"),
        post.get("posterior_run_id"),
    )
    ssot_fact_snapshot_id = _first_text(ssot.get("fact_snapshot_id"))
    ssot_posterior_run_id = _first_text(ssot.get("posterior_run_id"))
    issues: list[str] = []
    warnings: list[str] = []
    if not ssot:
        issues.append("posterior_ssot_fact_missing")
    else:
        if not _first_text(ssot.get("target_bucket"), ssot.get("top_bucket")):
            issues.append("posterior_ssot_fact_target_bucket_missing")
        if _float_or_none(ssot.get("target_probability")) is None and _float_or_none(ssot.get("top_probability")) is None:
            issues.append("posterior_ssot_fact_target_probability_missing")
        if not posterior_run_id:
            issues.append("posterior_run_id_missing")
        if ssot_fact_snapshot_id and fact_snapshot_id and ssot_fact_snapshot_id != fact_snapshot_id:
            issues.append("posterior_ssot_fact_snapshot_id_mismatch")
        if ssot_posterior_run_id and posterior_run_id and ssot_posterior_run_id != posterior_run_id:
            issues.append("posterior_ssot_fact_posterior_run_id_mismatch")
    return {
        "schema": "daily_review_posterior_lineage_v1",
        "ok": not issues,
        "status": "posterior_ssot_fact_present" if ssot else "posterior_ssot_fact_missing",
        "posterior_target_source": "posterior_ssot_fact" if ssot else "missing_posterior_ssot_fact",
        "posterior_ssot_fact_present": bool(ssot),
        "posterior_ssot_fact_source": ssot_source,
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": posterior_run_id,
        "posterior_ssot_fact_fact_snapshot_id": ssot_fact_snapshot_id,
        "posterior_ssot_fact_posterior_run_id": ssot_posterior_run_id,
        "target_bucket": target_bucket,
        "target_probability": target_probability,
        "target_probability_field": _first_text(ssot.get("target_probability_field")),
        "phase": _first_text(ssot.get("phase"), fact.get("phase")),
        "beta_weak": _float_or_none(ssot.get("beta_weak") if ssot.get("beta_weak") is not None else fact.get("beta_weak")),
        "beta_strong": _float_or_none(
            ssot.get("beta_strong") if ssot.get("beta_strong") is not None else fact.get("beta_strong")
        ),
        "phase_reason": _first_text(ssot.get("phase_reason"), fact.get("phase_reason")),
        "hard_mask_reason": _first_text(ssot.get("hard_mask_reason"), fact.get("hard_mask_reason")),
        "issues": issues,
        "warnings": warnings,
    }


def _audit_attachment_summary(audit_attachments: Mapping[str, Any] | None) -> dict[str, Any]:
    attachments = _as_mapping(audit_attachments)
    items: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for name, raw_payload in attachments.items():
        payload = _as_mapping(raw_payload)
        if not payload:
            continue
        item_blockers = [str(value) for value in _as_sequence(payload.get("blockers")) if str(value or "").strip()]
        item_warnings = [str(value) for value in _as_sequence(payload.get("warnings")) if str(value or "").strip()]
        for blocker in item_blockers:
            _append_unique(blockers, f"{name}:{blocker}")
        for warning in item_warnings:
            _append_unique(warnings, f"{name}:{warning}")
        item = {
            "name": str(name),
            "schema": _first_text(payload.get("schema")),
            "ok": payload.get("ok") is not False and not item_blockers,
            "read_only": payload.get("read_only") is not False,
            "blockers": item_blockers[:20],
            "warnings": item_warnings[:20],
        }
        counts = payload.get("counts")
        if isinstance(counts, Mapping):
            item["counts"] = dict(counts)
        snapshot = payload.get("snapshot")
        if isinstance(snapshot, Mapping):
            item["snapshot"] = {
                key: snapshot.get(key)
                for key in (
                    "snapshot_id",
                    "run_date",
                    "lineage_state_schema",
                    "lineage_placeholder",
                    "fact_lineage_complete",
                    "complete",
                )
                if key in snapshot
            }
        signal_count = payload.get("signal_count")
        if signal_count is not None:
            item["signal_count"] = signal_count
        items.append(item)
    failed = [item for item in items if item.get("ok") is False]
    return {
        "schema": "daily_weather_review_audit_attachment_summary_v1",
        "all_ok": not failed and not blockers,
        "attachment_count": len(items),
        "failed_attachment_count": len(failed),
        "items": items,
        "blockers": blockers,
        "warnings": warnings,
    }


def _audit_attachment_first_bad_link(audit_summary: Mapping[str, Any]) -> tuple[str, str]:
    if not audit_summary or audit_summary.get("all_ok") is not False:
        return "", ""
    items = _as_sequence(audit_summary.get("items"))
    first_failed = next((dict(_as_mapping(item)) for item in items if _as_mapping(item).get("ok") is False), {})
    name = _first_text(first_failed.get("name"))
    if name == "ui_snapshot_lineage":
        return "fact_write", "ui_snapshot_lineage_audit_failed"
    if name == "ssot_chain_replay":
        return "execution", "ssot_chain_replay_audit_failed"
    return "daily_review", "audit_attachment_failed"


def _distribution_rows(fact_snapshot: Mapping[str, Any], posterior: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    post = _as_mapping(posterior) or _as_mapping(fact_snapshot.get("posterior_temperature_distribution"))
    _ssot_source, ssot = _posterior_ssot_fact(fact_snapshot, posterior)
    raw = (
        post.get("bucket_probabilities")
        or post.get("distribution")
        or ssot.get("ranked_bucket_probabilities")
        or ssot.get("bucket_probabilities")
        or fact_snapshot.get("distribution")
        or fact_snapshot.get("rows")
    )
    rows: list[dict[str, Any]] = []
    for item in _as_sequence(raw):
        row = dict(_as_mapping(item))
        label = _first_text(row.get("outcome"), row.get("label"), row.get("bucket"))
        prob = _float_or_none(
            row.get("probability")
            if row.get("probability") is not None
            else row.get("q_temperature_bucket_posterior")
            if row.get("q_temperature_bucket_posterior") is not None
            else row.get("q_bayes_posterior")
        )
        if label and prob is not None:
            rows.append({"label": label, "probability": max(0.0, min(1.0, prob)), "raw": row})
    return rows


def _top_bucket(rows: Sequence[Mapping[str, Any]]) -> tuple[str, float | None]:
    best_label = ""
    best_prob: float | None = None
    for row in rows:
        prob = _float_or_none(row.get("probability"))
        label = _first_text(row.get("label"))
        if not label or prob is None:
            continue
        if best_prob is None or prob > best_prob:
            best_label = label
            best_prob = prob
    return best_label, best_prob


def _target_bucket(
    fact_snapshot: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    posterior: Mapping[str, Any] | None = None,
) -> tuple[str, float | None]:
    _ssot_source, ssot = _posterior_ssot_fact(fact_snapshot, posterior)
    label = _first_text(ssot.get("target_bucket"), ssot.get("top_bucket"))
    prob = _float_or_none(
        ssot.get("target_probability")
        if ssot.get("target_probability") is not None
        else ssot.get("top_probability")
    )
    if label:
        return label, prob
    return "", None


def _settlement_actual(settlement: Mapping[str, Any] | None, fact_snapshot: Mapping[str, Any]) -> float | None:
    source = _as_mapping(settlement)
    for key in ("settlement_actual", "settlement_actual_c", "actual", "actual_c"):
        value = _float_or_none(source.get(key))
        if value is not None:
            return value
    for key in ("settlement_actual", "observed_today_max_c", "hard_source_observed_value_c"):
        value = _float_or_none(fact_snapshot.get(key))
        if value is not None:
            return value
    return None


def _bucket_hit(label: str, actual: float | None) -> bool | None:
    if actual is None:
        return None
    text = _first_text(label)
    if not text:
        return None
    normalized = text.replace("℃", "").replace("°C", "").strip()
    try:
        return int(round(float(actual))) == int(round(float(normalized)))
    except ValueError:
        return None


def _decision_summary(decisions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(_as_mapping(item)) for item in decisions if isinstance(item, Mapping)]
    will_order = [row for row in rows if _truthy(row.get("will_order"))]
    blocked = [
        row
        for row in rows
        if not _truthy(row.get("will_order")) and _first_text(row.get("reason_codes_json"), row.get("reason"), row.get("decided_state"))
    ]
    return {
        "decision_count": len(rows),
        "will_order_count": len(will_order),
        "blocked_or_skip_count": len(blocked),
        "latest_decision_state": _first_text(rows[-1].get("decided_state"), rows[-1].get("action")) if rows else "",
    }


def _execution_summary(orders: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(_as_mapping(item)) for item in orders if isinstance(item, Mapping)]
    submitted = [
        row
        for row in rows
        if _first_text(row.get("order_id"), row.get("clob_order_id")) or _first_text(row.get("order_status"), row.get("status")) in {"submitted", "confirmed", "filled"}
    ]
    return {
        "order_count": len(rows),
        "submitted_order_count": len(submitted),
        "latest_order_status": _first_text(rows[-1].get("order_status"), rows[-1].get("status")) if rows else "",
    }


def _sequence_of_mappings(value: Any) -> list[dict[str, Any]]:
    return [dict(_as_mapping(item)) for item in _as_sequence(value) if isinstance(item, Mapping)]


def _source_trace(fact_snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(fact_snapshot.get("source_acquisition_trace"))


def _source_observation_rows(fact_snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _sequence_of_mappings(fact_snapshot.get("source_observations")):
        key = "|".join(
            (
                _first_text(item.get("source_event_id")),
                _first_text(item.get("source_id")),
                _first_text(item.get("valid_time"), item.get("validTime"), item.get("valid_time_local")),
            )
        )
        if key and key not in seen:
            seen.add(key)
            rows.append(item)
    trace = _source_trace(fact_snapshot)
    for item in _sequence_of_mappings(trace.get("source_observations")):
        key = "|".join(
            (
                _first_text(item.get("source_event_id")),
                _first_text(item.get("source_id")),
                _first_text(item.get("valid_time"), item.get("validTime"), item.get("valid_time_local")),
            )
        )
        if key and key not in seen:
            seen.add(key)
            rows.append(item)
    return rows


def _source_latency_rows(fact_snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    trace = _source_trace(fact_snapshot)
    return _sequence_of_mappings(trace.get("source_latency_trace"))


def _source_event_ids_from_fact(fact_snapshot: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for row in _source_observation_rows(fact_snapshot):
        _append_unique(out, row.get("source_event_id"))
    trace = _source_trace(fact_snapshot)
    for value in _as_sequence(trace.get("source_event_ids")):
        _append_unique(out, value)
    lineage = _as_mapping(fact_snapshot.get("lineage_state"))
    for value in _as_sequence(lineage.get("source_event_ids")):
        _append_unique(out, value)
    return out


def _source_ids_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        _append_unique(out, row.get("source_id"))
    return out


def _parser_versions_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        _append_unique(out, row.get("parser_version"))
        _append_unique(out, row.get("parser"))
    return out


def _timestamps_from_mapping(source: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            out[str(key)] = value
    return out


def _first_timestamp_from_rows(rows: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        for row in rows:
            value = row.get(key)
            if value not in (None, ""):
                out[str(key)] = value
                break
    return out


def _node(
    node_id: str,
    *,
    input_refs: Mapping[str, Any] | None = None,
    output_refs: Mapping[str, Any] | None = None,
    timestamps: Mapping[str, Any] | None = None,
    terminal_state: str = "",
    terminal_reason: str = "",
) -> dict[str, Any]:
    state = _first_text(terminal_state, "missing")
    return {
        "schema": CHAIN_NODE_SCHEMA,
        "node_id": node_id,
        "input_refs": dict(input_refs or {}),
        "output_refs": dict(output_refs or {}),
        "timestamps": dict(timestamps or {}),
        "timestamp_status": "available" if timestamps else "missing",
        "terminal_state": state,
        "terminal_reason": _first_text(terminal_reason, "not_recorded"),
        "ok": state in {"ok", "not_applicable"},
    }


def _first_decision_timestamps(decisions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(_as_mapping(item)) for item in decisions if isinstance(item, Mapping)]
    return _first_timestamp_from_rows(rows, ("created_at", "decision_written_at", "updated_at"))


def _first_order_timestamps(orders: Sequence[Mapping[str, Any]], timing: Mapping[str, Any]) -> dict[str, Any]:
    rows = [dict(_as_mapping(item)) for item in orders if isinstance(item, Mapping)]
    out = _first_timestamp_from_rows(rows, ("created_at", "order_checked_at", "submitted_at"))
    out.update(_timestamps_from_mapping(timing, ("orderbook_checked_at", "risk_checked_at", "submit_requested_at", "exchange_ack_at", "order_submitted_at", "terminal_at")))
    return out


def _availability_state(present: bool, *, missing_reason: str = "missing") -> tuple[str, str]:
    if present:
        return "ok", "available"
    return "missing", missing_reason


def _daily_review_chain_node_trace(
    *,
    fact_snapshot: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    source_health: Mapping[str, Any],
    posterior_lineage: Mapping[str, Any],
    decision_summary: Mapping[str, Any],
    execution_summary: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    timing: Mapping[str, Any],
    settlement_actual: float | None,
    target_hit: bool | None,
    first_bad_link: str,
) -> dict[str, Any]:
    fact = _as_mapping(fact_snapshot)
    source_rows = _source_observation_rows(fact)
    latency_rows = _source_latency_rows(fact)
    source_event_ids = _source_event_ids_from_fact(fact)
    source_ids = _source_ids_from_rows(source_rows) or _source_ids_from_rows(latency_rows)
    fact_snapshot_id = _first_text(fact.get("fact_snapshot_id"), _as_mapping(fact.get("lineage_state")).get("fact_snapshot_id"))
    fact_times = _timestamps_from_mapping(fact, ("fact_as_of", "created_at", "updated_at"))
    source_ok = source_health.get("ok") is not False and bool(source_rows or _source_trace(fact))
    source_state, source_reason = (
        ("blocked", _first_text(source_health.get("reason"), source_health.get("block_reason"), "source_health_failed"))
        if source_health and source_health.get("ok") is False
        else _availability_state(source_ok, missing_reason="source_observations_or_trace_missing")
    )
    parsing_state, parsing_reason = _availability_state(bool(source_rows), missing_reason="normalized_source_observations_missing")
    fact_state, fact_reason = _availability_state(bool(fact_snapshot_id), missing_reason="fact_snapshot_id_missing")

    prior_obj = _as_mapping(fact.get("temperature_prior")) or _as_mapping(fact.get("prior_layer"))
    prior_rows = _as_sequence(fact.get("prior_distribution")) or _as_sequence(prior_obj.get("bucket_probabilities"))
    prior_state, prior_reason = _availability_state(bool(prior_obj or prior_rows), missing_reason="prior_payload_missing")
    weak_obj = _as_mapping(fact.get("forecast_weak_likelihood")) or _as_mapping(fact.get("weak_likelihood"))
    weak_state, weak_reason = _availability_state(bool(weak_obj), missing_reason="forecast_weak_likelihood_missing")
    strong_obj = _as_mapping(fact.get("metar_strong_likelihood")) or _as_mapping(fact.get("strong_likelihood"))
    strong_state, strong_reason = _availability_state(bool(strong_obj), missing_reason="metar_strong_likelihood_missing")
    phase_present = bool(_first_text(posterior_lineage.get("phase"), fact.get("phase")))
    phase_state, phase_reason = _availability_state(phase_present, missing_reason="phase_weight_missing")
    posterior_state, posterior_reason = _availability_state(
        bool(rows) and posterior_lineage.get("ok") is not False,
        missing_reason="posterior_distribution_or_lineage_missing",
    )
    if posterior_lineage.get("ok") is False:
        posterior_state = "blocked"
        issues = [str(value or "").strip() for value in _as_sequence(posterior_lineage.get("issues"))]
        posterior_reason = next((value for value in issues if value), "posterior_lineage_failed")
    decision_count = int(decision_summary.get("decision_count") or 0)
    decision_state, decision_reason = _availability_state(decision_count > 0, missing_reason="decision_missing")
    will_order_count = int(decision_summary.get("will_order_count") or 0)
    submitted_count = int(execution_summary.get("submitted_order_count") or 0)
    order_count = int(execution_summary.get("order_count") or 0)
    if will_order_count > 0 and submitted_count <= 0:
        execution_state, execution_reason = "blocked", "will_order_without_submitted_order"
    elif order_count > 0 or timing:
        execution_state, execution_reason = "ok", "execution_trace_available"
    elif will_order_count <= 0:
        execution_state, execution_reason = "not_applicable", "no_order_expected"
    else:
        execution_state, execution_reason = "missing", "execution_missing"
    settlement_state, settlement_reason = _availability_state(settlement_actual is not None, missing_reason="settlement_actual_missing")

    nodes = [
        _node(
            "acquisition",
            input_refs={"source_health": dict(source_health or {})},
            output_refs={
                "source_event_ids": source_event_ids,
                "source_ids": source_ids,
                "source_observation_count": len(source_rows),
                "source_latency_count": len(latency_rows),
            },
            timestamps=_first_timestamp_from_rows(latency_rows or source_rows, ("fetched_at", "created_at", "valid_time", "validTime")),
            terminal_state=source_state,
            terminal_reason=source_reason,
        ),
        _node(
            "parsing",
            input_refs={"source_event_ids": source_event_ids, "source_ids": source_ids},
            output_refs={
                "normalized_observation_count": len(source_rows),
                "parser_versions": _parser_versions_from_rows(source_rows),
            },
            timestamps=_first_timestamp_from_rows(source_rows, ("parsed_at", "fetched_at", "created_at")),
            terminal_state=parsing_state,
            terminal_reason=parsing_reason,
        ),
        _node(
            "fact_write",
            input_refs={"source_event_ids": source_event_ids},
            output_refs={
                "fact_snapshot_id": fact_snapshot_id,
                "composer_version": _first_text(fact.get("composer_version")),
            },
            timestamps=fact_times,
            terminal_state=fact_state,
            terminal_reason=fact_reason,
        ),
        _node(
            "prior",
            input_refs={"fact_snapshot_id": fact_snapshot_id},
            output_refs={
                "prior_bucket_count": len(prior_rows),
                "prior_na_reason": _first_text(prior_obj.get("prior_na_reason"), fact.get("prior_na_reason")),
            },
            timestamps=_timestamps_from_mapping(prior_obj, ("created_at", "as_of", "computed_at")),
            terminal_state=prior_state,
            terminal_reason=prior_reason,
        ),
        _node(
            "weak",
            input_refs={"fact_snapshot_id": fact_snapshot_id},
            output_refs={
                "source_group_count": len(_as_sequence(weak_obj.get("source_groups"))),
                "na_reason": _first_text(weak_obj.get("na_reason"), weak_obj.get("weak_likelihood_na_reason")),
            },
            timestamps=_timestamps_from_mapping(weak_obj, ("created_at", "as_of", "computed_at")),
            terminal_state=weak_state,
            terminal_reason=weak_reason,
        ),
        _node(
            "phase_weight",
            input_refs={"fact_snapshot_id": fact_snapshot_id},
            output_refs={
                "phase": _first_text(posterior_lineage.get("phase"), fact.get("phase")),
                "beta_weak": posterior_lineage.get("beta_weak"),
                "beta_strong": posterior_lineage.get("beta_strong"),
                "phase_reason": _first_text(posterior_lineage.get("phase_reason"), fact.get("phase_reason")),
            },
            timestamps=_timestamps_from_mapping(fact, ("phase_computed_at", "posterior_written_at", "fact_as_of")),
            terminal_state=phase_state,
            terminal_reason=phase_reason,
        ),
        _node(
            "strong",
            input_refs={"fact_snapshot_id": fact_snapshot_id, "source_event_ids": source_event_ids},
            output_refs={
                "state_prob_count": len(_as_sequence(strong_obj.get("state_probs"))),
                "hard_constraint_count": len(_as_sequence(strong_obj.get("hard_constraints"))),
                "na_reason": _first_text(strong_obj.get("na_reason"), strong_obj.get("strong_likelihood_na_reason")),
            },
            timestamps=_timestamps_from_mapping(strong_obj, ("created_at", "as_of", "computed_at")),
            terminal_state=strong_state,
            terminal_reason=strong_reason,
        ),
        _node(
            "posterior",
            input_refs={"fact_snapshot_id": fact_snapshot_id},
            output_refs={
                "posterior_run_id": _first_text(posterior_lineage.get("posterior_run_id")),
                "target_bucket": _first_text(posterior_lineage.get("target_bucket")),
                "target_probability": posterior_lineage.get("target_probability"),
                "posterior_row_count": len(rows),
            },
            timestamps=_timestamps_from_mapping(fact, ("posterior_written_at", "fact_as_of", "created_at")),
            terminal_state=posterior_state,
            terminal_reason=posterior_reason,
        ),
        _node(
            "decision",
            input_refs={
                "posterior_run_id": _first_text(posterior_lineage.get("posterior_run_id")),
                "target_bucket": _first_text(posterior_lineage.get("target_bucket")),
            },
            output_refs=dict(decision_summary),
            timestamps=_first_decision_timestamps(decisions),
            terminal_state=decision_state,
            terminal_reason=decision_reason,
        ),
        _node(
            "execution",
            input_refs={"will_order_count": will_order_count},
            output_refs=dict(execution_summary),
            timestamps=_first_order_timestamps(orders, timing),
            terminal_state=execution_state,
            terminal_reason=execution_reason,
        ),
        _node(
            "settlement",
            input_refs={"event_date": fact.get("event_date")},
            output_refs={"settlement_actual": settlement_actual, "target_hit": target_hit},
            timestamps=_timestamps_from_mapping(fact, ("settlement_signal_created_at", "event_end_utc", "settled_at")),
            terminal_state=settlement_state,
            terminal_reason=settlement_reason,
        ),
    ]
    missing_nodes = [node["node_id"] for node in nodes if node.get("terminal_state") == "missing"]
    blocked_nodes = [node["node_id"] for node in nodes if node.get("terminal_state") == "blocked"]
    first_bad_node = first_bad_link if first_bad_link in CHAIN_NODE_ORDER else ""
    return {
        "schema": CHAIN_NODE_TRACE_SCHEMA,
        "node_order": list(CHAIN_NODE_ORDER),
        "node_count": len(nodes),
        "nodes": nodes,
        "first_bad_link": first_bad_link,
        "first_bad_link_node_id": first_bad_node,
        "missing_node_ids": missing_nodes,
        "blocked_node_ids": blocked_nodes,
        "all_required_nodes_present": [node["node_id"] for node in nodes] == list(CHAIN_NODE_ORDER),
        "read_only": True,
    }


def _first_bad_link(
    *,
    fact_snapshot: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    source_health: Mapping[str, Any],
    decision_summary: Mapping[str, Any],
    execution_summary: Mapping[str, Any],
    settlement_actual: float | None,
    target_hit: bool | None,
    posterior_lineage: Mapping[str, Any] | None = None,
    audit_summary: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    if source_health and source_health.get("ok") is False:
        return "source_acquisition", _first_text(source_health.get("reason"), source_health.get("block_reason"), "source_health_failed")
    audit_bad_link, audit_bad_reason = _audit_attachment_first_bad_link(_as_mapping(audit_summary))
    if audit_bad_link:
        return audit_bad_link, audit_bad_reason
    if not fact_snapshot.get("source_observations") and not fact_snapshot.get("source_acquisition_trace"):
        return "source_acquisition", "source_lineage_missing"
    if not _first_text(fact_snapshot.get("fact_snapshot_id"), _as_mapping(fact_snapshot.get("lineage_state")).get("fact_snapshot_id")):
        return "fact_snapshot", "fact_snapshot_id_missing"
    if not rows:
        return "posterior", "posterior_distribution_missing"
    posterior_lineage_ref = _as_mapping(posterior_lineage)
    if posterior_lineage_ref and posterior_lineage_ref.get("ok") is False:
        issues = [str(value or "").strip() for value in _as_sequence(posterior_lineage_ref.get("issues"))]
        return "posterior", next((value for value in issues if value), "posterior_lineage_failed")
    if int(decision_summary.get("will_order_count") or 0) > 0 and int(execution_summary.get("submitted_order_count") or 0) <= 0:
        return "execution", "will_order_without_submitted_order"
    if settlement_actual is None:
        return "settlement", "settlement_actual_missing"
    if target_hit is False:
        return "posterior_calibration", "target_bucket_missed_settlement"
    return "none", ""


def _calibration_candidates(
    *,
    review_id: str,
    first_bad_link: str,
    target_bucket: str,
    target_probability: float | None,
    settlement_actual: float | None,
    target_hit: bool | None,
    fact_snapshot_id: str = "",
    posterior_run_id: str = "",
    calibration_metrics: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if target_hit is not False and first_bad_link in {"", "none"}:
        return []
    candidate_type = {
        "source_acquisition": "source_quality_review",
        "fact_snapshot": "fact_lineage_review",
        "posterior": "posterior_distribution_review",
        "posterior_calibration": "posterior_calibration_review",
        "execution": "execution_slo_review",
    }.get(first_bad_link, "daily_review_manual_review")
    impact_layer = {
        "source_quality_review": "source_acquisition",
        "fact_lineage_review": "fact_write",
        "posterior_distribution_review": "posterior",
        "posterior_calibration_review": "posterior",
        "execution_slo_review": "execution",
    }.get(candidate_type, "daily_review")
    metrics = dict(calibration_metrics or {})
    return [
        {
            "schema": CALIBRATION_CANDIDATE_SCHEMA,
            "calibration_candidate_id": lineage_id_from_parts(
                "calibration_candidate_id",
                review_id,
                candidate_type,
                target_bucket,
                settlement_actual,
            ),
            "review_id": review_id,
            "candidate_type": candidate_type,
            "state": "proposed",
            "fact_snapshot_id": fact_snapshot_id,
            "posterior_run_id": posterior_run_id,
            "impact_layer": impact_layer,
            "target_bucket": target_bucket,
            "target_probability": target_probability,
            "settlement_actual": settlement_actual,
            "target_hit": target_hit,
            "trigger_sample": {
                "review_id": review_id,
                "fact_snapshot_id": fact_snapshot_id,
                "posterior_run_id": posterior_run_id,
                "first_bad_link": first_bad_link,
                "target_bucket": target_bucket,
                "settlement_actual": settlement_actual,
            },
            "expected_improvement_metric": (
                "posterior_brier"
                if impact_layer in {"posterior", "forecast_weak", "metar_strong"}
                else "signal_to_order_slo"
                if impact_layer == "execution"
                else "source_lineage_completeness"
            ),
            "possible_side_effects": [
                "single_day_candidate_may_overfit",
                "requires_review_before_parameter_change",
            ],
            "requires_historical_recompute": impact_layer in {"posterior", "forecast_weak", "metar_strong"},
            "proposal_json": {
                "action": "review_first_bad_link_before_parameter_change",
                "first_bad_link": first_bad_link,
                "impact_layer": impact_layer,
                "posterior_brier": metrics.get("posterior_brier"),
                "posterior_log_loss": metrics.get("posterior_log_loss"),
                "no_auto_apply": True,
            },
        }
    ]


def build_daily_weather_review(
    *,
    run_date: str,
    slug: str,
    event_date: str,
    fact_snapshot: Mapping[str, Any] | None = None,
    posterior: Mapping[str, Any] | None = None,
    decisions: Sequence[Mapping[str, Any]] = (),
    orders: Sequence[Mapping[str, Any]] = (),
    settlement: Mapping[str, Any] | None = None,
    source_health: Mapping[str, Any] | None = None,
    timing: Mapping[str, Any] | None = None,
    audit_attachments: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fact = dict(fact_snapshot or {})
    rows = _distribution_rows(fact, posterior)
    top_bucket, top_probability = _top_bucket(rows)
    target_bucket, target_probability = _target_bucket(fact, rows, posterior)
    actual = _settlement_actual(settlement, fact)
    target_hit = _bucket_hit(target_bucket, actual)
    decision = _decision_summary(decisions)
    execution = _execution_summary(orders)
    audit_summary = _audit_attachment_summary(audit_attachments)
    posterior_lineage = _posterior_lineage_state(
        fact,
        posterior,
        target_bucket=target_bucket,
        target_probability=target_probability,
    )
    first_bad_link, first_bad_link_reason = _first_bad_link(
        fact_snapshot=fact,
        rows=rows,
        source_health=dict(source_health or {}),
        decision_summary=decision,
        execution_summary=execution,
        settlement_actual=actual,
        target_hit=target_hit,
        posterior_lineage=posterior_lineage,
        audit_summary=audit_summary,
    )
    chain_node_trace = _daily_review_chain_node_trace(
        fact_snapshot=fact,
        rows=rows,
        source_health=dict(source_health or {}),
        posterior_lineage=posterior_lineage,
        decision_summary=decision,
        execution_summary=execution,
        decisions=decisions,
        orders=orders,
        timing=dict(timing or {}),
        settlement_actual=actual,
        target_hit=target_hit,
        first_bad_link=first_bad_link,
    )
    fact_snapshot_id = _first_text(fact.get("fact_snapshot_id"), _as_mapping(fact.get("lineage_state")).get("fact_snapshot_id"))
    posterior_run_id = _first_text(posterior_lineage.get("posterior_run_id"))
    review_id = lineage_id_from_parts(
        "review_id",
        slug,
        event_date,
        run_date,
        fact_snapshot_id,
        posterior_run_id,
        first_bad_link,
    )
    calibration_metrics = build_calibration_metrics(
        fact_snapshot=fact,
        posterior_rows=rows,
        settlement_actual=actual,
        target_bucket=target_bucket,
        target_probability=target_probability,
        target_hit=target_hit,
        decision_summary=decision,
        execution_summary=execution,
        first_bad_link=first_bad_link,
    )
    error_attribution = build_error_attribution(
        first_bad_link=first_bad_link,
        first_bad_link_reason=first_bad_link_reason,
        calibration_metrics=calibration_metrics,
        audit_summary=audit_summary,
    )
    daily_calibration_record = build_daily_calibration_record(
        review_id=review_id,
        run_date=str(run_date or event_date or ""),
        slug=str(slug or ""),
        event_date=str(event_date or ""),
        fact_snapshot_id=fact_snapshot_id,
        calibration_metrics=calibration_metrics,
        error_attribution=error_attribution,
        fact_snapshot=fact,
    )
    candidates = _calibration_candidates(
        review_id=review_id,
        first_bad_link=first_bad_link,
        target_bucket=target_bucket,
        target_probability=target_probability,
        settlement_actual=actual,
        target_hit=target_hit,
        fact_snapshot_id=fact_snapshot_id,
        posterior_run_id=posterior_run_id,
        calibration_metrics=calibration_metrics,
    )
    rolling_calibration_summary = build_rolling_calibration_summary(
        [daily_calibration_record],
        as_of_date=str(event_date or run_date or ""),
    )
    parameter_change_state = build_parameter_change_state(
        calibration_candidates=candidates,
        daily_calibration_record=daily_calibration_record,
        rolling_calibration_summary=rolling_calibration_summary,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "read_only": True,
        "run_date": str(run_date or event_date or ""),
        "slug": str(slug or ""),
        "event_date": str(event_date or ""),
        "review_id": review_id,
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": posterior_run_id,
        "first_bad_link": first_bad_link,
        "first_bad_link_reason": first_bad_link_reason,
        "review_json": {
            "schema": REVIEW_JSON_SCHEMA,
            "target_bucket": target_bucket,
            "target_probability": target_probability,
            "top_bucket": top_bucket,
            "top_probability": top_probability,
            "settlement_actual": actual,
            "target_hit": target_hit,
            "source_health": dict(source_health or {}),
            "posterior_lineage": posterior_lineage,
            "decision_summary": decision,
            "execution_summary": execution,
            "timing": dict(timing or {}),
            "chain_node_trace": chain_node_trace,
            "audit_attachment_summary": audit_summary,
            "calibration_metrics": calibration_metrics,
            "error_attribution": error_attribution,
            "daily_calibration_record": daily_calibration_record,
            "rolling_calibration_summary": rolling_calibration_summary,
            "parameter_change_state": parameter_change_state,
            "posterior_row_count": len(rows),
            "calibration_candidate_count": len(candidates),
            "bias_correction_candidate_count": len(candidates),
        },
        "calibration_candidates": candidates,
        "bias_correction_candidates": candidates,
        "daily_calibration_record": daily_calibration_record,
        "rolling_calibration_summary": rolling_calibration_summary,
        "parameter_change_state": parameter_change_state,
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_daily_weather_reviews": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
        "next_boundary": "daily_weather_reviews_table_apply_requires_confirmed_schema_and_writer",
    }
