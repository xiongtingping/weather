"""Dedicated resident submitter for prepared dynamic-three-leg intents.

It is not a generic queue.  The submitter performs one CAS from ``prepared``
to ``submitting`` before calling the already-durable local CLOB gateway.  A
crash or ambiguous response is reconciled by idempotency key and is never
blindly resubmitted.
"""

from __future__ import annotations

import json
import math
import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from trade_exit_reason_contract import exit_reason_family_for_code, is_executable_exit_reason_code
from market_alpha_take_profit_activation import (
    load_market_alpha_take_profit_activation,
    validate_market_alpha_take_profit_activation,
)
from dynamic_three_leg_position_lot_runtime import apply_reconciled_fill_to_position_lot


SCHEMA_VERSION = "resident_dynamic_three_leg_intent_submitter_v1"
RECONCILE_SCHEMA_VERSION = "resident_dynamic_three_leg_intent_reconcile_v1"
SUPERSESSION_SCHEMA_VERSION = "dynamic_three_leg_prepared_supersession_v1"
SUPERSESSION_REASON = "dynamic_three_leg_superseded_by_latest_posterior"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _enabled(name: str) -> bool:
    return _text(os.environ.get(name)).lower() in {"1", "true", "yes", "on"}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_text(table),)
    ).fetchone())


def _load_intent(conn: sqlite3.Connection, order_intent_id: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM posterior_execution_intents WHERE order_intent_id=?",
        (_text(order_intent_id),),
    ).fetchone()


def _require_clean_transaction(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The resident owns a dedicated connection; hidden outer transactions are unsafe.

    The pre-gateway ``submitting`` claim must be visible to a second process
    before any HTTP call starts.  That cannot be guaranteed when a caller has
    wrapped the resident in a larger transaction, so fail closed instead of
    pretending a savepoint is durable.
    """

    if conn.in_transaction:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "dynamic_three_leg_submitter_connection_has_open_transaction",
            "called_gateway": False,
        }
    return None


def _position_lot_binding_current(conn: sqlite3.Connection, row: Mapping[str, Any]) -> bool:
    if not _table_exists(conn, "dynamic_three_leg_position_lots"):
        return False
    lot_id = _text(row.get("position_lot_id"))
    version = _text(row.get("position_lot_version"))
    if not lot_id or not version:
        return False
    lot = conn.execute(
        """SELECT position_lot_version,bucket,outcome_side,token_id,remaining_size,state
             FROM dynamic_three_leg_position_lots WHERE position_lot_id=?""",
        (lot_id,),
    ).fetchone()
    if _text(row.get("order_action")) == "BUY":
        return lot is None
    try:
        requested_size = float(row.get("size"))
    except (TypeError, ValueError):
        return False
    return bool(
        lot is not None
        and _text(lot[0]) == version
        and _text(lot[1]) == _text(row.get("bucket"))
        and _text(lot[2]) == _text(row.get("outcome_side"))
        and _text(lot[3]) == _text(row.get("token_id"))
        and float(lot[4] or 0.0) + 1e-9 >= requested_size
        and _text(lot[5]) == "open"
    )


def _durable_update(conn: sqlite3.Connection, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
    """Commit one small ledger transition without committing caller-owned work."""

    if conn.in_transaction:
        raise RuntimeError("dynamic_three_leg_submitter_connection_has_open_transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for sql, parameters in statements:
            conn.execute(sql, parameters)
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def supersede_stale_prepared_dynamic_three_leg_intents(
    conn: sqlite3.Connection, *, superseded_at: str,
) -> dict[str, Any]:
    """Retire old-posterior prepared plans without touching exchange-side effects.

    The resident calls this only after the global reconcile-first check.  This
    function independently re-proves that every affected transition has no
    in-flight intent before changing intent, decision, transition and risk
    state in one SQLite transaction.
    """

    if conn.in_transaction:
        return {
            "schema": SUPERSESSION_SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["dynamic_three_leg_supersession_requires_clean_transaction"],
            "wrote_rows": False,
            "places_orders": False,
        }
    required = {
        "posterior_execution_intents", "auto_trade_decisions",
        "dynamic_three_leg_transition_plans", "dynamic_three_leg_risk_reservations",
        "event_fact_history", "event_fact_latest",
    }
    missing = sorted(table for table in required if not _table_exists(conn, table))
    if missing:
        return {
            "schema": SUPERSESSION_SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": [f"dynamic_three_leg_supersession_table_missing:{table}" for table in missing],
            "wrote_rows": False,
            "places_orders": False,
        }

    conn.execute("BEGIN IMMEDIATE")
    try:
        prepared = conn.execute(
            """SELECT order_intent_id,decision_id,transition_plan_id,posterior_run_id,
                      fact_snapshot_id,intent_json,exchange_post_started_at,
                      expected_order_id,exchange_order_id
                 FROM posterior_execution_intents
                WHERE state='prepared' AND reason_family<>'market_alpha_take_profit'
                ORDER BY created_at,order_intent_id"""
        ).fetchall()
        stale_by_transition: dict[str, list[tuple[str, str]]] = {}
        prepared_by_transition: dict[str, set[str]] = {}
        blockers: list[str] = []
        for row in prepared:
            intent_id, decision_id, transition_id = map(_text, row[:3])
            prepared_by_transition.setdefault(transition_id, set()).add(intent_id)
            try:
                payload = json.loads(_text(row[5]))
            except (TypeError, ValueError):
                payload = {}
            slug = _text(_mapping(payload).get("slug"))
            event_date = _text(_mapping(payload).get("event_date"))[:10]
            no_exchange_side_effect = not any(_text(value) for value in row[6:9])
            if not all((intent_id, decision_id, transition_id, slug, event_date)) or not no_exchange_side_effect:
                blockers.append(f"dynamic_three_leg_prepared_identity_unproven:{intent_id or 'missing'}")
                continue
            latest = conn.execute(
                """SELECT h.posterior_run_id,h.fact_snapshot_id
                     FROM event_fact_latest l
                     JOIN event_fact_history h ON h.id=l.fact_id
                    WHERE l.slug=? AND l.event_date=?""",
                (slug, event_date),
            ).fetchone()
            if latest is None:
                blockers.append(f"dynamic_three_leg_latest_ssot_missing:{intent_id}")
                continue
            if (_text(latest[0]), _text(latest[1])) != (_text(row[3]), _text(row[4])):
                stale_by_transition.setdefault(transition_id, []).append((intent_id, decision_id))

        stale_transition_ids = sorted(stale_by_transition)
        for transition_id in stale_transition_ids:
            stale_ids = {intent_id for intent_id, _decision_id in stale_by_transition[transition_id]}
            if stale_ids != prepared_by_transition.get(transition_id, set()):
                blockers.append(f"dynamic_three_leg_transition_mixed_posterior_prepared:{transition_id}")
            in_flight = conn.execute(
                """SELECT 1 FROM posterior_execution_intents
                    WHERE transition_plan_id=?
                      AND state IN ('submitting','gateway_accepted','reconcile_required','partially_filled')
                    LIMIT 1""",
                (transition_id,),
            ).fetchone()
            if in_flight is not None:
                blockers.append(f"dynamic_three_leg_transition_requires_reconcile:{transition_id}")
        if blockers:
            conn.rollback()
            return {
                "schema": SUPERSESSION_SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": sorted(set(blockers)),
                "superseded_transition_plan_ids": [],
                "superseded_order_intent_ids": [],
                "wrote_rows": False,
                "places_orders": False,
            }

        superseded_intent_ids: list[str] = []
        for transition_id in stale_transition_ids:
            transition = conn.execute(
                """SELECT phase,state,risk_reservation_id,posterior_run_id,fact_snapshot_id
                     FROM dynamic_three_leg_transition_plans
                    WHERE transition_plan_id=?""",
                (transition_id,),
            ).fetchone()
            transition_table = "dynamic_three_leg_transition_plans"
            if transition is None and _table_exists(conn, "dynamic_three_leg_continuation_transition_plans"):
                transition = conn.execute(
                    """SELECT phase,state,risk_reservation_id,posterior_run_id,fact_snapshot_id
                         FROM dynamic_three_leg_continuation_transition_plans
                        WHERE transition_plan_id=?""",
                    (transition_id,),
                ).fetchone()
                transition_table = "dynamic_three_leg_continuation_transition_plans"
            if transition is None or _text(transition[1]) != "prepared":
                raise RuntimeError("dynamic_three_leg_supersession_transition_not_prepared")
            intents = conn.execute(
                """SELECT order_intent_id,decision_id,state,posterior_run_id,fact_snapshot_id
                     FROM posterior_execution_intents WHERE transition_plan_id=?""",
                (transition_id,),
            ).fetchall()
            if not intents or any(
                (_text(row[3]), _text(row[4])) != (_text(transition[3]), _text(transition[4]))
                for row in intents
            ):
                raise RuntimeError("dynamic_three_leg_supersession_transition_identity_drift")
            any_fill = any(_text(row[2]) == "filled" for row in intents)
            if _table_exists(conn, "dynamic_three_leg_position_lot_events"):
                any_fill = any_fill or bool(conn.execute(
                    """SELECT 1
                         FROM dynamic_three_leg_position_lot_events e
                         JOIN posterior_execution_intents i
                           ON i.order_intent_id=e.order_intent_id
                        WHERE i.transition_plan_id=? AND e.applied_size>0 LIMIT 1""",
                    (transition_id,),
                ).fetchone())
            for intent_id, decision_id in stale_by_transition[transition_id]:
                changed_intent = conn.execute(
                    """UPDATE posterior_execution_intents
                          SET state='cancelled_superseded',last_error=?,updated_at=?
                        WHERE order_intent_id=? AND state='prepared'
                          AND exchange_post_started_at IS NULL
                          AND expected_order_id IS NULL AND exchange_order_id IS NULL""",
                    (SUPERSESSION_REASON, _text(superseded_at), intent_id),
                ).rowcount
                changed_decision = conn.execute(
                    """UPDATE auto_trade_decisions
                          SET decided_state='cancelled_superseded',will_order=0
                        WHERE decision_id=?""",
                    (decision_id,),
                ).rowcount
                if (changed_intent, changed_decision) != (1, 1):
                    raise RuntimeError("dynamic_three_leg_prepared_supersession_cas_failed")
                superseded_intent_ids.append(intent_id)
            changed_transition = conn.execute(
                f"""UPDATE {transition_table} SET state='superseded',updated_at=?
                      WHERE transition_plan_id=? AND state='prepared'""",
                (_text(superseded_at), transition_id),
            ).rowcount
            reservation_state = (
                "consumed" if _text(transition[0]) == "BUY_NEW" and any_fill
                else "superseded" if _text(transition[0]) == "BUY_NEW"
                else "released"
            )
            changed_reservation = conn.execute(
                """UPDATE dynamic_three_leg_risk_reservations
                      SET state=?,updated_at=?
                    WHERE risk_reservation_id=? AND bound_transition_plan_id=?
                      AND state IN ('active','bound','released')""",
                (reservation_state, _text(superseded_at), _text(transition[2]), transition_id),
            ).rowcount
            if (changed_transition, changed_reservation) != (1, 1):
                raise RuntimeError("dynamic_three_leg_transition_supersession_cas_failed")
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return {
        "schema": SUPERSESSION_SCHEMA_VERSION,
        "ok": True,
        "state": "superseded" if stale_transition_ids else "idle_current",
        "reason": SUPERSESSION_REASON if stale_transition_ids else None,
        "superseded_transition_plan_ids": stale_transition_ids,
        "superseded_order_intent_ids": superseded_intent_ids,
        "wrote_rows": bool(stale_transition_ids),
        "places_orders": False,
    }


def _finalize_transition_lifecycle(
    conn: sqlite3.Connection, transition_plan_id: str, at: str,
) -> dict[str, Any]:
    """Close transition/risk state only from durable intent terminal states."""

    transition = conn.execute(
        """SELECT phase,state,risk_reservation_id,posterior_run_id,fact_snapshot_id,1
             FROM dynamic_three_leg_transition_plans WHERE transition_plan_id=?""",
        (_text(transition_plan_id),),
    ).fetchone()
    transition_table = "dynamic_three_leg_transition_plans"
    if transition is None and _table_exists(conn, "dynamic_three_leg_continuation_transition_plans"):
        transition = conn.execute(
            """SELECT phase,state,risk_reservation_id,posterior_run_id,fact_snapshot_id,
                      transition_generation
                 FROM dynamic_three_leg_continuation_transition_plans
                WHERE transition_plan_id=?""",
            (_text(transition_plan_id),),
        ).fetchone()
        transition_table = "dynamic_three_leg_continuation_transition_plans"
    if transition is None:
        if not _table_exists(conn, "market_alpha_take_profit_cycles"):
            return {"changed": False, "state": "missing"}
        cycle = conn.execute(
            "SELECT state,order_intent_id FROM market_alpha_take_profit_cycles WHERE market_alpha_cycle_id=?",
            (_text(transition_plan_id),),
        ).fetchone()
        if cycle is None:
            return {"changed": False, "state": "missing"}
        intent = conn.execute(
            "SELECT state FROM posterior_execution_intents WHERE order_intent_id=?",
            (_text(cycle[1]),),
        ).fetchone()
        intent_state = _text(intent[0]) if intent else ""
        if intent_state == "filled":
            next_state = "completed"
        elif intent_state in {"failed", "expired", "cancelled_superseded"}:
            next_state = "expired" if intent_state == "expired" else "blocked"
        else:
            return {"changed": False, "state": _text(cycle[0]), "market_alpha": True}
        _durable_update(conn, [(
            "UPDATE market_alpha_take_profit_cycles SET state=?,updated_at=? WHERE market_alpha_cycle_id=?",
            (next_state, _text(at), _text(transition_plan_id)),
        )])
        return {"changed": True, "state": next_state, "market_alpha": True}
    phase, current_state, risk_id = map(_text, transition[:3])
    posterior_run_id, fact_snapshot_id = _text(transition[3]), _text(transition[4])
    transition_generation = int(transition[5])
    rows = conn.execute(
        "SELECT order_intent_id,decision_id,state FROM posterior_execution_intents WHERE transition_plan_id=?",
        (_text(transition_plan_id),),
    ).fetchall()
    states = [_text(row[2]) for row in rows]
    failure_states = {"failed", "expired", "cancelled_superseded"}
    any_failure = any(state in failure_states for state in states)
    all_filled = bool(states) and all(state == "filled" for state in states)
    if not any_failure and not all_filled:
        return {"changed": False, "state": current_state}
    next_state = "completed" if all_filled else "blocked"
    any_terminal_filled = any(state == "filled" for state in states)
    any_durable_buy_fill = False
    if _table_exists(conn, "dynamic_three_leg_position_lot_events"):
        any_durable_buy_fill = bool(conn.execute(
            """SELECT 1
                 FROM dynamic_three_leg_position_lot_events e
                 JOIN posterior_execution_intents i ON i.order_intent_id=e.order_intent_id
                WHERE i.transition_plan_id=? AND e.order_action='BUY' AND e.applied_size>0
                LIMIT 1""",
            (_text(transition_plan_id),),
        ).fetchone())
    reservation_state = (
        "consumed"
        if phase == "BUY_NEW" and (all_filled or any_terminal_filled or any_durable_buy_fill)
        else "released"
    )
    if conn.in_transaction:
        raise RuntimeError("dynamic_three_leg_submitter_connection_has_open_transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if any_failure:
            pending = conn.execute(
                "SELECT order_intent_id,decision_id FROM posterior_execution_intents WHERE transition_plan_id=? AND state='prepared'",
                (_text(transition_plan_id),),
            ).fetchall()
            for order_intent_id, decision_id in pending:
                conn.execute(
                    "UPDATE posterior_execution_intents SET state='cancelled_superseded',last_error=?,updated_at=? WHERE order_intent_id=? AND state='prepared'",
                    ("dynamic_three_leg_transition_terminal_sibling_failure", _text(at), _text(order_intent_id)),
                )
                conn.execute(
                    "UPDATE auto_trade_decisions SET decided_state='cancelled_superseded',will_order=0 WHERE decision_id=?",
                    (_text(decision_id),),
                )
        conn.execute(
            f"UPDATE {transition_table} SET state=?,updated_at=? WHERE transition_plan_id=? AND state='prepared'",
            (next_state, _text(at), _text(transition_plan_id)),
        )
        conn.execute(
            """UPDATE dynamic_three_leg_risk_reservations SET state=?,updated_at=?
               WHERE risk_reservation_id=? AND bound_transition_plan_id=? AND state IN ('bound','released')""",
            (reservation_state, _text(at), risk_id, _text(transition_plan_id)),
        )
        if (
            next_state == "completed"
            and phase == "SELL_OLD"
            and _table_exists(conn, "dynamic_three_leg_continuation_events")
        ):
            first_intent = conn.execute(
                "SELECT intent_json FROM posterior_execution_intents WHERE transition_plan_id=? ORDER BY created_at,order_intent_id LIMIT 1",
                (_text(transition_plan_id),),
            ).fetchone()
            try:
                intent_payload = json.loads(_text(first_intent[0]) if first_intent else "{}")
            except (TypeError, ValueError):
                intent_payload = {}
            slug = _text(_mapping(intent_payload).get("slug"))
            event_date = _text(_mapping(intent_payload).get("event_date"))
            if not slug or not event_date:
                raise RuntimeError("dynamic_three_leg_continuation_event_identity_missing")
            next_generation = transition_generation + 1
            continuation_id = "continuation_" + hashlib.sha256(
                _json({
                    "posterior_run_id": posterior_run_id,
                    "previous_transition_plan_id": _text(transition_plan_id),
                    "transition_generation": next_generation,
                }).encode("utf-8")
            ).hexdigest()[:24]
            conn.execute(
                """INSERT OR IGNORE INTO dynamic_three_leg_continuation_events(
                   continuation_event_id,posterior_run_id,fact_snapshot_id,slug,event_date,
                   previous_transition_plan_id,transition_generation,state,claim_generation,
                   created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,'pending',0,?,?)""",
                (
                    continuation_id, posterior_run_id, fact_snapshot_id, slug, event_date,
                    _text(transition_plan_id), next_generation, _text(at), _text(at),
                ),
            )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return {"changed": True, "state": next_state, "risk_reservation_state": reservation_state}


def submit_prepared_dynamic_three_leg_intent(
    conn: sqlite3.Connection,
    *,
    order_intent_id: str,
    submit_as_of: str,
    pre_submit_validator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    gateway_place_order: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    market_alpha_activation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit one exact durable intent; never creates or reprices an intent."""

    if not _enabled("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED") or not _enabled("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED"):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_resident_submitter_disabled", "called_gateway": False}
    transaction_issue = _require_clean_transaction(conn)
    if transaction_issue:
        return transaction_issue
    now = _parse_utc(submit_as_of)
    row = _load_intent(conn, order_intent_id)
    if row is None:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_execution_intent_missing", "called_gateway": False}
    state = _text(row["state"])
    if state in {"gateway_accepted", "filled"}:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": state, "idempotent": True, "called_gateway": False}
    if state in {"submitting", "reconcile_required", "partially_filled"}:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "reconcile_required", "reason": "dynamic_three_leg_existing_submission_requires_reconcile", "idempotency_key": _text(row["idempotency_key"]), "called_gateway": False}
    if state != "prepared" or int(row["live_submit_authorized"] or 0) != 1:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_execution_intent_not_submit_ready", "called_gateway": False}
    reason_family = _text(row["reason_family"])
    reason_code = _text(row["reason_code"])
    try:
        intent_payload = json.loads(_text(row["intent_json"]))
    except (TypeError, ValueError):
        intent_payload = {}
    is_market_alpha = bool(
        _text(row["order_action"]) == "SELL"
        and reason_family == "market_alpha_take_profit"
        and reason_code == "market_alpha_take_profit_top_yes_wave_completed"
    )
    authorized_market_alpha: Mapping[str, Any] = {}
    if is_market_alpha:
        try:
            authorized_market_alpha = validate_market_alpha_take_profit_activation(
                market_alpha_activation or load_market_alpha_take_profit_activation()
            )
        except (OSError, ValueError) as exc:
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": f"market_alpha_submit_activation_rejected:{exc}", "called_gateway": False}
        if (
            _text(authorized_market_alpha.get("strategy_artifact_sha256")) != _text(row["strategy_artifact_sha256"])
            or _text(authorized_market_alpha.get("activation_contract_sha256")) != _text(_mapping(intent_payload).get("activation_contract_sha256"))
            or not _enabled("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED")
            or not _enabled("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED")
        ):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "market_alpha_submit_activation_binding_or_environment_invalid", "called_gateway": False}
    elif _text(row["order_action"]) == "SELL" and (
        exit_reason_family_for_code(reason_code) != reason_family
        or not is_executable_exit_reason_code(reason_code)
    ):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_exit_reason_not_in_executable_closed_set", "called_gateway": False}
    hard_deadline = _parse_utc(row["observation_to_order_hard_deadline_at"])
    quote_expiry = _parse_utc(row["quote_expires_at"])
    second_yes_peak: datetime | None = None
    if _text(row["order_action"]) == "BUY" and reason_family == "strategy_leg" and reason_code == "second_yes":
        second_window = _mapping(_mapping(intent_payload).get("second_yes_entry_window_state"))
        second_yes_peak = _parse_utc(
            second_window.get("entry_window_end_at")
            or second_window.get("entry_window_closes_at")
            or second_window.get("predicted_peak_at")
        )
        if (
            _text(intent_payload.get("strategy_leg")) != "second_yes"
            or intent_payload.get("entry_allowed") is not True
            or second_window.get("schema") != "second_yes_entry_window_state_v1"
            or second_yes_peak is None
        ):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_second_yes_submit_window_binding_invalid", "called_gateway": False}
    expired_reason = "dynamic_three_leg_submit_deadline_or_quote_expired"
    expired = bool(now is None or hard_deadline is None or quote_expiry is None or now > hard_deadline or now > quote_expiry)
    if second_yes_peak is not None and (now is None or now >= second_yes_peak):
        expired = True
        expired_reason = "second_yes_entry_window_elapsed_before_submit"
    if expired:
        _durable_update(conn, [
            (
                "UPDATE posterior_execution_intents SET state='expired',last_error=?,updated_at=? WHERE order_intent_id=? AND state='prepared'",
                (expired_reason, _text(submit_as_of), _text(order_intent_id)),
            ),
            (
                "UPDATE auto_trade_decisions SET decided_state='expired',will_order=0 WHERE decision_id=?",
                (_text(row["decision_id"]),),
            ),
        ])
        lifecycle = _finalize_transition_lifecycle(conn, _text(row["transition_plan_id"]), _text(submit_as_of))
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "expired", "reason": expired_reason, "called_gateway": False, "transition_lifecycle": lifecycle}

    latest = conn.execute(
        """SELECT h.posterior_run_id,h.fact_snapshot_id FROM event_fact_latest l
           JOIN event_fact_history h ON h.id=l.fact_id
           WHERE h.posterior_run_id=? AND h.fact_snapshot_id=? AND l.fact_id=h.id""",
        (_text(row["posterior_run_id"]), _text(row["fact_snapshot_id"])),
    ).fetchone()
    if is_market_alpha:
        cycle = conn.execute(
            """SELECT state,position_snapshot_id,position_lot_id,position_lot_version,
                      strategy_artifact_sha256,activation_contract_sha256
               FROM market_alpha_take_profit_cycles WHERE market_alpha_cycle_id=?""",
            (_text(row["transition_plan_id"]),),
        ).fetchone() if _table_exists(conn, "market_alpha_take_profit_cycles") else None
        cycle_valid = bool(
            cycle is not None
            and _text(cycle[0]) == "prepared"
            and _text(cycle[1]) == _text(row["position_snapshot_id"])
            and _text(cycle[2]) == _text(row["position_lot_id"])
            and _text(cycle[3]) == _text(row["position_lot_version"])
            and _text(cycle[4]) == _text(row["strategy_artifact_sha256"])
            and _text(cycle[5]) == _text(authorized_market_alpha.get("activation_contract_sha256"))
        )
        if latest is None or not cycle_valid:
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "market_alpha_pre_submit_ssot_or_cycle_stale", "called_gateway": False}
    else:
        transition = conn.execute(
            "SELECT phase,state,position_snapshot_id,risk_reservation_id FROM dynamic_three_leg_transition_plans WHERE transition_plan_id=?",
            (_text(row["transition_plan_id"]),),
        ).fetchone()
        if transition is None and _table_exists(conn, "dynamic_three_leg_continuation_transition_plans"):
            transition = conn.execute(
                """SELECT phase,state,position_snapshot_id,risk_reservation_id
                     FROM dynamic_three_leg_continuation_transition_plans
                    WHERE transition_plan_id=?""",
                (_text(row["transition_plan_id"]),),
            ).fetchone()
        reservation = conn.execute(
            """SELECT state,bound_transition_plan_id,expires_at
               FROM dynamic_three_leg_risk_reservations WHERE risk_reservation_id=?""",
            (_text(row["risk_reservation_id"]),),
        ).fetchone()
        expected_phase = "SELL_OLD" if _text(row["order_action"]) == "SELL" else "BUY_NEW"
        reservation_expiry = _parse_utc(reservation[2]) if reservation is not None else None
        reservation_valid = bool(
            reservation is not None
            and _text(reservation[0]) == ("bound" if expected_phase == "BUY_NEW" else "released")
            and _text(reservation[1]) == _text(row["transition_plan_id"])
            and (
                expected_phase == "SELL_OLD"
                or (reservation_expiry is not None and now is not None and now <= reservation_expiry)
            )
        )
        if latest is None or transition is None or not reservation_valid or _text(transition[0]) != expected_phase or _text(transition[1]) != "prepared" or _text(transition[2]) != _text(row["position_snapshot_id"]) or _text(transition[3]) != _text(row["risk_reservation_id"]):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_pre_submit_ssot_or_transition_stale", "called_gateway": False}
    if not _position_lot_binding_current(conn, dict(row)):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_pre_submit_position_lot_stale", "called_gateway": False}
    try:
        validation = dict(pre_submit_validator(dict(row)))
    except Exception as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_pre_submit_validator_exception", "exception_type": type(exc).__name__, "called_gateway": False}
    # Durable risk is proved above from the execution SSOT.  The external CLOB
    # snapshot must prove only exchange state and cannot self-authorize risk.
    validation["risk_reservation_active"] = True
    validation["risk_reservation_source"] = "dynamic_three_leg_risk_reservations"
    required_flags = ("position_lot_current", "market_quote_current", "balance_sufficient")
    if validation.get("ok") is not True or any(validation.get(flag) is not True for flag in required_flags):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_pre_submit_validation_failed", "validation": validation, "called_gateway": False}

    conn.execute("BEGIN IMMEDIATE")
    try:
        changed = conn.execute(
            "UPDATE posterior_execution_intents SET state='submitting',updated_at=? WHERE order_intent_id=? AND state='prepared' AND live_submit_authorized=1",
            (_text(submit_as_of), _text(order_intent_id)),
        ).rowcount
        if changed != 1:
            raise RuntimeError("dynamic_three_leg_submit_claim_cas_failed")
        conn.execute(
            "UPDATE auto_trade_decisions SET decided_state='submitting' WHERE decision_id=?",
            (_text(row["decision_id"]),),
        )
        if is_market_alpha:
            changed_cycle = conn.execute(
                "UPDATE market_alpha_take_profit_cycles SET state='submitting',updated_at=? WHERE market_alpha_cycle_id=? AND state='prepared'",
                (_text(submit_as_of), _text(row["transition_plan_id"])),
            ).rowcount
            if changed_cycle != 1:
                raise RuntimeError("market_alpha_submit_cycle_claim_cas_failed")
    except Exception:
        conn.rollback()
        raise
    conn.commit()

    payload = json.loads(_text(row["intent_json"]))
    try:
        response = dict(gateway_place_order(payload))
    except Exception as exc:
        response = {"ok": False, "reconcile_required": True, "error": "gateway_exception_after_submitting_cas", "error_detail": str(exc)}
    exchange_post_started_at = _text(response.get("exchange_post_started_at")) if response.get("exchange_post_started") is True else ""
    exchange_order_id = _text(response.get("orderID") or response.get("orderId"))
    expected_order_id = _text(response.get("expected_order_id"))
    if response.get("ok") is True and (exchange_order_id or response.get("accepted") is True):
        terminal_state = "gateway_accepted"
        decision_state = "gateway_accepted"
        ok = True
    elif response.get("reconcile_required") is True or response.get("pending") is True or exchange_post_started_at:
        terminal_state = "reconcile_required"
        decision_state = "reconcile_required"
        ok = False
    else:
        terminal_state = "failed"
        decision_state = "submit_failed"
        ok = False
    terminal_statements = [
        (
            """UPDATE posterior_execution_intents
               SET state=?,gateway_response_json=?,exchange_post_started_at=?,expected_order_id=?,exchange_order_id=?,last_error=?,updated_at=?
               WHERE order_intent_id=? AND state='submitting'""",
            (terminal_state, _json(response), exchange_post_started_at or None, expected_order_id or None, exchange_order_id or None, _text(response.get("error")) or None, _text(submit_as_of), _text(order_intent_id)),
        ),
        (
            "UPDATE auto_trade_decisions SET decided_state=?,will_order=? WHERE decision_id=?",
            (decision_state, 1 if terminal_state in {"gateway_accepted", "reconcile_required"} else 0, _text(row["decision_id"])),
        ),
    ]
    if is_market_alpha:
        cycle_state = terminal_state if terminal_state in {"gateway_accepted", "reconcile_required"} else "blocked"
        terminal_statements.append((
            "UPDATE market_alpha_take_profit_cycles SET state=?,updated_at=? WHERE market_alpha_cycle_id=? AND state='submitting'",
            (cycle_state, _text(submit_as_of), _text(row["transition_plan_id"])),
        ))
    _durable_update(conn, terminal_statements)
    lifecycle = _finalize_transition_lifecycle(conn, _text(row["transition_plan_id"]), _text(submit_as_of))
    return {"schema": SCHEMA_VERSION, "ok": ok, "state": terminal_state, "idempotency_key": _text(row["idempotency_key"]), "exchange_post_started_at": exchange_post_started_at, "expected_order_id": expected_order_id, "exchange_order_id": exchange_order_id, "called_gateway": True, "response": response, "transition_lifecycle": lifecycle}


def reconcile_dynamic_three_leg_intent(
    conn: sqlite3.Connection,
    *,
    order_intent_id: str,
    reconcile_as_of: str,
    gateway_lookup_order: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Reconcile one previously claimed intent; this function never submits."""

    if not _enabled("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED") or not _enabled("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED"):
        return {"schema": RECONCILE_SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_resident_submitter_disabled", "called_gateway": False, "submitted_order": False}
    transaction_issue = _require_clean_transaction(conn)
    if transaction_issue:
        return {**transaction_issue, "schema": RECONCILE_SCHEMA_VERSION, "submitted_order": False}
    row = _load_intent(conn, order_intent_id)
    if row is None:
        return {"schema": RECONCILE_SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_execution_intent_missing", "called_gateway": False, "submitted_order": False}
    current_state = _text(row["state"])
    if current_state == "filled":
        return {"schema": RECONCILE_SCHEMA_VERSION, "ok": True, "state": "filled", "idempotent": True, "called_gateway": False, "submitted_order": False}
    if current_state not in {"submitting", "reconcile_required", "gateway_accepted", "partially_filled"}:
        return {"schema": RECONCILE_SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_intent_state_not_reconcilable", "called_gateway": False, "submitted_order": False}

    lookup = {
        "order_intent_id": _text(row["order_intent_id"]),
        "idempotency_key": _text(row["idempotency_key"]),
        "expected_order_id": _text(row["expected_order_id"]),
        "exchange_order_id": _text(row["exchange_order_id"]),
        "token_id": _text(row["token_id"]),
    }
    try:
        response = dict(gateway_lookup_order(lookup))
    except Exception as exc:
        response = {"ok": False, "status": "unknown", "error": "gateway_reconcile_exception", "error_detail": str(exc)}

    response_key = _text(response.get("idempotency_key"))
    response_order_id = _text(response.get("orderID") or response.get("orderId") or response.get("exchange_order_id"))
    response_token_id = _text(response.get("token_id") or response.get("asset_id"))
    known_order_ids = {_text(row["expected_order_id"]), _text(row["exchange_order_id"])} - {""}
    identity_matches = bool(
        (response_key and response_key == _text(row["idempotency_key"]))
        or (response_order_id and response_order_id in known_order_ids)
    )
    if response_token_id and response_token_id != _text(row["token_id"]):
        identity_matches = False
    status = _text(response.get("status")).lower()
    original_size_value = _finite(response.get("original_size"))
    size_matched_value = _finite(response.get("size_matched"))
    original_size = original_size_value if original_size_value is not None else 0.0
    size_matched = size_matched_value if size_matched_value is not None else 0.0
    intent_size = float(row["size"])
    fill_bearing_statuses = {
        "filled", "matched", "complete", "completed", "open", "live",
        "accepted", "pending", "partially_filled", "partial", "partially-filled",
    }
    size_fields_present = original_size_value is not None or size_matched_value is not None
    size_evidence_valid = bool(
        original_size_value is not None
        and size_matched_value is not None
        and original_size > 0
        and abs(original_size - intent_size) <= 1e-6
        and size_matched >= -1e-9
        and size_matched <= original_size + 1e-6
    )
    if status in fill_bearing_statuses or size_fields_present:
        if not size_evidence_valid:
            identity_matches = False
        response_price = response.get("price")
        if response_price is not None:
            parsed_response_price = _finite(response_price)
            if (
                parsed_response_price is None
                or abs(parsed_response_price - float(row["limit_price"])) > 1e-9
            ):
                identity_matches = False
    if not identity_matches:
        next_state, decision_state, ok = "reconcile_required", "reconcile_required", False
        error = "gateway_reconcile_identity_unproven"
    elif status in {"filled", "matched", "complete", "completed"} and size_matched + 1e-6 >= original_size:
        next_state, decision_state, ok = "filled", "filled", True
        error = ""
    elif status in {"open", "live", "accepted", "pending"} and original_size > 0 and 0 < size_matched < original_size:
        next_state, decision_state, ok = "partially_filled", "partially_filled", True
        error = ""
    elif status in {"open", "live", "accepted", "pending"}:
        next_state, decision_state, ok = "gateway_accepted", "gateway_accepted", True
        error = ""
    elif status in {"partially_filled", "partial", "partially-filled"}:
        next_state, decision_state, ok = "partially_filled", "partially_filled", True
        error = ""
    elif status in {"cancelled", "canceled", "rejected", "failed"}:
        next_state, decision_state, ok = "failed", "submit_failed", False
        error = f"gateway_reconciled_{status}"
    else:
        # "not found" is not proof of no exchange side effect after an
        # ambiguous POST.  Keep reconciling and never create a replacement.
        next_state, decision_state, ok = "reconcile_required", "reconcile_required", False
        error = "gateway_reconcile_nonterminal_or_not_found"

    exchange_order_id = response_order_id or _text(row["exchange_order_id"])
    is_market_alpha = (
        _text(row["reason_family"]) == "market_alpha_take_profit"
        and _text(row["reason_code"]) == "market_alpha_take_profit_top_yes_wave_completed"
        and _table_exists(conn, "market_alpha_take_profit_cycles")
    )
    cycle_state = ""
    if is_market_alpha:
        cycle_state = (
            "completed" if next_state == "filled"
            else "blocked" if next_state == "failed"
            else next_state
        )
    cumulative_for_lot = (
        max(0.0, min(size_matched, original_size))
        if identity_matches and size_evidence_valid
        else 0.0
    )
    if next_state == "filled" and cumulative_for_lot <= 0:
        cumulative_for_lot = float(row["size"])
    # getOrder.price is the frozen order limit, not a cumulative fill VWAP.
    # Position cost accounting therefore uses the durable intent price.  An
    # exchange price field, when present, was identity-checked above.
    fill_price = float(row["limit_price"])
    lot_result: dict[str, Any] | None = None
    if conn.in_transaction:
        raise RuntimeError("dynamic_three_leg_submitter_connection_has_open_transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if cumulative_for_lot > 0:
            lot_result = apply_reconciled_fill_to_position_lot(
                conn,
                intent=dict(row),
                cumulative_filled_size=cumulative_for_lot,
                fill_price=fill_price,
                applied_at=_text(reconcile_as_of),
            )
            if lot_result.get("ok") is not True:
                raise RuntimeError(_text(lot_result.get("reason")) or "dynamic_position_lot_fill_apply_blocked")
        conn.execute(
            """UPDATE posterior_execution_intents
               SET state=?,gateway_response_json=?,exchange_order_id=?,last_error=?,updated_at=?
               WHERE order_intent_id=? AND state IN ('submitting','reconcile_required','gateway_accepted','partially_filled')""",
            (next_state, _json(response), exchange_order_id or None, error or None, _text(reconcile_as_of), _text(order_intent_id)),
        )
        conn.execute(
            "UPDATE auto_trade_decisions SET decided_state=?,will_order=? WHERE decision_id=?",
            (decision_state, 0 if next_state == "failed" else 1, _text(row["decision_id"])),
        )
        if is_market_alpha:
            conn.execute(
                "UPDATE market_alpha_take_profit_cycles SET state=?,updated_at=? WHERE market_alpha_cycle_id=?",
                (cycle_state, _text(reconcile_as_of), _text(row["transition_plan_id"])),
            )
    except Exception as exc:
        conn.rollback()
        return {
            "schema": RECONCILE_SCHEMA_VERSION,
            "ok": False,
            "state": "reconcile_required",
            "reason": "dynamic_position_lot_atomic_reconcile_blocked",
            "detail": str(exc),
            "called_gateway": True,
            "submitted_order": False,
            "response": response,
        }
    conn.commit()
    lifecycle = _finalize_transition_lifecycle(conn, _text(row["transition_plan_id"]), _text(reconcile_as_of))
    return {
        "schema": RECONCILE_SCHEMA_VERSION,
        "ok": ok,
        "state": next_state,
        "reason": error or None,
        "idempotency_key": _text(row["idempotency_key"]),
        "exchange_order_id": exchange_order_id,
        "called_gateway": True,
        "submitted_order": False,
        "response": response,
        "position_lot_update": lot_result,
        "transition_lifecycle": lifecycle,
    }


__all__ = [
    "RECONCILE_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "SUPERSESSION_REASON",
    "SUPERSESSION_SCHEMA_VERSION",
    "reconcile_dynamic_three_leg_intent",
    "submit_prepared_dynamic_three_leg_intent",
    "supersede_stale_prepared_dynamic_three_leg_intents",
]
