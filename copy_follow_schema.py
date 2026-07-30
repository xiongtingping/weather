"""SQLite audit schema for the independent copy-follow trading line."""

from __future__ import annotations

import sqlite3


SCHEMA_VERSION = "weather_copy_follow_schema_v1"

DDL = """
CREATE TABLE IF NOT EXISTS copy_follow_source_activities (
  activity_id TEXT PRIMARY KEY,
  source_user TEXT NOT NULL,
  event_slug TEXT NOT NULL,
  event_date TEXT NOT NULL,
  city_slug TEXT NOT NULL,
  bucket TEXT NOT NULL,
  usdc_size REAL NOT NULL CHECK(usdc_size > 0),
  share_size REAL,
  price REAL,
  activity_timestamp INTEGER,
  payload_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_copy_follow_activity_event_bucket
ON copy_follow_source_activities(source_user,event_slug,event_date,bucket);

CREATE TABLE IF NOT EXISTS copy_follow_plans (
  plan_id TEXT PRIMARY KEY,
  source_user TEXT NOT NULL,
  event_slug TEXT NOT NULL,
  event_date TEXT NOT NULL,
  city_slug TEXT NOT NULL,
  selection_signature TEXT NOT NULL,
  buckets_json TEXT NOT NULL,
  source_notional_json TEXT NOT NULL,
  allocation_json TEXT NOT NULL,
  main_bucket TEXT NOT NULL,
  source_total_usdc REAL NOT NULL CHECK(source_total_usdc > 0),
  total_budget_usdc REAL NOT NULL CHECK(total_budget_usdc > 0),
  state TEXT NOT NULL CHECK(state IN ('planned','submitting','submitted','reconcile_required','failed','blocked','disabled')),
  plan_json TEXT NOT NULL,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(source_user,event_slug,selection_signature)
);

CREATE INDEX IF NOT EXISTS idx_copy_follow_plans_city_state
ON copy_follow_plans(city_slug,event_date,state,updated_at);

CREATE TABLE IF NOT EXISTS copy_follow_legs (
  leg_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  bucket TEXT NOT NULL,
  token_id TEXT NOT NULL,
  limit_price REAL NOT NULL CHECK(limit_price > 0 AND limit_price < 1),
  requested_size REAL NOT NULL CHECK(requested_size > 0),
  requested_notional_usdc REAL NOT NULL CHECK(requested_notional_usdc > 0),
  order_type TEXT NOT NULL CHECK(order_type='FOK'),
  idempotency_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN ('prepared','submitting','exchange_accepted','submit_failed','reconcile_required')),
  exchange_order_id TEXT,
  size_matched REAL NOT NULL DEFAULT 0,
  gateway_response_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(plan_id) REFERENCES copy_follow_plans(plan_id)
);

CREATE INDEX IF NOT EXISTS idx_copy_follow_legs_plan_state
ON copy_follow_legs(plan_id,state);

-- Dynamic copy-follow orders include protected initial FAK entries and later
-- standing GTC rebalances.  Keep them separate from the historical four-leg
-- FOK audit table above so the two execution contracts remain auditable.
CREATE TABLE IF NOT EXISTS copy_follow_orders (
  local_order_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  bucket TEXT NOT NULL,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  limit_price REAL NOT NULL CHECK(limit_price > 0 AND limit_price < 1),
  requested_size REAL NOT NULL CHECK(requested_size > 0),
  requested_notional_usdc REAL NOT NULL CHECK(requested_notional_usdc > 0),
  order_type TEXT NOT NULL CHECK(order_type IN ('GTC','FAK')),
  idempotency_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state IN ('prepared','submitting','open','partially_filled','filled','cancel_submitting','cancelled','submit_failed','reconcile_required')),
  exchange_order_id TEXT,
  size_matched REAL NOT NULL DEFAULT 0,
  gateway_response_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(plan_id) REFERENCES copy_follow_plans(plan_id)
);

CREATE INDEX IF NOT EXISTS idx_copy_follow_orders_plan_state
ON copy_follow_orders(plan_id,state,updated_at);

CREATE TABLE IF NOT EXISTS copy_follow_notifications (
  notification_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  plan_id TEXT,
  event_type TEXT NOT NULL,
  channel TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','sent','blocked_missing_recipient','failed')),
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_copy_follow_notifications_state
ON copy_follow_notifications(state,updated_at);
"""


def apply_copy_follow_schema(conn: sqlite3.Connection) -> dict[str, object]:
    conn.executescript(DDL)
    _migrate_dynamic_order_type_constraint(conn)
    return {"schema": SCHEMA_VERSION, "ok": True}


def _migrate_dynamic_order_type_constraint(conn: sqlite3.Connection) -> None:
    """Upgrade the first draft's GTC-only table without losing its audit rows."""

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='copy_follow_orders'"
    ).fetchone()
    sql = str(row[0] or "") if row else ""
    if not sql or "order_type='GTC'" not in sql or "order_type IN ('GTC','FAK')" in sql:
        return
    conn.execute("DROP INDEX IF EXISTS idx_copy_follow_orders_plan_state")
    conn.execute("ALTER TABLE copy_follow_orders RENAME TO copy_follow_orders_legacy_v1")
    conn.execute(
        """CREATE TABLE copy_follow_orders (
          local_order_id TEXT PRIMARY KEY,
          plan_id TEXT NOT NULL,
          bucket TEXT NOT NULL,
          token_id TEXT NOT NULL,
          side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
          limit_price REAL NOT NULL CHECK(limit_price > 0 AND limit_price < 1),
          requested_size REAL NOT NULL CHECK(requested_size > 0),
          requested_notional_usdc REAL NOT NULL CHECK(requested_notional_usdc > 0),
          order_type TEXT NOT NULL CHECK(order_type IN ('GTC','FAK')),
          idempotency_key TEXT NOT NULL UNIQUE,
          state TEXT NOT NULL CHECK(state IN ('prepared','submitting','open','partially_filled','filled','cancel_submitting','cancelled','submit_failed','reconcile_required')),
          exchange_order_id TEXT,
          size_matched REAL NOT NULL DEFAULT 0,
          gateway_response_json TEXT NOT NULL DEFAULT '{}',
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(plan_id) REFERENCES copy_follow_plans(plan_id)
        )"""
    )
    conn.execute(
        """INSERT INTO copy_follow_orders(
             local_order_id,plan_id,bucket,token_id,side,limit_price,requested_size,
             requested_notional_usdc,order_type,idempotency_key,state,exchange_order_id,
             size_matched,gateway_response_json,last_error,created_at,updated_at)
           SELECT local_order_id,plan_id,bucket,token_id,side,limit_price,requested_size,
                  requested_notional_usdc,order_type,idempotency_key,state,exchange_order_id,
                  size_matched,gateway_response_json,last_error,created_at,updated_at
             FROM copy_follow_orders_legacy_v1"""
    )
    conn.execute("DROP TABLE copy_follow_orders_legacy_v1")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_copy_follow_orders_plan_state ON copy_follow_orders(plan_id,state,updated_at)"
    )


__all__ = ["DDL", "SCHEMA_VERSION", "apply_copy_follow_schema"]
