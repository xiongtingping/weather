"""SQLite audit schema for the production basket lane."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
import json

from .policy import load_policy


SCHEMA_VERSION = "basket_strategy_schema_v2"

DDL = """
CREATE TABLE IF NOT EXISTS basket_strategy_activation_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  generation INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('disabled','enabled','paused')),
  strategy_id TEXT NOT NULL,
  activation_sha256 TEXT NOT NULL,
  activation_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS basket_strategy_cycles (
  cycle_id TEXT PRIMARY KEY,
  strategy_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  event_date TEXT NOT NULL,
  city_slug TEXT NOT NULL DEFAULT '',
  plan_sha256 TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared','submitting','submitted','partially_filled','reconcile_required','failed','blocked')),
  total_cost_usdc REAL NOT NULL,
  quantity_per_bucket REAL NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(slug,event_date,plan_sha256)
);

CREATE INDEX IF NOT EXISTS idx_basket_strategy_cycles_state_time
ON basket_strategy_cycles(state,updated_at);

CREATE TABLE IF NOT EXISTS basket_strategy_capital_ledger (
  ledger_id TEXT PRIMARY KEY,
  cycle_id TEXT,
  slug TEXT NOT NULL,
  event_date TEXT NOT NULL,
  city_slug TEXT NOT NULL,
  entry_type TEXT NOT NULL CHECK(entry_type IN ('reserve','release','realized_loss','realized_profit')),
  amount_usdc REAL NOT NULL CHECK(amount_usdc >= 0),
  reason TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_basket_strategy_capital_ledger_scope
ON basket_strategy_capital_ledger(entry_type,event_date,city_slug,slug,created_at);

CREATE TABLE IF NOT EXISTS basket_strategy_legs (
  leg_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL,
  order_intent_id TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE,
  bucket TEXT NOT NULL,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side='YES'),
  limit_price REAL NOT NULL CHECK(limit_price > 0 AND limit_price < 1),
  requested_size REAL NOT NULL CHECK(requested_size > 0),
  requested_notional_usdc REAL NOT NULL CHECK(requested_notional_usdc > 0),
  order_type TEXT NOT NULL CHECK(order_type='FOK'),
  state TEXT NOT NULL CHECK(state IN ('prepared','submitting','exchange_accepted','filled','submit_failed','reconcile_required')),
  exchange_order_id TEXT,
  size_matched REAL NOT NULL DEFAULT 0,
  gateway_response_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(cycle_id) REFERENCES basket_strategy_cycles(cycle_id)
);

CREATE INDEX IF NOT EXISTS idx_basket_strategy_legs_cycle_state
ON basket_strategy_legs(cycle_id,state);

CREATE TABLE IF NOT EXISTS basket_strategy_policy_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  policy_id TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared','enabled','disabled')),
  policy_sha256 TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  production_total_usdc REAL NOT NULL,
  test_total_usdc REAL NOT NULL,
  test_daily_loss_cap_usdc REAL NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS basket_strategy_position_targets (
  target_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  target_version INTEGER NOT NULL,
  bucket TEXT NOT NULL,
  role TEXT NOT NULL,
  target_profit_usdc REAL NOT NULL,
  target_quantity REAL NOT NULL,
  filled_quantity REAL NOT NULL DEFAULT 0,
  pending_quantity REAL NOT NULL DEFAULT 0,
  delta_quantity REAL NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK(state IN ('targeted','topup_ready','satisfied','blocked','reconcile_required')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(cycle_id,target_version,bucket)
);

CREATE TABLE IF NOT EXISTS basket_strategy_parent_orders (
  parent_order_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL,
  target_version INTEGER NOT NULL,
  snapshot_id TEXT NOT NULL,
  bucket TEXT NOT NULL,
  requested_quantity REAL NOT NULL,
  requested_notional_usdc REAL NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared','submitting','submitted','filled','partially_filled','reconcile_required','cancelled')),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS basket_strategy_child_orders (
  child_order_id TEXT PRIMARY KEY,
  parent_order_id TEXT NOT NULL,
  leg_id TEXT,
  price_level INTEGER NOT NULL,
  limit_price REAL NOT NULL,
  target_quantity REAL NOT NULL,
  target_notional_usdc REAL NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared','submitting','submitted','filled','submit_failed','reconcile_required')),
  idempotency_key TEXT NOT NULL UNIQUE,
  exchange_order_id TEXT,
  size_matched REAL NOT NULL DEFAULT 0,
  gateway_response_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(parent_order_id) REFERENCES basket_strategy_parent_orders(parent_order_id),
  FOREIGN KEY(leg_id) REFERENCES basket_strategy_legs(leg_id)
);

CREATE INDEX IF NOT EXISTS idx_basket_strategy_targets_cycle
ON basket_strategy_position_targets(cycle_id,target_version,state);

CREATE INDEX IF NOT EXISTS idx_basket_strategy_parent_cycle
ON basket_strategy_parent_orders(cycle_id,target_version,state);

CREATE INDEX IF NOT EXISTS idx_basket_strategy_child_parent
ON basket_strategy_child_orders(parent_order_id,state);
"""


def apply_basket_strategy_schema(conn: sqlite3.Connection) -> dict[str, object]:
    conn.executescript(DDL)
    cycle_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(basket_strategy_cycles)")}
    if "city_slug" not in cycle_columns:
        conn.execute("ALTER TABLE basket_strategy_cycles ADD COLUMN city_slug TEXT NOT NULL DEFAULT ''")
    policy = load_policy()
    risk = policy.get("risk_budget") if isinstance(policy.get("risk_budget"), dict) else {}
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    conn.execute(
        """INSERT INTO basket_strategy_policy_state(
             singleton,policy_id,schema_version,state,policy_sha256,policy_json,
             production_total_usdc,test_total_usdc,test_daily_loss_cap_usdc,updated_at)
           VALUES(1,?,?,?, ?,?,?,?,?,?)
           ON CONFLICT(singleton) DO UPDATE SET
             policy_id=excluded.policy_id,schema_version=excluded.schema_version,
             policy_sha256=excluded.policy_sha256,policy_json=excluded.policy_json,
             production_total_usdc=excluded.production_total_usdc,
             test_total_usdc=excluded.test_total_usdc,
             test_daily_loss_cap_usdc=excluded.test_daily_loss_cap_usdc,
             updated_at=excluded.updated_at""",
        (
            policy.get("policy_id", ""),
            policy.get("schema", ""),
            policy.get("state", "prepared"),
            policy.get("policy_sha256", ""),
            json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            float(risk.get("production_total_usdc", 0.0) or 0.0),
            float(risk.get("test_total_usdc", 0.0) or 0.0),
            float(risk.get("test_daily_loss_cap_usdc", 0.0) or 0.0),
            now,
        ),
    )
    conn.commit()
    return {"schema": SCHEMA_VERSION, "ok": True, "policy_id": policy.get("policy_id"), "policy_sha256": policy.get("policy_sha256")}


__all__ = ["DDL", "SCHEMA_VERSION", "apply_basket_strategy_schema"]
