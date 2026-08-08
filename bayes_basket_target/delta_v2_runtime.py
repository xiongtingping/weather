"""Pure production adapter for the unique DeltaBasketScore v2 objective.

The adapter deliberately reuses only the probability/support diagnostics from
the basket state machine.  It never calls the v1 optimizer or strict-delta
authorization path.  Every target and action candidate is bounded by the
activated v2 artifact, official WS depth, confirmed inventory/order state and
the caller-provided fill-distribution authority.
"""

from __future__ import annotations

from itertools import product
import math
from typing import Any, Callable, Mapping, Sequence

from .basket_kernel.contracts import (
    EPS,
    ContractError,
    Direction,
    TargetBasketPolicy,
    TargetBasketSnapshot,
    content_sha256,
)
from .basket_kernel.delta_v2 import (
    evaluate_action_v2,
    evaluate_basket_value_v2,
    select_action_v2,
    select_complete_maker_bundle_v2,
    solve_reservation_price_v2,
    solve_target_inventory_v2,
)
from .basket_kernel.support import (
    _choose_support,
    _derive_dual_risk_scope,
    probability_center_diagnostics,
)
from .basket_kernel.state_machine import resolve_basket_state
from .delta_v2_artifact import DeltaV2PolicyArtifact


SCHEMA = "delta_basket_score_runtime_plan_v2"
MAKER_EXECUTION_CONTRACT = "maker_post_only"
FOK_EXECUTION_CONTRACT = "single_bucket_fok"
FillResolver = Callable[..., Mapping[str, Any]]


def _floor_to_tick(price: float, tick: float) -> float:
    steps = math.floor((price + EPS) / tick)
    return round(steps * tick, 12)


def _maker_market_anchor(*, bid: float, ask: float, tick: float) -> float | None:
    """Return the most competitive non-crossing official-WS Maker price."""

    highest_post_only = _floor_to_tick(ask - tick, tick)
    if highest_post_only <= EPS:
        return None
    improve_bid = _floor_to_tick(bid + tick, tick)
    anchor = min(highest_post_only, improve_bid)
    if anchor + EPS < bid:
        return None
    return anchor


def _ask_depth_purchase(
    *,
    levels: Sequence[Any],
    cash_limit: float,
) -> tuple[float, float]:
    """Conservatively price target inventory against official ask depth."""

    remaining = cash_limit
    shares = 0.0
    spent = 0.0
    for level in levels:
        if remaining <= EPS:
            break
        level_cash = float(level.price) * float(level.shares)
        take_cash = min(remaining, level_cash)
        shares += take_cash / float(level.price)
        spent += take_cash
        remaining -= take_cash
    return shares, spent


def _reservation_grid(*, row: Any, artifact: DeltaV2PolicyArtifact) -> tuple[float, ...]:
    """Bind economic inversion to the live spread without making it a quote."""

    tick = float(row.price_tick_size)
    highest_post_only = _floor_to_tick(float(row.ask_price) - tick, tick)
    if highest_post_only <= EPS:
        return ()
    market_points = {
        _floor_to_tick(float(row.bid_price), tick),
        _floor_to_tick(float(row.bid_price) + tick, tick),
        highest_post_only,
    }
    values = {
        _floor_to_tick(float(price), tick)
        for price in (*artifact.price_grid, *market_points)
        if float(price) > EPS and float(price) <= highest_post_only + EPS
    }
    return tuple(sorted(price for price in values if price > EPS))


def _target_candidates(
    *,
    snapshot: TargetBasketSnapshot,
    desired: Sequence[str],
    artifact: DeltaV2PolicyArtifact,
    capital_available_usdc: float,
    minimum_clip_notional_usdc: float,
) -> list[dict[str, Any]]:
    """Build a bounded q-grid priced from official WS ask depth."""

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    current_shares = {row.bucket: row.filled_shares for row in snapshot.buckets}
    current_cost = {row.bucket: row.filled_cost_usdc for row in snapshot.buckets}
    pending_shares = {row.bucket: row.pending_shares for row in snapshot.buckets}
    pending_cost = {
        row.bucket: row.pending_notional_usdc for row in snapshot.buckets
    }
    ordered = tuple(sorted(desired, key=lambda bucket: (by_bucket[bucket].ordinal, bucket)))
    per_bucket_levels: list[tuple[float, ...]] = []
    quote_levels: dict[str, dict[float, tuple[float, float]]] = {}
    for bucket in ordered:
        row = by_bucket[bucket]
        quote_levels[bucket] = {}
        affordable: list[float] = []
        for clip in artifact.cash_clip_grid:
            if clip > capital_available_usdc + EPS:
                continue
            shares, spent = _ask_depth_purchase(
                levels=row.ask_levels,
                cash_limit=float(clip),
            )
            if (
                shares <= EPS
                or spent + EPS < float(clip)
            ):
                continue
            quote_levels[bucket][float(clip)] = (shares, spent)
            affordable.append(float(clip))
        per_bucket_levels.append((0.0, *affordable))
    candidate_count = math.prod(len(levels) for levels in per_bucket_levels)
    if candidate_count > 4096:
        raise ContractError("delta_v2_target_candidate_bound_exceeded")
    candidates: list[dict[str, Any]] = []
    for clips in product(*per_bucket_levels):
        if sum(clips) > capital_available_usdc + EPS:
            continue
        is_baseline = all(clip <= EPS for clip in clips)
        if not is_baseline and any(
            (
                current_shares[bucket]
                + pending_shares[bucket]
                <= EPS
                and clip <= EPS
            )
            for bucket, clip in zip(ordered, clips)
        ):
            # A basket activation is atomic at the target level: either every
            # selected support bucket receives a positive target, or the
            # optimizer compares against the no-new-action baseline and
            # HOLDS.  Every positive gap is emitted in one complete Maker
            # intent bundle; exchange fills may arrive independently, but no
            # proper subset is an authorized initial action.
            continue
        shares = {
            bucket: current_shares[bucket] + pending_shares[bucket]
            for bucket in current_shares
        }
        costs = {
            bucket: current_cost[bucket] + pending_cost[bucket]
            for bucket in current_cost
        }
        for bucket, clip in zip(ordered, clips):
            if clip <= EPS:
                continue
            acquired_shares, acquired_cost = quote_levels[bucket][float(clip)]
            shares[bucket] += acquired_shares
            costs[bucket] += acquired_cost
        candidates.append({
            "candidate_id": "q-" + content_sha256({
                "ordered_support": ordered,
                "clips": clips,
                "quote_version": snapshot.quote_version,
            })[:24],
            "shares_by_bucket": shares,
            "cost_by_bucket": costs,
            "pricing_basis": "official_ws_ask_depth",
            "candidate_kind": (
                "NO_NEW_ACTION_BASELINE"
                if is_baseline
                else "COMPLETE_BASKET_TARGET"
            ),
        })
    if not candidates:
        raise ContractError("delta_v2_target_candidate_set_empty")
    return candidates


def _hard_gates(
    snapshot: TargetBasketSnapshot,
    *,
    positive_gap: bool,
    budget_ok: bool,
    reservation_price_ok: bool = True,
    resource_probe_ok: bool,
) -> dict[str, bool]:
    return {
        "unique_ssot": True,
        "lineage_complete": bool(
            snapshot.evidence.posterior_sha256
            and snapshot.quote_version
            and snapshot.inventory_version
            and snapshot.order_state_version
        ),
        "registry_timezone_matches": True,
        "bucket_contract_matches": snapshot.contract_matches,
        "official_ws_fresh": snapshot.evidence_fresh,
        "inventory_known": True,
        "order_state_known": snapshot.order_state_known,
        "positive_inventory_gap": positive_gap,
        "budget_ok": budget_ok,
        "profit_floor_ok": True,
        "resource_probe_ok": resource_probe_ok,
        "reservation_price_ok": reservation_price_ok,
    }


def compile_delta_v2_runtime_plan(
    *,
    snapshot: TargetBasketSnapshot,
    basket_policy: TargetBasketPolicy,
    delta_policy: DeltaV2PolicyArtifact,
    fill_resolver: FillResolver,
    capital_available_usdc: float,
    minimum_profit_floor: float,
    resource_probe_ok: bool,
    regime_direction_allowed: bool = True,
    confirmed_cancel_for_fok: bool = False,
) -> dict[str, Any]:
    """Compile the single-event q* and best no-live action under v2."""

    basket_policy.validate()
    if not resource_probe_ok:
        raise ContractError("delta_v2_resource_probe_failed")
    if delta_policy.station_id not in {"*", snapshot.station_id}:
        raise ContractError("delta_v2_policy_station_mismatch")
    if delta_policy.stage not in {"*", snapshot.evidence.probability_stage}:
        raise ContractError("delta_v2_policy_stage_mismatch")
    (
        center,
        direction,
        base,
        bias,
        edge,
        desired,
        scores,
        candidate_lower,
        candidate_upper,
        expansion_layers,
        core_transition,
    ) = _choose_support(snapshot, basket_policy)
    shortfall, breakthrough = _derive_dual_risk_scope(snapshot, center)
    # Risk families classify already-admitted support; they never bypass the
    # continuous-core candidate boundary or its evidence/price/Maker gates.
    shortfall &= desired
    breakthrough &= desired
    if not desired:
        raise ContractError("delta_v2_support_empty")
    (
        diagnosed_center,
        runner_up,
        center_lead,
        center_stable,
    ) = probability_center_diagnostics(
        {row.bucket: row.probability for row in snapshot.buckets},
        {row.bucket: row.ordinal for row in snapshot.buckets},
        basket_policy,
    )
    # The diagnostic center is the probability argmax.  It is the anchor only
    # for stages that publish no point estimate of their own; when one exists
    # the anchor is deliberately not the argmax, because an open-ended edge
    # bucket spans an unbounded interval and outranks the 1°C buckets it is
    # compared against without the forecast pointing anywhere near it.  Keeping
    # the equality unconditional would reject exactly the cases the anchor was
    # introduced to fix (ZUUU: anchor 33°C, argmax "35°C or higher").  The
    # runner-up/lead/stability diagnostics below remain informative either way.
    if (
        snapshot.evidence.model_center_ordinal is None
        and diagnosed_center != center.bucket
    ):
        raise ContractError("delta_v2_center_diagnostic_mismatch")
    probabilities = {row.bucket: row.probability for row in snapshot.buckets}
    by_bucket = {row.bucket: row for row in snapshot.buckets}
    ordered_support = tuple(
        sorted(desired, key=lambda bucket: (by_bucket[bucket].ordinal, bucket))
    )
    target_solution = solve_target_inventory_v2(
        probabilities=probabilities,
        feasible_inventory_candidates=_target_candidates(
            snapshot=snapshot,
            desired=ordered_support,
            artifact=delta_policy,
            capital_available_usdc=capital_available_usdc,
            minimum_clip_notional_usdc=basket_policy.clip_min_notional_usdc,
        ),
        protected_buckets=ordered_support,
        shortfall_buckets=tuple(sorted(shortfall & desired)),
        breakthrough_buckets=tuple(sorted(breakthrough & desired)),
        weights=delta_policy.value_weights,
    )
    target_shares = dict(target_solution["target_shares_by_bucket"])
    current_shares = {row.bucket: row.filled_shares for row in snapshot.buckets}
    current_cost = {row.bucket: row.filled_cost_usdc for row in snapshot.buckets}
    pending_shares = {row.bucket: row.pending_shares for row in snapshot.buckets}
    pending_cost = {
        row.bucket: row.pending_notional_usdc for row in snapshot.buckets
    }
    basket_target_activated = (
        target_solution.get("winner_candidate_kind")
        == "COMPLETE_BASKET_TARGET"
    )
    final_support = ordered_support if basket_target_activated else ()
    execution_gap_support = tuple(
        bucket
        for bucket in final_support
        if (
            target_shares[bucket]
            - current_shares[bucket]
            - pending_shares[bucket]
        )
        > EPS
    )
    event_id = f"{snapshot.station_id}-{snapshot.event_date}"
    actions: list[dict[str, Any]] = []
    candidate_descriptors: list[tuple[str, float, float, str]] = []
    maker_candidate_diagnostics: dict[str, str] = {
        bucket: "official_ws_ask_unavailable"
        for bucket in ordered_support
        if not by_bucket[bucket].ask_available
    }
    for bucket in execution_gap_support:
        row = by_bucket[bucket]
        if not row.ask_available:
            maker_candidate_diagnostics[bucket] = (
                "official_ws_ask_unavailable"
            )
            continue
        gap = (
            target_shares[bucket]
            - current_shares[bucket]
            - pending_shares[bucket]
        )
        if gap <= EPS:
            continue
        reservation_grid = _reservation_grid(row=row, artifact=delta_policy)
        maker_anchor = _maker_market_anchor(
            bid=row.bid_price,
            ask=row.ask_price,
            tick=row.price_tick_size,
        )
        if maker_anchor is None:
            maker_candidate_diagnostics[bucket] = (
                "maker_post_only_price_unavailable_at_min_tick"
                if row.ask_price <= row.price_tick_size + EPS
                else "maker_post_only_market_anchor_unavailable"
            )
        elif not reservation_grid:
            maker_candidate_diagnostics[bucket] = (
                "maker_reservation_grid_empty"
            )
        for clip in delta_policy.cash_clip_grid:
            if (
                clip > capital_available_usdc + EPS
                or not reservation_grid
                or maker_anchor is None
            ):
                continue
            action_clip = min(float(clip), gap * maker_anchor)
            if action_clip <= EPS:
                continue
            for timeout in delta_policy.timeout_grid_seconds:
                candidate_descriptors.append(
                    (bucket, action_clip, maker_anchor, f"MAKER:{timeout}")
                )
        # FOK is intentionally a single zero-time candidate family and is
        # admitted only in the existing urgency window with sufficient depth.
        if (
            confirmed_cancel_for_fok
            and snapshot.time_left_minutes
            <= basket_policy.taker_time_threshold_minutes + EPS
        ):
            for clip in delta_policy.cash_clip_grid:
                if clip > capital_available_usdc + EPS:
                    continue
                shares, spent = _ask_depth_purchase(
                    levels=row.ask_levels,
                    cash_limit=float(clip),
                )
                if spent + EPS >= clip and shares <= gap + EPS:
                    candidate_descriptors.append(
                        (bucket, clip, row.ask_price, "FOK:0")
                    )
    if len(candidate_descriptors) > delta_policy.execution_weights.maximum_candidates:
        raise ContractError("delta_v2_action_candidate_bound_exceeded")
    for bucket, clip, price, mode_timeout in candidate_descriptors:
        mode, timeout_raw = mode_timeout.split(":", 1)
        timeout = float(timeout_raw)
        row = by_bucket[bucket]
        initial_fill = fill_resolver(
            station_id=snapshot.station_id,
            stage=snapshot.evidence.probability_stage,
            bucket=bucket,
            order_mode=mode,
            limit_price=price,
            timeout_seconds=timeout,
            queue_ahead_shares=row.maker_queue_ahead_shares,
            spread=max(0.0, row.ask_price - row.bid_price),
            depth=row.executable_depth_shares,
            quote_version=snapshot.quote_version,
        )
        distribution = initial_fill.get("fill_distribution")
        if not isinstance(distribution, Sequence):
            raise ContractError("delta_v2_fill_distribution_missing")
        costs = initial_fill.get("execution_costs")
        if not isinstance(costs, Mapping):
            raise ContractError("delta_v2_execution_costs_missing")
        action_kwargs = {
            "event_id": event_id,
            "bucket": bucket,
            "cash_clip": clip,
            "timeout_seconds": timeout,
            "order_mode": mode,
            "fill_distribution": distribution,
            "probabilities": probabilities,
            "current_shares_by_bucket": current_shares,
            "current_cost_by_bucket": current_cost,
            "confirmed_pending_shares_by_bucket": pending_shares,
            "confirmed_pending_cost_by_bucket": pending_cost,
            "target_shares_by_bucket": target_shares,
            "protected_buckets": ordered_support,
            "shortfall_buckets": tuple(sorted(shortfall & desired)),
            "breakthrough_buckets": tuple(sorted(breakthrough & desired)),
            "value_weights": delta_policy.value_weights,
            "execution_weights": delta_policy.execution_weights,
            "hard_gates": _hard_gates(
                snapshot,
                positive_gap=True,
                budget_ok=clip <= capital_available_usdc + EPS,
                resource_probe_ok=resource_probe_ok,
            ),
            "capital_available_usdc": capital_available_usdc,
            "minimum_profit_floor": minimum_profit_floor,
            "reservation_price": max(delta_policy.price_grid),
            "wait_cost": costs.get("wait", 0.0),
            "queue_risk": costs.get("queue", 0.0),
            "adverse_selection": costs.get("adverse_selection", 0.0),
            "reprice_risk": costs.get("reprice", 0.0),
            "capital_lock_cost": costs.get("capital_lock", 0.0),
            "fill_artifact_content_sha256": initial_fill.get(
                "fill_artifact_content_sha256"
            ),
            "fee_rate": basket_policy.fee_rate,
        }
        price_grid = (
            _reservation_grid(row=row, artifact=delta_policy)
            if mode == "MAKER"
            else tuple(
                sorted(
                    set(
                        price
                        for price in (*delta_policy.price_grid, row.ask_price)
                        if price + EPS >= row.ask_price
                    )
                )
            )
        )
        reservation = solve_reservation_price_v2(
            cash_clip=clip,
            price_grid=price_grid,
            action_kwargs={
                key: value
                for key, value in action_kwargs.items()
                if key not in {"cash_clip", "limit_price"}
            },
        )
        reserve = reservation.get("reservation_price")
        if reserve is None and mode != "MAKER":
            continue
        # A Maker leg may be individually negative while the complete basket
        # is jointly positive because floor/CVaR/concentration are nonlinear.
        # Keep the non-crossing market anchor as a bounded bundle candidate;
        # only the joint bundle selector may authorize it.
        reservation_price = (
            float(reserve) if reserve is not None else float(price)
        )
        if mode == "MAKER":
            maker_limit = min(
                float(price),
                _floor_to_tick(reservation_price, row.price_tick_size),
            )
            clip = min(float(clip), gap * maker_limit)
            if (
                clip <= EPS
                or maker_limit + EPS < row.bid_price
                or maker_limit >= row.ask_price - EPS
            ):
                continue
            price = maker_limit
            action_kwargs["cash_clip"] = clip
        elif (
            price > reservation_price + EPS
            or clip / price > gap + EPS
        ):
            continue
        resolved_fill = fill_resolver(
            station_id=snapshot.station_id,
            stage=snapshot.evidence.probability_stage,
            bucket=bucket,
            order_mode=mode,
            limit_price=price,
            timeout_seconds=timeout,
            queue_ahead_shares=row.maker_queue_ahead_shares,
            spread=max(0.0, row.ask_price - row.bid_price),
            depth=row.executable_depth_shares,
            quote_version=snapshot.quote_version,
        )
        final_distribution = resolved_fill.get("fill_distribution")
        final_costs = resolved_fill.get("execution_costs")
        if not isinstance(final_distribution, Sequence):
            raise ContractError("delta_v2_fill_distribution_missing")
        if not isinstance(final_costs, Mapping):
            raise ContractError("delta_v2_execution_costs_missing")
        action_kwargs.update({
            "fill_distribution": final_distribution,
            "reservation_price": reservation_price,
            "limit_price": price,
            "wait_cost": final_costs.get("wait", 0.0),
            "queue_risk": final_costs.get("queue", 0.0),
            "adverse_selection": final_costs.get("adverse_selection", 0.0),
            "reprice_risk": final_costs.get("reprice", 0.0),
            "capital_lock_cost": final_costs.get("capital_lock", 0.0),
            "fill_artifact_content_sha256": resolved_fill.get(
                "fill_artifact_content_sha256"
            ),
        })
        actions.append(evaluate_action_v2(**action_kwargs))
    maker_actions = [
        action for action in actions
        if str(dict(action.get("action") or {}).get("order_mode") or "").upper()
        == "MAKER"
    ]
    maker_action_buckets = {
        str(dict(action.get("action") or {}).get("bucket") or "")
        for action in maker_actions
    }
    for bucket in execution_gap_support:
        if bucket not in maker_action_buckets:
            maker_candidate_diagnostics.setdefault(
                bucket,
                "maker_candidate_rejected_by_reservation_or_size",
            )
    selection = select_complete_maker_bundle_v2(
        maker_actions,
        required_buckets=execution_gap_support,
        probabilities=probabilities,
        current_shares_by_bucket=current_shares,
        current_cost_by_bucket=current_cost,
        target_shares_by_bucket=target_shares,
        protected_buckets=ordered_support,
        shortfall_buckets=tuple(sorted(shortfall & desired)),
        breakthrough_buckets=tuple(sorted(breakthrough & desired)),
        value_weights=delta_policy.value_weights,
        execution_weights=delta_policy.execution_weights,
        capital_available_usdc=capital_available_usdc,
        minimum_profit_floor=minimum_profit_floor,
        fee_rate=basket_policy.fee_rate,
    )
    # Urgent FOK remains a single-bucket close-out mechanism.  It is never
    # allowed to coexist with a Maker bundle or bypass an incomplete basket.
    if (
        selection.get("authorized") is not True
        and len(execution_gap_support) == 1
        and confirmed_cancel_for_fok
    ):
        fok_selection = select_action_v2(
            [
                action for action in actions
                if str(
                    dict(action.get("action") or {}).get("order_mode") or ""
                ).upper() == "FOK"
            ],
            execution_weights=delta_policy.execution_weights,
        )
        fok_selected = fok_selection["selected_action"]
        if fok_selected.get("authorized") is True:
            selection = {
                "schema": "delta_basket_bundle_selection_v2",
                "required_buckets": list(execution_gap_support),
                "selected_actions": [fok_selected],
                "selected_bundle": None,
                "selected_action_sha256s": [
                    str(fok_selected.get("content_sha256") or "")
                ],
                "selected_score": float(
                    fok_selected.get("delta_basket_score_v2") or 0.0
                ),
                "capital_reserved_usdc": float(
                    fok_selected.get("capital_reserved_usdc") or 0.0
                ),
                "basket_complete": True,
                "authorized": True,
                "blockers": [],
                "hold_selected": False,
                "execution_mode": "SINGLE_BUCKET_FOK",
                "calls_order_gateway": False,
                "places_orders": False,
                "orders_submitted": 0,
                "live_submit_authorized": False,
                "content_sha256": content_sha256({
                    "schema": "delta_basket_bundle_selection_v2",
                    "selected_action_sha256s": [
                        str(fok_selected.get("content_sha256") or "")
                    ],
                    "execution_mode": "SINGLE_BUCKET_FOK",
                }),
            }
    if not regime_direction_allowed and selection.get("authorized") is True:
        selection = {
            **selection,
            "selected_actions": [],
            "selected_bundle": None,
            "selected_action_sha256s": [],
            "selected_score": 0.0,
            "capital_reserved_usdc": 0.0,
            "basket_complete": False,
            "authorized": False,
            "blockers": ["regime_direction_not_allowed"],
            "hold_selected": True,
        }
    if snapshot.terminal and selection.get("authorized") is True:
        selection = {
            **selection,
            "selected_actions": [],
            "selected_bundle": None,
            "selected_action_sha256s": [],
            "selected_score": 0.0,
            "capital_reserved_usdc": 0.0,
            "basket_complete": False,
            "authorized": False,
            "blockers": ["terminal_stop_adding"],
            "hold_selected": True,
        }
    realised = evaluate_basket_value_v2(
        probabilities=probabilities,
        shares_by_bucket=current_shares,
        cost_by_bucket=current_cost,
        protected_buckets=ordered_support,
        shortfall_buckets=tuple(sorted(shortfall & desired)),
        breakthrough_buckets=tuple(sorted(breakthrough & desired)),
        weights=delta_policy.value_weights,
        target_shares_by_bucket=target_shares,
    )
    targets: list[dict[str, Any]] = []
    selected_evaluations = [
        dict(row) for row in selection.get("selected_actions", ())
        if isinstance(row, Mapping)
    ]
    selected_actions_by_bucket = {
        str(dict(row.get("action") or {}).get("bucket") or ""): dict(
            row.get("action") or {}
        )
        for row in selected_evaluations
    }
    selected_buckets = tuple(sorted(
        bucket for bucket in selected_actions_by_bucket if bucket
    ))
    if set(selected_buckets) - set(execution_gap_support):
        raise ContractError(
            "delta_v2_selected_action_outside_execution_gap_support"
        )
    if selection.get("authorized") is True and set(selected_buckets) != set(
        execution_gap_support
    ):
        raise ContractError("delta_v2_selected_bundle_incomplete")
    for bucket in final_support:
        row = by_bucket[bucket]
        is_selected = (
            selection.get("authorized") is True
            and bucket in selected_actions_by_bucket
        )
        action = selected_actions_by_bucket.get(bucket, {}) if is_selected else {}
        targets.append({
            "bucket": bucket,
            "ordinal": row.ordinal,
            "role": (
                "BASE" if bucket in base else "BIAS" if bucket in bias else "EDGE"
            ),
            "base_score": scores.get(bucket, (0.0, 0.0, 0.0))[0],
            "bias_score": scores.get(bucket, (0.0, 0.0, 0.0))[1],
            "edge_score": scores.get(bucket, (0.0, 0.0, 0.0))[2],
            "target_shares": target_shares[bucket],
            "filled_shares": row.filled_shares,
            "filled_cost_usdc": row.filled_cost_usdc,
            "pending_shares": row.pending_shares,
            "pending_notional_usdc": row.pending_notional_usdc,
            "delta_shares": max(
                0.0,
                target_shares[bucket] - row.filled_shares - row.pending_shares,
            ),
            "clip_shares": float(action.get("maximum_shares") or 0.0),
            "clip_notional_usdc": float(action.get("cash_clip") or 0.0),
            "candidate_price": float(action.get("limit_price") or 0.0),
            "limit_price": float(action.get("limit_price") or 0.0),
            "effective_price": float(action.get("limit_price") or 0.0),
            "execution_candidate": (
                "TAKER_FOK"
                if action.get("order_mode") == "FOK"
                else "MAKER"
                if action.get("order_mode") == "MAKER"
                else "NONE"
            ),
            "execution_contract": (
                FOK_EXECUTION_CONTRACT
                if action.get("order_mode") == "FOK"
                else MAKER_EXECUTION_CONTRACT
                if action.get("order_mode") == "MAKER"
                else ""
            ),
            "post_only": action.get("order_mode") == "MAKER",
            "cancel_acknowledged": (
                bool(confirmed_cancel_for_fok)
                if action.get("order_mode") == "FOK"
                else False
            ),
            "target_price_id": (
                "delta_v2_price_"
                + str(
                    next(
                        (
                            value.get("content_sha256")
                            for value in selected_evaluations
                            if str(
                                dict(value.get("action") or {}).get("bucket")
                                or ""
                            )
                            == bucket
                        ),
                        "",
                    )
                )[:32]
                if is_selected
                else ""
            ),
            "execution_reason_codes": (
                [] if is_selected else ["delta_v2_action_not_selected"]
            ),
        })
    reason_codes: list[str] = []
    if selection.get("authorized") is not True:
        reason_codes.extend(str(value) for value in selection.get("blockers", ()))
        reason_codes.extend(
            f"{reason}:{bucket}"
            for bucket, reason in sorted(
                maker_candidate_diagnostics.items()
            )
        )
        reason_codes.append("delta_v2_complete_basket_hold_selected")
    if not center_stable:
        reason_codes.append("center_probability_lead_unstable")
    if not regime_direction_allowed:
        reason_codes.append("regime_direction_not_allowed")
    delta_v2_audit = {
        "schema": "delta_basket_score_authorization_v2",
        "selected_actions": selected_evaluations,
        "selected_bundle_sha256": selection.get("selected_bundle_sha256"),
        "authorized": selection.get("authorized") is True,
        "blockers": list(selection.get("blockers", ())),
        "delta_basket_score_v2": float(
            selection.get("selected_score", 0.0)
        ),
        "policy_artifact_id": delta_policy.artifact_id,
        "policy_artifact_sha256": delta_policy.content_sha256,
        "fill_distribution_artifact_sha256": (
            delta_policy.fill_distribution_artifact_sha256
        ),
        "realised_value_sha256": realised["content_sha256"],
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    delta_v2_audit["content_sha256"] = content_sha256(delta_v2_audit)
    selected_before = tuple(
        row.bucket for row in snapshot.buckets if row.selected_before
    )
    ordered_core = tuple(
        str(bucket)
        for bucket in core_transition["next_core_buckets"]
    )
    basket_state = resolve_basket_state(
        bucket_ordinals={
            row.bucket: row.ordinal for row in snapshot.buckets
        },
        desired_support=final_support,
        selected_before=selected_before,
        positive_gap_buckets=execution_gap_support,
        execution_authorized=selection.get("authorized") is True,
        order_state_known=snapshot.order_state_known,
        stop_adding=snapshot.terminal,
    )
    identity = {
        "schema": SCHEMA,
        "snapshot_sha256": snapshot.content_sha256,
        "quote_version": snapshot.quote_version,
        "inventory_version": snapshot.inventory_version,
        "order_state_version": snapshot.order_state_version,
        "station_id": snapshot.station_id,
        "event_date": snapshot.event_date,
        "observed_at": snapshot.observed_at,
        "market_id": snapshot.market_id,
        "state": "TRADE" if selection.get("authorized") is True else "HOLD",
        "basket_state": basket_state.state.value,
        "basket_state_reason": basket_state.reason,
        "basket_state_transition": basket_state.to_dict(),
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "center_bucket": center.bucket,
        "runner_up_bucket": runner_up,
        "center_lead_probability": center_lead,
        "center_lead_threshold_probability": (
            basket_policy.center_lead_min_probability
        ),
        "center_stable": center_stable,
        "direction": direction.value if isinstance(direction, Direction) else str(direction),
        "diagnostic_lower_ordinal": snapshot.evidence.model_envelope_lower_ordinal,
        "diagnostic_upper_ordinal": snapshot.evidence.model_envelope_upper_ordinal,
        "candidate_lower_ordinal": candidate_lower,
        "candidate_upper_ordinal": candidate_upper,
        "disagreement_expansion_layers": expansion_layers,
        "underreach_risk_buckets": sorted(shortfall),
        "upside_breakthrough_risk_buckets": sorted(breakthrough),
        "current_core_buckets": list(snapshot.current_core_buckets),
        "current_direction": (
            snapshot.current_direction.value
            if snapshot.current_direction is not None
            else None
        ),
        "core_buckets": list(ordered_core),
        "core_transition": core_transition,
        "candidate_support": list(ordered_support),
        "final_support": list(final_support),
        "basket_completion_required": True,
        "basket_target_activated": basket_target_activated,
        "execution_gap_support": list(execution_gap_support),
        "selected_buckets": list(selected_buckets),
        "excluded_candidate_buckets": [
            {
                "bucket": bucket,
                "reason": "zero_joint_target_inventory",
            }
            for bucket in ordered_support
            if bucket not in final_support
        ],
        "selected_before": list(selected_before),
        "targets": targets,
        "target_solution": target_solution,
        "delta_v2_action_selection": selection,
        "maker_candidate_diagnostics": dict(
            sorted(maker_candidate_diagnostics.items())
        ),
        "delta_v2_audit": delta_v2_audit,
        "scenario_pnl_usdc": realised["scenario_pnl"],
        "expected_pnl_usdc": realised["expected_pnl"],
        "profit_floor_usdc": realised["profit_floor"],
        "cvar_usdc": realised["cvar"],
        "total_target_shares": sum(target_shares.values()),
        "total_delta_notional_usdc": float(
            selection.get("capital_reserved_usdc") or 0.0
        ),
        "basket_optimizer_id": "DeltaBasketScoreV2",
        "basket_optimizer_converged": True,
        "fok_cancel_acknowledged": bool(confirmed_cancel_for_fok),
        "target_only": True,
        "places_orders": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }
    return {
        **identity,
        "plan_id": "delta_v2_plan_" + content_sha256(identity)[:32],
        "content_sha256": content_sha256(identity),
    }


__all__ = ["SCHEMA", "compile_delta_v2_runtime_plan"]
