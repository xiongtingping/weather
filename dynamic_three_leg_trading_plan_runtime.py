"""Pure dynamic three-leg plan built only from committed L3 realization state.

The plan is recomputed for every posterior revision.  It emits at most the
current top YES wave, current second YES settlement leg, and one composer-
declared strongly excluded NO.  It never freezes bucket selection, creates a
MarketAlpha cycle, or revives remote/trajectory/current-report NO rules.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from strong_posterior_three_leg_strategy import CAPITAL_POLICY_ID, build_strong_posterior_three_leg_strategy


SCHEMA_VERSION = "dynamic_three_leg_trading_plan_v1"
LEG_ORDER = ("top_yes_wave", "second_yes", "strongly_excluded_no")


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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:24]


def _market_rows(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = snapshot.get("buckets")
    if isinstance(raw, Mapping):
        return {_text(label): _mapping(row) for label, row in raw.items() if _text(label)}
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return {}
    rows: dict[str, Mapping[str, Any]] = {}
    for raw_row in raw:
        row = _mapping(raw_row)
        bucket = _text(row.get("bucket") or row.get("outcome") or row.get("label"))
        if bucket:
            rows[bucket] = row
    return rows


def _posterior_rows(realization: Mapping[str, Any], market: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    probabilities = _mapping(realization.get("market_bucket_probabilities"))
    rows: list[dict[str, Any]] = []
    for bucket, probability in probabilities.items():
        quote = _mapping(market.get(_text(bucket)))
        rows.append(
            {
                "bucket": _text(bucket),
                "probability": probability,
                # The selector uses these fields as executable BUY prices.
                "yes_bid": quote.get("yes_ask"),
                "no_bid": quote.get("no_ask"),
                "yes_token": quote.get("yes_token_id") or quote.get("yes_token"),
                "no_token": quote.get("no_token_id") or quote.get("no_token"),
            }
        )
    return rows


def _capital_notional(capital: Mapping[str, Any], leg: Mapping[str, Any]) -> float | None:
    configured = _finite(_mapping(capital.get("notional_by_leg")).get(_text(leg.get("strategy_leg"))))
    model_target = _finite(leg.get("target_notional_usdc"))
    cap = _finite(leg.get("target_notional_cap_usdc"))
    if configured is None or model_target is None or cap is None:
        return None
    value = min(configured, model_target, cap)
    return value if value > 0.0 else None


def build_dynamic_three_leg_trading_plan(
    *,
    realization_state: Mapping[str, Any],
    settlement_window_state: Mapping[str, Any],
    second_yes_entry_window_state: Mapping[str, Any],
    top_yes_wave_reentry_state: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    capital_plan: Mapping[str, Any],
    strategy_artifact_sha256: str,
) -> dict[str, Any]:
    """Build the current revision's three-leg BUY plan; never writes or orders."""

    state = _mapping(realization_state)
    window = _mapping(settlement_window_state)
    second_window = _mapping(second_yes_entry_window_state)
    reentry = _mapping(top_yes_wave_reentry_state)
    market_snapshot = _mapping(market_snapshot)
    capital = _mapping(capital_plan)
    market = _market_rows(market_snapshot)
    blockers: list[str] = []
    posterior_run_id = _text(state.get("posterior_run_id"))
    realization_state_id = _text(state.get("realization_state_id"))
    market_snapshot_id = _text(market_snapshot.get("market_snapshot_id"))
    strategy_artifact = _text(strategy_artifact_sha256)
    capital_snapshot_id = _text(capital.get("capital_snapshot_id"))
    risk_snapshot_id = _text(capital.get("risk_snapshot_id"))
    capital_epoch_id = _text(capital.get("capital_epoch_id"))
    risk_reservation_id = _text(capital.get("risk_reservation_id"))
    settlement_window_state_id = _text(window.get("settlement_window_state_id"))
    settlement_window_policy_artifact_sha256 = _text(window.get("policy_artifact_sha256"))
    second_yes_entry_window_state_id = _text(second_window.get("second_yes_entry_window_state_id"))
    reentry_state_id = _text(reentry.get("top_yes_wave_reentry_state_id"))
    if not posterior_run_id or not realization_state_id:
        blockers.append("three_leg_realization_identity_missing")
    if not market_snapshot_id or not market:
        blockers.append("three_leg_market_snapshot_or_buckets_missing")
    if not strategy_artifact:
        blockers.append("three_leg_strategy_artifact_missing")
    if (
        window.get("schema") != "settlement_window_state_v1"
        or window.get("ok") is not True
        or _text(window.get("state")) != "available"
        or not settlement_window_state_id
        or not settlement_window_policy_artifact_sha256
    ):
        blockers.append("three_leg_settlement_window_state_invalid")
    if (
        second_window.get("schema") != "second_yes_entry_window_state_v1"
        or second_window.get("ok") is not True
        or _text(second_window.get("state")) != "available"
        or _text(second_window.get("posterior_run_id")) != posterior_run_id
        or _text(second_window.get("realization_state_id")) != realization_state_id
        or not second_yes_entry_window_state_id
    ):
        blockers.append("three_leg_second_yes_entry_window_state_invalid")
    if (
        reentry.get("schema") != "top_yes_wave_reentry_state_v1"
        or reentry.get("ok") is not True
        or _text(reentry.get("state")) != "available"
        or _text(reentry.get("posterior_run_id")) != posterior_run_id
        or not reentry_state_id
    ):
        blockers.append("three_leg_top_yes_wave_reentry_state_invalid")
    if not all((capital_snapshot_id, risk_snapshot_id, capital_epoch_id, risk_reservation_id)):
        blockers.append("three_leg_capital_or_risk_identity_missing")
    posterior_rows = _posterior_rows(state, market)
    if not posterior_rows:
        blockers.append("three_leg_l3_market_bucket_probabilities_missing")
    evidence = _mapping(state.get("strong_physical_evidence"))
    if not evidence:
        blockers.append("three_leg_l3_strong_physical_evidence_missing")
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "read_only": True,
        }
    selected = build_strong_posterior_three_leg_strategy(
        posterior_buckets=posterior_rows,
        strong_physical_evidence=evidence,
        settlement_window_open=window.get("settlement_window_open") is True,
        available_cash_usdc=_finite(capital.get("available_cash_usdc")) or 0.0,
    )
    if selected.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(selected.get("block_reasons") or ["three_leg_selection_blocked"]),
            "read_only": True,
        }
    if _text(selected.get("capital_policy_id")) != CAPITAL_POLICY_ID:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["three_leg_capital_policy_identity_invalid"],
            "read_only": True,
        }
    candidates: list[dict[str, Any]] = []
    retention_candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for raw_leg in selected.get("legs") or []:
        leg = dict(_mapping(raw_leg))
        role = _text(leg.get("strategy_leg"))
        bucket = _text(leg.get("bucket"))
        side = _text(leg.get("side")).upper()
        quote = _mapping(market.get(bucket))
        price = _finite(leg.get("limit_price"))
        notional = _capital_notional(capital, leg)
        token_id = _text(
            quote.get("yes_token_id") or quote.get("yes_token")
            if side == "YES"
            else quote.get("no_token_id") or quote.get("no_token")
        )
        if role == "top_yes_wave" and bucket in {
            _text(value) for value in reentry.get("suppressed_buckets") or [] if _text(value)
        }:
            skipped.append({"strategy_leg": role, "bucket": bucket, "reason": "top_yes_wave_completed_under_same_posterior"})
            continue
        if role not in LEG_ORDER or leg.get("action") != "BUY" or not bucket:
            skipped.append({"strategy_leg": role, "bucket": bucket, "reason": _text(leg.get("block_reason") or "leg_hold")})
            continue
        if side != ("NO" if role == "strongly_excluded_no" else "YES"):
            skipped.append({"strategy_leg": role, "bucket": bucket, "reason": "three_leg_side_role_mismatch"})
            continue
        if price is None or not 0.0 < price < 1.0 or notional is None or not token_id:
            skipped.append({"strategy_leg": role, "bucket": bucket, "reason": "three_leg_executable_quote_capital_or_token_missing"})
            continue
        identity = {
            "posterior_run_id": posterior_run_id,
            "realization_state_id": realization_state_id,
            "strategy_leg": role,
            "bucket": bucket,
            "side": side,
            "market_snapshot_id": market_snapshot_id,
            "capital_snapshot_id": capital_snapshot_id,
            "risk_snapshot_id": risk_snapshot_id,
            "capital_epoch_id": capital_epoch_id,
            "risk_reservation_id": risk_reservation_id,
            "settlement_window_state_id": settlement_window_state_id,
            "settlement_window_policy_artifact_sha256": settlement_window_policy_artifact_sha256,
            "second_yes_entry_window_state_id": second_yes_entry_window_state_id,
            "top_yes_wave_reentry_state_id": reentry_state_id,
            "strategy_artifact_sha256": strategy_artifact,
            "capital_policy_id": CAPITAL_POLICY_ID,
        }
        candidate = {
            "candidate_id": _identity("trcand_", identity),
            **identity,
            "price": price,
            "notional": notional,
            "token_id": token_id,
            "reason_family": "strategy_leg",
            "reason_code": role,
            "uses_ev_edge_or_fair_value_gate": False,
            "dynamic_reselection": True,
            "entry_allowed": True,
        }
        if role == "second_yes" and second_window.get("entry_allowed") is not True:
            candidate["entry_allowed"] = False
            candidate["entry_block_reason"] = _text(
                second_window.get("entry_block_reason") or "second_yes_entry_window_closed"
            )
            retention_candidates.append(candidate)
            skipped.append({
                "strategy_leg": role,
                "bucket": bucket,
                "reason": candidate["entry_block_reason"],
                "existing_same_bucket_position_may_be_retained": True,
            })
        else:
            candidates.append(candidate)
    candidates.sort(key=lambda row: (LEG_ORDER.index(row["strategy_leg"]), row["bucket"]))
    retention_candidates.sort(key=lambda row: (LEG_ORDER.index(row["strategy_leg"]), row["bucket"]))
    plan_identity = {
        "posterior_run_id": posterior_run_id,
        "realization_state_id": realization_state_id,
        "market_snapshot_id": market_snapshot_id,
        "capital_snapshot_id": capital_snapshot_id,
        "risk_snapshot_id": risk_snapshot_id,
        "capital_epoch_id": capital_epoch_id,
        "risk_reservation_id": risk_reservation_id,
        "settlement_window_state_id": settlement_window_state_id,
        "settlement_window_policy_artifact_sha256": settlement_window_policy_artifact_sha256,
        "second_yes_entry_window_state_id": second_yes_entry_window_state_id,
        "top_yes_wave_reentry_state_id": reentry_state_id,
        "strategy_artifact_sha256": strategy_artifact,
        "capital_policy_id": CAPITAL_POLICY_ID,
        "candidate_ids": [row["candidate_id"] for row in candidates],
        "retention_candidate_ids": [row["candidate_id"] for row in retention_candidates],
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "trading_plan_id": _identity("trplan_", plan_identity),
        "plan_revision": 0,
        **plan_identity,
        "candidates": candidates,
        "retention_candidates": retention_candidates,
        "skipped": skipped,
        "second_yes_entry_window_state": dict(second_window),
        "boundary": {
            "realization_state_is_only_weather_input": True,
            "settlement_window_is_separate_execution_timing_input": True,
            "settlement_window_not_read_from_realization_state": True,
            "second_yes_entry_only_in_predicted_peak_final_30_minutes": True,
            "second_yes_window_close_does_not_invalidate_same_bucket_position": True,
            "same_posterior_completed_top_wave_reentry_suppressed": True,
            "new_posterior_reopens_dynamic_top_selection": True,
            "dynamic_bucket_selection_per_posterior": True,
            "approved_roles": list(LEG_ORDER),
            "remote_no_retired": True,
            "trajectory_no_retired": True,
            "current_report_no_retired": True,
            "market_alpha_cycle_not_created": True,
            "top_yes_exit_target_is_dynamic_090": True,
            "ev_edge_lcb_not_consumed": True,
            "places_orders": False,
        },
        "read_only": True,
    }


__all__ = ["LEG_ORDER", "SCHEMA_VERSION", "build_dynamic_three_leg_trading_plan"]
