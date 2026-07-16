"""The sole three-leg weather trading selection policy.

It consumes a committed posterior and explicit strong-physical support only:
one most-likely YES wave, one second-likely YES, and one strongly physically-excluded NO. It deliberately has no
EV/Edge/LCB input and never treats the current report temperature as a NO
signal.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "strong_posterior_three_leg_strategy_v2"
CAPITAL_POLICY_ID = "dynamic_three_leg_capital_policy_v2_top20_second4_99_excluded_no100"
TOP_YES_MAX_NOTIONAL_USDC = 20.0
# Strictly less than 5 USDC at cent precision, both per target and aggregate.
SECOND_YES_MAX_NOTIONAL_USDC = 4.99
LEAST_NO_MAX_NOTIONAL_USDC = 100.0
TOP_YES_WAVE_TARGET_PRICE = 0.90


def _finite(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalise(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    out, issues, seen = [], [], set()
    for raw in rows:
        bucket = _text(raw.get("bucket") or raw.get("outcome") or raw.get("label"))
        probability = _finite(raw.get("probability") if raw.get("probability") is not None else raw.get("posterior"))
        if not bucket or probability is None or probability < 0 or probability > 1:
            issues.append("three_leg_posterior_bucket_invalid")
            continue
        if bucket in seen:
            issues.append("three_leg_posterior_bucket_duplicate")
            continue
        seen.add(bucket)
        out.append({"bucket": bucket, "probability": probability, "hard_zero": bool(raw.get("hard_zero")), "yes_bid": _finite(raw.get("yes_bid")), "no_bid": _finite(raw.get("no_bid")), "yes_token": _text(raw.get("yes_token")), "no_token": _text(raw.get("no_token"))})
    if not out or abs(sum(row["probability"] for row in out) - 1.0) > 1e-6:
        issues.append("three_leg_posterior_distribution_missing_or_not_normalized")
    return out, sorted(set(issues))


def _strongly_excluded_no_candidate(
    rows: Sequence[Mapping[str, Any]], evidence: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    """Choose only a composer-declared physical exclusion, never low posterior alone."""
    evidence = evidence if isinstance(evidence, Mapping) else {}
    candidates: list[tuple[tuple[float, float, float, str], dict[str, Any]]] = []
    # Every NO must be emitted by the composer after the X->S->B projection.
    # L4 is forbidden from comparing physical X directly with market B or
    # recreating momentum/trajectory/cloud/rain logic from raw observations.
    exclusions = evidence.get("bucket_exclusions")
    if isinstance(exclusions, Mapping):
        exclusions = [dict(value, bucket=key) for key, value in exclusions.items() if isinstance(value, Mapping)]
    by_bucket = {str(row["bucket"]): dict(row) for row in rows}
    if isinstance(exclusions, Sequence) and not isinstance(exclusions, (str, bytes)):
        for raw in exclusions:
            if not isinstance(raw, Mapping) or raw.get("excluded") is not True:
                continue
            bucket = _text(raw.get("bucket") or raw.get("outcome"))
            row = by_bucket.get(bucket)
            kind = _text(raw.get("kind") or raw.get("reason"))
            if not row or kind not in {"observed_daily_high_exceeded", "remaining_warming_unattainable"}:
                continue
            if raw.get("composer_joint_strong_model") is not True:
                continue
            if (
                kind == "observed_daily_high_exceeded"
                and _text(raw.get("settlement_exclusion_certificate_state")) != "valid"
            ):
                continue
            confidence = _finite(raw.get("confidence"))
            if confidence is None or confidence < 0.0 or confidence > 1.0:
                continue
            selected = dict(row)
            selected["physical_exclusion_kind"] = kind
            selected["physical_exclusion_evidence"] = (
                "closed_bridge_certified_observed_daily_high_exceeded"
                if kind == "observed_daily_high_exceeded"
                else "composer_joint_momentum_trajectory_cloud_rain"
            )
            selected["physical_exclusion_confidence"] = confidence
            candidates.append(((2.0 if kind == "observed_daily_high_exceeded" else 1.0, confidence, 1.0 - selected["probability"], selected["bucket"]), selected))
    if not candidates:
        return None, "strongly_excluded_no_bucket_missing"
    return max(candidates, key=lambda item: item[0])[1], ""


def build_strong_posterior_three_leg_strategy(
    *, posterior_buckets: Sequence[Mapping[str, Any]], strong_physical_evidence: Mapping[str, Any] | None,
    settlement_window_open: bool, available_cash_usdc: float,
) -> dict[str, Any]:
    """Select main/second YES plus one report-proven impossible NO; fail closed."""
    rows, issues = _normalise(posterior_buckets)
    if issues:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": issues}
    evidence = strong_physical_evidence if isinstance(strong_physical_evidence, Mapping) else {}
    if evidence.get("joint_feature_model_applied") is not True or not _text(evidence.get("top_bucket")):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["three_leg_composer_strong_physical_evidence_invalid"]}
    excluded_buckets = {
        _text(raw.get("bucket") or raw.get("outcome"))
        for raw in evidence.get("bucket_exclusions") or []
        if isinstance(raw, Mapping) and raw.get("excluded") is True
    }
    yes_rows = [row for row in rows if not row["hard_zero"] and row["bucket"] not in excluded_buckets]
    if not yes_rows:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["three_leg_no_physically_reachable_yes_bucket"]}
    ranked = sorted(yes_rows, key=lambda row: (-row["probability"], row["bucket"]))
    top = ranked[0]
    second = next((row for row in ranked if row["bucket"] != top["bucket"]), None)
    top_supported = bool(
        evidence.get("joint_feature_model_applied") is True
        and _text(evidence.get("top_bucket")) == top["bucket"]
        and evidence.get("top_bucket_joint_strong_support") is True
    )
    excluded_no, excluded_no_reason = _strongly_excluded_no_candidate(rows, strong_physical_evidence)
    observed_daily_high_c = _finite((strong_physical_evidence or {}).get("safe_no_observed_max_c"))
    cash = max(0.0, _finite(available_cash_usdc) or 0.0)
    top_reason = "" if top_supported else "top_yes_strong_physical_support_incomplete"
    second_reason = ""
    no_reason = excluded_no_reason
    if not settlement_window_open:
        no_reason = "strongly_excluded_no_settlement_window_not_open"
    if top["yes_bid"] is None:
        top_reason = top_reason or "top_yes_quote_missing"
    if second is None:
        second_reason = "second_yes_bucket_missing"
    elif second["yes_bid"] is None:
        second_reason = second_reason or "second_yes_quote_missing"
    if excluded_no is not None and excluded_no["no_bid"] is None:
        no_reason = no_reason or "strongly_excluded_no_quote_missing"
    return {
        "schema": SCHEMA_VERSION, "ok": True, "state": "available", "strategy": "strong_posterior_top_yes_second_yes_and_strongly_excluded_no",
        "capital_policy_id": CAPITAL_POLICY_ID,
        "strong_physical_evidence_required": True,
        "trajectory_report_temperature_no_retired": True,
        "legs": [
            {"strategy_leg": "top_yes_wave", "bucket": top["bucket"], "side": "YES", "posterior": top["probability"], "limit_price": top["yes_bid"], "target_notional_usdc": min(cash, TOP_YES_MAX_NOTIONAL_USDC) if not top_reason else 0.0, "target_notional_cap_usdc": TOP_YES_MAX_NOTIONAL_USDC, "max_notional_usdc": TOP_YES_MAX_NOTIONAL_USDC, "action": "BUY" if not top_reason else "HOLD", "block_reason": top_reason, "dynamic_wave_exit_target_price": TOP_YES_WAVE_TARGET_PRICE, "wave_exit_policy": "current_top_yes_only_dynamic_posterior_reselection"},
            {"strategy_leg": "second_yes", "bucket": second["bucket"] if second else "", "side": "YES", "posterior": second["probability"] if second else None, "limit_price": second["yes_bid"] if second else None, "target_notional_usdc": min(cash, SECOND_YES_MAX_NOTIONAL_USDC) if not second_reason else 0.0, "target_notional_cap_usdc": SECOND_YES_MAX_NOTIONAL_USDC, "max_notional_usdc": SECOND_YES_MAX_NOTIONAL_USDC, "action": "BUY" if not second_reason else "HOLD", "block_reason": second_reason},
            {"strategy_leg": "strongly_excluded_no", "bucket": excluded_no["bucket"] if excluded_no else "", "side": "NO", "posterior": excluded_no["probability"] if excluded_no else None, "limit_price": excluded_no["no_bid"] if excluded_no else None, "target_notional_usdc": min(cash, LEAST_NO_MAX_NOTIONAL_USDC) if not no_reason else 0.0, "target_notional_cap_usdc": LEAST_NO_MAX_NOTIONAL_USDC, "max_notional_usdc": LEAST_NO_MAX_NOTIONAL_USDC, "action": "BUY" if not no_reason else "HOLD", "block_reason": no_reason, "physical_exclusion_kind": excluded_no.get("physical_exclusion_kind") if excluded_no else "", "physical_exclusion_evidence": excluded_no.get("physical_exclusion_evidence") if excluded_no else "", "physical_exclusion_confidence": excluded_no.get("physical_exclusion_confidence") if excluded_no else None, "observed_daily_high_c": observed_daily_high_c},
        ],
        "boundary": {"posterior_only": True, "strong_physical_evidence_only": True, "uses_ev_edge_lcb": False, "trajectory_no_absent": True, "physical_x_not_compared_directly_to_market_bucket_b": True, "strongly_excluded_no_requires_settlement_window": True, "writes_orders": False},
    }
