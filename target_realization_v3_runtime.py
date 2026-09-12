from __future__ import annotations

import math
import os
import sqlite3
import hashlib
from typing import Any, Dict, List, Mapping, Optional, Tuple

from second_yes_entry_window_runtime import build_second_yes_entry_window_state
from strong_posterior_three_leg_strategy import build_strong_posterior_three_leg_strategy


def _float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _probability(value: Any) -> Optional[float]:
    parsed = _float(value)
    if parsed is None:
        return None
    if parsed > 1.0:
        parsed /= 100.0
    return max(0.0, min(1.0, parsed))


def _env_float(name: str, default: float) -> float:
    parsed = _float(os.environ.get(name))
    return float(default) if parsed is None else float(parsed)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _date_key(value: Any) -> str:
    return str(value or "").strip()[:10]


def _available_cash_usdc() -> float:
    bankroll = max(
        0.0,
        _env_float("PM_TARGET_REALIZATION_BANKROLL_USDC", _env_float("PM_BANKROLL_USDC", 900.0)),
    )
    return max(0.0, _env_float("PM_TARGET_REALIZATION_AVAILABLE_CASH_USDC", bankroll))


def _bulk_market_state(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    labels: List[str],
) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    ticks: Dict[Tuple[str, str], Dict[str, Any]] = {}
    tokens: Dict[str, Dict[str, Any]] = {}
    if not labels:
        return ticks, tokens
    placeholders = ",".join("?" for _ in labels)
    try:
        rows = conn.execute(
            f"""
            SELECT t.outcome, t.side, t.best_bid, t.best_ask,
                   t.bid_depth_topN, t.ask_depth_topN, t.created_at
            FROM event_market_ticks t
            JOIN (
                SELECT outcome, side, MAX(id) AS max_id
                FROM event_market_ticks
                WHERE slug = ? AND event_date = ?
                  AND outcome IN ({placeholders})
                GROUP BY outcome, side
            ) latest ON latest.max_id = t.id
            """,
            (slug, event_date, *labels),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        ticks[(str(row["outcome"] or ""), str(row["side"] or "").upper())] = {
            "best_bid": _float(row["best_bid"]),
            "best_ask": _float(row["best_ask"]),
            "bid_depth_topN": _float(row["bid_depth_topN"]),
            "ask_depth_topN": _float(row["ask_depth_topN"]),
            "created_at": str(row["created_at"] or ""),
        }
    try:
        rows = conn.execute(
            f"""
            SELECT outcome, yes_token, no_token
            FROM market_tokens
            WHERE slug = ? AND event_date = ?
              AND outcome IN ({placeholders})
            """,
            (slug, event_date, *labels),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        tokens[str(row["outcome"] or "")] = {
            "yes_token": str(row["yes_token"] or ""),
            "no_token": str(row["no_token"] or ""),
        }
    return ticks, tokens


def _dynamic_strategy_positions(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
) -> List[Dict[str, Any]]:
    """Read only positions with durable posterior-settlement order provenance.

    Dynamic posterior reselection must never close an unrelated position just
    because it happens to share the same market.  Missing order provenance is
    deliberately fail-closed for dynamic exits.
    """
    # `edge_trades` has existed in several schema revisions.  Cost basis is
    # required to enforce the per-leg dollar caps across posterior revisions;
    # when an older ledger lacks it, preserve the position but fail closed for
    # any incremental BUY instead of treating its allocation as zero.
    try:
        columns = {
            str(row[1] or "")
            for row in conn.execute("PRAGMA table_info(edge_trades)").fetchall()
        }
    except sqlite3.Error:
        columns = set()
    avg_price_column = "e.avg_price" if "avg_price" in columns else "NULL"
    entry_price_column = "e.entry_price" if "entry_price" in columns else "NULL"
    try:
        rows = conn.execute(
            f"""
            SELECT e.outcome, e.side, e.position_size, e.entry_size, e.state,
                   {avg_price_column} AS avg_price, {entry_price_column} AS entry_price,
                   o.strategy
            FROM edge_trades e
            JOIN auto_orders o ON o.order_id = e.last_order_id
            WHERE e.slug = ? AND substr(e.event_date, 1, 10) = ?
              AND LOWER(COALESCE(o.strategy, '')) = 'posterior_settlement'
              AND (
                  LOWER(COALESCE(e.state, '')) IN ('entry', 'entry_pending', 'exit_pending')
                  OR COALESCE(e.position_size, 0) > 0.000001
              )
            """,
            (slug, event_date),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        bucket = str(row["outcome"] or "").strip()
        side = str(row["side"] or "").upper()
        size = _float(row["position_size"] or row["entry_size"])
        entry_price = _float(row["avg_price"])
        if entry_price is None:
            entry_price = _float(row["entry_price"])
        entry_notional = (
            float(size) * float(entry_price)
            if size is not None and size > 0 and entry_price is not None and entry_price > 0
            else None
        )
        if bucket and side in {"YES", "NO"}:
            out.append(
                {
                    "bucket": bucket,
                    "side": side,
                    "position_size": size,
                    "entry_price": entry_price,
                    "entry_notional_usdc": entry_notional,
                }
            )
    return out


def _strong_physical_evidence(payload: Mapping[str, Any], ssot: Mapping[str, Any]) -> Dict[str, Any]:
    """Read an explicit composer-owned physical support verdict only.

    The trading layer must not reverse-engineer weather from raw METAR or
    trajectory fields.  Missing verdicts deliberately keep both legs HOLD.
    """
    candidates = (
        ssot.get("strong_physical_evidence"),
        ssot.get("strong_observation_evidence"),
        payload.get("strong_physical_evidence"),
    )
    for value in candidates:
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _second_yes_entry_window_state(
    *, ssot: Mapping[str, Any], decision_as_of: str
) -> Dict[str, Any]:
    """Project the same L3 peak-timing contract used by the unique planner.

    Missing timing closes only the second-YES entry path.  It must not block
    top YES, strongly excluded NO, posterior publication, or AMSC ingestion.
    """

    realization = {
        "posterior_run_id": str(ssot.get("posterior_run_id") or ""),
        "realization_state_id": str(ssot.get("realization_state_id") or ""),
        "peak_timing_state": dict(ssot.get("peak_timing_state") or {})
        if isinstance(ssot.get("peak_timing_state"), Mapping)
        else {},
    }
    state = build_second_yes_entry_window_state(
        realization_state=realization,
        decision_as_of=decision_as_of,
    )
    if state.get("ok") is True and str(state.get("second_yes_entry_window_state_id") or ""):
        return state
    core = {
        "schema": "second_yes_entry_window_state_v1",
        "ok": False,
        "state": "blocked",
        "posterior_run_id": realization["posterior_run_id"],
        "realization_state_id": realization["realization_state_id"],
        "decision_as_of": str(decision_as_of or ""),
        "entry_allowed": False,
        "entry_block_reason": "authoritative_peak_timing_or_identity_unavailable",
        "block_reasons": list(state.get("block_reasons") or []),
        "existing_same_bucket_position_may_be_retained": True,
        "window_close_is_not_target_invalidation": True,
        "uses_ev_edge_lcb": False,
        "places_orders": False,
    }
    # The identity of a refusal is what was refused, not the moment it was
    # refused.  decision_as_of stays on the block for the reader, but hashing
    # it made this id change on every call -- and the id is part of the plan
    # id, which is what the job dedupe matches on.  With it in the hash the
    # dedupe could never fire, so an unchanged posterior enqueued a fresh buy
    # job every cycle.
    identity_material = {key: value for key, value in core.items() if key != "decision_as_of"}
    core["second_yes_entry_window_state_id"] = "secondyeswindow_" + hashlib.sha256(
        repr(sorted(identity_material.items())).encode("utf-8")
    ).hexdigest()[:24]
    return core


def _three_leg_plan(
    *, distribution: List[Dict[str, Any]], ssot: Mapping[str, Any], payload: Mapping[str, Any],
    event_date: str, run_date_et: str, fact_snapshot_id: str, posterior_run_id: str, computed_at: str,
    managed_positions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    settlement_window_open = ssot.get("settlement_window_open") is True
    second_yes_window = _second_yes_entry_window_state(
        ssot=ssot,
        decision_as_of=computed_at,
    )
    three_leg = build_strong_posterior_three_leg_strategy(
        posterior_buckets=distribution,
        strong_physical_evidence=_strong_physical_evidence(payload, ssot),
        settlement_window_open=settlement_window_open,
        available_cash_usdc=_available_cash_usdc(),
    )
    if three_leg.get("ok") is not True:
        return {"ok": False, "version": "strong_posterior_three_leg_strategy_v2", "reason": (three_leg.get("block_reasons") or ["strong_posterior_three_leg_strategy_blocked"])[0]}
    legs: List[Dict[str, Any]] = []
    for raw in three_leg.get("legs") or []:
        leg = dict(raw)
        leg["role"] = str(leg.get("strategy_leg") or "")
        leg["target_notional"] = float(leg.get("target_notional_usdc") or 0.0)
        leg["action"] = "add" if leg.get("action") == "BUY" else "hold"
        if (
            leg["role"] == "second_yes"
            and second_yes_window.get("entry_allowed") is not True
        ):
            leg["action"] = "hold"
            leg["block_reason"] = str(
                second_yes_window.get("entry_block_reason")
                or "second_yes_entry_window_closed"
            )
            leg["entry_allowed"] = False
            leg["existing_same_bucket_position_may_be_retained"] = True
        elif leg["role"] == "second_yes":
            leg["entry_allowed"] = True
        legs.append(leg)
    top = next((row for row in legs if row.get("strategy_leg") == "top_yes_wave"), {})
    second = next((row for row in legs if row.get("strategy_leg") == "second_yes"), {})
    excluded = next((row for row in legs if row.get("strategy_leg") == "strongly_excluded_no"), {})
    plan_id_material = "|".join((
        str(fact_snapshot_id or ""), str(posterior_run_id or ""), str(event_date or ""),
        str(top.get("bucket") or ""), str(second.get("bucket") or ""),
        str(excluded.get("bucket") or ""),
        str(second_yes_window.get("second_yes_entry_window_state_id") or ""),
    ))
    desired_keys = {
        (str(leg.get("bucket") or ""), str(leg.get("side") or "").upper())
        for leg in legs
        if str(leg.get("bucket") or "")
        and str(leg.get("strategy_leg") or "") in {"top_yes_wave", "second_yes", "strongly_excluded_no"}
    }
    positions_by_key = {
        (str(position.get("bucket") or ""), str(position.get("side") or "").upper()): position
        for position in managed_positions
    }
    # A current posterior revision is a desired allocation, not a fresh
    # invitation to buy the cap again.  Reconcile each continuing leg to the
    # cost already allocated to it.  Missing cost basis intentionally blocks
    # additional exposure rather than risking a cap breach.
    trim_legs: List[Dict[str, Any]] = []
    allocation_epsilon = 0.01
    for leg in legs:
        key = (str(leg.get("bucket") or ""), str(leg.get("side") or "").upper())
        position = positions_by_key.get(key)
        if not position:
            leg["current_notional_usdc"] = 0.0
            leg["delta_notional"] = float(leg.get("target_notional") or 0.0)
            continue
        current_notional = _float(position.get("entry_notional_usdc"))
        # Reconciliation uses the role's durable cap, not this revision's
        # quote-dependent order amount.  A temporarily missing quote must not
        # turn a still-valid $20 top YES (or $5 second/NO) into a zero target
        # and force an erroneous sell.
        target_notional = max(
            0.0,
            _float(leg.get("target_notional_cap_usdc"))
            or _float(leg.get("max_notional_usdc"))
            or _float(leg.get("target_notional"))
            or 0.0,
        )
        leg["reconciliation_target_notional_usdc"] = target_notional
        leg["current_position_size"] = _float(position.get("position_size"))
        leg["current_entry_price"] = _float(position.get("entry_price"))
        leg["current_notional_usdc"] = current_notional
        if current_notional is None:
            leg["action"] = "hold"
            leg["block_reason"] = "managed_position_cost_basis_missing"
            leg["delta_notional"] = None
            continue
        delta_notional = target_notional - current_notional
        leg["delta_notional"] = delta_notional
        if delta_notional > allocation_epsilon and str(leg.get("action") or "").strip().lower() == "add":
            leg["action"] = "add"
        else:
            leg["action"] = "hold"
            leg["block_reason"] = "target_allocation_already_filled"
        if delta_notional < -allocation_epsilon:
            entry_price = _float(position.get("entry_price"))
            position_size = _float(position.get("position_size"))
            if entry_price is None or entry_price <= 0 or position_size is None or position_size <= 0:
                leg["block_reason"] = "managed_position_trim_cost_basis_missing"
                continue
            trim_legs.append(
                {
                    "bucket": key[0],
                    "side": key[1],
                    "position_size": min(position_size, (-delta_notional) / entry_price),
                    "role": "dynamic_posterior_reselection_trim",
                    "reason_code": "target_realization_dynamic_posterior_reselection_sell",
                    "reason": "dynamic_posterior_reselection_current_leg_allocation_above_current_role_cap",
                }
            )
    sell_old_legs: List[Dict[str, Any]] = []
    for position in managed_positions:
        key = (str(position.get("bucket") or ""), str(position.get("side") or "").upper())
        if not key[0] or key[1] not in {"YES", "NO"} or key in desired_keys:
            continue
        sell_old_legs.append(
            {
                "bucket": key[0],
                "side": key[1],
                "position_size": _float(position.get("position_size")),
                "role": "dynamic_posterior_reselection_exit",
                "reason_code": "target_realization_dynamic_posterior_reselection_sell",
                "reason": "dynamic_posterior_reselection_no_longer_current_three_leg_member",
            }
        )
    sell_old_legs.extend(trim_legs)
    return {
        "ok": True,
        "version": "strong_posterior_three_leg_strategy_v2",
        "strategy_family": "strong_posterior_top_yes_second_yes_and_strongly_excluded_no",
        "decision_ssot": "posterior_ssot_fact",
        "probability_recomputed": False,
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": posterior_run_id,
        "event_date": event_date,
        "run_date_et": run_date_et,
        "computed_at": computed_at,
        "plan_id": "strong-posterior-" + hashlib.sha256(plan_id_material.encode("utf-8")).hexdigest()[:24],
        # This is a revision identity for one committed posterior fact, not a
        # frozen strategy selection.  Every newer posterior fact produces a
        # fresh selection and is reconciled against the currently managed
        # three legs before any new BUY is considered.
        "selection_policy": "dynamic_reselect_on_every_posterior_ssot_fact",
        "selection_is_frozen": False,
        "selection_revision_identity": {
            "fact_snapshot_id": fact_snapshot_id,
            "posterior_run_id": posterior_run_id,
        },
        "target_bucket": str(top.get("bucket") or ""),
        "target_posterior": _float(top.get("posterior")),
        "second_target_bucket": str(second.get("bucket") or ""),
        "second_target_posterior": _float(second.get("posterior")),
        "strongly_excluded_no_bucket": str(excluded.get("bucket") or ""),
        "strongly_excluded_no_posterior": _float(excluded.get("posterior")),
        "settlement_window_open": settlement_window_open,
        "second_yes_entry_window_state_id": str(
            second_yes_window.get("second_yes_entry_window_state_id") or ""
        ),
        "second_yes_entry_window_state": dict(second_yes_window),
        "legs": legs,
        "sell_old_legs": sell_old_legs,
        "sell_first_required": bool(sell_old_legs),
        "transition_state": "SELL_OLD" if sell_old_legs else "READY_FOR_ENTRY",
        "execution_price_policy": {"order_type": "GTC", "post_only": True, "buy_price_cap": None, "market_orders_allowed": False},
        "hard_constraints": ["posterior_ssot_fact_only", "strong_physical_evidence_required", "three_legs_only", "second_yes_final_30_minutes_only", "second_yes_notional_strictly_below_5_usdc", "strongly_excluded_no_100_usdc", "trajectory_no_retired", "no_ev_edge_lcb"],
        "boundary": {"remote_no_absent": True, "second_yes_present": True, "second_yes_window_closure_forbids_only_new_or_incremental_buy": True, "second_yes_existing_same_bucket_position_may_be_retained": True, "trajectory_no_absent": True, "dynamic_bucket_selection_per_posterior_fact": True, "top_yes_wave_exit_target_dynamic": True, "writes_orders": False},
    }


def build_target_realization_v3_decision_plan(
    conn: sqlite3.Connection,
    *,
    snapshot_payload: Mapping[str, Any],
    slug: str,
    event_date: str,
    computed_at: str = "",
) -> Dict[str, Any]:
    ssot = snapshot_payload.get("posterior_ssot_fact")
    ssot = ssot if isinstance(ssot, Mapping) else {}
    rows = ssot.get("bucket_probabilities") or ssot.get("ranked_bucket_probabilities") or []
    if not isinstance(rows, list):
        return {
            "ok": False,
            "version": "strong_posterior_three_leg_strategy_v2",
            "reason": "posterior_distribution_missing",
        }
    labels = [
        str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
        for row in rows
        if isinstance(row, Mapping)
    ]
    labels = [label for label in labels if label]
    ticks, tokens = _bulk_market_state(
        conn,
        slug=slug,
        event_date=event_date,
        labels=labels,
    )
    distribution: List[Dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        bucket = str(raw.get("bucket") or raw.get("outcome") or raw.get("label") or "").strip()
        probability = _probability(raw.get("probability"))
        if not bucket or probability is None:
            continue
        yes_tick = ticks.get((bucket, "YES")) or {}
        no_tick = ticks.get((bucket, "NO")) or {}
        token = tokens.get(bucket) or {}
        distribution.append(
            {
                "bucket": bucket,
                "probability": probability,
                "hard_zero": bool(raw.get("hard_zero") or _float(raw.get("hard_mask")) == 0.0),
                "yes_ask": yes_tick.get("best_ask"),
                "yes_bid": yes_tick.get("best_bid"),
                "yes_ask_depth": yes_tick.get("ask_depth_topN"),
                "yes_bid_depth": yes_tick.get("bid_depth_topN"),
                "no_ask": no_tick.get("best_ask"),
                "no_bid": no_tick.get("best_bid"),
                "no_ask_depth": no_tick.get("ask_depth_topN"),
                "no_bid_depth": no_tick.get("bid_depth_topN"),
                "yes_token": token.get("yes_token") or "",
                "no_token": token.get("no_token") or "",
            }
        )
    plan = _three_leg_plan(
        distribution=distribution, ssot=ssot, payload=snapshot_payload, event_date=event_date,
        run_date_et=_date_key(snapshot_payload.get("run_date")),
        fact_snapshot_id=str(ssot.get("fact_snapshot_id") or snapshot_payload.get("fact_snapshot_id") or ""),
        posterior_run_id=str(ssot.get("posterior_run_id") or snapshot_payload.get("posterior_run_id") or ""),
        computed_at=computed_at,
        managed_positions=_dynamic_strategy_positions(
            conn, slug=slug, event_date=event_date
        ),
    )
    if plan.get("ok") is True:
        plan["source"] = "posterior_target_direct_db_snapshot"
        plan["target_bucket_selection_source"] = "posterior_ssot_fact.highest_probability_bucket"
    return plan
