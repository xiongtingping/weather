"""Pure proportional capital allocator for Target Realization v6."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "dynamic_three_leg_v6_capital_allocation_v1"
CAPITAL_POLICY_ID = "dynamic_three_leg_capital_policy_v6_top20_second4_99_trajectory100_hard200"


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _identity(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "v6cap_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def allocate_v6_incremental_capital(
    *,
    candidates: Sequence[Mapping[str, Any]],
    available_capital_usdc: float,
    capital_snapshot_id: str,
    risk_snapshot_id: str,
    capital_epoch_id: str,
) -> dict[str, Any]:
    """Apply one scale factor to every eligible incremental BUY.

    Candidate input must already include current/off-role/pending exposure in
    ``existing_exposure_notional_usdc``.  This layer never sells positions and
    never reads prices, EV, Edge, or LCB.
    """

    blockers: list[str] = []
    available = _finite(available_capital_usdc)
    identities = tuple(_text(value) for value in (capital_snapshot_id, risk_snapshot_id, capital_epoch_id))
    if available is None or available < 0:
        blockers.append("v6_capital_available_invalid")
    if not all(identities):
        blockers.append("v6_capital_snapshot_identity_missing")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates:
        candidate_id = _text(raw.get("candidate_id"))
        target = _finite(raw.get("raw_target_notional_usdc"))
        exposure = _finite(raw.get("existing_exposure_notional_usdc"))
        if not candidate_id or candidate_id in seen:
            blockers.append("v6_capital_candidate_identity_invalid")
            continue
        seen.add(candidate_id)
        if target is None or target < 0 or exposure is None or exposure < 0:
            blockers.append("v6_capital_candidate_notional_invalid")
            continue
        incremental = max(0.0, target - exposure)
        rows.append({
            **dict(raw),
            "raw_target_notional_usdc": target,
            "existing_exposure_notional_usdc": exposure,
            "raw_incremental_target_notional_usdc": incremental,
        })
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "allocations": [],
            "places_orders": False,
        }
    requested = sum(row["raw_incremental_target_notional_usdc"] for row in rows)
    alpha = 1.0 if requested <= 1e-12 else min(1.0, max(0.0, float(available) / requested))
    allocations = []
    for row in rows:
        final = row["raw_incremental_target_notional_usdc"] * alpha
        allocations.append({
            **row,
            "capital_scale_factor": alpha,
            "final_incremental_target_notional_usdc": final,
        })
    identity = {
        "capital_snapshot_id": identities[0],
        "risk_snapshot_id": identities[1],
        "capital_epoch_id": identities[2],
        "available_capital_usdc": available,
        "requested_incremental_notional_usdc": requested,
        "capital_scale_factor": alpha,
        "allocations": [
            (row["candidate_id"], row["raw_incremental_target_notional_usdc"], row["final_incremental_target_notional_usdc"])
            for row in allocations
        ],
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "capital_allocation_id": _identity(identity),
        "capital_policy_id": CAPITAL_POLICY_ID,
        **identity,
        "allocated_incremental_notional_usdc": sum(row["final_incremental_target_notional_usdc"] for row in allocations),
        "allocations": allocations,
        "boundary": {
            "single_scale_factor_for_all_buy_new": True,
            "existing_off_role_and_pending_exposure_already_deducted": True,
            "does_not_force_sell": True,
            "does_not_consume_price_ev_edge_lcb": True,
            "places_orders": False,
        },
        "places_orders": False,
    }


__all__ = ["CAPITAL_POLICY_ID", "SCHEMA_VERSION", "allocate_v6_incremental_capital"]
