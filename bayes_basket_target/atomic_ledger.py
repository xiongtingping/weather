"""M5 atomic decision and capital ledger for the Bayes basket target lane.

This module is the only target-lane writer for decision facts.  It is a
database boundary, not an executor: it never opens a network connection,
calls CLOB, or submits an order.  A committed intent is a no-live candidate
with ``live_submit_authorized=0``; the single blocking authority remains
``auto_trade_decisions``.

The writer deliberately accepts already compiled observation/bound facts and
does not inspect weather, probabilities, TAF, NWP, or market REST data.  The
five facts are committed under one SQLite ``BEGIN IMMEDIATE`` transaction:
observation, bound, decision, intent(s), and capital reservation.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Sequence

from .contracts import ContractError, canonical_json, content_sha256
from .basket_kernel.state_machine import basket_lifecycle_projection
from .et_history import assert_event_day_started, event_day_window
from .execution_deadline import (
    TargetExecutionDeadlineError,
    TargetExecutionHardDeadline,
)
from .execution_version_vector import (
    ExecutionVersionVector,
    NoExecutionVersionPolicy,
    NO_EXECUTION_POLICY_SCHEMA,
    build_execution_version_vector,
    build_no_execution_version_policy,
)
from .ssot import (
    PRODUCTION_LEDGER_DDL,
    upgrade_execution_vector_contract,
)


SCHEMA = "bayes_basket_target_atomic_ledger_v1"
OBSERVATION_SCHEMA = "basket_target_observation_fact_v1"
BOUND_SCHEMA = "basket_target_bound_fact_v1"
PLAN_SCHEMA = "basket_target_plan_v1"
DECISION_SCHEMA = "basket_target_decision_fact_v1"
INTENT_SCHEMA = "basket_target_order_intent_v1"
RESERVATION_SCHEMA = "basket_target_capital_reservation_v1"

TARGET_LEDGER_TABLES = (
    "basket_target_observation_facts",
    "basket_target_bound_facts",
    "basket_target_execution_cycles",
    "auto_trade_decisions",
    "basket_target_order_intents",
    "basket_target_capital_pool",
    "basket_target_capital_reservations",
    "basket_target_prepared_supersession_receipts",
    "basket_target_capital_epoch_migrations",
    "basket_target_writer_lock_wait_telemetry",
)

CYCLE_STATES = frozenset({"prepared", "submitting", "submitted", "reconcile_required", "settled"})
INTENT_STATES = frozenset({"prepared", "submitting", "submitted", "reconcile_required", "filled", "cancelled", "failed"})
RESERVATION_STATES = frozenset({"active", "released", "consumed", "expired"})
LANES = frozenset({"basket", "hard_no"})
_HARD_NO_FORBIDDEN_PLAN_KEY_FRAGMENTS = (
    "probability",
    "posterior",
    "prior",
    "likelihood",
    "taf",
    "nwp",
    "regime",
    "forecast",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class AtomicLedgerError(ContractError):
    """Raised when the target atomic ledger cannot safely proceed."""


class InjectedAtomicFault(RuntimeError):
    """Test-only fault used to prove transaction rollback at each write point."""


def _assert_event_day_started(
    event_date: str,
    *,
    settlement_timezone: str,
    as_of_utc: str,
) -> None:
    """Reject future registry settlement days at the decision write boundary.

    Any externally discovered future market must be quarantined before it
    reaches this ledger.  Keeping this guard here makes the invariant hold
    even when an upstream caller bypasses the resident worker's scope filter.
    """

    try:
        assert_event_day_started(
            event_date,
            as_of_utc=as_of_utc,
            target_timezone=_text(
                "settlement_timezone", settlement_timezone
            ),
        )
    except ContractError as exc:
        raise AtomicLedgerError(str(exc)) from exc
    except Exception as exc:
        raise AtomicLedgerError("event_day_window_invalid") from exc


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise AtomicLedgerError(f"{name}_missing")
    return result


def _date(name: str, value: Any) -> str:
    result = _text(name, value)
    if not _DATE_RE.fullmatch(result):
        raise AtomicLedgerError(f"{name}_invalid_date")
    try:
        datetime.strptime(result, "%Y-%m-%d")
    except ValueError as exc:
        raise AtomicLedgerError(f"{name}_invalid_date") from exc
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AtomicLedgerError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AtomicLedgerError(f"{name}_timezone_required")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if not _HEX64_RE.fullmatch(result):
        raise AtomicLedgerError(f"{name}_invalid_sha256")
    return result


def _number(name: str, value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise AtomicLedgerError(f"{name}_invalid_number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AtomicLedgerError(f"{name}_invalid_number") from exc
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise AtomicLedgerError(f"{name}_invalid_number")
    return result


def _mapping(name: str, value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise AtomicLedgerError(f"{name}_json_invalid") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise AtomicLedgerError(f"{name}_mapping_required")


def _target_execution_deadline(
    value: Any,
    *,
    now: str | None = None,
    required: bool = True,
) -> str | None:
    if value in (None, ""):
        if required:
            raise AtomicLedgerError("target_execution_hard_deadline_missing")
        return None
    try:
        deadline = TargetExecutionHardDeadline.from_value(value)
        if now is not None:
            deadline.assert_not_expired(now)
        return deadline.isoformat
    except TargetExecutionDeadlineError as exc:
        raise AtomicLedgerError(str(exc)) from exc


def _rows(name: str, value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise AtomicLedgerError(f"{name}_list_required")
    result = [row for row in value if isinstance(row, Mapping)]
    if len(result) != len(value):
        raise AtomicLedgerError(f"{name}_row_invalid")
    return result


def _derive_id(prefix: str, identity: Mapping[str, Any]) -> str:
    return f"{prefix}_{content_sha256(identity)[:40]}"


def _verify_or_set_id(name: str, supplied: Any, expected: str) -> str:
    if supplied not in (None, "") and str(supplied) != expected:
        raise AtomicLedgerError(f"{name}_content_address_mismatch")
    return expected


def _content_hash(schema: str, identity: Mapping[str, Any]) -> str:
    return content_sha256({"schema": schema, **dict(identity)})


def _reason_codes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AtomicLedgerError("reason_codes_list_required")
    result = [str(item).strip() for item in value]
    if any(not item for item in result):
        raise AtomicLedgerError("reason_code_empty")
    if len(result) != len(set(result)):
        raise AtomicLedgerError("reason_codes_duplicate")
    return result


def _assert_plan_observation_bound_lineage(
    *,
    plan_lineage: Mapping[str, Any],
    observation: Mapping[str, Any],
    bound: Mapping[str, Any],
) -> None:
    """Bind the P1 plan to the exact canonical observation and hard bound.

    The atomic writer receives already-materialized facts, but it must not
    trust the caller to have joined them to the selected P1 envelope.  These
    identities are therefore checked both while building and immediately
    before committing a bundle.
    """

    expected = {
        "canonical_observation_event_id": str(
            observation.get("observation_id") or ""
        ),
        "canonical_observation_sha256": str(
            observation.get("raw_sha256") or ""
        ).lower(),
        "canonical_observation_committed_at": str(
            observation.get("canonical_committed_at") or ""
        ),
        "hard_bound_id": str(bound.get("bound_id") or ""),
        "source_lane": str(observation.get("source_lane") or "").upper(),
        "report_type": str(observation.get("report_type") or "").upper(),
    }
    for field, expected_value in expected.items():
        actual = str(plan_lineage.get(field) or "")
        if field == "canonical_observation_sha256":
            actual = actual.lower()
        elif field in {"source_lane", "report_type"}:
            actual = actual.upper()
        if not actual:
            raise AtomicLedgerError(
                f"plan_lineage_{field}_missing"
            )
        if actual != expected_value:
            raise AtomicLedgerError(
                f"plan_lineage_{field}_mismatch"
            )


def _assert_plan_lane_contract(
    *,
    lane: str,
    plan: Mapping[str, Any],
    plan_lineage: Mapping[str, Any],
    commit_boundary: bool = False,
) -> None:
    if lane == "basket":
        if str(plan_lineage.get("probability_stage") or "") != "P1":
            raise AtomicLedgerError(
                "target_atomic_commit_requires_p1"
                if commit_boundary
                else "target_atomic_bundle_requires_p1"
            )
        return

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for raw_key, nested in value.items():
                key = str(raw_key).strip().lower()
                if any(
                    fragment in key
                    for fragment in _HARD_NO_FORBIDDEN_PLAN_KEY_FRAGMENTS
                ):
                    raise AtomicLedgerError(
                        "hard_no_plan_forbidden_probability_input:"
                        + (f"{path}.{key}" if path else key)
                    )
                walk(nested, f"{path}.{key}" if path else key)
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for index, nested in enumerate(value):
                walk(nested, f"{path}[{index}]")

    walk(plan, "")


def build_target_atomic_bundle(
    *,
    observation: Mapping[str, Any],
    bound: Mapping[str, Any],
    plan: Mapping[str, Any],
    decision: Mapping[str, Any],
    intents: Sequence[Mapping[str, Any]] = (),
    reserved_amount_usdc: float | None = None,
    execution_version_vector: Mapping[str, Any] | ExecutionVersionVector | None = None,
    execution_version_vectors: Sequence[Mapping[str, Any] | ExecutionVersionVector] | None = None,
    no_execution_policy: Mapping[str, Any] | NoExecutionVersionPolicy | None = None,
    training_fact: Mapping[str, Any] | None = None,
    target_execution_hard_deadline_at: str | datetime | None = None,
    created_at_utc: str,
) -> dict[str, Any]:
    """Normalize a candidate and derive every identity from immutable content."""

    created_at = _timestamp("created_at_utc", created_at_utc)
    obs = {
        "observation_id": _text("observation_id", observation.get("observation_id")),
        "station_id": _text("observation_station_id", observation.get("station_id")),
        "event_date": _date("observation_event_date", observation.get("event_date")),
        "source_lane": _text("observation_source_lane", observation.get("source_lane")).upper(),
        "report_type": _text("observation_report_type", observation.get("report_type")).upper(),
        "raw_sha256": _sha("observation_raw_sha256", observation.get("raw_sha256")),
        "canonical_committed_at": _timestamp(
            "observation_canonical_committed_at", observation.get("canonical_committed_at")
        ),
    }
    if obs["source_lane"] not in {"AMSC", "AWC"}:
        raise AtomicLedgerError("observation_source_lane_invalid")
    if obs["report_type"] not in {"METAR", "SPECI"}:
        raise AtomicLedgerError("observation_report_type_invalid")
    obs_sha = _content_hash(OBSERVATION_SCHEMA, obs)
    observation_fact_id = _verify_or_set_id(
        "observation_fact_id", observation.get("observation_fact_id"), _derive_id("obsfact", obs)
    )

    bound_payload = _mapping("bound_json", bound.get("bound_json", bound.get("hard_bound" , {})))
    bound_identity = {
        "bound_id": _text("bound_id", bound.get("bound_id")),
        "observation_id": obs["observation_id"],
        "station_id": _text("bound_station_id", bound.get("station_id", obs["station_id"])),
        "event_date": _date("bound_event_date", bound.get("event_date", obs["event_date"])),
        "bound_generation": int(bound.get("bound_generation", 0)),
        "hard_lower_bound": None if bound.get("hard_lower_bound") in (None, "") else int(bound["hard_lower_bound"]),
        "bound_json": bound_payload,
    }
    if bound_identity["bound_generation"] < 0:
        raise AtomicLedgerError("bound_generation_invalid")
    if bound_identity["station_id"] != obs["station_id"] or bound_identity["event_date"] != obs["event_date"]:
        raise AtomicLedgerError("bound_observation_scope_mismatch")
    bound_sha = _content_hash(BOUND_SCHEMA, bound_identity)
    bound_fact_id = _verify_or_set_id(
        "bound_fact_id", bound.get("bound_fact_id"), _derive_id("boundfact", bound_identity)
    )

    plan_payload = _mapping("plan", plan)
    plan_lineage = plan_payload.get("lineage")
    if not isinstance(plan_lineage, Mapping):
        raise AtomicLedgerError("target_atomic_bundle_lineage_missing")
    lane = _text(
        "strategy_lane",
        decision.get("strategy_lane", plan_payload.get("strategy_lane")),
    ).lower()
    if lane not in LANES:
        raise AtomicLedgerError("strategy_lane_invalid")
    _assert_plan_lane_contract(
        lane=lane,
        plan=plan_payload,
        plan_lineage=plan_lineage,
    )
    settlement_timezone = _text(
        "plan_settlement_timezone", plan_payload.get("settlement_timezone")
    )
    lineage_timezone = _text(
        "plan_lineage_settlement_timezone",
        plan_lineage.get("settlement_timezone"),
    )
    if settlement_timezone != lineage_timezone:
        raise AtomicLedgerError("plan_lineage_settlement_timezone_mismatch")
    _assert_plan_observation_bound_lineage(
        plan_lineage=plan_lineage,
        observation=obs,
        bound=bound_identity,
    )
    # ``plan_id`` is an optional caller-facing label, not an input to its own
    # hash.  Excluding it avoids the usual self-referential content-address
    # trap while still rejecting a supplied ID that does not match the plan.
    plan_identity = {key: value for key, value in plan_payload.items() if key != "plan_id"}
    plan_sha = _content_hash(PLAN_SCHEMA, plan_identity)
    plan_id = _verify_or_set_id("plan_id", plan_payload.get("plan_id"), _derive_id("plan", plan_identity))
    station_id = _text("decision_station_id", decision.get("station_id", obs["station_id"]))
    event_date = _date("decision_event_date", decision.get("event_date", obs["event_date"]))
    if station_id != obs["station_id"] or event_date != obs["event_date"]:
        raise AtomicLedgerError("decision_observation_scope_mismatch")
    cycle_identity = {
        "plan_id": plan_id,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "strategy_lane": lane,
    }
    cycle_id = _verify_or_set_id("cycle_id", decision.get("cycle_id"), _derive_id("cycle", cycle_identity))

    decided_state = _text("decided_state", decision.get("decided_state", "prepared")).lower()
    if decided_state not in {"prepared", "blocked"}:
        raise AtomicLedgerError("initial_decided_state_invalid")
    will_order = int(bool(decision.get("will_order", False)))
    # This target deployment is explicitly no-live.  The strategy's desired
    # action can remain in details.candidate_will_order, but the authority
    # table may never authorize a submit here.
    if will_order != 0:
        raise AtomicLedgerError("target_no_live_will_order_forbidden")
    reasons = _reason_codes(decision.get("reason_codes", []))
    if decided_state == "blocked" and not reasons:
        raise AtomicLedgerError("blocked_decision_reason_missing")
    details = _mapping("decision_details", decision.get("details", {}))
    lifecycle = basket_lifecycle_projection(plan_payload, required=False)
    if lifecycle is not None and details.get("basket_lifecycle") != lifecycle:
        raise AtomicLedgerError("decision_basket_lifecycle_plan_drift")
    candidate_will_order = details.get("candidate_will_order") is True
    delta_v2_audit = plan_payload.get("delta_v2_audit")
    delta_v2_authorized = (
        isinstance(delta_v2_audit, Mapping)
        and delta_v2_audit.get("authorized") is True
    )
    delta_v2_required = plan_payload.get(
        "delta_v2_required"
    ) is True
    if (
        candidate_will_order
        and delta_v2_required
        and not delta_v2_authorized
    ):
        raise AtomicLedgerError(
            "candidate_delta_v2_authorization_missing"
        )
    if (
        candidate_will_order
        and delta_v2_required
        and details.get("delta_v2_authorized") is not True
    ):
        raise AtomicLedgerError(
            "decision_delta_v2_authorization_missing"
        )

    # Resolve one immutable deadline before deriving decision/intent hashes.
    # A missing deadline is permitted only for a true no-intent blocker.
    deadline_candidates: list[Any] = []
    if target_execution_hard_deadline_at not in (None, ""):
        deadline_candidates.append(target_execution_hard_deadline_at)
    if decision.get("target_execution_hard_deadline_at") not in (None, ""):
        deadline_candidates.append(decision.get("target_execution_hard_deadline_at"))
    for raw in intents:
        if raw.get("target_execution_hard_deadline_at") not in (None, ""):
            deadline_candidates.append(raw.get("target_execution_hard_deadline_at"))
        raw_json = raw.get("intent_json")
        if isinstance(raw_json, Mapping) and raw_json.get("target_execution_hard_deadline_at") not in (None, ""):
            deadline_candidates.append(raw_json.get("target_execution_hard_deadline_at"))
    deadline: str | None = None
    if deadline_candidates:
        normalized = [_target_execution_deadline(value) for value in deadline_candidates]
        if len(set(normalized)) != 1:
            raise AtomicLedgerError("target_execution_hard_deadline_content_drift")
        deadline = normalized[0]
    if intents:
        if deadline is None:
            raise AtomicLedgerError("target_execution_hard_deadline_missing")
        _target_execution_deadline(deadline, now=created_at)

    decision_identity = {
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "strategy_lane": lane,
        "decided_state": decided_state,
        "will_order": will_order,
        "reason_codes": reasons,
        "details": details,
        "target_execution_hard_deadline_at": deadline,
    }
    decision_sha = _content_hash(DECISION_SCHEMA, decision_identity)
    decision_id = _verify_or_set_id(
        "decision_id", decision.get("decision_id"), _derive_id("decision", decision_identity)
    )

    intent_rows: list[dict[str, Any]] = []
    for index, raw in enumerate(intents):
        item = {
            "bucket": _text(f"intent_{index}_bucket", raw.get("bucket")),
            "token_id": _text(f"intent_{index}_token_id", raw.get("token_id")),
            "side": _text(f"intent_{index}_side", raw.get("side")).upper(),
            "order_action": _text(f"intent_{index}_order_action", raw.get("order_action", "BUY")).upper(),
            "limit_price": _number(f"intent_{index}_limit_price", raw.get("limit_price"), positive=True),
            "requested_size": _number(f"intent_{index}_requested_size", raw.get("requested_size"), positive=True),
            "requested_notional_usdc": _number(
                f"intent_{index}_requested_notional_usdc", raw.get("requested_notional_usdc"), positive=True
            ),
            "state": _text(f"intent_{index}_state", raw.get("state", "prepared")).lower(),
            "intent_json": _mapping(f"intent_{index}_json", raw.get("intent_json", raw)),
            "target_execution_hard_deadline_at": deadline,
        }
        item["intent_json"]["target_execution_hard_deadline_at"] = deadline
        if not 0 < item["limit_price"] < 1:
            raise AtomicLedgerError(f"intent_{index}_limit_price_out_of_range")
        if item["side"] not in {"YES", "NO"} or item["order_action"] not in {"BUY", "SELL"}:
            raise AtomicLedgerError(f"intent_{index}_side_or_action_invalid")
        if item["state"] not in INTENT_STATES:
            raise AtomicLedgerError(f"intent_{index}_state_invalid")
        if lane == "basket" and (item["side"], item["order_action"]) != ("YES", "BUY"):
            raise AtomicLedgerError("basket_intent_must_be_yes_buy")
        if lane == "hard_no" and (item["side"], item["order_action"]) != ("NO", "BUY"):
            raise AtomicLedgerError("hard_no_intent_must_be_no_buy")
        identity = {"decision_id": decision_id, "ordinal": index, **item}
        item["order_intent_id"] = _verify_or_set_id(
            f"intent_{index}_order_intent_id", raw.get("order_intent_id"), _derive_id("intent", identity)
        )
        item["idempotency_key"] = _derive_id("intentempotency", {"order_intent_id": item["order_intent_id"]})
        item["intent_sha256"] = _content_hash(INTENT_SCHEMA, identity)
        intent_rows.append(item)

    if decided_state == "blocked" and intent_rows:
        raise AtomicLedgerError("blocked_decision_cannot_have_intents")
    total_notional = sum(float(item["requested_notional_usdc"]) for item in intent_rows)
    requested_reserve = total_notional if reserved_amount_usdc is None else _number(
        "reserved_amount_usdc", reserved_amount_usdc
    )
    if requested_reserve + 1e-9 < total_notional:
        raise AtomicLedgerError("reservation_below_intent_notional")
    if decided_state == "blocked" and requested_reserve > 1e-9:
        raise AtomicLedgerError("blocked_decision_cannot_reserve_capital")

    if execution_version_vector is not None and execution_version_vectors is not None:
        raise AtomicLedgerError("execution_version_vectors_ambiguous")
    raw_vectors: list[Mapping[str, Any] | ExecutionVersionVector] = []
    if execution_version_vectors is not None:
        raw_vectors = list(execution_version_vectors)
    elif execution_version_vector is not None:
        raw_vectors = [execution_version_vector]
    if len(raw_vectors) != len(intent_rows):
        raise AtomicLedgerError("execution_version_vector_intent_count_mismatch")
    normalized_vectors = [
        build_execution_version_vector(raw.to_dict() if isinstance(raw, ExecutionVersionVector) else raw)
        for raw in raw_vectors
    ]
    if intent_rows and normalized_vectors:
        for index, vector in enumerate(normalized_vectors):
            intent_price_id = intent_rows[index]["intent_json"].get("target_price_id")
            if intent_price_id not in (None, "") and str(intent_price_id) != vector.target_price_id:
                raise AtomicLedgerError(f"execution_version_vector_{index}_target_price_drift")

    no_execution: NoExecutionVersionPolicy | None = None
    if decided_state == "blocked" and not intent_rows:
        supplied_policy = no_execution_policy
        if supplied_policy is None:
            supplied_policy = {
                "policy": "NO_EXECUTION",
                "reason_codes": reasons,
            }
        no_execution = build_no_execution_version_policy(
            supplied_policy.to_dict() if isinstance(supplied_policy, NoExecutionVersionPolicy) else supplied_policy
        )
    elif no_execution_policy is not None:
        raise AtomicLedgerError("no_execution_policy_requires_zero_intents")
    reservation_identity = {
        "cycle_id": cycle_id,
        "strategy_lane": lane,
        "amount_usdc": round(requested_reserve, 8),
    }
    reservation_id = _derive_id("reserve", reservation_identity)
    reservation_sha = _content_hash(RESERVATION_SCHEMA, reservation_identity)
    result = {
        "schema": SCHEMA,
        "created_at": created_at,
        "observation": {**obs, "observation_fact_id": observation_fact_id, "content_sha256": obs_sha},
        "bound": {**bound_identity, "bound_fact_id": bound_fact_id, "content_sha256": bound_sha},
        "plan": {"plan_id": plan_id, "plan_sha256": plan_sha, "plan_json": plan_payload},
        "cycle": {
            **cycle_identity,
            "cycle_id": cycle_id,
            "plan_sha256": plan_sha,
            "requested_reserve_usdc": requested_reserve,
            "state": "prepared",
        },
        "decision": {
            **decision_identity,
            "decision_id": decision_id,
            "decision_sha256": decision_sha,
        },
        "intents": intent_rows,
        "reservation": {
            "reservation_id": reservation_id,
            "cycle_id": cycle_id,
            "strategy_lane": lane,
            "amount_usdc": requested_reserve,
            "state": "active",
            "idempotency_key": _derive_id("reservation_idempotency", {"reservation_id": reservation_id}),
            "reservation_sha256": reservation_sha,
        },
        "execution_version_vectors": [vector.to_dict() for vector in normalized_vectors],
        "no_execution_policy": no_execution.to_dict() if no_execution is not None else None,
    }
    if training_fact is not None:
        result["training_fact"] = dict(training_fact)
    return result


def apply_target_ledger_schema(conn: sqlite3.Connection, *, commit: bool = True) -> dict[str, Any]:
    """Install only the explicit M5 ledger DDL on a migration connection."""

    upgrade_execution_vector_contract(conn)
    statement = ""
    for line in PRODUCTION_LEDGER_DDL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise AtomicLedgerError("sqlite_ledger_script_incomplete_statement")
    for table in (
        "basket_target_execution_cycles",
        "auto_trade_decisions",
        "basket_target_order_intents",
    ):
        columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if table == "basket_target_execution_cycles" and "settlement_timezone" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN settlement_timezone TEXT"
            )
        if table == "auto_trade_decisions" and "target_execution_hard_deadline_at" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN target_execution_hard_deadline_at TEXT"
            )
        if table == "basket_target_order_intents" and "target_execution_hard_deadline_at" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN target_execution_hard_deadline_at TEXT"
            )
    if commit:
        conn.commit()
    return target_ledger_schema_status(conn)


def target_ledger_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required_columns = {
        "auto_trade_decisions": {
            "decision_id", "cycle_id", "plan_id", "settlement_timezone",
            "decided_state", "will_order", "target_execution_hard_deadline_at",
            "reason_codes_json", "decision_sha256"
        },
        "basket_target_execution_cycles": {
            "cycle_id", "plan_id", "settlement_timezone", "state"
        },
        "basket_target_execution_version_vectors": {
            "order_intent_id", "cycle_id", "decision_id", "vector_policy",
            "target_price_id", "quote_observed_at", "quote_expires_at",
            "probability_sha256", "official_ws_quote_sha256", "inventory_sha256",
            "order_state_sha256", "station_policy_sha256",
            "cash_policy_decision_sha256", "strict_delta_artifact_sha256",
            "economic_policy_schema", "economic_policy_artifact_sha256",
            "vector_json", "vector_sha256",
        },
        "basket_target_order_intents": {
            "order_intent_id", "decision_id", "cycle_id", "state",
            "live_submit_authorized", "target_execution_hard_deadline_at",
            "intent_json", "intent_sha256",
        },
        "basket_target_capital_pool": {"pool_id", "balance_usdc", "reserved_usdc", "capital_epoch_id", "version"},
        "basket_target_capital_epoch_migrations": {
            "migration_id", "previous_epoch_id", "next_epoch_id",
            "runtime_contract_sha256", "content_sha256",
        },
    }
    missing_columns: dict[str, list[str]] = {}
    for table, required in required_columns.items():
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")} if table in tables else set()
        missing_columns[table] = sorted(required - columns)
    vector_columns = (
        {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(basket_target_execution_version_vectors)"
            )
        }
        if "basket_target_execution_version_vectors" in tables
        else set()
    )
    forbidden_vector_columns = sorted(
        vector_columns.intersection(
            {
                "posterior_version",
                "posterior_sha256",
                "snapshot_sha256",
                "quote_version",
                "inventory_version",
                "order_state_version",
            }
        )
    )
    vector_table_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='basket_target_execution_version_vectors'"
    ).fetchone()
    vector_table_sql = str(vector_table_sql_row[0] or "") if vector_table_sql_row else ""
    legacy_vector_policy_present = "LEGACY_CAS" in vector_table_sql
    missing_tables = sorted(set(TARGET_LEDGER_TABLES) - tables)
    return {
        "schema": SCHEMA,
        "ok": (
            not missing_tables
            and not any(missing_columns.values())
            and not forbidden_vector_columns
            and not legacy_vector_policy_present
        ),
        "missing_tables": missing_tables,
        "missing_columns": {key: value for key, value in missing_columns.items() if value},
        "forbidden_execution_vector_columns": forbidden_vector_columns,
        "legacy_execution_vector_policy_present": legacy_vector_policy_present,
        "blocking_authority": "auto_trade_decisions",
        "places_orders": False,
        "calls_order_gateway": False,
    }


def initialize_capital_pool(
    conn: sqlite3.Connection,
    *,
    balance_usdc: float,
    capital_epoch_id: str,
    updated_at_utc: str,
    commit: bool = True,
) -> dict[str, Any]:
    """Create the single shared pool; changing an existing epoch fails closed."""

    status = target_ledger_schema_status(conn)
    if not status["ok"]:
        raise AtomicLedgerError("target_ledger_schema_incomplete")
    balance = _number("balance_usdc", balance_usdc)
    epoch = _text("capital_epoch_id", capital_epoch_id)
    updated = _timestamp("updated_at_utc", updated_at_utc)
    row = conn.execute(
        "SELECT balance_usdc,reserved_usdc,capital_epoch_id,version FROM basket_target_capital_pool WHERE pool_id=1"
    ).fetchone()
    if row is not None:
        if str(row[2]) != epoch or abs(float(row[0]) - balance) > 1e-9:
            raise AtomicLedgerError("capital_pool_epoch_or_balance_drift")
        return {"state": "already_initialized", "version": int(row[3])}
    conn.execute(
        "INSERT INTO basket_target_capital_pool(pool_id,balance_usdc,reserved_usdc,capital_epoch_id,version,updated_at) VALUES(1,?,0,?,0,?)",
        (balance, epoch, updated),
    )
    if commit:
        conn.commit()
    return {"state": "initialized", "version": 0, "balance_usdc": balance, "capital_epoch_id": epoch}


def record_writer_lock_wait(
    conn: sqlite3.Connection,
    *,
    operation: str,
    wait_ms: float,
    acquired_at_utc: str,
) -> None:
    """Upsert one bounded aggregate of a real target writer acquisition."""

    operation_text = _text("writer_operation", operation)
    wait = _number("writer_lock_wait_ms", wait_ms)
    existing = conn.execute(
        "SELECT sample_count,total_wait_ms,max_wait_ms "
        "FROM basket_target_writer_lock_wait_telemetry WHERE singleton=1"
    ).fetchone()
    if existing is None:
        sample_count = 1
        total_wait = wait
        maximum = wait
    else:
        sample_count = int(existing[0]) + 1
        total_wait = float(existing[1]) + wait
        maximum = max(float(existing[2]), wait)
    updated_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    identity = {
        "singleton": 1,
        "sample_count": sample_count,
        "total_wait_ms": round(total_wait, 6),
        "max_wait_ms": round(maximum, 6),
        "last_wait_ms": round(wait, 6),
        "last_operation": operation_text,
        "last_acquired_at": acquired_at_utc,
        "updated_at": updated_at,
    }
    sha = content_sha256({"schema": "basket_target_writer_lock_wait_telemetry_v1", **identity})
    conn.execute(
        """INSERT INTO basket_target_writer_lock_wait_telemetry(
             singleton,sample_count,total_wait_ms,max_wait_ms,last_wait_ms,
             last_operation,last_acquired_at,updated_at,content_sha256
           ) VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(singleton) DO UPDATE SET
             sample_count=excluded.sample_count,
             total_wait_ms=excluded.total_wait_ms,
             max_wait_ms=excluded.max_wait_ms,
             last_wait_ms=excluded.last_wait_ms,
             last_operation=excluded.last_operation,
             last_acquired_at=excluded.last_acquired_at,
             updated_at=excluded.updated_at,
             content_sha256=excluded.content_sha256""",
        (
            identity["singleton"], identity["sample_count"], identity["total_wait_ms"],
            identity["max_wait_ms"], identity["last_wait_ms"], identity["last_operation"],
            identity["last_acquired_at"], identity["updated_at"], sha,
        ),
    )


@contextmanager
def _atomic_transaction(conn: sqlite3.Connection, *, operation: str = "target_atomic") -> Iterator[None]:
    own = not conn.in_transaction
    savepoint = "basket_target_m5_atomic"
    lock_started_ns = time.perf_counter_ns()
    acquired_at_utc = ""
    if own:
        conn.execute("BEGIN IMMEDIATE")
        acquired_at_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except Exception:
        if own:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        if own:
            record_writer_lock_wait(
                conn,
                operation=operation,
                wait_ms=(time.perf_counter_ns() - lock_started_ns) / 1_000_000.0,
                acquired_at_utc=acquired_at_utc,
            )
            conn.commit()
        else:
            conn.execute(f"RELEASE {savepoint}")


def _fault(fault_after: str | None, point: str) -> None:
    if fault_after == point:
        raise InjectedAtomicFault(point)


def _assert_existing_or_absent(conn: sqlite3.Connection, table: str, key: str, value: str, sha_column: str, sha: str) -> bool:
    row = conn.execute(f"SELECT {sha_column} FROM {table} WHERE {key}=?", (value,)).fetchone()
    if row is None:
        return False
    if str(row[0] or "") != sha:
        raise AtomicLedgerError(f"{table}_content_address_drift")
    return True


def _ensure_pool(conn: sqlite3.Connection) -> tuple[float, float, str, int]:
    row = conn.execute(
        "SELECT balance_usdc,reserved_usdc,capital_epoch_id,version FROM basket_target_capital_pool WHERE pool_id=1"
    ).fetchone()
    if row is None:
        raise AtomicLedgerError("capital_pool_missing")
    return float(row[0]), float(row[1]), str(row[2]), int(row[3])


def _reserve(conn: sqlite3.Connection, row: Mapping[str, Any], *, created_at: str) -> None:
    balance, reserved, _epoch, _version = _ensure_pool(conn)
    amount = float(row["amount_usdc"])
    if amount > balance - reserved + 1e-9:
        raise AtomicLedgerError("capital_pool_insufficient")
    cur = conn.execute(
        "UPDATE basket_target_capital_pool SET reserved_usdc=reserved_usdc+?,version=version+1,updated_at=? "
        "WHERE pool_id=1 AND balance_usdc-reserved_usdc>=?",
        (amount, created_at, amount),
    )
    if cur.rowcount != 1:
        raise AtomicLedgerError("capital_pool_cas_conflict")
    conn.execute(
        "INSERT INTO basket_target_capital_reservations(reservation_id,cycle_id,strategy_lane,amount_usdc,state,idempotency_key,reservation_sha256,release_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            row["reservation_id"], row["cycle_id"], row["strategy_lane"], amount, row["state"],
            row["idempotency_key"], row["reservation_sha256"], None, created_at, created_at,
        ),
    )


def _execution_vector_rows(
    bundle: Mapping[str, Any],
    *,
    cycle_id: str,
    decision_id: str,
) -> list[dict[str, Any]]:
    """Normalize the vector binding rows at the SQL write boundary."""

    intents = [dict(item) for item in bundle.get("intents", [])]
    raw_vectors = bundle.get("execution_version_vectors") or []
    if not isinstance(raw_vectors, Sequence) or isinstance(raw_vectors, (str, bytes, bytearray)):
        raise AtomicLedgerError("execution_version_vectors_list_required")
    if len(raw_vectors) != len(intents):
        raise AtomicLedgerError("execution_version_vector_intent_count_mismatch")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_vectors):
        if not isinstance(raw, Mapping):
            raise AtomicLedgerError("execution_version_vector_mapping_required")
        vector = build_execution_version_vector(raw)
        intent_id = str(intents[index].get("order_intent_id") or "").strip()
        if not intent_id:
            raise AtomicLedgerError("execution_version_vector_intent_missing")
        rows.append({
            "order_intent_id": intent_id,
            "cycle_id": cycle_id,
            "decision_id": decision_id,
            "training_fact_id": None,
            "vector_policy": "EXECUTION",
            "strict_delta_artifact_sha256": None,
            **vector.to_dict(),
            "vector_json": canonical_json({"schema": vector.schema, **vector.to_dict()}),
            "vector_sha256": vector.vector_sha256,
        })
    policy_raw = bundle.get("no_execution_policy")
    if policy_raw is not None:
        if intents:
            raise AtomicLedgerError("no_execution_policy_requires_zero_intents")
        if str(bundle.get("decision", {}).get("decided_state") or "").lower() != "blocked":
            raise AtomicLedgerError("no_execution_policy_requires_blocked_decision")
        if not isinstance(policy_raw, Mapping):
            raise AtomicLedgerError("no_execution_policy_mapping_required")
        policy = build_no_execution_version_policy(policy_raw)
        rows.append({
            "order_intent_id": None,
            "cycle_id": cycle_id,
            "decision_id": decision_id,
            "training_fact_id": None,
            "vector_policy": "NO_EXECUTION",
            "target_price_id": None,
            "quote_observed_at": None,
            "quote_expires_at": None,
            "probability_sha256": None,
            "official_ws_quote_sha256": None,
            "inventory_sha256": None,
            "order_state_sha256": None,
            "station_policy_sha256": None,
            "cash_policy_decision_sha256": None,
            "strict_delta_artifact_sha256": None,
            "economic_policy_schema": None,
            "economic_policy_artifact_sha256": None,
            "vector_json": canonical_json(policy.to_dict()),
            "vector_sha256": policy.policy_sha256,
        })
    return rows


def _sqlite_text_or_none(value: Any) -> Any:
    """Normalize SQLite TEXT adapters without changing its value.

    ``sqlite3.Row`` is still a positional sequence, but a connection may use
    a bytes ``text_factory`` or a driver-specific TEXT adapter.  Decode only
    valid UTF-8 bytes; leave every other type untouched so a malformed legacy
    row fails closed at the contract comparison below.
    """

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value
    return value


def _assert_existing_blocked_no_execution_vector(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    decision_id: str,
    expected_policy: NoExecutionVersionPolicy,
) -> None:
    """Verify the exact current NO_EXECUTION row at a blocked-cycle replay.

    The policy digest is the content address.  JSON key order/whitespace is
    not content, so it is parsed and revalidated before comparison.  The
    schema/version and the complete NULL shape remain hard gates: an older
    row without the current policy schema, an execution-shaped row, or a row
    carrying hidden training/evidence data requires explicit offline
    migration and cannot be silently replayed.
    """

    rows = conn.execute(
        "SELECT order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
        "target_price_id,quote_observed_at,quote_expires_at,probability_sha256,"
        "official_ws_quote_sha256,inventory_sha256,order_state_sha256,"
        "station_policy_sha256,cash_policy_decision_sha256,strict_delta_artifact_sha256,"
        "economic_policy_schema,economic_policy_artifact_sha256,"
        "vector_json,vector_sha256 "
        "FROM basket_target_execution_version_vectors WHERE cycle_id=?",
        (cycle_id,),
    ).fetchall()

    def drift(reason: str) -> None:
        raise AtomicLedgerError(
            "blocked_cycle_no_execution_vector_content_address_drift:" + reason
        )

    if len(rows) != 1:
        drift("row_count")
    row = rows[0]
    values = [_sqlite_text_or_none(value) for value in tuple(row)]
    (
        order_intent_id,
        stored_cycle_id,
        stored_decision_id,
        training_fact_id,
        vector_policy,
        target_price_id,
        quote_observed_at,
        quote_expires_at,
        probability_sha256,
        official_ws_quote_sha256,
        inventory_sha256,
        order_state_sha256,
        station_policy_sha256,
        cash_policy_decision_sha256,
        strict_delta_artifact_sha256,
        economic_policy_schema,
        economic_policy_artifact_sha256,
        vector_json,
        vector_sha256,
    ) = values

    if (
        order_intent_id is not None
        or stored_cycle_id != cycle_id
        or stored_decision_id != decision_id
        or training_fact_id is not None
        or vector_policy != "NO_EXECUTION"
        or target_price_id is not None
        or quote_observed_at is not None
        or quote_expires_at is not None
        or any(
            value is not None
            for value in (
                probability_sha256,
                official_ws_quote_sha256,
                inventory_sha256,
                order_state_sha256,
                station_policy_sha256,
                cash_policy_decision_sha256,
                strict_delta_artifact_sha256,
                economic_policy_schema,
                economic_policy_artifact_sha256,
            )
        )
    ):
        drift("null_shape_or_identity")
    if not isinstance(vector_json, str) or not isinstance(vector_sha256, str):
        drift("sqlite_text_type")

    def reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError("duplicate_json_key")
            payload[key] = value
        return payload

    try:
        stored_payload = json.loads(
            vector_json,
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (TypeError, ValueError):
        drift("json_invalid")
    if not isinstance(stored_payload, Mapping):
        drift("json_shape")
    if stored_payload.get("schema") != NO_EXECUTION_POLICY_SCHEMA:
        drift("policy_version_migration_required")
    try:
        stored_policy = build_no_execution_version_policy(stored_payload)
    except ContractError:
        drift("policy_invalid")
    if stored_policy.to_dict() != expected_policy.to_dict():
        drift("policy_content")
    if vector_sha256.lower() != expected_policy.policy_sha256:
        drift("policy_sha256")


def _assert_training_fact_scope(
    fact: Mapping[str, Any],
    *,
    cycle_id: str,
    decision_id: str,
) -> None:
    if str(fact.get("cycle_id") or "") != cycle_id:
        raise AtomicLedgerError("training_fact_cycle_lineage_mismatch")
    if str(fact.get("decision_id") or "") != decision_id:
        raise AtomicLedgerError("training_fact_decision_lineage_mismatch")
    if not str(fact.get("fact_id") or "").strip():
        raise AtomicLedgerError("training_fact_id_missing")
    if not str(fact.get("content_sha256") or "").strip():
        raise AtomicLedgerError("training_fact_content_sha256_missing")


def _insert_training_fact(conn: sqlite3.Connection, fact: Mapping[str, Any]) -> str:
    """Append through the one canonical durable-forward-fact validator.

    The atomic ledger owns the transaction, while the forward fact validator
    owns the fact schema, content address, idempotency and decision-scope
    conflict rules.  Keeping the import local avoids making those contracts
    mutually import each other at module load time.
    """

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='basket_policy_training_fact'"
    ).fetchone()
    if table_exists is None:
        raise AtomicLedgerError("training_fact_table_missing")
    try:
        if fact.get("schema") != "delta_basket_score_training_fact_v2":
            raise AtomicLedgerError(
                "production_training_fact_schema_not_delta_v2"
            )
        from .delta_v2_training import commit_delta_v2_training_fact

        receipt = commit_delta_v2_training_fact(conn, fact=fact)
    except ContractError as exc:
        raise AtomicLedgerError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise AtomicLedgerError("training_fact_canonical_write_failed") from exc
    state = str(receipt.get("state") or "")
    if state not in {"committed", "already_committed"}:
        raise AtomicLedgerError("training_fact_commit_state_invalid")
    return state


def _assert_existing_cycle_training_fact_binding(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    decision_id: str,
    training_fact: Mapping[str, Any] | None,
    vector_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Require an existing cycle's fact and vector bindings to agree exactly."""

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND "
        "name='basket_policy_training_fact'"
    ).fetchone()
    vector_fact_ids = {
        str(row.get("training_fact_id") or "")
        for row in vector_rows
        if row.get("training_fact_id") is not None
    }
    if table_exists is None:
        if training_fact is not None or vector_fact_ids:
            raise AtomicLedgerError("training_fact_canonical_table_missing")
        return

    stored = conn.execute(
        "SELECT fact_id,content_sha256,decision_scope_key "
        "FROM basket_policy_training_fact "
        "WHERE cycle_id=? AND decision_id=? ORDER BY fact_id",
        (cycle_id, decision_id),
    ).fetchall()
    if training_fact is None:
        if stored or vector_fact_ids:
            raise AtomicLedgerError("cycle_existing_training_fact_presence_drift")
        return

    expected = (
        str(training_fact.get("fact_id") or ""),
        str(training_fact.get("content_sha256") or ""),
        str(training_fact.get("decision_scope_key") or ""),
    )
    if not all(expected):
        raise AtomicLedgerError("training_fact_binding_fields_missing")
    if len(stored) != 1 or tuple(str(value or "") for value in stored[0]) != expected:
        raise AtomicLedgerError("cycle_existing_training_fact_binding_drift")
    if vector_fact_ids != {expected[0]}:
        raise AtomicLedgerError("cycle_existing_training_fact_vector_binding_drift")


def _assert_existing_cycle_weather_fact_binding(
    conn: sqlite3.Connection,
    *,
    observation: Mapping[str, Any],
    bound: Mapping[str, Any],
) -> None:
    """Fail closed if an idempotent replay has lost either immutable fact."""

    observation_row = conn.execute(
        "SELECT observation_fact_id,content_sha256 FROM "
        "basket_target_observation_facts WHERE observation_id=?",
        (str(observation.get("observation_id") or ""),),
    ).fetchone()
    expected_observation = (
        str(observation.get("observation_fact_id") or ""),
        str(observation.get("content_sha256") or ""),
    )
    if (
        observation_row is None
        or tuple(str(value or "") for value in observation_row)
        != expected_observation
    ):
        raise AtomicLedgerError(
            "cycle_existing_observation_fact_binding_drift"
        )

    bound_row = conn.execute(
        "SELECT bound_fact_id,observation_id,content_sha256 FROM "
        "basket_target_bound_facts WHERE bound_id=?",
        (str(bound.get("bound_id") or ""),),
    ).fetchone()
    expected_bound = (
        str(bound.get("bound_fact_id") or ""),
        str(observation.get("observation_id") or ""),
        str(bound.get("content_sha256") or ""),
    )
    if (
        bound_row is None
        or tuple(str(value or "") for value in bound_row)
        != expected_bound
    ):
        raise AtomicLedgerError(
            "cycle_existing_bound_fact_binding_drift"
        )


def commit_target_atomic_bundle(
    conn: sqlite3.Connection,
    *,
    bundle: Mapping[str, Any],
    fault_after: str | None = None,
    execution_version_vector: Mapping[str, Any] | None = None,
    execution_version_vectors: Sequence[Mapping[str, Any]] | None = None,
    no_execution_policy: Mapping[str, Any] | None = None,
    training_fact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit all M5 facts or none; repeated identical commits are idempotent."""

    if any(value is not None for value in (execution_version_vector, execution_version_vectors, no_execution_policy, training_fact)):
        mutable_bundle = dict(bundle)
        if execution_version_vector is not None:
            if execution_version_vectors is not None:
                raise AtomicLedgerError("execution_version_vectors_ambiguous")
            mutable_bundle["execution_version_vectors"] = [execution_version_vector]
        elif execution_version_vectors is not None:
            mutable_bundle["execution_version_vectors"] = list(execution_version_vectors)
        if no_execution_policy is not None:
            mutable_bundle["no_execution_policy"] = no_execution_policy
        if training_fact is not None:
            mutable_bundle["training_fact"] = training_fact
        bundle = mutable_bundle

    status = target_ledger_schema_status(conn)
    if not status["ok"]:
        raise AtomicLedgerError("target_ledger_schema_incomplete")
    if bundle.get("schema") != SCHEMA:
        raise AtomicLedgerError("atomic_bundle_schema_invalid")
    # Re-normalization is intentionally not implicit: callers must use the
    # builder so that stored hashes and IDs are checked before entering SQL.
    required = ("observation", "bound", "plan", "cycle", "decision", "intents", "reservation")
    if any(key not in bundle for key in required):
        raise AtomicLedgerError("atomic_bundle_incomplete")
    obs = dict(bundle["observation"])
    bound = dict(bundle["bound"])
    cycle = dict(bundle["cycle"])
    decision = dict(bundle["decision"])
    intents = [dict(item) for item in bundle["intents"]]
    reservation = dict(bundle["reservation"])
    training_fact = bundle.get("training_fact")
    if training_fact is not None and not isinstance(training_fact, Mapping):
        raise AtomicLedgerError("training_fact_mapping_required")
    created_at = _timestamp("bundle_created_at", bundle.get("created_at"))
    cycle_timezone = _text(
        "cycle_settlement_timezone", cycle.get("settlement_timezone")
    )
    _assert_event_day_started(
        str(cycle.get("event_date") or ""),
        settlement_timezone=cycle_timezone,
        as_of_utc=created_at,
    )

    # Do not trust a previously normalized mapping merely because it carries
    # a hash.  Recompute every content address at the write boundary so a
    # caller cannot mutate details after planning and still reuse a cycle ID.
    obs_identity = {key: value for key, value in obs.items() if key not in {"observation_fact_id", "content_sha256"}}
    if obs.get("content_sha256") != _content_hash(OBSERVATION_SCHEMA, obs_identity):
        raise AtomicLedgerError("observation_content_address_drift")
    bound_identity = {key: value for key, value in bound.items() if key not in {"bound_fact_id", "content_sha256"}}
    if bound.get("content_sha256") != _content_hash(BOUND_SCHEMA, bound_identity):
        raise AtomicLedgerError("bound_content_address_drift")
    plan_identity = {key: value for key, value in dict(bundle["plan"].get("plan_json") or {}).items() if key != "plan_id"}
    if cycle.get("plan_sha256") != _content_hash(PLAN_SCHEMA, plan_identity):
        raise AtomicLedgerError("plan_content_address_drift")
    plan_lineage = plan_identity.get("lineage")
    if not isinstance(plan_lineage, Mapping):
        raise AtomicLedgerError("target_atomic_commit_lineage_missing")
    cycle_lane = _text("cycle_strategy_lane", cycle.get("strategy_lane")).lower()
    if cycle_lane not in LANES:
        raise AtomicLedgerError("strategy_lane_invalid")
    _assert_plan_lane_contract(
        lane=cycle_lane,
        plan=plan_identity,
        plan_lineage=plan_lineage,
        commit_boundary=True,
    )
    plan_timezone = _text(
        "plan_settlement_timezone", plan_identity.get("settlement_timezone")
    )
    lineage_timezone = _text(
        "plan_lineage_settlement_timezone",
        plan_lineage.get("settlement_timezone"),
    )
    if (
        plan_timezone != lineage_timezone
        or plan_timezone != cycle_timezone
        or str(decision.get("settlement_timezone") or "").strip()
        != cycle_timezone
    ):
        raise AtomicLedgerError("atomic_settlement_timezone_mismatch")
    _assert_plan_observation_bound_lineage(
        plan_lineage=plan_lineage,
        observation=obs,
        bound=bound,
    )
    expected_plan_id = _derive_id("plan", plan_identity)
    if bundle["plan"].get("plan_id") != expected_plan_id or cycle.get("plan_id") != expected_plan_id:
        raise AtomicLedgerError("plan_id_content_address_drift")
    cycle_identity = {
        "plan_id": expected_plan_id,
        "station_id": cycle.get("station_id"),
        "event_date": cycle.get("event_date"),
        "settlement_timezone": cycle_timezone,
        "strategy_lane": cycle.get("strategy_lane"),
    }
    if cycle.get("cycle_id") != _derive_id("cycle", cycle_identity):
        raise AtomicLedgerError("cycle_id_content_address_drift")
    deadline = _target_execution_deadline(
        decision.get("target_execution_hard_deadline_at"),
        now=created_at if intents else None,
        required=bool(intents),
    )
    if any(item.get("target_execution_hard_deadline_at") != deadline for item in intents):
        raise AtomicLedgerError("target_execution_hard_deadline_content_drift")
    decision_identity = {key: value for key, value in decision.items() if key not in {"decision_id", "decision_sha256"}}
    if decision.get("decision_sha256") != _content_hash(DECISION_SCHEMA, decision_identity):
        raise AtomicLedgerError("decision_content_address_drift")
    if decision.get("decision_id") != _derive_id("decision", decision_identity):
        raise AtomicLedgerError("decision_id_content_address_drift")
    for ordinal, item in enumerate(intents):
        intent_identity = {
            "decision_id": decision["decision_id"],
            "ordinal": ordinal,
            **{key: value for key, value in item.items() if key not in {"order_intent_id", "idempotency_key", "intent_sha256"}},
        }
        if item.get("intent_sha256") != _content_hash(INTENT_SCHEMA, intent_identity):
            raise AtomicLedgerError("intent_content_address_drift")
        expected_intent_id = _derive_id("intent", intent_identity)
        if item.get("order_intent_id") != expected_intent_id:
            raise AtomicLedgerError("intent_id_content_address_drift")
        if item.get("idempotency_key") != _derive_id(
            "intentempotency",
            {"order_intent_id": expected_intent_id},
        ):
            raise AtomicLedgerError("intent_idempotency_key_drift")
    reservation_identity = {
        "cycle_id": reservation.get("cycle_id"),
        "strategy_lane": reservation.get("strategy_lane"),
        "amount_usdc": round(float(reservation.get("amount_usdc", 0.0)), 8),
    }
    if reservation.get("reservation_sha256") != _content_hash(RESERVATION_SCHEMA, reservation_identity):
        raise AtomicLedgerError("reservation_content_address_drift")
    expected_reservation_id = _derive_id("reserve", reservation_identity)
    if reservation.get("reservation_id") != expected_reservation_id:
        raise AtomicLedgerError("reservation_id_content_address_drift")
    if reservation.get("idempotency_key") != _derive_id(
        "reservation_idempotency",
        {"reservation_id": expected_reservation_id},
    ):
        raise AtomicLedgerError("reservation_idempotency_key_drift")
    if cycle.get("state") != "prepared":
        raise AtomicLedgerError("initial_cycle_state_invalid")
    if decision.get("will_order") != 0:
        raise AtomicLedgerError("target_no_live_will_order_forbidden")
    if decision.get("decided_state") == "blocked" and (intents or float(reservation.get("amount_usdc", 0.0)) > 1e-9):
        raise AtomicLedgerError("blocked_bundle_has_execution_facts")
    if decision.get("cycle_id") != cycle.get("cycle_id") or decision.get("plan_id") != cycle.get("plan_id"):
        raise AtomicLedgerError("decision_cycle_lineage_mismatch")
    if reservation.get("cycle_id") != cycle.get("cycle_id"):
        raise AtomicLedgerError("reservation_cycle_lineage_mismatch")
    total_notional = sum(float(item.get("requested_notional_usdc", 0.0)) for item in intents)
    if total_notional > float(reservation.get("amount_usdc", 0.0)) + 1e-9:
        raise AtomicLedgerError("reservation_below_intent_notional")
    if any(item.get("state") not in INTENT_STATES for item in intents):
        raise AtomicLedgerError("intent_state_invalid")
    vector_rows = _execution_vector_rows(
        bundle,
        cycle_id=str(cycle["cycle_id"]),
        decision_id=str(decision["decision_id"]),
    )
    if training_fact is not None:
        _assert_training_fact_scope(
            training_fact,
            cycle_id=str(cycle["cycle_id"]),
            decision_id=str(decision["decision_id"]),
        )
    training_fact_id = (
        str(training_fact.get("fact_id"))
        if training_fact is not None
        else None
    )
    for row in vector_rows:
        row["training_fact_id"] = training_fact_id
    with _atomic_transaction(conn, operation="commit_target_atomic_bundle"):
        existing_cycle = conn.execute(
            "SELECT plan_sha256,requested_reserve_usdc,state,settlement_timezone "
            "FROM basket_target_execution_cycles WHERE cycle_id=?",
            (cycle["cycle_id"],),
        ).fetchone()
        if existing_cycle is not None:
            if (
                str(existing_cycle[0]) != str(cycle["plan_sha256"])
                or abs(float(existing_cycle[1]) - float(cycle["requested_reserve_usdc"])) > 1e-9
                or str(existing_cycle[3] or "") != cycle_timezone
            ):
                raise AtomicLedgerError("cycle_content_address_drift")
            decision_row = conn.execute(
                "SELECT decision_id,decision_sha256 FROM auto_trade_decisions WHERE cycle_id=?",
                (cycle["cycle_id"],),
            ).fetchone()
            reservation_row = conn.execute(
                "SELECT reservation_id,reservation_sha256 FROM basket_target_capital_reservations WHERE cycle_id=?",
                (cycle["cycle_id"],),
            ).fetchone()
            if (
                decision_row is None
                or str(decision_row[0]) != str(decision["decision_id"])
                or str(decision_row[1]) != str(decision["decision_sha256"])
            ):
                raise AtomicLedgerError("cycle_existing_decision_drift")
            if (
                reservation_row is None
                or str(reservation_row[0]) != str(reservation["reservation_id"])
                or str(reservation_row[1]) != str(reservation["reservation_sha256"])
            ):
                raise AtomicLedgerError("cycle_existing_reservation_drift")
            existing_intents = {
                (str(row[0]), str(row[1]), str(row[2]))
                for row in conn.execute(
                    "SELECT order_intent_id,intent_sha256,idempotency_key "
                    "FROM basket_target_order_intents WHERE cycle_id=?",
                    (cycle["cycle_id"],),
                ).fetchall()
            }
            expected_intents = {
                (
                    str(item["order_intent_id"]),
                    str(item["intent_sha256"]),
                    str(item["idempotency_key"]),
                )
                for item in intents
            }
            if existing_intents != expected_intents:
                raise AtomicLedgerError("cycle_existing_intents_drift")
            _assert_existing_cycle_weather_fact_binding(
                conn,
                observation=obs,
                bound=bound,
            )
            _assert_existing_cycle_training_fact_binding(
                conn,
                cycle_id=str(cycle["cycle_id"]),
                decision_id=str(decision["decision_id"]),
                training_fact=training_fact,
                vector_rows=vector_rows,
            )
            vector_signature = lambda row: (
                row["order_intent_id"], row["cycle_id"], row["decision_id"],
                row["training_fact_id"],
                row["vector_policy"], row["target_price_id"], row["quote_observed_at"],
                row["quote_expires_at"], row["probability_sha256"],
                row["official_ws_quote_sha256"], row["inventory_sha256"],
                row["order_state_sha256"], row["station_policy_sha256"],
                row["cash_policy_decision_sha256"], row["strict_delta_artifact_sha256"],
                row["economic_policy_schema"],
                row["economic_policy_artifact_sha256"],
                row["vector_json"], row["vector_sha256"],
            )
            existing_vectors = {
                tuple(row)
                for row in conn.execute(
                    "SELECT order_intent_id,cycle_id,decision_id,training_fact_id,"
                    "vector_policy,target_price_id,quote_observed_at,quote_expires_at,"
                    "probability_sha256,official_ws_quote_sha256,inventory_sha256,"
                    "order_state_sha256,station_policy_sha256,cash_policy_decision_sha256,"
                    "strict_delta_artifact_sha256,economic_policy_schema,"
                    "economic_policy_artifact_sha256,vector_json,vector_sha256 "
                    "FROM basket_target_execution_version_vectors WHERE cycle_id=?",
                    (cycle["cycle_id"],),
                ).fetchall()
            }
            expected_vectors = {vector_signature(row) for row in vector_rows}
            if existing_vectors != expected_vectors:
                raise AtomicLedgerError("cycle_existing_version_vectors_drift")
            return {
                "schema": SCHEMA, "state": "already_committed", "idempotent": True,
                "cycle_id": cycle["cycle_id"], "decision_id": decision["decision_id"],
                "reservation_id": reservation["reservation_id"], "orders_submitted": 0,
                "places_orders": False, "calls_order_gateway": False,
            }

        # The atomic ledger, not only the resident worker, owns uniqueness of
        # an actionable no-live candidate.  A caller that bypasses the worker
        # must not append a second active prepared authority; it has to use
        # ``supersede_prepared_cycle_and_commit_bundle`` so retirement and the
        # successor commit share one transaction.
        training_fact_columns = (
            {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(basket_policy_training_fact)"
                ).fetchall()
            }
            if training_fact is not None
            else set()
        )
        existing_training_scope = (
            conn.execute(
                "SELECT 1 FROM basket_policy_training_fact "
                "WHERE decision_scope_key=? LIMIT 1",
                (str(training_fact.get("decision_scope_key") or ""),),
            ).fetchone()
            if training_fact is not None
            and "decision_scope_key" in training_fact_columns
            else None
        )
        if (
            decision.get("decided_state") == "prepared"
            and training_fact is not None
            and "decision_scope_key" in training_fact_columns
            and existing_training_scope is None
        ):
            conflicting_active = conn.execute(
                "SELECT c.cycle_id FROM basket_target_execution_cycles c "
                "JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
                "WHERE c.station_id=? AND c.event_date=? "
                "AND c.strategy_lane=? AND c.cycle_id<>? "
                "AND c.state IN "
                "('prepared','submitting','submitted','reconcile_required') "
                "AND d.decided_state<>'blocked' LIMIT 1",
                (
                    cycle["station_id"],
                    cycle["event_date"],
                    cycle["strategy_lane"],
                    cycle["cycle_id"],
                ),
            ).fetchone()
            if conflicting_active is not None:
                raise AtomicLedgerError(
                    "target_active_candidate_supersession_required"
                )

        if _assert_existing_or_absent(conn, "basket_target_observation_facts", "observation_id", obs["observation_id"], "content_sha256", obs["content_sha256"]):
            pass
        else:
            conn.execute(
                "INSERT INTO basket_target_observation_facts(observation_fact_id,observation_id,station_id,event_date,source_lane,report_type,raw_sha256,canonical_committed_at,content_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (obs["observation_fact_id"], obs["observation_id"], obs["station_id"], obs["event_date"], obs["source_lane"], obs["report_type"], obs["raw_sha256"], obs["canonical_committed_at"], obs["content_sha256"], created_at),
            )
        _fault(fault_after, "observation")
        if not _assert_existing_or_absent(conn, "basket_target_bound_facts", "bound_id", bound["bound_id"], "content_sha256", bound["content_sha256"]):
            conn.execute(
                "INSERT INTO basket_target_bound_facts(bound_fact_id,bound_id,observation_id,station_id,event_date,bound_generation,hard_lower_bound,bound_json,content_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (bound["bound_fact_id"], bound["bound_id"], bound["observation_id"], bound["station_id"], bound["event_date"], bound["bound_generation"], bound["hard_lower_bound"], canonical_json(bound["bound_json"]), bound["content_sha256"], created_at),
            )
        _fault(fault_after, "bounds")
        conn.execute(
            "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,plan_sha256,requested_reserve_usdc,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cycle["cycle_id"], cycle["plan_id"], cycle["station_id"], cycle["event_date"], cycle_timezone, cycle["strategy_lane"], cycle["plan_sha256"], cycle["requested_reserve_usdc"], "prepared", created_at, created_at),
        )
        _fault(fault_after, "cycle")
        conn.execute(
            "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,decided_state,will_order,target_execution_hard_deadline_at,reason_codes_json,details_json,decision_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision["decision_id"], decision["cycle_id"], decision["plan_id"], decision["station_id"], decision["event_date"], cycle_timezone, decision["strategy_lane"], decision["decided_state"], decision["will_order"], deadline, canonical_json(decision["reason_codes"]), canonical_json(decision["details"]), decision["decision_sha256"], created_at),
        )
        _fault(fault_after, "decision")
        for item in intents:
            conn.execute(
                "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,requested_notional_usdc,state,live_submit_authorized,target_execution_hard_deadline_at,idempotency_key,intent_json,intent_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["order_intent_id"], decision["decision_id"], cycle["cycle_id"], item["bucket"], item["token_id"], item["side"], item["order_action"], item["limit_price"], item["requested_size"], item["requested_notional_usdc"], item["state"], 0, item["target_execution_hard_deadline_at"], item["idempotency_key"], canonical_json(item["intent_json"]), item["intent_sha256"], created_at),
            )
        _fault(fault_after, "intent")
        for row in vector_rows:
            conn.execute(
                "INSERT INTO basket_target_execution_version_vectors("
                "order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
                "target_price_id,quote_observed_at,"
                "quote_expires_at,probability_sha256,official_ws_quote_sha256,inventory_sha256,"
                "order_state_sha256,station_policy_sha256,cash_policy_decision_sha256,"
                "strict_delta_artifact_sha256,economic_policy_schema,"
                "economic_policy_artifact_sha256,vector_json,vector_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["order_intent_id"], row["cycle_id"], row["decision_id"],
                    row["training_fact_id"], row["vector_policy"], row["target_price_id"],
                    row["quote_observed_at"], row["quote_expires_at"], row["probability_sha256"],
                    row["official_ws_quote_sha256"], row["inventory_sha256"],
                    row["order_state_sha256"], row["station_policy_sha256"],
                    row["cash_policy_decision_sha256"], row["strict_delta_artifact_sha256"],
                    row["economic_policy_schema"],
                    row["economic_policy_artifact_sha256"],
                    row["vector_json"], row["vector_sha256"], created_at,
                ),
            )
        _fault(fault_after, "version_vector")
        training_state = None
        if training_fact is not None:
            training_state = _insert_training_fact(conn, training_fact)
        _fault(fault_after, "training_fact")
        _reserve(conn, reservation, created_at=created_at)
        _fault(fault_after, "reserve")
    return {
        "schema": SCHEMA, "state": "committed", "idempotent": False,
        "cycle_id": cycle["cycle_id"], "decision_id": decision["decision_id"],
        "reservation_id": reservation["reservation_id"], "intent_count": len(intents),
        "reserved_amount_usdc": float(reservation["amount_usdc"]), "orders_submitted": 0,
        "places_orders": False, "calls_order_gateway": False,
        "execution_version_vector_count": len(vector_rows),
        "training_fact_state": training_state,
    }


def commit_blocked_scope_decision(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    reason_codes: Sequence[str],
    details: Mapping[str, Any] | None = None,
    created_at_utc: str,
    strategy_lane: str = "basket",
    _pre_event_preselection_id: str = "",
) -> dict[str, Any]:
    """Persist a scope blocker without inventing execution facts.

    This is the deliberately smaller companion to ``commit_target_atomic_bundle``
    for a decision point that cannot identify a committed P1 observation/bound
    or a complete market snapshot yet.  It writes exactly one execution cycle
    and its blocking ``auto_trade_decisions`` row with ``will_order=0``.  It
    does not write observation facts, bound facts, intents, reservations,
    inventory, quotes, or order events.  The identity is content-addressed, so
    repeated daemon polls of the same blocker are idempotent and a later
    blocker change can be represented by a new immutable decision.
    """

    status = target_ledger_schema_status(conn)
    if not status["ok"]:
        raise AtomicLedgerError("target_ledger_schema_incomplete")
    station = _text("blocked_station_id", station_id)
    day = _date("blocked_event_date", event_date)
    timezone_name = _text(
        "blocked_settlement_timezone", settlement_timezone
    )
    lane = _text("blocked_strategy_lane", strategy_lane).lower()
    if lane not in LANES:
        raise AtomicLedgerError("strategy_lane_invalid")
    reasons = _reason_codes(reason_codes)
    if not reasons:
        raise AtomicLedgerError("blocked_decision_reason_missing")
    payload_details = _mapping("blocked_decision_details", details or {})
    created_at = _timestamp("created_at_utc", created_at_utc)
    preselection_id = str(_pre_event_preselection_id or "").strip()
    if preselection_id:
        if lane != "basket":
            raise AtomicLedgerError(
                "preliminary_decision_strategy_lane_invalid"
            )
        if reasons != ["preliminary_no_live_p1_pending"]:
            raise AtomicLedgerError(
                "preliminary_decision_reason_invalid"
            )
        preselection = conn.execute(
            "SELECT station_id,event_date,checkpoint_id,probability_stage,"
            "quote_manifest_sha256,plan_sha256,state,final_authority,"
            "live_submit_authorized,calls_order_gateway,orders_submitted "
            "FROM basket_p0_preselection_plan WHERE preselection_id=?",
            (preselection_id,),
        ).fetchone()
        if preselection is None:
            raise AtomicLedgerError(
                "preliminary_decision_preselection_missing"
            )
        if (
            str(preselection[0]) != station
            or str(preselection[1]) != day
            or str(preselection[3]) not in {"P0", "P0-M"}
            or str(preselection[6]) != "PRELIMINARY_NO_LIVE"
            or str(preselection[7]) != "P1"
            or int(preselection[8]) != 0
            or int(preselection[9]) != 0
            or int(preselection[10]) != 0
        ):
            raise AtomicLedgerError(
                "preliminary_decision_preselection_contract_invalid"
            )
        expected_details = {
            "preliminary_decision_version": 1,
            "preselection_id": preselection_id,
            "checkpoint_id": str(preselection[2]),
            "probability_stage": str(preselection[3]),
            "quote_manifest_sha256": str(preselection[4]),
            "preselection_plan_sha256": str(preselection[5]),
            "final_authority": "P1",
            "capital_authorized": False,
            "intents_written": 0,
            "reservations_written": 0,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
        if payload_details != expected_details:
            raise AtomicLedgerError(
                "preliminary_decision_details_invalid"
            )

    plan_payload = {
        "blocked_scope_plan_version": 1,
        "station_id": station,
        "event_date": day,
        "settlement_timezone": timezone_name,
        "strategy_lane": lane,
        "reason_codes": reasons,
        "details": payload_details,
        "target_execution_hard_deadline_at": None,
    }
    plan_sha = _content_hash(PLAN_SCHEMA, plan_payload)
    plan_id = _derive_id("plan", plan_payload)
    cycle_identity = {
        "plan_id": plan_id,
        "station_id": station,
        "event_date": day,
        "settlement_timezone": timezone_name,
        "strategy_lane": lane,
    }
    cycle_id = _derive_id("cycle", cycle_identity)
    decision_identity = {
        "cycle_id": cycle_id,
        "plan_id": plan_id,
        "station_id": station,
        "event_date": day,
        "settlement_timezone": timezone_name,
        "strategy_lane": lane,
        "decided_state": "blocked",
        "will_order": 0,
        "reason_codes": reasons,
        "details": payload_details,
    }
    decision_sha = _content_hash(DECISION_SCHEMA, decision_identity)
    decision_id = _derive_id("decision", decision_identity)
    no_execution_policy = build_no_execution_version_policy(
        {
            "policy": "NO_EXECUTION",
            "reason_codes": reasons,
        }
    )
    no_execution_json = canonical_json(no_execution_policy.to_dict())

    with _atomic_transaction(conn, operation="commit_blocked_scope_decision"):
        existing_cycle = conn.execute(
            "SELECT plan_id,plan_sha256,requested_reserve_usdc,state,settlement_timezone "
            "FROM basket_target_execution_cycles WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()
        if existing_cycle is not None:
            if (
                str(existing_cycle[0]) != plan_id
                or str(existing_cycle[1]) != plan_sha
                or abs(float(existing_cycle[2] or 0.0)) > 1e-9
                or str(existing_cycle[4] or "") != timezone_name
            ):
                raise AtomicLedgerError("blocked_cycle_content_address_drift")
            existing_decision = conn.execute(
                "SELECT decision_id,decision_sha256,decided_state,will_order,settlement_timezone,target_execution_hard_deadline_at "
                "FROM auto_trade_decisions WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
            if existing_decision is None:
                raise AtomicLedgerError("blocked_cycle_decision_missing")
            if (
                str(existing_decision[0]) != decision_id
                or str(existing_decision[1]) != decision_sha
                or str(existing_decision[2]) != "blocked"
                or int(existing_decision[3]) != 0
                or str(existing_decision[4] or "") != timezone_name
                or existing_decision[5] is not None
            ):
                raise AtomicLedgerError("blocked_cycle_decision_content_address_drift")
            _assert_existing_blocked_no_execution_vector(
                conn,
                cycle_id=cycle_id,
                decision_id=decision_id,
                expected_policy=no_execution_policy,
            )
            return {
                "schema": SCHEMA,
                "state": "already_committed",
                "idempotent": True,
                "cycle_id": cycle_id,
                "decision_id": decision_id,
                "intent_count": 0,
                "reserved_amount_usdc": 0.0,
                "orders_submitted": 0,
                "places_orders": False,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "execution_version_vector_count": 1,
            }

        if preselection_id:
            try:
                day_state = event_day_window(
                    day,
                    as_of_utc=created_at,
                    target_timezone=timezone_name,
                )["state"]
            except Exception as exc:
                raise AtomicLedgerError(
                    "preliminary_decision_event_window_invalid"
                ) from exc
            if day_state == "closed":
                raise AtomicLedgerError(
                    "preliminary_decision_event_day_closed"
                )
        else:
            _assert_event_day_started(
                day,
                settlement_timezone=timezone_name,
                as_of_utc=created_at,
            )

        conflicting_plan = conn.execute(
            "SELECT cycle_id,plan_sha256 FROM basket_target_execution_cycles WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        if conflicting_plan is not None:
            raise AtomicLedgerError("blocked_plan_id_collision")
        conn.execute(
            "INSERT INTO basket_target_execution_cycles("
            "cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,plan_sha256,"
            "requested_reserve_usdc,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, plan_id, station, day, timezone_name, lane, plan_sha, 0.0, "prepared", created_at, created_at),
        )
        conn.execute(
            "INSERT INTO auto_trade_decisions("
            "decision_id,cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,"
            "decided_state,will_order,target_execution_hard_deadline_at,reason_codes_json,details_json,decision_sha256,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id, cycle_id, plan_id, station, day, timezone_name, lane, "blocked", 0,
                None, canonical_json(reasons), canonical_json(payload_details), decision_sha, created_at,
            ),
        )
        conn.execute(
            "INSERT INTO basket_target_execution_version_vectors("
            "order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
            "target_price_id,quote_observed_at,quote_expires_at,vector_json,"
            "vector_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                None,
                cycle_id,
                decision_id,
                None,
                "NO_EXECUTION",
                None,
                None,
                None,
                no_execution_json,
                no_execution_policy.policy_sha256,
                created_at,
            ),
        )
    return {
        "schema": SCHEMA,
        "state": "committed",
        "idempotent": False,
        "cycle_id": cycle_id,
        "decision_id": decision_id,
        "intent_count": 0,
        "reserved_amount_usdc": 0.0,
        "orders_submitted": 0,
        "places_orders": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "execution_version_vector_count": 1,
    }


def commit_preliminary_scope_decision(
    conn: sqlite3.Connection,
    *,
    preselection_id: str,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    checkpoint_id: str,
    probability_stage: str,
    quote_manifest_sha256: str,
    preselection_plan_sha256: str,
    created_at_utc: str,
) -> dict[str, Any]:
    """Materialize a P0/P0-M decision with no execution authority.

    The referenced append-only preselection is the sole authority for every
    identity below.  This permits a not-yet-started registry day to complete
    ``official WS -> preselection -> decision`` without weakening the formal
    P1/truth/hard-NO start-day gate or creating intents/capital reservations.
    """

    stage = _text("preliminary_probability_stage", probability_stage)
    if stage not in {"P0", "P0-M"}:
        raise AtomicLedgerError(
            "preliminary_decision_probability_stage_invalid"
        )
    details = {
        "preliminary_decision_version": 1,
        "preselection_id": _text(
            "preliminary_preselection_id", preselection_id
        ),
        "checkpoint_id": _text(
            "preliminary_checkpoint_id", checkpoint_id
        ),
        "probability_stage": stage,
        "quote_manifest_sha256": _sha(
            "preliminary_quote_manifest_sha256",
            quote_manifest_sha256,
        ),
        "preselection_plan_sha256": _sha(
            "preliminary_plan_sha256",
            preselection_plan_sha256,
        ),
        "final_authority": "P1",
        "capital_authorized": False,
        "intents_written": 0,
        "reservations_written": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return commit_blocked_scope_decision(
        conn,
        station_id=station_id,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        strategy_lane="basket",
        reason_codes=["preliminary_no_live_p1_pending"],
        details=details,
        created_at_utc=created_at_utc,
        _pre_event_preselection_id=str(preselection_id),
    )


_CYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"submitting", "reconcile_required"}),
    "submitting": frozenset({"submitted", "reconcile_required"}),
    "submitted": frozenset({"reconcile_required"}),
    "reconcile_required": frozenset(),
    "settled": frozenset(),
}


def transition_target_cycle_state(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    expected_state: str,
    new_state: str,
    updated_at_utc: str,
) -> dict[str, Any]:
    """Perform a compare-and-set lifecycle transition; no implicit jumps."""

    cycle = _text("cycle_id", cycle_id)
    expected = _text("expected_state", expected_state).lower()
    target = _text("new_state", new_state).lower()
    updated = _timestamp("updated_at_utc", updated_at_utc)
    if expected not in CYCLE_STATES or target not in CYCLE_STATES:
        raise AtomicLedgerError("cycle_state_invalid")
    if target not in _CYCLE_TRANSITIONS[expected]:
        raise AtomicLedgerError("cycle_state_transition_invalid")
    if expected == "prepared" and target == "submitting":
        raise AtomicLedgerError(
            "cycle_submission_requires_persisted_candidate_cas_boundary"
        )
    with _atomic_transaction(conn, operation="transition_target_cycle_state"):
        row = conn.execute("SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?", (cycle,)).fetchone()
        if row is None:
            raise AtomicLedgerError("cycle_missing")
        current = str(row[0])
        if current != expected:
            raise AtomicLedgerError("cycle_state_cas_conflict")
        conn.execute("UPDATE basket_target_execution_cycles SET state=?,updated_at=? WHERE cycle_id=? AND state=?", (target, updated, cycle, expected))
    return {"schema": SCHEMA, "state": "transitioned", "cycle_id": cycle, "previous_state": expected, "new_state": target}


def close_target_cycle(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    outcome: str,
    closed_at_utc: str,
    reason: str,
) -> dict[str, Any]:
    """Settle a cycle and release or consume its unique active reservation."""

    cycle = _text("cycle_id", cycle_id)
    outcome_value = _text("outcome", outcome).lower()
    if outcome_value not in {"released", "consumed"}:
        raise AtomicLedgerError("cycle_close_outcome_invalid")
    closed_at = _timestamp("closed_at_utc", closed_at_utc)
    close_reason = _text("close_reason", reason)
    with _atomic_transaction(conn, operation="close_target_cycle"):
        row = conn.execute("SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?", (cycle,)).fetchone()
        if row is None:
            raise AtomicLedgerError("cycle_missing")
        current = str(row[0])
        reservation = conn.execute(
            "SELECT reservation_id,amount_usdc,state FROM basket_target_capital_reservations WHERE cycle_id=?", (cycle,)
        ).fetchone()
        if current == "settled":
            if reservation is not None and str(reservation[2]) == "active":
                raise AtomicLedgerError("settled_cycle_active_reservation")
            if (
                reservation is None
                or str(reservation[2]) != outcome_value
            ):
                raise AtomicLedgerError("settled_cycle_outcome_drift")
            return {"schema": SCHEMA, "state": "already_settled", "cycle_id": cycle, "outcome": outcome_value}
        if current not in {"prepared", "submitted", "reconcile_required"}:
            raise AtomicLedgerError("cycle_close_state_invalid")
        if reservation is None:
            raise AtomicLedgerError("reservation_missing")
        reservation_id, amount, reservation_state = str(reservation[0]), float(reservation[1]), str(reservation[2])
        if reservation_state == "active":
            balance, reserved, _epoch, _version = _ensure_pool(conn)
            if amount > reserved + 1e-9:
                raise AtomicLedgerError("pool_reserved_balance_drift")
            if outcome_value == "consumed" and amount > balance + 1e-9:
                raise AtomicLedgerError("pool_balance_drift")
            if outcome_value == "consumed":
                conn.execute("UPDATE basket_target_capital_pool SET balance_usdc=balance_usdc-?,reserved_usdc=reserved_usdc-?,version=version+1,updated_at=? WHERE pool_id=1", (amount, amount, closed_at))
            else:
                conn.execute("UPDATE basket_target_capital_pool SET reserved_usdc=reserved_usdc-?,version=version+1,updated_at=? WHERE pool_id=1", (amount, closed_at))
            conn.execute("UPDATE basket_target_capital_reservations SET state=?,release_reason=?,updated_at=? WHERE reservation_id=? AND state='active'", (outcome_value, close_reason, closed_at, reservation_id))
        elif reservation_state not in {"released", "consumed", "expired"}:
            raise AtomicLedgerError("reservation_state_invalid")
        conn.execute("UPDATE basket_target_execution_cycles SET state='settled',updated_at=? WHERE cycle_id=?", (closed_at, cycle))
    return {"schema": SCHEMA, "state": "settled", "cycle_id": cycle, "outcome": outcome_value, "released_or_consumed_usdc": float(amount)}


def supersede_prepared_cycle_and_commit_bundle(
    conn: sqlite3.Connection,
    *,
    old_cycle_id: str,
    new_decision_scope_key: str,
    bundle: Mapping[str, Any],
    superseded_at_utc: str,
    fault_after: str | None = None,
) -> dict[str, Any]:
    """Atomically retire one inert no-live candidate and commit its successor.

    Supersession is permitted only while every old intent is still local
    ``prepared`` state, no execution/reconciliation fact exists, and the
    reservation remains active.  The immutable old decision and training fact
    are retained; an append-only receipt links them to the new authority
    scope.  Any failure in the new bundle rolls the entire retirement back.
    """

    cycle_id = _text("old_cycle_id", old_cycle_id)
    new_scope = _text("new_decision_scope_key", new_decision_scope_key)
    superseded_at = _timestamp("superseded_at_utc", superseded_at_utc)
    if bundle.get("schema") != SCHEMA:
        raise AtomicLedgerError("supersession_bundle_schema_invalid")
    new_cycle = _text(
        "supersession_new_cycle_id",
        (bundle.get("cycle") or {}).get("cycle_id")
        if isinstance(bundle.get("cycle"), Mapping)
        else None,
    )
    new_decision = _text(
        "supersession_new_decision_id",
        (bundle.get("decision") or {}).get("decision_id")
        if isinstance(bundle.get("decision"), Mapping)
        else None,
    )

    with _atomic_transaction(
        conn, operation="supersede_prepared_cycle_and_commit_bundle"
    ):
        existing = conn.execute(
            "SELECT new_cycle_id,new_decision_id,new_decision_scope_key,"
            "content_sha256 FROM basket_target_prepared_supersession_receipts "
            "WHERE old_cycle_id=?",
            (cycle_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing[0]) != new_cycle
                or str(existing[1]) != new_decision
                or str(existing[2]) != new_scope
            ):
                raise AtomicLedgerError("prepared_supersession_content_drift")
            committed = commit_target_atomic_bundle(conn, bundle=bundle)
            return {
                **committed,
                "supersession_state": "already_committed",
                "supersession_content_sha256": str(existing[3]),
                "superseded_cycle_id": cycle_id,
            }

        row = conn.execute(
            "SELECT c.state,d.decision_id,d.decided_state,d.will_order,"
            "f.fact_id,f.decision_scope_key "
            "FROM basket_target_execution_cycles c "
            "JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
            "JOIN basket_policy_training_fact f "
            "ON f.cycle_id=c.cycle_id AND f.decision_id=d.decision_id "
            "WHERE c.cycle_id=?",
            (cycle_id,),
        ).fetchall()
        if len(row) != 1:
            raise AtomicLedgerError(
                "prepared_supersession_authority_binding_invalid"
            )
        (
            cycle_state,
            old_decision_id,
            decision_state,
            will_order,
            old_fact_id,
            old_scope,
        ) = row[0]
        if str(cycle_state) != "prepared" or str(decision_state) != "prepared":
            raise AtomicLedgerError("prepared_supersession_state_invalid")
        if int(will_order) != 0:
            raise AtomicLedgerError("prepared_supersession_will_order_forbidden")
        if str(old_scope) == new_scope:
            raise AtomicLedgerError("prepared_supersession_scope_unchanged")

        intents = conn.execute(
            "SELECT order_intent_id,state,live_submit_authorized "
            "FROM basket_target_order_intents WHERE cycle_id=? "
            "ORDER BY order_intent_id",
            (cycle_id,),
        ).fetchall()
        if not intents:
            raise AtomicLedgerError("prepared_supersession_intent_missing")
        if any(
            str(item[1]) != "prepared" or int(item[2]) != 0
            for item in intents
        ):
            raise AtomicLedgerError("prepared_supersession_intent_not_quiescent")
        intent_ids = [str(item[0]) for item in intents]
        placeholders = ",".join("?" for _ in intent_ids)
        execution_tables = {
            str(item[0])
            for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        checks = [
            ("basket_target_order_events", "order_intent_id"),
            ("basket_target_order_reconciliations", "order_intent_id"),
            ("basket_target_execution_receipts", "order_intent_id"),
            ("basket_target_execution_transition_versions", "order_intent_id"),
            ("basket_target_post_fill_recompute_requests", "order_intent_id"),
        ]
        for table, column in checks:
            if table not in execution_tables:
                continue
            found = conn.execute(
                f"SELECT 1 FROM {table} WHERE {column} IN ({placeholders}) "
                "LIMIT 1",
                intent_ids,
            ).fetchone()
            if found is not None:
                raise AtomicLedgerError(
                    "prepared_supersession_execution_fact_present"
                )

        reservation = conn.execute(
            "SELECT reservation_id,amount_usdc,state "
            "FROM basket_target_capital_reservations WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()
        if reservation is None or str(reservation[2]) != "active":
            raise AtomicLedgerError(
                "prepared_supersession_reservation_not_active"
            )
        reservation_id = str(reservation[0])
        released_amount = float(reservation[1])
        _balance, reserved, _epoch, _version = _ensure_pool(conn)
        if released_amount > reserved + 1e-9:
            raise AtomicLedgerError("pool_reserved_balance_drift")

        cancelled_json = canonical_json(intent_ids)
        receipt_body = {
            "schema": "basket_target_prepared_supersession_receipt_v1",
            "old_cycle_id": cycle_id,
            "old_decision_id": str(old_decision_id),
            "old_training_fact_id": str(old_fact_id),
            "old_decision_scope_key": str(old_scope),
            "new_cycle_id": new_cycle,
            "new_decision_id": new_decision,
            "new_decision_scope_key": new_scope,
            "reservation_id": reservation_id,
            "released_amount_usdc": round(released_amount, 8),
            "cancelled_intent_ids": intent_ids,
            "reason": "authority_input_superseded",
            "superseded_at": superseded_at,
        }
        receipt_sha = content_sha256(receipt_body)
        supersession_id = _derive_id("supersession", receipt_body)

        conn.execute(
            f"UPDATE basket_target_order_intents SET state='cancelled' "
            f"WHERE order_intent_id IN ({placeholders}) AND state='prepared'",
            intent_ids,
        )
        if conn.execute("SELECT changes()").fetchone()[0] != len(intent_ids):
            raise AtomicLedgerError("prepared_supersession_intent_cas_conflict")
        conn.execute(
            "UPDATE basket_target_capital_reservations "
            "SET state='released',release_reason='authority_input_superseded',"
            "updated_at=? WHERE reservation_id=? AND state='active'",
            (superseded_at, reservation_id),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise AtomicLedgerError(
                "prepared_supersession_reservation_cas_conflict"
            )
        conn.execute(
            "UPDATE basket_target_capital_pool "
            "SET reserved_usdc=reserved_usdc-?,version=version+1,updated_at=? "
            "WHERE pool_id=1 AND reserved_usdc>=?",
            (released_amount, superseded_at, released_amount),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise AtomicLedgerError("prepared_supersession_pool_cas_conflict")
        conn.execute(
            "UPDATE basket_target_execution_cycles "
            "SET state='settled',updated_at=? "
            "WHERE cycle_id=? AND state='prepared'",
            (superseded_at, cycle_id),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise AtomicLedgerError("prepared_supersession_cycle_cas_conflict")
        _fault(fault_after, "supersede_old")

        committed = commit_target_atomic_bundle(
            conn,
            bundle=bundle,
            fault_after=(
                fault_after.removeprefix("new_")
                if isinstance(fault_after, str)
                and fault_after.startswith("new_")
                else None
            ),
        )
        conn.execute(
            "INSERT INTO basket_target_prepared_supersession_receipts("
            "supersession_id,old_cycle_id,old_decision_id,"
            "old_training_fact_id,old_decision_scope_key,new_cycle_id,"
            "new_decision_id,new_decision_scope_key,reservation_id,"
            "released_amount_usdc,cancelled_intent_ids_json,reason,"
            "superseded_at,receipt_json,content_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                supersession_id,
                cycle_id,
                str(old_decision_id),
                str(old_fact_id),
                str(old_scope),
                new_cycle,
                new_decision,
                new_scope,
                reservation_id,
                released_amount,
                cancelled_json,
                "authority_input_superseded",
                superseded_at,
                canonical_json(receipt_body),
                receipt_sha,
            ),
        )
        _fault(fault_after, "supersession_receipt")
    return {
        **committed,
        "supersession_state": "committed",
        "supersession_id": supersession_id,
        "supersession_content_sha256": receipt_sha,
        "superseded_cycle_id": cycle_id,
        "released_amount_usdc": released_amount,
        "cancelled_intent_count": len(intent_ids),
    }


def arbitrate_capital_requests(
    requests: Sequence[Mapping[str, Any]],
    *,
    available_usdc: float,
    aging_step: float = 10.0,
) -> dict[str, Any]:
    """Deterministically choose all-or-none requests from the shared pool.

    ``priority`` is a policy input, not a source or market quota.  Aging is
    bounded only by the supplied request values and eventually lets an older
    low-priority request outrank a newer one, which prevents starvation while
    preserving a stable tie break.
    """

    available = _number("available_usdc", available_usdc)
    step = _number("aging_step", aging_step)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(requests):
        request_id = _text(f"request_{index}_id", raw.get("request_id"))
        if request_id in seen:
            raise AtomicLedgerError("capital_request_duplicate")
        seen.add(request_id)
        lane = _text(f"request_{index}_lane", raw.get("strategy_lane")).lower()
        if lane not in LANES:
            raise AtomicLedgerError("capital_request_lane_invalid")
        amount = _number(f"request_{index}_amount", raw.get("amount_usdc"), positive=True)
        try:
            priority = float(raw.get("priority", 0.0))
            starvation = float(raw.get("starvation_count", 0.0))
        except (TypeError, ValueError) as exc:
            raise AtomicLedgerError("capital_request_priority_invalid") from exc
        if not math.isfinite(priority) or not math.isfinite(starvation) or priority < 0 or starvation < 0:
            raise AtomicLedgerError("capital_request_priority_invalid")
        requested_at = _timestamp(f"request_{index}_requested_at", raw.get("requested_at"))
        effective = priority + starvation * step
        normalized.append({
            "request_id": request_id, "strategy_lane": lane, "amount_usdc": amount,
            "priority": priority, "starvation_count": starvation, "requested_at": requested_at,
            "effective_priority": effective,
        })
    normalized.sort(key=lambda row: (-row["effective_priority"], row["requested_at"], row["request_id"]))
    remaining = available
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in normalized:
        if row["amount_usdc"] <= remaining + 1e-9:
            accepted.append({**row, "state": "accepted"})
            remaining -= row["amount_usdc"]
        else:
            rejected.append({**row, "state": "deferred_insufficient_capital"})
    return {
        "schema": "basket_target_capital_arbitration_v1",
        "accepted": accepted,
        "rejected": rejected,
        "available_usdc": available,
        "accepted_usdc": available - remaining,
        "remaining_usdc": remaining,
        "deterministic": True,
        "shared_pool": True,
        "starvation_protection": True,
    }


__all__ = [
    "AtomicLedgerError",
    "CYCLE_STATES",
    "InjectedAtomicFault",
    "INTENT_STATES",
    "LANES",
    "PRODUCTION_LEDGER_DDL",
    "SCHEMA",
    "TARGET_LEDGER_TABLES",
    "apply_target_ledger_schema",
    "arbitrate_capital_requests",
    "build_target_atomic_bundle",
    "close_target_cycle",
    "supersede_prepared_cycle_and_commit_bundle",
    "commit_blocked_scope_decision",
    "commit_preliminary_scope_decision",
    "commit_target_atomic_bundle",
    "initialize_capital_pool",
    "record_writer_lock_wait",
    "target_ledger_schema_status",
    "transition_target_cycle_state",
]
