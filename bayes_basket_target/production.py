"""Production decision path with live submission deliberately disabled.

The target compiler is now exposed through the production vocabulary.  The
decision path consumes the same materialized P0/P0-M/P1, station-policy and
official-CLOB contracts used by the eventual executor, but this deployment
mode stops at a durable, auditable order *candidate*: it never calls a
gateway, writes an order/outbox/intent row, or treats a dry-run result as a
live authorization.

This is a production dry-run, not a second strategy and not a source-wallet
simulation.  There is one compiler and one set of contracts; the only
deployment switch is the final live-submit authority, which is hard-coded to
false in this build.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from typing import Any, Callable, Mapping, Sequence

from .atomic_ledger import (
    build_target_atomic_bundle,
    commit_target_atomic_bundle,
    supersede_prepared_cycle_and_commit_bundle,
)
from .cash_phase import (
    BasketCashPhaseInput,
    BasketCashPhasePolicyArtifact,
    evaluate_cash_phase,
)
from .basket_kernel.state_machine import basket_lifecycle_projection
from .policy import (
    GlobalBasketPriorArtifact,
    PolicyArtifact,
    StationBasketPolicyActivation,
    resolve_station_policy,
)
from .contracts import ProbabilityEnvelope, ContractError, content_sha256
from .delta_v2_artifact import DeltaV2PolicyArtifact
from .order_submission import submit_prepared_intents
from .stage_evidence import (
    assert_intent_buckets_within_bound,
    build_model_envelope_soft_bound,
    build_stage_evidence_root,
    persist_stage_evidence,
)
from .delta_v2_training import delta_v2_decision_scope_key
from .delta_v2_training import build_delta_v2_training_fact
from .delta_v2_training import committed_scope_fact_id
from .regime_artifact import RegimeArtifact
from .runtime import DeltaBasketScoreV2Optimizer
from .execution_deadline import (
    TargetExecutionDeadlineError,
    TargetExecutionHardDeadline,
    derive_target_execution_hard_deadline,
)
from .execution_version_vector import (
    DELTA_V2_ECONOMIC_POLICY_SCHEMA,
    SCHEMA as EXECUTION_VERSION_VECTOR_SCHEMA,
    ExecutionVersionVectorError,
    build_execution_version_vector,
)
from .settlement_time import assert_probability_timezone_matches_market
from .stages import select_current_stage


PRODUCTION_PLAN_SCHEMA = "bayes_basket_production_plan_v1"
# Live since the 2026-08-30 migration; kept identical to
# production_worker.PRODUCTION_EXECUTION_MODE, which a test asserts, because a
# split here would have the worker and the submission path each believing a
# different thing about what they are allowed to do.
PRODUCTION_EXECUTION_MODE = "production_live"
# Charter 82 (2026-09-05): the model-envelope soft bound beside a P0/P0-M
# open is an audit fact, not a gate.  It was the last place the "model
# envelope as an admissibility boundary" idea survived after charters 42, 45,
# 56 and 76 retired it from every other layer, and it never had a clause of
# its own.  Under charters 50/59/79 a legal opening window is 2-3 rungs wide
# while 71% of P0 envelopes (08-28..09-06, 450 station-days) span 1-2 rungs,
# so it refused every legal window on those days regardless of edge.  ``gate``
# is the retired arm, kept so the audit can price it (charter 80 (8)).
ENVELOPE_SOFT_BOUND_MODE = (
    os.environ.get("PM_CHARTER_82_ENVELOPE_SOFT_BOUND", "").strip().lower()
    or "audit"
)
if ENVELOPE_SOFT_BOUND_MODE not in {"audit", "gate"}:
    raise RuntimeError(
        f"unknown PM_CHARTER_82_ENVELOPE_SOFT_BOUND: {ENVELOPE_SOFT_BOUND_MODE}"
    )


def _apply_envelope_soft_bound(
    stage_soft_bound: Mapping[str, Any] | None,
    intents: Sequence[Mapping[str, Any]],
) -> None:
    """Charter 82: refuse on the soft bound only under the retired ``gate`` arm."""

    if ENVELOPE_SOFT_BOUND_MODE != "gate" or stage_soft_bound is None:
        return
    assert_intent_buckets_within_bound(
        bound=stage_soft_bound,
        intent_buckets=[str(item["bucket"]) for item in intents],
    )
STAGE_ZERO_EXECUTION_ANCHOR_SCHEMA = (
    "basket_target_stage_zero_execution_anchor_v1"
)


def _normalized_utc(name: str, value: Any) -> tuple[str, datetime]:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    normalized = parsed.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z"), normalized


def _required_sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _build_forward_evidence_binding(
    *,
    plan: Mapping[str, Any],
    lineage: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    production_inputs: Mapping[str, Any],
    delta_v2_audit: Mapping[str, Any],
    decision_at_utc: str,
) -> dict[str, Any]:
    """Bind one complete no-live evaluation to its independent source clocks.

    The cycle decision time is not copied into the source artifacts.  Instead,
    every selected source keeps its own timestamp and must satisfy strict
    as-of ordering against the atomic decision time.
    """

    decision_at, decision_dt = _normalized_utc(
        "forward_decision_at_utc", decision_at_utc
    )
    probability_at, probability_dt = _normalized_utc(
        "forward_probability_decision_at_utc",
        lineage.get("probability_decision_at_utc"),
    )
    quote_at, quote_dt = _normalized_utc(
        "forward_quote_observed_at_utc",
        market_snapshot.get("quote_observed_at"),
    )
    quote_expires_at, quote_expires_dt = _normalized_utc(
        "forward_quote_expires_at_utc",
        market_snapshot.get("quote_expires_at"),
    )
    cash_at, cash_dt = _normalized_utc(
        "forward_cash_observed_at_utc",
        lineage.get("cash_phase_observed_at_utc"),
    )
    if probability_dt > decision_dt:
        raise ContractError("forward_probability_after_decision")
    if quote_dt > decision_dt:
        raise ContractError("forward_quote_after_decision")
    if decision_dt >= quote_expires_dt:
        raise ContractError("forward_quote_expired_at_decision")
    if cash_dt > decision_dt:
        raise ContractError("forward_cash_after_decision")
    source = str(market_snapshot.get("quote_source") or "").strip().upper()
    if source != "OFFICIAL_CLOB_WS":
        raise ContractError("forward_quote_source_not_official_clob_ws")
    stage = str(lineage.get("probability_stage") or "").strip().upper()
    if stage not in {"P0", "P0-M", "P1"}:
        raise ContractError("forward_probability_stage_invalid")
    phase = str(lineage.get("cash_phase") or "").strip().upper()
    if phase not in {"HEAVY", "STANDARD", "TAIL"}:
        raise ContractError("forward_cash_phase_invalid")
    buckets = market_snapshot.get("buckets")
    if (
        isinstance(buckets, (str, bytes, bytearray))
        or not isinstance(buckets, Sequence)
        or not buckets
    ):
        raise ContractError("forward_ws_quote_scope_missing")
    binding = {
        "schema": "bayes_basket_target_forward_evidence_binding_v1",
        "station_id": str(plan.get("station_id") or "").strip(),
        "event_date": str(plan.get("event_date") or "").strip(),
        "settlement_timezone": str(
            market_snapshot.get("settlement_timezone") or ""
        ).strip(),
        "decision_at_utc": decision_at,
        "probability_stage": stage,
        "probability_decision_at_utc": probability_at,
        "probability_artifact_sha256": _required_sha(
            "forward_probability_artifact_sha256",
            lineage.get("probability_envelope_sha256"),
        ),
        "probability_lineage_sha256": _required_sha(
            "forward_probability_lineage_sha256",
            lineage.get("posterior_sha256"),
        ),
        "ws_quote_observed_at_utc": quote_at,
        "ws_quote_expires_at_utc": quote_expires_at,
        "ws_snapshot_sha256": _required_sha(
            "forward_ws_snapshot_sha256",
            market_snapshot.get("official_ws_quote_sha256")
            or market_snapshot.get("content_sha256"),
        ),
        "ws_quote_count": len(buckets),
        "inventory_sha256": _required_sha(
            "forward_inventory_sha256",
            market_snapshot.get("inventory_sha256"),
        ),
        "order_state_sha256": _required_sha(
            "forward_order_state_sha256",
            market_snapshot.get("order_state_sha256"),
        ),
        "cash_phase": phase,
        "cash_checkpoint_id": str(
            lineage.get("cash_phase_checkpoint_id") or ""
        ).strip(),
        "cash_observed_at_utc": cash_at,
        "cash_decision_sha256": _required_sha(
            "forward_cash_decision_sha256",
            lineage.get("cash_phase_decision_sha256"),
        ),
        "cash_policy_sha256": _required_sha(
            "forward_cash_policy_sha256",
            lineage.get("cash_phase_policy_sha256"),
        ),
        "station_policy_sha256": _required_sha(
            "forward_station_policy_sha256",
            lineage.get("policy_resolution_sha256"),
        ),
        "delta_v2_artifact_sha256": _required_sha(
            "forward_delta_v2_artifact_sha256",
            production_inputs.get("delta_v2_artifact_sha256"),
        ),
        "delta_v2_evaluation_sha256": _required_sha(
            "forward_delta_v2_evaluation_sha256",
            delta_v2_audit.get("content_sha256"),
        ),
        "quote_source": "OFFICIAL_CLOB_WS",
    }
    if not binding["station_id"] or not binding["event_date"]:
        raise ContractError("forward_scope_incomplete")
    if not binding["settlement_timezone"]:
        raise ContractError("forward_settlement_timezone_missing")
    if not binding["cash_checkpoint_id"]:
        raise ContractError("forward_cash_checkpoint_id_missing")
    binding["content_sha256"] = content_sha256(binding)
    return binding


def _bucket_ordinals_from_lineage(
    lineage: Mapping[str, Any],
) -> dict[str, int]:
    """Order the buckets by the temperature interval each one covers.

    The envelope's ordinals are this same order, so deriving it here lets a
    stage-rooted bound be checked against the plan alone.  An open edge bucket
    sorts by its finite side, which is what makes ``31C or below`` the lowest
    and ``41C or higher`` the highest rather than ties at infinity.
    """

    try:
        intervals = json.loads(
            str(lineage.get("bucket_intervals_c_json") or "{}")
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "production_stage_bucket_intervals_invalid"
        ) from exc
    if not isinstance(intervals, Mapping) or not intervals:
        raise ContractError("production_stage_bucket_intervals_missing")

    def edge(item: tuple[str, Any]) -> tuple[float, float]:
        bounds = item[1]
        if not isinstance(bounds, Sequence) or len(bounds) != 2:
            raise ContractError("production_stage_bucket_intervals_invalid")
        lower, upper = bounds
        return (
            float("-inf") if lower is None else float(lower),
            float("inf") if upper is None else float(upper),
        )

    return {
        str(bucket): index
        for index, (bucket, _bounds) in enumerate(
            sorted(intervals.items(), key=edge)
        )
    }


def _default_order_transport(path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """The one local gateway, reached the one approved way.

    Only ever selected when the plan carries live authority; a no-live plan
    keeps ``_no_transport`` and cannot reach the network.  The client refuses
    any base URL that is not loopback HTTP, so this cannot be repointed at an
    exchange directly.
    """

    import sys
    from pathlib import Path as _Path
    root = str(_Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from clob_order_service_client import ClobOrderServiceClient  # noqa: PLC0415

    client = ClobOrderServiceClient(timeout_seconds=15.0)
    if path != "/place-order":
        raise ContractError("production_order_transport_unknown_path")
    return client.place_order(payload)


def _no_transport(path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Refuse to reach the exchange when no caller supplied a way to.

    Reached only if authority was granted without a transport, which is a
    caller error rather than a market condition: failing here keeps the
    absence of a transport from silently reading as a refused order.
    """

    raise ContractError("production_order_transport_missing")


def compile_production_plan(
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
    policy_authority_scope: str = "FORMAL_PROMOTED",
    regime_artifact: RegimeArtifact | Mapping[str, Any] | None = None,
    require_regime_ssot: bool = True,
    resource_probe_ok: bool = False,
    delta_v2_owns_cadence: bool = False,
    expected_source_lane: str | None = None,
    order_transport: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
    live_submit_authorized: bool = False,
    chain_consolidation_tilt: float | None = None,
    last_acted_probability_identity: str | None = None,
    last_decision_signature: Mapping[str, Any] | None = None,
    effective_edge_tables: Mapping[str, Any] | None = None,
    convergence_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile one v2-only production decision, and deliver it when authorised.

    Delivery is the step this lane never had: intents were written and nothing
    carried them to the exchange, so the reconciliation tables stayed empty
    from the first day.  Authority is a parameter rather than a constant, but
    it defaults to withheld -- an unauthorised run still walks the whole
    delivery path and records the payload it would have sent, which is how a
    no-live run can be compared against the live one it stands in for.
    """

    result = DeltaBasketScoreV2Optimizer().compile(
        station_policy_stage=station_policy_stage,
        chain_consolidation_tilt=chain_consolidation_tilt,
        last_acted_probability_identity=last_acted_probability_identity,
        last_decision_signature=last_decision_signature,
        effective_edge_tables=effective_edge_tables,
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
        policy_authority_scope=policy_authority_scope,
        regime_artifact=regime_artifact,
        require_regime_ssot=require_regime_ssot,
        resource_probe_ok=resource_probe_ok,
        delta_v2_owns_cadence=delta_v2_owns_cadence,
        expected_source_lane=expected_source_lane,
    )
    live_mode = PRODUCTION_EXECUTION_MODE == "production_live"
    plan = dict(result)
    plan["schema"] = PRODUCTION_PLAN_SCHEMA
    plan["execution_mode"] = PRODUCTION_EXECUTION_MODE
    # The third of the three no-live locks, and the last found: the intents
    # CHECK and the runtime config moved with the go-live migration, while
    # this literal False rode along under them -- compiled into every plan,
    # demanded back by the validator below, and never passed as True by the
    # worker.  A live system with this still False submits nothing and says
    # "not_authorized" about it, which reads as a refusal rather than a lock.
    plan["live_submit_authorized"] = live_mode
    plan["production_decision_path"] = True
    plan["policy_authority_scope"] = str(policy_authority_scope)
    optimizer_status = str(plan.get("status") or "").strip().lower()
    plan["status"] = (
        "ready_no_live"
        if optimizer_status == "ready"
        else "hold"
        if optimizer_status == "hold"
        else "blocked"
    )
    plan["boundary"] = {
        "production_decision_path": True,
        "execution_mode": PRODUCTION_EXECUTION_MODE,
        "live_order_authorized": live_mode,
        "live_submit_authorized": live_mode,
        # What the cycle may do, not what it has done: orders_submitted stays
        # a fact reported by the submission receipt.
        "places_orders": live_mode,
        "orders_submitted": 0,
        "writes_production_db": False,
        "calls_order_gateway": live_mode,
        "network_access": live_mode,
        "order_candidates_are_non_authoritative": True,
        "target_compiler": "DeltaBasketScoreV2Optimizer",
        "target_compiler_schema": "bayes_basket_target_delta_v2_optimizer_v2",
    }
    plan["production_plan_sha256"] = content_sha256(
        {key: value for key, value in plan.items() if key != "production_plan_sha256"}
    )
    return plan


def _load_staged_revision_fact(
    conn: Any, *, revision_sha256: str
) -> dict[str, Any]:
    """Read the canonical revision fact this chain roots on, by its hash.

    Fails closed rather than inventing one.  A report the observation-identity
    lane has not identified yet has no fact, and a trade that rooted on a
    fabricated one would be citing evidence the store never agreed to.
    """

    sha = str(revision_sha256 or "").strip().lower()
    if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
        raise ContractError("target_atomic_revision_sha256_invalid")
    rows = conn.execute(
        # Either identity lane may hold the root: PR12 truth revisions for
        # AWC reports, parallel substitute identity facts for scheduled ASOS
        # slot fills.  Content addresses cannot collide across the two.
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type IN ("
        "  'truth_observation_revision','substitute_observation_revision'"
        ") AND content_sha256=? "
        "ORDER BY fact_id",
        (sha,),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError("target_atomic_revision_fact_not_staged")
    return json.loads(str(rows[0][0]))


def commit_production_candidate_atomic(
    conn: Any,
    *,
    compiled_plan: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    awc_observation_contract: Mapping[str, Any] | None,
    truth_observation_revision_content_sha256: str,
    bound: Mapping[str, Any] | None,
    created_at_utc: str,
    supersede_prepared_cycle_id: str | None = None,
    new_decision_scope_key: str | None = None,
    order_transport: Any = None,
    live_submit_authorized: bool = False,
    evaluation_at_utc: str | None = None,
) -> dict[str, Any]:
    """Persist one compiled no-live candidate through the unique M5 boundary.

    This is intentionally explicit and is not called by the read-only
    compiler.  The caller must provide the already materialized observation
    and bound identities plus the official-WS market snapshot.  The adapter
    only converts eligible sizing rows into immutable candidate intents; it
    never reads legacy order tables, contacts a gateway, or authorizes live
    submission.  A blocked compile still writes one blocking decision and a
    zero-value reservation, so the reason remains authoritative in
    ``auto_trade_decisions``.
    """

    plan = compiled_plan.get("plan")
    if not isinstance(plan, Mapping):
        raise ContractError("production_compiled_plan_missing")
    if compiled_plan.get("execution_mode") != PRODUCTION_EXECUTION_MODE:
        raise ContractError("production_execution_mode_invalid")
    live_mode = PRODUCTION_EXECUTION_MODE == "production_live"
    if compiled_plan.get("live_submit_authorized") is not live_mode:
        raise ContractError("production_live_submit_boundary_invalid")
    if compiled_plan.get("boundary", {}).get("calls_order_gateway") is not live_mode:
        raise ContractError("production_gateway_boundary_invalid")
    lineage = plan.get("lineage")
    # A P0 forward plan carries no ``plan.lineage`` at all.  Normalize once:
    # every downstream read in this function then degrades to the empty
    # string and its own envelope/plan fallback, instead of each new field
    # read rediscovering the None crash one production day at a time
    # (probability_stage, then settlement_timezone, then posterior_sha256).
    if not isinstance(lineage, Mapping):
        lineage = {}
    # All three stages may hold a position: P0 opens on the ensemble, P0-M
    # re-confirms on the TAF revision, P1 adjusts on observed METAR.  The
    # chain head each one roots on differs and is chosen below; the stage
    # itself is no longer the thing that decides who may act.
    #
    # The stage is read from where each compiler actually puts it.  A P0
    # forward plan carries no ``plan.lineage`` at all -- its stage lives on
    # the probability envelope -- and reading only the lineage field turned
    # every stage-open into ``stage_invalid``, which quietly rebuilt the
    # "P0 waits for P1" gate this comment says was removed (charter 46).
    candidate_stage = (
        str(lineage.get("probability_stage") or "").strip().upper()
        if isinstance(lineage, Mapping)
        else ""
    )
    if not candidate_stage:
        envelope = compiled_plan.get("probability_envelope")
        if isinstance(envelope, Mapping):
            evidence = envelope.get("evidence")
            candidate_stage = str(
                envelope.get("stage")
                or (
                    evidence.get("probability_stage")
                    if isinstance(evidence, Mapping)
                    else ""
                )
                or ""
            ).strip().upper()
    if candidate_stage not in {"P0", "P0-M", "P1"}:
        raise ContractError("production_candidate_atomic_stage_invalid")
    station_id = str(plan.get("station_id") or "").strip()
    event_date = str(plan.get("event_date") or "").strip()
    if not station_id or not event_date:
        # The same shape as the stage read above, and the fourth reader
        # today to trip on it: a plan blocked before the success path
        # carries no scope fields of its own, and an empty string compared
        # against the market's real values reads as a mismatch.  The
        # envelope is the authority on its own scope -- the compile was
        # keyed on it -- so absence falls back there, and a genuine
        # disagreement between two present values still refuses below.
        scope_envelope = compiled_plan.get("probability_envelope")
        if isinstance(scope_envelope, Mapping):
            station_id = station_id or str(
                scope_envelope.get("station_id") or ""
            ).strip()
            event_date = event_date or str(
                scope_envelope.get("event_date") or ""
            ).strip()
    market_station = str(market_snapshot.get("station_id") or "").strip()
    market_event_date = str(market_snapshot.get("event_date") or "").strip()
    if station_id != market_station or event_date != market_event_date:
        # Name both sides.  This stood as the terminal blocker on seven
        # stations for a whole cycle and the receipt carried nothing to
        # diagnose it with -- not which side was empty, not whether it was
        # the station or the date, not by how much.
        raise ContractError(
            "production_candidate_market_scope_mismatch:"
            f"plan={station_id or '(empty)'}@{event_date or '(empty)'}"
            f":market={market_station or '(empty)'}@{market_event_date or '(empty)'}"
        )
    market_timezone = str(
        market_snapshot.get("settlement_timezone") or ""
    ).strip()
    # Same shape as the stage read above: a P0 forward plan carries no
    # ``plan.lineage`` at all, so the timezone is read from where each
    # compiler actually puts it -- lineage, then the plan itself, then the
    # probability envelope the stage was already resolved from.
    candidate_probability_envelope = compiled_plan.get("probability_envelope")
    probability_timezone = str(
        (
            lineage.get("settlement_timezone")
            if isinstance(lineage, Mapping)
            else ""
        )
        or plan.get("settlement_timezone")
        or (
            candidate_probability_envelope.get("settlement_timezone")
            if isinstance(candidate_probability_envelope, Mapping)
            else ""
        )
        or ""
    ).strip()
    assert_probability_timezone_matches_market(
        probability_timezone=probability_timezone,
        market_timezone=market_timezone,
    )

    # Preserve the upstream plan identity as a field, but let the target
    # ledger derive its own content address from the complete immutable plan.
    plan_payload = dict(plan)
    # The atomic writer must independently enforce the active economic
    # authority.  Production v2 never relies only on the compiler's status.
    plan_payload["delta_v2_required"] = True
    upstream_plan_id = plan_payload.pop("plan_id", None)
    plan_payload["upstream_plan_id"] = upstream_plan_id
    plan_payload["production_cycle_sha256"] = compiled_plan.get("production_cycle_sha256")
    plan_payload["settlement_timezone"] = market_timezone
    # Carry the compile's authority onto the plan the ledger records.  The
    # flag was stamped on the outer compiled plan and the ledger reads the
    # inner one, so the intent it writes had no authority to follow and fell
    # back to a literal 0 -- every intent minted since go-live is flagged
    # no-live, including the ones a live cycle produced.  Not a new decision:
    # the validator above already pinned this exact field to the mode, and
    # this copies that same value one level down.
    plan_payload["live_submit_authorized"] = bool(
        compiled_plan.get("live_submit_authorized") is True
    )
    plan_lineage = dict(lineage) if isinstance(lineage, Mapping) else {}
    plan_lineage["settlement_timezone"] = probability_timezone
    # The ledger binds a P1 plan's lineage to the exact canonical observation
    # -- six identity keys the success path copies out of the envelope in
    # runtime.py.  A plan blocked upstream never made that copy, and the
    # ledger's binding check (the eighth reader of this absence today) then
    # refused the blocked decision for lacking a join it never performed,
    # burying the real reason -- p1_entry_window_not_open, charter 67 doing
    # its job -- under plan_lineage_source_lane_missing.  Complete the copy
    # here, from the same envelope the facts adapter has already validated,
    # only where the plan carries nothing: a tradable plan still answers for
    # its own join, and a present value is never overwritten, so a genuine
    # mis-join still refuses in the ledger exactly as before.
    if candidate_stage not in {"P0", "P0-M"} and compiled_plan.get(
        "status"
    ) not in {"ready_no_live", "hold"}:
        envelope_lineage = (
            candidate_probability_envelope.get("lineage")
            if isinstance(candidate_probability_envelope, Mapping)
            else None
        )
        if isinstance(envelope_lineage, Mapping):
            # Mirror the whole copy the success path makes -- the envelope
            # lineage spread plus the four computed identities runtime.py
            # writes beside it -- rather than the six keys the first ledger
            # check happens to read.  Picking keys one refusal at a time is
            # how this fix would otherwise be rediscovered one gate per
            # cycle: probability_stage was demanded by the very next check
            # after the observation binding was satisfied.
            for key, value in envelope_lineage.items():
                if str(plan_lineage.get(key) or "").strip():
                    continue
                if value not in (None, ""):
                    plan_lineage[key] = value
            for key, value in {
                "probability_stage": candidate_probability_envelope.get("stage"),
                "probability_envelope_sha256": (
                    candidate_probability_envelope.get("content_sha256")
                ),
                "posterior_sha256": (
                    candidate_probability_envelope.get("posterior_sha256")
                ),
                "probability_decision_at_utc": (
                    candidate_probability_envelope.get("decision_at_utc")
                ),
            }.items():
                if str(plan_lineage.get(key) or "").strip():
                    continue
                if value not in (None, ""):
                    plan_lineage[key] = str(value)
    # P1 roots on the observation the day produced; the two stages that run
    # before one exists root on their own evidence instead.  Both are derived
    # from this same lineage, so the caller supplies neither and cannot pair a
    # stage with a root that does not belong to it.  They are built here, ahead
    # of the forward-evidence binding, because that binding hashes the lineage:
    # anything added afterwards would leave the plan and its binding disagreeing
    # about what was signed.
    #
    # The checkpoint is defined by the probability envelope, not by the plan
    # lineage, which carries only the cash-phase and curve-root checkpoints.
    candidate_envelope = compiled_plan.get("probability_envelope")
    stage_checkpoint_id = (
        str(candidate_envelope.get("checkpoint_id") or "")
        if isinstance(candidate_envelope, Mapping)
        else ""
    )
    stage_root = (
        build_stage_evidence_root(
            probability_stage=candidate_stage,
            station_id=station_id,
            event_date=event_date,
            settlement_timezone=probability_timezone,
            checkpoint_id=stage_checkpoint_id,
            posterior_sha256=str(lineage.get("posterior_sha256") or ""),
            lineage=lineage,
        )
        if candidate_stage in {"P0", "P0-M"}
        else None
    )
    stage_soft_bound = (
        build_model_envelope_soft_bound(
            probability_stage=candidate_stage,
            lineage=lineage,
            # Ordered by the interval each bucket covers, which is the same
            # order the envelope's own ordinals carry.  Deriving it from the
            # lineage keeps the bound provable from the plan alone rather than
            # from a mapping the caller could pass inconsistently.
            bucket_ordinals=_bucket_ordinals_from_lineage(lineage),
        )
        if stage_root is not None
        else None
    )
    if stage_root is not None:
        plan_lineage["stage_evidence_root_id"] = str(
            stage_root["evidence_root_id"]
        )
        plan_lineage["stage_evidence_content_sha256"] = str(
            stage_root["content_sha256"]
        )
        plan_lineage["probability_checkpoint_id"] = stage_checkpoint_id
        plan_lineage["soft_bound_id"] = str(stage_soft_bound["bound_id"])
    plan_payload["lineage"] = plan_lineage
    target_deadline: str | None = None
    deadline_input = (
        plan_payload.get("target_execution_hard_deadline_at")
        or market_snapshot.get("target_execution_hard_deadline_at")
    )
    try:
        if deadline_input not in (None, ""):
            target_deadline = TargetExecutionHardDeadline.from_value(deadline_input).isoformat
        elif market_snapshot.get("quote_observed_at") not in (None, ""):
            # A multi-bucket quote is first actionable only when this complete
            # decision join exists. Quote freshness remains anchored to the
            # oldest official book observation; execution admission starts at
            # the atomic candidate decision and remains capped by quote expiry.
            target_deadline = derive_target_execution_hard_deadline(
                created_at_utc,
                quote_expires_at=market_snapshot.get("quote_expires_at"),
            )
        else:
            raise TargetExecutionDeadlineError("target_execution_hard_deadline_missing")
    except TargetExecutionDeadlineError as exc:
        target_deadline = None
        deadline_blocker = str(exc)
    else:
        deadline_blocker = ""
    plan_payload["target_execution_hard_deadline_at"] = target_deadline
    quote_by_bucket = {
        str(row.get("bucket")): row
        for row in (market_snapshot.get("buckets") or [])
        if isinstance(row, Mapping)
    }
    sizing_rows = [row for row in (compiled_plan.get("order_sizing_decisions") or []) if isinstance(row, Mapping)]
    delta_v2_audit = plan.get("delta_v2_audit")
    direction = str(plan_payload.get("direction") or "").strip().upper()
    current_direction_raw = plan_payload.get("current_direction")
    current_direction = (
        None
        if current_direction_raw in (None, "")
        else str(current_direction_raw).strip().upper()
    )
    core_transition = plan_payload.get("core_transition")
    # These validators guard the decision details written below, and they
    # were written for the one plan shape that existed: a tradable compile.
    # A plan blocked upstream carries no directional claim, no transition
    # and no lifecycle -- and today they were the fifth, sixth and seventh
    # readers to treat that absence as a wrong value (empty direction is not
    # UP/DOWN/NEUTRAL; None is not a transition mapping; "" is not a basket
    # state), each in turn standing as the terminal blocker over the reason
    # the plan was actually blocked.  Absence on a non-tradable plan is
    # recorded as absence; a *present* malformed value still refuses, and a
    # plan that claims tradability answers for every field exactly as
    # before.
    plan_claims_tradability = compiled_plan.get("status") in {
        "ready_no_live",
        "hold",
    }
    if direction not in {"UP", "DOWN", "NEUTRAL"}:
        if plan_claims_tradability or direction:
            raise ContractError("production_plan_direction_invalid")
        direction = None
    if (
        current_direction is not None
        and current_direction not in {"UP", "DOWN", "NEUTRAL"}
    ):
        raise ContractError("production_plan_current_direction_invalid")
    if (
        not isinstance(core_transition, Mapping)
        or str(
            core_transition.get("direction_transition") or ""
        ).strip().upper()
        not in {"INITIALIZE", "MIGRATE", "NEUTRALIZE", "KEEP", "HOLD"}
        or str(core_transition.get("transition") or "").strip().upper()
        not in {
            "INITIALIZE",
            "MIGRATE",
            "FILL_GAP",
            "KEEP",
            "STOP_ADDING",
            "HOLD",
        }
    ):
        if plan_claims_tradability or core_transition is not None:
            raise ContractError("production_plan_core_transition_invalid")
    production_inputs = compiled_plan.get("production_inputs")
    if not isinstance(production_inputs, Mapping):
        production_inputs = {}
    delta_v2_authorized = (
        isinstance(delta_v2_audit, Mapping)
        and delta_v2_audit.get("authorized") is True
    )
    if (
        compiled_plan.get("status") == "ready_no_live"
        and not delta_v2_authorized
    ):
        raise ContractError(
            "production_candidate_delta_v2_authorization_missing"
        )
    compile_ready = (
        compiled_plan.get("status") == "ready_no_live"
        and delta_v2_authorized
        and compiled_plan.get("cadence_allows_candidate") is True
    )
    target_by_bucket = {
        str(row.get("bucket") or ""): row
        for row in (plan.get("targets") or [])
        if isinstance(row, Mapping)
    }
    required_gap_support = {
        str(value or "").strip()
        for value in plan.get("execution_gap_support") or []
        if str(value or "").strip()
    }
    selected_bundle_buckets = {
        str(value or "").strip()
        for value in plan.get("selected_buckets") or []
        if str(value or "").strip()
    }
    if compile_ready and (
        not required_gap_support
        or selected_bundle_buckets != required_gap_support
    ):
        raise ContractError(
            "production_candidate_complete_basket_selection_invalid"
        )
    vector_base = {
        "probability_sha256": lineage.get("probability_envelope_sha256"),
        "official_ws_quote_sha256": (
            market_snapshot.get("official_ws_quote_sha256")
            or market_snapshot.get("content_sha256")
        ),
        "inventory_sha256": market_snapshot.get("inventory_sha256"),
        "order_state_sha256": market_snapshot.get("order_state_sha256"),
        "station_policy_sha256": lineage.get("policy_resolution_sha256"),
        "cash_policy_decision_sha256": lineage.get(
            "cash_phase_decision_sha256"
        ),
        "schema": EXECUTION_VERSION_VECTOR_SCHEMA,
        "economic_policy_schema": DELTA_V2_ECONOMIC_POLICY_SCHEMA,
        "economic_policy_artifact_sha256": (
            production_inputs.get("delta_v2_artifact_sha256")
        ),
        "quote_observed_at": market_snapshot.get("quote_observed_at"),
        "quote_expires_at": market_snapshot.get("quote_expires_at"),
    }
    forward_evidence_binding = None
    if (
        delta_v2_authorized
        and compiled_plan.get("schema") == PRODUCTION_PLAN_SCHEMA
    ):
        forward_evidence_binding = _build_forward_evidence_binding(
            plan=plan_payload,
            lineage=plan_lineage,
            market_snapshot=market_snapshot,
            production_inputs=production_inputs,
            delta_v2_audit=delta_v2_audit,
            decision_at_utc=created_at_utc,
        )
    forward_observation_status = (
        "ELIGIBLE"
        if forward_evidence_binding is not None
        else (
            "INELIGIBLE_DELTA_V2_NOT_AUTHORIZED"
            if compiled_plan.get("schema") == PRODUCTION_PLAN_SCHEMA
            else "INELIGIBLE_NONCANONICAL_CALLER"
        )
    )
    intents: list[dict[str, Any]] = []
    execution_version_vectors: list[dict[str, Any]] = []
    vector_blockers: list[str] = []
    if deadline_blocker and compile_ready:
        vector_blockers.append(deadline_blocker)
    for row in sizing_rows if compile_ready else ():
        if row.get("eligible") is not True:
            continue
        bucket = str(row.get("bucket") or "").strip()
        quote = quote_by_bucket.get(bucket)
        if quote is None:
            raise ContractError("production_candidate_quote_bucket_missing")
        target = target_by_bucket.get(bucket)
        if target is None:
            vector_blockers.append(
                f"execution_version_vector_target_missing:{bucket}"
            )
            continue
        target_price_id = str(target.get("target_price_id") or "").strip()
        try:
            vector = build_execution_version_vector(
                {
                    **vector_base,
                    "target_price_id": target_price_id,
                }
            )
        except ExecutionVersionVectorError as exc:
            vector_blockers.append(
                "execution_version_vector_incomplete:" + str(exc)
            )
            continue
        token_id = str(quote.get("token_id") or quote.get("yes_token") or "").strip()
        if not token_id:
            raise ContractError("production_candidate_token_id_missing")
        c_order = float(row.get("c_order") or 0.0)
        q_order = float(row.get("q_order") or 0.0)
        r_limit = float(row.get("r_limit") or 0.0)
        if c_order <= 0 or q_order <= 0 or r_limit <= 0:
            raise ContractError("production_candidate_sizing_invalid")
        execution_candidate = str(
            target.get("execution_candidate") or ""
        ).strip().upper()
        if execution_candidate not in {"MAKER", "TAKER_FOK"}:
            raise ContractError(
                "production_candidate_execution_type_invalid"
            )
        intents.append({
            "bucket": bucket,
            "token_id": token_id,
            "side": "YES",
            "order_action": "BUY",
            "limit_price": r_limit,
            "requested_size": q_order,
            "requested_notional_usdc": c_order,
            "intent_json": {
                "schema": "bayes_basket_production_candidate_intent_v1",
                "candidate_will_order": True,
                # Execution type is an optimizer/B13 decision.  Official WS
                # quote rows carry market evidence only and must never own or
                # override the Maker/FOK strategy choice.
                "execution_candidate": execution_candidate,
                "post_only": execution_candidate == "MAKER",
                "cancel_acknowledged": bool(
                    target.get("cancel_acknowledged", False)
                ),
                "sizing_content_sha256": row.get("content_sha256"),
                "target_price_id": target_price_id,
                # The compile's own authority, not a literal.  Fifth and
                # sixth of the family that began with the plan's hard-coded
                # False: the intent row already carries this correctly, so
                # written as False here the decision's own JSON contradicted
                # the row it describes, and an auditor reading the decision
                # concluded no live intent existed while the ledger held one.
                "live_submit_authorized": bool(live_mode),
                "target_execution_hard_deadline_at": target_deadline,
            },
        })
        execution_version_vectors.append(vector.to_dict())
    intent_buckets = [
        str(item.get("bucket") or "").strip() for item in intents
    ]
    if compile_ready and (
        len(intent_buckets) != len(set(intent_buckets))
        or set(intent_buckets) != required_gap_support
    ):
        raise ContractError(
            "production_candidate_complete_basket_intents_incomplete"
        )
    if vector_blockers:
        intents = []
        execution_version_vectors = []
    blocked = not compile_ready or not intents
    reason_codes = [str(value) for value in (compiled_plan.get("reason_codes") or [])]
    if (
        compiled_plan.get("status") == "ready_no_live"
        and compiled_plan.get("cadence_allows_candidate") is not True
    ):
        reason_codes.append(
            "cadence_"
            + str(compiled_plan.get("cadence_decision") or "HOLD").lower()
        )
    if blocked and not reason_codes:
        reason_codes = ["production_candidate_blocked"]
    reason_codes.extend(vector_blockers)
    reason_codes = list(dict.fromkeys(reason_codes))
    decision = {
        "strategy_lane": "basket",
        "decided_state": "blocked" if blocked else "prepared",
        "will_order": False,
        "reason_codes": reason_codes,
        "details": {
            "schema": "bayes_basket_production_decision_v1",
            "basket_lifecycle": basket_lifecycle_projection(
                plan_payload, required=plan_claims_tradability
            ),
            "current_core_buckets": list(
                plan_payload.get("current_core_buckets") or ()
            ),
            "core_buckets": list(
                plan_payload.get("core_buckets") or ()
            ),
            "direction": direction,
            "current_direction": current_direction,
            "core_transition": (
                dict(core_transition)
                if isinstance(core_transition, Mapping)
                else None
            ),
            "candidate_will_order": not blocked,
            "production_plan_sha256": compiled_plan.get("production_plan_sha256"),
            "delta_v2_authorized": delta_v2_authorized,
            # Charter 77: what the next compile compares itself with.
            "decision_signature": (
                dict(delta_v2_audit.get("decision_moment") or {})
                if isinstance(delta_v2_audit, Mapping)
                else None
            ),
            # Charter 68(c): the lambda tables this decision was sized under,
            # so "did production read the store" is answerable from the row.
            "effective_edge_tables": (
                dict(delta_v2_audit.get("effective_edge_tables") or {})
                if isinstance(delta_v2_audit, Mapping)
                else None
            ),
            # Arm B (2026-09-06): the convergence lift's shadow receipt, so
            # the page can show what the lift would have admitted.
            "convergence_claim": (
                dict(delta_v2_audit.get("convergence_claim") or {})
                if isinstance(delta_v2_audit, Mapping)
                else None
            ),
            "delta_v2_content_sha256": (
                delta_v2_audit.get("content_sha256")
                if isinstance(delta_v2_audit, Mapping)
                else None
            ),
            "execution_mode": PRODUCTION_EXECUTION_MODE,
            "live_submit_authorized": bool(live_mode),
            "target_execution_hard_deadline_at": target_deadline,
            "forward_evidence_binding": forward_evidence_binding,
            "forward_observation_status": forward_observation_status,
        },
    }
    # Root the chain on the fact already in the store when no caller carried a
    # contract.  The observation-identity lane stages these on its own timer,
    # so the trade names the revision it roots on and the ledger verifies the
    # store agrees; a report the lane has not reached yet fails closed here
    # rather than being written on the way past.
    observed_revision_fact = None
    if stage_root is None and awc_observation_contract is None:
        observed_revision_fact = _load_staged_revision_fact(
            conn,
            revision_sha256=truth_observation_revision_content_sha256,
        )
    # The economic identity of this decision (user ruling, 2026-09-02).
    # Computed here, before the bundle, so the decision row is addressed by
    # it from the first write; the training fact built after the bundle
    # carries the same key, which is what makes migrated history and new
    # rows agree.  A plan the key cannot be derived from is not an error at
    # this point -- the training-fact builder will raise its own named
    # contract on it -- so the decision falls back to the economic subset of
    # its details, which is how every pre-migration blocked row is keyed.
    try:
        decision_scope_key = delta_v2_decision_scope_key(
            compiled_plan=compiled_plan, market_snapshot=market_snapshot,
        )
    except ContractError:
        decision_scope_key = None
    bundle = build_target_atomic_bundle(
        decision_scope_key=decision_scope_key,
        awc_observation_contract=(
            None if stage_root is not None else awc_observation_contract
        ),
        observed_revision_fact=observed_revision_fact,
        truth_observation_revision_content_sha256=(
            "" if stage_root is not None
            else truth_observation_revision_content_sha256
        ),
        stage_evidence_root=stage_root,
        stage_soft_bound=stage_soft_bound,
        bound=None if stage_root is not None else bound,
        plan=plan_payload,
        decision=decision,
        intents=intents,
        reserved_amount_usdc=sum(float(item["requested_notional_usdc"]) for item in intents),
        created_at_utc=created_at_utc,
        execution_version_vectors=execution_version_vectors,
        target_execution_hard_deadline_at=target_deadline,
    )
    if stage_root is not None:
        # The root and the model-envelope soft bound are persisted in the
        # same transaction so the committed cycle can never cite a root that
        # was not written; both are idempotent under replay.  The bound is an
        # audit fact beside the open, not a gate on it (charter 82): the
        # windows charters 49/58/79 admit are the opening's whole structural
        # constraint, and the retired ``gate`` arm lives in
        # ``_apply_envelope_soft_bound``.
        _apply_envelope_soft_bound(stage_soft_bound, intents)
        persist_stage_evidence(
            conn,
            evidence_root=stage_root,
            soft_bound=stage_soft_bound,
            created_at_utc=created_at_utc,
        )
    training_fact = build_delta_v2_training_fact(
        conn,
        compiled_plan=compiled_plan,
        market_snapshot=market_snapshot,
        atomic_bundle=bundle,
        created_at_utc=created_at_utc,
    )
    # The atomic ledger is the sole write boundary.  Passing the already
    # content-addressed fact in the bundle binds its id to every execution or
    # NO_EXECUTION vector before any row is committed.
    bundle["training_fact"] = training_fact
    # A scope key already held by another fact is this same decision, already
    # on file (user ruling, 2026-09-02: key hit is idempotence).  Answer it
    # here, before the ledger, with a retain and no rows.  Until now this
    # reached commit_target_atomic_bundle, whose decision-id retain misses a
    # restatement (the id hashes the plan sha, which moves with the quote)
    # and whose scope refusal then raised
    # target_training_fact_decision_scope_already_committed -- which the
    # stage-open caller recorded as a *blocker* and answered with a
    # preliminary blocked decision.  Measured 2026-09-02 06:58Z: 23 of 27
    # committed forward scopes in one cycle carried that blocker.  The
    # scope-conflict case (same key, different scope-defining column) keeps
    # raising the owner's named conflict; supersession is a different act
    # and keeps its own path.
    if not supersede_prepared_cycle_id:
        held_fact_id = committed_scope_fact_id(conn, fact=training_fact)
        if held_fact_id is not None and held_fact_id != str(
            training_fact["fact_id"]
        ):
            standing = conn.execute(
                "SELECT cycle_id, decision_id FROM basket_policy_training_fact"
                " WHERE fact_id=?",
                (held_fact_id,),
            ).fetchone()
            retained_receipt = {
                "schema": "basket_target_atomic_bundle_receipt_v1",
                "state": "already_committed_decision_scope",
                "idempotent": True,
                "cycle_id": str(standing[0]) if standing else "",
                "decision_id": str(standing[1]) if standing else "",
                "intent_count": 0,
                "reserved_amount_usdc": 0.0,
                "orders_submitted": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "execution_version_vector_count": 0,
                "training_fact_state": "already_committed_decision_scope",
                "training_fact_id": held_fact_id,
            }
            return {
                "schema": "bayes_basket_target_production_candidate_atomic_v1",
                "state": "already_committed_decision_scope",
                "idempotent": True,
                "receipt": retained_receipt,
                "delta_v2_training_fact": {
                    "schema": "delta_basket_score_training_fact_v2",
                    "state": "already_committed_decision_scope",
                    "fact_id": held_fact_id,
                    "idempotent": True,
                },
                "candidate_will_order": False,
                "order_submission": None,
                "orders_submitted": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "writes_target_ledger": False,
                "live_submit_authorized": False,
            }
    if supersede_prepared_cycle_id:
        receipt = supersede_prepared_cycle_and_commit_bundle(
            conn,
            old_cycle_id=supersede_prepared_cycle_id,
            new_decision_scope_key=(
                new_decision_scope_key
                or str(training_fact.get("decision_scope_key") or "")
            ),
            bundle=bundle,
            superseded_at_utc=created_at_utc,
        )
    else:
        receipt = commit_target_atomic_bundle(conn, bundle=bundle)
    fact_row = conn.execute(
        "SELECT fact_id,content_sha256,decision_scope_key "
        "FROM basket_policy_training_fact WHERE fact_id=?",
        (training_fact["fact_id"],),
    ).fetchone()
    if fact_row is None or str(fact_row[1] or "") != str(training_fact["content_sha256"]):
        raise ContractError("production_training_fact_commit_missing")
    scope_row = conn.execute(
        "SELECT fact_id,content_sha256 FROM basket_policy_training_fact "
        "WHERE decision_scope_key=?",
        (training_fact["decision_scope_key"],),
    ).fetchone()
    if (
        scope_row is None
        or str(scope_row[0] or "") != str(training_fact["fact_id"])
        or str(scope_row[1] or "") != str(training_fact["content_sha256"])
    ):
        raise ContractError("production_training_fact_scope_drift")
    training_state = receipt.get("training_fact_state")
    if training_state is None:
        if receipt.get("state") != "already_committed":
            raise ContractError("production_training_fact_state_missing")
        training_state = "already_committed"
    training_receipt = {
        "schema": "delta_basket_score_training_fact_v2",
        "state": str(training_state),
        "fact_id": str(training_fact["fact_id"]),
        "idempotent": str(training_state) == "already_committed",
    }
    # Deliver whatever this cycle just prepared.  The submitter reads the
    # authority rather than asserting it, so an unauthorised run reaches the
    # exchange boundary and stops there, leaving the payload hash on the
    # receipt.  A cycle that prepared nothing returns an empty submission set
    # instead of a special case.
    # Authority comes from the compiled plan -- the same field the validator
    # above pinned to the execution mode -- unless the caller explicitly
    # withholds it.  Requiring every caller to separately pass True is how
    # the third lock stayed shut after the other two opened.
    effective_authority = bool(
        live_submit_authorized
        or compiled_plan.get("live_submit_authorized") is True
    )
    submission_receipt = submit_prepared_intents(
        conn,
        cycle_id=str(bundle["cycle"]["cycle_id"]),
        transport=(
            order_transport
            if order_transport is not None
            else _default_order_transport
            if effective_authority
            else _no_transport
        ),
        live_submit_authorized=effective_authority,
        observed_at_utc=str(evaluation_at_utc or created_at_utc),
    )
    result = {
        "schema": "bayes_basket_target_production_candidate_atomic_v1",
        "state": receipt["state"],
        "receipt": receipt,
        "delta_v2_training_fact": training_receipt,
        "candidate_will_order": not blocked,
        # What this cycle did, not what the lane is configured to allow.  These
        # were constants while nothing could submit; reporting them as facts is
        # what makes a receipt worth reading once something can.
        "order_submission": submission_receipt,
        "orders_submitted": int(submission_receipt["orders_submitted"]),
        "places_orders": bool(submission_receipt["orders_submitted"]),
        "calls_order_gateway": bool(submission_receipt["calls_order_gateway"]),
        "writes_target_ledger": True,
        "live_submit_authorized": bool(
            submission_receipt["live_submit_authorized"]
        ),
    }
    if forward_evidence_binding is not None:
        forward_receipt = {
            "schema": "bayes_basket_target_atomic_forward_cycle_receipt_v1",
            "cycle_id": str(bundle["cycle"]["cycle_id"]),
            "decision_id": str(bundle["decision"]["decision_id"]),
            "plan_id": str(bundle["plan"]["plan_id"]),
            "plan_sha256": str(bundle["plan"]["plan_sha256"]),
            "decision_sha256": str(bundle["decision"]["decision_sha256"]),
            "decision_at_utc": forward_evidence_binding["decision_at_utc"],
            "forward_evidence_binding": forward_evidence_binding,
            "execution_mode": PRODUCTION_EXECUTION_MODE,
            "writes_target_ledger": True,
            "orders_submitted": 0,
            "places_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "authorizes_order": False,
            "evidence_only": True,
        }
        forward_receipt["receipt_sha256"] = content_sha256(forward_receipt)
        result["forward_observation_receipt"] = forward_receipt
    return result


def commit_production_stage_zero_execution_atomic(
    conn: Any,
    *,
    compiled_plan: Mapping[str, Any],
    anchor: Mapping[str, Any],
    created_at_utc: str,
) -> dict[str, Any]:
    """Commit a pre-final no-execution decision and its exact forward fact.

    This companion validates the complete read-only compile against its
    content-addressed zero-execution anchor and delegates the single blocked
    decision/vector write to the dedicated ledger boundary.  The resident
    production_no_live entry point therefore never materializes observation,
    bound, intent or reservation facts.  An authorized P0/P0-M decision does
    atomically materialize its own exact forward-training fact, which is
    explicitly evidence-only and never borrows P1 METAR/SPECI facts.
    """

    if not isinstance(compiled_plan, Mapping):
        raise ContractError("stage_zero_execution_compiled_plan_mapping_required")
    if not isinstance(anchor, Mapping):
        raise ContractError("stage_zero_execution_anchor_mapping_required")
    if compiled_plan.get("execution_mode") != PRODUCTION_EXECUTION_MODE:
        raise ContractError("stage_zero_execution_execution_mode_invalid")
    # The plan carries the mode's authority; the anchor below carries this
    # commit's own zero execution.  Those are two different claims and the
    # go-live work separated them everywhere except here: `compile` stamps
    # the plan with live_mode and the validator pins it to live_mode, while
    # this guard still demanded the literal False.  Every P0 and P0-M
    # candidate in the resident cycle routes through this function, so a live
    # deployment refused its own first two stages with
    # stage_zero_execution_live_submit_boundary_invalid.
    live_mode = PRODUCTION_EXECUTION_MODE == "production_live"
    if compiled_plan.get("live_submit_authorized") is not live_mode:
        raise ContractError("stage_zero_execution_live_submit_boundary_invalid")
    boundary = compiled_plan.get("boundary")
    if (
        not isinstance(boundary, Mapping)
        or boundary.get("calls_order_gateway") is not live_mode
        or boundary.get("places_orders") is not live_mode
        or int(boundary.get("orders_submitted", -1)) != 0
    ):
        raise ContractError("stage_zero_execution_gateway_boundary_invalid")
    if anchor.get("schema") != STAGE_ZERO_EXECUTION_ANCHOR_SCHEMA:
        raise ContractError("stage_zero_execution_anchor_schema_invalid")
    if (
        anchor.get("final_authority") != "P1"
        or anchor.get("capital_authorized") is not False
        or int(anchor.get("intents_written", -1)) != 0
        or int(anchor.get("reservations_written", -1)) != 0
        or anchor.get("calls_order_gateway") is not False
        or anchor.get("live_submit_authorized") is not False
        or int(anchor.get("orders_submitted", -1)) != 0
    ):
        raise ContractError("stage_zero_execution_authority_boundary_invalid")

    anchor_probability = anchor.get("probability_envelope")
    if not isinstance(anchor_probability, Mapping):
        raise ContractError("stage_zero_execution_anchor_probability_missing")
    stage = str(anchor_probability.get("stage") or "").strip()
    if stage not in {"P0", "P0-M"}:
        raise ContractError("stage_zero_execution_anchor_probability_stage_invalid")
    candidate_probability = compiled_plan.get("probability_envelope")
    if not isinstance(candidate_probability, Mapping):
        raise ContractError("stage_zero_execution_candidate_probability_missing")
    if str(candidate_probability.get("stage") or "").strip() != stage:
        raise ContractError("stage_zero_execution_candidate_probability_stage_mismatch")
    plan = compiled_plan.get("plan")
    if not isinstance(plan, Mapping):
        raise ContractError("stage_zero_execution_candidate_plan_missing")
    lineage = plan.get("lineage")
    if (
        not isinstance(lineage, Mapping)
        or str(lineage.get("probability_stage") or "").strip() != stage
    ):
        raise ContractError("stage_zero_execution_candidate_plan_stage_mismatch")

    # Compare the complete compiled object, rather than reconstructing a
    # reduced projection.  The anchor is intentionally sensitive to every
    # basket input and cannot be replayed against later market/inventory state.
    expected_compiled_plan = {
        key: value
        for key, value in compiled_plan.items()
        if key != "compiled_plan_sha256"
    }
    anchored_compiled_plan = anchor.get("compiled_plan")
    if (
        not isinstance(anchored_compiled_plan, Mapping)
        or dict(anchored_compiled_plan) != expected_compiled_plan
    ):
        raise ContractError("stage_zero_execution_compiled_plan_anchor_mismatch")
    expected_compiled_plan_sha = content_sha256(expected_compiled_plan)
    if _required_sha(
        "stage_zero_execution_compiled_plan_sha",
        anchor.get("compiled_plan_sha256"),
    ) != expected_compiled_plan_sha:
        raise ContractError("stage_zero_execution_compiled_plan_sha_mismatch")
    supplied_anchor_sha = _required_sha(
        "stage_zero_execution_anchor_sha",
        anchor.get("content_sha256"),
    )
    expected_anchor_sha = content_sha256({
        key: value for key, value in anchor.items() if key != "content_sha256"
    })
    if supplied_anchor_sha != expected_anchor_sha:
        raise ContractError("stage_zero_execution_anchor_sha_mismatch")

    created_at, _created_at_dt = _normalized_utc(
        "stage_zero_execution_created_at_utc",
        created_at_utc,
    )
    anchor_evaluation_at, _anchor_evaluation_dt = _normalized_utc(
        "stage_zero_execution_anchor_evaluation_at_utc",
        anchor.get("evaluation_at_utc"),
    )
    candidate_evaluation_at, _candidate_evaluation_dt = _normalized_utc(
        "stage_zero_execution_candidate_evaluation_at_utc",
        compiled_plan.get("evaluation_at_utc"),
    )
    if (
        created_at != anchor_evaluation_at
        or candidate_evaluation_at != anchor_evaluation_at
    ):
        raise ContractError("stage_zero_execution_decision_clock_mismatch")

    delta_v2_audit = compiled_plan.get("delta_v2_audit")
    if delta_v2_audit is not None and not isinstance(delta_v2_audit, Mapping):
        raise ContractError("stage_zero_execution_delta_v2_audit_mapping_required")
    delta_v2_authorized = (
        isinstance(delta_v2_audit, Mapping)
        and delta_v2_audit.get("authorized") is True
    )
    forward_evidence_binding = None
    if delta_v2_authorized:
        market_snapshot = compiled_plan.get("production_market_snapshot")
        production_inputs = compiled_plan.get("production_inputs")
        if not isinstance(market_snapshot, Mapping):
            raise ContractError("stage_zero_execution_market_snapshot_missing")
        if not isinstance(production_inputs, Mapping):
            raise ContractError("stage_zero_execution_production_inputs_missing")
        forward_evidence_binding = _build_forward_evidence_binding(
            plan=plan,
            lineage=lineage,
            market_snapshot=market_snapshot,
            production_inputs=production_inputs,
            delta_v2_audit=delta_v2_audit,
            decision_at_utc=anchor_evaluation_at,
        )

    # The historical/post-fill P1 fact writer remains separate from the
    # resident dry-run entry point.  This dedicated P0/P0-M boundary creates
    # only a no-execution vector plus a fully lineage-bound forward fact.
    from .atomic_ledger import commit_stage_zero_execution_scope_decision

    receipt = commit_stage_zero_execution_scope_decision(
        conn,
        anchor=anchor,
        # Preserve the normalized decision clock as the durable SQLite
        # timestamp.  This is semantically identical to the caller's input
        # but cannot diverge in representation from the anchor.
        created_at_utc=anchor_evaluation_at,
        training_fact_authorized=delta_v2_authorized,
        forward_evidence_binding=forward_evidence_binding,
    )
    training_state = receipt.get("training_fact_state")
    if delta_v2_authorized and training_state is None:
        raise ContractError("stage_zero_execution_training_fact_state_missing")
    return {
        "schema": "bayes_basket_target_production_stage_zero_execution_atomic_v1",
        "state": receipt["state"],
        "receipt": receipt,
        "stage_zero_execution_anchor_sha256": supplied_anchor_sha,
        "candidate_will_order": False,
        "orders_submitted": 0,
        "places_orders": False,
        "calls_order_gateway": False,
        "writes_target_ledger": True,
        "live_submit_authorized": False,
        "intents_written": 0,
        "reservations_written": 0,
        "training_facts_written": int(
            training_state in {"committed", "already_committed"}
        ),
        "delta_v2_training_fact": (
            {
                "schema": "delta_basket_score_training_fact_v2",
                "state": str(training_state),
                "idempotent": training_state == "already_committed",
            }
            if training_state is not None
            else None
        ),
        "forward_observation_status": (
            "ELIGIBLE"
            if forward_evidence_binding is not None
            else "INELIGIBLE_DELTA_V2_NOT_AUTHORIZED"
        ),
    }


__all__ = [
    "PRODUCTION_EXECUTION_MODE",
    "PRODUCTION_PLAN_SCHEMA",
    "commit_production_candidate_atomic",
    "commit_production_stage_zero_execution_atomic",
    "compile_production_plan",
]
