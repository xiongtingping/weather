"""Pure bridge from materialized probability envelopes to a target plan.

This module is the single strategy compiler.  It is deliberately independent
of SQLite and order transport: callers provide already materialized P0/P1,
station-policy and official-WS evidence, and this function returns one
content-addressable ``production_no_live`` candidate.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import ContractError, ProbabilityEnvelope, content_sha256
from .center_width_calibration import ladder_rung_width_c
from datetime import datetime
from zoneinfo import ZoneInfo

from .basket_kernel.contracts import (
    TARGET_BASKET_SNAPSHOT_SCHEMA,
    ContractError as KernelContractError,
    TargetBasketPolicy,
    TargetBasketSnapshot,
)
from .basket_kernel.delta_v2 import (
    BASKET_TARGET_NET_ROI,
    basket_return_structure_v1,
    BUNDLE_RESCORE_SCHEMA,
    DEFAULT_P1_ENTRY_LOCAL_HOUR,
    EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC,
    EXECUTION_AUTHORIZATION_JOINT_VALUE,
    execution_authorization_rule_v1,
    CAPITAL_LAYER_RULE_VERSION,
    KELLY_OBJECTIVE_FORM,
    PRODUCTION_EXECUTION_AUTHORIZATION,
    rescore_maker_bundle_v2,
    scope_stake_cap_usdc,
    station_day_ceiling_usdc,
    refresh_capital_layer,
    completion_budget_usdc_v1,
    venue_minimum_shares_v1,
)
from .basket_kernel import delta_v2 as _capital_kernel
from .cash_phase import (
    BasketCashPhaseInput,
    BasketCashPhasePolicyArtifact,
    OrderSizingDecision,
    evaluate_cash_phase,
)
from .cadence import (
    CADENCE_GATE_SECONDS_KEY,
    CadenceDecision,
    CadenceSnapshot,
    CadenceThresholds,
    evaluate_cadence,
)
from .policy import (
    GlobalBasketPriorArtifact,
    PolicyArtifact,
    ResolvedStationBasketPolicy,
    StationBasketPolicyActivation,
    resolve_station_policy,
)
from .stages import select_current_stage
from .settlement_time import (
    assert_probability_timezone_matches_market,
    hours_to_settlement_if_known,
)
from .regime_artifact import RegimeArtifact, resolve_regime_artifact
from .regime_materialization import evaluate_regime_gate
from .metar_trajectory import assert_p1_execution_evidence
from .metar_path_state import admissible_p1_source_lanes
from .observed_high_floor import (
    dead_buckets_for_running_high,
    running_high_from_market_snapshot,
)
from .delta_v2_artifact import DeltaV2PolicyArtifact
from .delta_v2_runtime import (
    CHARTER_83_MIN_NOTIONAL,
    compile_delta_v2_runtime_plan,
    taf_tx_bucket_for,
)


def execution_minimum_lot_shares(
    *,
    min_order_size: Any,
    limit_price: Any,
) -> float:
    """The fewest shares the venue takes for one leg: its lot or its dollar.

    Charter 83 was written into the plan-level lift alone, and that lift
    judges affordability against the capital the consolidation tilt leaves
    (0.75 USDC on every lit P0 day), so it refused on the first live morning
    and on every plan since -- while the sizing below raised each leg to the
    market's lot regardless, which is how 0.862-share targets became the
    five-share orders the exchange then refused under a dollar (ZGSZ 32°C,
    $0.95).  The venue's rule is one rule with two floors, so the sizing
    reads both through the kernel's one function; ``off`` is the lot alone.
    """

    return venue_minimum_shares_v1(
        min_lot_shares=float(min_order_size or 0.0),
        ask=float(limit_price or 0.0),
        minimum_notional_usdc=(
            EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC
            if CHARTER_83_MIN_NOTIONAL == "on" else 0.0
        ),
    )


DELTA_V2_OPTIMIZER_SCHEMA = "bayes_basket_target_delta_v2_optimizer_v2"
DELTA_V2_OPTIMIZER_ID = "DeltaBasketScoreV2Optimizer"


def _capital_layer_policy_bounds(
    book_equity_usdc: float | None,
) -> dict[str, float]:
    """Charter 141: the policy's bankroll-shaped bounds, off the real book.

    ``total_budget_usdc`` and the three loss bounds (protected floor, outside
    loss, minimum expected pnl) are one number wearing four names.  They read
    the dashboard's per-station dial (``station_cap_v1.json``) until
    2026-09-17 -- a dial that no longer bounded spend (the per-scope stake
    cap replaced it on 2026-08-26) and that the station policy's own
    ``total_budget_usdc: 20`` then overwrote.  They are now the station-day's
    ceiling on the real book: nothing a station-day spends can exceed it, so
    a loss floor at minus the ceiling can never refuse what the ceiling
    admits.  Applied after the station policy, so no artifact can reinstate
    a stale number.  With no equity on the snapshot (a fixture) the policy
    keeps what it had.
    """

    if book_equity_usdc in (None, ""):
        return {}
    try:
        equity = float(book_equity_usdc)
    except (TypeError, ValueError):
        return {}
    if not math.isfinite(equity) or equity <= 0.0:
        return {}
    ceiling = max(1.0, equity * _capital_kernel.STATION_DAY_HARD_FRACTION)
    return {
        "total_budget_usdc": ceiling,
        "minimum_expected_pnl_after_usdc": -ceiling,
        "protected_floor_usdc": -ceiling,
        "max_outside_loss_usdc": ceiling,
    }


_DEFAULT_POLICY = {
    # 15 by operator ruling 2026-08-31, revised within the hour from 20:
    # at 20 the 114.29 budget spreads across barely 5.7 full stations, and
    # the operator judged that concentration a risk ("预算最多摊 5.7 个满额站：
    # 有风险：还是调整为10-15u吧"), taking the top of the new 10-15 range.
    # At 15 the same budget covers 7.6 full stations.  The three
    # bankroll-shaped bounds below scale with it: they are "the whole
    # bankroll" expressed as a number, not independent choices.  This is
    # also only the default -- the operator can move it live from the
    # control panel, which writes station_cap_v1.json, read fresh each cycle.
    "total_budget_usdc": 15.0,
    "base_coverage": 0.55,
    "entropy_coverage_gain": 0.20,
    "maximum_coverage": 0.92,
    "direction_skew_deadband": 0.05,
    "direction_entropy_max": 0.95,
    "direction_min_side_mass": 0.05,
    "center_lead_min_probability": 0.01,
    "edge_max_ask": 0.18,
    "edge_min_probability": 0.005,
    "peripheral_model_reliability_min": 0.15,
    "peripheral_min_coverage_value": 0.01,
    "max_disagreement_expansion_layers": 2,
    "maximum_support_buckets": 4,
    "dispersion_rungs_per_expansion_layer": 1.0,
    "clip_min_notional_usdc": 1.0,
    "price_tick_size": 0.01,
    "maximum_limit_price": 0.99,
    "minimum_expected_edge_per_share": 0.0,
    "minimum_expected_pnl_after_usdc": -15.0,
    "fee_rate": 0.0,
    "maker_queue_cost_per_share": 0.0,
    "taker_slippage_buffer_per_share": 0.0,
    "protected_floor_usdc": -15.0,
    "max_outside_loss_usdc": 15.0,
    "basket_downside_lambda_per_usdc": 0.02,
    "basket_directional_coverage_lambda_usdc": 0.05,
    "basket_concentration_lambda_usdc": 0.25,
    "disagreement_concentration_gain_per_layer": 0.50,
    "maximum_bucket_budget_fraction": 0.75,
    "basket_optimizer_max_iterations": 4000,
    "basket_optimizer_tolerance": 1e-8,
}


def evaluate_no_live_cadence(
    *,
    cadence_theta: Mapping[str, Any],
    current_snapshot: CadenceSnapshot | Mapping[str, Any] | None,
    previous_snapshot: CadenceSnapshot | Mapping[str, Any] | None,
    b13_b14_passed: bool,
    budget_passed: bool,
    order_state_known: bool,
    candidate_present: bool,
) -> str:
    """Evaluate cadence after the existing no-live execution gates.

    The caller supplies the immutable cadence/hazard observations and the
    already-evaluated B13/B14, budget, and order-state results.  This helper
    never derives a clock, hazard score, version, probability, support, or
    target share.  It only replaces the two aggregate gate booleans in the
    current snapshot with the post-gate result and returns one of the three
    cadence decisions.
    """

    thresholds = CadenceThresholds.from_station_policy_theta(cadence_theta)
    if thresholds is None:
        return CadenceDecision.HOLD.value

    if isinstance(current_snapshot, CadenceSnapshot):
        current_raw = current_snapshot.to_dict()
    elif isinstance(current_snapshot, Mapping):
        current_raw = dict(current_snapshot)
    else:
        return CadenceDecision.HOLD.value

    all_post_gates_passed = all(
        isinstance(value, bool) and value
        for value in (
            b13_b14_passed,
            budget_passed,
            order_state_known,
        )
    )
    # These are the only fields runtime is allowed to derive: they are the
    # result of gates that have already run.  Hazard, versions and time must
    # remain explicit inputs in the supplied snapshot.
    current_raw["safety_gates_passed"] = all_post_gates_passed
    current_raw["candidate_present"] = bool(
        isinstance(candidate_present, bool)
        and candidate_present
        and all_post_gates_passed
    )
    return evaluate_cadence(
        current_raw,
        previous_snapshot,
        thresholds,
    )


def _bucket_rung_width_c(envelope: ProbabilityEnvelope) -> float | None:
    """Celsius width of one rung, read off this station's own ladder.

    Charter clause 53 forbids hard-coding the rung by unit: the only lawful
    source is the registry's ``lower/upper``, which every producer already
    carries into lineage as ``bucket_intervals_c_json``.  Taking the median gap
    between consecutive finite bucket centres gives 1.0 on a Celsius book and
    10/9 on a Fahrenheit one, and stays correct for any ladder either book
    grows later -- the same derivation the calibration dashboard uses.

    ``None`` means the envelope did not publish enough geometry to answer:
    fewer than two finite buckets, or no intervals at all.  The two unbounded
    end buckets have no defined width and are excluded.
    """

    raw = envelope.lineage.get("bucket_intervals_c_json")
    if not raw:
        return None
    try:
        intervals = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("probability_bucket_intervals_invalid") from exc
    if not isinstance(intervals, Mapping):
        raise ContractError("probability_bucket_intervals_invalid")
    centers: list[float] = []
    for bucket, bounds in intervals.items():
        if str(bucket) not in envelope.bucket_ordinals:
            continue
        try:
            lower, upper = bounds[0], bounds[1]
        except (TypeError, IndexError, KeyError) as exc:
            raise ContractError("probability_bucket_intervals_invalid") from exc
        if lower is None or upper is None:
            continue
        try:
            center = (float(lower) + float(upper)) / 2.0
        except (TypeError, ValueError) as exc:
            raise ContractError("probability_bucket_intervals_invalid") from exc
        if not math.isfinite(center):
            raise ContractError("probability_bucket_intervals_invalid")
        centers.append(center)
    # Charter 198: one reading of the rung, shared with the width layer's
    # kernel -- the arithmetic that stood here, moved, not changed.
    return ladder_rung_width_c(centers)


def snapshot_from_probability_envelope(
    envelope: ProbabilityEnvelope,
    market_snapshot: Mapping[str, Any],
    *,
    checkpoint_changed: bool = True,
) -> TargetBasketSnapshot:
    """Build the candidate snapshot using only the envelope's p(B) vector."""

    if str(market_snapshot.get("station_id") or "") != envelope.station_id:
        raise ContractError("market_snapshot_station_mismatch")
    if str(market_snapshot.get("event_date") or "") != envelope.event_date:
        raise ContractError("market_snapshot_event_date_mismatch")
    assert_probability_timezone_matches_market(
        probability_timezone=envelope.settlement_timezone,
        market_timezone=str(market_snapshot.get("settlement_timezone") or ""),
    )
    if "forecast_dispersion_c" in market_snapshot:
        raise ContractError("market_snapshot_model_dispersion_forbidden")
    try:
        model_envelope_span_c = float(
            envelope.lineage.get("model_envelope_span_c", "0")
        )
    except (TypeError, ValueError) as exc:
        raise ContractError("probability_model_envelope_span_invalid") from exc
    if not math.isfinite(model_envelope_span_c) or model_envelope_span_c < 0.0:
        raise ContractError("probability_model_envelope_span_invalid")
    bucket_rung_width_c = _bucket_rung_width_c(envelope)
    if model_envelope_span_c > 0.0 and bucket_rung_width_c is None:
        # Only demanded when there is a Celsius span to convert.  A stage that
        # publishes no dispersion needs no exchange rate, and refusing it would
        # take down a scope over a number nothing was going to read.
        raise ContractError("probability_bucket_rung_width_missing")
    raw_lower_ordinal = envelope.lineage.get("model_envelope_lower_ordinal")
    raw_upper_ordinal = envelope.lineage.get("model_envelope_upper_ordinal")
    if (raw_lower_ordinal is None) != (raw_upper_ordinal is None):
        raise ContractError("probability_model_envelope_boundary_incomplete")
    # An absent boundary means "this stage publishes no envelope", so it is
    # read as a silence rather than an error.  A stage that names the envelope
    # buckets but omits the ordinals this reads is therefore not degraded, it
    # is invisible: the candidate boundary collapses onto the probability core
    # and the dispersion widening never runs, with nothing raised.  The bucket
    # names resolve through the envelope's own ordinal contract, so derive the
    # pair rather than refusing: a refusal would take down every already
    # committed envelope of a producer that only ever named the buckets, which
    # is the whole native P0 history.  An unresolvable bucket still fails.
    if raw_lower_ordinal is None:
        named_lower = str(
            envelope.lineage.get("model_envelope_lower_bucket") or ""
        )
        named_upper = str(
            envelope.lineage.get("model_envelope_upper_bucket") or ""
        )
        if bool(named_lower) != bool(named_upper):
            raise ContractError(
                "probability_model_envelope_boundary_incomplete"
            )
        if named_lower:
            if (
                named_lower not in envelope.bucket_ordinals
                or named_upper not in envelope.bucket_ordinals
            ):
                raise ContractError(
                    "probability_model_envelope_boundary_invalid"
                )
            raw_lower_ordinal = envelope.bucket_ordinals[named_lower]
            raw_upper_ordinal = envelope.bucket_ordinals[named_upper]
    model_envelope_lower_ordinal: int | None = None
    model_envelope_upper_ordinal: int | None = None
    if raw_lower_ordinal is not None:
        try:
            model_envelope_lower_ordinal = int(raw_lower_ordinal)
            model_envelope_upper_ordinal = int(raw_upper_ordinal)
        except (TypeError, ValueError) as exc:
            raise ContractError("probability_model_envelope_boundary_invalid") from exc
        lower_bucket = str(
            envelope.lineage.get("model_envelope_lower_bucket") or ""
        )
        upper_bucket = str(
            envelope.lineage.get("model_envelope_upper_bucket") or ""
        )
        if (
            lower_bucket not in envelope.bucket_ordinals
            or upper_bucket not in envelope.bucket_ordinals
            or envelope.bucket_ordinals[lower_bucket] != model_envelope_lower_ordinal
            or envelope.bucket_ordinals[upper_bucket] != model_envelope_upper_ordinal
            or model_envelope_lower_ordinal > model_envelope_upper_ordinal
        ):
            raise ContractError("probability_model_envelope_boundary_invalid")
    # The stage anchor.  Absent for stages that do not publish one, in which
    # case support selection falls back to its own center resolution.
    raw_center_bucket = str(envelope.lineage.get("model_center_bucket") or "")
    model_center_ordinal: int | None = None
    if raw_center_bucket:
        if raw_center_bucket not in envelope.bucket_ordinals:
            raise ContractError("probability_model_center_bucket_unknown")
        model_center_ordinal = int(envelope.bucket_ordinals[raw_center_bucket])
    formal_members_json = envelope.lineage.get("formal_model_members_json")
    raw_formal_lower = envelope.lineage.get(
        "formal_model_anchor_lower_ordinal"
    )
    raw_formal_upper = envelope.lineage.get(
        "formal_model_anchor_upper_ordinal"
    )
    if (raw_formal_lower is None) != (raw_formal_upper is None):
        raise ContractError("probability_formal_model_anchor_incomplete")
    if (formal_members_json is None) != (raw_formal_lower is None):
        raise ContractError("probability_formal_model_anchor_incomplete")
    if (
        envelope.posterior_version
        == "p0_nwp_historical_meta_anchor_v4"
        and formal_members_json is None
    ):
        raise ContractError(
            "probability_formal_model_anchor_required_for_v4"
        )
    formal_lower_ordinal: int | None = None
    formal_upper_ordinal: int | None = None
    if formal_members_json is not None:
        try:
            formal_members = json.loads(str(formal_members_json))
            formal_lower_ordinal = int(raw_formal_lower)
            formal_upper_ordinal = int(raw_formal_upper)
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ContractError(
                "probability_formal_model_anchor_invalid"
            ) from exc
        if (
            not isinstance(formal_members, list)
            or len(formal_members) < 2
            or formal_lower_ordinal > formal_upper_ordinal
        ):
            raise ContractError("probability_formal_model_anchor_invalid")
        member_ordinals: list[int] = []
        member_ids: set[str] = set()
        for member in formal_members:
            if not isinstance(member, Mapping):
                raise ContractError(
                    "probability_formal_model_anchor_invalid"
                )
            model_id = str(member.get("model_id") or "")
            bucket = str(member.get("calibrated_center_bucket") or "")
            try:
                weight = float(member.get("weight"))
            except (TypeError, ValueError) as exc:
                raise ContractError(
                    "probability_formal_model_anchor_invalid"
                ) from exc
            if (
                not model_id
                or model_id in member_ids
                or bucket not in envelope.bucket_ordinals
                or not math.isfinite(weight)
                or weight <= 0.0
            ):
                raise ContractError(
                    "probability_formal_model_anchor_invalid"
                )
            member_ids.add(model_id)
            member_ordinals.append(envelope.bucket_ordinals[bucket])
        if (
            min(member_ordinals) != formal_lower_ordinal
            or max(member_ordinals) != formal_upper_ordinal
        ):
            raise ContractError(
                "probability_formal_model_anchor_boundary_mismatch"
            )
    reported_high_bucket: str | None = None
    reported_high_ordinal: int | None = None
    reported_high_observation_id: str | None = None
    hard_bound_id: str | None = None
    reported_high_advanced: bool | None = None
    # Only fields that exist *because* an official report advanced the
    # station-day high belong here.  ``settlement_timezone`` deliberately does
    # not: every envelope carries it at every stage, so including it made the
    # P0 branch below reject every P0 envelope that was ever built -- the
    # forbidden-key check fires on *any* present key, and that one is always
    # present.  The P1 branch never reads it either.
    risk_lineage_keys = {
        "reported_high_bucket",
        "reported_high_ordinal",
        "canonical_observation_event_id",
        "hard_bound_id",
    }
    present_risk_keys = {
        key
        for key in risk_lineage_keys
        if str(envelope.lineage.get(key) or "").strip()
    }
    if envelope.stage == "P1":
        if present_risk_keys != risk_lineage_keys:
            raise ContractError(
                "p1_reported_high_risk_lineage_required"
            )
        reported_high_bucket = str(
            envelope.lineage["reported_high_bucket"]
        )
        try:
            reported_high_ordinal = int(
                envelope.lineage["reported_high_ordinal"]
            )
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "p1_reported_high_ordinal_invalid"
            ) from exc
        if (
            reported_high_bucket not in envelope.bucket_ordinals
            or envelope.bucket_ordinals[reported_high_bucket]
            != reported_high_ordinal
        ):
            raise ContractError(
                "p1_reported_high_bucket_ordinal_mismatch"
            )
        reported_high_observation_id = str(
            envelope.lineage["canonical_observation_event_id"]
        )
        hard_bound_id = str(envelope.lineage["hard_bound_id"])
        raw_advanced = envelope.lineage.get("reported_high_advanced")
        if raw_advanced is None:
            reported_high_advanced = True
        elif raw_advanced == "true":
            reported_high_advanced = True
        elif raw_advanced == "false":
            reported_high_advanced = False
        else:
            raise ContractError("p1_reported_high_advanced_invalid")
    elif present_risk_keys:
        raise ContractError("p0_reported_high_risk_lineage_forbidden")
    observed_running_high_c = running_high_from_market_snapshot(
        market_snapshot
    )
    observed_dead_buckets = dead_buckets_for_running_high(
        ordered_buckets=sorted(
            envelope.probabilities,
            key=lambda bucket: envelope.bucket_ordinals.get(bucket, 0),
        ),
        bucket_intervals_c_json=envelope.lineage.get(
            "bucket_intervals_c_json"
        ),
        running_high_c=observed_running_high_c,
    )
    raw_buckets = market_snapshot.get("buckets")
    if not isinstance(raw_buckets, Sequence) or isinstance(raw_buckets, (str, bytes, bytearray)) or not raw_buckets:
        raise ContractError("market_snapshot_buckets_missing")
    seen: set[str] = set()
    buckets: list[dict[str, Any]] = []
    raw_reliability = envelope.lineage.get(
        "peripheral_model_reliability_by_bucket_json",
        "{}",
    )
    try:
        reliability_by_bucket = json.loads(raw_reliability)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("probability_peripheral_model_reliability_invalid") from exc
    if not isinstance(reliability_by_bucket, Mapping):
        raise ContractError("probability_peripheral_model_reliability_invalid")
    if set(reliability_by_bucket) - set(envelope.probabilities):
        raise ContractError("probability_peripheral_model_reliability_support_mismatch")
    for raw in raw_buckets:
        if not isinstance(raw, Mapping):
            raise ContractError("market_snapshot_bucket_invalid")
        bucket = str(raw.get("bucket") or "")
        if bucket not in envelope.probabilities or bucket in seen:
            raise ContractError("market_snapshot_probability_support_mismatch")
        seen.add(bucket)
        row = dict(raw)
        row["probability"] = envelope.probabilities[bucket]
        row["ordinal"] = envelope.bucket_ordinals[bucket]
        try:
            reliability = float(reliability_by_bucket.get(bucket, 0.0))
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "probability_peripheral_model_reliability_invalid"
            ) from exc
        if not math.isfinite(reliability) or not 0.0 <= reliability <= 1.0:
            raise ContractError("probability_peripheral_model_reliability_invalid")
        row["peripheral_model_reliability"] = reliability
        buckets.append(row)
    if seen != set(envelope.probabilities):
        raise ContractError("market_snapshot_probability_support_incomplete")
    observed_at = str(market_snapshot.get("observed_at") or envelope.decision_at_utc)
    quote_observed_at = str(market_snapshot.get("quote_observed_at") or observed_at)
    raw = {
        "schema": TARGET_BASKET_SNAPSHOT_SCHEMA,
        "snapshot_id": str(market_snapshot.get("snapshot_id") or "target_snapshot_" + envelope.content_sha256[:24]),
        "sequence": int(market_snapshot.get("sequence", 0)),
        "observed_at": observed_at,
        "market_id": str(market_snapshot.get("market_id") or ""),
        "station_id": envelope.station_id,
        "event_date": envelope.event_date,
        "quote_version": str(market_snapshot.get("quote_version") or ""),
        "quote_observed_at": quote_observed_at,
        "quote_source": str(market_snapshot.get("quote_source") or "OFFICIAL_CLOB_WS"),
        "inventory_version": str(market_snapshot.get("inventory_version") or ""),
        "order_state_version": str(market_snapshot.get("order_state_version") or ""),
        "evidence": {
            "probability_stage": envelope.stage,
            "posterior_version": envelope.posterior_version,
            "posterior_sha256": envelope.posterior_sha256,
            "forecast_dispersion_c": model_envelope_span_c,
            "bucket_rung_width_c": bucket_rung_width_c,
            "model_envelope_lower_ordinal": model_envelope_lower_ordinal,
            "model_envelope_upper_ordinal": model_envelope_upper_ordinal,
            "model_center_ordinal": model_center_ordinal,
            "formal_model_members_json": formal_members_json,
            "formal_model_anchor_lower_ordinal": formal_lower_ordinal,
            "formal_model_anchor_upper_ordinal": formal_upper_ordinal,
            "reported_high_bucket": reported_high_bucket,
            "reported_high_ordinal": reported_high_ordinal,
            "reported_high_observation_id": (
                reported_high_observation_id
            ),
            "hard_bound_id": hard_bound_id,
            "reported_high_advanced": reported_high_advanced,
            # Charter 125: the observed-high floor, at every stage.
            #
            # Deliberately not part of the risk lineage above.  That block is
            # P1's chain root -- which observation the posterior was built on
            # -- and is forbidden elsewhere precisely so a forecast cannot
            # pose as a settled fact.  This pair claims nothing about how the
            # posterior was built; it is the arithmetic bound the day's own
            # thermometer puts under the ladder, and a P0 basket is as
            # subject to it as a P1 one.  ``observed_running_high_c`` is
            # absent for a pre-day compile, which is most P0 decisions, and
            # the two keys are then omitted entirely rather than written as
            # null -- an absent floor must leave every downstream dict, and
            # therefore every content hash, byte-identical to the pre-ruling
            # one.
            **(
                {
                    "observed_running_high_c": observed_running_high_c,
                    "observed_dead_buckets": observed_dead_buckets,
                }
                if observed_running_high_c is not None
                else {}
            ),
        },
        "buckets": buckets,
        "checkpoint_changed": bool(
            market_snapshot.get(
                "checkpoint_changed", checkpoint_changed
            )
            and (
                envelope.stage != "P1"
                or reported_high_advanced is not False
            )
        ),
        "quote_changed": bool(market_snapshot.get("quote_changed", False)),
        "fill_changed": bool(market_snapshot.get("fill_changed", False)),
        "time_gate_changed": bool(market_snapshot.get("time_gate_changed", False)),
        "evidence_fresh": bool(market_snapshot.get("evidence_fresh", True)),
        "contract_matches": bool(market_snapshot.get("contract_matches", True)),
        "order_state_known": bool(market_snapshot.get("order_state_known", True)),
        "risk_inputs_complete": bool(market_snapshot.get("risk_inputs_complete", True)),
        "terminal": bool(market_snapshot.get("terminal", False)),
        "time_left_minutes": float(market_snapshot.get("time_left_minutes", 1440.0) or 0.0),
        "recent_fill_rate": float(market_snapshot.get("recent_fill_rate", 0.0) or 0.0),
        # Charter 136 (五): the seat a broken chain completes back to.  Omitted
        # when the chain has sent nothing, so the snapshot hashes as before.
        **(
            {"chain_seat_buckets": [
                str(bucket) for bucket in market_snapshot["chain_seat_buckets"]
            ]}
            if market_snapshot.get("chain_seat_buckets") else {}
        ),
        # Charter 143: the size each seat bucket was last sent at.
        **(
            {"chain_sent_shares": {
                str(bucket): float(shares)
                for bucket, shares in dict(
                    market_snapshot["chain_sent_shares"]
                ).items()
            }}
            if market_snapshot.get("chain_sent_shares") else {}
        ),
    }
    return TargetBasketSnapshot.from_mapping(raw)


def _assert_current_p1_source_lane(
    envelope: ProbabilityEnvelope,
    *,
    expected_source_lane: str | None,
) -> None:
    """Defend the pure runtime boundary against a wrong-lane final P1.

    The authoritative lane is resolved by the target-SSOT caller and is
    carried explicitly into this database-free optimizer.  This function is
    intentionally not a source of authority itself: callers that do not own
    a target registry may use the optimizer for offline analysis, while every
    production compiler supplies the registry-derived lane.
    """

    if envelope.stage != "P1":
        return
    if expected_source_lane is None:
        return
    lane = str(expected_source_lane or "").strip().lower()
    if lane not in {"amsc", "awc"}:
        raise ContractError("probability_expected_source_lane_invalid")
    supplied = str(envelope.lineage.get("source_lane") or "").strip().lower()
    if supplied not in admissible_p1_source_lanes(lane):
        raise ContractError("probability_p1_source_lane_mismatch")
    if (
        str(envelope.lineage.get("prediction_root_stage") or "")
        .strip()
        .upper()
        not in {"P0", "P0-M"}
    ):
        raise ContractError("probability_p1_root_stage_mismatch")


def _executable_depth_cash(
    *,
    ask_levels: Sequence[Any],
    execution_candidate: str,
    limit_price: float,
    cash_clip: float,
) -> float:
    """How much of the book this order can actually consume, in cash.

    A Taker lifts the offer, so the offer at or below its limit is exactly
    what it can take.

    A post-only Maker bid rests inside the spread and takes nothing off the
    offer, so the offer is not a bound on how large it may be.  Capping a
    Maker leg by total ask depth -- what this did until 2026-08-16 -- refused
    it outright with ``executable_depth_empty`` on a book with no offer at
    all, which is precisely the book where resting a bid is the only thing
    left to do.  Maker fill risk is priced where it is measured: the queue
    ahead, the spread and the depth all enter the fill distribution the
    bundle scored, so charging it a second time here as a hard size cap is
    the double counting clause 43 forbids.  What genuinely bounds a Maker leg
    is its own clip, the inventory gap and the budget, and the sizing step
    caps by all three already; this returns the clip so the depth term is
    present, auditable and non-binding rather than absent.
    """

    if str(execution_candidate) == "TAKER_FOK":
        # Valued at the order's own limit, because the sizing step turns this
        # back into shares as ``depth / limit``.  Valued at each level's price
        # it understated the shares on any book that steps up to the limit:
        # 5 @ 0.20 + 5 @ 0.30 is ten shares a 0.30 FOK can lift, and 2.50 /
        # 0.30 capped the order at eight (audit 2026-09-14).  On a single
        # level the two valuations are the same number.
        return float(limit_price) * sum(
            float(level.shares)
            for level in ask_levels
            if float(level.price) <= float(limit_price) + 1e-9
        )
    return max(0.0, float(cash_clip))


CLAMPED_CLIP_REASON = "sizing_clamped_below_authorized_clip"
# The other way execution changes the basket that was scored.  Charter 70/83
# raise a sub-lot leg to one exchange lot, and the raise is allowed to exceed
# the clip the objective authorized -- so the shares that go out are not the
# shares that were judged, in the direction nothing was checking.
#
# Measured 2026-09-08 over 234 seated baskets: 96% had their share vector
# changed by the lot, the two readings of the same basket differ by a median
# 6.2 points and a range of -22.7 to +123.8, and 40 of them (17%) buy a
# basket worse than the one authorized.  The mechanism is that the venue's
# one-dollar floor asks ceil(1/price) shares, so the cheapest leg gets the
# most shares -- and cheap is what the model calls unlikely.  Legs under
# p=0.10 average 13.3 shares, legs over p=0.30 average 5.9.
#
# MPMG 2026-09-09 is the judicial case: 25 shares of a 0.001 bucket at 4c,
# 8 of a 0.172 at 14c, 5 of a 0.44 at 35c.  Equal-share reading +15.7%,
# actual-share reading -7.0%, and it went out -- only the 25-share leg
# filled, so the book paid a dollar for a bucket it gives one chance in a
# thousand.
RAISED_LOT_REASON = "raised_to_exchange_minimum_lot"
RESHAPED_SIZING_REASONS = frozenset({CLAMPED_CLIP_REASON, RAISED_LOT_REASON})
# What a basket says when the shares that would go out do not pass the
# evaluation the shares that were judged passed.
RESHAPED_BUNDLE_REFUSED_REASON = "reshaped_bundle_rescore_refused"
# The basket that would go out does not clear the net return it is bought
# for, read on the shares it would actually hold.
BASKET_NET_ROI_REFUSED_REASON = "basket_net_roi_below_target"


def roi_gate_admission_price_v1(
    *,
    execution_candidate: Any,
    limit_price: float,
    ask_price: float | None,
) -> float:
    """The price the net-ROI target judges one leg on.

    A resting leg is judged at the offer, like every other gate in the chain:
    what the market will sell for now is the admission test, and the rung is
    only what we pay if the market comes to us.  A crossing leg is judged at
    its own limit, which under charter 127 IS the walked offer -- for a take
    the admission price and the payment price are the same number, and
    substituting anything else would be inventing one.

    Falls back to the limit when the book has no offer at all: a basket
    priced against nothing is not a stricter test, it is an unanswerable one.
    """

    if str(execution_candidate or "").strip().upper() == "TAKER_FOK":
        return float(limit_price)
    if ask_price is None or float(ask_price) <= 0.0:
        return float(limit_price)
    return float(ask_price)


def _rescore_clamped_bundle(
    *,
    sizing_decisions: Sequence[Any],
    resize: Any,
    delta_plan: Mapping[str, Any],
    snapshot: TargetBasketSnapshot,
    delta_policy: DeltaV2PolicyArtifact,
    policy: TargetBasketPolicy,
    capital_available_usdc: float,
    selected_action_evaluations: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Re-authorize the bundle at the sizes execution would actually send.

    Returns None when nothing was cut -- the ordinary case, and the one where
    the authorization already covers what goes out.  Otherwise it re-sizes
    with the cut permitted, and puts those exact quantities back through
    ``rescore_maker_bundle_v2``: the same joint fill lattice, the same value
    weights, the same floor and epsilon that granted the original
    authorization.  Every input is taken from where the compile put it rather
    than reconstructed -- ``bundle_scored_scopes`` exists because the risk
    scopes the bundle was scored against are the seat-intersected ones and
    the plan's diagnostic fields are not.

    The receipt is returned either way; a refusal leaves the caller's
    refusing sizing in place, which is what blocks the trade.
    """

    if not any(
        item.reason_code in RESHAPED_SIZING_REASONS
        for item in sizing_decisions
        if item.candidate_present
    ):
        return None
    required = tuple(sorted(
        str(value) for value in delta_plan.get("execution_gap_support", ())
    ))
    scopes = delta_plan.get("bundle_scored_scopes")
    solution = delta_plan.get("target_solution")
    if not required or not isinstance(scopes, Mapping) or not isinstance(
        solution, Mapping
    ):
        return {
            "schema": BUNDLE_RESCORE_SCHEMA,
            "authorized": False,
            "blockers": ["rescore_inputs_unavailable"],
        }
    cut, _remaining = resize(clamped_clip_authorized=True)
    sized = {
        item.bucket: item for item in cut if item.candidate_present
    }
    if set(sized) != set(required) or not all(
        item.eligible for item in sized.values()
    ):
        # Something other than the clip cut refuses these legs -- a clip under
        # the minimum, an empty book.  Those are not a re-scoring question.
        return {
            "schema": BUNDLE_RESCORE_SCHEMA,
            "authorized": False,
            "blockers": sorted({
                item.reason_code
                for item in sized.values()
                if not item.eligible
            } or {"rescore_bundle_incomplete"}),
        }
    try:
        return rescore_maker_bundle_v2(
            selected_action_evaluations,
            required_buckets=required,
            probabilities={
                row.bucket: row.probability for row in snapshot.buckets
            },
            current_shares_by_bucket={
                row.bucket: row.filled_shares for row in snapshot.buckets
            },
            current_cost_by_bucket={
                row.bucket: row.filled_cost_usdc for row in snapshot.buckets
            },
            target_shares_by_bucket=dict(
                solution.get("target_shares_by_bucket") or {}
            ),
            protected_buckets=tuple(
                str(value) for value in scopes.get("protected_buckets", ())
            ),
            shortfall_buckets=tuple(
                str(value) for value in scopes.get("shortfall_buckets", ())
            ),
            breakthrough_buckets=tuple(
                str(value)
                for value in scopes.get("breakthrough_buckets", ())
            ),
            value_weights=delta_policy.value_weights,
            execution_weights=delta_policy.execution_weights,
            capital_available_usdc=capital_available_usdc,
            minimum_profit_floor=float(
                policy.minimum_expected_pnl_after_usdc
            ),
            # Charter 116: the compile's repair verdict, carried on the plan.
            # Recomputing it here would judge the repair against a book that
            # has moved since the target was struck.
            completion_repair=delta_plan.get("completion_repair"),
            # Charter 73: the re-score authorizes on the rule and the
            # belief the plan was authorized on.
            # Charter 92: the same rule function the compile used, so a
            # ticket re-prices on the whole-basket edge it was seated on.
            authorization_rule=execution_authorization_rule_v1(
                winner_candidate_origin=delta_plan.get(
                    "basket_winner_candidate_origin"
                ),
                objective_form=delta_plan.get("basket_objective_form"),
            ),
            edge_probabilities=(
                (
                    (delta_plan.get("basket_support_selection") or {})
                    .get("seated") or {}
                ).get("window_probabilities") or None
            ),
            sized_shares_by_bucket={
                bucket: item.q_order for bucket, item in sized.items()
            },
            sized_notional_by_bucket={
                bucket: item.c_order for bucket, item in sized.items()
            },
            fee_rate=policy.fee_rate,
        )
    except (ContractError, KernelContractError) as exc:
        # A re-score that cannot be computed is a refusal, never a pass.
        return {
            "schema": BUNDLE_RESCORE_SCHEMA,
            "authorized": False,
            "blockers": [f"rescore_contract_error:{exc}"],
        }


def _compile_target_plan(
    *,
    # Which stage's execution policy governs the action, which is not always
    # the stage that produced the distribution.  An opening plan is opened at
    # P0 whatever probability it opens on; binding the two together meant a
    # station whose P0-M envelope landed before its preselection could not
    # resolve any policy at all, because P0-M has never had one under the
    # current strategy version.  Measured over 2026-08-19..23: every station
    # that got a plan had its P0-M envelope arrive after the plan, and every
    # station that got none had a P0-M envelope arrive first -- four to eight
    # stations a day, twenty-one on 2026-08-23.  Defaults to the envelope's
    # own stage, which is what every non-opening caller wants.
    station_policy_stage: str | None = None,
    probability_envelopes: Sequence[ProbabilityEnvelope | Mapping[str, Any]],
    market_snapshot: Mapping[str, Any],
    policy_activations: Sequence[StationBasketPolicyActivation | Mapping[str, Any]],
    global_priors: Mapping[str, GlobalBasketPriorArtifact] | Sequence[GlobalBasketPriorArtifact],
    policy_artifacts: Mapping[str, PolicyArtifact] | Sequence[PolicyArtifact],
    evaluation_at_utc: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    strategy_version: str,
    base_policy: Mapping[str, Any] | None = None,
    delta_v2_policy: DeltaV2PolicyArtifact | Mapping[str, Any] | None = None,
    delta_v2_fill_resolver: Callable[..., Mapping[str, Any]] | None = None,
    cash_phase_policy: BasketCashPhasePolicyArtifact | Mapping[str, Any] | None = None,
    cash_phase_input: BasketCashPhaseInput | Mapping[str, Any] | None = None,
    cadence_snapshot: CadenceSnapshot | Mapping[str, Any] | None = None,
    previous_cadence_snapshot: CadenceSnapshot | Mapping[str, Any] | None = None,
    delta_v2_owns_cadence: bool = False,
    resource_probe_ok: bool = False,
    policy_authority_scope: str = "FORMAL_PROMOTED",
    regime_artifact: RegimeArtifact | Mapping[str, Any] | None = None,
    require_regime_ssot: bool = True,
    expected_source_lane: str | None = None,
    chain_consolidation_tilt: float | None = None,
    last_acted_probability_identity: str | None = None,
    last_decision_signature: Mapping[str, Any] | None = None,
    effective_edge_tables: Mapping[str, Any] | None = None,
    off_centre_claim_bands: Mapping[str, Any] | None = None,
    convergence_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile one full stage → policy → v2 delta plan, no-live boundary."""

    # Charter 141: the operator's capital dials, re-read when the file moved.
    capital_layer = refresh_capital_layer()

    def _blocked(
        *,
        blockers: Sequence[str],
        station_policy: dict[str, Any] | None = None,
        regime_audit: Mapping[str, Any] | None = None,
        remaining_budget_before: float,
        extra_plan: Mapping[str, Any] | None = None,
        window_gate_counts: Mapping[str, Any] | None = None,
        window_failures: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        delta_audit = {
            "schema": "delta_basket_score_authorization_v2",
            "authorized": False,
            "blockers": list(blockers),
        }
        # Which gate emptied the window list, when the compile died there.
        # This is what window_gate_counts_v1 was written for on 2026-09-05
        # ("a blocked compile recorded only the error's name"), and it had
        # never reached a stored row: the solver attaches the funnel to its
        # exception, and the only reader was the blocked-scope decision
        # path, which these holds do not take -- measured 2026-09-07, all
        # 253 rows carrying the key had it null, and a support_empty scope
        # wrote no decision row at all. Three of eight compiles in the
        # 03:46Z batch blocked this way with nothing recorded about why.
        if window_gate_counts is not None:
            delta_audit["window_gate_counts"] = dict(window_gate_counts)
        # The per-window solve failures ride with the counts, not apart from
        # them: ``delta_v2_no_solvable_support_window`` is the one refusal
        # whose counts say nothing (every window was admissible), so a plan
        # that carried only the counts described that case as an empty
        # funnel and left the actual reason -- one row per window that threw
        # -- on the exception this branch has just absorbed.
        if window_failures is not None:
            delta_audit["window_failures"] = [
                dict(row) for row in window_failures
            ]
        return {
            "schema": "bayes_basket_target_plan_v1",
            "status": "blocked",
            "reason_codes": list(blockers),
            "stage": envelope.stage,
            "probability_envelope": envelope.to_dict(),
            "resolved_station_policy": station_policy or {},
            "plan": dict(extra_plan) if isinstance(extra_plan, Mapping) else None,
            "delta_v2_audit": delta_audit,
            "cash_phase_decision": None,
            "order_sizing_decisions": [],
            "bundle_rescore": None,
            "cadence_decision": CadenceDecision.HOLD.value,
            "cadence_allows_candidate": False,
            "remaining_budget_usdc_before": remaining_budget_before,
            "remaining_budget_usdc_after": remaining_budget_before,
            "station_reserve_violations": [],
            "boundary": {
                "target_only": True,
                "execution_mode": "production_no_live",
                "live_order_authorized": False,
                "places_orders": False,
                "orders_submitted": 0,
                "writes_production_db": False,
                "calls_order_gateway": False,
                "cadence_decision": CadenceDecision.HOLD.value,
                "cadence_allows_candidate": False,
                "cadence_order_authorized": False,
                "blocked": True,
            },
            "regime_audit": (
                dict(regime_audit)
                if isinstance(regime_audit, Mapping)
                else {
                    "schema": "bayes_basket_regime_gate_v1",
                    "status": "NOT_APPLIED",
                    "required": bool(require_regime_ssot),
                }
            ),
        }

    envelope = select_current_stage(probability_envelopes, at_utc=evaluation_at_utc)
    # The checkpoint immediately before the one being served, on the same
    # station-day: rebalancing follows where p(t) has moved, so it needs the
    # previous reading to compare against.  Absent (a first checkpoint, or a
    # caller that supplied only one envelope) means no direction is known
    # and every bucket stays eligible, which is the pre-rule behaviour.
    previous_probabilities: dict[str, float] | None = None
    _prior_chain = sorted(
        (
            item
            if isinstance(item, ProbabilityEnvelope)
            else ProbabilityEnvelope.from_mapping(item)
            for item in probability_envelopes
        ),
        key=lambda item: str(item.decision_at_utc),
    )
    _earlier = [
        item
        for item in _prior_chain
        if str(item.station_id) == str(envelope.station_id)
        and str(item.event_date) == str(envelope.event_date)
        and str(item.decision_at_utc) < str(envelope.decision_at_utc)
    ]
    if _earlier:
        previous_probabilities = {
            str(bucket): float(value)
            for bucket, value in _earlier[-1].probabilities.items()
        }
    _assert_current_p1_source_lane(
        envelope,
        expected_source_lane=expected_source_lane,
    )
    snapshot = snapshot_from_probability_envelope(envelope, market_snapshot)
    raw_budget_for_block = market_snapshot.get("remaining_budget_usdc", 0.0)
    try:
        remaining_budget_before = (
            float(raw_budget_for_block)
            if not isinstance(raw_budget_for_block, bool)
            else 0.0
        )
    except (TypeError, ValueError):
        remaining_budget_before = 0.0
    blockers: list[str] = []
    if delta_v2_policy is None:
        blockers.append("delta_v2_policy_missing")
    if not callable(delta_v2_fill_resolver):
        blockers.append("delta_v2_fill_resolver_missing")
    if cash_phase_policy is None:
        blockers.append("cash_phase_policy_missing")
    if cash_phase_input is None:
        blockers.append("cash_phase_input_missing")
    if blockers:
        return _blocked(
            blockers=blockers,
            remaining_budget_before=remaining_budget_before,
        )

    if require_regime_ssot and regime_artifact is None:
        return _blocked(
            blockers=["regime_ssot_missing"],
            remaining_budget_before=remaining_budget_before,
            regime_audit={
                "schema": "bayes_basket_regime_gate_v1",
                "status": "NOT_APPLIED",
                "required": True,
                "blocker": "regime_ssot_missing",
            },
        )

    normalized_cash_input = (
        cash_phase_input
        if isinstance(cash_phase_input, BasketCashPhaseInput)
        else BasketCashPhaseInput.from_mapping(cash_phase_input)
    )
    cash_phase_blocker: str | None = None
    try:
        cash_phase_decision = evaluate_cash_phase(
            cash_phase_policy
            if isinstance(cash_phase_policy, BasketCashPhasePolicyArtifact)
            else BasketCashPhasePolicyArtifact.from_mapping(cash_phase_policy),
            normalized_cash_input,
        )
    except ContractError as exc:
        if str(exc) != "cash_phase_unobserved_phase_fail_closed":
            raise
        # An unobserved cash phase blocks sizing and execution authority, not
        # the upstream probability/WS basket preselection.  Continue only far
        # enough to materialize the v2 support/direction diagnostic, then
        # return a zero-order blocked plan below.
        cash_phase_decision = None
        cash_phase_blocker = str(exc)

    resolved = resolve_station_policy(
        activations=policy_activations,
        global_priors=global_priors,
        artifacts=policy_artifacts,
        station_id=envelope.station_id,
        stage=station_policy_stage or envelope.stage,
        season_bin=season_bin,
        maturity_bin=maturity_bin,
        liquidity_bin=liquidity_bin,
        strategy_version=strategy_version,
        # Resolve the execution policy at the action evaluation time.  The
        # probability envelope keeps its own immutable evidence as-of clock.
        decision_at_utc=evaluation_at_utc,
        required_authority_scope=policy_authority_scope,
    )

    resolved_regime: RegimeArtifact | None = None
    if regime_artifact is not None:
        resolved_regime = resolve_regime_artifact(
            regime_artifact,
            station_id=envelope.station_id,
            event_date=envelope.event_date,
            stage=envelope.stage,
            checkpoint_id=envelope.checkpoint_id,
            settlement_timezone=envelope.settlement_timezone,
            probability_sha256=envelope.content_sha256,
            evaluation_at_utc=evaluation_at_utc,
        )

    policy_config = dict(_DEFAULT_POLICY)
    policy_config.update(resolved.to_adaptive_policy_overrides())
    policy_config.update(dict(base_policy or {}))
    policy_config.update(
        _capital_layer_policy_bounds(market_snapshot.get("book_equity_usdc"))
    )
    policy = TargetBasketPolicy.from_mapping(policy_config)
    if resolved_regime is not None:
        # Regime may only tighten concentration; never alter p(B).
        multiplier = min(1.0, max(0.05, resolved_regime.concentration_multiplier))
        policy_config["basket_concentration_lambda_usdc"] = (
            float(policy_config["basket_concentration_lambda_usdc"]) / multiplier
        )
        policy = TargetBasketPolicy.from_mapping(policy_config)

    raw_budget = market_snapshot.get("remaining_budget_usdc")
    if raw_budget is None or isinstance(raw_budget, bool):
        blockers = ["remaining_budget_usdc_required"]
        return _blocked(
            blockers=blockers,
            remaining_budget_before=remaining_budget_before,
            station_policy=resolved.to_dict(),
        )
    try:
        remaining_budget = float(raw_budget)
    except (TypeError, ValueError) as exc:
        raise ContractError("remaining_budget_usdc_invalid") from exc
    if not math.isfinite(remaining_budget) or remaining_budget < 0.0:
        raise ContractError("remaining_budget_usdc_invalid")
    # A station may risk at most its own bankroll, ever (user ruling,
    # 2026-08-16: one station, 30 USDC).  What arrives here is the *pool's*
    # remaining balance, and the allocator hands it to a station unchanged
    # whenever that station has no history to split on -- 514.29 to each of
    # forty-five stations against a book that holds 514.29 once.
    #
    # Letting a station spend past its own bankroll breaks the objective
    # rather than merely over-committing the book, because the curve is
    # scored against that bankroll: at ``total_budget_usdc`` 30 the effective
    # bankroll is 30/f = 120, so a settlement outside the basket costs the
    # whole 121 already spent and ``curve_score`` returns minus infinity --
    # for every candidate including buying nothing.  The station then holds
    # forever, with no reason code, because nothing can outscore anything.
    # Measured on a manufactured position: 20 spent still traded, 90 already
    # held, 121 was the wall.
    #
    # Capping here makes that wall unreachable by construction rather than by
    # hope: 30 of an effective 120 leaves the worst settlement at 0.75 of the
    # bankroll.  The ceiling is the resolved policy's, so a station that is
    # later allotted more follows it without a second dial to keep in sync.
    station_committed_usdc = sum(
        row.filled_cost_usdc + row.pending_notional_usdc
        for row in snapshot.buckets
    )
    # A share of the book, not a flat number (user ruling, 2026-08-26).  The
    # flat 30 could not shrink when the book lost or grow when it won, and
    # the paper ledger -- which is where every measured ROI figure in this
    # system comes from -- has capped at 2% of its own equity since it was
    # built.  Same station-day, two sizes, depending on which side you asked.
    # ``scope_stake_cap_usdc`` is the kernel's, so there is one definition.
    #
    # The equity is the pool's, and ``remaining_budget_usdc`` is the pool's
    # remaining balance -- the same number while nothing is reserved, which
    # is the case for as long as this lane holds no inventory.  Once it does,
    # the producer must put the pool's own ``balance_usdc`` on the snapshot
    # as ``book_equity_usdc``: remaining cash is not equity, and sizing off
    # it would shrink the allowance every time the book committed.
    book_equity_usdc = float(
        market_snapshot.get("book_equity_usdc") or remaining_budget
    )
    # Charter 141 (user rulings 2026-09-17): two numbers for the station-day.
    # ``station_capital_usdc`` is what sizing may target (6.5% of the real
    # book less the chain), ``station_ceiling_usdc`` what may go out (10%):
    # the venue-minimum lift, a completion, the budget gate and the order
    # sizing below.  No stage share.
    station_capital_usdc = scope_stake_cap_usdc(
        book_equity_usdc=book_equity_usdc,
        stage=str(snapshot.evidence.probability_stage or ""),
        committed_usdc=station_committed_usdc,
    )
    station_ceiling_usdc = station_day_ceiling_usdc(
        book_equity_usdc=book_equity_usdc,
        committed_usdc=station_committed_usdc,
    )
    # Charter 141: the whole book's exposure bound, when the producer read
    # the account.  Open exposure is the marked value of open positions plus
    # resting buys; the room is what the exposure fraction of the real book
    # still leaves, and no station-day may spend past it.  Absent (a fixture,
    # the paper ledger), no bound -- the ledger has no account to read.
    portfolio_room_usdc: float | None = None
    _exposure = market_snapshot.get("account_open_exposure_usdc")
    _exposure_fraction = market_snapshot.get("portfolio_exposure_fraction")
    if _exposure not in (None, "") and _exposure_fraction not in (None, ""):
        portfolio_room_usdc = max(
            0.0,
            float(_exposure_fraction) * book_equity_usdc - float(_exposure),
        )
        station_capital_usdc = min(station_capital_usdc, portfolio_room_usdc)
        station_ceiling_usdc = min(station_ceiling_usdc, portfolio_room_usdc)
    pool_remaining_usdc = remaining_budget
    sizing_capital_usdc = min(remaining_budget, station_capital_usdc)
    remaining_budget = min(remaining_budget, station_ceiling_usdc)

    try:
        resolved_delta_policy = (
            delta_v2_policy
            if isinstance(delta_v2_policy, DeltaV2PolicyArtifact)
            else DeltaV2PolicyArtifact.from_mapping(delta_v2_policy)
        )
    except ContractError:
        raise

    if resource_probe_ok is not True:
        return _blocked(
            blockers=["resource_probe_unavailable"],
            remaining_budget_before=remaining_budget_before,
            station_policy=resolved.to_dict(),
        )

    # Charter 67: P1 holds until the local hour the daily maximum forms in.
    # An explicit HOLD, not a compile refusal -- the observation and bound
    # persistence upstream of this point must keep flowing, and a stage
    # whose window has not opened has declined to act, not failed to.  The
    # ledger's entry walk carries the same clause from the same kernel
    # constant, so the strategy measured is the strategy run.
    if (
        str(snapshot.evidence.probability_stage or "") == "P1"
        and DEFAULT_P1_ENTRY_LOCAL_HOUR > 0
        and envelope.settlement_timezone
    ):
        try:
            _local = datetime.fromisoformat(
                str(snapshot.observed_at).replace("Z", "+00:00")
            ).astimezone(ZoneInfo(str(envelope.settlement_timezone)))
        except (ValueError, KeyError):
            _local = None
        if _local is not None and (
            _local.hour + _local.minute / 60.0 < DEFAULT_P1_ENTRY_LOCAL_HOUR
        ):
            return _blocked(
                blockers=["p1_entry_window_not_open"],
                remaining_budget_before=remaining_budget_before,
                station_policy=resolved.to_dict(),
                extra_plan={
                    "p1_entry_window": {
                        "opens_local_hour": DEFAULT_P1_ENTRY_LOCAL_HOUR,
                        "decision_local_time": _local.strftime("%H:%M"),
                        "charter": 67,
                    }
                },
            )

    # Charter 119: a P1 that does not carry its own evidence may not seat.
    # Same shape as charter 67 above and for the same reason -- an explicit
    # HOLD before the compile, never on the P1 commit path, which persists
    # the canonical observation and the hard bound and must keep flowing.
    #
    # The rule existed since the suspension but had one call site the worker
    # routes P1 away from, so it judged nothing: the seven P1-driven fills of
    # 2026-09-07 were priced by envelopes it refused.  Replayed over every P1
    # that reached this compile since the suspension settled on 09-03 it
    # refuses none of 1948, because charter 67 already holds P1 to the local
    # afternoon and by then one of the three outermost claimants has always
    # spoken.  It is a floor under that coincidence, not a change to it.
    if str(snapshot.evidence.probability_stage or "") == "P1":
        try:
            assert_p1_execution_evidence(envelope.lineage)
        except ContractError as exc:
            return _blocked(
                blockers=[str(exc)],
                remaining_budget_before=remaining_budget_before,
                station_policy=resolved.to_dict(),
                extra_plan={
                    "p1_execution_evidence": {
                        "charter": 119,
                        "refusal": str(exc),
                        "observation_likelihood_mode": str(
                            envelope.lineage.get("observation_likelihood_mode")
                            or ""
                        ),
                        "soft_likelihood_policy": str(
                            envelope.lineage.get(
                                "metar_trajectory_soft_likelihood_policy"
                            ) or ""
                        ),
                        # Which likelihood receipts the envelope carries at
                        # all (charter 150's, and for v11-and-earlier heads
                        # the three retired outermost claimants).  A refusal
                        # that names only itself sends the next reader to
                        # re-derive this by hand.
                        "receipts_present": sorted(
                            key for key in (
                                "p1_observation_likelihood_json",
                                "p1_four_layer_serving_json",
                                "regional_path_prior_json",
                                "metar_ceiling_json",
                            )
                            if str(envelope.lineage.get(key) or "").strip()
                        ),
                    }
                },
            )

    try:
        delta_plan = compile_delta_v2_runtime_plan(
            # Charter 110(六)'s tau, from the one clock both lanes read.
            # Settlement is the end of the event day on the station's own
            # timezone; the completion cutoff is three hours before it.
            hours_to_cutoff=hours_to_settlement_if_known(
                snapshot.event_date,
                as_of_utc=snapshot.quote_observed_at or snapshot.observed_at,
                settlement_timezone=envelope.settlement_timezone,
            ),
            snapshot=snapshot,
            basket_policy=policy,
            delta_policy=resolved_delta_policy,
            fill_resolver=delta_v2_fill_resolver,
            capital_available_usdc=sizing_capital_usdc,
            # Charter 141: the ceiling everything after the seat reads.
            capital_ceiling_usdc=remaining_budget,
            minimum_profit_floor=float(
                policy.minimum_expected_pnl_after_usdc
            ),
            resource_probe_ok=resource_probe_ok,
            previous_probabilities=previous_probabilities,
            chain_consolidation_tilt=chain_consolidation_tilt,
            last_acted_probability_identity=last_acted_probability_identity,
            last_decision_signature=last_decision_signature,
            effective_edge_tables=effective_edge_tables,
            off_centre_claim_bands=off_centre_claim_bands,
            convergence_evidence=convergence_evidence,
            # The P0-M TAF TX bucket the enumerator's TAF arms read.  Off
            # by default (PM_P0M_TAF_MODE); until 2026-09-04 only the paper
            # ledger passed it, so the arm could not have acted live.
            taf_tx_bucket=taf_tx_bucket_for(envelope, str(envelope.stage)),
            # Charter 71: the identity this compile is served under, and the
            # frame the growth gate reads -- since charter 141 the station-
            # day's ceiling share of the whole book, so a chain that spent
            # past the sizing share is not ruined by its own completion.
            chain_growth_frame_usdc=(
                book_equity_usdc * _capital_kernel.STATION_DAY_HARD_FRACTION
                if market_snapshot.get("book_equity_usdc") not in (None, "")
                else None
            ),
            # Charter 72: the whole book, for the Kelly stake.
            book_equity_usdc=(
                book_equity_usdc
                if market_snapshot.get("book_equity_usdc") not in (None, "")
                else None
            ),
            # Newest first: the certainty gate asks whether the mass centre
            # has held still, and the answer is read off the same envelope
            # chain the previous-probabilities comparison already walks.
            prior_centre_buckets=[
                max(item.probabilities, key=lambda b: item.probabilities[b])
                for item in reversed(_earlier)
                if item.probabilities
            ],
            # Charter 136 (四) as charter 141 rewrote it: what a broken seat
            # may spend to complete itself -- the station-day's ceiling.
            completion_budget_usdc=completion_budget_usdc_v1(
                book_equity_usdc=book_equity_usdc,
                stage=str(snapshot.evidence.probability_stage or ""),
                committed_usdc=station_committed_usdc,
            ),
        )
    # The kernel raises its own ContractError class, not this module's: the
    # two are distinct types (``basket_kernel.contracts`` is self-contained
    # by design), so ``except ContractError`` here never matched a kernel
    # raise and the hold codes named below escaped as fail-closed compile
    # failures -- the forward lane recorded market evidence and nothing
    # else, no decision and no fact.  Measured 2026-09-02 07:14Z: 14 forward
    # station-days a cycle (KLGA, ZUUU, WSSS, EFHK, KORD, LEMD, ...) whose
    # every support window the price gates refused -- a HOLD by every rule
    # in this file -- reported as ``blocked_fail_closed``.
    except (ContractError, KernelContractError) as exc:
        # No window can hold a seat is a HOLD, not a crash.  Clause 50 says
        # a ladder that cannot reach its floor should not be bought at all,
        # and once the incumbent lost its waiver (2026-08-16) a station whose
        # every window fails the centre rung or the length floor reaches this
        # path in the ordinary course of a day rather than as a contract
        # breach.  Only these two are absorbed: everything else here is still
        # a real violation and must surface.
        # The certainty gate is a decision, not a breach: the market has
        # finished pricing this day and the plan declines to pay for a
        # consensus it helped form.  Waiting is the action, so it absorbs
        # into HOLD like the two structural refusals beside it.
        if not str(exc).startswith("delta_v2_certainty_gate_lit") and str(
            exc
        ) not in {
            # One verdict per cause (2026-09-08).  All three are the
            # objective answering "nothing to buy here", so all three absorb
            # into a named HOLD; splitting the names changed what the reader
            # learns, not what the day does.
            "delta_v2_support_empty",
            "delta_v2_support_scope_unquoted",
            "delta_v2_support_desired_empty",
            "delta_v2_no_solvable_support_window",
        }:
            raise
        # A hold is a decision, and the ledger records decisions off the
        # plan: the economic decision key and the training fact both read
        # ``plan`` as a mapping (``delta_v2_plan_mapping_required``), and the
        # atomic writer refuses a compile without one.  The entry-window
        # hold above carries a small plan for exactly that reason; without
        # the same here, every hold this branch produced was refused at
        # commit under a name that hid the hold -- measured 2026-09-02
        # 14:10Z, EGLC, LEMD, SAEZ and SBGR, the first cycle their compiles
        # reached this branch instead of dying earlier.
        return _blocked(
            blockers=[str(exc)],
            remaining_budget_before=remaining_budget_before,
            station_policy=resolved.to_dict(),
            window_gate_counts=getattr(exc, "window_gate_counts", None),
            window_failures=getattr(exc, "window_failures", None),
            extra_plan={
                "hold": {
                    "reason": str(exc),
                    "decided_by": "delta_v2_runtime",
                }
            },
        )

    selected_actions_by_bucket: dict[str, dict[str, Any]] = {}
    # The whole action evaluations, not just their actions: a re-score needs
    # the fill distribution, the hard gates and the execution cost that the
    # authorization was computed from.
    selected_action_evaluations: list[dict[str, Any]] = []
    if isinstance(delta_plan.get("delta_v2_action_selection"), Mapping):
        selected_rows = delta_plan["delta_v2_action_selection"].get(
            "selected_actions", ()
        )
        if isinstance(selected_rows, Sequence) and not isinstance(
            selected_rows, (str, bytes, bytearray)
        ):
            for selected in selected_rows:
                if not isinstance(selected, Mapping):
                    continue
                action = selected.get("action")
                if not isinstance(action, Mapping):
                    continue
                bucket = str(action.get("bucket") or "")
                if bucket:
                    selected_actions_by_bucket[bucket] = dict(action)
                    selected_action_evaluations.append(dict(selected))
    selected_authorized = (
        delta_plan.get("state") == "TRADE"
        and dict(delta_plan.get("delta_v2_audit", {})).get("authorized") is True
    )
    if resolved_regime is not None:
        regime_gate = evaluate_regime_gate(
            resolved_regime,
            station_id=envelope.station_id,
            event_date=envelope.event_date,
            stage=envelope.stage,
            checkpoint_id=envelope.checkpoint_id,
            settlement_timezone=envelope.settlement_timezone,
            evaluation_at_utc=evaluation_at_utc,
            probability_sha256=envelope.content_sha256,
            probability_direction=str(delta_plan.get("direction") or ""),
            center_bucket=str(delta_plan.get("center_bucket") or ""),
        )
        # The gate is recorded, never enforced (charter 52c, 2026-08-16).
        # Direction has exactly one source: which side of the centre carries
        # the probability mass and the EV.  Weather state is already inside
        # p(t) -- the radiation shift, the synoptic cooling fallback and the
        # calibrated spread all consumed it -- so a regime layer voting on
        # direction a second time is the double counting clause 43 forbids,
        # applied in the strategy layer where clause 43 forbids it most.
        # It also read the direction it was judging out of the plan it then
        # overruled, which makes it a veto on the objective rather than an
        # independent piece of evidence.  The receipt stays because the
        # regime read is still worth seeing beside a decision -- but it stays
        # in ``regime_audit``, which already carries ``direction_allowed``
        # alongside the state, the reasons and every number behind them.
        #
        # It used to be appended to ``blockers`` as well (user ruling
        # 2026-09-08, "regime 生效了吗").  That made a receipt for something
        # that blocks nothing the second most common blocker in the book --
        # 4664 of them, more than every real refusal but one -- so any count
        # of "why did this day not trade" was wrong by that much.  A blocker
        # list is what refused; a disagreement that does not refuse belongs
        # in the audit and is already there.
    else:
        regime_gate = None

    if cash_phase_decision is None:
        return _blocked(
            blockers=[cash_phase_blocker or "cash_phase_unresolved"],
            station_policy={
                "station_id": resolved.station_id,
                "activation_id": resolved.activation_id,
                "global_prior_id": resolved.global_prior_id,
                "target_artifact_id": resolved.target_artifact_id,
                "reserve_artifact_id": resolved.reserve_artifact_id,
                "cadence_artifact_id": resolved.cadence_artifact_id,
                "fallback_reason": resolved.fallback_reason,
                "policy_sha256": resolved.policy_sha256,
                "authority_scope": resolved.authority_scope,
                "policy_id": policy.policy_id,
                "station_policy_id": resolved.target_artifact_id,
                "cash_phase_observed_at_utc": (
                    normalized_cash_input.observed_at_utc
                ),
            },
            regime_audit=(
                regime_gate.audit
                if regime_gate is not None
                else {
                    "schema": "bayes_basket_regime_gate_v1",
                    "status": "NOT_APPLIED",
                    "required": bool(require_regime_ssot),
                }
            ),
            remaining_budget_before=remaining_budget,
            extra_plan={
                **dict(delta_plan),
                "cash_phase_blocked": True,
                "cash_phase_blocker": cash_phase_blocker,
                "execution_authorized": False,
                "intents_written": 0,
                "reservations_written": 0,
                "live_submit_authorized": False,
                "calls_order_gateway": False,
                "orders_submitted": 0,
            },
        )

    row_by_bucket = {row.bucket: row for row in snapshot.buckets}
    # Charter 136 (四)'s special case -- a risk-budget completion sized past the
    # station remainder -- is gone with charter 141: ``remaining_budget`` is
    # the station-day's ceiling for every order, and the completion budget is
    # that same ceiling.
    # Which family each leg goes out as, read off the plan the optimizer
    # wrote.  Charter 127 lets one basket carry both -- a held ladder whose
    # short rung is completed crosses while an ordinary entry rests -- so
    # "what does this leg pay" is a per-leg question now, not a per-run one.
    execution_candidate_by_bucket = {
        str(row.get("bucket", "")): str(row.get("execution_candidate", "NONE"))
        for row in (
            delta_plan.get("targets", [])
            if isinstance(delta_plan, Mapping) else []
        )
        if isinstance(row, Mapping)
    }

    def _size_every_leg(
        *,
        clamped_clip_authorized: bool,
    ) -> tuple[list[OrderSizingDecision], float]:
        decisions: list[OrderSizingDecision] = []
        remaining = remaining_budget
        rows = (
            delta_plan.get("targets", [])
            if isinstance(delta_plan, Mapping)
            else []
        )
        for row in rows:
            row_bucket = str(row.get("bucket", ""))
            snap_row = row_by_bucket[row_bucket]
            candidate_price = float(row.get("candidate_price", 0.0) or 0.0)
            delta_shares = float(row.get("delta_shares", 0.0) or 0.0)
            clip_notional = row.get("clip_notional_usdc")
            execution_candidate = str(row.get("execution_candidate", "NONE"))
            is_selected = (
                selected_authorized
                and execution_candidate != "NONE"
                and row_bucket in selected_actions_by_bucket
            )
            selected_action = selected_actions_by_bucket.get(row_bucket, {})
            candidate_present = (
                is_selected
                and float(selected_action.get("cash_clip") or 0.0) > 0.0
            )
            executable_depth_cash = (
                _executable_depth_cash(
                    ask_levels=snap_row.ask_levels,
                    execution_candidate=execution_candidate,
                    limit_price=candidate_price,
                    cash_clip=float(selected_action.get("cash_clip") or 0.0),
                )
                if candidate_present
                else 0.0
            )
            sizing = OrderSizingDecision.compute(
                bucket=row_bucket,
                phase_decision=cash_phase_decision,
                delta_shares=delta_shares,
                r_limit=candidate_price,
                remaining_budget=remaining,
                executable_depth_cash=executable_depth_cash,
                # Charter 98: only a Taker's depth term is a market bound.
                # The Maker branch of ``_executable_depth_cash`` returns the
                # leg's own clip so the term is present and auditable; saying
                # so here keeps the lot raise from being refused by the clip
                # the raise is allowed to exceed.
                depth_is_market_bound=(
                    str(execution_candidate) == "TAKER_FOK"
                ),
                candidate_present=candidate_present,
                target_clip_notional_usdc=(
                    float(clip_notional)
                    if (candidate_present and clip_notional is not None)
                    else None
                ),
                minimum_clip_notional_usdc=float(
                    policy.clip_min_notional_usdc
                ),
                # The policy floor is cash; this one is shares, and it is the
                # exchange's rule rather than ours.  Both have to hold: a leg
                # can clear the notional floor and still be under a lot.
                #
                # Asserted on every priced leg, MAKER and TAKER_FOK alike.
                # It was MAKER-only while the FOK family was unreachable in
                # production and the only lot rejections the exchange had
                # returned were GTC; charter 65 made FOK the production path
                # on 2026-08-31, and the gateway's own sizing rule
                # (``exchangeOrderSizingRule``) refuses any order below the
                # market's ``orderMinSize`` regardless of type.  Passing 0
                # here for a FOK leg only deferred that refusal to the
                # exchange boundary and hid it from the sizing record; the
                # lot is the same five shares either way.  A NONE candidate
                # has no order to size and keeps 0.
                minimum_lot_shares=(
                    execution_minimum_lot_shares(
                        min_order_size=snap_row.min_order_size,
                        limit_price=candidate_price,
                    )
                    if execution_candidate in ("MAKER", "TAKER_FOK")
                    else 0.0
                ),
                # DeltaBasketScore v2 owns the selected adaptive clip.  The
                # legacy phase cash template remains auditable cadence
                # context, but may not silently replace the unique
                # objective's size.
                cash_template_caps_target=False,
                share_quantum=float(
                    market_snapshot.get("share_quantum", 0.01) or 0.01
                ),
                clamped_clip_authorized=clamped_clip_authorized,
            )
            decisions.append(sizing)
            remaining = sizing.remaining_budget_after
        return decisions, remaining

    order_sizing_decisions, remaining_after = _size_every_leg(
        clamped_clip_authorized=False
    )
    # Depth, budget or a shrunken gap can cut a leg below the clip the
    # objective authorized.  The cut basket is not a smaller version of the
    # authorized one -- floor, CVaR, concentration and coverage are joint
    # over the legs -- so it is neither sent unexamined nor refused on sight:
    # the sizes that would actually go out are put back through the identical
    # evaluation that granted the authorization, and the same gates rule.
    bundle_rescore = _rescore_clamped_bundle(
        sizing_decisions=order_sizing_decisions,
        resize=_size_every_leg,
        delta_plan=delta_plan,
        snapshot=snapshot,
        delta_policy=resolved_delta_policy,
        policy=policy,
        capital_available_usdc=remaining_budget,
        selected_action_evaluations=selected_action_evaluations,
    )
    if bundle_rescore is not None and bundle_rescore.get("authorized") is True:
        order_sizing_decisions, remaining_after = _size_every_leg(
            clamped_clip_authorized=True
        )
    elif bundle_rescore is not None:
        # A refused re-score has to stop the basket, and for a CUT basket it
        # already does: the legs the clip cut are sitting ineligible with
        # ``sizing_clamped_below_authorized_clip`` and nothing can be sent.
        # A RAISED basket is the opposite shape -- every leg is eligible and
        # priced, the raise succeeded, and the only thing wrong with it is
        # that the basket now going out is not the basket that was judged.
        # So the refusal has to be applied rather than assumed.
        order_sizing_decisions = [
            item.refused(RESHAPED_BUNDLE_REFUSED_REASON)
            if item.candidate_present else item
            for item in order_sizing_decisions
        ]
        remaining_after = remaining_budget

    # The odds structure of the basket that is actually bought, and the
    # target it has to clear (user ruling 2026-09-09: "目标设置为5%",
    # "把闸也上了").
    #
    # On the FINAL share vector, because that is the only vector this
    # question has an answer for.  ``n_k`` shares of bucket k at an all-in
    # premium C return ``n_k - C`` when the day settles there, so the
    # basket's gross multiple in that scenario is ``n_k / C`` and its net
    # expectation is ``sum p_k n_k - C``.  Only an equal basket collapses
    # that to ``P - Q`` on one share of each, and charter 70/83's lot raise
    # leaves 96% of seated baskets unequal.
    #
    # Measured 2026-09-09 over 235 seated baskets: nine pass the equal-share
    # reading and lose money on the one that was bought, MPMG 2026-09-09
    # worst at -7.0% against an equal-share Q of 0.530 that clears the
    # target comfortably.  Twenty-seven (11%) carry a negative net ROI.
    basket_return_structure: dict[str, Any] | None = None
    priced_legs = [
        item for item in order_sizing_decisions
        if item.candidate_present and item.eligible and item.q_order > 0.0
    ]
    edge_belief = (
        (delta_plan.get("basket_support_selection") or {}).get("seated") or {}
    ).get("window_probabilities") if isinstance(delta_plan, Mapping) else None
    # Which price this test judges on (2026-09-11).
    #
    # It read ``r_limit`` -- the price the order is SENT at -- and for a
    # resting rung that is bid+1t, the price this lane hopes to fill at.
    # Every other gate in the chain judges on the offer: the seat's edge
    # gate, charter 68's lambda, charter 72's Kelly stake and charters
    # 93/123's post-lift re-reads all price the basket at what the market
    # will sell it for right now.  That is the 2026-08-27 ruling and it has
    # three same-entry rebuilds behind it (+50.7 / +55.5 / +62.8 USDC), whose
    # decomposition is the reason: half the maker losses were bets the lane
    # price flattered into passing, and the fills that do arrive at the
    # hoped-for price arrive because the market knew better -- of legs that
    # settled winners 18% filled, of legs that settled losers 88% did.
    #
    # So this gate was the one place the retired basis survived.  Measured on
    # the 134 seats of the 08-28..09-08 book that carry a two-sided quote:
    # the rung costs 0.933 of the offer at the median, the target admits
    # 51.5% of seats priced at the rung against 35.1% priced at the offer,
    # and the 22 baskets (one in six) the two answers disagree about read
    # -4.0% at the offer and +11.7% at the rung.  Those 22 are the class the
    # ruling names outright.
    #
    # A crossing leg is not affected and must not be: charter 127 makes the
    # designated completion take, so its ``r_limit`` IS the offer it walks --
    # the admission price and the payment price are one number by
    # construction, which is what the whole basis argument is trying to buy.
    admission_price_by_bucket = {
        str(item.bucket): roi_gate_admission_price_v1(
            execution_candidate=execution_candidate_by_bucket.get(
                str(item.bucket)
            ),
            limit_price=item.r_limit,
            ask_price=(
                float(row_by_bucket[str(item.bucket)].ask_price)
                if str(item.bucket) in row_by_bucket
                and row_by_bucket[str(item.bucket)].ask_available
                else None
            ),
        )
        for item in priced_legs
    }
    if priced_legs and isinstance(edge_belief, Mapping):
        support = tuple(str(item.bucket) for item in priced_legs)
        try:
            basket_return_structure = basket_return_structure_v1(
                shares_by_bucket={
                    str(item.bucket): item.q_order for item in priced_legs
                },
                price_by_bucket={
                    str(item.bucket): admission_price_by_bucket[str(item.bucket)]
                    for item in priced_legs
                },
                claim_by_bucket={
                    bucket: float(edge_belief.get(bucket, 0.0) or 0.0)
                    for bucket in support
                },
                support=support,
                fee_rate=float(policy.fee_rate),
            )
        except (ContractError, KernelContractError):
            # A structure that cannot be computed is not a pass.  The gate
            # below reads ``meets_target``, which stays False.
            basket_return_structure = None
        # Charter 116 is not overruled here.  A completion that makes an
        # illegal floor legal is a repair, and an expectation test is the
        # wrong question to put to a repair -- the missing leg is missing
        # precisely because ``claim < ask``, so a repair prices negative by
        # construction and a net-ROI target would refuse every one of them.
        # The repair still records both currencies (charter 116's own
        # accounting); it just is not asked to clear a profit target.
        repair_exempt = bool(
            (delta_plan.get("completion_repair") or {}).get("exempt")
            if isinstance(delta_plan, Mapping) else False
        )
        if not repair_exempt and not (
            basket_return_structure or {}
        ).get("meets_target"):
            order_sizing_decisions = [
                item.refused(BASKET_NET_ROI_REFUSED_REASON)
                if item.candidate_present else item
                for item in order_sizing_decisions
            ]
            remaining_after = remaining_budget

    candidate_present_for_cadence = any(
        item.candidate_present for item in order_sizing_decisions
    )
    selected_sizing = [
        item for item in order_sizing_decisions if item.candidate_present
    ]
    required_gap_support = set(
        str(value) for value in delta_plan.get("execution_gap_support", ())
    )
    sized_selected_buckets = {item.bucket for item in selected_sizing}
    complete_bundle_sized = (
        bool(required_gap_support)
        and sized_selected_buckets == required_gap_support
    )
    sizing_eligible_for_cadence = (
        complete_bundle_sized
        and all(item.eligible for item in selected_sizing)
    )
    sizing_blockers = list(dict.fromkeys(
        item.reason_code
        for item in order_sizing_decisions
        if item.candidate_present and not item.eligible
    ))
    blockers.extend(
        reason for reason in sizing_blockers if reason not in blockers
    )
    if bundle_rescore is not None and bundle_rescore.get("authorized") is not True:
        # The refusing sizing above is what actually blocks; these name the
        # reason the cut basket could not be re-authorized.
        for reason in [
            "bundle_rescore_refused",
            *(str(value) for value in bundle_rescore.get("blockers", ())),
        ]:
            if reason not in blockers:
                blockers.append(reason)
    effective_cadence_snapshot = (
        cadence_snapshot if cadence_snapshot is not None else market_snapshot.get("cadence_snapshot")
    )
    effective_previous_cadence_snapshot = (
        previous_cadence_snapshot if previous_cadence_snapshot is not None else market_snapshot.get("previous_cadence_snapshot")
    )
    selected_cash = sum(
        float(action.get("cash_clip") or 0.0)
        for action in selected_actions_by_bucket.values()
    )
    budget_passed = selected_cash <= remaining_budget + 1e-9 and selected_cash > 0.0
    if delta_v2_owns_cadence:
        cadence_decision = (
            CadenceDecision.ALLOW_CANDIDATE.value
            if (
                selected_authorized
                and sizing_eligible_for_cadence
                and budget_passed
                and snapshot.order_state_known
                and candidate_present_for_cadence
            )
            else CadenceDecision.HOLD.value
        )
        cadence_authority = "DELTA_BASKET_SCORE_V2"
    else:
        cadence_decision = evaluate_no_live_cadence(
            cadence_theta=resolved.cadence_theta,
            current_snapshot=effective_cadence_snapshot,
            previous_snapshot=effective_previous_cadence_snapshot,
            b13_b14_passed=selected_authorized and sizing_eligible_for_cadence,
            budget_passed=budget_passed,
            order_state_known=snapshot.order_state_known,
            candidate_present=candidate_present_for_cadence,
        )
        cadence_authority = "LEGACY_CADENCE_ARTIFACT"
    cadence_allows = (
        sizing_eligible_for_cadence
        and cadence_decision == CadenceDecision.ALLOW_CANDIDATE.value
    )

    status = "ready" if selected_authorized and cadence_allows else "hold"
    if not selected_authorized:
        blockers.extend(
            value
            for value in dict(delta_plan.get("delta_v2_audit", {})).get("blockers", [])
            if str(value) not in blockers
        )
    if not blockers and not cadence_allows:
        blockers.append("cadence_gate_blocked")

    resolved_station_policy = {
        "station_id": resolved.station_id,
        "activation_id": resolved.activation_id,
        "global_prior_id": resolved.global_prior_id,
        "target_artifact_id": resolved.target_artifact_id,
        "reserve_artifact_id": resolved.reserve_artifact_id,
        "cadence_artifact_id": resolved.cadence_artifact_id,
        "fallback_reason": resolved.fallback_reason,
        "policy_sha256": resolved.policy_sha256,
        "authority_scope": resolved.authority_scope,
        "policy_id": policy.policy_id,
        "station_policy_id": resolved.target_artifact_id,
        "cash_phase_observed_at_utc": normalized_cash_input.observed_at_utc,
    }
    plan = dict(delta_plan)
    plan.update(
        {
            "target_only": True,
            "settlement_timezone": envelope.settlement_timezone,
            "execution_mode": "production_no_live",
            "live_order_authorized": False,
            "places_orders": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "delta_v2_audit": dict(dict(delta_plan.get("delta_v2_audit", {}))),
            "lineage": {
                **(dict(delta_plan.get("lineage", {})) if isinstance(delta_plan.get("lineage"), Mapping) else {}),
                # Preserve the complete probability lineage, not only the
                # optimizer's economic references.  The M5 atomic writer
                # independently binds the selected P1 plan to its canonical
                # METAR/SPECI observation and hard-bound identities.
                **dict(envelope.lineage),
                "probability_envelope_sha256": envelope.content_sha256,
                "posterior_sha256": envelope.posterior_sha256,
                "probability_stage": envelope.stage,
                "probability_decision_at_utc": envelope.decision_at_utc,
                "settlement_timezone": envelope.settlement_timezone,
                "policy_resolution_sha256": resolved.policy_sha256,
                "policy_activation_id": resolved.activation_id,
                "global_prior_id": resolved.global_prior_id,
                "quote_version": snapshot.quote_version,
                "inventory_version": snapshot.inventory_version,
                "order_state_version": snapshot.order_state_version,
                "cash_phase_policy_version": cash_phase_decision.policy_version,
                "cash_phase_policy_sha256": cash_phase_decision.policy_sha256,
                "cash_phase_decision_sha256": content_sha256(cash_phase_decision.to_dict()),
                "cash_phase_observed_at_utc": normalized_cash_input.observed_at_utc,
                "cash_phase": cash_phase_decision.next_phase.value,
                "cash_phase_checkpoint_id": cash_phase_decision.checkpoint_id,
                "policy_authority_scope": resolved.authority_scope,
                "cadence_artifact_key": CADENCE_GATE_SECONDS_KEY,
                "cadence_decision": cadence_decision,
            },
        }
    )
    return {
        "schema": "bayes_basket_target_plan_v1",
        "status": status,
        "reason_codes": list(dict.fromkeys(blockers)),
        "stage": envelope.stage,
        "probability_envelope": envelope.to_dict(),
        "resolved_station_policy": resolved_station_policy,
        "plan": plan,
        "cash_phase_decision": cash_phase_decision.to_dict(),
        "order_sizing_decisions": [item.to_dict() for item in order_sizing_decisions],
        # Recorded whether it passed or not: a refusal that does not say what
        # it read is not auditable, and the coverage/net-profit gap is the
        # number this exists to surface.
        "basket_return_structure": basket_return_structure,
        # Present only when sizing cut a leg below its authorized clip; the
        # verdict that decided whether the cut basket could still be sent.
        "bundle_rescore": bundle_rescore,
        "cadence_decision": cadence_decision,
        "cadence_authority": cadence_authority,
        "cadence_allows_candidate": cadence_allows,
        "remaining_budget_usdc_before": remaining_budget,
        "remaining_budget_usdc_after": remaining_after,
        # Both sides of the cap, so a reader can tell a station held back by
        # its own bankroll from one held back by an empty book.
        "station_capital_usdc": station_capital_usdc,
        # Charter 141: the ceiling orders were sized inside, the book it was
        # read off, and the whole-book room when the account was read.
        "station_ceiling_usdc": station_ceiling_usdc,
        "book_equity_usdc": book_equity_usdc,
        "portfolio_room_usdc": portfolio_room_usdc,
        "capital_layer_rule": CAPITAL_LAYER_RULE_VERSION,
        "capital_layer": capital_layer,
        "station_committed_usdc": station_committed_usdc,
        "pool_remaining_usdc": pool_remaining_usdc,
        "station_reserve_violations": [],
        "delta_v2_audit": dict(delta_plan.get("delta_v2_audit", {})),
        "boundary": {
            "target_only": True,
            "execution_mode": "production_no_live",
            "live_order_authorized": False,
            "places_orders": False,
            "orders_submitted": 0,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "cadence_decision": cadence_decision,
            "cadence_allows_candidate": cadence_allows,
            "cadence_order_authorized": False,
        },
        "regime_audit": (
            regime_gate.audit
            if regime_gate is not None
            else {
                "schema": "bayes_basket_regime_gate_v1",
                "status": "NOT_APPLIED",
                "required": bool(require_regime_ssot),
            }
        ),
    }


class DeltaBasketScoreV2Optimizer:
    """The sole production economic-decision boundary."""

    schema = DELTA_V2_OPTIMIZER_SCHEMA
    optimizer_id = DELTA_V2_OPTIMIZER_ID
    version = "v2"

    def compile(
        self,
        *,
        station_policy_stage: str | None = None,
        probability_envelopes: Sequence[ProbabilityEnvelope | Mapping[str, Any]],
        market_snapshot: Mapping[str, Any],
        policy_activations: Sequence[StationBasketPolicyActivation | Mapping[str, Any]],
        global_priors: Mapping[str, GlobalBasketPriorArtifact] | Sequence[GlobalBasketPriorArtifact],
        policy_artifacts: Mapping[str, PolicyArtifact] | Sequence[PolicyArtifact],
        evaluation_at_utc: str,
        season_bin: str,
        maturity_bin: str,
        liquidity_bin: str,
        strategy_version: str,
        base_policy: Mapping[str, Any] | None = None,
        delta_v2_policy: DeltaV2PolicyArtifact | Mapping[str, Any] | None = None,
        delta_v2_fill_resolver: Callable[..., Mapping[str, Any]] | None = None,
        cash_phase_policy: BasketCashPhasePolicyArtifact | Mapping[str, Any] | None = None,
        cash_phase_input: BasketCashPhaseInput | Mapping[str, Any] | None = None,
        cadence_snapshot: CadenceSnapshot | Mapping[str, Any] | None = None,
        previous_cadence_snapshot: CadenceSnapshot | Mapping[str, Any] | None = None,
        delta_v2_owns_cadence: bool = False,
        resource_probe_ok: bool = False,
        policy_authority_scope: str = "FORMAL_PROMOTED",
        regime_artifact: RegimeArtifact | Mapping[str, Any] | None = None,
        require_regime_ssot: bool = True,
        expected_source_lane: str | None = None,
    chain_consolidation_tilt: float | None = None,
    last_acted_probability_identity: str | None = None,
    last_decision_signature: Mapping[str, Any] | None = None,
    effective_edge_tables: Mapping[str, Any] | None = None,
    off_centre_claim_bands: Mapping[str, Any] | None = None,
    convergence_evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = _compile_target_plan(
            station_policy_stage=station_policy_stage,
            probability_envelopes=probability_envelopes,
            market_snapshot=market_snapshot,
            policy_activations=policy_activations,
            global_priors=global_priors,
            policy_artifacts=policy_artifacts,
            evaluation_at_utc=evaluation_at_utc,
            season_bin=season_bin,
            maturity_bin=maturity_bin,
            liquidity_bin=liquidity_bin,
            strategy_version=strategy_version,
            base_policy=base_policy,
            delta_v2_policy=delta_v2_policy,
            delta_v2_fill_resolver=delta_v2_fill_resolver,
            cash_phase_policy=cash_phase_policy,
            cash_phase_input=cash_phase_input,
            cadence_snapshot=cadence_snapshot,
            previous_cadence_snapshot=previous_cadence_snapshot,
            delta_v2_owns_cadence=delta_v2_owns_cadence,
            resource_probe_ok=resource_probe_ok,
            policy_authority_scope=policy_authority_scope,
            regime_artifact=regime_artifact,
            require_regime_ssot=require_regime_ssot,
            expected_source_lane=expected_source_lane,
            chain_consolidation_tilt=chain_consolidation_tilt,
            last_acted_probability_identity=last_acted_probability_identity,
            last_decision_signature=last_decision_signature,
            effective_edge_tables=effective_edge_tables,
            off_centre_claim_bands=off_centre_claim_bands,
            convergence_evidence=convergence_evidence,
        )
        boundary = dict(result.get("boundary") or {})
        boundary.update({
            "economic_decision_point": self.optimizer_id,
            "economic_decision_point_schema": self.schema,
            "all_weather_and_market_inputs_jointly_solved": True,
        })
        result["boundary"] = boundary
        result["optimizer"] = {
            "schema": self.schema,
            "optimizer_id": self.optimizer_id,
            "version": self.version,
            "input_contract": (
                "materialized_probability_envelope + official_clob_ws_snapshot + "
                "resolved_station_policy + materialized_regime_artifact + "
                "versioned_cash_phase + promoted_delta_v2_policy + "
                "promoted_fill_distribution"
            ),
            "side_effects": [],
        }
        return result


def compile_target_plan(
    *,
    station_policy_stage: str | None = None,
    probability_envelopes: Sequence[ProbabilityEnvelope | Mapping[str, Any]],
    market_snapshot: Mapping[str, Any],
    policy_activations: Sequence[StationBasketPolicyActivation | Mapping[str, Any]],
    global_priors: Mapping[str, GlobalBasketPriorArtifact] | Sequence[GlobalBasketPriorArtifact],
    policy_artifacts: Mapping[str, PolicyArtifact] | Sequence[PolicyArtifact],
    evaluation_at_utc: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    strategy_version: str,
    base_policy: Mapping[str, Any] | None = None,
    delta_v2_policy: DeltaV2PolicyArtifact | Mapping[str, Any] | None = None,
    delta_v2_fill_resolver: Callable[..., Mapping[str, Any]] | None = None,
    cash_phase_policy: BasketCashPhasePolicyArtifact | Mapping[str, Any] | None = None,
    cash_phase_input: BasketCashPhaseInput | Mapping[str, Any] | None = None,
    cadence_snapshot: CadenceSnapshot | Mapping[str, Any] | None = None,
    previous_cadence_snapshot: CadenceSnapshot | Mapping[str, Any] | None = None,
    delta_v2_owns_cadence: bool = False,
    resource_probe_ok: bool = False,
    policy_authority_scope: str = "FORMAL_PROMOTED",
    regime_artifact: RegimeArtifact | Mapping[str, Any] | None = None,
    require_regime_ssot: bool = True,
    expected_source_lane: str | None = None,
    chain_consolidation_tilt: float | None = None,
    last_acted_probability_identity: str | None = None,
    last_decision_signature: Mapping[str, Any] | None = None,
    effective_edge_tables: Mapping[str, Any] | None = None,
    off_centre_claim_bands: Mapping[str, Any] | None = None,
    convergence_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compatibility-free public facade for the one optimizer boundary."""

    return DeltaBasketScoreV2Optimizer().compile(
        station_policy_stage=station_policy_stage,
        chain_consolidation_tilt=chain_consolidation_tilt,
        last_acted_probability_identity=last_acted_probability_identity,
            last_decision_signature=last_decision_signature,
            effective_edge_tables=effective_edge_tables,
            off_centre_claim_bands=off_centre_claim_bands,
            convergence_evidence=convergence_evidence,
        probability_envelopes=probability_envelopes,
        market_snapshot=market_snapshot,
        policy_activations=policy_activations,
        global_priors=global_priors,
        policy_artifacts=policy_artifacts,
        evaluation_at_utc=evaluation_at_utc,
        season_bin=season_bin,
        maturity_bin=maturity_bin,
        liquidity_bin=liquidity_bin,
        strategy_version=strategy_version,
        base_policy=base_policy,
        delta_v2_policy=delta_v2_policy,
        delta_v2_fill_resolver=delta_v2_fill_resolver,
        cash_phase_policy=cash_phase_policy,
        cash_phase_input=cash_phase_input,
        cadence_snapshot=cadence_snapshot,
        previous_cadence_snapshot=previous_cadence_snapshot,
        delta_v2_owns_cadence=delta_v2_owns_cadence,
        resource_probe_ok=resource_probe_ok,
        policy_authority_scope=policy_authority_scope,
        regime_artifact=regime_artifact,
        require_regime_ssot=require_regime_ssot,
        expected_source_lane=expected_source_lane,
    )


__all__ = [
    "DELTA_V2_OPTIMIZER_ID",
    "DELTA_V2_OPTIMIZER_SCHEMA",
    "DeltaBasketScoreV2Optimizer",
    "compile_target_plan",
    "evaluate_no_live_cadence",
    "snapshot_from_probability_envelope",
]
