"""Composer-owned peak-time state for execution timing, never a second X model."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "peak_timing_state_l3_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def build_peak_timing_state(
    *,
    forecast_peak_timing_prior: Mapping[str, Any] | None,
    strong_observation_update: Mapping[str, Any],
    post_event_trajectory: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
) -> dict[str, Any]:
    """Update forecast timing context with the current strong posterior marginal.

    The probability that the daily high has already formed is read from the
    same strong update's Termination Risk. The remaining mass is distributed
    only across forecast-supported future peak slots. This state does not
    change p(X) and cannot synthesize a future clock when timing is absent.
    """

    prior = _mapping(forecast_peak_timing_prior)
    update = _mapping(strong_observation_update)
    trajectory = _mapping(post_event_trajectory)
    event = _mapping(canonical_observation_event)
    event_id = _text(event.get("canonical_observation_event_id"))
    update_id = _text(update.get("strong_observation_update_id"))
    forecast_snapshot_id = _text(update.get("forecast_evidence_snapshot_id"))
    blockers: list[str] = []
    if update.get("ok") is not True or not update_id or not event_id:
        blockers.append("peak_timing_strong_update_invalid")
    if _text(update.get("canonical_observation_event_id")) != event_id:
        blockers.append("peak_timing_event_update_mismatch")
    if trajectory.get("ok") is not True or _text(trajectory.get("temporal_role")) != "post_event":
        blockers.append("peak_timing_post_event_trajectory_invalid")
    if _text(trajectory.get("latest_canonical_observation_event_id")) != event_id:
        blockers.append("peak_timing_trajectory_event_mismatch")
    if prior and _text(prior.get("forecast_evidence_snapshot_id")) != forecast_snapshot_id:
        blockers.append("peak_timing_prior_snapshot_mismatch")
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "does_not_update_physical_x": True,
            "read_only": True,
        }

    latest_at = _time(trajectory.get("latest_valid_time"))
    peak_evidence = _mapping(trajectory.get("path_state_evidence"))
    observed_peak_at = _time(peak_evidence.get("observed_peak_valid_time"))
    termination = _mapping(update.get("termination_risk"))
    peak_already = _finite(termination.get("probability_no_material_new_daily_high"))
    if latest_at is None or observed_peak_at is None or peak_already is None or not 0.0 <= peak_already <= 1.0:
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "na",
            "na_reason": "peak_timing_strong_trajectory_or_termination_marginal_missing",
            "canonical_observation_event_id": event_id,
            "strong_observation_update_id": update_id,
            "forecast_evidence_snapshot_id": forecast_snapshot_id,
            "entry_peak_at": "",
            "does_not_update_physical_x": True,
            "read_only": True,
        }

    future_rows: list[tuple[datetime, float]] = []
    rows = prior.get("distribution")
    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
        for raw in rows:
            row = _mapping(raw)
            peak_at = _time(row.get("peak_at"))
            probability = _finite(row.get("probability"))
            if peak_at is not None and probability is not None and probability > 0.0 and peak_at > latest_at:
                future_rows.append((peak_at, probability))
    future_weight = sum(weight for _slot, weight in future_rows)
    future_mass = max(0.0, 1.0 - peak_already)
    distribution: list[dict[str, Any]] = [{
        "time_state": "observed_peak_to_date",
        "peak_at": observed_peak_at.isoformat().replace("+00:00", "Z"),
        "probability": peak_already,
    }]
    if future_weight > 0.0:
        distribution.extend(
            {
                "time_state": "future_forecast_supported_slot",
                "peak_at": slot.isoformat().replace("+00:00", "Z"),
                "probability": future_mass * weight / future_weight,
            }
            for slot, weight in sorted(future_rows)
        )
    elif future_mass > 0.0:
        # Do not silently transfer unresolved future mass to the observed
        # peak, and do not fabricate a clock after every forecast-supported
        # slot has expired.  The unresolved row keeps the timing marginal
        # normalized while remaining ineligible for second-YES admission.
        distribution.append({
            "time_state": "future_peak_time_unresolved",
            "peak_at": "",
            "probability": future_mass,
        })
    mode = max(distribution, key=lambda row: (float(row["probability"]), _text(row.get("peak_at"))))
    entry_peak_at = _text(mode.get("peak_at")) if _text(mode.get("time_state")) == "future_forecast_supported_slot" else ""
    if not entry_peak_at:
        entry_na_reason = (
            "forecast_supported_future_peak_slot_missing"
            if future_mass > 0.0 and future_weight <= 0.0
            else "daily_peak_already_is_timing_mode"
        )
    else:
        entry_na_reason = ""
    core = {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "canonical_observation_event_id": event_id,
        "strong_observation_update_id": update_id,
        "forecast_evidence_snapshot_id": forecast_snapshot_id,
        "latest_observation_at": latest_at.isoformat().replace("+00:00", "Z"),
        "observed_peak_at": observed_peak_at.isoformat().replace("+00:00", "Z"),
        "peak_already_probability": peak_already,
        "future_peak_probability": future_mass,
        "distribution": distribution,
        "mode_time_state": _text(mode.get("time_state")),
        "mode_probability": float(mode["probability"]),
        "entry_peak_at": entry_peak_at,
        "entry_peak_at_na_reason": entry_na_reason,
        "timing_update_formula": "termination_risk_mass_on_observed_peak_plus_remaining_mass_on_forecast_supported_future_slots",
        "does_not_update_physical_x": True,
        "market_or_trading_input_consumed": False,
        "read_only": True,
    }
    return {**core, "peak_timing_state_id": f"peaktiming_{_sha(core)[:24]}"}


def validate_peak_timing_state_contract(
    value: Any, *, expected_event_id: str, expected_update_id: str
) -> tuple[dict[str, Any], list[str]]:
    """Validate the public timing contract and its exact event/update lineage."""

    timing = _mapping(value)
    issues: list[str] = []
    state = _text(timing.get("state"))
    if _text(timing.get("schema")) != SCHEMA_VERSION:
        issues.append("public_peak_timing_schema_invalid")
    if timing.get("ok") is not True or state not in {"available", "na"}:
        issues.append("public_peak_timing_state_invalid")
    if _text(timing.get("canonical_observation_event_id")) != _text(expected_event_id):
        issues.append("public_peak_timing_event_binding_mismatch")
    if _text(timing.get("strong_observation_update_id")) != _text(expected_update_id):
        issues.append("public_peak_timing_update_binding_mismatch")
    if timing.get("does_not_update_physical_x") is not True:
        issues.append("public_peak_timing_physical_x_boundary_missing")
    entry_peak_at = _text(timing.get("entry_peak_at"))
    if entry_peak_at and _time(entry_peak_at) is None:
        issues.append("public_peak_timing_entry_peak_at_invalid")
    if state == "na":
        if entry_peak_at:
            issues.append("public_peak_timing_na_has_entry_peak")
        if not _text(timing.get("na_reason")):
            issues.append("public_peak_timing_na_reason_missing")
    if state == "available":
        if not _text(timing.get("peak_timing_state_id")):
            issues.append("public_peak_timing_identity_missing")
        peak_already = _finite(timing.get("peak_already_probability"))
        future = _finite(timing.get("future_peak_probability"))
        if (
            peak_already is None
            or future is None
            or not 0.0 <= peak_already <= 1.0
            or not 0.0 <= future <= 1.0
        ):
            issues.append("public_peak_timing_probability_invalid")
        elif abs(peak_already + future - 1.0) > 1e-8:
            issues.append("public_peak_timing_probability_not_normalized")
        distribution = timing.get("distribution")
        rows = (
            list(distribution)
            if isinstance(distribution, Sequence)
            and not isinstance(distribution, (str, bytes, bytearray))
            else []
        )
        probabilities = [_finite(_mapping(row).get("probability")) for row in rows]
        if (
            not rows
            or any(probability is None or probability < 0.0 for probability in probabilities)
            or abs(sum(probability or 0.0 for probability in probabilities) - 1.0) > 1e-8
        ):
            issues.append("public_peak_timing_distribution_not_normalized")
        if entry_peak_at:
            supported = any(
                _text(_mapping(row).get("time_state"))
                == "future_forecast_supported_slot"
                and _text(_mapping(row).get("peak_at")) == entry_peak_at
                for row in rows
            )
            if not supported:
                issues.append("public_peak_timing_entry_not_forecast_supported")
        elif not _text(timing.get("entry_peak_at_na_reason")):
            issues.append("public_peak_timing_entry_na_reason_missing")
    return {
        "state": state,
        "peak_timing_state_id": _text(timing.get("peak_timing_state_id")),
        "entry_peak_at": entry_peak_at,
        "entry_peak_at_na_reason": _text(
            timing.get("entry_peak_at_na_reason") or timing.get("na_reason")
        ),
        "peak_already_probability": timing.get("peak_already_probability"),
        "future_peak_probability": timing.get("future_peak_probability"),
        "does_not_update_physical_x": timing.get("does_not_update_physical_x") is True,
    }, sorted(set(issues))


__all__ = [
    "SCHEMA_VERSION",
    "build_peak_timing_state",
    "validate_peak_timing_state_contract",
]
