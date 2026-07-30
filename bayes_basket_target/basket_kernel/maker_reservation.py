"""Bounded Maker price/wait optimizer for the Target basket kernel.

The module is deliberately a pure function boundary.  It has no market,
clock, database, order or promotion side effects.  A candidate is evaluated
against the same ``J0`` and ``J1`` through :func:`evaluate_delta_objective`.
Pending Maker inventory is never passed to that objective: it is returned as
``confirmed_pending`` execution state only.

The optimizer searches an explicit tick grid and an explicit timeout grid.
It does not derive a price from ``best_bid`` or ``best_ask - tick``.  The
chosen price is the highest-scoring admissible pair, with deterministic tie
breakers.  This makes low/high/low quote paths and midnight time wrap replay
without hidden clock or quote-state mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ContractError, EPS, content_sha256
from .objective import ObjectiveWeights, evaluate_delta_objective
from ..maker_grid_oos_artifact import (
    GRID_FIELDS,
    MAKER_GRID_OOS_ARTIFACT_SCHEMA,
    SCOPE_FIELDS,
    grid_point_key,
    validate_maker_grid_oos_artifact,
)


MAKER_RESERVATION_SCHEMA = "target_maker_reservation_solution_v1"
MAKER_RESERVATION_VERSION = "maker_price_wait_optimizer_v1"
_UTC = timezone.utc


def _number(name: str, value: Any, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool):
        raise ContractError(f"maker_reservation_{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"maker_reservation_{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"maker_reservation_{name}_invalid")
    if nonnegative and result < 0.0:
        raise ContractError(f"maker_reservation_{name}_negative")
    return result


def _positive(name: str, value: Any) -> float:
    result = _number(name, value)
    if result <= EPS:
        raise ContractError(f"maker_reservation_{name}_nonpositive")
    return result


def _probability(name: str, value: Any) -> float:
    result = _number(name, value)
    if result > 1.0 + EPS:
        raise ContractError(f"maker_reservation_{name}_outside_unit_interval")
    return min(1.0, result)


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"maker_reservation_{name}_missing")
    return result


def _timestamp(value: Any) -> datetime:
    text = _text("timestamp", value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("maker_reservation_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError("maker_reservation_timestamp_timezone_required")
    return parsed.astimezone(_UTC)


def elapsed_seconds_with_wrap(
    start: Any,
    end: Any,
    *,
    period_seconds: float = 86_400.0,
) -> float:
    """Return a deterministic non-negative elapsed duration.

    Full timezone-aware ISO timestamps use their real date and therefore do
    not need a modulo operation.  Numeric clock offsets are treated as a
    repeating clock, which makes ``23:59 -> 00:01`` deterministic.
    """

    period = _positive("wrap_period_seconds", period_seconds)
    if isinstance(start, (int, float)) and not isinstance(start, bool) and isinstance(end, (int, float)) and not isinstance(end, bool):
        first = _number("wrap_start_seconds", start)
        last = _number("wrap_end_seconds", end)
        if first >= period or last >= period:
            raise ContractError("maker_reservation_wrap_clock_out_of_range")
        return (last - first) % period
    first_dt = _timestamp(start)
    last_dt = _timestamp(end)
    delta = (last_dt - first_dt).total_seconds()
    if delta < -EPS:
        raise ContractError("maker_reservation_timestamp_moved_backwards")
    return max(0.0, delta)


def _quote_digest(row: Mapping[str, Any]) -> str:
    identity = {
        "best_bid_price": row.get("best_bid_price"),
        "best_ask_price": row.get("best_ask_price"),
        "queue_ahead_shares": row.get("queue_ahead_shares", 0.0),
        "quote_version": row.get("quote_version"),
    }
    return content_sha256(identity)


def canonicalize_quote_path(
    quote_path: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Fold official-WS quote observations without repricing empty polls.

    A row marked ``empty_poll`` is an observation, not a new quote.  When
    sequence numbers are present, sequence order wins; otherwise the caller's
    event order is retained (the latter is important for a real low/high/low
    stream).  The final non-empty quote is the only executable quote.
    """

    rows = list(quote_path)
    if not rows:
        return {
            "schema": "target_maker_quote_path_v1",
            "quotes": [],
            "empty_poll_count": 0,
            "transition_prices": [],
            "current_quote": None,
            "content_sha256": content_sha256({"quotes": []}),
        }
    if any(not isinstance(row, Mapping) for row in rows):
        raise ContractError("maker_reservation_quote_path_row_invalid")
    if all(row.get("sequence") is not None for row in rows):
        try:
            rows = sorted(enumerate(rows), key=lambda pair: (int(pair[1]["sequence"]), pair[0]))
        except (TypeError, ValueError) as exc:
            raise ContractError("maker_reservation_quote_sequence_invalid") from exc
        rows = [row for _, row in rows]
    current: dict[str, Any] | None = None
    quotes: list[dict[str, Any]] = []
    transitions: list[float] = []
    empty_polls = 0
    previous_digest: str | None = None
    for raw in rows:
        if raw.get("empty_poll") is True:
            empty_polls += 1
            continue
        if raw.get("best_ask_price") is None:
            raise ContractError("maker_reservation_quote_ask_missing")
        row = {
            "best_bid_price": raw.get("best_bid_price"),
            "best_ask_price": _number("quote_best_ask_price", raw["best_ask_price"]),
            "queue_ahead_shares": _number("quote_queue_ahead_shares", raw.get("queue_ahead_shares", 0.0)),
            "quote_version": raw.get("quote_version"),
            "observed_at": raw.get("observed_at"),
        }
        digest = _quote_digest(row)
        if current is not None and digest == previous_digest:
            continue
        if current is not None:
            transitions.append(row["best_ask_price"] - current["best_ask_price"])
        current = row
        previous_digest = digest
        quotes.append(row)
    identity = {
        "quotes": quotes,
        "empty_poll_count": empty_polls,
        "transition_prices": transitions,
    }
    return {
        "schema": "target_maker_quote_path_v1",
        **identity,
        "current_quote": current,
        "content_sha256": content_sha256(identity),
    }


def maker_repricing_decision(
    previous_quote: Mapping[str, Any] | None,
    current_quote: Mapping[str, Any] | None,
    *,
    empty_poll: bool = False,
) -> dict[str, Any]:
    """Return a pure repricing decision; identical/empty observations HOLD."""

    if empty_poll:
        return {
            "action": "HOLD",
            "reprice": False,
            "reason": "empty_poll_no_reprice",
            "previous_quote_sha256": _quote_digest(previous_quote or {}),
            "current_quote_sha256": _quote_digest(current_quote or {}),
        }
    previous_sha = _quote_digest(previous_quote or {})
    current_sha = _quote_digest(current_quote or {})
    changed = previous_sha != current_sha
    return {
        "action": "REPRICE" if changed else "HOLD",
        "reprice": changed,
        "reason": "quote_changed" if changed else "quote_unchanged",
        "previous_quote_sha256": previous_sha,
        "current_quote_sha256": current_sha,
    }


def _mapping_terms(raw: Mapping[str, Any] | None) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        return {}
    allowed = {
        "fill_probability",
        "queue_cost_per_filled_share",
        "nonfill_opportunity_cost_per_share",
        "adverse_selection_cost_per_filled_share",
        "wait_cost_per_filled_share",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ContractError("maker_reservation_cost_terms_unknown_fields")
    result: dict[str, float] = {}
    for key in allowed & set(raw):
        result[key] = (
            _probability(key, raw[key])
            if key == "fill_probability"
            else _number(key, raw[key])
        )
    return result


def _lookup_terms(raw: Mapping[Any, Any] | None, key: float) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        return {}
    candidates = (key, str(key), f"{key:g}")
    value: Any = None
    for candidate in candidates:
        if candidate in raw:
            value = raw[candidate]
            break
    if value is None:
        return {}
    return _mapping_terms(value)


def _artifact_metrics(
    artifact: Any,
    *,
    formal_activation_eligible: bool,
    expected_scope: Mapping[str, Any] | None,
    as_of_utc: str | None,
) -> tuple[dict[str, float], str | None, str | None]:
    if artifact is None:
        if formal_activation_eligible:
            return {}, None, "maker_grid_oos_formal_artifact_required"
        return {}, None, None
    if formal_activation_eligible:
        return (
            {},
            _artifact_sha256(artifact),
            "maker_grid_oos_formal_artifact_required",
        )
    if hasattr(artifact, "metrics"):
        raw_metrics = artifact.metrics
        artifact_sha = getattr(artifact, "content_sha256", None)
    elif isinstance(artifact, Mapping):
        raw_metrics = artifact.get("metrics", artifact)
        artifact_sha = artifact.get("content_sha256")
    else:
        return {}, None, "maker_reservation_artifact_invalid"
    try:
        metrics = _mapping_terms(raw_metrics)
    except ContractError as exc:
        return {}, None, str(exc)
    required = {
        "fill_probability",
        "queue_cost_per_filled_share",
        "nonfill_opportunity_cost_per_share",
        "adverse_selection_cost_per_filled_share",
        "wait_cost_per_filled_share",
    }
    if not required <= set(metrics):
        return {}, artifact_sha, "maker_grid_oos_cost_metrics_incomplete"
    return metrics, str(artifact_sha) if artifact_sha else None, None


def _weights(value: ObjectiveWeights | Mapping[str, Any]) -> ObjectiveWeights:
    if isinstance(value, ObjectiveWeights):
        return value
    if not isinstance(value, Mapping):
        raise ContractError("maker_reservation_objective_weights_required")
    required = (
        "lambda_profit_floor",
        "lambda_downside",
        "lambda_shortfall",
        "lambda_breakthrough",
        "lambda_concentration",
        "lambda_execution",
    )
    if not set(required) <= set(value):
        raise ContractError("maker_reservation_objective_weights_incomplete")
    return ObjectiveWeights(**{
        field: _number(f"objective_{field}", value[field])
        for field in required
    })


def _insurance_contract_ok(
    audit: Mapping[str, Any],
    contract: Mapping[str, Any] | None,
    *,
    epsilon_score: float,
) -> tuple[bool, str | None]:
    if float(audit["delta_expected_pnl"]) > epsilon_score:
        return True, "positive_expected_pnl"
    if not isinstance(contract, Mapping) or contract.get("enabled") is not True:
        return False, "positive_ev_or_insurance_required"
    if contract.get("oos_activated") is not True:
        return False, "insurance_oos_not_activated"
    if contract.get("risk_scope_ok") is not True:
        return False, "insurance_risk_scope_invalid"
    protection = max(
        float(audit.get("delta_profit_floor", 0.0)),
        -float(audit.get("delta_protected_tail_loss", 0.0)),
    )
    if protection <= epsilon_score:
        return False, "insurance_protection_not_improved"
    subsidy = _number("insurance_subsidy", contract.get("subsidy_usdc", 0.0))
    absolute_cap = _number("insurance_absolute_cap", contract.get("absolute_cap_usdc", 0.0))
    ratio_cap = _number("insurance_ratio_cap", contract.get("ratio_cap", 0.0))
    positive_delta_ev = _number(
        "insurance_positive_delta_ev",
        contract.get("positive_delta_ev_usdc", 0.0),
    )
    if subsidy > min(absolute_cap, ratio_cap * positive_delta_ev) + EPS:
        return False, "insurance_subsidy_cap_exceeded"
    return True, "limited_oos_insurance"


def _price_grid(
    *,
    tick: float,
    best_ask: float,
    maximum_price: float,
    minimum_price: float,
    explicit: Sequence[Any] | None,
) -> list[float]:
    if explicit is not None:
        raw_prices = list(explicit)
    else:
        upper = min(best_ask, maximum_price)
        max_index = int(math.floor((upper - EPS) / tick))
        min_index = int(math.ceil((minimum_price - EPS) / tick))
        raw_prices = [index * tick for index in range(min_index, max_index + 1)]
    prices = sorted({round(_number("candidate_price", value), 12) for value in raw_prices})
    valid = [
        price for price in prices
        if price + EPS >= minimum_price
        and price + EPS < best_ask
        and price <= maximum_price + EPS
        and abs((price / tick) - round(price / tick)) <= 1e-7
    ]
    if not valid:
        raise ContractError("maker_reservation_post_only_price_grid_empty")
    return valid


def _is_exact_grid_artifact(artifact: Any) -> bool:
    return isinstance(artifact, Mapping) and str(artifact.get("schema") or "") == MAKER_GRID_OOS_ARTIFACT_SCHEMA


def _formal_exact_grid_scope(
    expected_scope: Mapping[str, Any] | None,
    *,
    include_point_bins: bool,
) -> dict[str, str]:
    if not isinstance(expected_scope, Mapping):
        raise ContractError("maker_grid_oos_formal_scope_required")
    scope: dict[str, str] = {}
    fields = SCOPE_FIELDS if include_point_bins else SCOPE_FIELDS[:5]
    for field in fields:
        scope[field] = _text(f"formal_scope_{field}", expected_scope.get(field))
    return scope


def _formal_expected_grid(
    *,
    expected_grid: Sequence[Mapping[str, Any]] | None,
    expected_scope: Mapping[str, Any] | None,
    prices: Sequence[float],
    tick: float,
    timeouts: Sequence[float],
) -> tuple[list[dict[str, Any]], dict[tuple[float, float, float], tuple[Any, ...]]]:
    """Build the exact point identities consumed by the formal solver.

    ``price_distance_bin`` belongs to a price point, not to the whole curve.
    Callers may therefore provide the complete expected grid with per-point
    bins.  The five stable cycle dimensions remain identical across every
    point.  A sparse grid, duplicate price/timeout pair, neighbouring point,
    or implicit cross-product completion is rejected.
    """

    common_scope = _formal_exact_grid_scope(
        expected_scope,
        include_point_bins=False,
    )
    if expected_grid is None:
        point_scope = _formal_exact_grid_scope(
            expected_scope,
            include_point_bins=True,
        )
        canonical = [
            {
                "candidate_price": price,
                "price_tick": tick,
                "timeout_seconds": timeout,
                **point_scope,
            }
            for timeout in timeouts
            for price in prices
        ]
    else:
        if isinstance(expected_grid, (str, bytes)) or not isinstance(
            expected_grid,
            Sequence,
        ):
            raise ContractError("maker_grid_oos_expected_grid_sequence_required")
        canonical = []
        for raw in expected_grid:
            if not isinstance(raw, Mapping):
                raise ContractError("maker_grid_oos_expected_grid_point_invalid")
            try:
                key = grid_point_key(raw)
            except ContractError:
                raise
            point = dict(zip(GRID_FIELDS, key, strict=True))
            for field, value in common_scope.items():
                if point[field] != value:
                    raise ContractError(
                        f"maker_grid_oos_exact_scope_mismatch:{field}"
                    )
            canonical.append(point)

    expected_pairs = {
        (price, tick, timeout)
        for timeout in timeouts
        for price in prices
    }
    point_identity_by_pair: dict[
        tuple[float, float, float],
        tuple[Any, ...],
    ] = {}
    for point in canonical:
        pair = (
            float(point["candidate_price"]),
            float(point["price_tick"]),
            float(point["timeout_seconds"]),
        )
        if pair in point_identity_by_pair:
            raise ContractError("maker_grid_oos_duplicate_price_timeout_point")
        point_identity_by_pair[pair] = tuple(point[field] for field in GRID_FIELDS)
    if set(point_identity_by_pair) != expected_pairs:
        raise ContractError("maker_grid_oos_price_timeout_grid_mismatch")
    return canonical, point_identity_by_pair


def _exact_grid_point_identity(point: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(point[field] for field in GRID_FIELDS)


def _artifact_sha256(raw: Any) -> str | None:
    if hasattr(raw, "content_sha256"):
        sha = getattr(raw, "content_sha256")
        return str(sha) if sha else None
    if isinstance(raw, Mapping):
        sha = raw.get("content_sha256")
        return str(sha) if sha else None
    return None


def solve_maker_reservation(
    *,
    probabilities: Mapping[str, Any],
    filled_shares_by_bucket: Mapping[str, Any],
    filled_cost_by_bucket: Mapping[str, Any],
    bucket: str,
    requested_shares: float,
    protected_buckets: Sequence[str],
    weights: ObjectiveWeights | Mapping[str, Any],
    concentration_scale_usdc: float,
    price_tick_size: float,
    best_ask_price: float | None = None,
    maximum_limit_price: float = 0.99,
    minimum_price: float | None = None,
    explicit_price_grid: Sequence[Any] | None = None,
    timeout_candidates_seconds: Sequence[Any] | None = None,
    timeout_seconds: float | None = None,
    max_timeout_seconds: float | None = None,
    time_remaining_seconds: float | None = None,
    artifact: Any = None,
    oos_artifact: Any = None,
    expected_scope: Mapping[str, Any] | None = None,
    expected_grid: Sequence[Mapping[str, Any]] | None = None,
    as_of_utc: str | None = None,
    formal_activation_eligible: bool = True,
    cost_terms: Mapping[str, Any] | None = None,
    cost_terms_by_timeout: Mapping[Any, Mapping[str, Any]] | None = None,
    cost_terms_by_price: Mapping[Any, Mapping[str, Any]] | None = None,
    fee_rate: float = 0.0,
    baseline_execution_risk: float = 0.0,
    shortfall_buckets: Sequence[str] = (),
    breakthrough_buckets: Sequence[str] = (),
    insurance_contract: Mapping[str, Any] | None = None,
    epsilon_score: float = 1e-6,
    confirmed_pending_shares: float = 0.0,
    confirmed_pending_notional: float = 0.0,
    best_bid_price: float | None = None,
    quote_path: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Solve one deterministic post-only price/timeout pair.

    ``artifact`` is the formal OOS artifact when formal activation is being
    attempted.  ``cost_terms`` is accepted only for counterfactual/no-live
    work and never makes that result promotion eligible.  Timeout and price
    term maps are explicit deterministic scenario inputs; the function does
    not estimate them online.
    """

    if artifact is not None and oos_artifact is not None:
        raise ContractError("maker_reservation_artifact_alias_ambiguous")
    artifact = artifact if artifact is not None else oos_artifact
    pending_shares = _number("confirmed_pending_shares", confirmed_pending_shares)
    pending_notional = _number("confirmed_pending_notional", confirmed_pending_notional)
    score_epsilon = _positive("epsilon_score", epsilon_score)
    requested = _positive("requested_shares", requested_shares)
    tick = _positive("price_tick_size", price_tick_size)
    best_ask = best_ask_price
    path_audit = None
    if quote_path is not None:
        path_audit = canonicalize_quote_path(quote_path)
        current = path_audit["current_quote"]
        if current is not None:
            best_ask = current["best_ask_price"]
    if best_ask is None:
        return _hold("maker_reservation_quote_missing", pending_shares, pending_notional)
    best_ask = _positive("best_ask_price", best_ask)
    maximum = _number("maximum_limit_price", maximum_limit_price)
    minimum = tick if minimum_price is None else _positive("minimum_price", minimum_price)
    if minimum > maximum + EPS:
        return _hold("maker_reservation_price_range_empty", pending_shares, pending_notional)
    if max_timeout_seconds is not None:
        max_timeout = _positive("max_timeout_seconds", max_timeout_seconds)
    else:
        max_timeout = None
    if timeout_candidates_seconds is None:
        if timeout_seconds is None:
            raise ContractError("maker_reservation_timeout_grid_required")
        raw_timeouts = [timeout_seconds]
    else:
        raw_timeouts = list(timeout_candidates_seconds)
        if timeout_seconds is not None:
            raw_timeouts.append(timeout_seconds)
    timeouts = sorted({_positive("timeout_seconds", value) for value in raw_timeouts})
    if max_timeout is not None:
        timeouts = [value for value in timeouts if value <= max_timeout + EPS]
    if time_remaining_seconds is not None:
        remaining = _number("time_remaining_seconds", time_remaining_seconds)
        timeouts = [value for value in timeouts if value <= remaining + EPS]
    if not timeouts:
        return _hold("maker_reservation_timeout_range_empty", pending_shares, pending_notional)
    weight_object = _weights(weights)
    price_grid = _price_grid(
        tick=tick,
        best_ask=best_ask,
        maximum_price=maximum,
        minimum_price=minimum,
        explicit=explicit_price_grid,
    )
    if formal_activation_eligible and _is_exact_grid_artifact(artifact):
        artifact_sha = _artifact_sha256(artifact)
        try:
            formal_grid, point_identity_by_pair = _formal_expected_grid(
                expected_grid=expected_grid,
                expected_scope=expected_scope,
                prices=price_grid,
                tick=tick,
                timeouts=timeouts,
            )
            validated_grid = validate_maker_grid_oos_artifact(
                artifact,
                expected_grid=artifact.get("grid_spec"),
                as_of_utc=as_of_utc,
            )
        except (ContractError, TypeError, ValueError) as exc:
            return _hold(str(exc), pending_shares, pending_notional, artifact_sha)
        point_lookup = {
            _exact_grid_point_identity(point): point
            for point in validated_grid["points"]
        }
        missing_formal_points = [
            identity
            for identity in point_identity_by_pair.values()
            if identity not in point_lookup
        ]
        if missing_formal_points:
            return _hold(
                "maker_grid_oos_point_missing",
                pending_shares,
                pending_notional,
                validated_grid.content_sha256,
            )
        evaluations: list[dict[str, Any]] = []
        for timeout in timeouts:
            for price in price_grid:
                point = point_lookup.get(
                    point_identity_by_pair[(price, tick, timeout)]
                )
                if point is None:
                    return _hold(
                        "maker_grid_oos_point_missing",
                        pending_shares,
                        pending_notional,
                        validated_grid.content_sha256,
                    )
                point_curve = point["curve"]
                required_curve_fields = (
                    "fill_probability",
                    "queue_cost_per_filled_share",
                    "nonfill_opportunity_cost_per_share",
                    "adverse_selection_cost_per_filled_share",
                    "wait_cost_per_filled_share",
                )
                point_terms = {
                    key: point_curve[key]
                    for key in required_curve_fields
                    if key in point_curve
                }
                if len(point_terms) != len(required_curve_fields):
                    return _hold(
                        "maker_grid_oos_cost_metrics_incomplete",
                        pending_shares,
                        pending_notional,
                        validated_grid.content_sha256,
                    )
                probability = _probability("fill_probability", point_terms["fill_probability"])
                queue = _number("queue_cost_per_filled_share", point_terms["queue_cost_per_filled_share"])
                nonfill = _number("nonfill_opportunity_cost_per_share", point_terms["nonfill_opportunity_cost_per_share"])
                adverse = _number("adverse_selection_cost_per_filled_share", point_terms["adverse_selection_cost_per_filled_share"])
                wait = _number("wait_cost_per_filled_share", point_terms["wait_cost_per_filled_share"])
                expected_filled = requested * probability
                unit_cost = price * (1.0 + _number("fee_rate", fee_rate)) + queue + adverse + wait
                expected_cost = expected_filled * unit_cost
                execution_risk = requested * (
                    probability * (queue + adverse + wait)
                    + (1.0 - probability) * nonfill
                )
                action_shares = {key: 0.0 for key in probabilities}
                action_cost = {key: 0.0 for key in probabilities}
                if bucket not in action_shares:
                    raise ContractError("maker_reservation_bucket_unknown")
                action_shares[bucket] = expected_filled
                action_cost[bucket] = expected_cost
                audit = evaluate_delta_objective(
                    probabilities=probabilities,
                    filled_shares_by_bucket=filled_shares_by_bucket,
                    filled_cost_by_bucket=filled_cost_by_bucket,
                    action_shares_by_bucket=action_shares,
                    action_cost_by_bucket=action_cost,
                    protected_buckets=protected_buckets,
                    shortfall_buckets=shortfall_buckets,
                    breakthrough_buckets=breakthrough_buckets,
                    weights=weight_object,
                    baseline_execution_risk=baseline_execution_risk,
                    action_execution_risk=execution_risk,
                    concentration_scale_usdc=concentration_scale_usdc,
                )
                insurance_ok, contract_reason = _insurance_contract_ok(
                    audit,
                    insurance_contract,
                    epsilon_score=score_epsilon,
                )
                authorized = (
                    float(audit["delta_basket_score"]) > score_epsilon
                    and insurance_ok
                )
                evaluations.append({
                    "price": price,
                    "timeout_seconds": timeout,
                    "grid_point_key": tuple(point[field] for field in GRID_FIELDS),
                    "grid_point_sha256": content_sha256(
                        {
                            "schema": "maker_grid_oos_point_key_v1",
                            **{field: point[field] for field in GRID_FIELDS},
                        }
                    ),
                    "curve_sha256": point["curve_sha256"],
                    "scope_sha256": point["scope_sha256"],
                    "fill_probability": probability,
                    "expected_filled_shares": expected_filled,
                    "expected_cost_usdc": expected_cost,
                    "queue_cost_per_filled_share": queue,
                    "nonfill_opportunity_cost_per_share": nonfill,
                    "adverse_selection_cost_per_filled_share": adverse,
                    "wait_cost_per_filled_share": wait,
                    "confirmed_pending_shares": pending_shares,
                    "confirmed_pending_notional": pending_notional,
                    "delta_basket_score": float(audit["delta_basket_score"]),
                    "delta_expected_pnl": float(audit["delta_expected_pnl"]),
                    "delta_profit_floor": float(audit["delta_profit_floor"]),
                    "scenario_pnl": dict(audit["proposed"]["scenario_pnl"]),
                    "contract_ok": insurance_ok,
                    "contract_reason": contract_reason,
                    "authorized": authorized,
                    "objective_content_sha256": audit["content_sha256"],
                })
        eligible = [row for row in evaluations if row["authorized"]]
        if not eligible:
            result = _hold(
                "maker_reservation_no_positive_delta",
                pending_shares,
                pending_notional,
                validated_grid.content_sha256,
            )
            result["evaluations"] = evaluations
            result["quote_path"] = path_audit
            result["formal_activation_eligible"] = True
            result["artifact_curve_sha256"] = validated_grid.curve_sha256
            return _seal(result)
        selected = sorted(
            eligible,
            key=lambda row: (-row["delta_basket_score"], row["price"], row["timeout_seconds"]),
        )[0]
        result = {
            "schema": MAKER_RESERVATION_SCHEMA,
            "version": MAKER_RESERVATION_VERSION,
            "status": "AUTHORIZED",
            "authorized": True,
            "will_order": False,
            "promotion_eligible": True,
            "formal_activation_eligible": True,
            "reservation_price": selected["price"],
            "timeout_seconds": selected["timeout_seconds"],
            "selected": selected,
            "evaluations": evaluations,
            "artifact_sha256": validated_grid.content_sha256,
            "artifact_curve_sha256": validated_grid.curve_sha256,
            "confirmed_pending": {
                "shares": pending_shares,
                "notional_usdc": pending_notional,
                "enters_scenario_pnl": False,
                "enters_profit_floor": False,
            },
            "best_bid_price_observed": best_bid_price,
            "fixed_tick_subtract": False,
            "copied_best_bid": False,
            "calls_gateway": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "quote_path": path_audit,
        }
        return _seal(result)
    metrics, artifact_sha, artifact_error = _artifact_metrics(
        artifact,
        formal_activation_eligible=formal_activation_eligible,
        expected_scope=expected_scope,
        as_of_utc=as_of_utc,
    )
    if artifact_error is not None and formal_activation_eligible:
        return _hold(artifact_error, pending_shares, pending_notional, artifact_sha)
    base_terms = dict(metrics)
    base_terms.update(_mapping_terms(cost_terms))
    required = {
        "fill_probability",
        "queue_cost_per_filled_share",
        "nonfill_opportunity_cost_per_share",
        "adverse_selection_cost_per_filled_share",
        "wait_cost_per_filled_share",
    }
    if not required <= set(base_terms):
        return _hold("maker_reservation_cost_terms_missing", pending_shares, pending_notional, artifact_sha)
    price_grid = _price_grid(
        tick=tick,
        best_ask=best_ask,
        maximum_price=maximum,
        minimum_price=minimum,
        explicit=explicit_price_grid,
    )
    evaluations: list[dict[str, Any]] = []
    for timeout in timeouts:
        timeout_terms = _lookup_terms(cost_terms_by_timeout, timeout)
        for price in price_grid:
            terms = dict(base_terms)
            terms.update(timeout_terms)
            terms.update(_lookup_terms(cost_terms_by_price, price))
            probability = _probability("fill_probability", terms["fill_probability"])
            queue = _number("queue_cost_per_filled_share", terms["queue_cost_per_filled_share"])
            nonfill = _number("nonfill_opportunity_cost_per_share", terms["nonfill_opportunity_cost_per_share"])
            adverse = _number("adverse_selection_cost_per_filled_share", terms["adverse_selection_cost_per_filled_share"])
            wait = _number("wait_cost_per_filled_share", terms["wait_cost_per_filled_share"])
            expected_filled = requested * probability
            unit_cost = price * (1.0 + _number("fee_rate", fee_rate)) + queue + adverse + wait
            expected_cost = expected_filled * unit_cost
            execution_risk = requested * (
                probability * (queue + adverse + wait)
                + (1.0 - probability) * nonfill
            )
            action_shares = dict(probabilities)
            action_cost = dict(probabilities)
            action_shares = {key: 0.0 for key in action_shares}
            action_cost = {key: 0.0 for key in action_cost}
            if bucket not in action_shares:
                raise ContractError("maker_reservation_bucket_unknown")
            action_shares[bucket] = expected_filled
            action_cost[bucket] = expected_cost
            audit = evaluate_delta_objective(
                probabilities=probabilities,
                filled_shares_by_bucket=filled_shares_by_bucket,
                filled_cost_by_bucket=filled_cost_by_bucket,
                action_shares_by_bucket=action_shares,
                action_cost_by_bucket=action_cost,
                protected_buckets=protected_buckets,
                shortfall_buckets=shortfall_buckets,
                breakthrough_buckets=breakthrough_buckets,
                weights=weight_object,
                baseline_execution_risk=baseline_execution_risk,
                action_execution_risk=execution_risk,
                concentration_scale_usdc=concentration_scale_usdc,
            )
            insurance_ok, contract_reason = _insurance_contract_ok(
                audit,
                insurance_contract,
                epsilon_score=score_epsilon,
            )
            authorized = (
                float(audit["delta_basket_score"]) > score_epsilon
                and insurance_ok
            )
            evaluations.append({
                "price": price,
                "timeout_seconds": timeout,
                "fill_probability": probability,
                "expected_filled_shares": expected_filled,
                "expected_cost_usdc": expected_cost,
                "queue_cost_per_filled_share": queue,
                "nonfill_opportunity_cost_per_share": nonfill,
                "adverse_selection_cost_per_filled_share": adverse,
                "wait_cost_per_filled_share": wait,
                "confirmed_pending_shares": pending_shares,
                "confirmed_pending_notional": pending_notional,
                "delta_basket_score": float(audit["delta_basket_score"]),
                "delta_expected_pnl": float(audit["delta_expected_pnl"]),
                "delta_profit_floor": float(audit["delta_profit_floor"]),
                "scenario_pnl": dict(audit["proposed"]["scenario_pnl"]),
                "contract_ok": insurance_ok,
                "contract_reason": contract_reason,
                "authorized": authorized,
                "objective_content_sha256": audit["content_sha256"],
            })
    eligible = [row for row in evaluations if row["authorized"]]
    if not eligible:
        result = _hold("maker_reservation_no_positive_delta", pending_shares, pending_notional, artifact_sha)
        result["evaluations"] = evaluations
        result["quote_path"] = path_audit
        result["formal_activation_eligible"] = bool(formal_activation_eligible)
        return _seal(result)
    selected = sorted(
        eligible,
        key=lambda row: (-row["delta_basket_score"], row["price"], row["timeout_seconds"]),
    )[0]
    result = {
        "schema": MAKER_RESERVATION_SCHEMA,
        "version": MAKER_RESERVATION_VERSION,
        "status": "COUNTERFACTUAL_ONLY",
        "authorized": False,
        "will_order": False,
        "promotion_eligible": False,
        "formal_activation_eligible": False,
        "reservation_price": selected["price"],
        "timeout_seconds": selected["timeout_seconds"],
        "selected": selected,
        "evaluations": evaluations,
        "artifact_sha256": artifact_sha,
        "confirmed_pending": {
            "shares": pending_shares,
            "notional_usdc": pending_notional,
            "enters_scenario_pnl": False,
            "enters_profit_floor": False,
        },
        "best_bid_price_observed": best_bid_price,
        "fixed_tick_subtract": False,
        "copied_best_bid": False,
        "calls_gateway": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "quote_path": path_audit,
    }
    return _seal(result)


def _hold(
    reason: str,
    pending_shares: float,
    pending_notional: float,
    artifact_sha: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": MAKER_RESERVATION_SCHEMA,
        "version": MAKER_RESERVATION_VERSION,
        "status": "HOLD",
        "authorized": False,
        "will_order": False,
        "promotion_eligible": False,
        "reason_codes": [reason],
        "artifact_sha256": artifact_sha,
        "confirmed_pending": {
            "shares": pending_shares,
            "notional_usdc": pending_notional,
            "enters_scenario_pnl": False,
            "enters_profit_floor": False,
        },
        "calls_gateway": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


def _seal(result: dict[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in result.items() if key != "content_sha256"}
    return {**body, "content_sha256": content_sha256(body)}


optimize_maker_reservation = solve_maker_reservation
solve_maker_price_and_wait = solve_maker_reservation
invert_maker_reservation = solve_maker_reservation


__all__ = [
    "MAKER_RESERVATION_SCHEMA",
    "MAKER_RESERVATION_VERSION",
    "canonicalize_quote_path",
    "elapsed_seconds_with_wrap",
    "invert_maker_reservation",
    "maker_repricing_decision",
    "optimize_maker_reservation",
    "solve_maker_price_and_wait",
    "solve_maker_reservation",
]
