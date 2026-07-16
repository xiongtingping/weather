from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from sequential_bayes_schema_contract import apply_forward_schema


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/production_transaction_b_no_order_drill.py"
spec = importlib.util.spec_from_file_location("production_transaction_b_no_order_drill", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def _backup(path: Path) -> None:
    conn = sqlite3.connect(path)
    apply_forward_schema(conn)
    conn.executescript("""
      CREATE TABLE event_fact_history (id INTEGER PRIMARY KEY AUTOINCREMENT,run_date TEXT NOT NULL,slug TEXT NOT NULL,event_date TEXT NOT NULL,fact_as_of TEXT NOT NULL,composer_version TEXT NOT NULL,fact_json TEXT NOT NULL,created_at TEXT NOT NULL,correlation_id TEXT,fact_snapshot_id TEXT UNIQUE,posterior_run_id TEXT);
      CREATE TABLE event_fact_latest (slug TEXT NOT NULL,event_date TEXT NOT NULL,fact_id INTEGER NOT NULL,updated_at TEXT NOT NULL,fact_snapshot_id TEXT,correlation_id TEXT,PRIMARY KEY(slug,event_date));
      CREATE TABLE auto_orders (id INTEGER PRIMARY KEY);
      CREATE TABLE auto_trade_decisions (id INTEGER PRIMARY KEY);
      CREATE TABLE edge_trades (id INTEGER PRIMARY KEY);
    """)
    conn.commit(); conn.close()


def test_no_order_drill_uses_backup_and_keeps_order_tables_unchanged(tmp_path) -> None:
    backup = tmp_path / "archive.verified.db"
    _backup(backup)
    result = module.run_drill(backup)
    assert result["ok"] is True
    assert result["state"] == "temporary_no_order_drill_passed"
    assert result["weak_forecast_revision_transaction"]["ok"] is True
    assert result["weak_forecast_revision_transaction"]["places_orders"] is False
    assert result["weak_forecast_revision_outbox_delta"] == 0
    assert result["order_and_decision_table_deltas"] == {"auto_orders": 0, "auto_trade_decisions": 0, "edge_trades": 0}
    assert backup.exists()


def test_no_order_drill_refuses_live_archive_path() -> None:
    result = module.run_drill(Path(module.ROOT) / "data/archive.db")
    assert result["ok"] is False
    assert result["blockers"] == ["verified_maintenance_backup_required_not_live_archive"]
