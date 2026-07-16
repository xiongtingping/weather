"""Atomic production decision/intent writer for the dynamic three-leg path.

This boundary never calls CLOB.  It converts one claimed latest posterior
outbox plus one frozen transition/market snapshot into durable decisions and
execution intents.  Production activation is deliberately double opt-in.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from lineage_identity_runtime import lineage_id_from_parts
from dynamic_three_leg_position_lot_runtime import build_position_lot_identity
from dynamic_three_leg_production_activation import validate_dynamic_three_leg_activation
from trade_exit_reason_contract import exit_reason_family_for_code, is_executable_exit_reason_code


SCHEMA_VERSION = "production_dynamic_three_leg_decision_writer_v1"
ACTIVATION_SCHEMA = "dynamic_three_leg_production_activation_v1"
REQUIRED_AUTO_DECISION_COLUMNS = {
    "run_id", "run_date", "slug", "event_date", "outcome", "side", "action",
    "decided_state", "will_order", "reason_codes_json", "reasons_json",
    "details_json", "created_at", "correlation_id", "fact_snapshot_id",
    "posterior_run_id", "decision_id", "order_intent_id",
}


class ProductionDynamicThreeLegIntegrityError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_utc(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _env_enabled(name: str) -> bool:
    return _text(os.environ.get(name)).lower() in {"1", "true", "yes", "on"}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {_text(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _activation_blockers(activation: Mapping[str, Any], strategy_artifact_sha256: str) -> list[str]:
    try:
        value = validate_dynamic_three_leg_activation(_mapping(activation))
    except ValueError:
        return ["dynamic_three_leg_production_activation_contract_invalid"]
    blockers: list[str] = []
    if value.get("schema") != ACTIVATION_SCHEMA or value.get("state") != "enabled":
        blockers.append("dynamic_three_leg_production_activation_contract_invalid")
    if _text(value.get("strategy_artifact_sha256")) != _text(strategy_artifact_sha256):
        blockers.append("dynamic_three_leg_production_strategy_artifact_mismatch")
    if not _text(value.get("cutover_artifact_sha256")):
        blockers.append("dynamic_three_leg_production_cutover_artifact_missing")
    if value.get("live_decision_write_authorized") is not True:
        blockers.append("dynamic_three_leg_production_decision_write_not_authorized")
    if value.get("live_submit_authorized") is not True:
        blockers.append("dynamic_three_leg_production_submit_not_authorized")
    if not _env_enabled("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED"):
        blockers.append("dynamic_three_leg_production_environment_disabled")
    if not _env_enabled("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED"):
        blockers.append("dynamic_three_leg_live_submit_environment_disabled")
    return blockers


def _assert_production_schema(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "v2_shadow_runtime_manifest"):
        raise ProductionDynamicThreeLegIntegrityError("dynamic_three_leg_production_refuses_shadow_manifest")
    required = {
        "posterior_commit_outbox", "posterior_outbox_execution_contracts",
        "dynamic_three_leg_transition_plans", "dynamic_three_leg_risk_reservations",
        "posterior_execution_intents",
        "event_fact_history", "event_fact_latest", "auto_trade_decisions",
    }
    missing = sorted(table for table in required if not _table_exists(conn, table))
    if missing:
        raise ProductionDynamicThreeLegIntegrityError("dynamic_three_leg_production_tables_missing:" + ",".join(missing))
    missing_decision = sorted(REQUIRED_AUTO_DECISION_COLUMNS - _columns(conn, "auto_trade_decisions"))
    if missing_decision:
        raise ProductionDynamicThreeLegIntegrityError(
            "dynamic_three_leg_production_auto_decision_columns_missing:" + ",".join(missing_decision)
        )


def _market_rows(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    buckets = snapshot.get("buckets")
    return buckets if isinstance(buckets, Mapping) else {}


def _execution_rows(
    *,
    transition_plan: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    decision_as_of: str,
    hard_deadline_at: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    transition = _mapping(transition_plan)
    market = _mapping(market_snapshot)
    buckets = _market_rows(market)
    blockers: list[str] = []
    decision_time = _parse_utc(decision_as_of)
    market_as_of = _parse_utc(market.get("as_of"))
    market_expires = _parse_utc(market.get("expires_at"))
    hard_deadline = _parse_utc(hard_deadline_at)
    if not _text(market.get("market_snapshot_id")) or decision_time is None or market_as_of is None or market_expires is None:
        blockers.append("dynamic_three_leg_execution_market_snapshot_invalid")
    elif market_as_of > decision_time or decision_time > market_expires:
        blockers.append("dynamic_three_leg_execution_market_snapshot_not_current")
    if hard_deadline is None or decision_time is None or decision_time > hard_deadline:
        blockers.append("dynamic_three_leg_execution_observation_deadline_expired")
    phase = _text(transition.get("phase"))
    raw_rows = _rows(transition.get("sell_intents")) if phase == "SELL_OLD" else _rows(transition.get("buy_candidates")) if phase == "BUY_NEW" else []
    executable: list[dict[str, Any]] = []
    for raw in raw_rows:
        row = dict(raw)
        bucket = _text(row.get("bucket"))
        outcome_side = _text(row.get("side")).upper()
        quote = _mapping(buckets.get(bucket))
        action = "SELL" if phase == "SELL_OLD" else "BUY"
        price_field = ("yes_bid" if outcome_side == "YES" else "no_bid") if action == "SELL" else ("yes_ask" if outcome_side == "YES" else "no_ask")
        token_field = "yes_token_id" if outcome_side == "YES" else "no_token_id"
        price = _finite(quote.get(price_field))
        token_id = _text(row.get("token_id") or quote.get(token_field))
        if not bucket or outcome_side not in {"YES", "NO"} or price is None or not 0 < price < 1 or not token_id:
            blockers.append("dynamic_three_leg_execution_quote_or_token_missing")
            continue
        if row.get("token_id") and _text(row.get("token_id")) != _text(quote.get(token_field)):
            blockers.append("dynamic_three_leg_execution_position_market_token_mismatch")
            continue
        if action == "SELL":
            size = _finite(row.get("requested_sell_size"))
            notional = size * price if size is not None else None
            reason_family = _text(row.get("reason_family"))
            reason_code = _text(row.get("reason_code"))
            candidate_id = ""
        else:
            notional = _finite(row.get("notional"))
            size = notional / price if notional is not None else None
            reason_family = "strategy_leg"
            reason_code = _text(row.get("strategy_leg"))
            candidate_id = _text(row.get("candidate_id"))
        if size is None or size <= 0 or notional is None or notional <= 0 or not reason_family or not reason_code:
            blockers.append("dynamic_three_leg_execution_size_or_reason_invalid")
            continue
        if action == "SELL" and (
            exit_reason_family_for_code(reason_code) != reason_family
            or not is_executable_exit_reason_code(reason_code)
        ):
            blockers.append("dynamic_three_leg_execution_exit_reason_not_in_executable_closed_set")
            continue
        executable.append({
            **row,
            "candidate_id": candidate_id,
            "bucket": bucket,
            "outcome_side": outcome_side,
            "order_action": action,
            "token_id": token_id,
            "limit_price": price,
            "size": size,
            "notional": notional,
            "reason_family": reason_family,
            "reason_code": reason_code,
            "quote_expires_at": _text(market.get("expires_at")),
        })
    if phase in {"SELL_OLD", "BUY_NEW"} and not executable:
        blockers.append("dynamic_three_leg_execution_no_executable_rows")
    return executable, sorted(set(blockers))


def commit_production_dynamic_three_leg_decision_bundle(
    conn: sqlite3.Connection,
    *,
    outbox_event_id: str,
    lease_owner: str,
    claim_generation: int,
    slug: str,
    event_date: str,
    desired_trading_plan: Mapping[str, Any],
    transition_plan: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    production_activation: Mapping[str, Any],
    decision_as_of: str,
    fault_after: str = "",
) -> dict[str, Any]:
    """Atomically persist plan, decisions, intents, and outbox completion."""

    _assert_production_schema(conn)
    desired = _mapping(desired_trading_plan)
    transition = _mapping(transition_plan)
    strategy_artifact = _text(desired.get("strategy_artifact_sha256"))
    blockers = _activation_blockers(production_activation, strategy_artifact)
    transition_id = _text(transition.get("transition_plan_id"))
    transition_sha = _sha256(transition)
    existing = conn.execute(
        "SELECT transition_sha256,state FROM dynamic_three_leg_transition_plans WHERE transition_plan_id=?",
        (transition_id,),
    ).fetchone() if transition_id else None
    if existing is not None:
        if _text(existing[0]) != transition_sha:
            raise ProductionDynamicThreeLegIntegrityError("dynamic_three_leg_production_transition_replay_drift")
        completed = conn.execute(
            "SELECT state FROM posterior_commit_outbox WHERE outbox_event_id=?",
            (_text(outbox_event_id),),
        ).fetchone()
        if completed is None or _text(completed[0]) != "completed":
            raise ProductionDynamicThreeLegIntegrityError("dynamic_three_leg_production_existing_plan_outbox_inconsistent")
        intents = int(conn.execute("SELECT COUNT(*) FROM posterior_execution_intents WHERE transition_plan_id=?", (transition_id,)).fetchone()[0])
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "already_committed", "idempotent": True, "intent_count": intents, "places_orders": False}
    outbox = conn.execute(
        """
        SELECT o.posterior_run_id,o.fact_snapshot_id,o.correlation_id,o.state,o.lease_owner,o.claim_generation,
               x.observation_to_order_hard_deadline_at,x.execution_eligible_at_commit
        FROM posterior_commit_outbox o
        JOIN posterior_outbox_execution_contracts x ON x.outbox_event_id=o.outbox_event_id AND x.posterior_run_id=o.posterior_run_id
        WHERE o.outbox_event_id=?
        """,
        (_text(outbox_event_id),),
    ).fetchone()
    if outbox is None:
        blockers.append("dynamic_three_leg_production_outbox_missing")
    else:
        if _text(outbox[3]) != "claimed" or _text(outbox[4]) != _text(lease_owner) or int(outbox[5] or 0) != int(claim_generation):
            blockers.append("dynamic_three_leg_production_outbox_claim_not_current")
        if int(outbox[7] or 0) != 1:
            blockers.append("dynamic_three_leg_production_outbox_ineligible_at_commit")
        if _text(outbox[0]) != _text(desired.get("posterior_run_id")) or _text(outbox[0]) != _text(transition.get("posterior_run_id")):
            blockers.append("dynamic_three_leg_production_posterior_binding_mismatch")
        if _text(outbox[1]) != _text(desired.get("fact_snapshot_id")):
            blockers.append("dynamic_three_leg_production_fact_binding_mismatch")
    if desired.get("schema") != "dynamic_three_leg_trading_plan_v1":
        blockers.append("dynamic_three_leg_production_desired_plan_schema_invalid")
    required_plan_bindings = (
        "market_snapshot_id", "capital_snapshot_id", "risk_snapshot_id",
        "capital_epoch_id", "risk_reservation_id", "second_yes_entry_window_state_id",
    )
    if any(not _text(desired.get(field)) for field in required_plan_bindings):
        blockers.append("dynamic_three_leg_production_capital_or_risk_binding_missing")
    if _text(desired.get("market_snapshot_id")) != _text(market_snapshot.get("market_snapshot_id")):
        blockers.append("dynamic_three_leg_production_market_snapshot_binding_mismatch")
    if transition.get("ok") is not True or _text(transition.get("trading_plan_id")) != _text(desired.get("trading_plan_id")):
        blockers.append("dynamic_three_leg_production_transition_invalid")
    if _text(transition.get("strategy_artifact_sha256")) != strategy_artifact:
        blockers.append("dynamic_three_leg_production_transition_artifact_mismatch")
    if any(_text(transition.get(field)) != _text(desired.get(field)) for field in required_plan_bindings):
        blockers.append("dynamic_three_leg_production_transition_capital_or_risk_binding_mismatch")
    phase = _text(transition.get("phase"))
    sell_rows = _rows(transition.get("sell_intents"))
    buy_rows = _rows(transition.get("buy_candidates"))
    if phase == "SELL_OLD" and (not sell_rows or buy_rows):
        blockers.append("dynamic_three_leg_production_sell_first_invariant_broken")
    elif phase == "BUY_NEW" and (not buy_rows or sell_rows):
        blockers.append("dynamic_three_leg_production_buy_phase_invariant_broken")
    elif phase == "HOLD" and (sell_rows or buy_rows):
        blockers.append("dynamic_three_leg_production_hold_phase_invariant_broken")
    elif phase not in {"SELL_OLD", "BUY_NEW", "HOLD"}:
        blockers.append("dynamic_three_leg_production_transition_phase_invalid")
    latest = conn.execute(
        """SELECT h.posterior_run_id,h.fact_snapshot_id FROM event_fact_latest l
           JOIN event_fact_history h ON h.id=l.fact_id WHERE l.slug=? AND l.event_date=?""",
        (_text(slug), _text(event_date)),
    ).fetchone()
    if latest is None or outbox is None or _text(latest[0]) != _text(outbox[0]) or _text(latest[1]) != _text(outbox[1]):
        blockers.append("dynamic_three_leg_production_outbox_not_latest_ssot")
    executable, execution_blockers = _execution_rows(
        transition_plan=transition,
        market_snapshot=market_snapshot,
        decision_as_of=decision_as_of,
        hard_deadline_at=_text(outbox[6]) if outbox is not None else "",
    )
    blockers.extend(execution_blockers)
    decision_time = _parse_utc(decision_as_of)
    second_window = _mapping(desired.get("second_yes_entry_window_state"))
    second_window_start = _parse_utc(
        second_window.get("entry_window_start_at") or second_window.get("entry_window_opens_at")
    )
    second_window_end = _parse_utc(
        second_window.get("entry_window_end_at")
        or second_window.get("entry_window_closes_at")
        or second_window.get("predicted_peak_at")
    )
    if any(_text(row.get("strategy_leg")) == "second_yes" for row in executable):
        if (
            second_window.get("schema") != "second_yes_entry_window_state_v1"
            or _text(second_window.get("second_yes_entry_window_state_id"))
            != _text(desired.get("second_yes_entry_window_state_id"))
            or second_window.get("entry_allowed") is not True
            or decision_time is None
            or second_window_start is None
            or second_window_end is None
            or not (second_window_start <= decision_time < second_window_end)
        ):
            blockers.append("dynamic_three_leg_second_yes_execution_window_not_open")
        else:
            for row in executable:
                if _text(row.get("strategy_leg")) != "second_yes":
                    continue
                quote_expiry = _parse_utc(row.get("quote_expires_at"))
                if quote_expiry is None:
                    blockers.append("dynamic_three_leg_second_yes_quote_expiry_invalid")
                else:
                    row["quote_expires_at"] = _iso_utc(min(quote_expiry, second_window_end))
    reservation = conn.execute(
        """SELECT trading_plan_id,posterior_run_id,capital_snapshot_id,risk_snapshot_id,
                  capital_epoch_id,strategy_artifact_sha256,reserved_buy_notional,
                  state,bound_transition_plan_id,expires_at
           FROM dynamic_three_leg_risk_reservations WHERE risk_reservation_id=?""",
        (_text(desired.get("risk_reservation_id")),),
    ).fetchone()
    if reservation is None:
        blockers.append("dynamic_three_leg_production_risk_reservation_missing")
    else:
        expected_reservation_bindings = (
            _text(desired.get("trading_plan_id")), _text(desired.get("posterior_run_id")),
            _text(desired.get("capital_snapshot_id")), _text(desired.get("risk_snapshot_id")),
            _text(desired.get("capital_epoch_id")),
        )
        if tuple(_text(value) for value in reservation[:5]) != expected_reservation_bindings or _text(reservation[5]) != strategy_artifact:
            blockers.append("dynamic_three_leg_production_risk_reservation_binding_mismatch")
        if _text(reservation[7]) != "active" or _text(reservation[8]):
            blockers.append("dynamic_three_leg_production_risk_reservation_not_active")
        reservation_expiry = _parse_utc(reservation[9])
        if decision_time is None or reservation_expiry is None or decision_time > reservation_expiry:
            blockers.append("dynamic_three_leg_production_risk_reservation_expired")
        required_buy_notional = sum(
            float(row["notional"]) for row in executable if _text(row.get("order_action")) == "BUY"
        )
        reserved_buy_notional = _finite(reservation[6])
        if reserved_buy_notional is None or reserved_buy_notional + 1e-9 < required_buy_notional:
            blockers.append("dynamic_three_leg_production_risk_reservation_insufficient")
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(blockers)), "wrote_rows": False, "places_orders": False}

    decisions: list[dict[str, Any]] = []
    intents: list[dict[str, Any]] = []
    hard_deadline = _text(outbox[6])
    for default_sequence, row in enumerate(executable, start=1):
        decision_id = lineage_id_from_parts("decision_id", {"transition_plan_id": transition_id, "action": row["order_action"], "candidate_id": row.get("candidate_id"), "lot": row.get("position_lot_id"), "lot_version": row.get("position_lot_version")})
        order_intent_id = lineage_id_from_parts("order_intent_id", {"decision_id": decision_id, "deadline": hard_deadline})
        if row["order_action"] == "BUY":
            lot_id, lot_version = build_position_lot_identity(
                order_intent_id=order_intent_id,
                bucket=row["bucket"],
                outcome_side=row["outcome_side"],
                token_id=row["token_id"],
            )
            row["position_lot_id"] = lot_id
            row["position_lot_version"] = lot_version
        execution_sequence = int(row.get("execution_sequence") or default_sequence)
        idempotency_key = "dynamic-three-leg:" + hashlib.sha256(_json({"order_intent_id": order_intent_id, "token_id": row["token_id"], "action": row["order_action"], "price": row["limit_price"], "size": row["size"]}).encode("utf-8")).hexdigest()
        intent_payload = {
            "schema": "dynamic_three_leg_execution_intent_v1",
            "order_intent_id": order_intent_id,
            "idempotency_key": idempotency_key,
            "transition_plan_id": transition_id,
            "trading_plan_id": desired.get("trading_plan_id"),
            "posterior_run_id": outbox[0],
            "fact_snapshot_id": outbox[1],
            "position_snapshot_id": transition.get("position_snapshot_id"),
            "position_lot_id": row.get("position_lot_id"),
            "position_lot_version": row.get("position_lot_version"),
            "strategy_artifact_sha256": strategy_artifact,
            "market_snapshot_id": desired.get("market_snapshot_id"),
            "capital_snapshot_id": desired.get("capital_snapshot_id"),
            "risk_snapshot_id": desired.get("risk_snapshot_id"),
            "capital_epoch_id": desired.get("capital_epoch_id"),
            "risk_reservation_id": desired.get("risk_reservation_id"),
            "slug": _text(slug), "event_date": _text(event_date),
            "bucket": row["bucket"], "outcome_side": row["outcome_side"],
            "token_id": row["token_id"], "side": row["order_action"],
            "price": row["limit_price"], "size": row["size"], "order_type": "GTC",
            "notional": row["notional"],
            "expected_token_balance": row.get("expected_token_balance"),
            # Frozen marketable limit: execute immediately only inside the
            # committed quote; never reprice or cross beyond limit_price.
            "post_only": False, "direct_target_order_context": True,
            "direct_target_exit_order": row["order_action"] == "SELL",
            "observation_to_order_hard_deadline_at": hard_deadline,
            "quote_expires_at": row["quote_expires_at"],
            "reason_family": row["reason_family"], "reason_code": row["reason_code"],
            "execution_sequence": execution_sequence,
        }
        if _text(row.get("strategy_leg")) == "second_yes":
            intent_payload.update({
                "strategy_leg": "second_yes",
                "entry_allowed": True,
                "second_yes_entry_window_state": dict(
                    _mapping(desired.get("second_yes_entry_window_state"))
                ),
            })
        decisions.append({"decision_id": decision_id, "order_intent_id": order_intent_id, "row": row, "details": intent_payload})
        intents.append({
            "order_intent_id": order_intent_id, "decision_id": decision_id,
            "transition_plan_id": transition_id, "trading_plan_id": _text(desired.get("trading_plan_id")),
            "posterior_run_id": _text(outbox[0]), "realization_state_id": _text(desired.get("realization_state_id")),
            "fact_snapshot_id": _text(outbox[1]), "position_snapshot_id": _text(transition.get("position_snapshot_id")),
            "position_lot_id": _text(row.get("position_lot_id")) or None, "position_lot_version": _text(row.get("position_lot_version")) or None,
            "strategy_artifact_sha256": strategy_artifact, "candidate_id": _text(row.get("candidate_id")) or None,
            "market_snapshot_id": _text(desired.get("market_snapshot_id")), "capital_snapshot_id": _text(desired.get("capital_snapshot_id")),
            "risk_snapshot_id": _text(desired.get("risk_snapshot_id")), "capital_epoch_id": _text(desired.get("capital_epoch_id")),
            "risk_reservation_id": _text(desired.get("risk_reservation_id")),
            "bucket": row["bucket"], "outcome_side": row["outcome_side"], "order_action": row["order_action"],
            "token_id": row["token_id"], "limit_price": float(row["limit_price"]), "size": float(row["size"]), "notional": float(row["notional"]),
            "reason_family": row["reason_family"], "reason_code": row["reason_code"], "state": "prepared", "live_submit_authorized": 1,
            "observation_to_order_hard_deadline_at": hard_deadline, "idempotency_key": idempotency_key,
            "quote_expires_at": row["quote_expires_at"],
            "intent_json": _json(intent_payload), "intent_sha256": _sha256(intent_payload),
            "created_at": _text(decision_as_of), "updated_at": _text(decision_as_of),
        })

    # A closed second-YES peak window is an authoritative no-order decision,
    # not an omitted candidate.  Persist it alongside any executable top-YES
    # or safe-NO rows so UI and audit consumers never have to infer why the
    # second leg did not buy.  Retention is deliberately not an execution
    # intent: an existing same-bucket position may remain, while no new or
    # incremental BUY is authorized.
    for raw in _rows(desired.get("retention_candidates")):
        row = dict(raw)
        if _text(row.get("strategy_leg")) != "second_yes":
            continue
        reason_code = _text(
            row.get("entry_block_reason")
            or second_window.get("entry_block_reason")
            or "second_yes_entry_window_closed"
        )
        decision_id = lineage_id_from_parts(
            "decision_id",
            {
                "transition_plan_id": transition_id,
                "action": "HOLD",
                "candidate_id": row.get("candidate_id"),
                "reason_code": reason_code,
            },
        )
        decisions.append({
            "decision_id": decision_id,
            "order_intent_id": "",
            "row": {
                "bucket": _text(row.get("bucket")),
                "outcome_side": _text(row.get("side")).upper(),
                "order_action": "HOLD",
                "reason_family": "strategy_state",
                "reason_code": reason_code,
            },
            "details": {
                "schema": "dynamic_three_leg_no_order_decision_v1",
                "transition_plan_id": transition_id,
                "trading_plan_id": desired.get("trading_plan_id"),
                "posterior_run_id": outbox[0],
                "fact_snapshot_id": outbox[1],
                "realization_state_id": desired.get("realization_state_id"),
                "candidate_id": row.get("candidate_id"),
                "strategy_leg": "second_yes",
                "state": "hold_no_order",
                "will_order": False,
                "entry_allowed": False,
                "entry_block_reason": reason_code,
                "second_yes_entry_window_state": dict(second_window),
                "same_bucket_existing_position_may_be_retained": True,
                "uses_ev_edge_lcb": False,
            },
        })

    if _text(transition.get("phase")) == "HOLD":
        decision_id = lineage_id_from_parts("decision_id", {"transition_plan_id": transition_id, "action": "HOLD"})
        decisions.append({
            "decision_id": decision_id,
            "order_intent_id": "",
            "row": {
                "bucket": "",
                "outcome_side": "",
                "order_action": "HOLD",
                "reason_family": "strategy_state",
                "reason_code": "dynamic_three_leg_target_allocation_already_satisfied",
            },
            "details": {
                "schema": "dynamic_three_leg_no_order_decision_v1",
                "transition_plan_id": transition_id,
                "posterior_run_id": outbox[0],
                "fact_snapshot_id": outbox[1],
                "state": "hold_no_order",
            },
        })

    conn.execute("SAVEPOINT production_dynamic_three_leg_decision_tx")
    try:
        plan_row = {
            "transition_plan_id": transition_id, "trading_plan_id": _text(desired.get("trading_plan_id")),
            "posterior_run_id": _text(outbox[0]), "realization_state_id": _text(desired.get("realization_state_id")),
            "fact_snapshot_id": _text(outbox[1]), "position_snapshot_id": _text(transition.get("position_snapshot_id")),
            "strategy_artifact_sha256": strategy_artifact, "phase": _text(transition.get("phase")),
            "market_snapshot_id": _text(desired.get("market_snapshot_id")), "capital_snapshot_id": _text(desired.get("capital_snapshot_id")),
            "risk_snapshot_id": _text(desired.get("risk_snapshot_id")), "capital_epoch_id": _text(desired.get("capital_epoch_id")),
            "risk_reservation_id": _text(desired.get("risk_reservation_id")),
            "transition_json": _json(transition), "transition_sha256": transition_sha,
            "state": "completed" if _text(transition.get("phase")) == "HOLD" else "prepared",
            "created_at": _text(decision_as_of), "updated_at": _text(decision_as_of),
        }
        fields = list(plan_row)
        conn.execute(f"INSERT INTO dynamic_three_leg_transition_plans({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", tuple(plan_row[field] for field in fields))
        reservation_state = "bound" if _text(transition.get("phase")) == "BUY_NEW" else "released"
        conn.execute(
            """UPDATE dynamic_three_leg_risk_reservations
               SET state=?,bound_transition_plan_id=?,updated_at=?
               WHERE risk_reservation_id=? AND state='active' AND bound_transition_plan_id IS NULL""",
            (reservation_state, transition_id, _text(decision_as_of), _text(desired.get("risk_reservation_id"))),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise ProductionDynamicThreeLegIntegrityError("dynamic_three_leg_production_risk_reservation_bind_cas_failed")
        if fault_after == "plan":
            raise RuntimeError("production_dynamic_three_leg_fault_after_plan")
        for decision in decisions:
            row = decision["row"]
            will_order = 0 if row["order_action"] == "HOLD" else 1
            conn.execute(
                """INSERT INTO auto_trade_decisions(run_id,run_date,slug,event_date,outcome,side,action,decided_state,will_order,reason_codes_json,reasons_json,details_json,created_at,correlation_id,fact_snapshot_id,posterior_run_id,decision_id,order_intent_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_text(outbox[0]), _text(event_date), _text(slug), _text(event_date), row["bucket"], row["outcome_side"], "hold" if row["order_action"] == "HOLD" else "exit" if row["order_action"] == "SELL" else "entry", "hold_no_order" if not will_order else "intent_prepared", will_order, _json([row["reason_code"]]), _json([{"reason_family": row["reason_family"], "reason_code": row["reason_code"]}]), _json(decision["details"]), _text(decision_as_of), _text(outbox[2]), _text(outbox[1]), _text(outbox[0]), decision["decision_id"], decision["order_intent_id"] or None),
            )
        if fault_after == "decisions":
            raise RuntimeError("production_dynamic_three_leg_fault_after_decisions")
        for intent in intents:
            fields = list(intent)
            conn.execute(f"INSERT INTO posterior_execution_intents({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", tuple(intent[field] for field in fields))
        if fault_after == "intents":
            raise RuntimeError("production_dynamic_three_leg_fault_after_intents")
        conn.execute(
            """UPDATE posterior_commit_outbox SET state='completed',completed_at=?,completed_decision_id=?,lease_owner=NULL,lease_until=NULL
               WHERE outbox_event_id=? AND state='claimed' AND lease_owner=? AND claim_generation=?""",
            (_text(decision_as_of), decisions[0]["decision_id"], _text(outbox_event_id), _text(lease_owner), int(claim_generation)),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise ProductionDynamicThreeLegIntegrityError("dynamic_three_leg_production_outbox_completion_cas_failed")
    except Exception:
        conn.execute("ROLLBACK TO production_dynamic_three_leg_decision_tx")
        conn.execute("RELEASE production_dynamic_three_leg_decision_tx")
        raise
    conn.execute("RELEASE production_dynamic_three_leg_decision_tx")
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "committed", "idempotent": False, "transition_plan_id": transition_id, "decision_ids": [row["decision_id"] for row in decisions], "order_intent_ids": [row["order_intent_id"] for row in intents], "places_orders": False}


__all__ = [
    "ACTIVATION_SCHEMA",
    "SCHEMA_VERSION",
    "ProductionDynamicThreeLegIntegrityError",
    "commit_production_dynamic_three_leg_decision_bundle",
]
