"""Execution eligibility contract for the Live sequential Bayes posterior.

The weather posterior remains owned by ``posterior_ssot_fact``.  This module
does not recompute probability; it only proves that a decision/order consumes
the expected Live composer snapshot and that the snapshot is inside the
signal-to-order deadline.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "sequential_bayes_execution_contract_v1"
EXPECTED_COMPOSER_SCHEMA = "sequential_remaining_warming_bayes_runtime_v31"
EXPECTED_ACTIVATION_MODE = "live_no_shadow"
EXPECTED_FORMULA = (
    "continuous_forecast_error_prior_then_sequential_state_zero_inflated_"
    "remaining_warming_mixture_then_bucket_integral"
)
# The live v3.1 composer exposes this exact, versioned continuous surface.
# Keep the consumer strict: accepting an older surface here would silently
# detach the execution gate from the sole permitted production model.
EXPECTED_CONTINUOUS_DISTRIBUTION_SCHEMA = (
    "sequential_remaining_warming_continuous_distribution_v31"
)
EXPECTED_CONTINUOUS_TARGET_VARIABLE = "settlement_daily_max_c"
DEFAULT_SLO_MS = 3000
DEFAULT_HARD_DEADLINE_MS = DEFAULT_SLO_MS
EPS = 1e-9


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str:
    return dt.isoformat(timespec="milliseconds") if dt is not None else ""


def _bounded_execution_deadlines(slo_ms: int, hard_deadline_ms: int) -> tuple[int, int]:
    """Clamp all runtime overrides to the immutable three-second objective."""

    slo_value = min(DEFAULT_SLO_MS, max(1, int(slo_ms)))
    hard_value = min(DEFAULT_HARD_DEADLINE_MS, slo_value, max(1, int(hard_deadline_ms)))
    return slo_value, hard_value


def _observation_trigger_time(item: Mapping[str, Any]) -> tuple[str, str, datetime | None]:
    """Return the immutable earliest actionable time for this posterior update.

    A posterior commit timestamp is not a valid proxy for the time at which a
    physical report became actionable: a queued materialization could make a
    very old report appear freshly actionable.  New resident ingress supplies
    ``first_actionable_seen_at``.  The legacy materializer only has its
    transport-completed time, which remains usable as a conservative fallback
    but is clearly labelled as such.
    """

    for timing_key in (
        "strong_observation_timing",
        "source_to_posterior_timing",
        "amsc_posterior_materialization_timing",
    ):
        timing = _mapping(item.get(timing_key))
        for field in (
            "latest_source_first_actionable_seen_at",
            "first_actionable_seen_at",
        ):
            value = str(timing.get(field) or "").strip()
            parsed = _parse_dt(value)
            if parsed is not None:
                return value, f"{timing_key}.{field}", parsed
        for field in (
            "latest_source_response_completed_at",
            "source_response_completed_at",
            "response_completed_at",
        ):
            value = str(timing.get(field) or "").strip()
            parsed = _parse_dt(value)
            if parsed is not None:
                return value, f"{timing_key}.{field}", parsed
    return "", "", None


def _contract_id(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "seqexec_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _bucket_rows(ssot: Mapping[str, Any]) -> list[tuple[str, float]]:
    rows = ssot.get("bucket_probabilities")
    out: list[tuple[str, float]] = []
    if not isinstance(rows, list):
        return out
    for raw in rows:
        row = _mapping(raw)
        bucket = str(row.get("bucket") or row.get("outcome") or "").strip()
        probability = _float(row.get("probability"))
        if bucket and probability is not None and probability >= 0.0:
            out.append((bucket, probability))
    return out


def _continuous_distribution_contract_status(
    value: Any,
    *,
    expected_bucket_rows: list[tuple[str, float]],
) -> dict[str, Any]:
    """Validate the v3.1 continuous surface without recomputing posterior.

    The execution layer never derives a new probability.  It does, however,
    prove that the submitted bucket surface is still the integral of one
    normalized, monotone continuous v3.1 posterior.
    """

    item = _mapping(value)
    schema = str(item.get("schema") or "")
    if schema != EXPECTED_CONTINUOUS_DISTRIBUTION_SCHEMA:
        return {"ok": False, "reason": "schema_mismatch", "schema": schema}
    if str(item.get("source") or "") != EXPECTED_COMPOSER_SCHEMA:
        return {"ok": False, "reason": "source_mismatch", "schema": schema}
    if str(item.get("target_variable") or "") != EXPECTED_CONTINUOUS_TARGET_VARIABLE:
        return {"ok": False, "reason": "target_variable_mismatch", "schema": schema}
    step = _float(item.get("grid_step_c"))
    grid = item.get("grid")
    if step is None or step <= 0.0 or not isinstance(grid, list) or len(grid) < 2:
        return {"ok": False, "reason": "grid_missing_or_invalid", "schema": schema}
    temperatures: list[float] = []
    masses: list[float] = []
    cdfs: list[float] = []
    for raw in grid:
        row = _mapping(raw)
        temp = _float(row.get("temp_c"))
        mass = _float(row.get("mass"))
        cdf = _float(row.get("cdf"))
        if temp is None or mass is None or cdf is None or mass < -EPS or cdf < -EPS or cdf > 1.0 + EPS:
            return {"ok": False, "reason": "grid_point_invalid", "schema": schema}
        temperatures.append(temp)
        masses.append(max(0.0, mass))
        cdfs.append(cdf)
    for previous, current in zip(temperatures, temperatures[1:]):
        if current <= previous or abs((current - previous) - step) > 1e-6:
            return {"ok": False, "reason": "grid_not_strict_uniform", "schema": schema}
    total_mass = sum(masses)
    if abs(total_mass - 1.0) > 1e-6:
        return {
            "ok": False,
            "reason": "grid_mass_not_normalized",
            "schema": schema,
            "mass_sum": round(total_mass, 12),
        }
    running = 0.0
    previous_cdf = -EPS
    for mass, cdf in zip(masses, cdfs):
        running += mass
        if cdf + EPS < previous_cdf or abs(cdf - running) > 1e-6:
            return {"ok": False, "reason": "grid_cdf_inconsistent", "schema": schema}
        previous_cdf = cdf
    if abs(cdfs[-1] - 1.0) > 1e-6:
        return {"ok": False, "reason": "grid_cdf_not_terminal_one", "schema": schema}
    continuous_rows = _bucket_rows(item)
    continuous_map = {bucket: probability for bucket, probability in continuous_rows}
    expected_map = {bucket: probability for bucket, probability in expected_bucket_rows}
    if set(continuous_map) != set(expected_map) or any(
        abs(continuous_map[bucket] - expected_map[bucket]) > 1e-8
        for bucket in expected_map
    ):
        return {"ok": False, "reason": "bucket_integral_mismatch", "schema": schema}
    return {
        "ok": True,
        "reason": "normalized_monotone_grid_and_bucket_integral_match",
        "schema": schema,
        "grid_point_count": len(grid),
        "mass_sum": round(total_mass, 12),
    }


def build_sequential_bayes_execution_contract(
    payload: Mapping[str, Any],
    *,
    expected_target_bucket: str = "",
    expected_target_probability: Any = None,
    now: Any = None,
    slo_ms: int = DEFAULT_SLO_MS,
    hard_deadline_ms: int = DEFAULT_HARD_DEADLINE_MS,
) -> dict[str, Any]:
    item = _mapping(payload)
    ssot = _mapping(item.get("posterior_ssot_fact"))
    boundaries = _mapping(ssot.get("boundaries"))
    layer_chain = _mapping(ssot.get("layer_chain"))
    posterior_boundary = _mapping(item.get("posterior_boundary"))
    composer = str(
        item.get("posterior_composer_schema")
        or ssot.get("posterior_composer_schema")
        or boundaries.get("live_primary_composer")
        or ""
    ).strip()
    activation = str(
        item.get("posterior_activation_mode")
        or ssot.get("posterior_activation_mode")
        or boundaries.get("posterior_activation_mode")
        or ""
    ).strip()
    formula = str(layer_chain.get("formula") or "").strip()
    target_bucket = str(ssot.get("target_bucket") or "").strip()
    target_probability = _float(ssot.get("target_probability"))
    rows = _bucket_rows(ssot)
    continuous_distribution = _continuous_distribution_contract_status(
        item.get("posterior_continuous_distribution"),
        expected_bucket_rows=rows,
    )
    probability_sum = sum(probability for _bucket, probability in rows)
    top_bucket = max(rows, key=lambda row: row[1])[0] if rows else ""
    top_probability = max((row[1] for row in rows), default=None)
    fact_snapshot_id = str(ssot.get("fact_snapshot_id") or item.get("fact_snapshot_id") or "").strip()
    posterior_run_id = str(ssot.get("posterior_run_id") or item.get("posterior_run_id") or "").strip()
    outer_fact_snapshot_id = str(item.get("fact_snapshot_id") or "").strip()
    outer_posterior_run_id = str(item.get("posterior_run_id") or "").strip()
    ssot_fact_snapshot_id = str(ssot.get("fact_snapshot_id") or "").strip()
    ssot_posterior_run_id = str(ssot.get("posterior_run_id") or "").strip()
    fact_id = int(_float(item.get("fact_id")) or 0)
    fact_as_of = str(item.get("fact_as_of") or ssot.get("posterior_as_of") or "").strip()
    observation_trigger_at, observation_trigger_time_source, observation_trigger_dt = _observation_trigger_time(item)
    blockers: list[str] = []
    if str(ssot.get("state") or "").strip().lower() != "available":
        blockers.append("sequential_bayes_ssot_not_available")
    if composer != EXPECTED_COMPOSER_SCHEMA:
        blockers.append("sequential_bayes_composer_schema_mismatch")
    if activation != EXPECTED_ACTIVATION_MODE:
        blockers.append("sequential_bayes_activation_not_live_no_shadow")
    if formula != EXPECTED_FORMULA:
        blockers.append("sequential_bayes_formula_mismatch")
    if not rows or abs(probability_sum - 1.0) > 1e-6:
        blockers.append("sequential_bayes_ssot_distribution_not_normalized")
    if not target_bucket or target_bucket != top_bucket:
        blockers.append("sequential_bayes_ssot_target_not_distribution_top")
    if target_probability is None or top_probability is None or abs(target_probability - top_probability) > EPS:
        blockers.append("sequential_bayes_ssot_target_probability_mismatch")
    if expected_target_bucket and str(expected_target_bucket).strip() != target_bucket:
        blockers.append("sequential_bayes_expected_target_bucket_mismatch")
    expected_probability = _float(expected_target_probability)
    if expected_probability is not None and (
        target_probability is None or abs(expected_probability - target_probability) > EPS
    ):
        blockers.append("sequential_bayes_expected_target_probability_mismatch")
    if not fact_snapshot_id:
        blockers.append("sequential_bayes_fact_snapshot_id_missing")
    if not posterior_run_id:
        blockers.append("sequential_bayes_posterior_run_id_missing")
    if outer_fact_snapshot_id and ssot_fact_snapshot_id and outer_fact_snapshot_id != ssot_fact_snapshot_id:
        blockers.append("sequential_bayes_fact_snapshot_lineage_mismatch")
    if outer_posterior_run_id and ssot_posterior_run_id and outer_posterior_run_id != ssot_posterior_run_id:
        blockers.append("sequential_bayes_posterior_run_lineage_mismatch")
    if boundaries.get("legacy_bucket_product_posterior_used") is not False:
        blockers.append("sequential_bayes_legacy_bucket_product_boundary_missing")
    if boundaries.get("shadow_posterior_emitted") is not False:
        blockers.append("sequential_bayes_shadow_posterior_boundary_missing")
    if posterior_boundary.get("legacy_bucket_product_posterior_not_used") is not True:
        blockers.append("sequential_bayes_live_pipeline_boundary_missing")
    if continuous_distribution.get("reason") == "schema_mismatch":
        blockers.append("sequential_bayes_posterior_continuous_distribution_contract_missing")
    elif continuous_distribution.get("ok") is not True:
        blockers.append("sequential_bayes_posterior_continuous_distribution_invalid")
    for field, schema in (
        ("sequential_state_filter", "sequential_metar_state_filter_v1"),
        ("remaining_warming_mixture", "zero_inflated_remaining_warming_mixture_v1"),
        ("observation_information_gate", "continuous_observation_information_gate_v1"),
    ):
        if str(_mapping(item.get(field)).get("schema") or "") != schema:
            blockers.append(f"sequential_bayes_{field}_contract_missing")
    hard_violation = _float(
        _mapping(_mapping(item.get("hard_constraints")).get("posterior")).get(
            "hard_constraint_violation_count"
        )
    )
    if hard_violation not in (None, 0.0):
        blockers.append("sequential_bayes_hard_constraint_violation")

    now_dt = _parse_dt(now) or datetime.now(timezone.utc)
    signal_dt = _parse_dt(fact_as_of)
    elapsed_ms = (
        max(0.0, (now_dt - signal_dt).total_seconds() * 1000.0)
        if signal_dt is not None
        else None
    )
    slo_value, hard_value = _bounded_execution_deadlines(slo_ms, hard_deadline_ms)
    if signal_dt is None:
        blockers.append("sequential_bayes_signal_time_missing")
    elif elapsed_ms is not None and elapsed_ms > hard_value:
        blockers.append("sequential_bayes_signal_to_order_hard_deadline_exceeded")
    observation_elapsed_ms = (
        max(0.0, (now_dt - observation_trigger_dt).total_seconds() * 1000.0)
        if observation_trigger_dt is not None
        else None
    )
    if observation_trigger_dt is None:
        blockers.append("sequential_bayes_observation_trigger_time_missing")
    elif signal_dt is not None and observation_trigger_dt > signal_dt:
        blockers.append("sequential_bayes_observation_trigger_after_posterior")
    elif observation_elapsed_ms is not None and observation_elapsed_ms > hard_value:
        blockers.append("sequential_bayes_observation_to_order_hard_deadline_exceeded")
    identity = {
        "composer_schema": composer,
        "activation_mode": activation,
        "formula": formula,
        "fact_id": fact_id,
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": posterior_run_id,
        "signal_at": fact_as_of,
        "observation_trigger_at": observation_trigger_at,
        "observation_trigger_time_source": observation_trigger_time_source,
        "target_bucket": target_bucket,
        "target_probability": target_probability,
        "lineage_identity_consistent": not any(
            blocker in {
                "sequential_bayes_fact_snapshot_lineage_mismatch",
                "sequential_bayes_posterior_run_lineage_mismatch",
            }
            for blocker in blockers
        ),
    }
    return {
        "schema": SCHEMA_VERSION,
        "contract_id": _contract_id(identity),
        "trade_eligible": not blockers,
        "blockers": blockers,
        **identity,
        "distribution_probability_sum": round(probability_sum, 12),
        "continuous_distribution_contract": continuous_distribution,
        "signal_to_order_slo_ms": slo_value,
        "signal_to_order_hard_deadline_ms": hard_value,
        "signal_to_order_elapsed_ms": round(elapsed_ms, 3) if elapsed_ms is not None else None,
        "signal_to_order_slo_met_at_check": bool(elapsed_ms is not None and elapsed_ms <= slo_value),
        "signal_to_order_hard_deadline_met_at_check": bool(
            elapsed_ms is not None and elapsed_ms <= hard_value
        ),
        "signal_to_order_slo_deadline_at": _iso(signal_dt + timedelta(milliseconds=slo_value)) if signal_dt else "",
        "signal_to_order_hard_deadline_at": _iso(signal_dt + timedelta(milliseconds=hard_value)) if signal_dt else "",
        "observation_to_order_slo_ms": slo_value,
        "observation_to_order_hard_deadline_ms": hard_value,
        "observation_to_order_elapsed_ms": round(observation_elapsed_ms, 3) if observation_elapsed_ms is not None else None,
        "observation_to_order_slo_met_at_check": bool(
            observation_elapsed_ms is not None and observation_elapsed_ms <= slo_value
        ),
        "observation_to_order_hard_deadline_met_at_check": bool(
            observation_elapsed_ms is not None and observation_elapsed_ms <= hard_value
        ),
        "observation_to_order_slo_deadline_at": _iso(
            observation_trigger_dt + timedelta(milliseconds=slo_value)
        ) if observation_trigger_dt else "",
        "observation_to_order_hard_deadline_at": _iso(
            observation_trigger_dt + timedelta(milliseconds=hard_value)
        ) if observation_trigger_dt else "",
        "probability_recomputed": False,
        "decision_source": "posterior_ssot_fact",
    }


def validate_sequential_bayes_execution_context(
    details: Mapping[str, Any],
    *,
    now: Any = None,
) -> dict[str, Any]:
    contract = _mapping(details.get("sequential_bayes_execution_contract"))
    blockers: list[str] = []
    if contract.get("schema") != SCHEMA_VERSION:
        blockers.append("sequential_bayes_execution_contract_missing")
    if contract.get("composer_schema") != EXPECTED_COMPOSER_SCHEMA:
        blockers.append("sequential_bayes_execution_composer_mismatch")
    if contract.get("activation_mode") != EXPECTED_ACTIVATION_MODE:
        blockers.append("sequential_bayes_execution_activation_mismatch")
    if contract.get("formula") != EXPECTED_FORMULA:
        blockers.append("sequential_bayes_execution_formula_mismatch")
    if contract.get("trade_eligible") is not True:
        blockers.extend(str(value) for value in contract.get("blockers") or [] if str(value))
    now_dt = _parse_dt(now) or datetime.now(timezone.utc)
    hard_deadline = _parse_dt(contract.get("signal_to_order_hard_deadline_at"))
    slo_deadline = _parse_dt(contract.get("signal_to_order_slo_deadline_at"))
    if hard_deadline is None:
        blockers.append("sequential_bayes_execution_hard_deadline_missing")
    elif now_dt > hard_deadline:
        blockers.append("sequential_bayes_signal_to_order_hard_deadline_exceeded")
    observation_hard_deadline = _parse_dt(contract.get("observation_to_order_hard_deadline_at"))
    observation_slo_deadline = _parse_dt(contract.get("observation_to_order_slo_deadline_at"))
    if contract.get("transition_timer_reset") is not True:
        if not str(contract.get("observation_trigger_at") or "").strip() or observation_hard_deadline is None:
            blockers.append("sequential_bayes_execution_observation_deadline_missing")
        elif now_dt > observation_hard_deadline:
            blockers.append("sequential_bayes_observation_to_order_hard_deadline_exceeded")
    return {
        "schema": "sequential_bayes_execution_context_validation_v1",
        "ok": not blockers,
        "blockers": sorted(set(blockers)),
        "contract_id": str(contract.get("contract_id") or ""),
        "composer_schema": str(contract.get("composer_schema") or ""),
        "activation_mode": str(contract.get("activation_mode") or ""),
        "signal_at": str(contract.get("signal_at") or ""),
        "observation_trigger_at": str(contract.get("observation_trigger_at") or ""),
        "checked_at": _iso(now_dt),
        "slo_met": bool(slo_deadline is not None and now_dt <= slo_deadline),
        "hard_deadline_met": bool(hard_deadline is not None and now_dt <= hard_deadline),
        "observation_slo_met": bool(
            observation_slo_deadline is not None and now_dt <= observation_slo_deadline
        ),
        "observation_hard_deadline_met": bool(
            observation_hard_deadline is not None and now_dt <= observation_hard_deadline
        ),
        "signal_to_order_elapsed_ms": (
            round(max(0.0, (now_dt - (_parse_dt(contract.get("signal_at")) or now_dt)).total_seconds() * 1000.0), 3)
        ),
    }


def build_target_realization_transition_execution_contract(
    origin_contract: Mapping[str, Any],
    *,
    transition_signal_at: Any,
    now: Any = None,
    transition_kind: str = "sell_fill_to_buy_refresh",
    slo_ms: int = DEFAULT_SLO_MS,
    hard_deadline_ms: int = DEFAULT_HARD_DEADLINE_MS,
) -> dict[str, Any]:
    origin = _mapping(origin_contract)
    signal_dt = _parse_dt(transition_signal_at)
    now_dt = _parse_dt(now) or datetime.now(timezone.utc)
    slo_value, hard_value = _bounded_execution_deadlines(slo_ms, hard_deadline_ms)
    blockers: list[str] = []
    if origin.get("schema") != SCHEMA_VERSION:
        blockers.append("sequential_bayes_transition_origin_contract_missing")
    if origin.get("composer_schema") != EXPECTED_COMPOSER_SCHEMA:
        blockers.append("sequential_bayes_transition_composer_mismatch")
    if origin.get("activation_mode") != EXPECTED_ACTIVATION_MODE:
        blockers.append("sequential_bayes_transition_activation_mismatch")
    if origin.get("formula") != EXPECTED_FORMULA:
        blockers.append("sequential_bayes_transition_formula_mismatch")
    if signal_dt is None:
        blockers.append("target_realization_transition_signal_time_missing")
    elapsed_ms = (
        max(0.0, (now_dt - signal_dt).total_seconds() * 1000.0)
        if signal_dt is not None
        else None
    )
    if elapsed_ms is not None and elapsed_ms > hard_value:
        blockers.append("target_realization_fill_to_buy_hard_deadline_exceeded")
    identity = {
        "composer_schema": str(origin.get("composer_schema") or ""),
        "activation_mode": str(origin.get("activation_mode") or ""),
        "formula": str(origin.get("formula") or ""),
        "fact_id": int(_float(origin.get("fact_id")) or 0),
        "fact_snapshot_id": str(origin.get("fact_snapshot_id") or ""),
        "posterior_run_id": str(origin.get("posterior_run_id") or ""),
        "signal_at": _iso(signal_dt),
        "target_bucket": str(origin.get("target_bucket") or ""),
        "target_probability": _float(origin.get("target_probability")),
        "transition_kind": str(transition_kind or "sell_fill_to_buy_refresh"),
        "origin_contract_id": str(origin.get("contract_id") or ""),
    }
    return {
        "schema": SCHEMA_VERSION,
        "contract_id": _contract_id(identity),
        "trade_eligible": not blockers,
        "blockers": blockers,
        **identity,
        "distribution_probability_sum": origin.get("distribution_probability_sum"),
        "signal_to_order_slo_ms": slo_value,
        "signal_to_order_hard_deadline_ms": hard_value,
        "signal_to_order_elapsed_ms": round(elapsed_ms, 3) if elapsed_ms is not None else None,
        "signal_to_order_slo_met_at_check": bool(elapsed_ms is not None and elapsed_ms <= slo_value),
        "signal_to_order_hard_deadline_met_at_check": bool(
            elapsed_ms is not None and elapsed_ms <= hard_value
        ),
        "signal_to_order_slo_deadline_at": _iso(signal_dt + timedelta(milliseconds=slo_value)) if signal_dt else "",
        "signal_to_order_hard_deadline_at": _iso(signal_dt + timedelta(milliseconds=hard_value)) if signal_dt else "",
        "probability_recomputed": False,
        "decision_source": "posterior_ssot_fact_refreshed_after_sell_fill",
        "transition_timer_reset": True,
    }


__all__ = [
    "SCHEMA_VERSION",
    "EXPECTED_COMPOSER_SCHEMA",
    "EXPECTED_ACTIVATION_MODE",
    "EXPECTED_FORMULA",
    "EXPECTED_CONTINUOUS_DISTRIBUTION_SCHEMA",
    "EXPECTED_CONTINUOUS_TARGET_VARIABLE",
    "DEFAULT_SLO_MS",
    "DEFAULT_HARD_DEADLINE_MS",
    "build_sequential_bayes_execution_contract",
    "build_target_realization_transition_execution_contract",
    "validate_sequential_bayes_execution_context",
]
