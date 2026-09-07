"""Produce the target lane's own order-state ingress for no-live mode.

The target compiler has no order writer while ``production_no_live`` is
active.  That absence is a fact only when the activation and the target
strategy ledger agree and no target cycle/order remains in a live or
reconciliation state.  This module emits the explicit zero-pending state in
that narrow case; it never imports legacy order rows as execution evidence.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .et_history import assert_event_day_started, event_day_window
from .activation import assert_target_database_owner, load_activation
from .market_registry import load_active_registry_rows, load_started_registry_rows


SCHEMA = "bayes_basket_target_order_state_producer_v1"
INGRESS_SCHEMA = "bayes_basket_order_state_ingress_v1"

# ``prepared`` is a non-authoritative no-live candidate written by the target
# compiler.  It does not represent an exchange order and therefore must not
# make the target's zero-pending snapshot impossible to materialize.  Every
# state below represents an order that may still exist at the gateway, or a
# reconciliation that is not terminal yet.
TARGET_CYCLE_BLOCKING_STATES = frozenset({"submitting", "submitted", "reconcile_required"})
TARGET_INTENT_BLOCKING_STATES = frozenset({"submitting", "submitted", "reconcile_required"})
TARGET_RECONCILIATION_BLOCKING_STATES = frozenset({"pending", "unknown", "resolved_partial"})
TARGET_DECISION_BLOCKING_STATES = frozenset({"submitting", "submitted", "reconcile_required"})



# The execution mode is read from the worker rather than restated here.  Both
# guards below spelled "production_no_live" as a literal, so the go-live flip
# -- which changed the constant in two other modules and never touched this
# file -- left this lane demanding the mode the system had just left.
def _expected_execution_mode() -> str:
    from .production import PRODUCTION_EXECUTION_MODE  # noqa: PLC0415

    return PRODUCTION_EXECUTION_MODE


def _assert_activation_row_matches_mode(
    db_activation: Mapping[str, Any],
) -> None:
    """The DB row must carry this mode's own boundaries, not no-live's.

    A live contract states the live flags true and a no-live contract states
    them false; either disagreeing with the mode in force is drift.  The
    previous form asserted false unconditionally, which is correct only while
    no-live is the only mode that exists.
    """

    mode = _expected_execution_mode()
    if db_activation.get("execution_mode") != mode:
        raise ContractError("target_activation_state_mode_drift")
    expected = mode == "production_live"
    for key in ("live_submit_authorized", "places_orders", "calls_order_gateway"):
        if db_activation.get(key) is not expected:
            raise ContractError(f"target_activation_state_{key}_drift")


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


def _activation(path: Path) -> dict[str, Any]:
    return load_activation(path)


def _registry_scopes(
    conn: sqlite3.Connection,
    *,
    station_id: str = "",
    event_date: str = "",
    include_listing_days: bool = False,
) -> dict[tuple[str, str], dict[str, Any]]:
    # Charter 48: a listed day's order state is the opening state -- nothing
    # filled, nothing resting -- and it has to be recorded so the stage-open
    # can bind to it.  The started-day view stays the default for every
    # caller that reads a day which has begun.
    rows = (
        load_active_registry_rows(conn, station_id=station_id, event_date=event_date)
        if include_listing_days
        else load_started_registry_rows(conn, station_id=station_id, event_date=event_date)
    )
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            _text("station_id", row["station_id"]),
            _text("event_date", row["event_date"]),
        )
        scope = result.setdefault(key, {
            "settlement_timezone": _text(
                "settlement_timezone", row["settlement_timezone"]
            ),
            "buckets": [],
        })
        timezone_name = _text(
            "settlement_timezone", row["settlement_timezone"]
        )
        if scope["settlement_timezone"] != timezone_name:
            raise ContractError(
                "order_state_registry_settlement_timezone_not_unique"
            )
        scope["buckets"].append(_text("bucket", row["outcome"]))
    if not result:
        raise ContractError("order_state_registry_scope_empty")
    return result


# The scope-restricted form of each quiescence probe.  The snapshot this
# guard protects is produced per (station, event day) -- both are arguments
# to every builder below -- but the probes were fleet-wide, so one resting
# order anywhere refused the zero-pending snapshot everywhere.
#
# It could not bite while the execution family was taker: an intent was
# ``submitted`` for the moment between the POST and its fill, and nothing
# rested.  Charter 94 (2026-09-06) made the resting maker ladder the only
# order family production forms, and ``submitted`` became "on the book, for
# as long as the bid holds".  Measured that evening: 10 resting orders for
# 2026-09-07 and 2026-09-08 (13.07 USDC) refused the market ingress for ten
# 2026-09-06 scopes that had no order of their own -- CYYZ, KATL, KAUS,
# KBKF, KDAL, KHOU, KLAX, KLGA, KMIA, KORD -- so quotes stopped refreshing
# and every scope died on production_quote_expired.  The stalest book was
# 281 minutes old, and the charter-94 lifecycle that is supposed to follow
# the bid was reading it.
#
# The claim is unchanged: a scope may not be called zero-pending while it
# holds a pending order.  What changes is that the question is asked about
# the scope being produced.
_SCOPE_PREDICATES = {
    "basket_target_execution_cycles": (
        "SELECT 1 FROM basket_target_execution_cycles c"
        " WHERE c.state IN ({states})"
        " AND (?='' OR c.station_id=?) AND (?='' OR c.event_date=?) LIMIT 1"
    ),
    # LEFT JOIN, and a row whose scope cannot be established blocks every
    # scope.  An inner join would have dropped it instead -- silently turning
    # "I cannot tell whose this is" into "it is nobody's", which is the one
    # answer this guard must never give.  Caught by
    # test_no_live_order_state_rejects_nonterminal_target_reconciliation.
    "basket_target_order_intents": (
        "SELECT 1 FROM basket_target_order_intents i"
        " LEFT JOIN basket_target_execution_cycles c ON c.cycle_id=i.cycle_id"
        " WHERE i.state IN ({states})"
        " AND (?='' OR c.station_id IS NULL OR c.station_id=?)"
        " AND (?='' OR c.event_date IS NULL OR c.event_date=?) LIMIT 1"
    ),
    "basket_target_order_reconciliations": (
        "SELECT 1 FROM basket_target_order_reconciliations r"
        " LEFT JOIN basket_target_order_intents i ON i.order_intent_id=r.order_intent_id"
        " LEFT JOIN basket_target_execution_cycles c ON c.cycle_id=i.cycle_id"
        " WHERE r.state IN ({states})"
        " AND (?='' OR c.station_id IS NULL OR c.station_id=?)"
        " AND (?='' OR c.event_date IS NULL OR c.event_date=?) LIMIT 1"
    ),
    "auto_trade_decisions": (
        "SELECT 1 FROM auto_trade_decisions d"
        " WHERE d.decided_state IN ({states})"
        " AND (?='' OR d.station_id=?) AND (?='' OR d.event_date=?) LIMIT 1"
    ),
}


def _assert_target_activation_and_quiescence(
    conn: sqlite3.Connection,
    activation: Mapping[str, Any],
    *,
    station_id: str = "",
    event_date: str = "",
) -> None:
    """Reject stale activation, or a pending cycle/order in THIS scope.

    An empty ``station_id``/``event_date`` keeps the fleet-wide question,
    which is what a caller producing a fleet-wide snapshot is entitled to
    ask.
    """

    assert_target_database_owner(conn)

    try:
        row = conn.execute(
            "SELECT state,activation_sha256,activation_json "
            "FROM basket_strategy_activation_state WHERE singleton=1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ContractError("target_activation_state_unavailable") from exc
    expected_hash = _text("activation_contract_sha256", activation.get("activation_contract_sha256"))
    if row is None or str(row[0]) != "enabled" or str(row[1] or "") != expected_hash:
        raise ContractError("target_activation_state_drift")
    try:
        db_activation = json.loads(str(row[2] or "{}"))
    except (TypeError, ValueError) as exc:
        raise ContractError("target_activation_state_json_invalid") from exc
    if not isinstance(db_activation, Mapping):
        raise ContractError("target_activation_state_mode_drift")
    _assert_activation_row_matches_mode(db_activation)

    # The target ledger is the only order-state authority for this lane.  In
    # particular, do not probe basket_strategy_* or any table from the shared
    # live line: those rows are not target order evidence.
    state_checks = (
        (
            "basket_target_execution_cycles",
            "state",
            TARGET_CYCLE_BLOCKING_STATES,
            "target_basket_target_execution_cycles_not_quiescent",
        ),
        (
            "basket_target_order_intents",
            "state",
            TARGET_INTENT_BLOCKING_STATES,
            "target_basket_target_order_intents_not_quiescent",
        ),
        (
            "basket_target_order_reconciliations",
            "state",
            TARGET_RECONCILIATION_BLOCKING_STATES,
            "target_basket_target_order_reconciliations_not_quiescent",
        ),
        (
            "auto_trade_decisions",
            "decided_state",
            TARGET_DECISION_BLOCKING_STATES,
            "target_auto_trade_decisions_not_quiescent",
        ),
    )
    scope_station = str(station_id or "")
    scope_day = str(event_date or "")
    for table, _state_column, active_states, reason in state_checks:
        try:
            placeholders = ",".join("?" for _ in active_states)
            found = conn.execute(
                _SCOPE_PREDICATES[table].format(states=placeholders),
                (
                    *sorted(active_states),
                    scope_station, scope_station, scope_day, scope_day,
                ),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ContractError(f"{table}_state_unavailable") from exc
        if found is not None:
            raise ContractError(reason)

    # ``auto_trade_decisions`` is the sole execution-blocking authority.  A
    # malformed or cross-wired row must never be converted into a known-zero
    # pending state, even though the target schema normally constrains this
    # column to zero in production_no_live.
    try:
        live_authority = conn.execute(
            "SELECT 1 FROM auto_trade_decisions WHERE will_order<>0"
            " AND (?='' OR station_id=?) AND (?='' OR event_date=?) LIMIT 1",
            (scope_station, scope_station, scope_day, scope_day),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ContractError("auto_trade_decisions_authority_unavailable") from exc
    if live_authority is not None:
        raise ContractError("target_auto_trade_decisions_live_order_authorized")


def build_no_live_order_state_ingress(
    conn: sqlite3.Connection,
    *,
    activation_path: Path,
    observed_at_utc: str,
    station_id: str = "",
    event_date: str = "",
    include_listing_days: bool = False,
) -> dict[str, Any]:
    """Build zero-pending order state only after target quiescence is proven."""

    observed_at = _timestamp("observed_at_utc", observed_at_utc)
    activation = _activation(Path(activation_path))
    _assert_target_activation_and_quiescence(
        conn, activation, station_id=station_id, event_date=event_date
    )
    scopes = _registry_scopes(
        conn,
        station_id=station_id,
        event_date=event_date,
        include_listing_days=include_listing_days,
    )
    activation_hash = _text("activation_contract_sha256", activation.get("activation_contract_sha256"))
    version = "ord_" + content_sha256({
        "schema": SCHEMA,
        "activation_contract_sha256": activation_hash,
        "observed_at_utc": observed_at,
        "scopes": sorted(f"{station}|{day}" for station, day in scopes),
    })[:40]
    markets: list[dict[str, Any]] = []
    for (scope_station, scope_date), scope in sorted(scopes.items()):
        buckets = scope["buckets"]
        if include_listing_days:
            # Resolved, not asserted: a listed day is admitted as the
            # opening state (charter 48), a malformed date still fails.
            event_day_window(
                scope_date,
                as_of_utc=observed_at,
                target_timezone=scope["settlement_timezone"],
            )
        else:
            assert_event_day_started(
                scope_date,
                as_of_utc=observed_at,
                target_timezone=scope["settlement_timezone"],
            )
        snapshot_id = "ord_snapshot_" + content_sha256({
            "order_state_version": version,
            "station_id": scope_station,
            "event_date": scope_date,
        })[:40]
        markets.append({
            "station_id": scope_station,
            "event_date": scope_date,
            "settlement_timezone": scope["settlement_timezone"],
            "order_state_snapshot_id": snapshot_id,
            "order_state_version": version,
            "observed_at": observed_at,
            "known": 1,
            "order_state_json": {
                "pending_by_bucket": {bucket: 0.0 for bucket in buckets},
                "pending_notional_by_bucket": {bucket: 0.0 for bucket in buckets},
                "source": "target_lane_no_live_quiescent",
                "activation_contract_sha256": activation_hash,
            },
        })
    return {
        "schema": INGRESS_SCHEMA,
        "producer_schema": SCHEMA,
        "source": "target_lane_no_live_quiescent",
        "activation_contract_sha256": activation_hash,
        "observed_at": observed_at,
        "known": True,
        "markets": markets,
        "places_orders": False,
        "calls_order_gateway": False,
    }


def build_execution_transition_order_state_ingress(
    conn: sqlite3.Connection,
    *,
    activation_path: Path,
    observed_at_utc: str,
    station_id: str = "",
    event_date: str = "",
    include_listing_days: bool = False,
) -> dict[str, Any]:
    """Project the formal receipt transition state for worker consumption.

    This is read-only.  A confirmed pending Maker/FOK amount is exposed only
    from the content-addressed transition table.  Any unknown, partial, late
    or race state fails closed instead of being represented as a zero-pending
    snapshot.  Full-fill and zero-fill cancel-confirm states naturally expose
    zero pending and their transition version remains in the lineage.
    """

    observed_at = _timestamp("observed_at_utc", observed_at_utc)
    activation = _activation(Path(activation_path))
    assert_target_database_owner(conn)
    expected_hash = _text("activation_contract_sha256", activation.get("activation_contract_sha256"))
    try:
        row = conn.execute(
            "SELECT state,activation_sha256,activation_json FROM basket_strategy_activation_state WHERE singleton=1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ContractError("target_activation_state_unavailable") from exc
    if row is None or str(row[0]) != "enabled" or str(row[1] or "") != expected_hash:
        raise ContractError("target_activation_state_drift")
    try:
        db_activation = json.loads(str(row[2] or "{}"))
    except (TypeError, ValueError) as exc:
        raise ContractError("target_activation_state_json_invalid") from exc
    if not isinstance(db_activation, Mapping):
        raise ContractError("target_activation_state_mode_drift")
    _assert_activation_row_matches_mode(db_activation)

    from .execution_transition_runtime import apply_execution_transition_schema

    apply_execution_transition_schema(conn)
    scopes = _registry_scopes(
        conn,
        station_id=station_id,
        event_date=event_date,
        include_listing_days=include_listing_days,
    )
    pending_states = frozenset({"POST_ACKED", "FOK_SUBMITTED", "CANCEL_REQUESTED"})
    blocked_states = frozenset({"RECONCILE_REQUIRED"})
    transitions = conn.execute(
        "SELECT station_id,event_date,bucket,confirmed_pending_size,confirmed_pending_notional_usdc,state,content_sha256 "
        "FROM basket_target_execution_transition_versions ORDER BY station_id,event_date,bucket,transition_seq"
    ).fetchall()
    by_scope: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for transition in transitions:
        key = (str(transition[0]), str(transition[1]))
        if key in scopes:
            by_scope.setdefault(key, []).append(tuple(transition))
    markets: list[dict[str, Any]] = []
    for (scope_station, scope_date), scope in sorted(scopes.items()):
        pending_by_bucket = {bucket: 0.0 for bucket in scope["buckets"]}
        pending_notional_by_bucket = {bucket: 0.0 for bucket in scope["buckets"]}
        versions: list[str] = []
        for transition in by_scope.get((scope_station, scope_date), []):
            bucket = str(transition[2])
            state = str(transition[5])
            versions.append(str(transition[6]))
            if state in blocked_states:
                raise ContractError("execution_transition_reconcile_required")
            if state in pending_states:
                if bucket not in pending_by_bucket:
                    raise ContractError("execution_transition_bucket_outside_registry")
                pending_by_bucket[bucket] += float(transition[3])
                pending_notional_by_bucket[bucket] += float(transition[4])
        version = "ord_" + content_sha256({
            "schema": SCHEMA,
            "activation_contract_sha256": expected_hash,
            "observed_at_utc": observed_at,
            "station_id": scope_station,
            "event_date": scope_date,
            "pending_by_bucket": pending_by_bucket,
            "pending_notional_by_bucket": pending_notional_by_bucket,
            "transition_content_sha256": sorted(versions),
        })[:40]
        if include_listing_days:
            # Resolved, not asserted: a listed day is admitted as the
            # opening state (charter 48), a malformed date still fails.
            event_day_window(
                scope_date,
                as_of_utc=observed_at,
                target_timezone=scope["settlement_timezone"],
            )
        else:
            assert_event_day_started(
                scope_date,
                as_of_utc=observed_at,
                target_timezone=scope["settlement_timezone"],
            )
        markets.append({
            "station_id": scope_station,
            "event_date": scope_date,
            "settlement_timezone": scope["settlement_timezone"],
            "order_state_snapshot_id": "ord_snapshot_" + content_sha256({"version": version, "scope": f"{scope_station}|{scope_date}"})[:40],
            "order_state_version": version,
            "observed_at": observed_at,
            "known": 1,
            "order_state_json": {
                "pending_by_bucket": pending_by_bucket,
                "pending_notional_by_bucket": pending_notional_by_bucket,
                "source": "target_execution_transition_runtime_no_live",
                "activation_contract_sha256": expected_hash,
                "transition_content_sha256": sorted(versions),
            },
        })
    return {
        "schema": INGRESS_SCHEMA,
        "producer_schema": SCHEMA,
        "source": "target_execution_transition_runtime_no_live",
        "activation_contract_sha256": expected_hash,
        "observed_at": observed_at,
        "known": True,
        "markets": markets,
        "places_orders": False,
        "calls_order_gateway": False,
    }


__all__ = [
    "INGRESS_SCHEMA",
    "SCHEMA",
    "build_execution_transition_order_state_ingress",
    "build_no_live_order_state_ingress",
]
