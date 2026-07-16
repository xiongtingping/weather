"""Read-only release gate for the observation-T1 execution deadline.

It intentionally separates three pieces of evidence that are often confused:
the source contract itself, the running poller process having loaded that
source revision, and a forward archive decision carrying the T1 fields.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from sequential_bayes_execution_contract import (
    EXPECTED_ACTIVATION_MODE,
    EXPECTED_COMPOSER_SCHEMA,
    EXPECTED_CONTINUOUS_DISTRIBUTION_SCHEMA,
    EXPECTED_CONTINUOUS_TARGET_VARIABLE,
    EXPECTED_FORMULA,
    build_sequential_bayes_execution_contract,
)


SCHEMA_VERSION = "t1_execution_activation_audit_v1"
T1_REQUIRED_FIELDS = (
    "observation_trigger_at",
    "observation_trigger_time_source",
    "observation_to_order_hard_deadline_at",
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _probe_payload(*, first_actionable_seen_at: str) -> dict[str, Any]:
    buckets = [
        {"bucket": "36°C", "probability": 0.6},
        {"bucket": "37°C", "probability": 0.3},
        {"bucket": "38°C", "probability": 0.1},
    ]
    return {
        "fact_id": 1,
        "fact_as_of": "2026-07-12T00:00:00Z",
        "fact_snapshot_id": "fact_activation_probe",
        "posterior_run_id": "post_activation_probe",
        "posterior_composer_schema": EXPECTED_COMPOSER_SCHEMA,
        "posterior_activation_mode": EXPECTED_ACTIVATION_MODE,
        "source_to_posterior_timing": {
            "latest_source_first_actionable_seen_at": first_actionable_seen_at
        },
        "posterior_continuous_distribution": {
            "schema": EXPECTED_CONTINUOUS_DISTRIBUTION_SCHEMA,
            "source": EXPECTED_COMPOSER_SCHEMA,
            "target_variable": EXPECTED_CONTINUOUS_TARGET_VARIABLE,
            "grid_step_c": 0.1,
            "grid": [
                {"temp_c": 35.8, "mass": 0.6, "cdf": 0.6},
                {"temp_c": 35.9, "mass": 0.3, "cdf": 0.9},
                {"temp_c": 36.0, "mass": 0.1, "cdf": 1.0},
            ],
            "bucket_probabilities": buckets,
        },
        "sequential_state_filter": {"schema": "sequential_metar_state_filter_v1"},
        "remaining_warming_mixture": {
            "schema": "zero_inflated_remaining_warming_mixture_v1"
        },
        "observation_information_gate": {
            "schema": "continuous_observation_information_gate_v1"
        },
        "posterior_boundary": {
            "legacy_bucket_product_posterior_not_used": True,
            "live_no_shadow": True,
        },
        "hard_constraints": {"posterior": {"hard_constraint_violation_count": 0}},
        "posterior_ssot_fact": {
            "state": "available",
            "fact_snapshot_id": "fact_activation_probe",
            "posterior_run_id": "post_activation_probe",
            "target_bucket": "36°C",
            "target_probability": 0.6,
            "bucket_probabilities": buckets,
            "layer_chain": {"formula": EXPECTED_FORMULA},
            "boundaries": {
                "live_primary_composer": EXPECTED_COMPOSER_SCHEMA,
                "posterior_activation_mode": EXPECTED_ACTIVATION_MODE,
                "legacy_bucket_product_posterior_used": False,
                "shadow_posterior_emitted": False,
            },
        },
    }


def source_t1_contract_probe() -> dict[str, Any]:
    """Exercise the loaded source revision without touching an archive DB."""

    fresh = build_sequential_bayes_execution_contract(
        _probe_payload(first_actionable_seen_at="2026-07-12T00:00:00Z"),
        now="2026-07-12T00:00:02Z",
    )
    stale = build_sequential_bayes_execution_contract(
        _probe_payload(first_actionable_seen_at="2026-07-11T23:50:00Z"),
        now="2026-07-12T00:00:02Z",
    )
    fresh_has_fields = all(_text(fresh.get(field)) for field in T1_REQUIRED_FIELDS)
    stale_blocked = (
        "sequential_bayes_observation_to_order_hard_deadline_exceeded"
        in {str(value) for value in stale.get("blockers") or []}
    )
    return {
        "ok": bool(fresh.get("trade_eligible") is True and fresh_has_fields and stale_blocked),
        "fresh_contract_id": _text(fresh.get("contract_id")),
        "fresh_t1_fields": {field: fresh.get(field) for field in T1_REQUIRED_FIELDS},
        "stale_blockers": list(stale.get("blockers") or []),
        "checks": {
            "fresh_contract_trade_eligible": fresh.get("trade_eligible") is True,
            "fresh_contract_carries_t1_fields": fresh_has_fields,
            "stale_observation_fail_closed": stale_blocked,
        },
    }


def build_t1_execution_activation_audit(
    *,
    execution_records: Sequence[Mapping[str, Any]],
    process_rows: Sequence[Mapping[str, Any]],
    required_code_mtime_epoch: float,
) -> dict[str, Any]:
    """Combine source, process, and archived forward evidence without writes."""

    source_probe = source_t1_contract_probe()
    records = [_mapping(value) for value in execution_records]
    latest_record = records[0] if records else {}
    archived_gate = _mapping(latest_record.get("execution_contract_observation_t1_gate"))
    archived_gate_present = _text(archived_gate.get("state")) == "present"

    pollers = [
        _mapping(row)
        for row in process_rows
        if "hard_source_direct_poller.py" in _text(_mapping(row).get("command"))
        and " --loop" in _text(_mapping(row).get("command"))
    ]
    fresh_pollers = [
        row
        for row in pollers
        if float(row.get("started_at_epoch") or 0.0) + 1.0 >= float(required_code_mtime_epoch)
    ]
    checks = {
        "source_contract_t1_fail_closed": source_probe.get("ok") is True,
        "exactly_one_runtime_poller": len(pollers) == 1,
        "runtime_poller_loaded_after_t1_code": len(fresh_pollers) == 1,
        "forward_archive_contract_carries_t1_gate": archived_gate_present,
    }
    blockers = [name for name, ok in checks.items() if not ok]
    if not records:
        state = "awaiting_forward_trace"
    elif not checks["source_contract_t1_fail_closed"]:
        state = "source_contract_blocked"
    elif not checks["exactly_one_runtime_poller"] or not checks["runtime_poller_loaded_after_t1_code"]:
        state = "runtime_not_loaded"
    elif not archived_gate_present:
        state = "runtime_contract_not_observed"
    else:
        state = "activation_evidence_ready"
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": state,
        "read_only": True,
        "checks": checks,
        "blockers": blockers,
        "source_probe": source_probe,
        "runtime": {
            "required_code_mtime_epoch": float(required_code_mtime_epoch),
            "poller_count": len(pollers),
            "fresh_poller_count": len(fresh_pollers),
            "pollers": pollers,
        },
        "latest_forward_record": latest_record,
        "archived_t1_gate": archived_gate,
        "guards": {
            "writes_archive_db": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "T1_REQUIRED_FIELDS",
    "build_t1_execution_activation_audit",
    "source_t1_contract_probe",
]
