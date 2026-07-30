"""Pure Target Realization v6 composer.

This is the sole successor policy composer.  It consumes one committed market
posterior plus frozen execution snapshots and produces a complete desired
portfolio/exit state.  It never reads raw weather, writes SQLite, or orders.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from dynamic_three_leg_v6_capital_allocator import (
    CAPITAL_POLICY_ID,
    allocate_v6_incremental_capital,
)
from dynamic_three_leg_v6_position_cycle_runtime import (
    advance_main_yes_position_cycle,
    stage_number_for_ratio,
)


SCHEMA_VERSION = "target_realization_v6_composer_v1"
LEG_CAPS = {"main_yes": 20.0, "second_yes": 4.99, "trajectory_no": 100.0, "hard_exclusion_no": 200.0}
RISK_BANDS = ((0.50, 1.0), (0.35, 0.75), (0.20, 0.50), (0.10, 0.25), (0.0, 0.0))
TRAJECTORY_NO_BANDS = ((0.98, 100.0, "q_no_98"), (0.95, 75.0, "q_no_95"), (0.90, 50.0, "q_no_90"), (0.80, 25.0, "q_no_80"), (0.70, 10.0, "q_no_70"))
TRAJECTORY_NO_DOWNSHIFT_HYSTERESIS = 0.01


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _identity(prefix: str, value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _hard_certificate_valid(row: Mapping[str, Any], *, posterior_run_id: str, realization_state_id: str) -> bool:
    certificate = row.get("hard_exclusion_certificate")
    cert = certificate if isinstance(certificate, Mapping) else {}
    return bool(
        cert.get("state") == "valid"
        and cert.get("full_support_exclusion_verified") is True
        and _text(cert.get("bucket")) == _text(row.get("bucket"))
        and _text(cert.get("posterior_run_id")) == posterior_run_id
        and _text(cert.get("realization_state_id")) == realization_state_id
        and _text(cert.get("canonical_observation_event_id"))
        and _text(cert.get("bridge_artifact_sha256"))
        and _text(cert.get("target_contract_sha256"))
        and _text(cert.get("authority_sha256"))
        and _text(cert.get("certificate_sha256"))
    )


def _normalise_posterior(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    out: list[dict[str, Any]] = []
    issues: list[str] = []
    seen_bucket: set[str] = set()
    seen_ordinal: set[int] = set()
    for raw in rows:
        bucket = _text(raw.get("bucket"))
        probability = _finite(raw.get("probability"))
        try:
            ordinal = int(raw.get("ordinal"))
        except (TypeError, ValueError):
            ordinal = -10**9
        if not bucket or bucket in seen_bucket or ordinal in seen_ordinal:
            issues.append("v6_posterior_bucket_identity_invalid")
            continue
        if probability is None or not 0 <= probability <= 1:
            issues.append("v6_posterior_probability_invalid")
            continue
        seen_bucket.add(bucket)
        seen_ordinal.add(ordinal)
        out.append({**dict(raw), "bucket": bucket, "probability": probability, "ordinal": ordinal})
    if not out or abs(sum(row["probability"] for row in out) - 1.0) > 1e-6:
        issues.append("v6_posterior_not_normalized")
    return out, sorted(set(issues))


def _risk_target_ratio(p_above_top: float) -> float:
    return next(ratio for lower, ratio in RISK_BANDS if p_above_top >= lower)


def _time_cap_ratio(*, decision_at: datetime, entry_peak_at: datetime, future_risk: bool) -> tuple[float, str]:
    minutes = (entry_peak_at - decision_at).total_seconds() / 60.0
    if minutes > 60:
        return 0.25, "more_than_60m_before_peak"
    if minutes > 30:
        return 0.50, "30_to_60m_before_peak"
    if minutes > 0:
        return 0.75, "final_30m_before_peak"
    return (1.0, "at_or_after_peak_with_future_risk") if future_risk else (0.0, "at_or_after_peak_without_future_risk")


def _trajectory_band(q_no: float, *, prior_amount: float | None = None) -> tuple[float, str] | None:
    for lower, amount, band in TRAJECTORY_NO_BANDS:
        if q_no >= lower:
            return amount, band
    if prior_amount is not None:
        for lower, amount, band in TRAJECTORY_NO_BANDS:
            if abs(prior_amount - amount) <= 1e-9 and q_no >= lower - TRAJECTORY_NO_DOWNSHIFT_HYSTERESIS:
                return amount, f"{band}_hysteresis_hold"
    return None


def _exposure(
    snapshot: Mapping[str, Any], *, strategy_leg: str, bucket: str, side: str,
) -> float:
    total = 0.0
    for raw in snapshot.get("exposures") or []:
        if not isinstance(raw, Mapping):
            continue
        if (
            _text(raw.get("strategy_leg")) == strategy_leg
            and _text(raw.get("bucket")) == bucket
            and _text(raw.get("side")).upper() == side
        ):
            value = _finite(raw.get("notional_usdc"))
            if value is not None and value > 0:
                total += value
    return total


def _side_buckets(snapshot: Mapping[str, Any], *, side: str) -> set[str]:
    return {
        _text(raw.get("bucket"))
        for raw in snapshot.get("exposures") or []
        if isinstance(raw, Mapping)
        and _text(raw.get("side")).upper() == side
        and (_finite(raw.get("position_size")) or 0.0) > 1e-9
        and _text(raw.get("bucket"))
    }


def _no_bucket_exposure(snapshot: Mapping[str, Any], *, bucket: str) -> float:
    total = 0.0
    for raw in snapshot.get("exposures") or []:
        if not isinstance(raw, Mapping):
            continue
        if _text(raw.get("bucket")) == bucket and _text(raw.get("side")).upper() == "NO":
            value = _finite(raw.get("notional_usdc"))
            if value is not None and value > 0:
                total += value
    return total


def _main_slot_exposure(snapshot: Mapping[str, Any], *, current_bucket: str) -> float:
    total = 0.0
    for raw in snapshot.get("exposures") or []:
        if not isinstance(raw, Mapping) or _text(raw.get("side")).upper() != "YES":
            continue
        if _text(raw.get("strategy_leg")) == "main_yes" or _text(raw.get("bucket")) == current_bucket:
            value = _finite(raw.get("notional_usdc"))
            if value is not None and value > 0:
                total += value
    return total


def _second_slot_exposure(snapshot: Mapping[str, Any]) -> float:
    total = 0.0
    for raw in snapshot.get("exposures") or []:
        if not isinstance(raw, Mapping):
            continue
        if _text(raw.get("strategy_leg")) == "second_yes" and _text(raw.get("side")).upper() == "YES":
            value = _finite(raw.get("notional_usdc"))
            if value is not None and value > 0:
                total += value
    return total


def build_target_realization_v6_plan(
    *,
    posterior_run_id: str,
    realization_state_id: str,
    canonical_observation_event_id: str,
    posterior_buckets: Sequence[Mapping[str, Any]],
    peak_timing_state: Mapping[str, Any],
    future_warming_or_rebound_risk: bool,
    second_yes_entry_allowed: bool,
    position_exposure_snapshot: Mapping[str, Any],
    coverage_snapshot: Mapping[str, Any],
    prior_main_yes_cycle: Mapping[str, Any] | None,
    available_capital_usdc: float,
    capital_snapshot_id: str,
    risk_snapshot_id: str,
    capital_epoch_id: str,
    strategy_artifact_sha256: str,
    decision_as_of: str,
    prior_no_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose all v6 roles, cycle state, exits, and proportional BUY targets."""

    posterior_id = _text(posterior_run_id)
    realization_id = _text(realization_state_id)
    event_id = _text(canonical_observation_event_id)
    artifact = _text(strategy_artifact_sha256)
    rows, blockers = _normalise_posterior(posterior_buckets)
    if not all((posterior_id, realization_id, event_id, artifact)):
        blockers.append("v6_composer_lineage_missing")
    if position_exposure_snapshot.get("complete") is not True:
        blockers.append("v6_position_exposure_snapshot_incomplete")
    if coverage_snapshot.get("complete") is not True:
        blockers.append("v6_coverage_snapshot_incomplete")
    decision_time = _parse_utc(decision_as_of)
    entry_peak_time = _parse_utc(peak_timing_state.get("entry_peak_at"))
    if decision_time is None:
        blockers.append("v6_decision_time_invalid")
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(blockers)), "places_orders": False}

    hard_by_bucket = {
        row["bucket"]: _hard_certificate_valid(row, posterior_run_id=posterior_id, realization_state_id=realization_id)
        for row in rows
    }
    reachable = [row for row in rows if not hard_by_bucket[row["bucket"]]]
    if not reachable:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["v6_no_reachable_yes_bucket"], "places_orders": False}
    ranked = sorted(reachable, key=lambda row: (-row["probability"], row["bucket"]))
    main = ranked[0]
    second = next((row for row in ranked[1:] if row["bucket"] != main["bucket"]), None)
    hotter = [row for row in rows if row["ordinal"] > main["ordinal"]]
    p_above = sum(row["probability"] for row in hotter)
    all_hotter_hard = bool(hotter) and all(hard_by_bucket[row["bucket"]] for row in hotter)
    main_invalidated = hard_by_bucket[main["bucket"]]
    if main_invalidated:
        mode = "invalidated"
    elif all_hotter_hard:
        mode = "hard_locked"
    elif p_above >= 0.10:
        mode = "upside_risk"
    else:
        mode = "normal"
    risk_ratio = _risk_target_ratio(p_above)
    if entry_peak_time is None:
        time_ratio, peak_phase = 0.0, "entry_peak_at_unavailable"
    else:
        time_ratio, peak_phase = _time_cap_ratio(
            decision_at=decision_time, entry_peak_at=entry_peak_time,
            future_risk=bool(future_warming_or_rebound_risk),
        )
    stage_ratio = min(risk_ratio, time_ratio) if mode == "upside_risk" else 0.0
    cycle = advance_main_yes_position_cycle(
        posterior_run_id=posterior_id,
        bucket=main["bucket"],
        mode=mode,
        stage_target_ratio=stage_ratio,
        canonical_observation_event_id=event_id,
        prior_cycle=prior_main_yes_cycle,
        coverage_reconciled=coverage_snapshot.get("all_conflicting_sell_and_cancel_reconciled") is True,
    )
    if cycle.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": cycle.get("block_reasons"), "places_orders": False}

    yes_buckets = _side_buckets(position_exposure_snapshot, side="YES")
    no_buckets = _side_buckets(position_exposure_snapshot, side="NO")
    candidates: list[dict[str, Any]] = []
    candidates.append({
        "candidate_id": _identity("v6cand_", (posterior_id, "main_yes", main["bucket"])),
        "strategy_leg": "main_yes", "bucket": main["bucket"], "side": "YES",
        "p_yes": main["probability"], "raw_target_notional_usdc": 20.0,
        "entry_allowed": main["bucket"] not in no_buckets,
        "entry_block_reason": "v6_same_bucket_opposite_no_exposure" if main["bucket"] in no_buckets else "",
        "existing_exposure_notional_usdc": _main_slot_exposure(position_exposure_snapshot, current_bucket=main["bucket"]),
    })
    if second is not None:
        old_main_on_second = any(
            isinstance(raw, Mapping)
            and _text(raw.get("strategy_leg")) == "main_yes"
            and _text(raw.get("side")).upper() == "YES"
            and _text(raw.get("bucket")) == second["bucket"]
            and (_finite(raw.get("position_size")) or 0.0) > 1e-9
            for raw in position_exposure_snapshot.get("exposures") or []
        )
        second_block_reason = (
            "v6_same_bucket_opposite_no_exposure" if second["bucket"] in no_buckets
            else "v6_old_main_reclassification_exit_pending" if old_main_on_second
            else "v6_second_yes_entry_window_closed" if not second_yes_entry_allowed or entry_peak_time is None
            else ""
        )
        candidates.append({
            "candidate_id": _identity("v6cand_", (posterior_id, "second_yes", second["bucket"])),
            "strategy_leg": "second_yes", "bucket": second["bucket"], "side": "YES",
            "p_yes": second["probability"], "raw_target_notional_usdc": 4.99,
            "entry_allowed": not second_block_reason,
            "entry_block_reason": second_block_reason,
            "existing_exposure_notional_usdc": _second_slot_exposure(position_exposure_snapshot),
        })

    excluded_yes = {main["bucket"], second["bucket"] if second else ""}
    hard_no_rows = [row for row in rows if hard_by_bucket[row["bucket"]] and row["bucket"] not in excluded_yes]
    no_candidate: dict[str, Any] | None = None
    if hard_no_rows:
        chosen = sorted(hard_no_rows, key=lambda row: (row["probability"], row["bucket"]))[0]
        no_candidate = {
            "candidate_id": _identity("v6cand_", (posterior_id, "hard_exclusion_no", chosen["bucket"])),
            "strategy_leg": "hard_exclusion_no", "no_mode": "hard_exclusion_no",
            "bucket": chosen["bucket"], "side": "NO", "p_yes": chosen["probability"],
            "q_no": 1.0 - chosen["probability"], "raw_target_notional_usdc": 200.0,
            "hard_exclusion_certificate_id": _text(chosen.get("hard_exclusion_certificate", {}).get("certificate_sha256")),
            "existing_exposure_notional_usdc": _no_bucket_exposure(position_exposure_snapshot, bucket=chosen["bucket"]),
        }
    else:
        trajectory_rows = []
        for row in rows:
            if row["bucket"] in excluded_yes:
                continue
            q_no = 1.0 - row["probability"]
            prior_amount = None
            if (
                _text((prior_no_state or {}).get("no_mode")) == "trajectory_no"
                and _text((prior_no_state or {}).get("bucket")) == row["bucket"]
            ):
                prior_amount = _finite((prior_no_state or {}).get("raw_target_notional_usdc"))
            band = _trajectory_band(q_no, prior_amount=prior_amount)
            if row.get("soft_unreachable") is True and band is None:
                return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["trajectory_no_probability_projection_inconsistent"], "places_orders": False}
            if band is not None:
                trajectory_rows.append((q_no, row, band))
        if trajectory_rows:
            q_no, chosen, (amount, band_name) = sorted(
                trajectory_rows,
                key=lambda item: (-item[0], not bool(item[1].get("soft_unreachable")), item[1]["bucket"]),
            )[0]
            no_candidate = {
                "candidate_id": _identity("v6cand_", (posterior_id, "trajectory_no", chosen["bucket"], band_name)),
                "strategy_leg": "trajectory_no", "no_mode": "trajectory_no",
                "bucket": chosen["bucket"], "side": "NO", "p_yes": chosen["probability"],
                "q_no": q_no, "soft_unreachable": bool(chosen.get("soft_unreachable")),
                "amount_band": band_name, "raw_target_notional_usdc": amount,
                "existing_exposure_notional_usdc": _no_bucket_exposure(position_exposure_snapshot, bucket=chosen["bucket"]),
            }
    if no_candidate:
        conflicting_no_buckets = no_buckets - {no_candidate["bucket"]}
        if no_candidate["bucket"] in yes_buckets:
            no_candidate["entry_allowed"] = False
            no_candidate["entry_block_reason"] = "v6_same_bucket_opposite_yes_exposure"
        elif conflicting_no_buckets:
            no_candidate["entry_allowed"] = False
            no_candidate["entry_block_reason"] = "v6_existing_different_no_bucket_must_close_first"
        else:
            no_candidate["entry_allowed"] = True
            no_candidate["entry_block_reason"] = ""
        candidates.append(no_candidate)

    eligible_buys = [row for row in candidates if row.get("entry_allowed", True) is True]
    allocation = allocate_v6_incremental_capital(
        candidates=eligible_buys,
        available_capital_usdc=available_capital_usdc,
        capital_snapshot_id=capital_snapshot_id,
        risk_snapshot_id=risk_snapshot_id,
        capital_epoch_id=capital_epoch_id,
    )
    if allocation.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": allocation.get("block_reasons"), "places_orders": False}
    by_id = {row["candidate_id"]: row for row in allocation["allocations"]}
    final_candidates = [by_id.get(row["candidate_id"], {**row, "capital_scale_factor": allocation["capital_scale_factor"], "raw_incremental_target_notional_usdc": 0.0, "final_incremental_target_notional_usdc": 0.0}) for row in candidates]

    target_exit = None
    if mode == "upside_risk" and cycle["incremental_stage_count"] > 0:
        stage = stage_number_for_ratio(stage_ratio)
        target_exit = {
            "exit_reason_family": "target_realization",
            "exit_reason_code": f"target_realization_main_yes_upside_risk_stage_{stage}",
            "main_yes_position_cycle_id": cycle["main_yes_position_cycle_id"],
            "stage_target_ratio": stage_ratio,
            "release_standing_coverage_first": True,
        }
    elif mode == "invalidated":
        target_exit = {
            "exit_reason_family": "target_realization",
            "exit_reason_code": "target_realization_main_yes_invalidated",
            "main_yes_position_cycle_id": cycle["main_yes_position_cycle_id"],
            "stage_target_ratio": 1.0,
            "release_standing_coverage_first": True,
        }

    off_role_main_yes_exits = []
    for exposure in position_exposure_snapshot.get("exposures") or []:
        if not isinstance(exposure, Mapping):
            continue
        if (
            _text(exposure.get("strategy_leg")) == "main_yes"
            and _text(exposure.get("side")).upper() == "YES"
            and _text(exposure.get("bucket")) != main["bucket"]
            and (_finite(exposure.get("position_size")) or 0.0) > 1e-9
        ):
            off_role_main_yes_exits.append({
                "position_lot_id": _text(exposure.get("position_lot_id")),
                "bucket": _text(exposure.get("bucket")),
                "main_yes_position_cycle_id": _text(exposure.get("main_yes_position_cycle_id")) or cycle["main_yes_position_cycle_id"],
                "exit_reason_family": "target_realization",
                "exit_reason_code": "target_realization_main_yes_invalidated",
                "stage_target_ratio": 1.0,
                "release_standing_coverage_first": True,
            })

    identity = {
        "posterior_run_id": posterior_id,
        "realization_state_id": realization_id,
        "canonical_observation_event_id": event_id,
        "strategy_artifact_sha256": artifact,
        "candidate_ids": [row["candidate_id"] for row in final_candidates],
        "main_yes_position_cycle_id": cycle["main_yes_position_cycle_id"],
        "capital_allocation_id": allocation["capital_allocation_id"],
    }
    plan_id = _identity("v6plan_", identity)
    risk_reservation_id = _identity("v6reserve_", (plan_id, allocation["capital_allocation_id"]))
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "v6_trading_plan_id": plan_id,
        "risk_reservation_id": risk_reservation_id,
        **identity,
        "capital_policy_id": CAPITAL_POLICY_ID,
        "main_yes_bucket": main["bucket"],
        "second_yes_bucket": second["bucket"] if second else "",
        "main_yes_mode": mode,
        "p_above_top": p_above,
        "risk_target_ratio": risk_ratio,
        "time_cap_ratio": time_ratio,
        "peak_phase": peak_phase,
        "main_yes_stage_target_ratio": stage_ratio,
        "main_yes_cycle": cycle,
        "no_mode": no_candidate.get("no_mode") if no_candidate else "none",
        "candidates": final_candidates,
        "capital_allocation": allocation,
        "target_realization_exit": target_exit,
        "off_role_main_yes_exits": off_role_main_yes_exits,
        "boundary": {
            "posterior_is_only_weather_probability_input": True,
            "exactly_one_no_slot": True,
            "uniform_capital_scale_factor": True,
            "position_cycle_watermark_monotonic": True,
            "standing_coverage_release_precedes_main_yes_target_sell": True,
            "trajectory_no_downshift_hysteresis_pp": 1,
            "uses_ev_edge_lcb": False,
            "writes_database": False,
            "places_orders": False,
        },
        "places_orders": False,
    }


__all__ = [
    "LEG_CAPS", "RISK_BANDS", "SCHEMA_VERSION", "TRAJECTORY_NO_BANDS",
    "TRAJECTORY_NO_DOWNSHIFT_HYSTERESIS",
    "build_target_realization_v6_plan",
]
