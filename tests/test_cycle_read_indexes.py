"""Three production-cycle reads get the index they stop one column short of.

User ruling 2026-10-03 「停机建索引」.  Each read is taken here exactly as
production issues it -- traced off the real caller where there is one -- and
its plan is pinned on a fresh store built from the production DDL (the live
store has no sqlite_stat tables, so its planner reads the same schema):

* the forward training check's order-state snapshot by version;
* the basket's open exposure, a decision probe per filled or resting order;
* "does this station issue TAF TX";
* the quote-lease read: a station-day's newest quote expiry (2026-10-04).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from bayes_basket_target.account_equity import open_exposure_by_station_day
from bayes_basket_target.native_ensemble_p0 import _station_issues_taf_tx
from bayes_basket_target.ssot import (
    apply_production_ssot_schema,
    declared_index_names,
    missing_declared_indexes,
)

ROOT = Path(__file__).resolve().parents[1]
VERSION = "basket_order_state_snapshot_version_idx"
EXPOSURE = "auto_trade_decisions_exposure_idx"
TAF_TX = "calibration_forward_taf_capture_tx_idx"
EXPIRY = "basket_official_ws_snapshot_scope_expiry_idx"
NEW = (VERSION, EXPOSURE, TAF_TX, EXPIRY)


def _store(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    apply_production_ssot_schema(conn)
    return conn


def _migration():
    spec = importlib.util.spec_from_file_location(
        "cycle_read_indexes",
        ROOT / "scripts" / "apply_bayes_basket_target_cycle_read_indexes.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _traced(conn: sqlite3.Connection, call) -> list[str]:
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        call()
    finally:
        conn.set_trace_callback(None)
    return statements


def _plan(conn: sqlite3.Connection, sql: str) -> str:
    sql = sql.replace("'2026-09-30'", "?")
    return " | ".join(
        str(row[3]) for row in conn.execute(
            "EXPLAIN QUERY PLAN " + sql, ("x",) * sql.count("?"),
        )
    )


def test_the_migration_states_the_ddl_verbatim() -> None:
    declared = declared_index_names()
    assert {name: declared.get(name) for name in NEW} == {
        VERSION: "basket_order_state_snapshot",
        EXPOSURE: "auto_trade_decisions",
        TAF_TX: "calibration_forward_taf_capture",
        EXPIRY: "basket_official_ws_snapshot",
    }
    ddl = " ".join((ROOT / "bayes_basket_target" / "ssot.py").read_text(encoding="utf-8").split())
    for name, statement in _migration().STATEMENTS:
        assert " ".join(statement.split()) in ddl, name


def test_the_order_state_snapshot_is_found_by_its_version(tmp_path) -> None:
    conn = _store(tmp_path / "ssot.db")
    sql = (
        "SELECT * FROM basket_order_state_snapshot WHERE station_id=? AND event_date=? "
        "AND order_state_version=? AND state='COMMITTED' LIMIT 2"
    )
    source = (ROOT / "bayes_basket_target" / "forward_training_validation.py").read_text(encoding="utf-8")
    assert '"station_id=? AND event_date=? AND order_state_version=? "' in source
    plan = _plan(conn, sql)
    assert f"INDEX {VERSION} (station_id=? AND event_date=? AND order_state_version=? AND state=?)" in plan, plan


def test_the_exposure_probe_stays_in_the_index(tmp_path) -> None:
    conn = _store(tmp_path / "ssot.db")
    statements = _traced(conn, lambda: open_exposure_by_station_day(conn, as_of_utc="2026-10-03T12:00:00Z"))
    sql = next(s for s in statements if "FROM basket_target_order_reconciliations r" in s)
    plan = _plan(conn, sql)
    assert f"SEARCH d USING COVERING INDEX {EXPOSURE}" in plan, plan


def test_the_taf_tx_question_reads_only_rows_that_carry_one(tmp_path) -> None:
    conn = _store(tmp_path / "ssot.db")
    statements = _traced(conn, lambda: _station_issues_taf_tx(conn, station_id="KLGA"))
    sql = next(s for s in statements if "tx_c IS NOT NULL" in s)
    plan = _plan(conn, sql.replace("'KLGA'", "?"))
    assert f"INDEX {TAF_TX}" in plan, plan


def test_a_station_days_newest_quote_expiry_is_one_seek(tmp_path) -> None:
    from bayes_basket_target.production_worker import TargetProductionWorker

    conn = _store(tmp_path / "ssot.db")
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    statements = _traced(conn, lambda: worker._scope_day_quote_expiry("2026-10-05", "KLGA"))
    sql = next(s for s in statements if "MAX(quote_expires_at)" in s)
    plan = _plan(conn, sql.replace("'KLGA'", "?").replace("'2026-10-05'", "?"))
    assert f"SEARCH basket_official_ws_snapshot USING COVERING INDEX {EXPIRY} (station_id=? AND event_date=?)" in plan, plan
    # The fleet-wide form keeps the day index.
    statements = _traced(conn, lambda: worker._scope_day_quote_expiry("2026-10-05"))
    sql = next(s for s in statements if "MAX(quote_expires_at)" in s)
    assert "basket_official_ws_snapshot_day_expiry_idx" in _plan(conn, sql.replace("'2026-10-05'", "?"))


def _store_without_the_new_indexes(tmp_path: Path) -> Path:
    path = tmp_path / "live.db"
    conn = _store(path)
    for name in NEW:
        conn.execute(f"DROP INDEX {name}")
    conn.execute("PRAGMA foreign_keys=OFF")
    # Rows only need to exist for the counts each index must reproduce.
    for table, rows in (
        ("basket_order_state_snapshot", [
            {"order_state_snapshot_id": f"s{i}", "station_id": f"S{i % 3}", "event_date": "2026-10-03",
             "order_state_version": f"ord_{i}", "observed_at": "2026-10-03T00:00:00Z", "known": 1,
             "order_state_json": "{}", "content_sha256": f"{i:064x}",
             "state": "QUARANTINED" if i == 4 else "COMMITTED", "created_at": "2026-10-03T00:00:00Z"}
            for i in range(9)
        ]),
        ("auto_trade_decisions", [
            {"decision_id": f"d{i}", "station_id": "S0", "event_date": "2026-10-03",
             "strategy_lane": "basket", "decided_state": "blocked", "will_order": 0,
             "target_execution_hard_deadline_at": None, "decision_scope_key": None,
             "decision_sha256": f"{i + 100:064x}"}
            for i in range(7)
        ]),
        ("calibration_forward_taf_capture", [
            {"taf_capture_id": f"t{i}", "station_id": f"S{i % 2}", "revision_sequence": 1,
             "tx_c": 20.0 if i % 3 == 0 else None, "tx_available": 1 if i % 3 == 0 else 0,
             "source_object_sha256": f"{i + 200:064x}", "source_capture_sha256": f"{i + 300:064x}",
             "content_sha256": f"{i + 400:064x}", "cloud_cover_fraction": 0.5,
             "precipitation_probability": 0.1, "wind_speed_mps": 3.0,
             "source_class": "official_forward_native_receipt",
             "authority_scope": "PRODUCTION_NO_LIVE_ONLY"}
            for i in range(10)
        ]),
        ("basket_official_ws_snapshot", [
            {"snapshot_id": f"w{i}", "station_id": f"S{i % 4}", "event_date": "2026-10-04",
             "sequence": i, "quote_expires_at": f"2026-10-04T00:0{i}:00Z",
             "state": "COMMITTED", "content_sha256": f"{i + 500:064x}",
             "quote_freshness_policy_sha256": "f" * 64, "quote_source": "OFFICIAL_CLOB_WS",
             "remaining_budget_usdc": 0.0}
            for i in range(6)
        ]),
    ):
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        for row in rows:
            full = {c: f"v-{row.get(c, c)}-{id(row)}" for c in columns}
            full.update(row)
            conn.execute(
                f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(full[c] for c in columns),
            )
    conn.commit()
    conn.close()
    return path


def test_the_migration_adds_all_three_and_proves_them_against_their_tables(tmp_path) -> None:
    path = _store_without_the_new_indexes(tmp_path)
    before = sqlite3.connect(path)
    assert sorted(missing_declared_indexes(before)) == sorted(NEW)
    before.close()
    receipt = _migration().migrate(db_path=path, confirmed_writer_stopped=False)
    assert receipt["state"] == "migrated"
    assert receipt["indexes_created"] == sorted(NEW)
    rows = {item["index"]: item["rows"] for item in receipt["index_verification"]}
    assert rows == {VERSION: 8, EXPOSURE: 7, TAF_TX: 4, EXPIRY: 6}
    assert all(item["planner_used_index"] for item in receipt["index_verification"])
    again = _migration().migrate(db_path=path, confirmed_writer_stopped=False)
    assert again["indexes_created"] == [] and sorted(again["indexes_already_present"]) == sorted(NEW)
    after = sqlite3.connect(path)
    assert missing_declared_indexes(after) == []


def test_the_production_store_needs_the_writer_stopped(tmp_path, monkeypatch) -> None:
    module = _migration()
    path = _store_without_the_new_indexes(tmp_path)
    monkeypatch.setattr(module, "DEFAULT_DB", path.resolve())
    import pytest

    from bayes_basket_target.contracts import ContractError

    with pytest.raises(ContractError, match="quiescence_confirmation_required"):
        module.migrate(db_path=path, confirmed_writer_stopped=False)
    monkeypatch.setattr(module, "_service_running", lambda: True)
    with pytest.raises(ContractError, match="writer_still_running"):
        module.migrate(db_path=path, confirmed_writer_stopped=True)
    assert json.loads(json.dumps({"ok": True}))
