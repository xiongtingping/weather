"""Versioned contracts for the isolated TargetBasketPlan target candidate."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence


TARGET_BASKET_SNAPSHOT_SCHEMA = "target_basket_snapshot_v6"
TARGET_BASKET_POLICY_SCHEMA = "target_basket_policy_v10"
TARGET_BASKET_PLAN_SCHEMA = "target_basket_plan_v12"
TARGET_PRICE_RESULT_SCHEMA = "target_target_price_result_v1"
TARGET_MAKER_CANDIDATE_SCHEMA = "target_target_maker_candidate_v2"
TARGET_MAKER_EVENT_SCHEMA = "target_target_maker_event_v1"
TARGET_MAKER_RECEIPT_SCHEMA = "target_target_maker_receipt_v1"
TAKER_DEPTH_SNAPSHOT_SCHEMA = "target_taker_depth_snapshot_v1"
TARGET_TAKER_CANDIDATE_SCHEMA = "target_target_taker_candidate_v2"
TARGET_TAKER_RECEIPT_SCHEMA = "target_target_taker_receipt_v1"
URGENCY_EVALUATION_SCHEMA = "target_urgency_evaluation_v1"
EXECUTION_GATE_DECISION_SCHEMA = "target_execution_gate_decision_v2"
ZERO_ORDER_REPLAY_SCHEMA = "target_basket_zero_order_replay_v5"
EPS = 1e-9


class ContractError(ValueError):
    """Raised when an immutable target input violates its contract."""


class BasketState(str, Enum):
    SEED = "SEED"
    EXTEND_LOW = "EXTEND_LOW"
    EXTEND_HIGH = "EXTEND_HIGH"
    FILL_GAP = "FILL_GAP"
    REPLENISH = "REPLENISH"
    HOLD = "HOLD"
    STOP = "STOP"


class BucketRole(str, Enum):
    BASE = "BASE"
    BIAS = "BIAS"
    EDGE = "EDGE"


class ExecutionCandidate(str, Enum):
    NONE = "NONE"
    MAKER = "MAKER"
    TAKER_FOK = "TAKER_FOK"


class MakerLifecycleState(str, Enum):
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    CANCELLED_PARTIAL = "CANCELLED_PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"


class MakerEventType(str, Enum):
    QUEUE_TRADE = "QUEUE_TRADE"
    QUEUE_SNAPSHOT = "QUEUE_SNAPSHOT"
    TIMEOUT = "TIMEOUT"
    CANCEL_REQUEST = "CANCEL_REQUEST"
    CANCEL_ACK = "CANCEL_ACK"
    FILL_REPORT = "FILL_REPORT"
    REJECT = "REJECT"
    UNKNOWN_POST = "UNKNOWN_POST"
    AUTHORITATIVE_STATUS = "AUTHORITATIVE_STATUS"


class MakerExchangeStatus(str, Enum):
    OPEN = "OPEN"
    CANCELLED = "CANCELLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class TakerSimulationState(str, Enum):
    FILLABLE = "FILLABLE"
    FOK_NOT_FILLABLE = "FOK_NOT_FILLABLE"
    PRICE_LIMIT_BREACHED = "PRICE_LIMIT_BREACHED"
    STALE_QUOTE = "STALE_QUOTE"
    INVALID_DEPTH = "INVALID_DEPTH"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"


class Direction(str, Enum):
    DOWN = "DOWN"
    NEUTRAL = "NEUTRAL"
    UP = "UP"


def _optional_direction(value: Any) -> Direction | None:
    if value in (None, ""):
        return None
    try:
        return Direction(str(value).strip().upper())
    except ValueError as exc:
        raise ContractError("snapshot_current_direction_invalid") from exc


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_must_be_finite_number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_must_be_finite_number") from exc
    if not math.isfinite(parsed):
        raise ContractError(f"{name}_must_be_finite_number")
    return parsed


def _price_is_tick_aligned(price: float, tick_size: float) -> bool:
    return abs(price / tick_size - round(price / tick_size)) <= 1e-7


def _nonnegative(name: str, value: Any) -> float:
    parsed = _finite(name, value)
    if parsed < 0:
        raise ContractError(f"{name}_must_be_nonnegative")
    return parsed


def _positive(name: str, value: Any) -> float:
    parsed = _finite(name, value)
    if parsed <= 0:
        raise ContractError(f"{name}_must_be_positive")
    return parsed


def _unit_interval(name: str, value: Any, *, closed: bool = True) -> float:
    parsed = _finite(name, value)
    valid = 0 <= parsed <= 1 if closed else 0 < parsed < 1
    if not valid:
        raise ContractError(f"{name}_outside_unit_interval")
    return parsed


def _required_text(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{name}_missing")
    return text


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name}_must_be_boolean")
    return value


def _sha256_text(name: str, value: Any) -> str:
    text = _required_text(name, value).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ContractError(f"{name}_invalid")
    return text


def _iso8601(name: str, value: Any) -> str:
    text = _required_text(name, value)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    return text


def _iso_date(name: str, value: Any) -> str:
    text = _required_text(name, value)
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_date") from exc
    return text


@dataclass(frozen=True)
class MaterializedEvidence:
    """The only weather-side boundary the basket may consume."""

    probability_stage: str
    posterior_version: str
    posterior_sha256: str
    forecast_dispersion_c: float = 0.0
    model_envelope_lower_ordinal: int | None = None
    model_envelope_upper_ordinal: int | None = None
    formal_model_members_json: str | None = None
    formal_model_anchor_lower_ordinal: int | None = None
    formal_model_anchor_upper_ordinal: int | None = None
    reported_high_bucket: str | None = None
    reported_high_ordinal: int | None = None
    reported_high_observation_id: str | None = None
    hard_bound_id: str | None = None
    reported_high_advanced: bool | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MaterializedEvidence":
        allowed_fields = {
            "probability_stage",
            "posterior_version",
            "posterior_sha256",
            "forecast_dispersion_c",
            "model_envelope_lower_ordinal",
            "model_envelope_upper_ordinal",
            "formal_model_members_json",
            "formal_model_anchor_lower_ordinal",
            "formal_model_anchor_upper_ordinal",
            "reported_high_bucket",
            "reported_high_ordinal",
            "reported_high_observation_id",
            "hard_bound_id",
            "reported_high_advanced",
        }
        if set(raw) - allowed_fields:
            raise ContractError("materialized_evidence_unknown_fields")
        raw_lower = raw.get("model_envelope_lower_ordinal")
        raw_upper = raw.get("model_envelope_upper_ordinal")
        if (raw_lower is None) != (raw_upper is None):
            raise ContractError("model_envelope_boundary_incomplete")
        try:
            lower = None if raw_lower is None else int(raw_lower)
            upper = None if raw_upper is None else int(raw_upper)
        except (TypeError, ValueError) as exc:
            raise ContractError("model_envelope_boundary_invalid") from exc
        if lower is not None and (
            isinstance(raw_lower, bool)
            or isinstance(raw_upper, bool)
            or lower > upper
        ):
            raise ContractError("model_envelope_boundary_invalid")
        formal_members = raw.get("formal_model_members_json")
        raw_formal_lower = raw.get("formal_model_anchor_lower_ordinal")
        raw_formal_upper = raw.get("formal_model_anchor_upper_ordinal")
        if (raw_formal_lower is None) != (raw_formal_upper is None):
            raise ContractError("formal_model_anchor_boundary_incomplete")
        if (formal_members in (None, "")) != (raw_formal_lower is None):
            raise ContractError("formal_model_anchor_lineage_incomplete")
        try:
            formal_lower = (
                None if raw_formal_lower is None else int(raw_formal_lower)
            )
            formal_upper = (
                None if raw_formal_upper is None else int(raw_formal_upper)
            )
        except (TypeError, ValueError) as exc:
            raise ContractError("formal_model_anchor_boundary_invalid") from exc
        if formal_lower is not None and (
            isinstance(raw_formal_lower, bool)
            or isinstance(raw_formal_upper, bool)
            or formal_lower > formal_upper
        ):
            raise ContractError("formal_model_anchor_boundary_invalid")
        risk_raw = (
            raw.get("reported_high_bucket"),
            raw.get("reported_high_ordinal"),
            raw.get("reported_high_observation_id"),
            raw.get("hard_bound_id"),
        )
        if any(item not in (None, "") for item in risk_raw) and not all(
            item not in (None, "") for item in risk_raw
        ):
            raise ContractError("reported_high_risk_lineage_incomplete")
        try:
            reported_high_ordinal = (
                None
                if risk_raw[1] in (None, "")
                else int(risk_raw[1])
            )
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "reported_high_ordinal_invalid"
            ) from exc
        value = cls(
            probability_stage=_required_text("probability_stage", raw.get("probability_stage")),
            posterior_version=_required_text("posterior_version", raw.get("posterior_version")),
            posterior_sha256=_sha256_text("posterior_sha256", raw.get("posterior_sha256")),
            forecast_dispersion_c=_nonnegative("forecast_dispersion_c", raw.get("forecast_dispersion_c", 0.0)),
            model_envelope_lower_ordinal=lower,
            model_envelope_upper_ordinal=upper,
            formal_model_members_json=(
                None if formal_members in (None, "") else str(formal_members)
            ),
            formal_model_anchor_lower_ordinal=formal_lower,
            formal_model_anchor_upper_ordinal=formal_upper,
            reported_high_bucket=(
                None
                if risk_raw[0] in (None, "")
                else str(risk_raw[0])
            ),
            reported_high_ordinal=reported_high_ordinal,
            reported_high_observation_id=(
                None
                if risk_raw[2] in (None, "")
                else str(risk_raw[2])
            ),
            hard_bound_id=(
                None
                if risk_raw[3] in (None, "")
                else str(risk_raw[3])
            ),
            reported_high_advanced=(
                None
                if raw.get("reported_high_advanced") is None
                else _boolean(
                    "reported_high_advanced",
                    raw.get("reported_high_advanced"),
                )
            ),
        )
        if value.probability_stage not in {"P0", "P0-M", "P1"}:
            raise ContractError("probability_stage_invalid")
        if (
            value.probability_stage != "P1"
            and value.reported_high_bucket is not None
        ):
            raise ContractError("reported_high_risk_requires_p1")
        if (
            value.probability_stage == "P1"
            and value.reported_high_bucket is None
        ):
            raise ContractError("p1_reported_high_risk_lineage_required")
        if (
            value.probability_stage != "P1"
            and value.reported_high_advanced is not None
        ):
            raise ContractError("reported_high_advanced_requires_p1")
        return value


@dataclass(frozen=True)
class AskLevel:
    price: float
    shares: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AskLevel":
        return cls(
            price=_unit_interval("ask_level_price", raw.get("price"), closed=False),
            shares=_positive("ask_level_shares", raw.get("shares")),
        )

    def to_dict(self) -> dict[str, float]:
        return {"price": self.price, "shares": self.shares}


@dataclass(frozen=True)
class BucketObservation:
    bucket: str
    ordinal: int
    probability: float
    bid_price: float
    ask_levels: tuple[AskLevel, ...]
    ask_available: bool
    bid_levels: tuple[AskLevel, ...]
    price_tick_size: float
    maker_queue_ahead_shares: float
    maker_queue_known: bool
    peripheral_model_reliability: float = 0.0
    filled_shares: float = 0.0
    filled_cost_usdc: float = 0.0
    pending_shares: float = 0.0
    pending_notional_usdc: float = 0.0
    active_before: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BucketObservation":
        if "limit_price" in raw:
            raise ContractError("bucket_limit_price_forbidden_use_internal_solver")
        if "ask_price" in raw or "executable_depth_shares" in raw:
            raise ContractError("bucket_single_level_ask_forbidden_use_ask_levels")
        ask_values = raw.get("ask_levels")
        if not isinstance(ask_values, Sequence) or isinstance(ask_values, (str, bytes, bytearray)):
            raise ContractError("bucket_ask_levels_invalid")
        asks = tuple(AskLevel.from_mapping(item) for item in ask_values if isinstance(item, Mapping))
        if len(asks) != len(ask_values):
            raise ContractError("bucket_ask_level_row_invalid")
        bid_values = raw.get("bid_levels", ())
        if (
            not isinstance(bid_values, Sequence)
            or isinstance(bid_values, (str, bytes, bytearray))
        ):
            raise ContractError("bucket_bid_levels_invalid")
        bids = tuple(
            AskLevel.from_mapping(item)
            for item in bid_values
            if isinstance(item, Mapping)
        )
        if len(bids) != len(bid_values):
            raise ContractError("bucket_bid_level_row_invalid")
        try:
            ordinal = int(raw.get("ordinal"))
        except (TypeError, ValueError) as exc:
            raise ContractError("bucket_ordinal_invalid") from exc
        value = cls(
            bucket=_required_text("bucket", raw.get("bucket")),
            ordinal=ordinal,
            probability=_unit_interval("bucket_probability", raw.get("probability")),
            bid_price=_unit_interval("bucket_bid_price", raw.get("bid_price")),
            ask_levels=asks,
            ask_available=_boolean(
                "bucket_ask_available",
                raw.get("ask_available", bool(asks)),
            ),
            bid_levels=bids,
            price_tick_size=_unit_interval(
                "bucket_price_tick_size",
                raw.get("price_tick_size", 0.01),
                closed=False,
            ),
            maker_queue_ahead_shares=_nonnegative(
                "bucket_maker_queue_ahead_shares", raw.get("maker_queue_ahead_shares", 0.0)
            ),
            maker_queue_known=_boolean("bucket_maker_queue_known", raw.get("maker_queue_known", False)),
            peripheral_model_reliability=_unit_interval(
                "bucket_peripheral_model_reliability",
                raw.get("peripheral_model_reliability", 0.0),
            ),
            filled_shares=_nonnegative("bucket_filled_shares", raw.get("filled_shares", 0.0)),
            filled_cost_usdc=_nonnegative("bucket_filled_cost_usdc", raw.get("filled_cost_usdc", 0.0)),
            pending_shares=_nonnegative("bucket_pending_shares", raw.get("pending_shares", 0.0)),
            pending_notional_usdc=_nonnegative(
                "bucket_pending_notional_usdc", raw.get("pending_notional_usdc", 0.0)
            ),
            active_before=_boolean("bucket_active_before", raw.get("active_before", False)),
        )
        prices = [level.price for level in value.ask_levels]
        if any(current <= previous + EPS for previous, current in zip(prices, prices[1:])):
            raise ContractError("bucket_ask_levels_must_be_strictly_increasing")
        bid_prices = [level.price for level in value.bid_levels]
        if any(
            current >= previous - EPS
            for previous, current in zip(bid_prices, bid_prices[1:])
        ):
            raise ContractError(
                "bucket_bid_levels_must_be_strictly_decreasing"
            )
        if value.bid_levels and (
            abs(value.bid_price - value.bid_levels[0].price) > EPS
        ):
            raise ContractError("bucket_best_bid_depth_mismatch")
        if value.ask_available != bool(value.ask_levels):
            raise ContractError("bucket_ask_availability_mismatch")
        if not value.ask_available and not value.bid_levels:
            raise ContractError("bucket_book_empty")
        quoted_prices = [
            *(level.price for level in value.ask_levels),
            *(level.price for level in value.bid_levels),
        ]
        if value.bid_price > EPS:
            quoted_prices.append(value.bid_price)
        if any(
            not _price_is_tick_aligned(price, value.price_tick_size)
            for price in quoted_prices
        ):
            raise ContractError("bucket_price_tick_mismatch")
        if (
            value.ask_available
            and value.bid_price + EPS >= value.ask_price
        ):
            raise ContractError("bucket_bid_must_be_below_ask")
        return value

    @property
    def ask_price(self) -> float:
        if not self.ask_available:
            raise ContractError("bucket_ask_unavailable")
        return self.ask_levels[0].price

    @property
    def executable_depth_shares(self) -> float:
        return sum(level.shares for level in self.ask_levels)

    @property
    def selected_before(self) -> bool:
        return self.active_before or self.filled_shares > EPS or self.pending_shares > EPS


@dataclass(frozen=True)
class TargetBasketPolicy:
    """Immutable candidate parameters; promotion must lock one full policy."""

    policy_id: str = "target_basket_target_candidate_v1"
    total_budget_usdc: float = 30.0
    base_coverage: float = 0.55
    entropy_coverage_gain: float = 0.20
    maximum_coverage: float = 0.92
    direction_skew_deadband: float = 0.05
    direction_entropy_max: float = 0.95
    direction_min_side_mass: float = 0.05
    center_lead_min_probability: float = 0.01
    edge_max_ask: float = 0.18
    edge_min_probability: float = 0.005
    peripheral_model_reliability_min: float = 0.15
    peripheral_min_coverage_value: float = 0.01
    max_disagreement_expansion_layers: int = 2
    dispersion_c_per_expansion_layer: float = 1.0
    clip_min_notional_usdc: float = 1.0
    # There is deliberately no legacy source-wallet clip fallback.  The
    # target compiler must bind this cap from its explicit self-owned
    # policy/cash phase input; a direct kernel policy without it is invalid.
    clip_max_notional_usdc: float | None = None
    price_tick_size: float = 0.01
    maximum_limit_price: float = 0.99
    minimum_expected_edge_per_share: float = 0.0
    minimum_expected_pnl_after_usdc: float = -30.0
    fee_rate: float = 0.0
    maker_queue_cost_per_share: float = 0.0
    taker_slippage_buffer_per_share: float = 0.0
    taker_max_quote_age_seconds: float = 5.0
    maker_timeout_seconds: float = 300.0
    taker_time_threshold_minutes: float = 120.0
    taker_urgency_threshold: float = 0.67
    taker_spread_reference: float = 0.10
    urgency_weight_time: float = 0.45
    urgency_weight_gap: float = 0.25
    urgency_weight_fill: float = 0.15
    urgency_weight_spread: float = 0.075
    urgency_weight_depth: float = 0.075
    protected_floor_usdc: float = -30.0
    max_outside_loss_usdc: float = 30.0
    basket_downside_lambda_per_usdc: float = 0.02
    basket_directional_coverage_lambda_usdc: float = 0.05
    basket_concentration_lambda_usdc: float = 0.25
    disagreement_concentration_gain_per_layer: float = 0.50
    maximum_bucket_budget_fraction: float = 0.75
    basket_optimizer_max_iterations: int = 4000
    basket_optimizer_tolerance: float = 1e-8

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "TargetBasketPolicy":
        raw = raw if isinstance(raw, Mapping) else {}
        deprecated_role_fields = {
            "allocation_mode",
            "base_budget_usdc",
            "bias_budget_usdc",
            "edge_budget_usdc",
            "base_share_target",
            "bias_share_target",
            "edge_share_target",
            "max_bias_buckets",
            "max_edge_buckets",
        }
        if deprecated_role_fields & set(raw):
            raise ContractError(
                "deprecated_role_allocation_field_forbidden"
            )
        if "dispersion_coverage_gain_per_c" in raw:
            raise ContractError(
                "legacy_dispersion_core_coverage_gain_forbidden"
            )
        clip_max_raw = raw.get("clip_max_notional_usdc", cls.clip_max_notional_usdc)
        if clip_max_raw is None:
            raise ContractError("clip_max_notional_usdc_required")
        value = cls(
            policy_id=_required_text("policy_id", raw.get("policy_id", cls.policy_id)),
            total_budget_usdc=_positive("total_budget_usdc", raw.get("total_budget_usdc", cls.total_budget_usdc)),
            base_coverage=_unit_interval("base_coverage", raw.get("base_coverage", cls.base_coverage)),
            entropy_coverage_gain=_nonnegative(
                "entropy_coverage_gain", raw.get("entropy_coverage_gain", cls.entropy_coverage_gain)
            ),
            maximum_coverage=_unit_interval("maximum_coverage", raw.get("maximum_coverage", cls.maximum_coverage)),
            direction_skew_deadband=_unit_interval(
                "direction_skew_deadband", raw.get("direction_skew_deadband", cls.direction_skew_deadband)
            ),
            direction_entropy_max=_unit_interval(
                "direction_entropy_max", raw.get("direction_entropy_max", cls.direction_entropy_max)
            ),
            direction_min_side_mass=_unit_interval(
                "direction_min_side_mass", raw.get("direction_min_side_mass", cls.direction_min_side_mass)
            ),
            center_lead_min_probability=_unit_interval(
                "center_lead_min_probability",
                raw.get("center_lead_min_probability", cls.center_lead_min_probability),
            ),
            edge_max_ask=_unit_interval("edge_max_ask", raw.get("edge_max_ask", cls.edge_max_ask), closed=False),
            edge_min_probability=_unit_interval(
                "edge_min_probability", raw.get("edge_min_probability", cls.edge_min_probability)
            ),
            peripheral_model_reliability_min=_unit_interval(
                "peripheral_model_reliability_min",
                raw.get(
                    "peripheral_model_reliability_min",
                    cls.peripheral_model_reliability_min,
                ),
            ),
            peripheral_min_coverage_value=_nonnegative(
                "peripheral_min_coverage_value",
                raw.get(
                    "peripheral_min_coverage_value",
                    cls.peripheral_min_coverage_value,
                ),
            ),
            max_disagreement_expansion_layers=int(
                raw.get(
                    "max_disagreement_expansion_layers",
                    cls.max_disagreement_expansion_layers,
                )
            ),
            dispersion_c_per_expansion_layer=_positive(
                "dispersion_c_per_expansion_layer",
                raw.get(
                    "dispersion_c_per_expansion_layer",
                    cls.dispersion_c_per_expansion_layer,
                ),
            ),
            clip_min_notional_usdc=_positive(
                "clip_min_notional_usdc", raw.get("clip_min_notional_usdc", cls.clip_min_notional_usdc)
            ),
            clip_max_notional_usdc=_positive(
                "clip_max_notional_usdc",
                clip_max_raw,
            ),
            price_tick_size=_unit_interval(
                "price_tick_size", raw.get("price_tick_size", cls.price_tick_size), closed=False
            ),
            maximum_limit_price=_unit_interval(
                "maximum_limit_price", raw.get("maximum_limit_price", cls.maximum_limit_price), closed=False
            ),
            minimum_expected_edge_per_share=_unit_interval(
                "minimum_expected_edge_per_share",
                raw.get("minimum_expected_edge_per_share", cls.minimum_expected_edge_per_share),
            ),
            minimum_expected_pnl_after_usdc=_finite(
                "minimum_expected_pnl_after_usdc",
                raw.get("minimum_expected_pnl_after_usdc", cls.minimum_expected_pnl_after_usdc),
            ),
            fee_rate=_unit_interval("fee_rate", raw.get("fee_rate", cls.fee_rate)),
            maker_queue_cost_per_share=_nonnegative(
                "maker_queue_cost_per_share",
                raw.get("maker_queue_cost_per_share", cls.maker_queue_cost_per_share),
            ),
            taker_slippage_buffer_per_share=_nonnegative(
                "taker_slippage_buffer_per_share",
                raw.get("taker_slippage_buffer_per_share", cls.taker_slippage_buffer_per_share),
            ),
            taker_max_quote_age_seconds=_positive(
                "taker_max_quote_age_seconds",
                raw.get("taker_max_quote_age_seconds", cls.taker_max_quote_age_seconds),
            ),
            maker_timeout_seconds=_positive(
                "maker_timeout_seconds", raw.get("maker_timeout_seconds", cls.maker_timeout_seconds)
            ),
            taker_time_threshold_minutes=_positive(
                "taker_time_threshold_minutes",
                raw.get("taker_time_threshold_minutes", cls.taker_time_threshold_minutes),
            ),
            taker_urgency_threshold=_unit_interval(
                "taker_urgency_threshold", raw.get("taker_urgency_threshold", cls.taker_urgency_threshold)
            ),
            taker_spread_reference=_unit_interval(
                "taker_spread_reference",
                raw.get("taker_spread_reference", cls.taker_spread_reference),
                closed=False,
            ),
            urgency_weight_time=_unit_interval(
                "urgency_weight_time", raw.get("urgency_weight_time", cls.urgency_weight_time)
            ),
            urgency_weight_gap=_unit_interval(
                "urgency_weight_gap", raw.get("urgency_weight_gap", cls.urgency_weight_gap)
            ),
            urgency_weight_fill=_unit_interval(
                "urgency_weight_fill", raw.get("urgency_weight_fill", cls.urgency_weight_fill)
            ),
            urgency_weight_spread=_unit_interval(
                "urgency_weight_spread", raw.get("urgency_weight_spread", cls.urgency_weight_spread)
            ),
            urgency_weight_depth=_unit_interval(
                "urgency_weight_depth", raw.get("urgency_weight_depth", cls.urgency_weight_depth)
            ),
            protected_floor_usdc=_finite("protected_floor_usdc", raw.get("protected_floor_usdc", cls.protected_floor_usdc)),
            max_outside_loss_usdc=_positive(
                "max_outside_loss_usdc", raw.get("max_outside_loss_usdc", cls.max_outside_loss_usdc)
            ),
            basket_downside_lambda_per_usdc=_nonnegative(
                "basket_downside_lambda_per_usdc",
                raw.get(
                    "basket_downside_lambda_per_usdc",
                    cls.basket_downside_lambda_per_usdc,
                ),
            ),
            basket_directional_coverage_lambda_usdc=_nonnegative(
                "basket_directional_coverage_lambda_usdc",
                raw.get(
                    "basket_directional_coverage_lambda_usdc",
                    cls.basket_directional_coverage_lambda_usdc,
                ),
            ),
            basket_concentration_lambda_usdc=_positive(
                "basket_concentration_lambda_usdc",
                raw.get(
                    "basket_concentration_lambda_usdc",
                    cls.basket_concentration_lambda_usdc,
                ),
            ),
            disagreement_concentration_gain_per_layer=_nonnegative(
                "disagreement_concentration_gain_per_layer",
                raw.get(
                    "disagreement_concentration_gain_per_layer",
                    cls.disagreement_concentration_gain_per_layer,
                ),
            ),
            maximum_bucket_budget_fraction=_unit_interval(
                "maximum_bucket_budget_fraction",
                raw.get(
                    "maximum_bucket_budget_fraction",
                    cls.maximum_bucket_budget_fraction,
                ),
                closed=False,
            ),
            basket_optimizer_max_iterations=int(
                raw.get(
                    "basket_optimizer_max_iterations",
                    cls.basket_optimizer_max_iterations,
                )
            ),
            basket_optimizer_tolerance=_positive(
                "basket_optimizer_tolerance",
                raw.get(
                    "basket_optimizer_tolerance",
                    cls.basket_optimizer_tolerance,
                ),
            ),
        )
        value.validate()
        return value

    def validate(self) -> None:
        if self.clip_max_notional_usdc is None:
            raise ContractError("clip_max_notional_usdc_required")
        if self.maximum_coverage + EPS < self.base_coverage:
            raise ContractError("maximum_coverage_below_base_coverage")
        if not 0 <= self.max_disagreement_expansion_layers <= 2:
            raise ContractError("disagreement_expansion_cap_out_of_range")
        if self.clip_max_notional_usdc + EPS < self.clip_min_notional_usdc:
            raise ContractError("clip_max_below_clip_min")
        if self.price_tick_size > self.maximum_limit_price + EPS:
            raise ContractError("price_tick_exceeds_maximum_limit")
        if self.minimum_expected_edge_per_share >= 1.0 - EPS:
            raise ContractError("minimum_expected_edge_too_large")
        if self.maker_queue_cost_per_share >= 1.0 or self.taker_slippage_buffer_per_share >= 1.0:
            raise ContractError("execution_cost_buffer_too_large")
        if self.basket_optimizer_max_iterations <= 0:
            raise ContractError("basket_optimizer_max_iterations_must_be_positive")
        if self.basket_optimizer_tolerance >= 1e-3:
            raise ContractError("basket_optimizer_tolerance_too_large")
        urgency_weight_sum = (
            self.urgency_weight_time
            + self.urgency_weight_gap
            + self.urgency_weight_fill
            + self.urgency_weight_spread
            + self.urgency_weight_depth
        )
        if abs(urgency_weight_sum - 1.0) > 1e-9:
            raise ContractError("urgency_weights_must_sum_to_one")
    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["schema"] = TARGET_BASKET_POLICY_SCHEMA
        return result


@dataclass(frozen=True)
class TargetBasketSnapshot:
    snapshot_id: str
    sequence: int
    observed_at: str
    market_id: str
    station_id: str
    event_date: str
    quote_version: str
    quote_observed_at: str
    quote_source: str
    inventory_version: str
    order_state_version: str
    evidence: MaterializedEvidence
    buckets: tuple[BucketObservation, ...]
    current_core_buckets: tuple[str, ...] = ()
    current_direction: Direction | None = None
    checkpoint_changed: bool = False
    quote_changed: bool = False
    fill_changed: bool = False
    time_gate_changed: bool = False
    evidence_fresh: bool = True
    contract_matches: bool = True
    order_state_known: bool = True
    risk_inputs_complete: bool = True
    terminal: bool = False
    time_left_minutes: float = 1440.0
    recent_fill_rate: float = 0.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TargetBasketSnapshot":
        if str(raw.get("schema") or TARGET_BASKET_SNAPSHOT_SCHEMA) != TARGET_BASKET_SNAPSHOT_SCHEMA:
            raise ContractError("snapshot_schema_invalid")
        bucket_values = raw.get("buckets")
        if not isinstance(bucket_values, Sequence) or isinstance(bucket_values, (str, bytes, bytearray)):
            raise ContractError("snapshot_buckets_invalid")
        buckets = tuple(BucketObservation.from_mapping(item) for item in bucket_values if isinstance(item, Mapping))
        if len(buckets) != len(bucket_values) or not buckets:
            raise ContractError("snapshot_bucket_row_invalid")
        try:
            sequence = int(raw.get("sequence"))
        except (TypeError, ValueError) as exc:
            raise ContractError("snapshot_sequence_invalid") from exc
        value = cls(
            snapshot_id=_required_text("snapshot_id", raw.get("snapshot_id")),
            sequence=sequence,
            observed_at=_iso8601("observed_at", raw.get("observed_at")),
            market_id=_required_text("market_id", raw.get("market_id")),
            station_id=_required_text("station_id", raw.get("station_id")),
            event_date=_iso_date("event_date", raw.get("event_date")),
            quote_version=_required_text("quote_version", raw.get("quote_version")),
            quote_observed_at=_iso8601("quote_observed_at", raw.get("quote_observed_at")),
            quote_source=_required_text("quote_source", raw.get("quote_source")),
            inventory_version=_required_text("inventory_version", raw.get("inventory_version")),
            order_state_version=_required_text("order_state_version", raw.get("order_state_version")),
            evidence=MaterializedEvidence.from_mapping(raw.get("evidence") or {}),
            buckets=buckets,
            current_core_buckets=tuple(
                _required_text("current_core_bucket", item)
                for item in (raw.get("current_core_buckets") or ())
            ),
            current_direction=_optional_direction(
                raw.get("current_direction")
            ),
            checkpoint_changed=_boolean("checkpoint_changed", raw.get("checkpoint_changed", False)),
            quote_changed=_boolean("quote_changed", raw.get("quote_changed", False)),
            fill_changed=_boolean("fill_changed", raw.get("fill_changed", False)),
            time_gate_changed=_boolean("time_gate_changed", raw.get("time_gate_changed", False)),
            evidence_fresh=_boolean("evidence_fresh", raw.get("evidence_fresh", True)),
            contract_matches=_boolean("contract_matches", raw.get("contract_matches", True)),
            order_state_known=_boolean("order_state_known", raw.get("order_state_known", True)),
            risk_inputs_complete=_boolean("risk_inputs_complete", raw.get("risk_inputs_complete", True)),
            terminal=_boolean("terminal", raw.get("terminal", False)),
            time_left_minutes=_nonnegative("time_left_minutes", raw.get("time_left_minutes", 1440.0)),
            recent_fill_rate=_unit_interval("recent_fill_rate", raw.get("recent_fill_rate", 0.0)),
        )
        value.validate()
        return value

    def validate(self) -> None:
        if self.sequence < 0:
            raise ContractError("snapshot_sequence_negative")
        buckets = [item.bucket for item in self.buckets]
        ordinals = [item.ordinal for item in self.buckets]
        if len(set(buckets)) != len(buckets):
            raise ContractError("snapshot_bucket_duplicate")
        if len(set(ordinals)) != len(ordinals):
            raise ContractError("snapshot_bucket_ordinal_duplicate")
        if len(set(self.current_core_buckets)) != len(
            self.current_core_buckets
        ):
            raise ContractError("snapshot_current_core_bucket_duplicate")
        if set(self.current_core_buckets) - set(buckets):
            raise ContractError("snapshot_current_core_bucket_unknown")
        if self.current_core_buckets:
            ordinal_by_bucket = {
                item.bucket: item.ordinal for item in self.buckets
            }
            indexes = tuple(
                ordinal_by_bucket[bucket]
                for bucket in self.current_core_buckets
            )
            lower, upper = min(indexes), max(indexes)
            canonical = tuple(
                item.bucket
                for item in sorted(
                    self.buckets,
                    key=lambda row: (row.ordinal, row.bucket),
                )
                if lower <= item.ordinal <= upper
            )
            if self.current_core_buckets != canonical:
                raise ContractError(
                    "snapshot_current_core_buckets_not_canonical_contiguous"
                )
        if self.quote_source != "OFFICIAL_CLOB_WS":
            raise ContractError("snapshot_quote_source_not_official_clob_ws")
        observed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        quote_observed = datetime.fromisoformat(self.quote_observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None or quote_observed.tzinfo is None:
            raise ContractError("snapshot_timestamps_must_be_timezone_aware")
        if quote_observed > observed:
            raise ContractError("snapshot_quote_observed_in_future")
        probability_sum = sum(item.probability for item in self.buckets)
        if abs(probability_sum - 1.0) > 1e-6:
            raise ContractError("snapshot_probability_sum_not_one")

    @property
    def trigger_changed(self) -> bool:
        return self.checkpoint_changed or self.quote_changed or self.fill_changed or self.time_gate_changed

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["schema"] = TARGET_BASKET_SNAPSHOT_SCHEMA
        return value

    @property
    def content_sha256(self) -> str:
        return content_sha256(self.to_dict())


@dataclass(frozen=True)
class TakerDepthSnapshot:
    depth_snapshot_id: str
    market_id: str
    event_date: str
    bucket: str
    quote_version: str
    quote_observed_at: str
    evaluated_at: str
    quote_age_seconds: float
    ask_levels: tuple[AskLevel, ...]
    source: str = "OFFICIAL_CLOB_WS"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TakerDepthSnapshot":
        ask_values = raw.get("ask_levels")
        if not isinstance(ask_values, Sequence) or isinstance(ask_values, (str, bytes, bytearray)):
            raise ContractError("taker_depth_ask_levels_invalid")
        asks = tuple(AskLevel.from_mapping(item) for item in ask_values if isinstance(item, Mapping))
        if len(asks) != len(ask_values) or not asks:
            raise ContractError("taker_depth_ask_level_row_invalid")
        value = cls(
            depth_snapshot_id=_required_text("taker_depth_snapshot_id", raw.get("depth_snapshot_id")),
            market_id=_required_text("taker_depth_market_id", raw.get("market_id")),
            event_date=_iso_date("taker_depth_event_date", raw.get("event_date")),
            bucket=_required_text("taker_depth_bucket", raw.get("bucket")),
            quote_version=_required_text("taker_depth_quote_version", raw.get("quote_version")),
            quote_observed_at=_iso8601("taker_depth_quote_observed_at", raw.get("quote_observed_at")),
            evaluated_at=_iso8601("taker_depth_evaluated_at", raw.get("evaluated_at")),
            quote_age_seconds=_nonnegative("taker_depth_quote_age_seconds", raw.get("quote_age_seconds")),
            ask_levels=asks,
            source=_required_text("taker_depth_source", raw.get("source")),
        )
        prices = [level.price for level in value.ask_levels]
        if any(current <= previous + EPS for previous, current in zip(prices, prices[1:])):
            raise ContractError("taker_depth_prices_must_be_strictly_increasing")
        if value.source != "OFFICIAL_CLOB_WS":
            raise ContractError("taker_depth_source_not_official_clob_ws")
        if not value.depth_snapshot_id.startswith("taker_depth_"):
            raise ContractError("taker_depth_snapshot_id_invalid")
        identity = value.identity_dict()
        expected = "taker_depth_" + content_sha256(identity)[:32]
        if value.depth_snapshot_id != expected:
            raise ContractError("taker_depth_snapshot_identity_mismatch")
        return value

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": TAKER_DEPTH_SNAPSHOT_SCHEMA,
            "market_id": self.market_id,
            "event_date": self.event_date,
            "bucket": self.bucket,
            "quote_version": self.quote_version,
            "quote_observed_at": self.quote_observed_at,
            "evaluated_at": self.evaluated_at,
            "quote_age_seconds": self.quote_age_seconds,
            "ask_levels": [level.to_dict() for level in self.ask_levels],
            "source": self.source,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"depth_snapshot_id": self.depth_snapshot_id, **self.identity_dict()}

    @property
    def content_sha256(self) -> str:
        return content_sha256(self.to_dict())


@dataclass(frozen=True)
class TargetPriceResult:
    pricing_id: str
    input_sha256: str
    bucket: str
    candidate_shares: float
    posterior_probability: float
    current_expected_pnl_usdc: float
    minimum_expected_pnl_after_usdc: float
    minimum_expected_edge_per_share: float
    fee_rate: float
    remaining_budget_usdc: float
    protected_floor_usdc: float
    all_in_caps_per_share: Mapping[str, float]
    binding_constraints: tuple[str, ...]
    raw_all_in_cap_per_share: float
    limit_price: float
    all_in_limit_cost_per_share: float
    expected_pnl_after_at_limit_usdc: float
    minimum_protected_pnl_at_limit_usdc: float
    capital_after_at_limit_usdc: float
    feasible: bool
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TARGET_PRICE_RESULT_SCHEMA,
            "pricing_id": self.pricing_id,
            "input_sha256": self.input_sha256,
            "bucket": self.bucket,
            "candidate_shares": self.candidate_shares,
            "posterior_probability": self.posterior_probability,
            "current_expected_pnl_usdc": self.current_expected_pnl_usdc,
            "minimum_expected_pnl_after_usdc": self.minimum_expected_pnl_after_usdc,
            "minimum_expected_edge_per_share": self.minimum_expected_edge_per_share,
            "fee_rate": self.fee_rate,
            "remaining_budget_usdc": self.remaining_budget_usdc,
            "protected_floor_usdc": self.protected_floor_usdc,
            "all_in_caps_per_share": dict(self.all_in_caps_per_share),
            "binding_constraints": list(self.binding_constraints),
            "raw_all_in_cap_per_share": self.raw_all_in_cap_per_share,
            "limit_price": self.limit_price,
            "all_in_limit_cost_per_share": self.all_in_limit_cost_per_share,
            "expected_pnl_after_at_limit_usdc": self.expected_pnl_after_at_limit_usdc,
            "minimum_protected_pnl_at_limit_usdc": self.minimum_protected_pnl_at_limit_usdc,
            "capital_after_at_limit_usdc": self.capital_after_at_limit_usdc,
            "feasible": self.feasible,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class TargetTakerCandidate:
    candidate_id: str
    market_id: str
    event_date: str
    bucket: str
    created_at: str
    requested_shares: float
    maximum_exchange_price: float
    all_in_limit_cost_per_share: float
    target_price_id: str
    depth_snapshot_id: str
    fee_rate: float
    slippage_buffer_per_share: float
    quote_version: str
    lineage: Mapping[str, str]
    fok: bool = True
    target_only: bool = True
    places_orders: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TargetTakerCandidate":
        lineage_raw = raw.get("lineage")
        if not isinstance(lineage_raw, Mapping):
            raise ContractError("taker_candidate_lineage_invalid")
        lineage = {
            _required_text("taker_candidate_lineage_key", key): _required_text(
                "taker_candidate_lineage_value", value
            )
            for key, value in lineage_raw.items()
        }
        required_lineage = {
            "posterior_version",
            "posterior_sha256",
            "snapshot_sha256",
            "quote_version",
            "inventory_version",
            "order_state_version",
            "target_price_id",
            "depth_snapshot_id",
        }
        if not required_lineage.issubset(lineage):
            raise ContractError("taker_candidate_lineage_incomplete")
        if "fok" in raw and _boolean("taker_candidate_fok", raw.get("fok")) is not True:
            raise ContractError("taker_candidate_must_be_fok")
        if "target_only" in raw and _boolean("taker_candidate_target_only", raw.get("target_only")) is not True:
            raise ContractError("taker_candidate_must_be_target_only")
        if "places_orders" in raw and _boolean("taker_candidate_places_orders", raw.get("places_orders")) is not False:
            raise ContractError("taker_candidate_places_orders_forbidden")
        value = cls(
            candidate_id=_required_text("taker_candidate_id", raw.get("candidate_id")),
            market_id=_required_text("taker_candidate_market_id", raw.get("market_id")),
            event_date=_iso_date("taker_candidate_event_date", raw.get("event_date")),
            bucket=_required_text("taker_candidate_bucket", raw.get("bucket")),
            created_at=_iso8601("taker_candidate_created_at", raw.get("created_at")),
            requested_shares=_positive("taker_candidate_requested_shares", raw.get("requested_shares")),
            maximum_exchange_price=_unit_interval(
                "taker_candidate_maximum_exchange_price",
                raw.get("maximum_exchange_price"),
                closed=False,
            ),
            all_in_limit_cost_per_share=_positive(
                "taker_candidate_all_in_limit_cost_per_share",
                raw.get("all_in_limit_cost_per_share"),
            ),
            target_price_id=_required_text("taker_candidate_target_price_id", raw.get("target_price_id")),
            depth_snapshot_id=_required_text("taker_candidate_depth_snapshot_id", raw.get("depth_snapshot_id")),
            fee_rate=_unit_interval("taker_candidate_fee_rate", raw.get("fee_rate")),
            slippage_buffer_per_share=_nonnegative(
                "taker_candidate_slippage_buffer_per_share",
                raw.get("slippage_buffer_per_share"),
            ),
            quote_version=_required_text("taker_candidate_quote_version", raw.get("quote_version")),
            lineage=lineage,
        )
        if not value.candidate_id.startswith("taker_candidate_"):
            raise ContractError("taker_candidate_id_invalid")
        if value.quote_version != lineage["quote_version"]:
            raise ContractError("taker_candidate_quote_lineage_mismatch")
        if value.target_price_id != lineage["target_price_id"]:
            raise ContractError("taker_candidate_target_price_lineage_mismatch")
        if value.depth_snapshot_id != lineage["depth_snapshot_id"]:
            raise ContractError("taker_candidate_depth_lineage_mismatch")
        expected = "taker_candidate_" + content_sha256(value.identity_dict())[:32]
        if value.candidate_id != expected:
            raise ContractError("taker_candidate_identity_mismatch")
        return value

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": TARGET_TAKER_CANDIDATE_SCHEMA,
            "market_id": self.market_id,
            "event_date": self.event_date,
            "bucket": self.bucket,
            "created_at": self.created_at,
            "requested_shares": self.requested_shares,
            "maximum_exchange_price": self.maximum_exchange_price,
            "all_in_limit_cost_per_share": self.all_in_limit_cost_per_share,
            "target_price_id": self.target_price_id,
            "depth_snapshot_id": self.depth_snapshot_id,
            "fee_rate": self.fee_rate,
            "slippage_buffer_per_share": self.slippage_buffer_per_share,
            "quote_version": self.quote_version,
            "lineage": dict(self.lineage),
            "fok": True,
            "target_only": True,
            "places_orders": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, **self.identity_dict()}


@dataclass(frozen=True)
class TargetTakerReceipt:
    receipt_id: str
    candidate_id: str
    depth_snapshot_id: str
    state: TakerSimulationState
    requested_shares: float
    filled_shares: float
    vwap_price: float
    gross_notional_usdc: float
    fee_usdc: float
    slippage_buffer_usdc: float
    all_in_notional_usdc: float
    all_in_effective_price: float
    marginal_price: float
    maximum_exchange_price: float
    depth_levels_consumed: int
    available_shares_within_limit: float
    quote_age_seconds: float
    reconcile_required: bool
    reason_codes: tuple[str, ...]
    target_only: bool = True
    places_orders: bool = False
    orders_submitted: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TARGET_TAKER_RECEIPT_SCHEMA,
            "receipt_id": self.receipt_id,
            "candidate_id": self.candidate_id,
            "depth_snapshot_id": self.depth_snapshot_id,
            "state": self.state.value,
            "requested_shares": self.requested_shares,
            "filled_shares": self.filled_shares,
            "vwap_price": self.vwap_price,
            "gross_notional_usdc": self.gross_notional_usdc,
            "fee_usdc": self.fee_usdc,
            "slippage_buffer_usdc": self.slippage_buffer_usdc,
            "all_in_notional_usdc": self.all_in_notional_usdc,
            "all_in_effective_price": self.all_in_effective_price,
            "marginal_price": self.marginal_price,
            "maximum_exchange_price": self.maximum_exchange_price,
            "depth_levels_consumed": self.depth_levels_consumed,
            "available_shares_within_limit": self.available_shares_within_limit,
            "quote_age_seconds": self.quote_age_seconds,
            "reconcile_required": self.reconcile_required,
            "reason_codes": list(self.reason_codes),
            "target_only": True,
            "places_orders": False,
            "orders_submitted": 0,
        }


@dataclass(frozen=True)
class UrgencyEvaluation:
    urgency_id: str
    snapshot_sha256: str
    policy_sha256: str
    bucket: str
    target_shares: float
    delta_shares: float
    spread: float
    total_ask_depth_shares: float
    components: Mapping[str, float]
    weights: Mapping[str, float]
    urgency_score: float
    urgency_threshold: float
    urgent: bool
    trigger_flags: Mapping[str, bool]
    trigger_changed: bool
    reason_codes: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "UrgencyEvaluation":
        components_raw = raw.get("components")
        weights_raw = raw.get("weights")
        flags_raw = raw.get("trigger_flags")
        if not isinstance(components_raw, Mapping) or not isinstance(weights_raw, Mapping):
            raise ContractError("urgency_component_mapping_invalid")
        if not isinstance(flags_raw, Mapping):
            raise ContractError("urgency_trigger_flags_invalid")
        required_components = {"time", "gap", "fill", "spread", "depth"}
        if set(components_raw) != required_components or set(weights_raw) != required_components:
            raise ContractError("urgency_component_keys_invalid")
        required_flags = {"checkpoint", "quote", "fill", "time_gate"}
        if set(flags_raw) != required_flags:
            raise ContractError("urgency_trigger_flag_keys_invalid")
        reasons_raw = raw.get("reason_codes") or []
        if not isinstance(reasons_raw, Sequence) or isinstance(reasons_raw, (str, bytes, bytearray)):
            raise ContractError("urgency_reason_codes_invalid")
        value = cls(
            urgency_id=_required_text("urgency_id", raw.get("urgency_id")),
            snapshot_sha256=_sha256_text("urgency_snapshot_sha256", raw.get("snapshot_sha256")),
            policy_sha256=_sha256_text("urgency_policy_sha256", raw.get("policy_sha256")),
            bucket=_required_text("urgency_bucket", raw.get("bucket")),
            target_shares=_nonnegative("urgency_target_shares", raw.get("target_shares")),
            delta_shares=_nonnegative("urgency_delta_shares", raw.get("delta_shares")),
            spread=_nonnegative("urgency_spread", raw.get("spread")),
            total_ask_depth_shares=_nonnegative(
                "urgency_total_ask_depth_shares", raw.get("total_ask_depth_shares")
            ),
            components={key: _unit_interval(f"urgency_component_{key}", components_raw[key]) for key in sorted(required_components)},
            weights={key: _unit_interval(f"urgency_weight_{key}", weights_raw[key]) for key in sorted(required_components)},
            urgency_score=_unit_interval("urgency_score", raw.get("urgency_score")),
            urgency_threshold=_unit_interval("urgency_threshold", raw.get("urgency_threshold")),
            urgent=_boolean("urgency_urgent", raw.get("urgent")),
            trigger_flags={key: _boolean(f"urgency_trigger_{key}", flags_raw[key]) for key in sorted(required_flags)},
            trigger_changed=_boolean("urgency_trigger_changed", raw.get("trigger_changed")),
            reason_codes=tuple(_required_text("urgency_reason_code", item) for item in reasons_raw),
        )
        expected = "urgency_" + content_sha256(value.identity_dict())[:32]
        if value.urgency_id != expected:
            raise ContractError("urgency_identity_mismatch")
        if abs(sum(value.weights.values()) - 1.0) > 1e-9:
            raise ContractError("urgency_weights_must_sum_to_one")
        if value.urgent != (value.urgency_score + EPS >= value.urgency_threshold):
            raise ContractError("urgency_boolean_score_mismatch")
        if value.trigger_changed != any(value.trigger_flags.values()):
            raise ContractError("urgency_trigger_boolean_mismatch")
        return value

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": URGENCY_EVALUATION_SCHEMA,
            "snapshot_sha256": self.snapshot_sha256,
            "policy_sha256": self.policy_sha256,
            "bucket": self.bucket,
            "target_shares": self.target_shares,
            "delta_shares": self.delta_shares,
            "spread": self.spread,
            "total_ask_depth_shares": self.total_ask_depth_shares,
            "components": dict(self.components),
            "weights": dict(self.weights),
            "urgency_score": self.urgency_score,
            "urgency_threshold": self.urgency_threshold,
            "urgent": self.urgent,
            "trigger_flags": dict(self.trigger_flags),
            "trigger_changed": self.trigger_changed,
            "reason_codes": list(self.reason_codes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"urgency_id": self.urgency_id, **self.identity_dict()}


@dataclass(frozen=True)
class ExecutionGateDecision:
    gate_id: str
    urgency_id: str
    snapshot_sha256: str
    policy_sha256: str
    bucket: str
    proposed_execution: ExecutionCandidate
    proposed_notional_usdc: float
    effective_price: float
    all_in_limit_cost_per_share: float
    committed_notional_usdc: float
    budget_usdc: float
    expected_versions: Mapping[str, str]
    candidate_versions: Mapping[str, str]
    evidence_fresh: bool
    contract_matches: bool
    budget_ok: bool
    order_state_known: bool
    risk_inputs_complete: bool
    risk_floor_ok: bool
    delta_positive: bool
    trigger_changed: bool
    clip_minimum_met: bool
    effective_price_ok: bool
    versions_match: bool
    candidate_present: bool
    allow: bool
    reason_codes: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ExecutionGateDecision":
        expected_raw = raw.get("expected_versions")
        candidate_raw = raw.get("candidate_versions")
        if not isinstance(expected_raw, Mapping) or not isinstance(candidate_raw, Mapping):
            raise ContractError("execution_gate_versions_invalid")
        expected = {_required_text("execution_gate_version_key", key): _required_text("execution_gate_expected_version", val) for key, val in expected_raw.items()}
        candidate = {_required_text("execution_gate_version_key", key): _required_text("execution_gate_candidate_version", val) for key, val in candidate_raw.items()}
        required_versions = {
            "posterior_version", "posterior_sha256", "snapshot_sha256", "quote_version",
            "inventory_version", "order_state_version", "target_price_id",
        }
        if set(expected) != required_versions or set(candidate) != required_versions:
            raise ContractError("execution_gate_version_keys_invalid")
        try:
            proposed_execution = ExecutionCandidate(str(raw.get("proposed_execution") or ""))
        except ValueError as exc:
            raise ContractError("execution_gate_proposed_execution_invalid") from exc
        reasons_raw = raw.get("reason_codes") or []
        if not isinstance(reasons_raw, Sequence) or isinstance(reasons_raw, (str, bytes, bytearray)):
            raise ContractError("execution_gate_reason_codes_invalid")
        value = cls(
            gate_id=_required_text("execution_gate_id", raw.get("gate_id")),
            urgency_id=_required_text("execution_gate_urgency_id", raw.get("urgency_id")),
            snapshot_sha256=_sha256_text("execution_gate_snapshot_sha256", raw.get("snapshot_sha256")),
            policy_sha256=_sha256_text("execution_gate_policy_sha256", raw.get("policy_sha256")),
            bucket=_required_text("execution_gate_bucket", raw.get("bucket")),
            proposed_execution=proposed_execution,
            proposed_notional_usdc=_nonnegative("execution_gate_proposed_notional", raw.get("proposed_notional_usdc")),
            effective_price=_nonnegative("execution_gate_effective_price", raw.get("effective_price")),
            all_in_limit_cost_per_share=_nonnegative("execution_gate_all_in_limit", raw.get("all_in_limit_cost_per_share")),
            committed_notional_usdc=_nonnegative("execution_gate_committed_notional", raw.get("committed_notional_usdc")),
            budget_usdc=_positive("execution_gate_budget", raw.get("budget_usdc")),
            expected_versions=expected,
            candidate_versions=candidate,
            evidence_fresh=_boolean("execution_gate_evidence_fresh", raw.get("evidence_fresh")),
            contract_matches=_boolean("execution_gate_contract_matches", raw.get("contract_matches")),
            budget_ok=_boolean("execution_gate_budget_ok", raw.get("budget_ok")),
            order_state_known=_boolean("execution_gate_order_state_known", raw.get("order_state_known")),
            risk_inputs_complete=_boolean("execution_gate_risk_inputs_complete", raw.get("risk_inputs_complete")),
            risk_floor_ok=_boolean("execution_gate_risk_floor_ok", raw.get("risk_floor_ok")),
            delta_positive=_boolean("execution_gate_delta_positive", raw.get("delta_positive")),
            trigger_changed=_boolean("execution_gate_trigger_changed", raw.get("trigger_changed")),
            clip_minimum_met=_boolean("execution_gate_clip_minimum_met", raw.get("clip_minimum_met")),
            effective_price_ok=_boolean("execution_gate_effective_price_ok", raw.get("effective_price_ok")),
            versions_match=_boolean("execution_gate_versions_match", raw.get("versions_match")),
            candidate_present=_boolean("execution_gate_candidate_present", raw.get("candidate_present")),
            allow=_boolean("execution_gate_allow", raw.get("allow")),
            reason_codes=tuple(_required_text("execution_gate_reason_code", item) for item in reasons_raw),
        )
        expected_id = "execution_gate_" + content_sha256(value.identity_dict())[:32]
        if value.gate_id != expected_id:
            raise ContractError("execution_gate_identity_mismatch")
        factors = (
            value.evidence_fresh, value.contract_matches, value.budget_ok,
            value.order_state_known, value.risk_inputs_complete, value.risk_floor_ok,
            value.delta_positive, value.trigger_changed, value.clip_minimum_met,
            value.effective_price_ok, value.versions_match, value.candidate_present,
        )
        if value.allow != all(factors):
            raise ContractError("execution_gate_allow_factor_mismatch")
        return value

    def identity_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("gate_id", None)
        value["schema"] = EXECUTION_GATE_DECISION_SCHEMA
        value["proposed_execution"] = self.proposed_execution.value
        value["reason_codes"] = list(self.reason_codes)
        return value

    def to_dict(self) -> dict[str, Any]:
        return {"gate_id": self.gate_id, **self.identity_dict()}


@dataclass(frozen=True)
class BucketTarget:
    bucket: str
    ordinal: int
    role: BucketRole
    base_score: float
    bias_score: float
    edge_score: float
    target_cost_usdc: float
    target_shares: float
    filled_shares: float
    filled_cost_usdc: float
    pending_shares: float
    pending_notional_usdc: float
    delta_shares: float
    ask_price: float
    limit_price: float
    all_in_limit_cost_per_share: float
    target_price_id: str
    limit_binding_constraints: tuple[str, ...]
    price_eligible: bool
    immediate_depth_shares: float
    clip_shares: float
    clip_notional_usdc: float
    candidate_price: float
    effective_price: float
    execution_candidate: ExecutionCandidate
    execution_reason_codes: tuple[str, ...]
    urgency: UrgencyEvaluation
    execution_gate: ExecutionGateDecision
    maker_candidate_id: str | None = None
    taker_candidate_id: str | None = None
    taker_receipt_id: str | None = None
    taker_depth_snapshot_id: str | None = None
    taker_requested_shares: float = 0.0
    vwap_price: float = 0.0
    gross_notional_usdc: float = 0.0
    fee_usdc: float = 0.0
    slippage_buffer_usdc: float = 0.0
    all_in_effective_price: float = 0.0
    depth_levels_consumed: int = 0
    fok_fillable: bool = False
    maker_timeout_seconds: float = 0.0
    maker_grid_artifact_sha256: str | None = None
    maker_grid_artifact_curve_sha256: str | None = None
    maker_grid_point_sha256: str | None = None
    maker_grid_curve_sha256: str | None = None
    maker_queue_ahead_shares: float = 0.0
    maker_queue_known: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["role"] = self.role.value
        value["execution_candidate"] = self.execution_candidate.value
        value["limit_binding_constraints"] = list(self.limit_binding_constraints)
        value["execution_reason_codes"] = list(self.execution_reason_codes)
        value["urgency"] = self.urgency.to_dict()
        value["execution_gate"] = self.execution_gate.to_dict()
        return value


@dataclass(frozen=True)
class TargetMakerCandidate:
    candidate_id: str
    market_id: str
    event_date: str
    bucket: str
    created_at: str
    requested_shares: float
    limit_price: float
    all_in_limit_cost_per_share: float
    maker_price: float
    effective_price: float
    best_ask_at_creation: float
    initial_queue_ahead_shares: float
    timeout_seconds: float
    quote_version: str
    order_state_version: str
    lineage: Mapping[str, str]
    target_only: bool = True
    places_orders: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TargetMakerCandidate":
        lineage_raw = raw.get("lineage")
        if not isinstance(lineage_raw, Mapping):
            raise ContractError("maker_candidate_lineage_invalid")
        lineage = {
            _required_text("maker_candidate_lineage_key", key): _required_text(
                "maker_candidate_lineage_value", value
            )
            for key, value in lineage_raw.items()
        }
        required_lineage = {
            "snapshot_id", "snapshot_sha256", "posterior_version", "posterior_sha256",
            "quote_version", "inventory_version",
            "order_state_version", "target_price_id",
        }
        if not required_lineage.issubset(lineage):
            raise ContractError("maker_candidate_lineage_incomplete")
        if "target_only" in raw and _boolean("maker_candidate_target_only", raw.get("target_only")) is not True:
            raise ContractError("maker_candidate_must_be_target_only")
        if "places_orders" in raw and _boolean("maker_candidate_places_orders", raw.get("places_orders")) is not False:
            raise ContractError("maker_candidate_places_orders_forbidden")
        value = cls(
            candidate_id=_required_text("maker_candidate_id", raw.get("candidate_id")),
            market_id=_required_text("maker_candidate_market_id", raw.get("market_id")),
            event_date=_iso_date("maker_candidate_event_date", raw.get("event_date")),
            bucket=_required_text("maker_candidate_bucket", raw.get("bucket")),
            created_at=_iso8601("maker_candidate_created_at", raw.get("created_at")),
            requested_shares=_positive("maker_candidate_requested_shares", raw.get("requested_shares")),
            limit_price=_unit_interval("maker_candidate_limit_price", raw.get("limit_price"), closed=False),
            all_in_limit_cost_per_share=_positive(
                "maker_candidate_all_in_limit_cost_per_share",
                raw.get("all_in_limit_cost_per_share"),
            ),
            maker_price=_unit_interval("maker_candidate_maker_price", raw.get("maker_price"), closed=False),
            effective_price=_positive("maker_candidate_effective_price", raw.get("effective_price")),
            best_ask_at_creation=_unit_interval(
                "maker_candidate_best_ask", raw.get("best_ask_at_creation"), closed=False
            ),
            initial_queue_ahead_shares=_nonnegative(
                "maker_candidate_initial_queue", raw.get("initial_queue_ahead_shares", 0.0)
            ),
            timeout_seconds=_positive("maker_candidate_timeout_seconds", raw.get("timeout_seconds")),
            quote_version=_required_text("maker_candidate_quote_version", raw.get("quote_version")),
            order_state_version=_required_text(
                "maker_candidate_order_state_version", raw.get("order_state_version")
            ),
            lineage=lineage,
        )
        if not value.candidate_id.startswith("maker_candidate_"):
            raise ContractError("maker_candidate_id_invalid")
        if value.maker_price + EPS >= value.best_ask_at_creation:
            raise ContractError("maker_candidate_would_cross")
        if value.effective_price > value.all_in_limit_cost_per_share + EPS:
            raise ContractError("maker_candidate_effective_price_above_limit")
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TARGET_MAKER_CANDIDATE_SCHEMA,
            "candidate_id": self.candidate_id,
            "market_id": self.market_id,
            "event_date": self.event_date,
            "bucket": self.bucket,
            "created_at": self.created_at,
            "requested_shares": self.requested_shares,
            "limit_price": self.limit_price,
            "all_in_limit_cost_per_share": self.all_in_limit_cost_per_share,
            "maker_price": self.maker_price,
            "effective_price": self.effective_price,
            "best_ask_at_creation": self.best_ask_at_creation,
            "initial_queue_ahead_shares": self.initial_queue_ahead_shares,
            "timeout_seconds": self.timeout_seconds,
            "quote_version": self.quote_version,
            "order_state_version": self.order_state_version,
            "lineage": dict(self.lineage),
            "target_only": True,
            "places_orders": False,
        }


@dataclass(frozen=True)
class TargetMakerEvent:
    event_id: str
    candidate_id: str
    sequence: int
    observed_at: str
    event_type: MakerEventType
    shares: float = 0.0
    queue_ahead_shares: float | None = None
    cumulative_filled_shares: float | None = None
    exchange_status: MakerExchangeStatus | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TargetMakerEvent":
        try:
            sequence = int(raw.get("sequence"))
        except (TypeError, ValueError) as exc:
            raise ContractError("maker_event_sequence_invalid") from exc
        try:
            event_type = MakerEventType(str(raw.get("event_type") or ""))
        except ValueError as exc:
            raise ContractError("maker_event_type_invalid") from exc
        exchange_status = None
        if raw.get("exchange_status") is not None:
            try:
                exchange_status = MakerExchangeStatus(str(raw.get("exchange_status")))
            except ValueError as exc:
                raise ContractError("maker_event_exchange_status_invalid") from exc
        value = cls(
            event_id=_required_text("maker_event_id", raw.get("event_id")),
            candidate_id=_required_text("maker_event_candidate_id", raw.get("candidate_id")),
            sequence=sequence,
            observed_at=_iso8601("maker_event_observed_at", raw.get("observed_at")),
            event_type=event_type,
            shares=_nonnegative("maker_event_shares", raw.get("shares", 0.0)),
            queue_ahead_shares=(
                _nonnegative("maker_event_queue_ahead_shares", raw.get("queue_ahead_shares"))
                if raw.get("queue_ahead_shares") is not None
                else None
            ),
            cumulative_filled_shares=(
                _nonnegative("maker_event_cumulative_filled_shares", raw.get("cumulative_filled_shares"))
                if raw.get("cumulative_filled_shares") is not None
                else None
            ),
            exchange_status=exchange_status,
        )
        value.validate()
        return value

    def validate(self) -> None:
        if self.sequence < 0:
            raise ContractError("maker_event_sequence_negative")
        if self.event_type is MakerEventType.QUEUE_TRADE and self.shares <= EPS:
            raise ContractError("maker_queue_trade_shares_must_be_positive")
        if self.event_type is MakerEventType.QUEUE_SNAPSHOT and self.queue_ahead_shares is None:
            raise ContractError("maker_queue_snapshot_value_missing")
        if self.event_type is MakerEventType.FILL_REPORT and self.cumulative_filled_shares is None:
            raise ContractError("maker_fill_report_cumulative_missing")
        if self.event_type is MakerEventType.AUTHORITATIVE_STATUS:
            if self.exchange_status is None:
                raise ContractError("maker_authoritative_status_missing")
            if self.cumulative_filled_shares is None:
                raise ContractError("maker_authoritative_fill_missing")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TARGET_MAKER_EVENT_SCHEMA,
            "event_id": self.event_id,
            "candidate_id": self.candidate_id,
            "sequence": self.sequence,
            "observed_at": self.observed_at,
            "event_type": self.event_type.value,
            "shares": self.shares,
            "queue_ahead_shares": self.queue_ahead_shares,
            "cumulative_filled_shares": self.cumulative_filled_shares,
            "exchange_status": self.exchange_status.value if self.exchange_status else None,
        }

    @property
    def content_sha256(self) -> str:
        return content_sha256(self.to_dict())


@dataclass(frozen=True)
class TargetMakerReceipt:
    receipt_id: str
    candidate_id: str
    state: MakerLifecycleState
    requested_shares: float
    modeled_filled_shares: float
    authoritative_filled_shares: float | None
    filled_shares: float
    remaining_shares: float
    queue_ahead_shares: float
    cancel_requested: bool
    cancel_acknowledged: bool
    reconcile_required: bool
    reason_codes: tuple[str, ...]
    processed_event_ids: tuple[str, ...]
    duplicate_event_count: int
    target_only: bool = True
    places_orders: bool = False
    orders_submitted: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TARGET_MAKER_RECEIPT_SCHEMA,
            "receipt_id": self.receipt_id,
            "candidate_id": self.candidate_id,
            "state": self.state.value,
            "requested_shares": self.requested_shares,
            "modeled_filled_shares": self.modeled_filled_shares,
            "authoritative_filled_shares": self.authoritative_filled_shares,
            "filled_shares": self.filled_shares,
            "remaining_shares": self.remaining_shares,
            "queue_ahead_shares": self.queue_ahead_shares,
            "cancel_requested": self.cancel_requested,
            "cancel_acknowledged": self.cancel_acknowledged,
            "reconcile_required": self.reconcile_required,
            "reason_codes": list(self.reason_codes),
            "processed_event_ids": list(self.processed_event_ids),
            "duplicate_event_count": self.duplicate_event_count,
            "target_only": True,
            "places_orders": False,
            "orders_submitted": 0,
        }


@dataclass(frozen=True)
class TargetBasketPlan:
    plan_id: str
    input_sha256: str
    policy_sha256: str
    snapshot_id: str
    sequence: int
    market_id: str
    station_id: str
    event_date: str
    observed_at: str
    state: BasketState
    previous_state: BasketState | None
    reason_codes: tuple[str, ...]
    center_bucket: str
    runner_up_bucket: str | None
    center_lead_probability: float
    center_lead_threshold_probability: float
    center_stable: bool
    direction: Direction
    diagnostic_lower_ordinal: int | None
    diagnostic_upper_ordinal: int | None
    candidate_lower_ordinal: int
    candidate_upper_ordinal: int
    disagreement_expansion_layers: int
    underreach_risk_buckets: tuple[str, ...]
    upside_breakthrough_risk_buckets: tuple[str, ...]
    current_core_buckets: tuple[str, ...]
    core_buckets: tuple[str, ...]
    desired_support: tuple[str, ...]
    selected_before: tuple[str, ...]
    targets: tuple[BucketTarget, ...]
    total_target_cost_usdc: float
    total_target_shares: float
    total_delta_notional_usdc: float
    scenario_pnl_usdc: Mapping[str, float]
    outside_pnl_usdc: float
    basket_optimizer_id: str
    baseline_score: float
    proposed_score: float
    delta_basket_score: float
    expected_pnl_usdc: float
    downside_risk_usdc2: float
    concentration_index: float
    effective_concentration_lambda_usdc: float
    effective_maximum_bucket_budget_fraction: float
    realised_total_cost_usdc: float
    execution_cost_usdc: float
    basket_optimizer_iterations: int
    basket_optimizer_converged: bool
    strict_delta_audit: Mapping[str, Any] = field(default_factory=dict)
    immediate_all_in_yes_ask_sum: float | None = None
    immediate_execution_gate_passed: bool | None = None
    target_only: bool = True
    places_orders: bool = False
    writes_production_db: bool = False
    calls_order_gateway: bool = False
    lineage: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TARGET_BASKET_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "input_sha256": self.input_sha256,
            "policy_sha256": self.policy_sha256,
            "snapshot_id": self.snapshot_id,
            "sequence": self.sequence,
            "market_id": self.market_id,
            "station_id": self.station_id,
            "event_date": self.event_date,
            "observed_at": self.observed_at,
            "state": self.state.value,
            "previous_state": self.previous_state.value if self.previous_state else None,
            "reason_codes": list(self.reason_codes),
            "center_bucket": self.center_bucket,
            "runner_up_bucket": self.runner_up_bucket,
            "center_lead_probability": self.center_lead_probability,
            "center_lead_threshold_probability": self.center_lead_threshold_probability,
            "center_stable": self.center_stable,
            "direction": self.direction.value,
            "diagnostic_lower_ordinal": self.diagnostic_lower_ordinal,
            "diagnostic_upper_ordinal": self.diagnostic_upper_ordinal,
            "candidate_lower_ordinal": self.candidate_lower_ordinal,
            "candidate_upper_ordinal": self.candidate_upper_ordinal,
            "disagreement_expansion_layers": self.disagreement_expansion_layers,
            "underreach_risk_buckets": list(
                self.underreach_risk_buckets
            ),
            "upside_breakthrough_risk_buckets": list(
                self.upside_breakthrough_risk_buckets
            ),
            "current_core_buckets": list(self.current_core_buckets),
            "core_buckets": list(self.core_buckets),
            "desired_support": list(self.desired_support),
            "selected_before": list(self.selected_before),
            "targets": [item.to_dict() for item in self.targets],
            "total_target_cost_usdc": self.total_target_cost_usdc,
            "total_target_shares": self.total_target_shares,
            "total_delta_notional_usdc": self.total_delta_notional_usdc,
            "scenario_pnl_usdc": dict(self.scenario_pnl_usdc),
            "outside_pnl_usdc": self.outside_pnl_usdc,
            "basket_optimizer_id": self.basket_optimizer_id,
            "baseline_score": self.baseline_score,
            "proposed_score": self.proposed_score,
            "delta_basket_score": self.delta_basket_score,
            "expected_pnl_usdc": self.expected_pnl_usdc,
            "downside_risk_usdc2": self.downside_risk_usdc2,
            "concentration_index": self.concentration_index,
            "effective_concentration_lambda_usdc": self.effective_concentration_lambda_usdc,
            "effective_maximum_bucket_budget_fraction": self.effective_maximum_bucket_budget_fraction,
            "realised_total_cost_usdc": self.realised_total_cost_usdc,
            "execution_cost_usdc": self.execution_cost_usdc,
            "basket_optimizer_iterations": self.basket_optimizer_iterations,
            "basket_optimizer_converged": self.basket_optimizer_converged,
            "strict_delta_audit": dict(self.strict_delta_audit),
            "immediate_all_in_yes_ask_sum": self.immediate_all_in_yes_ask_sum,
            "immediate_execution_gate_passed": self.immediate_execution_gate_passed,
            "target_only": self.target_only,
            "places_orders": self.places_orders,
            "writes_production_db": self.writes_production_db,
            "calls_order_gateway": self.calls_order_gateway,
            "lineage": dict(self.lineage),
        }


__all__ = [
    "TARGET_BASKET_PLAN_SCHEMA",
    "TARGET_BASKET_POLICY_SCHEMA",
    "TARGET_BASKET_SNAPSHOT_SCHEMA",
    "TARGET_MAKER_CANDIDATE_SCHEMA",
    "TARGET_MAKER_EVENT_SCHEMA",
    "TARGET_MAKER_RECEIPT_SCHEMA",
    "TAKER_DEPTH_SNAPSHOT_SCHEMA",
    "TARGET_TAKER_CANDIDATE_SCHEMA",
    "TARGET_TAKER_RECEIPT_SCHEMA",
    "URGENCY_EVALUATION_SCHEMA",
    "EXECUTION_GATE_DECISION_SCHEMA",
    "TARGET_PRICE_RESULT_SCHEMA",
    "ZERO_ORDER_REPLAY_SCHEMA",
    "TargetBasketPlan",
    "TargetBasketPolicy",
    "TargetBasketSnapshot",
    "AskLevel",
    "BasketState",
    "BucketObservation",
    "BucketRole",
    "BucketTarget",
    "ContractError",
    "Direction",
    "EPS",
    "ExecutionCandidate",
    "ExecutionGateDecision",
    "MakerEventType",
    "MakerExchangeStatus",
    "MakerLifecycleState",
    "TargetMakerCandidate",
    "TargetMakerEvent",
    "TargetMakerReceipt",
    "MaterializedEvidence",
    "TargetPriceResult",
    "TakerDepthSnapshot",
    "TargetTakerCandidate",
    "TargetTakerReceipt",
    "TakerSimulationState",
    "UrgencyEvaluation",
    "canonical_json",
    "content_sha256",
]
