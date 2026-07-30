"""Pure main-YES position-cycle controller for Target Realization v6."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "dynamic_three_leg_v6_main_yes_position_cycle_v1"
STAGE_RATIOS = (0.0, 0.25, 0.50, 0.75, 1.0)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _identity(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "maincycle_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def stage_number_for_ratio(ratio: float) -> int:
    for index, value in enumerate(STAGE_RATIOS):
        if abs(float(ratio) - value) <= 1e-9:
            return index
    raise ValueError("v6_main_yes_stage_ratio_invalid")


def advance_main_yes_position_cycle(
    *,
    posterior_run_id: str,
    bucket: str,
    mode: str,
    stage_target_ratio: float,
    canonical_observation_event_id: str,
    prior_cycle: Mapping[str, Any] | None,
    coverage_reconciled: bool,
) -> dict[str, Any]:
    """Advance a cycle without allowing repeated stage sells or fake confirmations."""

    posterior = _text(posterior_run_id)
    bucket = _text(bucket)
    event_id = _text(canonical_observation_event_id)
    target = _finite(stage_target_ratio)
    blockers: list[str] = []
    if not posterior or not bucket or not event_id:
        blockers.append("v6_main_yes_cycle_identity_missing")
    if mode not in {"normal", "upside_risk", "hard_locked", "invalidated"}:
        blockers.append("v6_main_yes_cycle_mode_invalid")
    try:
        target_stage = stage_number_for_ratio(float(target)) if target is not None else -1
    except ValueError:
        target_stage = -1
        blockers.append("v6_main_yes_cycle_stage_target_invalid")
    prior = dict(prior_cycle or {})
    if prior and (
        prior.get("schema") != SCHEMA_VERSION
        or not _text(prior.get("main_yes_position_cycle_id"))
        or not _text(prior.get("bucket"))
    ):
        blockers.append("v6_main_yes_prior_cycle_invalid")
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(blockers))}

    same_bucket = bool(prior and _text(prior.get("bucket")) == bucket)
    prior_stage = int(prior.get("sell_stage_high_watermark") or 0) if same_bucket else 0
    prior_events = [
        _text(value) for value in prior.get("low_risk_confirmation_event_ids") or [] if _text(value)
    ] if same_bucket else []
    if mode == "normal" and event_id not in prior_events:
        prior_events.append(event_id)
    elif mode != "normal":
        prior_events = []
    prior_events = prior_events[-2:]
    reentry_ready = bool(mode == "hard_locked" or len(prior_events) >= 2)
    needs_new_cycle = bool(
        not prior
        or not same_bucket
        or (
            same_bucket
            and int(prior.get("sell_stage_high_watermark") or 0) > 0
            and reentry_ready
            and coverage_reconciled
        )
    )
    if needs_new_cycle:
        generation = (int(prior.get("cycle_generation") or 0) + 1) if same_bucket else 1
        core = {
            "posterior_run_id": posterior,
            "bucket": bucket,
            "cycle_generation": generation,
            "opened_by_canonical_event_id": event_id,
        }
        cycle_id = _identity(core)
        prior_stage = 0
        prior_events = [] if mode != "normal" else [event_id]
    else:
        generation = int(prior.get("cycle_generation") or 1)
        cycle_id = _text(prior.get("main_yes_position_cycle_id"))

    requested_stage = max(prior_stage, target_stage) if mode == "upside_risk" else prior_stage
    incremental_stage = max(0, requested_stage - prior_stage)
    state = "invalidated" if mode == "invalidated" else "active"
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": state,
        "main_yes_position_cycle_id": cycle_id,
        "cycle_generation": generation,
        "posterior_run_id": posterior,
        "bucket": bucket,
        "main_yes_mode": mode,
        "sell_stage_high_watermark": prior_stage,
        "previous_sell_stage_high_watermark": prior_stage,
        "requested_sell_stage": requested_stage,
        "incremental_stage_count": incremental_stage,
        "stage_target_ratio": STAGE_RATIOS[requested_stage],
        "low_risk_confirmation_event_ids": prior_events,
        "reentry_confirmation_count": len(prior_events),
        "reentry_ready": reentry_ready,
        "new_cycle_created": needs_new_cycle,
        "coverage_reconciled": bool(coverage_reconciled),
        "canonical_observation_event_id": event_id,
        "boundary": {
            "watermark_monotonic_within_cycle": True,
            "watermark_advances_only_on_reconciled_stage_fill": True,
            "only_distinct_canonical_events_confirm_reentry": True,
            "market_refresh_and_continuation_do_not_confirm": True,
            "coverage_reconcile_required_before_new_cycle": True,
        },
    }


__all__ = ["SCHEMA_VERSION", "STAGE_RATIOS", "advance_main_yes_position_cycle", "stage_number_for_ratio"]
