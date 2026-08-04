"""Deterministic pure-YES basket planner.

The planner itself never imports an order gateway and never writes state.  The
production adapter consumes its output and is the only code allowed to submit
the YES legs.  The basket consumes an existing P1/P0 market-bucket posterior;
the upstream model/TAF/regime chain is not rebuilt here.  METAR/SPECI is the
only source allowed to exclude lower buckets.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from weather_basket_yes_runtime import conservative_market_temperature, parse_bucket_interval, parse_metar_temperature
from .probability_adapter import extract_probability_projection
from .policy import (
    POLICY_ID,
    allocate_profit_weighted,
    validate_window_roles,
)


BASKET_STRATEGY_SCHEMA_VERSION = "weather_basket_yes_v2"
MODEL_IDS: tuple[str, ...] = ("gfs_seamless", "icon_seamless", "ecmwf_ifs025")
TAF_IDS: tuple[str, ...] = ("zuuu_taf_forecast_amsc", "zuuu_taf_forecast_peak")
LIVE_SUBMIT_AUTHORIZED = False
EPS = 1e-9

REGIME_UP_LABELS = frozenset(
    {
        "clear_dry",
        "forecast_low_cloud_fraction_direct",
        "dry_air_background",
        "dry_or_ventilated",
        "warm_ridge_proxy",
        "basin_heat_buildup",
        "weak_ventilation_heat_buildup",
        "moderate_ventilation_heat_buildup",
        "weak_ventilation_static",
        "boundary_layer_heating_proxy",
        "post_cool_rebound",
    }
)
REGIME_DOWN_LABELS = frozenset(
    {
        "convection_or_showers",
        "wet_precipitation",
        "cloudy_stable",
        "cloud_limited_or_rain_cooled",
        "cloud_limited_proxy",
        "monsoon_cloud_precip_proxy",
        "humid_monsoon",
        "humid_monsoon_background",
        "well_ventilated",
        "moderate_ventilation",
        "moist_column_cloud_favorable",
        "forecast_overcast_direct",
        "accumulated_precipitation_present_window_not_point_localized",
        "forecast_convective_cloud_geometry_direct",
        "convective_environment_favorable_not_occurrence",
        "fog_low_cloud",
    }
)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive(value: Any) -> float | None:
    parsed = _finite(value)
    return parsed if parsed is not None and parsed > 0 else None


def _iso(value: Any) -> str:
    return str(value or "").strip()


def _stable_id(prefix: str, value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _first_number(mapping: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        parsed = _finite(mapping.get(key))
        if parsed is not None:
            return parsed
    return None


def _member_mean(value: Any) -> float | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    values = [_finite(item) for item in value]
    values = [item for item in values if item is not None]
    return sum(values) / len(values) if values else None


@dataclass(frozen=True)
class BasketStrategyConfig:
    """Explicit, deterministic basket selection and execution controls.

    The selected window is contiguous. Quantities are assigned by the
    persisted policy: edge buckets provide settlement coverage while the
    fixed core positions receive the 2:1 profit-weighted allocation.
    """

    bankroll_usdc: float = 30.0
    basket_width: int = 3
    direction_deadband_c: float = 0.5
    minimum_guaranteed_profit_usdc: float = 0.0
    fee_rate: float = 0.0
    minimum_order_shares: float = 1.0
    live_submit_authorized: bool = False
    # Production can opt into progressive convergence without changing the
    # planner contract used by historical/offline callers.  The active lane
    # enables this as 4 -> 3 -> 2; each tranche is still an all-YES basket
    # held to settlement.
    dynamic_convergence_enabled: bool = False
    convergence_widths: tuple[int, ...] = (4, 3, 2)
    convergence_target_4: float = 0.60
    convergence_target_3: float = 0.72
    convergence_target_2: float = 0.84
    tranche_budget_usdc: float | None = None
    allow_dynamic_additions: bool = False
    # The production basket can temporarily disable only the regime
    # agreement gate.  Probability-center selection remains mandatory.
    regime_gate_enabled: bool = True
    policy_id: str = POLICY_ID
    allocation_mode: str = "profit_weighted_settlement_floor"
    role_probability_tolerance: float = 0.0
    edge_profit_floor_usdc: float = 0.000001
    parent_order_cap_usdc: float = 2.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "BasketStrategyConfig":
        raw = raw if isinstance(raw, Mapping) else {}
        values = {
            "bankroll_usdc": raw.get("bankroll_usdc", cls.bankroll_usdc),
            "basket_width": raw.get("basket_width", cls.basket_width),
            "direction_deadband_c": raw.get("direction_deadband_c", cls.direction_deadband_c),
            "minimum_guaranteed_profit_usdc": raw.get(
                "minimum_guaranteed_profit_usdc", cls.minimum_guaranteed_profit_usdc
            ),
            "fee_rate": raw.get("fee_rate", cls.fee_rate),
            "minimum_order_shares": raw.get("minimum_order_shares", cls.minimum_order_shares),
            "live_submit_authorized": raw.get("live_submit_authorized", cls.live_submit_authorized),
            "dynamic_convergence_enabled": raw.get("dynamic_convergence_enabled", cls.dynamic_convergence_enabled),
            "convergence_widths": tuple(int(item) for item in (raw.get("convergence_widths") or cls.convergence_widths)),
            "convergence_target_4": raw.get("convergence_target_4", cls.convergence_target_4),
            "convergence_target_3": raw.get("convergence_target_3", cls.convergence_target_3),
            "convergence_target_2": raw.get("convergence_target_2", cls.convergence_target_2),
            "tranche_budget_usdc": raw.get("tranche_budget_usdc", cls.tranche_budget_usdc),
            "allow_dynamic_additions": raw.get("allow_dynamic_additions", cls.allow_dynamic_additions),
            "regime_gate_enabled": raw.get("regime_gate_enabled", cls.regime_gate_enabled),
            "policy_id": str(raw.get("policy_id", cls.policy_id) or cls.policy_id),
            "allocation_mode": str(raw.get("allocation_mode", cls.allocation_mode) or cls.allocation_mode),
            "role_probability_tolerance": raw.get("role_probability_tolerance", cls.role_probability_tolerance),
            "edge_profit_floor_usdc": raw.get("edge_profit_floor_usdc", cls.edge_profit_floor_usdc),
            "parent_order_cap_usdc": raw.get("parent_order_cap_usdc", cls.parent_order_cap_usdc),
        }
        return cls(**values)

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.bankroll_usdc <= 0 or not math.isfinite(self.bankroll_usdc):
            issues.append("bankroll_invalid")
        if self.basket_width not in {2, 3, 4}:
            issues.append("basket_width_must_be_one_of_2_3_4")
        if self.direction_deadband_c < 0 or not math.isfinite(self.direction_deadband_c):
            issues.append("direction_deadband_invalid")
        if self.minimum_guaranteed_profit_usdc < 0:
            issues.append("minimum_guaranteed_profit_negative")
        if self.fee_rate < 0 or self.fee_rate >= 1:
            issues.append("fee_rate_invalid")
        if self.minimum_order_shares <= 0:
            issues.append("minimum_order_shares_invalid")
        if not isinstance(self.live_submit_authorized, bool):
            issues.append("live_submit_authorized_invalid")
        if not isinstance(self.dynamic_convergence_enabled, bool):
            issues.append("dynamic_convergence_enabled_invalid")
        if self.dynamic_convergence_enabled:
            if tuple(self.convergence_widths) != (4, 3, 2):
                issues.append("convergence_widths_must_be_4_3_2")
            for name, value in (
                ("convergence_target_4", self.convergence_target_4),
                ("convergence_target_3", self.convergence_target_3),
                ("convergence_target_2", self.convergence_target_2),
            ):
                if not 0 < float(value) <= 1:
                    issues.append(f"{name}_invalid")
            if self.tranche_budget_usdc is not None and (
                self.tranche_budget_usdc <= 0 or not math.isfinite(self.tranche_budget_usdc)
            ):
                issues.append("tranche_budget_invalid")
        if not isinstance(self.allow_dynamic_additions, bool):
            issues.append("allow_dynamic_additions_invalid")
        if not isinstance(self.regime_gate_enabled, bool):
            issues.append("regime_gate_enabled_invalid")
        if not self.policy_id:
            issues.append("basket_policy_id_missing")
        if self.allocation_mode not in {"profit_weighted_settlement_floor", "equal_share"}:
            issues.append("basket_allocation_mode_invalid")
        if self.parent_order_cap_usdc <= 0 or not math.isfinite(self.parent_order_cap_usdc):
            issues.append("parent_order_cap_invalid")
        if self.role_probability_tolerance < 0 or not math.isfinite(float(self.role_probability_tolerance)):
            issues.append("basket_role_probability_tolerance_invalid")
        if self.edge_profit_floor_usdc < 0 or not math.isfinite(float(self.edge_profit_floor_usdc)):
            issues.append("basket_edge_profit_floor_invalid")
        return issues


def _canonical_source_row(source_id: str, value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        peak = _first_number(value, ("forecast_peak_c", "peak_c", "value_c", "forecast_peak_temp_c"))
        row = dict(value)
    else:
        peak = _finite(value)
        row = {}
    if peak is None:
        return None
    row["source_id"] = source_id
    row["forecast_peak_c"] = peak
    row.setdefault("fetched_at", row.get("as_of") or row.get("created_at") or row.get("available_at"))
    return row


def extract_model_taf_inputs(
    features: Mapping[str, Any] | None,
    *,
    source_observations: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Extract the existing weather canonical fields without refreshing sources.

    The adapter accepts fields emitted by ``weather_source_acquisition_manager``
    and the existing forecast snapshots.  It intentionally selects only the
    three requested model members plus the primary/declared TAF.
    """

    features = features if isinstance(features, Mapping) else {}
    out: dict[str, dict[str, Any]] = {}
    direct_keys = {
        "gfs_seamless": ("gfs_model_peak_c", "gfs_forecast_peak_c", "gfs_peak_c"),
        "icon_seamless": ("icon_model_peak_c", "icon_forecast_peak_c", "icon_peak_c"),
        "ecmwf_ifs025": ("ecmwf_model_peak_c", "ecmwf_forecast_peak_c", "ecmwf_peak_c"),
    }
    member_keys = {
        "gfs_seamless": ("gfs_seamless", "gfs"),
        "icon_seamless": ("icon_seamless", "icon"),
        "ecmwf_ifs025": ("ecmwf_ifs025", "ecmwf"),
    }
    raw_members = features.get("model_members_decimal_raw")
    if not isinstance(raw_members, Mapping):
        raw_members = {}
    for source_id, keys in direct_keys.items():
        peak = _first_number(features, keys)
        if peak is None:
            for key in member_keys[source_id]:
                peak = _member_mean(raw_members.get(key))
                if peak is not None:
                    break
        if peak is not None:
            out[source_id] = {
                "source_id": source_id,
                "forecast_peak_c": peak,
                "fetched_at": features.get(f"{source_id}_fetched_at") or features.get("forecast_fetched_at"),
                "source_event_id": features.get(f"{source_id}_source_event_id") or "",
            }

    taf_peak = _first_number(
        features,
        ("taf_forecast_peak_c", "amsc_taf_forecast_peak_c", "taf_peak_c", "taf_tx_c"),
    )
    taf_source = str(features.get("taf_forecast_peak_source_id") or features.get("taf_forecast_peak_source") or "").strip()
    if taf_source not in TAF_IDS:
        taf_source = "zuuu_taf_forecast_amsc"
    if taf_peak is not None:
        out["taf"] = {
            "source_id": taf_source,
            "forecast_peak_c": taf_peak,
            "fetched_at": features.get("taf_forecast_peak_fetched_at") or features.get("forecast_fetched_at"),
            "source_event_id": features.get("taf_forecast_peak_source_event_id") or "",
        }

    for raw in source_observations or ():
        if not isinstance(raw, Mapping):
            continue
        source_id = str(raw.get("source_id") or "").strip()
        if source_id in MODEL_IDS:
            row = _canonical_source_row(source_id, raw)
            if row is not None:
                out[source_id] = row
        elif source_id in TAF_IDS:
            row = _canonical_source_row(source_id, raw)
            if row is not None:
                out["taf"] = row
    return out


def classify_regime_direction(regime: Mapping[str, Any] | None) -> dict[str, Any]:
    """Map existing physical/climo regime labels or vectors to a conservative gate.

    A regime can support or contradict the model/TAF state, but it never
    creates a direction by itself.  Conflicting UP/DOWN labels are explicit
    and fail closed.
    """

    regime = regime if isinstance(regime, Mapping) else {}
    labels: list[str] = []
    nested_states: list[str] = []
    explicit_direction = _iso(regime.get("target_day_regime") or regime.get("regime_direction") or regime.get("direction_state")).upper()
    for key in (
        "forecast_weather_regime",
        "basin_regime",
        "primary_regime_label",
        "subtropical_high_regime",
        "monsoon_humidity_regime",
        "basin_ventilation_regime",
        "basin_heat_mechanism_state",
        "basin_heat_mechanism_status",
        "post_cool_rebound_intraday_likelihood_state",
        "post_cool_rebound_regime_prior_state",
        # These are the direct physical sub-regimes emitted by
        # official_nwp_physical_regime_runtime.  They are evidence for the
        # agreement gate, not an independent direction vote.
        "cloud_regime",
        "precipitation_regime",
        "convection_regime",
    ):
        value = regime.get(key)
        if value is not None and str(value).strip():
            labels.append(str(value).strip().lower())
    for nested_key in (
        "physical_regime",
        "official_nwp_physical_regime",
        "climo_regime",
        "basin_heat_mechanism_model",
        "forecast_background_context",
    ):
        nested = regime.get(nested_key)
        if isinstance(nested, Mapping):
            nested_result = classify_regime_direction(nested)
            labels.extend(nested_result.get("labels") or [])
            nested_state = str(nested_result.get("state") or "UNKNOWN")
            if nested_state in {"UP", "DOWN", "CONFLICT"}:
                nested_states.append(nested_state)
    up = sorted({label for label in labels if label in REGIME_UP_LABELS})
    down = sorted({label for label in labels if label in REGIME_DOWN_LABELS})
    direct_state = "CONFLICT" if up and down else "UP" if up else "DOWN" if down else "UNKNOWN"
    if direct_state != "CONFLICT" and explicit_direction in {"UP", "DOWN"}:
        direct_state = explicit_direction
    elif direct_state != "CONFLICT" and explicit_direction in {"NEUTRAL", "UNKNOWN"}:
        direct_state = explicit_direction
    nested_up = any(state == "UP" for state in nested_states)
    nested_down = any(state == "DOWN" for state in nested_states)
    if direct_state == "UNKNOWN" and nested_up and nested_down:
        direct_state = "CONFLICT"
    elif direct_state == "UNKNOWN" and nested_up:
        direct_state = "UP"
    elif direct_state == "UNKNOWN" and nested_down:
        direct_state = "DOWN"
    elif direct_state in {"UP", "DOWN"} and ((direct_state == "UP" and nested_down) or (direct_state == "DOWN" and nested_up)):
        direct_state = "CONFLICT"
    vector = regime.get("regime_probability_vector") or regime.get("forecast_regime_probabilities")
    vector_up = vector_down = 0.0
    if isinstance(vector, Mapping):
        for label, raw_probability in vector.items():
            probability = _finite(raw_probability)
            if probability is None or probability < 0.0:
                continue
            label_text = str(label).strip().lower()
            if label_text in REGIME_UP_LABELS:
                vector_up += probability
            elif label_text in REGIME_DOWN_LABELS:
                vector_down += probability
    vector_state = "UNKNOWN"
    if vector_up >= 0.5 and vector_up > vector_down + 0.1:
        vector_state = "UP"
    elif vector_down >= 0.5 and vector_down > vector_up + 0.1:
        vector_state = "DOWN"
    if direct_state in {"UP", "DOWN", "CONFLICT"} and vector_state in {"UP", "DOWN"} and direct_state != vector_state:
        state = "CONFLICT"
    elif direct_state in {"UP", "DOWN", "CONFLICT"}:
        state = direct_state
    else:
        state = vector_state
    confidence = _finite(regime.get("basin_regime_confidence"))
    return {
        "state": state,
        "labels": sorted(set(labels)),
        "supporting_up_labels": up,
        "suppressing_down_labels": down,
        "confidence": max(0.0, min(1.0, confidence)) if confidence is not None else None,
        "vector_up_mass": vector_up if isinstance(vector, Mapping) else None,
        "vector_down_mass": vector_down if isinstance(vector, Mapping) else None,
        "role": "direction_agreement_gate_only",
    }


def build_direction_bias(
    *,
    market_anchor_c: float,
    probability_projection: Mapping[str, Any],
    config: BasketStrategyConfig,
    target_event_date: str = "",
    regime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Gate a committed P0/P1 probability center against tomorrow's regime.

    The basket never rebuilds weather probabilities from model point values.
    ``probability_projection`` is the existing P1 (preferred) or P0 market
    bucket SSOT.  Regime only confirms or blocks the direction implied by its
    highest-probability exact bucket.
    """

    issues = config.validate()
    anchor = _finite(market_anchor_c)
    if anchor is None:
        issues.append("market_anchor_missing_or_invalid")
    projection = probability_projection if isinstance(probability_projection, Mapping) else {}
    probabilities = projection.get("probabilities")
    rows = projection.get("rows")
    if projection.get("status") != "available" or not isinstance(probabilities, Mapping) or not probabilities:
        issues.extend(projection.get("reason_codes") or ["probability_projection_unavailable"])
    probability_rows = [
        row for row in (rows if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)) else [])
        if isinstance(row, Mapping)
    ]
    if not probability_rows:
        issues.append("probability_center_requires_exact_bucket")
    center_row = max(probability_rows, key=lambda row: (float(row.get("probability") or 0.0), -(int(row["bucket_c"]) if isinstance(row.get("bucket_c"), int) else 10**9))) if probability_rows else None
    if center_row is not None and not isinstance(center_row.get("bucket_c"), int):
        issues.append("probability_center_requires_exact_bucket")
    center_bucket_c = int(center_row["bucket_c"]) if center_row and isinstance(center_row.get("bucket_c"), int) else None
    center_bucket = str(center_row.get("canonical_bucket") or center_row.get("bucket") or "") if center_row else ""
    center_probability = float(center_row.get("probability") or 0.0) if center_row else None
    if config.regime_gate_enabled:
        regime_direction = classify_regime_direction(regime)
        regime_state = str(regime_direction.get("state") or "UNKNOWN")
    else:
        regime_direction = {
            "state": "DISABLED",
            "labels": [],
            "supporting_up_labels": [],
            "suppressing_down_labels": [],
            "confidence": None,
            "vector_up_mass": None,
            "vector_down_mass": None,
            "role": "direction_agreement_gate_disabled",
        }
        regime_state = "DISABLED"
    center_direction = "NEUTRAL"
    if anchor is not None and center_bucket_c is not None:
        center_delta = center_bucket_c - anchor
        if center_delta > config.direction_deadband_c + EPS:
            center_direction = "UP"
        elif center_delta < -config.direction_deadband_c - EPS:
            center_direction = "DOWN"
    else:
        center_delta = None
    if config.regime_gate_enabled:
        if regime_state == "CONFLICT":
            issues.append("regime_conflict")
        elif regime_state not in {"UP", "DOWN"}:
            issues.append("regime_neutral_or_unknown")
        elif center_direction != regime_state:
            issues.append("probability_center_regime_conflict")
    elif center_direction not in {"UP", "DOWN"}:
        issues.append("probability_center_direction_neutral")
    effective_direction = regime_state if config.regime_gate_enabled else center_direction
    if issues:
        return {
            "status": "blocked",
            "direction": "BLOCKED",
            "direction_state": "BLOCKED",
            "direction_horizon": "tomorrow",
            "target_event_date": str(target_event_date or ""),
            "reason_codes": sorted(set(issues)),
            "market_anchor_c": anchor,
            "posterior_stage": str(projection.get("stage") or ""),
            "center_bucket": center_bucket,
            "center_bucket_c": center_bucket_c,
            "center_probability": center_probability,
            "center_direction": center_direction,
            "regime_gate": (
                "conflict_blocked"
                if {"probability_center_regime_conflict", "regime_conflict"}.intersection(issues)
                else "blocked"
            ),
            "probabilities": dict(probabilities) if isinstance(probabilities, Mapping) else {},
            "probability_projection": dict(projection),
            "regime": regime_direction,
        }
    return {
        "status": "available",
        "direction": effective_direction,
        "direction_state": effective_direction,
        "direction_horizon": "tomorrow",
        "target_event_date": str(target_event_date or ""),
        "market_anchor_c": float(anchor),
        "posterior_stage": str(projection.get("stage") or ""),
        "center_bucket": center_bucket,
        "center_bucket_c": center_bucket_c,
        "center_probability": center_probability,
        "center_direction": center_direction,
        "center_delta_vs_market_c": center_delta,
        "probabilities": dict(probabilities),
        "posterior_checkpoint_id": str(projection.get("posterior_checkpoint_id") or ""),
        "posterior_run_id": str(projection.get("posterior_run_id") or ""),
        "realization_state_id": str(projection.get("realization_state_id") or ""),
        "regime_gate": "agree" if config.regime_gate_enabled else "disabled",
        "regime": regime_direction,
        "reason_codes": [],
        "direction_rule": (
            "argmax_p1_or_p0_market_bucket_vs_market_anchor_with_regime_gate"
            if config.regime_gate_enabled
            else "argmax_p1_or_p0_market_bucket_vs_market_anchor_regime_gate_disabled"
        ),
    }


def _quote_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        bucket_c = _finite(raw.get("bucket_c"))
        if bucket_c is None:
            continue
        yes_ask = _finite(raw.get("yes_ask"))
        ask_size = _positive(raw.get("yes_ask_size"))
        if yes_ask is None or ask_size is None or not 0 < yes_ask < 1:
            continue
        label = str(raw.get("outcome") or raw.get("bucket") or raw.get("label") or f"{bucket_c:g}°C").strip()
        normalized.append(
            {
                **dict(raw),
                "bucket": label,
                "bucket_c": int(round(bucket_c)),
                "yes_ask": yes_ask,
                "yes_ask_size": ask_size,
                "yes_token": str(raw.get("yes_token") or "").strip(),
                "no_token": str(raw.get("no_token") or "").strip(),
            }
        )
    return sorted(normalized, key=lambda row: row["bucket_c"])


def _probability_by_bucket_c(probabilities: Mapping[str, Any] | None) -> dict[int, float]:
    result: dict[int, float] = {}
    for label, raw in (probabilities or {}).items():
        try:
            parsed = parse_bucket_interval(str(label))
            lower = parsed.get("lower")
            if lower is None:
                lower = parsed.get("upper")
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if lower is not None and math.isfinite(value) and value >= 0:
            result[int(round(float(lower)))] = value
    return result


def _select_basket_quotes(
    quotes: Sequence[Mapping[str, Any]],
    *,
    center_bucket_c: int,
    config: BasketStrategyConfig,
    reported_floor_c: int | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[str] = []
    exact = [row for row in quotes if str(row.get("kind") or "single") == "single"]
    edge_centres = [
        row for row in quotes
        if str(row.get("kind") or "single") in {"lower_tail", "upper_tail"}
        and isinstance(row.get("bucket_c"), int)
        and int(row["bucket_c"]) == center_bucket_c
    ]
    if reported_floor_c is not None and center_bucket_c < reported_floor_c:
        return [], ["probability_center_reported_excluded"]
    if reported_floor_c is not None:
        exact = [row for row in exact if int(row["bucket_c"]) >= reported_floor_c]
        edge_centres = [row for row in edge_centres if int(row["bucket_c"]) >= reported_floor_c]
    # An edge contract can be the posterior centre.  In that case it is the
    # first/last leg of the contiguous window; for an interior exact centre,
    # edge contracts remain excluded so the basket stays exact-temperature.
    if edge_centres:
        edge = edge_centres[0]
        if str(edge.get("kind") or "") == "lower_tail":
            exact = [edge, *exact]
        else:
            exact = [*exact, edge]
    exact = sorted(exact, key=lambda row: row["bucket_c"])
    if len(exact) < config.basket_width:
        return [], ["insufficient_exact_buckets_after_reported_floor"]
    contiguous_groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for row in exact:
        if current and row["bucket_c"] != current[-1]["bucket_c"] + 1:
            contiguous_groups.append(current)
            current = []
        current.append(row)
    if current:
        contiguous_groups.append(current)
    groups = [group for group in contiguous_groups if len(group) >= config.basket_width]
    if not groups:
        return [], ["exact_bucket_ladder_not_contiguous_for_basket"]
    group = next((candidate for candidate in groups if any(row["bucket_c"] == center_bucket_c for row in candidate)), None)
    if group is None:
        return [], ["probability_center_missing_from_market_ladder"]
    center = next(index for index, row in enumerate(group) if row["bucket_c"] == center_bucket_c)
    half = config.basket_width // 2
    start = max(0, min(len(group) - config.basket_width, center - half))
    selected = group[start : start + config.basket_width]
    if len(selected) != config.basket_width:
        issues.append("basket_selection_width_mismatch")
    if selected and selected[-1]["bucket_c"] - selected[0]["bucket_c"] != config.basket_width - 1:
        issues.append("basket_selection_not_contiguous")
    return selected, issues


def _dynamic_width_target(config: BasketStrategyConfig, width: int) -> float:
    return {
        4: float(config.convergence_target_4),
        3: float(config.convergence_target_3),
        2: float(config.convergence_target_2),
    }.get(width, 1.0)


def _select_dynamic_basket_quotes(
    quotes: Sequence[Mapping[str, Any]],
    *,
    center_bucket_c: int,
    width: int,
    probabilities: Mapping[str, Any] | None,
    config: BasketStrategyConfig,
    reported_floor_c: int | None,
) -> tuple[list[dict[str, Any]], list[str], float, float]:
    """Select the best contiguous window for the current convergence stage.

    The centre may be an edge bucket.  Windows are scored by materialized
    posterior mass, with all-in ask cost as the deterministic tie-breaker.
    No point forecast or market mean is introduced here.
    """
    issues: list[str] = []
    exact = [row for row in quotes if str(row.get("kind") or "single") == "single"]
    edge_centres = [
        row for row in quotes
        if str(row.get("kind") or "single") in {"lower_tail", "upper_tail"}
        and isinstance(row.get("bucket_c"), int)
        and int(row["bucket_c"]) == center_bucket_c
    ]
    if reported_floor_c is not None:
        exact = [row for row in exact if int(row["bucket_c"]) >= reported_floor_c]
        edge_centres = [row for row in edge_centres if int(row["bucket_c"]) >= reported_floor_c]
    if edge_centres:
        edge = edge_centres[0]
        if str(edge.get("kind") or "") == "lower_tail":
            exact = [edge, *exact]
        else:
            exact = [*exact, edge]
    exact = sorted(exact, key=lambda row: row["bucket_c"])
    if len(exact) < width:
        return [], ["insufficient_exact_buckets_after_reported_floor"], 0.0, _dynamic_width_target(config, width)
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for row in exact:
        if current and row["bucket_c"] != current[-1]["bucket_c"] + 1:
            groups.append(current)
            current = []
        current.append(row)
    if current:
        groups.append(current)
    candidates: list[tuple[float, float, list[dict[str, Any]]]] = []
    probability_by_c = _probability_by_bucket_c(probabilities)
    for group in groups:
        for start in range(0, len(group) - width + 1):
            window = group[start : start + width]
            if not any(int(row["bucket_c"]) == center_bucket_c for row in window):
                continue
            if validate_window_roles(
                window,
                probability_by_c,
                tolerance=float(config.role_probability_tolerance),
            ):
                continue
            mass = sum(probability_by_c.get(int(row["bucket_c"]), 0.0) for row in window)
            cost = sum(float(row["yes_ask"]) * (1.0 + config.fee_rate) for row in window)
            candidates.append((mass, cost, window))
    if not candidates:
        return [], ["probability_center_missing_from_market_ladder"], 0.0, _dynamic_width_target(config, width)
    # Highest posterior coverage wins; the cheaper basket wins ties.  The
    # final bucket tuple makes repeated evaluation deterministic.
    mass, cost, selected = max(
        candidates,
        key=lambda item: (item[0], -item[1], tuple(-int(row["bucket_c"]) for row in item[2])),
    )
    target = _dynamic_width_target(config, width)
    if mass + EPS < target:
        return [dict(row) for row in selected], ["convergence_coverage_below_stage_target"], mass, target
    return [dict(row) for row in selected], [], mass, target


def _build_basket_component(
    *,
    event: Mapping[str, Any],
    quotes: Sequence[Mapping[str, Any]],
    direction_bias: Mapping[str, Any],
    config: BasketStrategyConfig,
    reported_floor_c: int | None,
    probability_projection: Mapping[str, Any] | None = None,
    completed_widths: Sequence[int] | None = None,
) -> dict[str, Any]:
    if direction_bias.get("status") != "available":
        return {"status": "blocked", "reason_codes": ["direction_bias_unavailable"]}
    direction = str(direction_bias.get("direction") or "")
    if direction not in {"UP", "DOWN"}:
        return {"status": "blocked", "reason_codes": ["direction_bias_neutral_or_blocked"]}
    center_bucket_c = direction_bias.get("center_bucket_c")
    if not isinstance(center_bucket_c, int):
        return {"status": "blocked", "reason_codes": ["probability_center_requires_exact_bucket"]}
    completed = {int(item) for item in (completed_widths or ()) if str(item).isdigit()}
    tranche_width = config.basket_width
    convergence_coverage = None
    convergence_target = None
    if config.dynamic_convergence_enabled:
        eligible = [width for width in config.convergence_widths if width not in completed]
        if not eligible and config.allow_dynamic_additions:
            # Once 4/3/2 have all been established, only the narrowest
            # converged basket may receive an additive top-up.  Existing legs
            # remain untouched; this is not a sell/rebalance/merge action.
            eligible = [min(config.convergence_widths)]
        if not eligible:
            return {"status": "blocked", "reason_codes": ["convergence_stages_complete"]}
        # Start with the widest valid tranche.  If it no longer meets its
        # target, enter at the narrowest stage that does; after a tranche is
        # submitted, the next poll advances strictly 4 -> 3 -> 2.
        tranche_width = eligible[0]
        candidate_results: list[tuple[int, list[dict[str, Any]], list[str], float, float]] = []
        for width in eligible:
            selected_candidate, candidate_issues, mass, target = _select_dynamic_basket_quotes(
                quotes,
                center_bucket_c=center_bucket_c,
                width=width,
                probabilities=(probability_projection or {}).get("probabilities"),
                config=config,
                reported_floor_c=reported_floor_c,
            )
            candidate_results.append((width, selected_candidate, candidate_issues, mass, target))
            if not candidate_issues:
                tranche_width = width
                break
        else:
            width, selected_candidate, candidate_issues, mass, target = candidate_results[0]
            return {
                "status": "blocked",
                "reason_codes": sorted(set(candidate_issues or ["convergence_stage_not_eligible"])),
                "tranche_width": width,
                "convergence_coverage": mass,
                "convergence_target": target,
            }
        selected, issues, convergence_coverage, convergence_target = next(
            result[1:] for result in candidate_results if result[0] == tranche_width
        )
    else:
        selected, issues = _select_basket_quotes(
            quotes,
            center_bucket_c=center_bucket_c,
            config=config,
            reported_floor_c=reported_floor_c,
        )
    if issues:
        return {"status": "blocked", "reason_codes": issues}
    role_probability_issues = validate_window_roles(
        selected,
        _probability_by_bucket_c((probability_projection or {}).get("probabilities")),
        tolerance=float(config.role_probability_tolerance),
    )
    if role_probability_issues:
        return {
            "status": "blocked",
            "reason_codes": role_probability_issues,
            "selected_buckets": [row.get("bucket") for row in selected],
        }
    missing_tokens = [row["bucket"] for row in selected if not row["yes_token"]]
    if missing_tokens:
        return {"status": "blocked", "reason_codes": ["yes_token_missing"], "missing_buckets": missing_tokens}
    all_in_prices = [float(row["yes_ask"]) * (1.0 + config.fee_rate) for row in selected]
    per_share_cost = sum(all_in_prices)
    if per_share_cost >= 1.0 - EPS:
        return {
            "status": "blocked",
            "reason_codes": ["basket_worst_case_floor_not_profitable"],
            "selected_buckets": [row["bucket"] for row in selected],
            "per_share_all_in_cost": per_share_cost,
        }
    capital_limit = config.bankroll_usdc
    if config.tranche_budget_usdc is not None:
        capital_limit = min(capital_limit, config.tranche_budget_usdc)
    allocation = allocate_profit_weighted(
        selected,
        budget_usdc=capital_limit,
        fee_rate=config.fee_rate,
        minimum_order_shares=config.minimum_order_shares,
        edge_profit_floor_usdc=config.edge_profit_floor_usdc,
        max_leg_notional_usdc=config.parent_order_cap_usdc,
    )
    if config.allocation_mode == "equal_share":
        quantity_by_capital = capital_limit / per_share_cost
        quantity_by_depth = min(float(row["yes_ask_size"]) for row in selected)
        quantity = min(quantity_by_capital, quantity_by_depth)
        quantities = [quantity for _row in selected]
        total_cost = quantity * per_share_cost
        settlement_profits = [quantity - total_cost for _row in selected]
        allocation = {
            "status": "ready",
            "mode": "equal_share",
            "roles": [],
            "quantities": quantities,
            "total_cost_usdc": total_cost,
            "settlement_profit_by_bucket": settlement_profits,
            "depth_scale": quantity / quantity_by_capital if quantity_by_capital else 0.0,
            "worst_case_profit_usdc": min(settlement_profits),
            "best_case_profit_usdc": max(settlement_profits),
        }
    if allocation.get("status") != "ready":
        return {
            "status": "blocked",
            "reason_codes": list(allocation.get("reason_codes") or ["basket_allocation_blocked"]),
            "selected_buckets": [row["bucket"] for row in selected],
            "allocation": allocation,
        }
    quantities = [float(value) for value in allocation.get("quantities") or []]
    if len(quantities) != len(selected):
        return {"status": "blocked", "reason_codes": ["basket_allocation_quantity_mismatch"]}
    quantity = min(quantities)
    guaranteed_profit = float(allocation.get("worst_case_profit_usdc") or 0.0)
    if quantity < config.minimum_order_shares - EPS:
        return {
            "status": "blocked",
            "reason_codes": ["basket_depth_below_minimum_order"],
            "selected_buckets": [row["bucket"] for row in selected],
            "quantity": quantity,
        }
    if guaranteed_profit + EPS < config.minimum_guaranteed_profit_usdc:
        return {
            "status": "blocked",
            "reason_codes": ["basket_guaranteed_profit_below_configured_floor"],
            "selected_buckets": [row["bucket"] for row in selected],
            "quantity": quantity,
            "guaranteed_profit_usdc": guaranteed_profit,
        }
    orders = [
        {
            "intent_id": _stable_id("basket_intent_", [event.get("event_id"), row["bucket"], quantity]),
            "side": "YES",
            "token": row["yes_token"],
            "bucket": row["bucket"],
            "bucket_c": row["bucket_c"],
            "quantity": quantities[index],
            "limit_price": row["yes_ask"],
            "estimated_notional_usdc": quantities[index] * all_in_prices[index],
            "role": (allocation.get("roles") or [{} for _ in selected])[index].get("role") if isinstance((allocation.get("roles") or [{} for _ in selected])[index], Mapping) else "",
            "target_settlement_profit_usdc": float((allocation.get("settlement_profit_by_bucket") or [0.0 for _ in selected])[index]),
            "order_type": "FOK",
            "fill_policy": "all_legs_required",
        }
        for index, row in enumerate(selected)
    ]
    return {
        "status": "ready",
        "mode": "pure_yes_basket",
        "execution": {
            "single_shot": not config.dynamic_convergence_enabled,
            "staged_build": config.dynamic_convergence_enabled,
            "selective_pairing": False,
            "merge_enabled": False,
            "all_legs_required": True,
            "dynamic_additions": bool(config.dynamic_convergence_enabled and config.allow_dynamic_additions),
            "live_submit_authorized": config.live_submit_authorized,
        },
        "direction": direction,
        "posterior_stage": direction_bias.get("posterior_stage"),
        "center_bucket": direction_bias.get("center_bucket"),
        "center_bucket_c": center_bucket_c,
        "center_probability": direction_bias.get("center_probability"),
        "reported_floor_c": reported_floor_c,
        "tranche_width": tranche_width,
        # The live basket is a dynamic convergence stage, not a fixed
        # four-leg order.  Keep both the selected width and the actual leg
        # count explicit for execution logs, prechecks and the read-only UI.
        "current_stage_width": tranche_width,
        "leg_count": len(selected),
        "convergence_sequence": list(config.convergence_widths) if config.dynamic_convergence_enabled else [tranche_width],
        "convergence_coverage": convergence_coverage,
        "convergence_target": convergence_target,
        "completed_tranche_widths": sorted(completed),
        "tranche_signature": _stable_id(
            "basket_tranche_",
            [
                event.get("event_id"),
                tranche_width,
                [row["bucket"] for row in selected],
                [round(float(row["yes_ask"]), 8) for row in selected],
                direction_bias.get("posterior_checkpoint_id") or direction_bias.get("posterior_run_id"),
            ],
        ),
        "selected_buckets": [row["bucket"] for row in selected],
        "quantity_per_bucket": quantity,
        "quantity_by_bucket": {row["bucket"]: quantities[index] for index, row in enumerate(selected)},
        "role_assignments": allocation.get("roles") or [],
        "allocation_mode": allocation.get("mode"),
        "primary_secondary_profit_ratio": "2:1" if tranche_width == 4 else "1:1" if tranche_width == 3 else "2:1",
        "settlement_profit_by_bucket": {
            row["bucket"]: float((allocation.get("settlement_profit_by_bucket") or [0.0 for _ in selected])[index])
            for index, row in enumerate(selected)
        },
        "per_share_all_in_cost": per_share_cost,
        "total_cost_usdc": float(allocation.get("total_cost_usdc") or 0.0),
        "worst_case_settlement_payout_usdc": quantity,
        "guaranteed_profit_usdc": guaranteed_profit,
        "macro_topup": {
            "delta_rule": "max(target - filled - pending, 0)",
            "additive_only": True,
            "recompute_on_new_posterior_or_quote": True,
        },
        "micro_split": {
            "split_by_ask_level": True,
            "target_recomputed": False,
            "child_order_cap_usdc": 2.0,
        },
        "orders": orders,
    }


def _official_report_floor(
    *,
    event: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return an AWC-proven temperature floor for filtering impossible YES.

    The report is never an order component and its absence does not become a
    synthetic probability gate.  The pure basket simply retains every bucket
    when no formal, current report floor is available.
    """

    report = report if isinstance(report, Mapping) else {}
    raw_text = str(report.get("raw_text") or report.get("raw") or "").strip()
    if not raw_text:
        return {"status": "unavailable", "reason_codes": ["official_report_missing"]}
    if bool(report.get("correction_suspected")):
        return {"status": "unavailable", "reason_codes": ["official_report_correction_suspected"]}
    source_lane = str(report.get("source_lane") or "").strip().lower()
    if source_lane != "awc":
        return {"status": "unavailable", "reason_codes": ["official_report_source_lane_not_awc"]}
    observation_id = str(report.get("observation_id") or "").strip()
    if not observation_id:
        return {"status": "unavailable", "reason_codes": ["official_report_observation_id_missing"]}
    try:
        parsed = parse_metar_temperature(raw_text, expected_station=str(report.get("expected_station") or ""))
        unit = str(event.get("market_unit") or "C").strip().upper()
        floor = conservative_market_temperature(parsed.reported_temperature_c, unit)
    except (TypeError, ValueError) as exc:
        return {"status": "unavailable", "reason_codes": [f"official_report_invalid:{exc}"]}
    return {
        "status": "available",
        "source_lane": source_lane,
        "observation_id": observation_id,
        "report_type": parsed.report_type,
        "station_id": parsed.station_id,
        "reported_temperature_c": parsed.reported_temperature_c,
        "reported_floor_market_unit": floor,
    }


def build_basket_yes_plan(request: Mapping[str, Any]) -> dict[str, Any]:
    """Build a deterministic plan without submitting anything."""

    request = request if isinstance(request, Mapping) else {}
    config = BasketStrategyConfig.from_mapping(request.get("config"))
    config_issues = config.validate()
    event = request.get("event") if isinstance(request.get("event"), Mapping) else request
    buckets = request.get("buckets")
    if not isinstance(buckets, Sequence) or isinstance(buckets, (str, bytes, bytearray)):
        buckets = []
    market_rows = [dict(row) for row in buckets if isinstance(row, Mapping)]
    quotes = _quote_rows(market_rows)
    probability_projection = extract_probability_projection(request)
    direction = build_direction_bias(
        market_anchor_c=request.get("market_anchor_c", event.get("market_anchor_c")),
        probability_projection=probability_projection,
        config=config,
        target_event_date=str(event.get("target_event_date") or event.get("event_date") or ""),
        regime=request.get("regime") if isinstance(request.get("regime"), Mapping) else None,
    )
    official_report_floor = _official_report_floor(
        event=event,
        report=request.get("report") if isinstance(request.get("report"), Mapping) else None,
    )
    reported_floor_c = official_report_floor.get("reported_floor_market_unit")
    basket = _build_basket_component(
        event=event,
        quotes=quotes,
        direction_bias=direction,
        config=config,
        reported_floor_c=(
            int(reported_floor_c)
            if isinstance(reported_floor_c, (int, float))
            else None
        ),
        probability_projection=probability_projection,
        completed_widths=request.get("completed_tranche_widths"),
    )
    if config_issues:
        basket = {"status": "blocked", "reason_codes": config_issues}
    overall_status = "ready" if basket.get("status") == "ready" else "blocked"
    if overall_status == "ready":
        plan_state = "BASKET_READY"
    elif direction.get("status") == "available":
        plan_state = "REGIME_CHECKED"
    elif probability_projection.get("status") == "available":
        plan_state = "PROBABILITY_READY"
    else:
        plan_state = "DRAFT"
    plan = {
        "schema": BASKET_STRATEGY_SCHEMA_VERSION,
        "plan_id": _stable_id(
            "basket_plan_",
            [event.get("event_id"), request.get("as_of"), direction, basket, official_report_floor],
        ),
        "status": overall_status,
        "execution_mode": "live" if config.live_submit_authorized else "plan_only",
        "live_submit_authorized": config.live_submit_authorized,
        "plan_state": plan_state,
        "state_machine": {
            "current": plan_state,
            "next": ["FROZEN"] if plan_state == "BASKET_READY" else [],
            "live_transition_allowed": False,
        },
        "event": dict(event),
        "strategy_contract": {
            "pure_yes_basket": True,
            "basket_core": "p0_or_p1_market_bucket_probability_center",
            "direction_state_values": ["UP", "DOWN", "NEUTRAL"],
            "direction_horizon": "tomorrow",
            "regime_role": "agreement_gate_only",
            "regime_gate_enabled": config.regime_gate_enabled,
            "probability_ssot": "P1_preferred_else_P0_market_bucket_projection",
            "center_rule": "argmax_p_B_without_mean_or_direction_shift",
            "p1_updates_before_entry": True,
            "post_entry_recenter": False,
            "single_shot_basket": not config.dynamic_convergence_enabled,
            "staged_build": config.dynamic_convergence_enabled,
            "convergence_sequence": list(config.convergence_widths) if config.dynamic_convergence_enabled else [],
            "dynamic_additions": bool(config.dynamic_convergence_enabled and config.allow_dynamic_additions),
            "convergence_rule": (
                "widest_eligible_stage_then_advance_4_to_3_to_2_once_each"
                if config.dynamic_convergence_enabled
                else "fixed_width"
            ),
            "selective_pairing": False,
            "merge_enabled": False,
            "model_taf_role": "upstream_regime_and_P0_weak_likelihood_only",
            "official_report_role": "filter_mathematically_impossible_yes_buckets_only",
            "official_report_source": "AWC_METAR_SPECI_only",
            "selection_policy_id": config.policy_id,
            "selection_role_rule": {
                "four": ["non_core_edge_left", "primary_core", "secondary_core", "non_core_edge_right"],
                "three": ["non_core_edge_left", "core", "non_core_edge_right"],
                "two": ["primary_core", "secondary_core"],
            },
            "primary_secondary_profit_ratio": "2:1",
            "roles_fixed_by_position": True,
            "move_window_instead_of_role_swap": True,
            "edge_bucket_can_be_center": True,
            "macro_topup": "recompute target then max(target-filled-pending,0); additive only",
            "micro_split": "child orders split by ask level; never recompute target",
            "risk_budget": {
                "production_total_usdc": 1000.0,
                "test_total_usdc": 100.0,
                "test_daily_loss_cap_usdc": 5.0,
                "unallocated_yes_pool_usdc": 30.0,
                "city_day_cap_usdc": 10.0,
                "single_event_cap_usdc": 5.0,
                "parent_order_cap_usdc": 2.0,
            },
            "fact_semantics": {
                "awc_hourly_metar_is_settlement_fact": True,
                "awc_speci_is_effective_fact": True,
            },
        },
        "config": asdict(config),
        "probability_projection": probability_projection,
        "direction_bias": direction,
        "basket": basket,
        "official_report_floor": official_report_floor,
        "guards": {
            "writes_database": False,
            "reads_order_gateway": False,
            "submits_orders": config.live_submit_authorized,
            "reads_probability_runtime": False,
            "reads_materialized_posterior_projection": True,
            "uses_taf_for_reported_floor": False,
            "uses_model_for_reported_floor": False,
        },
    }
    return plan


__all__ = [
    "BASKET_STRATEGY_SCHEMA_VERSION",
    "BasketStrategyConfig",
    "build_basket_yes_plan",
    "build_direction_bias",
    "classify_regime_direction",
    "extract_model_taf_inputs",
]
