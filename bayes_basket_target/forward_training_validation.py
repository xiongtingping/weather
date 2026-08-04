"""Exact-version validation for production DeltaBasketScore v2 facts.

The resident target worker calls this module at low frequency when it has
already compiled one no-live decision.  A feature fact binds the exact
probability envelope, official CLOB WS snapshot, confirmed inventory,
known order state, policy vector, decision and candidate intents used at that
decision point.  It does not write tick history and never calls a gateway.

Settlement labels are owned by ``delta_v2_settlement_label``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .contracts import (
    ContractError,
    ProbabilityEnvelope,
    canonical_json,
    content_sha256,
)
from .basket_kernel.state_machine import basket_lifecycle_projection
from .execution_version_vector import (
    ExecutionVersionVectorError,
    build_execution_version_vector,
    validate_no_execution_version_policy,
)
from .settlement_time import settlement_day_window
from .station_policy_forward_outcome_binding import (
    build_future_station_policy_outcome_materialization,
)
FACT_SCHEMA = "delta_basket_score_training_fact_v2"
FACT_AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_FORWARD"
MAX_FEATURE_JSON_BYTES = 262_144
MAX_LABEL_OUTCOME_JSON_BYTES = 65_536
MAX_LABEL_BATCH = 64

_DECISION_SCOPE_VOLATILE_KEYS = frozenset(
    {
        "created_at",
        "created_at_utc",
        "updated_at",
        "captured_at_utc",
        "decision_at_utc",
        "evaluated_at",
        "evaluated_at_utc",
        "evaluation_at_utc",
        "observed_at",
        "observed_at_utc",
        "quote_observed_at",
        "quote_expires_at",
        "target_execution_hard_deadline_at",
        "effective_quote_observed_at",
        "stream_confirmed_at",
        "source_sequence",
        "sequence",
        "production_plan_sha256",
        "production_cycle_sha256",
        "plan_id",
        "cycle_id",
        "decision_id",
        "snapshot_id",
        "quote_snapshot_id",
        "book_observed_at",
        "quote_version",
        "inventory_version",
        "order_state_version",
        "cash_phase_observed_at_utc",
        "cash_phase_checkpoint_id",
        "cash_phase_decision_sha256",
    }
)


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _instant(name: str, value: Any) -> datetime:
    text = _timestamp(name, value)
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _json_mapping(name: str, value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(f"{name}_json_invalid") from exc
        if isinstance(decoded, Mapping):
            return dict(decoded)
    raise ContractError(f"{name}_mapping_required")


def _table_row(
    conn: sqlite3.Connection,
    *,
    table: str,
    where: str,
    params: Sequence[Any],
    missing: str,
    ambiguous: str,
) -> dict[str, Any]:
    cursor = conn.execute(
        f"SELECT * FROM {table} WHERE {where} LIMIT 2",
        tuple(params),
    )
    rows = cursor.fetchall()
    if not rows:
        raise ContractError(missing)
    if len(rows) != 1:
        raise ContractError(ambiguous)
    columns = tuple(item[0] for item in (cursor.description or ()))
    row = rows[0]
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return {name: row[index] for index, name in enumerate(columns)}


def _registry_timezone(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> str:
    rows = conn.execute(
        "SELECT DISTINCT settlement_timezone "
        "FROM basket_target_market_registry "
        "WHERE station_id=? AND event_date=?",
        (station_id, event_date),
    ).fetchall()
    values = {str(row[0] or "").strip() for row in rows}
    if len(values) != 1 or not next(iter(values), ""):
        raise ContractError("delta_v2_market_registry_timezone_not_unique")
    timezone_name = next(iter(values))
    settlement_day_window(event_date, timezone_name)
    return timezone_name


def _bounded_json(name: str, value: Mapping[str, Any]) -> str:
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > MAX_FEATURE_JSON_BYTES:
        raise ContractError(f"{name}_resource_limit_exceeded")
    return encoded


def _decision_semantics(value: Any) -> Any:
    """Strip transport clocks and derived row identities from pure output."""

    if isinstance(value, Mapping):
        return {
            str(key): _decision_semantics(item)
            for key, item in sorted(
                value.items(), key=lambda pair: str(pair[0])
            )
            if str(key) not in _DECISION_SCOPE_VOLATILE_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_decision_semantics(item) for item in value]
    return value


def _economic_decision_semantics(
    compiled_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Project only fields that can change the target economic instruction."""

    plan = _json_mapping(
        "delta_v2_plan", compiled_plan.get("plan")
    )
    action_selection = plan.get("delta_v2_action_selection")
    if not isinstance(action_selection, Mapping):
        action_selection = {}
    selected_rows = action_selection.get("selected_actions")
    if not isinstance(selected_rows, Sequence) or isinstance(
        selected_rows, (str, bytes, bytearray)
    ):
        selected_rows = ()
    selected_actions = []
    for selected in selected_rows:
        if not isinstance(selected, Mapping):
            continue
        action = selected.get("action")
        if not isinstance(action, Mapping):
            continue
        selected_actions.append({
            "bucket": action.get("bucket"),
            "order_mode": action.get("order_mode"),
            "limit_price": action.get("limit_price"),
            "maximum_shares": action.get("maximum_shares"),
            "cash_clip": action.get("cash_clip"),
            "authorized": selected.get("authorized"),
            "blockers": selected.get("blockers"),
        })
    selected_actions.sort(
        key=lambda row: (
            str(row.get("bucket") or ""),
            str(row.get("order_mode") or ""),
            float(row.get("limit_price") or 0.0),
            float(row.get("cash_clip") or 0.0),
        )
    )
    cash = compiled_plan.get("cash_phase_decision")
    if not isinstance(cash, Mapping):
        cash = {}
    sizing = compiled_plan.get("order_sizing_decisions")
    if not isinstance(sizing, Sequence) or isinstance(
        sizing, (str, bytes, bytearray)
    ):
        sizing = ()
    targets = plan.get("targets")
    if not isinstance(targets, Sequence) or isinstance(
        targets, (str, bytes, bytearray)
    ):
        targets = ()
    cadence = compiled_plan.get("cadence_decision")
    if not isinstance(cadence, Mapping):
        cadence = {}
    return _decision_semantics(
        {
            "status": compiled_plan.get("status"),
            "reason_codes": list(
                compiled_plan.get("reason_codes") or []
            ),
            "center_bucket": plan.get("center_bucket"),
            "center_stable": plan.get("center_stable"),
            "direction": plan.get("direction"),
            "candidate_support": plan.get("candidate_support"),
            "final_support": plan.get("final_support"),
            "basket_completion_required": plan.get(
                "basket_completion_required"
            ),
            "basket_target_activated": plan.get(
                "basket_target_activated"
            ),
            "execution_gap_support": plan.get(
                "execution_gap_support"
            ),
            "selected_buckets": plan.get("selected_buckets"),
            "selected_score": action_selection.get("selected_score"),
            "selected_actions": selected_actions,
            "cash_phase": {
                "clock_phase": cash.get("clock_phase"),
                "next_phase": cash.get("next_phase"),
                "cash_template_usdc": cash.get(
                    "cash_template_usdc"
                ),
                "transitioned": cash.get("transitioned"),
                "reason_code": cash.get("reason_code"),
            },
            "sizing": [
                {
                    key: item.get(key)
                    for key in (
                        "bucket",
                        "eligible",
                        "candidate_present",
                        "c_order",
                        "q_order",
                        "r_limit",
                        "delta_shares",
                        "reason_code",
                        "remaining_budget_after",
                    )
                }
                for item in sizing
                if isinstance(item, Mapping)
            ],
            "targets": [
                {
                    key: item.get(key)
                    for key in (
                        "bucket",
                        "target_shares",
                        "filled_shares",
                        "pending_shares",
                        "execution_candidate",
                        "candidate_price",
                        "limit_price",
                        "clip_shares",
                        "clip_notional_usdc",
                        "post_only",
                        "execution_reason_codes",
                    )
                }
                for item in targets
                if isinstance(item, Mapping)
            ],
            "cadence": cadence,
            "cadence_allows_candidate": compiled_plan.get(
                "cadence_allows_candidate"
            ),
        }
    )


def delta_v2_decision_scope_key(
    *,
    compiled_plan: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
) -> str:
    """Address one economically distinct forward decision point.

    Wall clocks and WS connection keepalives remain audit metadata.  They do
    not create a new decision.  Probability, economic book, inventory,
    order-state, promoted policy/cost artifacts, or deterministic decision
    semantics must change before this key changes.
    """

    envelope = _json_mapping(
        "delta_v2_probability_envelope",
        compiled_plan.get("probability_envelope"),
    )
    production_inputs = _json_mapping(
        "delta_v2_production_inputs",
        compiled_plan.get("production_inputs"),
    )
    resolved_policy = _json_mapping(
        "delta_v2_resolved_station_policy",
        compiled_plan.get("resolved_station_policy"),
    )
    lineage = _json_mapping(
        "delta_v2_probability_lineage", envelope.get("lineage")
    )
    plan = _json_mapping(
        "delta_v2_plan", compiled_plan.get("plan")
    )
    plan_lineage = _json_mapping(
        "delta_v2_plan_lineage", plan.get("lineage")
    )
    station_id = _text(
        "delta_v2_station_id", envelope.get("station_id")
    ).upper()
    event_date = _text(
        "delta_v2_event_date", envelope.get("event_date")
    )
    settlement_timezone = _text(
        "delta_v2_settlement_timezone",
        envelope.get("settlement_timezone"),
    )
    stage = _text("delta_v2_stage", envelope.get("stage")).upper()
    if (
        station_id
        != str(market_snapshot.get("station_id") or "").strip().upper()
        or event_date
        != str(market_snapshot.get("event_date") or "").strip()
        or settlement_timezone
        != str(
            market_snapshot.get("settlement_timezone") or ""
        ).strip()
    ):
        raise ContractError("delta_v2_decision_scope_market_mismatch")
    official_exclusion_mask_sha256 = content_sha256(
        {
            "schema": "official_exclusion_mask_v1",
            "hard_excluded_buckets": str(
                lineage.get("hard_excluded_buckets") or ""
            ),
        }
    )
    decision_semantics = _economic_decision_semantics(compiled_plan)
    authority_vector = {
        "schema": "delta_v2_forward_decision_scope_v2",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "stage": stage,
        "probability_envelope_id": _text(
            "delta_v2_probability_envelope_id",
            envelope.get("checkpoint_id"),
        ),
        "probability_envelope_sha256": _sha(
            "delta_v2_probability_envelope_sha256",
            envelope.get("content_sha256")
            or lineage.get("probability_envelope_sha256")
            or plan_lineage.get("probability_envelope_sha256"),
        ),
        "posterior_sha256": _sha(
            "delta_v2_posterior_sha256",
            envelope.get("posterior_sha256"),
        ),
        "official_exclusion_mask_sha256": (
            official_exclusion_mask_sha256
        ),
        # Exact snapshot ids/hashes remain in the immutable training fact for
        # lineage, but they are not an economic decision identity.  The WS
        # listener and no-live q0 producers content-address transport clocks,
        # so using their row hashes here turns harmless keepalives into tick
        # history and repeatedly supersedes an unchanged prepared candidate.
        #
        # Inventory/order economics are projected into every bucket.  Quote
        # economics that can alter a decision are already reflected by the
        # deterministic plan/cadence/sizing semantics below.  A fresh snapshot
        # with an unchanged output therefore zero-writes; any changed action,
        # score, limit, size, phase, probability, inventory or pending state
        # still changes this scope.
        "market_position_semantics": _decision_semantics(
            {
                "remaining_budget_usdc": market_snapshot.get(
                    "remaining_budget_usdc"
                ),
                "buckets": [
                    {
                        "bucket": item.get("bucket"),
                        "token_id": item.get("token_id"),
                        "filled_shares": item.get("filled_shares"),
                        "filled_cost_usdc": item.get(
                            "filled_cost_usdc"
                        ),
                        "pending_shares": item.get("pending_shares"),
                        "pending_notional_usdc": item.get(
                            "pending_notional_usdc"
                        ),
                        "active_before": item.get("active_before"),
                    }
                    for item in (
                        market_snapshot.get("buckets")
                        if isinstance(
                            market_snapshot.get("buckets"), Sequence
                        )
                        and not isinstance(
                            market_snapshot.get("buckets"),
                            (str, bytes, bytearray),
                        )
                        else ()
                    )
                    if isinstance(item, Mapping)
                ],
            }
        ),
        "policy_artifact_vector": {
            "strategy_version": production_inputs.get("strategy_version"),
            # Policy resolution carries the cash observation clock for audit;
            # it is not a new policy version and must not defeat idempotency.
            "station_policy": _decision_semantics(resolved_policy),
            "quote_freshness_policy_sha256": market_snapshot.get(
                "quote_freshness_policy_sha256"
            ),
            "cash_phase_policy_sha256": production_inputs.get(
                "cash_phase_policy_sha256"
            ),
            "delta_v2_artifact_sha256": production_inputs.get(
                "delta_v2_artifact_sha256"
            ),
            "maker_grid_oos_artifact_sha256_by_bucket": (
                production_inputs.get(
                    "maker_grid_oos_artifact_sha256_by_bucket"
                )
            ),
            "regime_artifact_sha256": production_inputs.get(
                "regime_artifact_sha256"
            ),
        },
        "decision_semantics": decision_semantics,
    }
    return content_sha256(authority_vector)


def build_validated_forward_fact_delta_v2(
    conn: sqlite3.Connection,
    *,
    compiled_plan: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    atomic_bundle: Mapping[str, Any],
    created_at_utc: str,
) -> dict[str, Any]:
    """Build one immutable fact from the exact inputs used by a decision."""

    created_at = _timestamp("delta_v2_fact_created_at_utc", created_at_utc)
    envelope = _json_mapping(
        "delta_v2_probability_envelope",
        compiled_plan.get("probability_envelope"),
    )
    stage = _text("delta_v2_stage", envelope.get("stage")).upper()
    if stage not in {"P0", "P0-M", "P1"}:
        raise ContractError("delta_v2_training_stage_invalid")
    station_id = _text("delta_v2_station_id", envelope.get("station_id")).upper()
    event_date = _text("delta_v2_event_date", envelope.get("event_date"))
    probability_decision_at = _timestamp(
        "delta_v2_probability_decision_at_utc",
        envelope.get("decision_at_utc"),
    )
    # The envelope clock is when the probability was materialized.  The
    # forward decision clock is the atomic M5 capture instant; conflating the
    # two made a later official quote appear to be from the future.
    decision_at = created_at
    if _instant(
        "delta_v2_probability_decision_at_utc",
        probability_decision_at,
    ) > _instant("delta_v2_decision_at_utc", decision_at):
        raise ContractError("delta_v2_probability_not_strict_asof")
    settlement_timezone = _text(
        "delta_v2_settlement_timezone",
        envelope.get("settlement_timezone"),
    )
    if _registry_timezone(
        conn,
        station_id=station_id,
        event_date=event_date,
    ) != settlement_timezone:
        raise ContractError("delta_v2_training_registry_timezone_mismatch")

    checkpoint_id = _text(
        "delta_v2_probability_envelope_id",
        envelope.get("checkpoint_id"),
    )
    envelope_contract = ProbabilityEnvelope.from_mapping(envelope)
    envelope_row = _table_row(
        conn,
        table="basket_probability_envelope",
        where="envelope_id=? AND station_id=? AND event_date=? AND state='COMMITTED'",
        params=(checkpoint_id, station_id, event_date),
        missing="delta_v2_probability_envelope_missing",
        ambiguous="delta_v2_probability_envelope_ambiguous",
    )
    posterior_sha = _sha(
        "delta_v2_posterior_sha256", envelope.get("posterior_sha256")
    )
    if (
        str(envelope_row["content_sha256"] or "")
        != envelope_contract.content_sha256
        or str(envelope_row["posterior_sha256"] or "") != posterior_sha
        or str(envelope_row["settlement_timezone"] or "") != settlement_timezone
    ):
        raise ContractError("delta_v2_probability_envelope_drift")

    snapshot_id = _text(
        "delta_v2_ws_snapshot_id", market_snapshot.get("snapshot_id")
    )
    quote_row = _table_row(
        conn,
        table="basket_official_ws_snapshot",
        where="snapshot_id=? AND station_id=? AND event_date=? AND state='COMMITTED'",
        params=(snapshot_id, station_id, event_date),
        missing="delta_v2_ws_snapshot_missing",
        ambiguous="delta_v2_ws_snapshot_ambiguous",
    )
    if (
        str(quote_row["quote_source"] or "") != "OFFICIAL_CLOB_WS"
        or str(quote_row["settlement_timezone"] or "") != settlement_timezone
        or str(quote_row["quote_version"] or "")
        != str(market_snapshot.get("quote_version") or "")
    ):
        raise ContractError("delta_v2_ws_snapshot_drift")
    try:
        source_sequence = int(quote_row["sequence"])
    except (TypeError, ValueError) as exc:
        raise ContractError("delta_v2_source_sequence_invalid") from exc
    if source_sequence < 0:
        raise ContractError("delta_v2_source_sequence_invalid")
    try:
        raw_source_sequence = market_snapshot["sequence"]
        if isinstance(raw_source_sequence, bool):
            raise ValueError("boolean_sequence")
        if (
            isinstance(raw_source_sequence, float)
            and not raw_source_sequence.is_integer()
        ):
            raise ValueError("fractional_sequence")
        observed_source_sequence = int(raw_source_sequence)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("delta_v2_source_sequence_invalid") from exc
    if observed_source_sequence < 0:
        raise ContractError("delta_v2_source_sequence_invalid")
    if source_sequence != observed_source_sequence:
        raise ContractError("delta_v2_source_sequence_drift")
    quote_observed_at = _timestamp(
        "delta_v2_quote_observed_at",
        quote_row["quote_observed_at"],
    )
    quote_expires_at = _timestamp(
        "delta_v2_quote_expires_at",
        quote_row["quote_expires_at"],
    )
    quote_freshness_policy_sha256 = _sha(
        "delta_v2_quote_freshness_policy_sha256",
        quote_row["quote_freshness_policy_sha256"],
    )
    if (
        str(market_snapshot.get("quote_observed_at") or "")
        != quote_observed_at
        or str(market_snapshot.get("quote_expires_at") or "")
        != quote_expires_at
        or str(
            market_snapshot.get("quote_freshness_policy_sha256") or ""
        )
        != quote_freshness_policy_sha256
    ):
        raise ContractError("delta_v2_quote_freshness_drift")
    if not (
        _instant("delta_v2_quote_observed_at", quote_observed_at)
        <= _instant("delta_v2_decision_at_utc", decision_at)
        < _instant("delta_v2_quote_expires_at", quote_expires_at)
    ):
        raise ContractError("delta_v2_quote_not_fresh_at_decision")

    inventory_version = _text(
        "delta_v2_inventory_version", quote_row["inventory_version"]
    )
    inventory_row = _table_row(
        conn,
        table="basket_confirmed_inventory_snapshot",
        where=(
            "station_id=? AND event_date=? AND inventory_version=? "
            "AND state='COMMITTED'"
        ),
        params=(station_id, event_date, inventory_version),
        missing="delta_v2_inventory_snapshot_missing",
        ambiguous="delta_v2_inventory_snapshot_ambiguous",
    )
    if int(inventory_row["complete"] or 0) != 1:
        raise ContractError("delta_v2_inventory_snapshot_incomplete")

    order_state_version = _text(
        "delta_v2_order_state_version", quote_row["order_state_version"]
    )
    order_state_row = _table_row(
        conn,
        table="basket_order_state_snapshot",
        where=(
            "station_id=? AND event_date=? AND order_state_version=? "
            "AND state='COMMITTED'"
        ),
        params=(station_id, event_date, order_state_version),
        missing="delta_v2_order_state_snapshot_missing",
        ambiguous="delta_v2_order_state_snapshot_ambiguous",
    )
    if int(order_state_row["known"] or 0) != 1:
        raise ContractError("delta_v2_order_state_snapshot_unknown")

    cycle = _json_mapping("delta_v2_atomic_cycle", atomic_bundle.get("cycle"))
    decision = _json_mapping(
        "delta_v2_atomic_decision", atomic_bundle.get("decision")
    )
    plan_record = _json_mapping(
        "delta_v2_atomic_plan_record", atomic_bundle.get("plan")
    )
    plan = _json_mapping(
        "delta_v2_atomic_plan", plan_record.get("plan_json")
    )
    if (
        str(cycle.get("station_id") or "").upper() != station_id
        or str(cycle.get("event_date") or "") != event_date
        or str(cycle.get("settlement_timezone") or "") != settlement_timezone
        or str(decision.get("cycle_id") or "") != str(cycle.get("cycle_id") or "")
        or str(decision.get("plan_id") or "") != str(cycle.get("plan_id") or "")
    ):
        raise ContractError("delta_v2_training_decision_scope_mismatch")

    intents_raw = atomic_bundle.get("intents")
    if not isinstance(intents_raw, Sequence) or isinstance(
        intents_raw, (str, bytes, bytearray)
    ):
        raise ContractError("delta_v2_training_intents_list_required")
    intents = [dict(item) for item in intents_raw if isinstance(item, Mapping)]
    if len(intents) != len(intents_raw):
        raise ContractError("delta_v2_training_intent_invalid")
    intent_refs = sorted(
        (
            _text("delta_v2_order_intent_id", item.get("order_intent_id")),
            _sha("delta_v2_intent_sha256", item.get("intent_sha256")),
        )
        for item in intents
    )
    vectors_raw = atomic_bundle.get("execution_version_vectors")
    if (
        not isinstance(vectors_raw, Sequence)
        or isinstance(vectors_raw, (str, bytes, bytearray))
    ):
        raise ContractError(
            "delta_v2_execution_version_vectors_list_required"
        )
    try:
        execution_vectors = [
            build_execution_version_vector(item).to_dict()
            for item in vectors_raw
            if isinstance(item, Mapping)
        ]
    except ExecutionVersionVectorError as exc:
        raise ContractError(
            "delta_v2_execution_version_vector_invalid:" + str(exc)
        ) from exc
    if len(execution_vectors) != len(vectors_raw):
        raise ContractError(
            "delta_v2_execution_version_vector_mapping_required"
        )
    if len(execution_vectors) != len(intents):
        raise ContractError(
            "delta_v2_execution_version_vector_intent_count_mismatch"
        )
    for index, (intent, vector) in enumerate(
        zip(intents, execution_vectors)
    ):
        intent_json = _json_mapping(
            f"delta_v2_intent_{index}_json",
            intent.get("intent_json"),
        )
        if (
            _text(
                f"delta_v2_intent_{index}_target_price_id",
                intent_json.get("target_price_id"),
            )
            != vector["target_price_id"]
        ):
            raise ContractError(
                "delta_v2_execution_version_vector_target_price_drift"
            )
    no_execution_raw = atomic_bundle.get("no_execution_policy")
    no_execution_policy: dict[str, Any] | None = None
    if intents:
        if no_execution_raw is not None:
            raise ContractError(
                "delta_v2_no_execution_policy_with_intents"
            )
    else:
        if not isinstance(no_execution_raw, Mapping):
            raise ContractError(
                "delta_v2_no_execution_policy_required"
            )
        try:
            no_execution_policy = validate_no_execution_version_policy(
                no_execution_raw
            ).to_dict()
        except ExecutionVersionVectorError as exc:
            raise ContractError(
                "delta_v2_no_execution_policy_invalid:" + str(exc)
            ) from exc

    production_inputs = _json_mapping(
        "delta_v2_production_inputs",
        compiled_plan.get("production_inputs"),
    )
    resolved_policy = _json_mapping(
        "delta_v2_resolved_station_policy",
        compiled_plan.get("resolved_station_policy"),
    )
    lineage = _json_mapping(
        "delta_v2_probability_lineage", envelope.get("lineage")
    )
    if stage == "P1" and "hard_excluded_buckets" not in lineage:
        raise ContractError("delta_v2_official_exclusion_mask_missing")
    official_exclusion_mask_sha256 = content_sha256(
        {
            "schema": "official_exclusion_mask_v1",
            "hard_excluded_buckets": str(
                lineage.get("hard_excluded_buckets") or ""
            ),
        }
    )
    policy_vector = {
        "schema": "delta_basket_score_policy_version_vector_v2",
        "strategy_version": _text(
            "delta_v2_strategy_version",
            production_inputs.get("strategy_version"),
        ),
        "season_bin": _text(
            "delta_v2_season_bin", production_inputs.get("season_bin")
        ),
        "maturity_bin": _text(
            "delta_v2_maturity_bin",
            production_inputs.get("probability_maturity_bin"),
        ),
        "liquidity_bin": _text(
            "delta_v2_liquidity_bin",
            production_inputs.get("liquidity_bin"),
        ),
        "station_policy": resolved_policy,
        "cash_phase_policy_version": production_inputs.get(
            "cash_phase_policy_version"
        ),
        "cash_phase_policy_sha256": production_inputs.get(
            "cash_phase_policy_sha256"
        ),
        "delta_v2_artifact_id": production_inputs.get(
            "delta_v2_artifact_id"
        ),
        "delta_v2_artifact_sha256": production_inputs.get(
            "delta_v2_artifact_sha256"
        ),
        "probability_lineage_refs": {
            key: value
            for key, value in lineage.items()
            if key.endswith("_id")
            or key.endswith("_ids")
            or key.endswith("_version")
            or key.endswith("_sha256")
        },
    }
    station_policy_outcome_materialization = (
        build_future_station_policy_outcome_materialization(
            station_id=station_id,
            event_date=event_date,
            settlement_timezone=settlement_timezone,
            stage=stage,
            decision_at_utc=decision_at,
            strategy_version=str(policy_vector["strategy_version"]),
            season_bin=str(policy_vector["season_bin"]),
            maturity_bin=str(policy_vector["maturity_bin"]),
            liquidity_bin=str(policy_vector["liquidity_bin"]),
        )
    )
    feature = {
        "schema": "delta_basket_score_forward_feature_vector_v2",
        "basket_lifecycle": basket_lifecycle_projection(plan),
        "candidate_status": compiled_plan.get("status"),
        "reason_codes": list(compiled_plan.get("reason_codes") or []),
        "plan": plan,
        "cash_phase_decision": compiled_plan.get("cash_phase_decision"),
        "order_sizing_decisions": list(
            compiled_plan.get("order_sizing_decisions") or []
        ),
        # This is the same exact execution-vector contract persisted by the
        # atomic ledger.  Forward/OOS evidence must never resurrect the
        # retired posterior/quote/inventory/order version tuple.
        "execution_version_vectors": execution_vectors,
        "no_execution_policy": no_execution_policy,
        # This future-only skeleton records the precise post-label contract
        # gaps.  It cannot accept caller scores, probabilities, or proxy
        # outputs; every family remains unavailable until its own approved
        # frozen-output plus utility-metric contract exists.
        "station_policy_outcome_materialization": (
            station_policy_outcome_materialization
        ),
        "bucket_intervals_c_json": lineage.get("bucket_intervals_c_json"),
        "no_live_boundary": {
            "execution_mode": "production_no_live",
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    policy_json = _bounded_json("delta_v2_policy_vector", policy_vector)
    feature_json = _bounded_json("delta_v2_feature", feature)
    partition_key = "|".join(
        (
            station_id,
            str(policy_vector["season_bin"]),
            stage,
            str(policy_vector["maturity_bin"]),
            str(policy_vector["liquidity_bin"]),
        )
    )
    decision_scope_key = delta_v2_decision_scope_key(
        compiled_plan=compiled_plan,
        market_snapshot=market_snapshot,
    )
    identity = {
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "decision_at_utc": decision_at,
        "decision_scope_key": decision_scope_key,
        "captured_at_utc": created_at,
        "source_sequence": source_sequence,
        "quote_observed_at": quote_observed_at,
        "quote_expires_at": quote_expires_at,
        "quote_freshness_policy_sha256": (
            quote_freshness_policy_sha256
        ),
        "stage": stage,
        "probability_envelope_id": checkpoint_id,
        "posterior_version": _text(
            "delta_v2_posterior_version", envelope.get("posterior_version")
        ),
        "posterior_sha256": posterior_sha,
        "official_exclusion_mask_sha256": official_exclusion_mask_sha256,
        "ws_snapshot_id": snapshot_id,
        "quote_version": _text(
            "delta_v2_quote_version", quote_row["quote_version"]
        ),
        "quote_sha256": _sha(
            "delta_v2_quote_sha256", quote_row["content_sha256"]
        ),
        "inventory_snapshot_id": _text(
            "delta_v2_inventory_snapshot_id",
            inventory_row["inventory_snapshot_id"],
        ),
        "inventory_version": inventory_version,
        "inventory_sha256": _sha(
            "delta_v2_inventory_sha256", inventory_row["content_sha256"]
        ),
        "order_state_snapshot_id": _text(
            "delta_v2_order_state_snapshot_id",
            order_state_row["order_state_snapshot_id"],
        ),
        "order_state_version": order_state_version,
        "order_state_sha256": _sha(
            "delta_v2_order_state_sha256",
            order_state_row["content_sha256"],
        ),
        "cycle_id": _text("delta_v2_cycle_id", cycle.get("cycle_id")),
        "decision_id": _text(
            "delta_v2_decision_id", decision.get("decision_id")
        ),
        "decision_sha256": _sha(
            "delta_v2_decision_sha256", decision.get("decision_sha256")
        ),
        "plan_sha256": _sha(
            "delta_v2_plan_sha256", plan_record.get("plan_sha256")
        ),
        "intent_count": len(intents),
        "intent_set_sha256": content_sha256(intent_refs),
        "policy_version_vector_json": policy_json,
        "feature_json": feature_json,
        "partition_key": partition_key,
        "authority_scope": FACT_AUTHORITY_SCOPE,
        "created_at_utc": created_at,
    }
    fact_sha = content_sha256({"schema": FACT_SCHEMA, **identity})
    return {
        "fact_id": f"d2fact_{fact_sha[:40]}",
        **identity,
        "content_sha256": fact_sha,
    }



__all__ = [
    "FACT_AUTHORITY_SCOPE",
    "FACT_SCHEMA",
    "build_validated_forward_fact_delta_v2",
    "delta_v2_decision_scope_key",
]
