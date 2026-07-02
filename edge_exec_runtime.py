from __future__ import annotations

from typing import Any, Dict, Optional
import math


def _to_float(val: Any) -> Optional[float]:
    try:
        num = float(val)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def estimate_tradeable_edge_floor(
    *,
    edge_raw: Any,
    p_reach: Any,
    p_reach_lcb: Any,
    p_reach_ucb: Any,
    spread: Any = None,
    fee_cost: Any = None,
    hold_cost: Any = None,
    slippage_buffer: Any = None,
    edge_semantics: str = "raw_exec",
) -> Dict[str, Any]:
    edge_raw_f = _to_float(edge_raw)
    semantics = str(edge_semantics or "raw_exec").strip().lower() or "raw_exec"
    if edge_raw_f is None:
        return {}
    if semantics in {"realized_v2", "v2_realized", "realized"}:
        # edge_realizable_v2 already includes execution/realization effects.
        # Do not multiply by path probability or subtract miss cost again.
        return {
            "edge_expected_tradeable": float(edge_raw_f),
            "edge_lcb_tradeable": float(edge_raw_f),
            "edge_ucb_tradeable": float(edge_raw_f),
            "edge_miss_cost": 0.0,
            "edge_slippage_buffer": 0.0,
            "edge_floor_mode": "realized_v2_passthrough",
            "path_gate_enforced": False,
        }

    p_reach_f = _to_float(p_reach)
    p_lcb_f = _to_float(p_reach_lcb)
    p_ucb_f = _to_float(p_reach_ucb)
    if p_reach_f is None:
        return {}
    if p_lcb_f is None:
        p_lcb_f = max(0.0, p_reach_f - 0.20)
    if p_ucb_f is None:
        p_ucb_f = min(1.0, p_reach_f + 0.20)
    p_reach_f = max(0.0, min(1.0, p_reach_f))
    p_lcb_f = max(0.0, min(1.0, p_lcb_f))
    p_ucb_f = max(0.0, min(1.0, p_ucb_f))

    spread_f = _to_float(spread)
    fee_cost_f = _to_float(fee_cost)
    hold_cost_f = _to_float(hold_cost)
    slip_buf_f = _to_float(slippage_buffer)
    if fee_cost_f is None:
        fee_cost_f = 0.0
    if hold_cost_f is None:
        hold_cost_f = 0.0
    if slip_buf_f is None:
        slip_buf_f = 0.003
    miss_cost = fee_cost_f + hold_cost_f + slip_buf_f
    if spread_f is not None and spread_f > 0:
        miss_cost += spread_f * 0.5
    miss_cost = max(0.0, miss_cost)

    expected = p_reach_f * edge_raw_f - (1.0 - p_reach_f) * miss_cost
    edge_lcb = p_lcb_f * edge_raw_f - (1.0 - p_lcb_f) * miss_cost * 1.10
    edge_ucb = p_ucb_f * edge_raw_f - (1.0 - p_ucb_f) * miss_cost * 0.90

    return {
        "edge_expected_tradeable": float(expected),
        "edge_lcb_tradeable": float(edge_lcb),
        "edge_ucb_tradeable": float(edge_ucb),
        "edge_miss_cost": float(miss_cost),
        "edge_slippage_buffer": float(slip_buf_f),
        "edge_floor_mode": "raw_exec_path_floor",
        "path_gate_enforced": True,
    }
