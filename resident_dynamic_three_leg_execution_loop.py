"""Default-off resident loop for the unique dynamic-three-leg execution lane."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dynamic_three_leg_clob_gateway_adapter import LocalDynamicThreeLegClobGateway
from dynamic_three_leg_production_activation import load_dynamic_three_leg_activation
from production_dynamic_three_leg_input_provider import freeze_production_dynamic_three_leg_inputs
from production_posterior_commit_outbox_worker import ProductionPosteriorCommitOutboxWorker
from market_alpha_take_profit_activation import load_market_alpha_take_profit_activation
from production_market_alpha_take_profit_coordinator import run_market_alpha_take_profit_once
from production_market_alpha_take_profit_writer import supersede_stale_prepared_market_alpha_intents
from production_dynamic_three_leg_continuation_worker import process_dynamic_three_leg_continuation_once
from resident_dynamic_three_leg_intent_submitter import (
    reconcile_dynamic_three_leg_intent,
    submit_prepared_dynamic_three_leg_intent,
    supersede_stale_prepared_dynamic_three_leg_intents,
)


SCHEMA_VERSION = "resident_dynamic_three_leg_execution_loop_v1"
ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "archive.db"
REQUIRED_TABLES = {
    "posterior_execution_intents", "dynamic_three_leg_transition_plans",
    "dynamic_three_leg_risk_reservations", "auto_trade_decisions",
    "event_fact_history", "event_fact_latest",
    "dynamic_three_leg_position_lots", "dynamic_three_leg_position_lot_events",
    "dynamic_three_leg_continuation_events", "dynamic_three_leg_continuation_transition_plans",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _enabled(name: str) -> bool:
    return _text(os.environ.get(name)).lower() in {"1", "true", "yes", "on"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _open(path: Path) -> sqlite3.Connection:
    # Autocommit is mandatory: the submitter's prepared->submitting CAS must be
    # visible to another process before the local gateway is called.
    conn = sqlite3.connect(path, timeout=2.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=2000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _schema_blockers(conn: sqlite3.Connection) -> list[str]:
    present = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [f"dynamic_three_leg_execution_table_missing:{name}" for name in sorted(REQUIRED_TABLES - present)]


def _next_reconcile(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """SELECT order_intent_id FROM posterior_execution_intents
           WHERE state IN ('submitting','reconcile_required','gateway_accepted','partially_filled')
           ORDER BY CASE state WHEN 'submitting' THEN 0 WHEN 'reconcile_required' THEN 1
                    WHEN 'partially_filled' THEN 2 ELSE 3 END,
                    updated_at ASC, order_intent_id ASC LIMIT 1"""
    ).fetchone()
    return _text(row[0]) if row else ""


def _next_prepared(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """SELECT order_intent_id FROM posterior_execution_intents
           WHERE state='prepared' AND live_submit_authorized=1
           ORDER BY observation_to_order_hard_deadline_at ASC, created_at ASC,
                    COALESCE(json_extract(intent_json,'$.execution_sequence'),1) ASC,
                    order_intent_id ASC
           LIMIT 1"""
    ).fetchone()
    return _text(row[0]) if row else ""


def run_resident_dynamic_three_leg_execution_once(
    *, db_path: Path = DEFAULT_DB, gateway: LocalDynamicThreeLegClobGateway | Any | None = None,
    action_as_of: str | None = None,
) -> dict[str, Any]:
    submit_enabled = _enabled("PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED") and _enabled("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED")
    decision_enabled = _enabled("PM_DYNAMIC_THREE_LEG_DECISION_RESIDENT_ENABLED") and _enabled("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED")
    market_alpha_enabled = _enabled("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED") and _enabled("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED")
    if not submit_enabled and not decision_enabled and not market_alpha_enabled:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "disabled", "wrote_rows": False, "called_gateway": False}
    if _enabled("PM_LIVE_PAUSED") or Path(os.environ.get("PM_LIVE_PAUSE_FILE", ROOT / "data/live_paused.flag")).exists():
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "paused", "wrote_rows": False, "called_gateway": False}
    if not db_path.exists():
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_execution_db_missing", "called_gateway": False}
    conn = _open(db_path)
    try:
        blockers = _schema_blockers(conn)
        if blockers:
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": blockers, "called_gateway": False}
        client = gateway or LocalDynamicThreeLegClobGateway(
            os.environ.get("PM_CLOB_ORDER_SERVICE_URL", "http://127.0.0.1:8765"),
            timeout_seconds=float(os.environ.get("PM_DYNAMIC_THREE_LEG_GATEWAY_TIMEOUT_SEC", "1.5")),
        )
        now = action_as_of or _utc_now()
        reconcile_id = _next_reconcile(conn) if submit_enabled else ""
        if reconcile_id:
            result = reconcile_dynamic_three_leg_intent(
                conn, order_intent_id=reconcile_id, reconcile_as_of=now,
                gateway_lookup_order=client.lookup_order,
            )
            return {"schema": SCHEMA_VERSION, "ok": result.get("ok") is True, "state": "reconciled", "order_intent_id": reconcile_id, "result": result, "called_gateway": result.get("called_gateway") is True}
        dynamic_supersession = supersede_stale_prepared_dynamic_three_leg_intents(
            conn, superseded_at=now,
        )
        if dynamic_supersession.get("ok") is not True:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "dynamic_supersession_blocked",
                "dynamic_supersession": dynamic_supersession,
                "called_gateway": False,
            }
        market_alpha_supersession = supersede_stale_prepared_market_alpha_intents(
            conn, superseded_at=now,
        )
        if market_alpha_supersession.get("ok") is not True:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "market_alpha_supersession_blocked",
                "market_alpha_supersession": market_alpha_supersession,
                "dynamic_supersession": dynamic_supersession,
                "called_gateway": False,
            }
        decision_result: dict[str, Any] | None = None
        continuation_result: dict[str, Any] | None = None
        market_alpha_result: dict[str, Any] | None = None
        if decision_enabled:
            try:
                activation = load_dynamic_three_leg_activation()
            except (OSError, ValueError) as exc:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "reason": "dynamic_three_leg_decision_activation_rejected",
                    "detail": str(exc),
                    "called_gateway": False,
                }
            worker = ProductionPosteriorCommitOutboxWorker(
                conn,
                lease_owner=f"dynamic-three-leg-resident:{os.getpid()}",
            )
            decision_result = worker.process_ready_latest_outbox(
                decision_as_of=now,
                frozen_inputs_for=lambda outbox: freeze_production_dynamic_three_leg_inputs(
                    conn,
                    outbox=outbox,
                    preliminary_as_of=now,
                    exchange_snapshot_fetcher=client.freeze_dynamic_three_leg_snapshot,
                    production_activation=activation,
                ),
                max_events=1,
            )
            if decision_result.get("ok") is not True and decision_result.get("state") != "idle_no_latest_outbox_ready":
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "decision_blocked",
                    "decision_result": decision_result,
                    "called_gateway": False,
                }
            continuation_result = process_dynamic_three_leg_continuation_once(
                conn,
                preliminary_as_of=now,
                lease_owner=f"dynamic-three-leg-continuation:{os.getpid()}",
                frozen_inputs_for=lambda event: freeze_production_dynamic_three_leg_inputs(
                    conn,
                    outbox=event,
                    preliminary_as_of=now,
                    exchange_snapshot_fetcher=client.freeze_dynamic_three_leg_snapshot,
                    production_activation=activation,
                ),
            )
            if continuation_result.get("ok") is not True:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "continuation_blocked",
                    "decision_result": decision_result,
                    "continuation_result": continuation_result,
                    "called_gateway": False,
                }
        if market_alpha_enabled:
            try:
                market_alpha_activation = load_market_alpha_take_profit_activation()
            except (OSError, ValueError) as exc:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "reason": "market_alpha_activation_rejected",
                    "detail": str(exc),
                    "decision_result": decision_result,
                    "continuation_result": continuation_result,
                    "called_gateway": False,
                }
            market_alpha_result = run_market_alpha_take_profit_once(
                conn,
                raw_guard_snapshot_fetcher=client.freeze_execution_guard_snapshot,
                activation=market_alpha_activation,
                preliminary_as_of=now,
            )
            if market_alpha_result.get("ok") is not True:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "market_alpha_blocked",
                    "decision_result": decision_result,
                    "market_alpha_result": market_alpha_result,
                    "called_gateway": False,
                }
        prepared_id = _next_prepared(conn) if submit_enabled else ""
        if not prepared_id:
            return {
                "schema": SCHEMA_VERSION,
                "ok": True,
                "state": "decision_committed" if decision_result and decision_result.get("state") == "dispatched" else "idle",
                "decision_result": decision_result,
                "continuation_result": continuation_result,
                "market_alpha_result": market_alpha_result,
                "market_alpha_supersession": market_alpha_supersession,
                "wrote_rows": bool(decision_result and decision_result.get("state") == "dispatched"),
                "called_gateway": False,
            }
        submit_now = _utc_now() if action_as_of is None else now
        result = submit_prepared_dynamic_three_leg_intent(
            conn, order_intent_id=prepared_id, submit_as_of=submit_now,
            pre_submit_validator=lambda intent: client.validate_execution_guard(intent),
            gateway_place_order=client.place_order,
        )
        return {"schema": SCHEMA_VERSION, "ok": result.get("ok") is True, "state": "submitted" if result.get("called_gateway") else "submit_blocked", "order_intent_id": prepared_id, "decision_result": decision_result, "continuation_result": continuation_result, "market_alpha_result": market_alpha_result, "market_alpha_supersession": market_alpha_supersession, "dynamic_supersession": dynamic_supersession, "result": result, "called_gateway": result.get("called_gateway") is True}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    poll = max(0.02, float(os.environ.get("PM_DYNAMIC_THREE_LEG_RESIDENT_POLL_SEC", "0.05")))
    while True:
        result = run_resident_dynamic_three_leg_execution_once(db_path=args.db)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        if args.once or result.get("state") in {"disabled", "paused", "blocked", "decision_blocked", "continuation_blocked", "market_alpha_blocked", "market_alpha_supersession_blocked", "dynamic_supersession_blocked"}:
            return 0 if result.get("state") in {"disabled", "paused"} else 2
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
