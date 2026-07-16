"""Pure L1 candidate for the dynamic current-top YES wave take profit.

Bucket membership is reselected from the latest posterior on every
evaluation.  Only the take-profit price is frozen at entry.  The builder
requires complete position-lot, approved strategy-artifact, and fresh
executable-quote lineage; it writes nothing and never submits an order.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Mapping


SCHEMA_VERSION = "dynamic_top_yes_wave_exit_v2"
WAVE_TARGET_PRICE = 0.90
REASON_FAMILY = "market_alpha_take_profit"
REASON_CODE = "market_alpha_take_profit_top_yes_wave_completed"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _utc(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def build_dynamic_top_yes_wave_exit_candidate(
    *,
    posterior_ssot_fact: Mapping[str, Any] | None,
    position: Mapping[str, Any] | None,
    executable_sell_quote: Mapping[str, Any] | None,
    approved_strategy_artifact_sha256: str,
    decision_as_of: str,
) -> dict[str, Any]:
    ssot = posterior_ssot_fact if isinstance(posterior_ssot_fact, Mapping) else {}
    position = position if isinstance(position, Mapping) else {}
    quote = executable_sell_quote if isinstance(executable_sell_quote, Mapping) else {}
    top_bucket = _text(ssot.get("target_bucket"))
    posterior_run_id = _text(ssot.get("posterior_run_id"))
    realization_state_id = _text(ssot.get("realization_state_id"))
    bucket = _text(position.get("bucket") or position.get("outcome"))
    side = _text(position.get("side")).upper()
    token_id = _text(position.get("token_id"))
    position_lot_id = _text(position.get("position_lot_id"))
    position_lot_version = _text(position.get("position_lot_version"))
    entry_artifact = _text(position.get("entry_strategy_artifact_sha256"))
    approved_artifact = _text(approved_strategy_artifact_sha256)
    frozen_target = _number(position.get("frozen_take_profit_price"))
    position_size = _number(position.get("position_size"))
    quote_token_id = _text(quote.get("token_id"))
    price = _number(quote.get("price_for_requested_size"))
    requested_size = _number(quote.get("requested_size"))
    quote_as_of = _utc(quote.get("as_of"))
    quote_expires = _utc(quote.get("expires_at"))
    decision = _utc(decision_as_of)
    reasons: list[str] = []
    if not posterior_run_id or not realization_state_id or not top_bucket:
        reasons.append("posterior_top_identity_missing")
    if _text(position.get("management_scope")) != "dynamic_three_leg_weather":
        reasons.append("position_management_scope_invalid")
    if _text(position.get("strategy_leg")) != "top_yes_wave" or side != "YES":
        reasons.append("position_not_top_yes_wave")
    if not bucket or bucket != top_bucket:
        reasons.append("position_not_current_dynamic_top_yes")
    if not all((position_lot_id, position_lot_version, token_id)) or position_size is None or position_size <= 0:
        reasons.append("position_lot_identity_or_size_invalid")
    if not approved_artifact or entry_artifact != approved_artifact:
        reasons.append("position_strategy_artifact_not_approved")
    if frozen_target is None or abs(frozen_target - WAVE_TARGET_PRICE) > 1e-12:
        reasons.append("position_frozen_take_profit_price_invalid")
    if not _text(quote.get("market_snapshot_id")) or quote_token_id != token_id:
        reasons.append("executable_sell_quote_identity_invalid")
    if requested_size is None or position_size is None or abs(requested_size - position_size) > 1e-9:
        reasons.append("executable_sell_quote_size_mismatch")
    if decision is None or quote_as_of is None or quote_expires is None or quote_as_of > decision or decision > quote_expires:
        reasons.append("executable_sell_quote_not_current")
    if price is None:
        reasons.append("executable_sell_quote_missing")
    elif frozen_target is not None and price < frozen_target:
        reasons.append("dynamic_wave_target_not_reached")
    candidate = not reasons
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "candidate" if candidate else "hold",
        "posterior_run_id": posterior_run_id,
        "realization_state_id": realization_state_id,
        "current_top_bucket": top_bucket,
        "position_bucket": bucket,
        "position_side": side,
        "position_lot_id": position_lot_id,
        "position_lot_version": position_lot_version,
        "token_id": token_id,
        "requested_sell_size": position_size,
        "market_snapshot_id": _text(quote.get("market_snapshot_id")),
        "executable_sell_price": price,
        "limit_price": frozen_target,
        "frozen_take_profit_price": frozen_target,
        "strategy_artifact_sha256": approved_artifact,
        "reason_family": REASON_FAMILY if candidate else "",
        "reason_code": REASON_CODE if candidate else "",
        "posterior_reselected_at_evaluation": True,
        "bucket_selection_dynamic": True,
        "take_profit_price_frozen_at_entry": True,
        "order_limit_uses_frozen_take_profit_price": True,
        "writes_orders": False,
        "reason_codes": sorted(set(reasons)),
    }


__all__ = [
    "REASON_CODE",
    "REASON_FAMILY",
    "SCHEMA_VERSION",
    "WAVE_TARGET_PRICE",
    "build_dynamic_top_yes_wave_exit_candidate",
]
