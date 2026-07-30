"""Strict incremental basket objective and mode-specific cost curves.

This module is the sole economic authorization contract for a new YES-basket
action.  It compares one immutable state with and without the proposed action;
absolute portfolio scores and top-of-book linear cost approximations are not
accepted here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping, Sequence

from .contracts import (
    EPS,
    BucketObservation,
    ContractError,
    TargetBasketPolicy,
    TargetBasketSnapshot,
    content_sha256,
)
from .objective import ObjectiveWeights, evaluate_delta_objective
from ..maker_grid_oos_artifact import (
    GRID_FIELDS as MAKER_GRID_FIELDS,
    MAKER_GRID_OOS_ARTIFACT_SCHEMA,
    SCOPE_FIELDS as MAKER_GRID_SCOPE_FIELDS,
    MakerGridOOSArtifact,
    grid_point_key,
    scope_sha256 as maker_grid_scope_sha256,
    validate_maker_grid_oos_artifact,
)


STRICT_DELTA_SCHEMA = "strict_delta_basket_objective_v1"
FOK_COST_CURVE_SCHEMA = "official_ws_fok_cost_curve_v1"
MAKER_COST_CURVE_SCHEMA = "oos_maker_expected_cost_curve_v1"
SOLVER_VERSION = "strict_delta_solver_v2"


def _finite(name: str, value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(number):
        raise ContractError(f"{name}_invalid")
    return number


def _nonnegative(name: str, value: Any) -> float:
    number = _finite(name, value)
    if number < 0.0:
        raise ContractError(f"{name}_negative")
    return number


@dataclass(frozen=True)
class StrictDeltaPolicy:
    policy_version: str
    epsilon_score: float
    lambda_profit_floor: float
    lambda_downside: float
    lambda_shortfall: float
    lambda_breakthrough: float
    lambda_concentration: float
    lambda_execution: float
    expected_pnl_policy_floor: float
    subsidy_absolute_cap: float
    subsidy_ratio_cap: float
    coverage_policy_oos_activated: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StrictDeltaPolicy":
        value = cls(
            policy_version=str(raw.get("policy_version") or "").strip(),
            epsilon_score=_nonnegative(
                "strict_delta_epsilon_score",
                raw.get("epsilon_score"),
            ),
            lambda_profit_floor=_nonnegative(
                "strict_delta_lambda_profit_floor",
                raw.get("lambda_profit_floor"),
            ),
            lambda_downside=_nonnegative(
                "strict_delta_lambda_downside",
                raw.get("lambda_downside"),
            ),
            lambda_shortfall=_nonnegative(
                "strict_delta_lambda_shortfall",
                raw.get("lambda_shortfall"),
            ),
            lambda_breakthrough=_nonnegative(
                "strict_delta_lambda_breakthrough",
                raw.get("lambda_breakthrough"),
            ),
            lambda_concentration=_nonnegative(
                "strict_delta_lambda_concentration",
                raw.get("lambda_concentration"),
            ),
            lambda_execution=_nonnegative(
                "strict_delta_lambda_execution",
                raw.get("lambda_execution"),
            ),
            expected_pnl_policy_floor=_finite(
                "strict_delta_expected_pnl_policy_floor",
                raw.get("expected_pnl_policy_floor"),
            ),
            subsidy_absolute_cap=_nonnegative(
                "strict_delta_subsidy_absolute_cap",
                raw.get("subsidy_absolute_cap"),
            ),
            subsidy_ratio_cap=_nonnegative(
                "strict_delta_subsidy_ratio_cap",
                raw.get("subsidy_ratio_cap"),
            ),
            coverage_policy_oos_activated=(
                raw.get("coverage_policy_oos_activated") is True
            ),
        )
        if not value.policy_version:
            raise ContractError("strict_delta_policy_version_missing")
        return value

    @property
    def content_sha256(self) -> str:
        return content_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "strict_delta_policy_v1",
            **asdict(self),
        }


def build_fok_cost_curve(
    item: BucketObservation,
    policy: TargetBasketPolicy,
    *,
    quote_version: str,
) -> dict[str, Any]:
    """Build the exact cumulative all-in FOK curve from official WS asks."""

    segments = []
    cumulative_shares = 0.0
    cumulative_cost = 0.0
    for level in item.ask_levels:
        all_in_unit = (
            level.price * (1.0 + policy.fee_rate)
            + policy.taker_slippage_buffer_per_share
        )
        cumulative_shares += level.shares
        cumulative_cost += level.shares * all_in_unit
        segments.append({
            "exchange_price": level.price,
            "all_in_unit_cost": all_in_unit,
            "level_shares": level.shares,
            "cumulative_shares": cumulative_shares,
            "cumulative_all_in_cost": cumulative_cost,
        })
    identity = {
        "schema": FOK_COST_CURVE_SCHEMA,
        "bucket": item.bucket,
        "quote_version": str(quote_version),
        "fee_rate": policy.fee_rate,
        "slippage_buffer_per_share": (
            policy.taker_slippage_buffer_per_share
        ),
        "segments": segments,
    }
    return {
        **identity,
        "mode": "FOK",
        "content_sha256": content_sha256(identity),
    }


def price_fok_curve(
    curve: Mapping[str, Any],
    shares: float,
) -> dict[str, Any]:
    """Price an all-or-none share amount by walking the cumulative curve."""

    requested = _nonnegative("fok_curve_requested_shares", shares)
    segments = curve.get("segments")
    if (
        curve.get("schema") != FOK_COST_CURVE_SCHEMA
        or not isinstance(segments, Sequence)
        or isinstance(segments, (str, bytes, bytearray))
        or not segments
    ):
        raise ContractError("fok_cost_curve_invalid")
    if str(curve.get("content_sha256") or "") != content_sha256(
        _curve_identity(curve)
    ):
        raise ContractError("cost_curve_content_sha256_mismatch")
    remaining = requested
    cost = 0.0
    gross = 0.0
    consumed = 0
    marginal = 0.0
    cumulative_shares = 0.0
    cumulative_cost = 0.0
    previous_exchange = -1.0
    for raw in segments:
        if not isinstance(raw, Mapping):
            raise ContractError("fok_cost_curve_segment_invalid")
        available = _nonnegative(
            "fok_curve_level_shares",
            raw.get("level_shares"),
        )
        take = min(remaining, available)
        if take <= EPS:
            continue
        unit = _nonnegative(
            "fok_curve_all_in_unit_cost",
            raw.get("all_in_unit_cost"),
        )
        exchange = _nonnegative(
            "fok_curve_exchange_price",
            raw.get("exchange_price"),
        )
        if exchange <= previous_exchange + EPS:
            raise ContractError("fok_cost_curve_prices_not_increasing")
        previous_exchange = exchange
        cumulative_shares += available
        cumulative_cost += available * unit
        if abs(
            _nonnegative(
                "fok_curve_cumulative_shares",
                raw.get("cumulative_shares"),
            )
            - cumulative_shares
        ) > EPS:
            raise ContractError("fok_cost_curve_cumulative_mismatch")
        if abs(
            _nonnegative(
                "fok_curve_cumulative_cost",
                raw.get("cumulative_all_in_cost"),
            )
            - cumulative_cost
        ) > EPS:
            raise ContractError("fok_cost_curve_cumulative_mismatch")
        cost += take * unit
        gross += take * exchange
        remaining -= take
        consumed += 1
        marginal = exchange
        if remaining <= EPS:
            break
    fillable = remaining <= EPS
    priced_shares = requested if fillable else 0.0
    return {
        "mode": "FOK",
        "requested_shares": requested,
        "fillable": fillable,
        "filled_shares": priced_shares,
        "all_in_cost": cost if fillable else 0.0,
        "all_in_vwap": (
            cost / requested if fillable and requested > EPS else 0.0
        ),
        "exchange_vwap": (
            gross / requested if fillable and requested > EPS else 0.0
        ),
        "marginal_exchange_price": marginal if fillable else 0.0,
        "depth_levels_consumed": consumed if fillable else 0,
        "curve_sha256": str(curve.get("content_sha256") or ""),
    }


def build_maker_cost_curve(
    item: BucketObservation,
    policy: TargetBasketPolicy,
    *,
    quote_version: str,
    candidate_price: float,
    fill_probability: float | None = None,
    nonfill_opportunity_cost_per_share: float | None = None,
    adverse_selection_cost_per_filled_share: float | None = None,
    wait_cost_per_share: float | None = None,
    oos_activated: bool = False,
    oos_artifact_sha256: str = "",
    oos_model_version: str = "",
    maker_grid_artifact: MakerGridOOSArtifact | Mapping[str, Any] | None = None,
    maker_grid_point_key: Sequence[Any] | None = None,
    expected_scope: Mapping[str, Any] | None = None,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    """Build a conservative expected Maker curve from an OOS fill model."""

    grid_artifact: MakerGridOOSArtifact | None = None
    grid_point: Mapping[str, Any] | None = None
    if maker_grid_artifact is not None:
        if maker_grid_point_key is None:
            raise ContractError("maker_grid_cost_curve_point_key_required")
        grid_artifact = validate_maker_grid_oos_artifact(
            maker_grid_artifact,
            expected_grid=maker_grid_artifact.get("grid_spec"),
            as_of_utc=as_of_utc,
        )
        supplied_key = tuple(maker_grid_point_key)
        if len(supplied_key) != len(MAKER_GRID_FIELDS):
            raise ContractError("maker_grid_cost_curve_point_key_invalid")
        points = {
            tuple(point[field] for field in MAKER_GRID_FIELDS): point
            for point in grid_artifact["points"]
        }
        grid_point = points.get(supplied_key)
        if grid_point is None:
            raise ContractError("maker_grid_cost_curve_point_missing")
        if expected_scope is not None:
            for field, expected in expected_scope.items():
                if field not in MAKER_GRID_SCOPE_FIELDS:
                    raise ContractError(
                        "maker_grid_cost_curve_expected_scope_invalid"
                    )
                if str(grid_point[field]) != str(expected):
                    raise ContractError(
                        f"maker_grid_cost_curve_scope_mismatch:{field}"
                    )
        metrics = grid_point["curve"]
        fill_probability = metrics["fill_probability"]
        nonfill_opportunity_cost_per_share = metrics[
            "nonfill_opportunity_cost_per_share"
        ]
        adverse_selection_cost_per_filled_share = metrics[
            "adverse_selection_cost_per_filled_share"
        ]
        wait_cost_per_share = metrics["wait_cost_per_filled_share"]
        oos_activated = True
        oos_artifact_sha256 = grid_artifact.content_sha256
        oos_model_version = str(grid_artifact["artifact_version"])
    if (
        fill_probability is None
        or nonfill_opportunity_cost_per_share is None
        or adverse_selection_cost_per_filled_share is None
        or wait_cost_per_share is None
    ):
        raise ContractError("maker_cost_curve_oos_metrics_required")

    price = _nonnegative("maker_curve_candidate_price", candidate_price)
    probability = _nonnegative(
        "maker_curve_fill_probability",
        fill_probability,
    )
    if price <= EPS or price >= 1.0 or probability > 1.0:
        raise ContractError("maker_cost_curve_input_invalid")
    if not oos_activated:
        raise ContractError("maker_cost_curve_oos_activation_required")
    artifact_sha = str(oos_artifact_sha256 or "").strip()
    model_version = str(oos_model_version or "").strip()
    if len(artifact_sha) != 64 or not model_version:
        raise ContractError("maker_cost_curve_oos_artifact_invalid")
    queue_cost = (
        _nonnegative(
            "maker_curve_queue_cost",
            grid_point["curve"]["queue_cost_per_filled_share"],
        )
        if grid_point is not None
        else policy.maker_queue_cost_per_share
    )
    nonfill = _nonnegative(
        "maker_curve_nonfill_opportunity_cost",
        nonfill_opportunity_cost_per_share,
    )
    adverse = _nonnegative(
        "maker_curve_adverse_selection_cost",
        adverse_selection_cost_per_filled_share,
    )
    wait = _nonnegative(
        "maker_curve_wait_cost",
        wait_cost_per_share,
    )
    cancel_risk = 0.0
    late_fill_risk = 0.0
    cancel_risk_cost = 0.0
    late_fill_risk_cost = 0.0
    if grid_point is not None:
        cancel_risk = float(grid_point["curve"]["cancel_risk"])
        late_fill_risk = float(grid_point["curve"]["late_fill_risk"])
        cancel_risk_cost = float(
            grid_point["curve"]["cancel_risk_cost_per_requested_share"]
        )
        late_fill_risk_cost = float(
            grid_point["curve"]["late_fill_risk_cost_per_requested_share"]
        )
    expected_fill_unit_cost = (
        price * (1.0 + policy.fee_rate)
        + queue_cost
        + adverse
        + wait
    )
    expected_risk_unit_cost = (
        probability * (queue_cost + adverse + wait)
        + (1.0 - probability) * nonfill
        + cancel_risk * cancel_risk_cost
        + late_fill_risk * late_fill_risk_cost
    )
    identity = {
        "schema": MAKER_COST_CURVE_SCHEMA,
        "bucket": item.bucket,
        "quote_version": str(quote_version),
        "fee_rate": policy.fee_rate,
        "candidate_price": price,
        "fill_probability": probability,
        "expected_fill_unit_cost": expected_fill_unit_cost,
        "expected_execution_risk_per_requested_share": (
            expected_risk_unit_cost
        ),
        "nonfill_opportunity_cost_per_share": nonfill,
        "adverse_selection_cost_per_filled_share": adverse,
        "queue_cost_per_filled_share": queue_cost,
        "wait_cost_per_filled_share": wait,
        "oos_activated": True,
        "oos_artifact_sha256": artifact_sha,
        "oos_model_version": model_version,
        "oos_artifact_schema": (
            MAKER_GRID_OOS_ARTIFACT_SCHEMA
            if grid_artifact is not None
            else None
        ),
        "oos_artifact": (
            grid_artifact.to_dict()
            if grid_artifact is not None
            else None
        ),
        "oos_scope": (
            {
                field: grid_point[field]
                for field in MAKER_GRID_SCOPE_FIELDS
            }
            if grid_point is not None
            else None
        ),
        "oos_scope_sha256": (
            maker_grid_scope_sha256({
                field: grid_point[field]
                for field in MAKER_GRID_SCOPE_FIELDS
            })
            if grid_point is not None
            else None
        ),
        "oos_train_window": (
            {
                "start_utc": grid_point["train_start_utc"],
                "end_utc": grid_point["train_end_utc"],
            }
            if grid_point is not None
            else None
        ),
        "oos_holdout_window": (
            {
                "start_utc": grid_point["holdout_start_utc"],
                "end_utc": grid_point["holdout_end_utc"],
            }
            if grid_point is not None
            else None
        ),
        "oos_n_eff": (
            grid_point["n_eff"]
            if grid_point is not None
            else None
        ),
        "oos_holdout_n": (
            grid_point["holdout_n"]
            if grid_point is not None
            else None
        ),
        "oos_valid_from_utc": (
            grid_artifact["as_of_utc"]
            if grid_artifact is not None
            else None
        ),
        "oos_valid_until_utc": None,
        "oos_promotion_evidence_sha256": (
            grid_point["promotion_evidence"]["content_sha256"]
            if grid_point is not None
            else None
        ),
        "maker_grid_point_key": (
            list(grid_point_key(grid_point))
            if grid_point is not None
            else None
        ),
        "maker_grid_point_sha256": (
            str(grid_point["grid_point_key"])
            if grid_point is not None
            else None
        ),
        "maker_grid_curve_sha256": (
            str(grid_point["curve_sha256"])
            if grid_point is not None
            else None
        ),
        "maker_grid_artifact_curve_sha256": (
            grid_artifact.curve_sha256
            if grid_artifact is not None
            else None
        ),
        "maker_timeout_seconds": (
            float(grid_point["timeout_seconds"])
            if grid_point is not None
            else None
        ),
        "cancel_risk": cancel_risk,
        "late_fill_risk": late_fill_risk,
        "cancel_risk_cost_per_requested_share": cancel_risk_cost,
        "late_fill_risk_cost_per_requested_share": late_fill_risk_cost,
    }
    return {
        **identity,
        "mode": "MAKER",
        "content_sha256": content_sha256(identity),
    }


def _curve_identity(curve: Mapping[str, Any]) -> dict[str, Any]:
    schema = curve.get("schema")
    if schema == FOK_COST_CURVE_SCHEMA:
        fields = (
            "schema",
            "bucket",
            "quote_version",
            "fee_rate",
            "slippage_buffer_per_share",
            "segments",
        )
    elif schema == MAKER_COST_CURVE_SCHEMA:
        fields = (
            "schema",
            "bucket",
            "quote_version",
            "fee_rate",
            "candidate_price",
            "fill_probability",
            "expected_fill_unit_cost",
            "expected_execution_risk_per_requested_share",
            "nonfill_opportunity_cost_per_share",
            "adverse_selection_cost_per_filled_share",
            "queue_cost_per_filled_share",
            "wait_cost_per_filled_share",
            "oos_activated",
            "oos_artifact_sha256",
            "oos_model_version",
            "oos_artifact_schema",
            "oos_artifact",
            "oos_scope",
            "oos_scope_sha256",
            "oos_train_window",
            "oos_holdout_window",
            "oos_n_eff",
            "oos_holdout_n",
            "oos_valid_from_utc",
            "oos_valid_until_utc",
            "oos_promotion_evidence_sha256",
            "cancel_risk",
            "late_fill_risk",
            "cancel_risk_cost_per_requested_share",
            "late_fill_risk_cost_per_requested_share",
            "maker_grid_point_key",
            "maker_grid_point_sha256",
            "maker_grid_curve_sha256",
            "maker_grid_artifact_curve_sha256",
            "maker_timeout_seconds",
        )
    else:
        raise ContractError("cost_curve_schema_invalid")
    return {field: curve.get(field) for field in fields}


def _validate_curve_binding(
    curve: Mapping[str, Any],
    *,
    bucket: str,
    quote_version: str,
    target_policy: TargetBasketPolicy,
    expected_oos_scope: Mapping[str, Any] | None = None,
    as_of_utc: str | None = None,
) -> str:
    if str(curve.get("bucket") or "") != bucket:
        raise ContractError("cost_curve_bucket_mismatch")
    if str(curve.get("quote_version") or "") != quote_version:
        raise ContractError("cost_curve_quote_version_mismatch")
    supplied_sha = str(curve.get("content_sha256") or "")
    if supplied_sha != content_sha256(_curve_identity(curve)):
        raise ContractError("cost_curve_content_sha256_mismatch")
    if abs(
        _nonnegative("cost_curve_fee_rate", curve.get("fee_rate"))
        - target_policy.fee_rate
    ) > EPS:
        raise ContractError("cost_curve_target_policy_mismatch")
    if curve.get("schema") == FOK_COST_CURVE_SCHEMA and abs(
        _nonnegative(
            "cost_curve_slippage_buffer",
            curve.get("slippage_buffer_per_share"),
        )
        - target_policy.taker_slippage_buffer_per_share
    ) > EPS:
        raise ContractError("cost_curve_target_policy_mismatch")
    if curve.get("schema") == MAKER_COST_CURVE_SCHEMA:
        raw_artifact = curve.get("oos_artifact")
        if raw_artifact is not None:
            scope = expected_oos_scope or curve.get("oos_scope")
            if not isinstance(scope, Mapping):
                raise ContractError("maker_cost_curve_exact_scope_required")
            artifact_schema = curve.get("oos_artifact_schema")
            if artifact_schema == MAKER_GRID_OOS_ARTIFACT_SCHEMA:
                artifact = validate_maker_grid_oos_artifact(
                    raw_artifact,
                    expected_grid=raw_artifact.get("grid_spec"),
                    as_of_utc=as_of_utc,
                )
                if artifact.content_sha256 != str(
                    curve.get("oos_artifact_sha256") or ""
                ):
                    raise ContractError(
                        "maker_cost_curve_oos_artifact_sha_mismatch"
                    )
                raw_key = curve.get("maker_grid_point_key")
                if (
                    isinstance(raw_key, (str, bytes))
                    or not isinstance(raw_key, Sequence)
                ):
                    raise ContractError(
                        "maker_grid_cost_curve_point_key_invalid"
                    )
                point_key = tuple(raw_key)
                points = {
                    tuple(point[field] for field in MAKER_GRID_FIELDS): point
                    for point in artifact["points"]
                }
                point = points.get(point_key)
                if point is None:
                    raise ContractError(
                        "maker_grid_cost_curve_point_missing"
                    )
                for field, expected in scope.items():
                    if (
                        field not in MAKER_GRID_SCOPE_FIELDS
                        or str(point[field]) != str(expected)
                    ):
                        raise ContractError(
                            f"maker_grid_cost_curve_scope_mismatch:{field}"
                        )
                point_scope = {
                    field: point[field]
                    for field in MAKER_GRID_SCOPE_FIELDS
                }
                if curve.get(
                    "oos_scope_sha256"
                ) != maker_grid_scope_sha256(point_scope):
                    raise ContractError(
                        "maker_cost_curve_oos_scope_sha_mismatch"
                    )
                if curve.get("maker_grid_point_sha256") != point[
                    "grid_point_key"
                ]:
                    raise ContractError(
                        "maker_grid_cost_curve_point_sha_mismatch"
                    )
                if curve.get("maker_grid_curve_sha256") != point[
                    "curve_sha256"
                ]:
                    raise ContractError(
                        "maker_grid_cost_curve_curve_sha_mismatch"
                    )
                if curve.get(
                    "maker_grid_artifact_curve_sha256"
                ) != artifact.curve_sha256:
                    raise ContractError(
                        "maker_grid_cost_curve_artifact_curve_sha_mismatch"
                    )
                if abs(
                    float(curve.get("candidate_price"))
                    - float(point["candidate_price"])
                ) > EPS or abs(
                    float(curve.get("maker_timeout_seconds"))
                    - float(point["timeout_seconds"])
                ) > EPS:
                    raise ContractError(
                        "maker_grid_cost_curve_point_binding_mismatch"
                    )
            else:
                raise ContractError(
                    "maker_cost_curve_oos_artifact_schema_invalid"
                )
    return supplied_sha


def _maker_action(
    curve: Mapping[str, Any],
    shares: float,
) -> dict[str, Any]:
    requested = _nonnegative("maker_curve_requested_shares", shares)
    if (
        curve.get("schema") != MAKER_COST_CURVE_SCHEMA
        or curve.get("oos_activated") is not True
    ):
        raise ContractError("maker_cost_curve_invalid")
    fill_probability = _nonnegative(
        "maker_curve_fill_probability",
        curve.get("fill_probability"),
    )
    if fill_probability > 1.0:
        raise ContractError("maker_cost_curve_fill_probability_invalid")
    expected_filled = requested * fill_probability
    fill_unit = _nonnegative(
        "maker_curve_expected_fill_unit_cost",
        curve.get("expected_fill_unit_cost"),
    )
    risk_unit = _nonnegative(
        "maker_curve_expected_execution_risk",
        curve.get("expected_execution_risk_per_requested_share"),
    )
    price = _nonnegative(
        "maker_curve_candidate_price",
        curve.get("candidate_price"),
    )
    fee_rate = _nonnegative(
        "maker_curve_fee_rate",
        curve.get("fee_rate"),
    )
    queue = _nonnegative(
        "maker_curve_queue_cost",
        curve.get("queue_cost_per_filled_share"),
    )
    adverse = _nonnegative(
        "maker_curve_adverse_selection_cost",
        curve.get("adverse_selection_cost_per_filled_share"),
    )
    wait = _nonnegative(
        "maker_curve_wait_cost",
        curve.get("wait_cost_per_filled_share"),
    )
    nonfill = _nonnegative(
        "maker_curve_nonfill_opportunity_cost",
        curve.get("nonfill_opportunity_cost_per_share"),
    )
    cancel_risk = _nonnegative(
        "maker_curve_cancel_risk",
        curve.get("cancel_risk", 0.0),
    )
    late_risk = _nonnegative(
        "maker_curve_late_fill_risk",
        curve.get("late_fill_risk", 0.0),
    )
    cancel_risk_cost = _nonnegative(
        "maker_curve_cancel_risk_cost",
        curve.get("cancel_risk_cost_per_requested_share", 0.0),
    )
    late_risk_cost = _nonnegative(
        "maker_curve_late_fill_risk_cost",
        curve.get("late_fill_risk_cost_per_requested_share", 0.0),
    )
    if abs(
        fill_unit - (price * (1.0 + fee_rate) + queue + adverse + wait)
    ) > EPS:
        raise ContractError("maker_cost_curve_decomposition_mismatch")
    if abs(
        risk_unit
        - (
            fill_probability * (queue + adverse + wait)
            + (1.0 - fill_probability) * nonfill
            + cancel_risk * cancel_risk_cost
            + late_risk * late_risk_cost
        )
    ) > EPS:
        raise ContractError("maker_cost_curve_decomposition_mismatch")
    return {
        "mode": "MAKER",
        "requested_shares": requested,
        "expected_filled_shares": expected_filled,
        "expected_all_in_cost": expected_filled * fill_unit,
        "execution_risk": requested * risk_unit,
        "curve_sha256": str(curve.get("content_sha256") or ""),
    }


def linearize_maker_cost_curve(
    curve: Mapping[str, Any],
    *,
    bucket: str,
    quote_version: str,
    target_policy: TargetBasketPolicy,
) -> dict[str, Any]:
    """Return one-requested-share OOS Maker economics for joint solving."""

    curve_sha = _validate_curve_binding(
        curve,
        bucket=bucket,
        quote_version=quote_version,
        target_policy=target_policy,
    )
    action = _maker_action(curve, 1.0)
    candidate_price = _nonnegative(
        "maker_curve_candidate_price",
        curve.get("candidate_price"),
    )
    fee_rate = _nonnegative(
        "maker_curve_fee_rate",
        curve.get("fee_rate"),
    )
    return {
        "mode": "MAKER",
        "expected_filled_shares_per_requested_share": float(
            action["expected_filled_shares"]
        ),
        "expected_all_in_cost_per_requested_share": float(
            action["expected_all_in_cost"]
        ),
        "execution_risk_per_requested_share": float(
            action["execution_risk"]
        ),
        # A confirmed Maker reserves the full requested notional even though
        # only conservative expected fills enter J.
        "budget_reservation_per_requested_share": (
            candidate_price * (1.0 + fee_rate)
        ),
        "curve_sha256": curve_sha,
    }


def evaluate_strict_delta(
    snapshot: TargetBasketSnapshot,
    target_policy: TargetBasketPolicy,
    strict_policy: StrictDeltaPolicy,
    *,
    proposed_actions: Mapping[str, Mapping[str, Any]],
    protected_buckets: Sequence[str],
    shortfall_buckets: Sequence[str],
    breakthrough_buckets: Sequence[str],
    version_vector: Mapping[str, str],
    maker_grid_oos_scope_by_bucket: Mapping[str, Mapping[str, Any]] | None = None,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    """Evaluate one proposal against the exact same immutable baseline."""

    by_bucket = {item.bucket: item for item in snapshot.buckets}
    protected = set(protected_buckets)
    shortfall = set(shortfall_buckets)
    breakthrough = set(breakthrough_buckets)
    unknown = (
        protected | shortfall | breakthrough | set(proposed_actions)
    ) - set(by_bucket)
    if unknown:
        raise ContractError("strict_delta_bucket_scope_unknown")
    if not protected:
        raise ContractError("strict_delta_protected_buckets_required")
    if not proposed_actions:
        raise ContractError("strict_delta_actions_required")
    expected_versions = {
        "probability_artifact_sha256": (
            snapshot.evidence.posterior_sha256
        ),
        "quote_snapshot_sha256": snapshot.quote_version,
        "inventory_version": snapshot.inventory_version,
        "order_state_version": snapshot.order_state_version,
        "policy_version": strict_policy.policy_version,
        "solver_version": SOLVER_VERSION,
    }
    if dict(version_vector) != expected_versions:
        raise ContractError("strict_delta_version_vector_mismatch")

    action_shares: dict[str, float] = {}
    requested_shares: dict[str, float] = {}
    cost_by_bucket: dict[str, float] = {}
    execution_risk_by_bucket: dict[str, float] = {}
    delta_ev_by_bucket: dict[str, float] = {}
    curve_sha_by_bucket: dict[str, str] = {}
    blockers: list[str] = []
    coverage_subsidized_by_bucket: dict[str, bool] = {}
    for bucket, raw in sorted(proposed_actions.items()):
        if not isinstance(raw, Mapping):
            raise ContractError("strict_delta_action_mapping_required")
        requested = _nonnegative(
            "strict_delta_action_shares",
            raw.get("shares"),
        )
        if requested <= EPS:
            blockers.append(f"{bucket}:action_shares_not_positive")
        mode = str(raw.get("mode") or "").upper()
        curve = raw.get("cost_curve")
        if not isinstance(curve, Mapping):
            raise ContractError("strict_delta_cost_curve_missing")
        item = by_bucket[bucket]
        coverage_subsidized_by_bucket[bucket] = (
            raw.get("coverage_subsidized") is True
        )
        requested_shares[bucket] = requested
        if mode == "FOK":
            curve_sha_by_bucket[bucket] = _validate_curve_binding(
                curve,
                bucket=bucket,
                quote_version=snapshot.quote_version,
                target_policy=target_policy,
            )
            priced = price_fok_curve(curve, requested)
            if not priced["fillable"]:
                blockers.append(f"{bucket}:fok_not_fillable")
                action_shares[bucket] = 0.0
                cost_by_bucket[bucket] = 0.0
                execution_risk_by_bucket[bucket] = 0.0
            else:
                action_shares[bucket] = requested
                cost_by_bucket[bucket] = float(
                    priced["all_in_cost"]
                )
                execution_risk_by_bucket[bucket] = 0.0
        elif mode == "MAKER":
            curve_sha_by_bucket[bucket] = _validate_curve_binding(
                curve,
                bucket=bucket,
                quote_version=snapshot.quote_version,
                target_policy=target_policy,
                expected_oos_scope=(
                    (maker_grid_oos_scope_by_bucket or {}).get(bucket)
                ),
                as_of_utc=as_of_utc,
            )
            maker = _maker_action(curve, requested)
            action_shares[bucket] = float(
                maker["expected_filled_shares"]
            )
            cost_by_bucket[bucket] = float(
                maker["expected_all_in_cost"]
            )
            execution_risk_by_bucket[bucket] = float(
                maker["execution_risk"]
            )
        else:
            raise ContractError("strict_delta_execution_mode_invalid")
        delta_ev_by_bucket[bucket] = (
            item.probability * action_shares[bucket]
            - cost_by_bucket[bucket]
        )

    action_cost = sum(cost_by_bucket.values())
    execution_risk = sum(execution_risk_by_bucket.values())
    objective = evaluate_delta_objective(
        probabilities={
            item.bucket: item.probability for item in snapshot.buckets
        },
        filled_shares_by_bucket={
            item.bucket: item.filled_shares for item in snapshot.buckets
        },
        filled_cost_by_bucket={
            item.bucket: item.filled_cost_usdc for item in snapshot.buckets
        },
        action_shares_by_bucket=action_shares,
        action_cost_by_bucket=cost_by_bucket,
        protected_buckets=tuple(protected),
        shortfall_buckets=tuple(shortfall),
        breakthrough_buckets=tuple(breakthrough),
        weights=ObjectiveWeights(
            lambda_profit_floor=strict_policy.lambda_profit_floor,
            lambda_downside=strict_policy.lambda_downside,
            lambda_shortfall=strict_policy.lambda_shortfall,
            lambda_breakthrough=strict_policy.lambda_breakthrough,
            lambda_concentration=strict_policy.lambda_concentration,
            lambda_execution=strict_policy.lambda_execution,
        ),
        # Pending Maker exposure is deliberately absent from realised PnL.
        # It remains represented by the immutable order state and budget
        # reservation below; any calibrated pending execution-risk measure
        # must be supplied by its own OOS artifact, not inferred from notional.
        baseline_execution_risk=0.0,
        action_execution_risk=execution_risk,
        concentration_scale_usdc=target_policy.total_budget_usdc,
    )
    baseline = objective["baseline"]
    proposed = objective["proposed"]
    deltas = {
        key: objective[key]
        for key in (
            "delta_expected_pnl",
            "delta_downside",
            "delta_shortfall_coverage",
            "delta_breakthrough_coverage",
            "delta_concentration",
            "delta_execution_risk",
            "delta_profit_floor",
            "delta_protected_tail_loss",
        )
    }
    delta_score = float(objective["delta_basket_score"])
    if any(
        abs(
            delta_ev_by_bucket.get(bucket, 0.0)
            - objective["delta_ev_by_bucket"].get(bucket, 0.0)
        ) > 1e-8
        for bucket in set(delta_ev_by_bucket)
        | set(objective["delta_ev_by_bucket"])
    ):
        raise ContractError("strict_delta_delta_ev_kernel_mismatch")
    delta_ev_reconciliation_slack = float(
        objective["delta_ev_reconciliation_slack"]
    )

    negative_subsidy = sum(
        max(0.0, -value) for value in delta_ev_by_bucket.values()
    )
    positive_delta_ev = sum(
        max(0.0, value) for value in delta_ev_by_bucket.values()
    )
    subsidy_cap = min(
        strict_policy.subsidy_absolute_cap,
        strict_policy.subsidy_ratio_cap * positive_delta_ev,
    )
    negative_buckets = {
        bucket
        for bucket, value in delta_ev_by_bucket.items()
        if value < -EPS
    }
    declared_coverage_buckets = {
        bucket
        for bucket, declared in coverage_subsidized_by_bucket.items()
        if declared
    }
    coverage_subsidized = bool(negative_buckets)
    coverage_bucket_scope_ok = negative_buckets <= (
        shortfall | breakthrough
    )
    negative_buckets_declared = (
        negative_buckets <= declared_coverage_buckets
    )
    protected_tail_loss_improved = (
        deltas["delta_profit_floor"] > EPS
        or deltas["delta_protected_tail_loss"] < -EPS
    )
    hard_constraints = {
        "evidence_fresh": snapshot.evidence_fresh,
        "order_state_known": snapshot.order_state_known,
        "contract_matches": snapshot.contract_matches,
        "budget_ok": (
            action_cost
            <= max(
                0.0,
                target_policy.total_budget_usdc
                - sum(
                    item.filled_cost_usdc
                    + item.pending_notional_usdc
                    for item in snapshot.buckets
                ),
            )
            + EPS
        ),
        "expected_pnl_floor_ok": (
            proposed["expected_pnl"]
            + EPS
            >= strict_policy.expected_pnl_policy_floor
        ),
        "subsidy_cap_ok": (
            negative_subsidy <= subsidy_cap + EPS
        ),
        "coverage_oos_ok": (
            not coverage_subsidized
            or strict_policy.coverage_policy_oos_activated
        ),
        "coverage_improves_protected_risk": (
            not coverage_subsidized
            or protected_tail_loss_improved
        ),
        "coverage_bucket_scope_ok": coverage_bucket_scope_ok,
        "negative_buckets_explicitly_declared": (
            negative_buckets_declared
        ),
        "regular_bucket_delta_ev_nonnegative": all(
            value >= -EPS
            or (
                bucket in declared_coverage_buckets
                and bucket in (shortfall | breakthrough)
            )
            for bucket, value in delta_ev_by_bucket.items()
        ),
        "delta_score_strictly_positive": (
            delta_score > strict_policy.epsilon_score
        ),
        "mode_curves_complete": (
            set(curve_sha_by_bucket) == set(proposed_actions)
            and all(
                len(value) == 64 for value in curve_sha_by_bucket.values()
            )
        ),
        "actions_nonempty": any(
            value > EPS for value in requested_shares.values()
        ),
    }
    authorized = not blockers and all(hard_constraints.values())
    identity = {
        "schema": STRICT_DELTA_SCHEMA,
        "baseline_score": baseline["score"],
        "proposed_score": proposed["score"],
        "delta_basket_score": delta_score,
        **deltas,
        "delta_ev_by_bucket": delta_ev_by_bucket,
        "delta_ev_sum": sum(delta_ev_by_bucket.values()),
        "delta_ev_reconciliation_slack": (
            delta_ev_reconciliation_slack
        ),
        "coverage_subsidized": coverage_subsidized,
        "coverage_subsidized_by_bucket": (
            coverage_subsidized_by_bucket
        ),
        "negative_delta_ev_buckets": sorted(negative_buckets),
        "negative_delta_ev_subsidy": negative_subsidy,
        "positive_delta_ev": positive_delta_ev,
        "subsidy_cap": subsidy_cap,
        "subsidy_cap_slack": subsidy_cap - negative_subsidy,
        "mode_specific_cost_curve_sha256": curve_sha_by_bucket,
        **expected_versions,
        "target_policy_sha256": content_sha256(
            target_policy.to_dict()
        ),
        "strict_policy_sha256": strict_policy.content_sha256,
        "objective_kernel_version": objective["kernel_version"],
        "objective_audit_sha256": objective["content_sha256"],
        "pending_execution_state": {
            "pending_shares_by_bucket": {
                item.bucket: item.pending_shares
                for item in snapshot.buckets
                if item.pending_shares > EPS
            },
            "pending_notional_by_bucket": {
                item.bucket: item.pending_notional_usdc
                for item in snapshot.buckets
                if item.pending_notional_usdc > EPS
            },
            "included_in_realised_objective": False,
        },
        "constraint_slacks": {
            "budget_usdc": (
                target_policy.total_budget_usdc
                - sum(
                    item.filled_cost_usdc
                    + item.pending_notional_usdc
                    for item in snapshot.buckets
                )
                - action_cost
            ),
            "expected_pnl_usdc": (
                proposed["expected_pnl"]
                - strict_policy.expected_pnl_policy_floor
            ),
            "delta_score": (
                delta_score - strict_policy.epsilon_score
            ),
            "subsidy_usdc": subsidy_cap - negative_subsidy,
            "delta_ev_reconciliation_usdc": (
                -abs(delta_ev_reconciliation_slack)
            ),
        },
        "hard_constraints": hard_constraints,
        "blockers": blockers,
        "authorized": authorized,
    }
    return {
        **identity,
        "baseline": baseline,
        "proposed": proposed,
        "requested_shares_by_bucket": requested_shares,
        "expected_action_shares_by_bucket": action_shares,
        "action_cost_by_bucket": cost_by_bucket,
        "execution_risk_by_bucket": execution_risk_by_bucket,
        "content_sha256": content_sha256(identity),
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
    }


__all__ = [
    "FOK_COST_CURVE_SCHEMA",
    "MAKER_COST_CURVE_SCHEMA",
    "SOLVER_VERSION",
    "STRICT_DELTA_SCHEMA",
    "StrictDeltaPolicy",
    "build_fok_cost_curve",
    "build_maker_cost_curve",
    "evaluate_strict_delta",
    "linearize_maker_cost_curve",
    "price_fok_curve",
]
