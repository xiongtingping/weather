"""Atomic fill application and read-only snapshots for strategy position lots."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Any, Mapping


SCHEMA_VERSION = "dynamic_three_leg_position_lot_runtime_v1"
MANAGEMENT_SCOPE = "dynamic_three_leg_weather"
STRATEGY_LEGS = {"top_yes_wave", "second_yes", "strongly_excluded_no"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _id(prefix: str, value: Any) -> str:
    return prefix + _sha256(value)[:24]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_text(table),)
    ).fetchone())


def build_position_lot_identity(
    *, order_intent_id: str, bucket: str, outcome_side: str, token_id: str,
) -> tuple[str, str]:
    identity = {
        "order_intent_id": _text(order_intent_id),
        "bucket": _text(bucket),
        "outcome_side": _text(outcome_side).upper(),
        "token_id": _text(token_id),
    }
    if not all(identity.values()) or identity["outcome_side"] not in {"YES", "NO"}:
        raise ValueError("dynamic_position_lot_identity_invalid")
    return _id("positionlot_", identity), "1"


def apply_reconciled_fill_to_position_lot(
    conn: sqlite3.Connection,
    *,
    intent: Mapping[str, Any],
    cumulative_filled_size: float,
    fill_price: float,
    applied_at: str,
) -> dict[str, Any]:
    """Apply only the new cumulative fill delta inside the caller transaction.

    This function does not begin, commit or roll back a transaction.  The
    intent state update and lot event therefore share one durability boundary.
    """

    if not _table_exists(conn, "dynamic_three_leg_position_lots") or not _table_exists(
        conn, "dynamic_three_leg_position_lot_events"
    ):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_position_lot_schema_missing"}
    row = dict(intent)
    order_intent_id = _text(row.get("order_intent_id"))
    action = _text(row.get("order_action")).upper()
    lot_id = _text(row.get("position_lot_id"))
    expected_version_text = _text(row.get("position_lot_version"))
    cumulative = _finite(cumulative_filled_size)
    price = _finite(fill_price)
    intent_limit_price = _finite(row.get("limit_price"))
    if (
        not order_intent_id or action not in {"BUY", "SELL"} or not lot_id
        or cumulative is None or cumulative <= 0 or price is None or not 0 < price < 1
        or intent_limit_price is None or abs(price - intent_limit_price) > 1e-9
        or not _text(applied_at)
    ):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": (
                "dynamic_position_lot_accounting_price_mismatch"
                if price is not None and intent_limit_price is not None
                and abs(price - intent_limit_price) > 1e-9
                else "dynamic_position_lot_fill_identity_invalid"
            ),
        }
    try:
        expected_version = int(expected_version_text)
    except (TypeError, ValueError):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_position_lot_version_invalid"}
    if expected_version < 1:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_position_lot_version_invalid"}

    previous_event = conn.execute(
        """SELECT cumulative_filled_size,position_lot_version_after
             FROM dynamic_three_leg_position_lot_events
            WHERE order_intent_id=? ORDER BY cumulative_filled_size DESC LIMIT 1""",
        (order_intent_id,),
    ).fetchone()
    previous_cumulative = float(previous_event[0]) if previous_event else 0.0
    if cumulative <= previous_cumulative + 1e-9:
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "already_applied",
            "idempotent": True,
            "position_lot_id": lot_id,
            "cumulative_filled_size": previous_cumulative,
        }
    delta = cumulative - previous_cumulative
    lot = conn.execute(
        """SELECT position_lot_version,management_scope,slug,event_date,bucket,outcome_side,
                  token_id,entry_strategy_leg,entry_strategy_artifact_sha256,
                  entry_order_intent_id,entry_posterior_run_id,entry_price,acquired_size,
                  remaining_size,remaining_cost_usdc,frozen_take_profit_price,state
             FROM dynamic_three_leg_position_lots WHERE position_lot_id=?""",
        (lot_id,),
    ).fetchone()
    payload = {}
    try:
        payload = json.loads(_text(row.get("intent_json")) or "{}")
    except (TypeError, ValueError):
        payload = {}
    slug = _text(payload.get("slug"))
    event_date = _text(payload.get("event_date"))
    bucket = _text(row.get("bucket"))
    outcome_side = _text(row.get("outcome_side")).upper()
    token_id = _text(row.get("token_id"))
    strategy_artifact = _text(row.get("strategy_artifact_sha256"))
    reason_family = _text(row.get("reason_family"))
    reason_code = _text(row.get("reason_code"))

    if action == "BUY":
        strategy_leg = reason_code if reason_family == "strategy_leg" else ""
        if (
            strategy_leg not in STRATEGY_LEGS or not all(
                (slug, event_date, bucket, outcome_side, token_id, strategy_artifact)
            ) or outcome_side not in {"YES", "NO"}
        ):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_position_lot_buy_provenance_invalid"}
        if lot is None:
            if previous_event is not None or expected_version != 1:
                return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_position_lot_buy_history_inconsistent"}
            version_before, version_after = 0, 1
            frozen_tp = 0.90 if strategy_leg == "top_yes_wave" else None
            conn.execute(
                """INSERT INTO dynamic_three_leg_position_lots(
                   position_lot_id,position_lot_version,management_scope,slug,event_date,bucket,
                   outcome_side,token_id,entry_strategy_leg,entry_strategy_artifact_sha256,
                   entry_order_intent_id,entry_posterior_run_id,entry_price,acquired_size,
                   remaining_size,remaining_cost_usdc,frozen_take_profit_price,state,
                   opened_at,closed_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lot_id, version_after, MANAGEMENT_SCOPE, slug, event_date, bucket,
                    outcome_side, token_id, strategy_leg, strategy_artifact, order_intent_id,
                    _text(row.get("posterior_run_id")), price, delta, delta, delta * price,
                    frozen_tp, "open", _text(applied_at), None, _text(applied_at),
                ),
            )
        else:
            version_before = int(lot[0])
            expected_current = int(previous_event[1]) if previous_event else expected_version
            if (
                version_before != expected_current
                or _text(lot[9]) != order_intent_id
                or tuple(map(_text, (lot[4], lot[5], lot[6], lot[7], lot[8])))
                != (bucket, outcome_side, token_id, strategy_leg, strategy_artifact)
                or _text(lot[16]) != "open"
            ):
                return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_position_lot_buy_binding_conflict"}
            version_after = version_before + 1
            conn.execute(
                """UPDATE dynamic_three_leg_position_lots
                      SET position_lot_version=?,acquired_size=acquired_size+?,
                          remaining_size=remaining_size+?,remaining_cost_usdc=remaining_cost_usdc+?,
                          updated_at=?
                    WHERE position_lot_id=? AND position_lot_version=?""",
                (version_after, delta, delta, delta * price, _text(applied_at), lot_id, version_before),
            )
    else:
        if lot is None or outcome_side not in {"YES", "NO"}:
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_position_lot_sell_lot_missing"}
        version_before = int(lot[0])
        expected_current = int(previous_event[1]) if previous_event else expected_version
        if (
            version_before != expected_current
            or tuple(map(_text, (lot[4], lot[5], lot[6]))) != (bucket, outcome_side, token_id)
            or _text(lot[16]) != "open"
        ):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_position_lot_sell_binding_conflict"}
        remaining_size = float(lot[13])
        remaining_cost = float(lot[14])
        if delta > remaining_size + 1e-8:
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_position_lot_sell_exceeds_remaining_size"}
        average_cost = remaining_cost / remaining_size if remaining_size > 0 else 0.0
        next_size = max(0.0, remaining_size - delta)
        next_cost = max(0.0, remaining_cost - delta * average_cost)
        if next_size <= 1e-8:
            next_size, next_cost, next_state, closed_at = 0.0, 0.0, "closed", _text(applied_at)
        else:
            next_state, closed_at = "open", None
        version_after = version_before + 1
        conn.execute(
            """UPDATE dynamic_three_leg_position_lots
                  SET position_lot_version=?,remaining_size=?,remaining_cost_usdc=?,state=?,
                      closed_at=?,updated_at=?
                WHERE position_lot_id=? AND position_lot_version=?""",
            (version_after, next_size, next_cost, next_state, closed_at, _text(applied_at), lot_id, version_before),
        )

    event_payload = {
        "position_lot_id": lot_id,
        "position_lot_version_before": version_before,
        "position_lot_version_after": version_after,
        "order_intent_id": order_intent_id,
        "order_action": action,
        "cumulative_filled_size": cumulative,
        "applied_size": delta,
        "applied_price": price,
        "accounting_price_source": "frozen_order_limit_price",
        "reason_family": reason_family,
        "reason_code": reason_code,
        "applied_at": _text(applied_at),
    }
    event_id = _id("positionlotevent_", event_payload)
    conn.execute(
        """INSERT INTO dynamic_three_leg_position_lot_events(
           position_lot_event_id,position_lot_id,position_lot_version_before,
           position_lot_version_after,order_intent_id,order_action,cumulative_filled_size,
           applied_size,applied_price,reason_family,reason_code,event_json,event_sha256,applied_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id, lot_id, version_before, version_after, order_intent_id, action,
            cumulative, delta, price, reason_family, reason_code, _json(event_payload),
            _sha256(event_payload), _text(applied_at),
        ),
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "fill_applied",
        "idempotent": False,
        "position_lot_id": lot_id,
        "position_lot_version": str(version_after),
        "applied_size": delta,
        "cumulative_filled_size": cumulative,
        "position_lot_event_id": event_id,
    }


def load_dynamic_three_leg_position_snapshot(
    conn: sqlite3.Connection, *, slug: str, event_date: str, as_of: str,
) -> dict[str, Any]:
    """Read the strategy lot SSOT and every unresolved execution intent."""

    if not _table_exists(conn, "dynamic_three_leg_position_lots"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "complete": False,
            "block_reasons": ["dynamic_position_lot_schema_missing"],
            "positions": [],
            "unresolved_intents": [],
        }
    lot_rows = conn.execute(
        """SELECT position_lot_id,position_lot_version,bucket,outcome_side,token_id,
                  entry_strategy_leg,entry_strategy_artifact_sha256,entry_price,
                  remaining_size,remaining_cost_usdc,frozen_take_profit_price,opened_at
             FROM dynamic_three_leg_position_lots
            WHERE slug=? AND event_date=? AND state='open' AND remaining_size>0
            ORDER BY entry_strategy_leg,bucket,outcome_side,opened_at,position_lot_id""",
        (_text(slug), _text(event_date)),
    ).fetchall()
    positions = []
    for row in lot_rows:
        size = float(row[8])
        cost = float(row[9])
        positions.append({
            "management_scope": MANAGEMENT_SCOPE,
            "position_lot_id": _text(row[0]),
            "position_lot_version": str(int(row[1])),
            "bucket": _text(row[2]),
            "side": _text(row[3]),
            "token_id": _text(row[4]),
            "strategy_leg": _text(row[5]),
            "entry_strategy_artifact_sha256": _text(row[6]),
            "entry_price": float(row[7]),
            "average_cost": cost / size,
            "position_size": size,
            "entry_notional_usdc": cost,
            "frozen_take_profit_price": float(row[10]) if row[10] is not None else None,
            "opened_at": _text(row[11]),
        })
    unresolved_rows = conn.execute(
        """SELECT order_intent_id,position_lot_id,position_lot_version,token_id,
                  order_action,state,updated_at
             FROM posterior_execution_intents
            WHERE state IN ('prepared','submitting','gateway_accepted','reconcile_required','partially_filled')
              AND json_extract(intent_json,'$.slug')=?
              AND json_extract(intent_json,'$.event_date')=?
            ORDER BY created_at,order_intent_id""",
        (_text(slug), _text(event_date)),
    ).fetchall()
    unresolved = [
        {
            "order_intent_id": _text(row[0]),
            "position_lot_id": _text(row[1]),
            "position_lot_version": _text(row[2]),
            "token_id": _text(row[3]),
            "order_action": _text(row[4]),
            "state": _text(row[5]),
            "updated_at": _text(row[6]),
        }
        for row in unresolved_rows
    ]
    identity = {
        "slug": _text(slug),
        "event_date": _text(event_date),
        "as_of": _text(as_of),
        "positions": positions,
        "unresolved_intents": unresolved,
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "complete": True,
        "position_snapshot_id": _id("positionsnapshot_", identity),
        "as_of": _text(as_of),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "positions": positions,
        "unresolved_intents": unresolved,
        "source": "dynamic_three_leg_position_lots",
        "legacy_edge_trades_consumed": False,
    }


__all__ = [
    "MANAGEMENT_SCOPE",
    "SCHEMA_VERSION",
    "apply_reconciled_fill_to_position_lot",
    "build_position_lot_identity",
    "load_dynamic_three_leg_position_snapshot",
]
