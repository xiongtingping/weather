"""Atomic decision/intent writer for independent top-YES wave take profit."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Mapping

from dynamic_top_yes_wave_exit_runtime import (
    REASON_CODE,
    REASON_FAMILY,
    build_dynamic_top_yes_wave_exit_candidate,
)
from lineage_identity_runtime import lineage_id_from_parts
from market_alpha_take_profit_activation import validate_market_alpha_take_profit_activation
from top_yes_wave_reentry_runtime import build_top_yes_wave_reentry_state


SCHEMA_VERSION = "production_market_alpha_take_profit_writer_v1"
SUPERSESSION_SCHEMA_VERSION = "market_alpha_prepared_supersession_v1"
SUPERSESSION_REASON = "market_alpha_superseded_by_latest_posterior_target_realization"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _enabled(name: str) -> bool:
    return _text(os.environ.get(name)).lower() in {"1", "true", "yes", "on"}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _blocked(*reasons: str, candidate: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        "candidate": dict(_mapping(candidate)),
        "wrote_rows": False,
        "places_orders": False,
    }


def supersede_stale_prepared_market_alpha_intents(
    conn: sqlite3.Connection, *, superseded_at: str,
) -> dict[str, Any]:
    """Cancel only stale prepared alpha intents before the latest posterior is planned.

    ``prepared`` proves that no exchange side effect has started.  Every in-flight
    state is deliberately excluded and remains owned by reconcile-first.
    """

    if conn.in_transaction:
        return {
            "schema": SUPERSESSION_SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["market_alpha_supersession_requires_clean_transaction"],
            "wrote_rows": False,
            "places_orders": False,
        }
    required = {
        "market_alpha_take_profit_cycles", "posterior_execution_intents",
        "auto_trade_decisions", "event_fact_history", "event_fact_latest",
    }
    missing = sorted(table for table in required if not _table_exists(conn, table))
    if missing:
        return {
            "schema": SUPERSESSION_SCHEMA_VERSION,
            "ok": True,
            "state": "not_applicable_schema_missing",
            "na_reasons": [f"market_alpha_supersession_table_missing:{table}" for table in missing],
            "superseded_order_intent_ids": [],
            "wrote_rows": False,
            "places_orders": False,
        }

    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            """SELECT i.order_intent_id,i.decision_id,i.transition_plan_id,
                      i.posterior_run_id,i.fact_snapshot_id,i.intent_json,
                      i.exchange_post_started_at,i.expected_order_id,i.exchange_order_id,
                      c.market_alpha_cycle_id,c.state
                 FROM posterior_execution_intents i
                 JOIN market_alpha_take_profit_cycles c
                   ON c.market_alpha_cycle_id=i.transition_plan_id
                  AND c.order_intent_id=i.order_intent_id
                WHERE i.reason_family='market_alpha_take_profit'
                  AND i.reason_code='market_alpha_take_profit_top_yes_wave_completed'
                  AND i.state='prepared'
                ORDER BY i.created_at,i.order_intent_id"""
        ).fetchall()
        stale: list[tuple[str, str, str]] = []
        blockers: list[str] = []
        for row in rows:
            order_intent_id = _text(row[0])
            try:
                payload = json.loads(_text(row[5]))
            except (TypeError, ValueError):
                payload = {}
            slug = _text(_mapping(payload).get("slug"))
            event_date = _text(_mapping(payload).get("event_date"))[:10]
            no_exchange_side_effect = not any(_text(value) for value in row[6:9])
            if (
                not slug or not event_date or not no_exchange_side_effect
                or _text(row[9]) != _text(row[2]) or _text(row[10]) != "prepared"
            ):
                blockers.append(f"market_alpha_prepared_identity_unproven:{order_intent_id}")
                continue
            latest = conn.execute(
                """SELECT h.posterior_run_id,h.fact_snapshot_id
                     FROM event_fact_latest l
                     JOIN event_fact_history h ON h.id=l.fact_id
                    WHERE l.slug=? AND l.event_date=?""",
                (slug, event_date),
            ).fetchone()
            if latest is None:
                blockers.append(f"market_alpha_latest_ssot_missing:{order_intent_id}")
                continue
            if (_text(latest[0]), _text(latest[1])) != (_text(row[3]), _text(row[4])):
                stale.append((order_intent_id, _text(row[1]), _text(row[9])))
        if blockers:
            conn.rollback()
            return {
                "schema": SUPERSESSION_SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": blockers,
                "superseded_order_intent_ids": [],
                "wrote_rows": False,
                "places_orders": False,
            }
        for order_intent_id, decision_id, cycle_id in stale:
            changed_intent = conn.execute(
                """UPDATE posterior_execution_intents
                      SET state='cancelled_superseded',last_error=?,updated_at=?
                    WHERE order_intent_id=? AND state='prepared'""",
                (SUPERSESSION_REASON, _text(superseded_at), order_intent_id),
            ).rowcount
            changed_decision = conn.execute(
                """UPDATE auto_trade_decisions
                      SET decided_state='cancelled_superseded',will_order=0
                    WHERE decision_id=?""",
                (decision_id,),
            ).rowcount
            changed_cycle = conn.execute(
                """UPDATE market_alpha_take_profit_cycles
                      SET state='blocked',updated_at=?
                    WHERE market_alpha_cycle_id=? AND state='prepared'""",
                (_text(superseded_at), cycle_id),
            ).rowcount
            if (changed_intent, changed_decision, changed_cycle) != (1, 1, 1):
                raise RuntimeError("market_alpha_prepared_supersession_cas_failed")
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    superseded_ids = [row[0] for row in stale]
    return {
        "schema": SUPERSESSION_SCHEMA_VERSION,
        "ok": True,
        "state": "superseded" if superseded_ids else "idle_current",
        "reason": SUPERSESSION_REASON if superseded_ids else None,
        "superseded_order_intent_ids": superseded_ids,
        "wrote_rows": bool(superseded_ids),
        "places_orders": False,
    }


def commit_market_alpha_take_profit_candidate(
    conn: sqlite3.Connection,
    *,
    posterior_ssot_fact: Mapping[str, Any],
    position_snapshot_id: str,
    position: Mapping[str, Any],
    executable_sell_quote: Mapping[str, Any],
    activation: Mapping[str, Any],
    decision_as_of: str,
    fault_after: str = "",
) -> dict[str, Any]:
    """Write one SELL decision and intent; never calls CLOB."""

    if conn.in_transaction:
        return _blocked("market_alpha_writer_requires_clean_transaction")
    required_tables = {"market_alpha_take_profit_cycles", "dynamic_three_leg_position_lots", "posterior_execution_intents", "auto_trade_decisions", "event_fact_history", "event_fact_latest"}
    missing = sorted(table for table in required_tables if not _table_exists(conn, table))
    if missing:
        return _blocked(*[f"market_alpha_table_missing:{table}" for table in missing])
    try:
        authorized = validate_market_alpha_take_profit_activation(activation)
    except ValueError as exc:
        return _blocked(f"market_alpha_activation_rejected:{exc}")
    if not _enabled("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED") or not _enabled("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED"):
        return _blocked("market_alpha_production_environment_disabled")
    strategy_artifact = _text(authorized.get("strategy_artifact_sha256"))
    candidate = build_dynamic_top_yes_wave_exit_candidate(
        posterior_ssot_fact=posterior_ssot_fact,
        position=position,
        executable_sell_quote=executable_sell_quote,
        approved_strategy_artifact_sha256=strategy_artifact,
        decision_as_of=decision_as_of,
    )
    if candidate.get("state") != "candidate":
        return _blocked(*list(candidate.get("reason_codes") or ["market_alpha_candidate_not_ready"]), candidate=candidate)
    fact = _mapping(posterior_ssot_fact)
    slug, day = _text(fact.get("slug")), _text(fact.get("event_date"))[:10]
    posterior_run_id = _text(candidate.get("posterior_run_id"))
    realization_state_id = _text(candidate.get("realization_state_id"))
    fact_snapshot_id = _text(fact.get("fact_snapshot_id"))
    if not all((slug, day, posterior_run_id, realization_state_id, fact_snapshot_id, _text(position_snapshot_id))):
        return _blocked("market_alpha_ssot_or_position_snapshot_identity_missing", candidate=candidate)
    latest = conn.execute(
        """SELECT h.posterior_run_id,h.fact_snapshot_id FROM event_fact_latest l
           JOIN event_fact_history h ON h.id=l.fact_id WHERE l.slug=? AND l.event_date=?""",
        (slug, day),
    ).fetchone()
    if latest is None or (_text(latest[0]), _text(latest[1])) != (posterior_run_id, fact_snapshot_id):
        return _blocked("market_alpha_posterior_not_latest_ssot", candidate=candidate)
    current_lot = conn.execute(
        """SELECT position_lot_version,bucket,outcome_side,token_id,entry_strategy_leg,
                  entry_strategy_artifact_sha256,remaining_size,frozen_take_profit_price,state
             FROM dynamic_three_leg_position_lots WHERE position_lot_id=?""",
        (_text(candidate.get("position_lot_id")),),
    ).fetchone()
    entry_leg = _text(candidate.get("entry_strategy_leg"))
    target_source = _text(candidate.get("take_profit_price_source"))
    stored_target = float(current_lot[7]) if current_lot is not None and current_lot[7] is not None else None
    dynamic_role_target_valid = bool(
        current_lot is not None
        and (
            (
                entry_leg == "top_yes_wave"
                and _text(current_lot[4]) == "top_yes_wave"
                and stored_target is not None
                and abs(stored_target - 0.90) <= 1e-12
                and target_source == "entry_frozen_top_yes_wave"
            )
            or (
                entry_leg == "second_yes"
                and _text(current_lot[4]) == "second_yes"
                and stored_target is None
                and target_source == "promoted_current_top_yes_policy"
            )
        )
    )
    if not (
        current_lot is not None
        and _text(current_lot[0]) == _text(candidate.get("position_lot_version"))
        and _text(current_lot[1]) == _text(candidate.get("position_bucket"))
        and _text(current_lot[2]) == "YES"
        and _text(current_lot[3]) == _text(candidate.get("token_id"))
        and dynamic_role_target_valid
        and _text(current_lot[5]) == strategy_artifact
        and float(current_lot[6] or 0.0) + 1e-9 >= float(candidate.get("requested_sell_size") or 0.0)
        and abs(float(candidate.get("frozen_take_profit_price") or 0.0) - 0.90) <= 1e-12
        and _text(current_lot[8]) == "open"
    ):
        return _blocked("market_alpha_position_lot_ssot_stale", candidate=candidate)
    active_weather_exit = conn.execute(
        """SELECT 1 FROM posterior_execution_intents
           WHERE position_lot_id=? AND position_lot_version=? AND order_action='SELL'
             AND reason_family='target_realization'
             AND state IN ('prepared','submitting','gateway_accepted','reconcile_required','partially_filled') LIMIT 1""",
        (_text(candidate.get("position_lot_id")), _text(candidate.get("position_lot_version"))),
    ).fetchone()
    if active_weather_exit is not None:
        return _blocked("market_alpha_superseded_by_target_realization_exit", candidate=candidate)
    identity = {
        "posterior_run_id": posterior_run_id,
        "realization_state_id": realization_state_id,
        "fact_snapshot_id": fact_snapshot_id,
        "position_lot_id": candidate.get("position_lot_id"),
        "position_lot_version": candidate.get("position_lot_version"),
        "market_snapshot_id": candidate.get("market_snapshot_id"),
        "reason_code": REASON_CODE,
        "strategy_artifact_sha256": strategy_artifact,
    }
    cycle_id = "macycle_" + _sha(identity)[:24]
    decision_id = lineage_id_from_parts("decision_id", {"market_alpha_cycle_id": cycle_id})
    order_intent_id = lineage_id_from_parts("order_intent_id", {"decision_id": decision_id, "action": "SELL"})
    quote_expiry = _text(executable_sell_quote.get("expires_at"))
    intent_payload = {
        "schema": "market_alpha_take_profit_execution_intent_v1",
        "market_alpha_cycle_id": cycle_id,
        "order_intent_id": order_intent_id,
        "posterior_run_id": posterior_run_id,
        "fact_snapshot_id": fact_snapshot_id,
        "realization_state_id": realization_state_id,
        "position_snapshot_id": _text(position_snapshot_id),
        "position_lot_id": candidate.get("position_lot_id"),
        "position_lot_version": candidate.get("position_lot_version"),
        "strategy_artifact_sha256": strategy_artifact,
        "activation_contract_sha256": authorized.get("activation_contract_sha256"),
        "market_snapshot_id": candidate.get("market_snapshot_id"),
        "slug": slug,
        "event_date": day,
        "bucket": candidate.get("position_bucket"),
        "outcome_side": "YES",
        "token_id": candidate.get("token_id"),
        "side": "SELL",
        "price": candidate.get("frozen_take_profit_price"),
        "size": candidate.get("requested_sell_size"),
        "notional": float(candidate["frozen_take_profit_price"]) * float(candidate["requested_sell_size"]),
        "order_type": "GTC",
        "post_only": False,
        "reason_family": REASON_FAMILY,
        "reason_code": REASON_CODE,
        "execution_deadline_at": quote_expiry,
        "quote_expires_at": quote_expiry,
    }
    idempotency_key = "market-alpha:" + _sha(intent_payload)
    intent_payload["idempotency_key"] = idempotency_key
    cycle_payload = {**identity, "candidate": candidate, "decision_id": decision_id, "order_intent_id": order_intent_id, "activation_contract_sha256": authorized.get("activation_contract_sha256")}
    cycle_sha = _sha(cycle_payload)
    existing = conn.execute(
        "SELECT cycle_sha256,state,order_intent_id FROM market_alpha_take_profit_cycles WHERE market_alpha_cycle_id=?",
        (cycle_id,),
    ).fetchone()
    if existing is not None:
        if _text(existing[0]) != cycle_sha:
            raise RuntimeError("market_alpha_cycle_replay_drift")
        return {"schema": SCHEMA_VERSION, "ok": True, "state": _text(existing[1]), "idempotent": True, "market_alpha_cycle_id": cycle_id, "order_intent_id": _text(existing[2]), "wrote_rows": False, "places_orders": False}
    na_id = "market_alpha_sell_not_applicable"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """INSERT INTO market_alpha_take_profit_cycles(
               market_alpha_cycle_id,posterior_run_id,realization_state_id,fact_snapshot_id,
               position_snapshot_id,position_lot_id,position_lot_version,strategy_artifact_sha256,
               market_snapshot_id,bucket,token_id,requested_sell_size,frozen_take_profit_price,
               executable_sell_price,reason_family,reason_code,decision_id,order_intent_id,
               activation_contract_sha256,quote_expires_at,state,cycle_json,cycle_sha256,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cycle_id, posterior_run_id, realization_state_id, fact_snapshot_id, _text(position_snapshot_id),
             candidate["position_lot_id"], candidate["position_lot_version"], strategy_artifact,
             candidate["market_snapshot_id"], candidate["position_bucket"], candidate["token_id"],
             candidate["requested_sell_size"], candidate["frozen_take_profit_price"], candidate["executable_sell_price"],
             REASON_FAMILY, REASON_CODE, decision_id, order_intent_id, authorized["activation_contract_sha256"],
             quote_expiry, "prepared", _json(cycle_payload), cycle_sha, _text(decision_as_of), _text(decision_as_of)),
        )
        if fault_after == "cycle":
            raise RuntimeError("market_alpha_fault_after_cycle")
        conn.execute(
            """INSERT INTO auto_trade_decisions(
               run_id,run_date,slug,event_date,outcome,side,action,decided_state,will_order,
               reason_codes_json,reasons_json,details_json,created_at,correlation_id,
               fact_snapshot_id,posterior_run_id,decision_id,order_intent_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (posterior_run_id, day, slug, day, candidate["position_bucket"], "YES", "exit", "intent_prepared", 1,
             _json([REASON_CODE]), _json([{"reason_family": REASON_FAMILY, "reason_code": REASON_CODE}]),
             _json(intent_payload), _text(decision_as_of), cycle_id, fact_snapshot_id, posterior_run_id, decision_id, order_intent_id),
        )
        if fault_after == "decision":
            raise RuntimeError("market_alpha_fault_after_decision")
        intent_row = {
            "order_intent_id": order_intent_id, "decision_id": decision_id,
            "transition_plan_id": cycle_id, "trading_plan_id": cycle_id,
            "posterior_run_id": posterior_run_id, "realization_state_id": realization_state_id,
            "fact_snapshot_id": fact_snapshot_id, "position_snapshot_id": _text(position_snapshot_id),
            "position_lot_id": candidate["position_lot_id"], "position_lot_version": candidate["position_lot_version"],
            "strategy_artifact_sha256": strategy_artifact, "candidate_id": None,
            "market_snapshot_id": candidate["market_snapshot_id"], "capital_snapshot_id": na_id,
            "risk_snapshot_id": na_id, "capital_epoch_id": na_id, "risk_reservation_id": na_id + ":" + cycle_id,
            "bucket": candidate["position_bucket"], "outcome_side": "YES", "order_action": "SELL",
            "token_id": candidate["token_id"], "limit_price": candidate["frozen_take_profit_price"],
            "size": candidate["requested_sell_size"], "notional": intent_payload["notional"],
            "reason_family": REASON_FAMILY, "reason_code": REASON_CODE, "state": "prepared",
            "live_submit_authorized": 1, "observation_to_order_hard_deadline_at": quote_expiry,
            "quote_expires_at": quote_expiry, "idempotency_key": idempotency_key,
            "intent_json": _json(intent_payload), "intent_sha256": _sha(intent_payload),
            "created_at": _text(decision_as_of), "updated_at": _text(decision_as_of),
        }
        fields = list(intent_row)
        conn.execute(
            f"INSERT INTO posterior_execution_intents({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            tuple(intent_row[field] for field in fields),
        )
        if fault_after == "intent":
            raise RuntimeError("market_alpha_fault_after_intent")
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "prepared", "idempotent": False, "market_alpha_cycle_id": cycle_id, "decision_id": decision_id, "order_intent_id": order_intent_id, "wrote_rows": True, "places_orders": False}


def same_posterior_wave_reentry_suppressed(
    conn: sqlite3.Connection, *, posterior_run_id: str, bucket: str,
) -> bool:
    """A completed wave can re-enter only after a new posterior run."""

    if not _table_exists(conn, "market_alpha_take_profit_cycles"):
        return False
    return bool(conn.execute(
        """SELECT 1 FROM market_alpha_take_profit_cycles
           WHERE posterior_run_id=? AND bucket=? AND state='completed' LIMIT 1""",
        (_text(posterior_run_id), _text(bucket)),
    ).fetchone())


def load_top_yes_wave_reentry_state(
    conn: sqlite3.Connection, *, posterior_run_id: str,
) -> dict[str, Any]:
    """Load completed cycles as one explicit input for the next BUY plan."""

    if not _table_exists(conn, "market_alpha_take_profit_cycles"):
        return {
            "schema": "top_yes_wave_reentry_state_v1",
            "ok": False,
            "state": "blocked",
            "block_reasons": ["market_alpha_take_profit_cycles_table_missing"],
            "read_only": True,
        }
    rows = conn.execute(
        """SELECT market_alpha_cycle_id,posterior_run_id,bucket,state
           FROM market_alpha_take_profit_cycles
           WHERE posterior_run_id=? AND state='completed'
           ORDER BY bucket,market_alpha_cycle_id""",
        (_text(posterior_run_id),),
    ).fetchall()
    return build_top_yes_wave_reentry_state(
        posterior_run_id=_text(posterior_run_id),
        completed_cycles=[
            {
                "market_alpha_cycle_id": _text(row[0]),
                "posterior_run_id": _text(row[1]),
                "bucket": _text(row[2]),
                "state": _text(row[3]),
            }
            for row in rows
        ],
    )


__all__ = [
    "SCHEMA_VERSION",
    "SUPERSESSION_REASON",
    "commit_market_alpha_take_profit_candidate",
    "load_top_yes_wave_reentry_state",
    "same_posterior_wave_reentry_suppressed",
    "supersede_stale_prepared_market_alpha_intents",
]
