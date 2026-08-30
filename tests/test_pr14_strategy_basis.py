from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from bayes_basket_target.canonical_expected_scope_manifest import validate_scope_basis
from bayes_basket_target.canonical_ssot_v12_migration import TARGET_TABLE_DDL, TARGET_TRIGGER_DDL
from bayes_basket_target.pr14_strategy_basis import (
    PR14StrategyBasisError,
    build_pr14_strategy_artifacts,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest
from bayes_basket_target.fleet import expected_station_count


AS_OF = "2026-08-04T12:00:00Z"


def _db(tmp_path: Path, *, will_order_station: str = "") -> Path:
    path = tmp_path / "pr14.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(TARGET_TABLE_DDL)
        for ddl in TARGET_TRIGGER_DDL:
            conn.execute(ddl)
        conn.execute(
            "CREATE TABLE auto_trade_decisions("
            "decision_id TEXT PRIMARY KEY,station_id TEXT,decided_state TEXT,"
            "will_order INTEGER,reason_codes_json TEXT,decision_sha256 TEXT,created_at TEXT)"
        )
        for station in coordinate_registry_manifest()["station_ids"]:
            conn.execute(
                "INSERT INTO auto_trade_decisions VALUES(?,?,?,?,?,?,?)",
                (
                    "decision-" + station,
                    station,
                    "ready" if station == will_order_station else "blocked",
                    1 if station == will_order_station else 0,
                    '["p1_final_authority_pending"]',
                    station.lower().ljust(64, "0")[:64],
                    AS_OF,
                ),
            )
    return path


def test_pr14_has_45_unresolved_rows_and_two_empty_expected_bases(tmp_path: Path) -> None:
    path = _db(tmp_path)
    before = path.stat()
    policy, utility, terminals, receipt = build_pr14_strategy_artifacts(
        path, as_of_utc=AS_OF
    )
    after = path.stat()
    assert policy["expected_keys"] == utility["expected_keys"] == []
    assert len(policy["unresolved_input_receipts"]) == expected_station_count()
    assert len(utility["unresolved_input_receipts"]) == expected_station_count()
    assert terminals["terminal_scope_count"] == receipt["terminal_scope_count"] == expected_station_count()
    assert receipt["strategy_policy_expected_key_count"] == 0
    assert receipt["strategy_utility_expected_key_count"] == 0
    assert receipt["will_order_scope_count"] == 0
    assert receipt["hard_no_strategy_removed"] is True
    assert receipt["buy_no_intent_count"] == receipt["orders_submitted"] == 0
    assert receipt["publishes_pr04_pointer"] is False
    assert receipt["starts_services"] is False
    assert receipt["releases_live_pause"] is False
    validate_scope_basis(policy)
    validate_scope_basis(utility)
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_station_rows_bind_exact_latest_decision_and_pure_yes_boundary(tmp_path: Path) -> None:
    policy, utility, terminals, _receipt = build_pr14_strategy_artifacts(
        _db(tmp_path), as_of_utc=AS_OF
    )
    rows = terminals["terminal_scopes"]
    assert [row["station_id"] for row in rows] == coordinate_registry_manifest()["station_ids"]
    assert set(policy["unresolved_input_receipts"]) == {row["content_sha256"] for row in rows}
    assert set(utility["unresolved_input_receipts"]) == {row["content_sha256"] for row in rows}
    assert all(row["latest_auto_trade_decision"]["will_order"] is False for row in rows)
    assert all(
        row["terminal_by_scope"]["PURE_YES_BASKET"]["buy_no_intent_emitted"] is False
        for row in rows
    )
    assert all(row["utility_contract_state"] == "DEFINED_UNBOUND" for row in rows)


def test_missing_station_decision_fails_closed(tmp_path: Path) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM auto_trade_decisions WHERE station_id='ZUUU'")
    with pytest.raises(PR14StrategyBasisError, match="decision_set_not_exact_45"):
        build_pr14_strategy_artifacts(path, as_of_utc=AS_OF)


def test_will_order_decision_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PR14StrategyBasisError, match="decision_not_fail_closed"):
        build_pr14_strategy_artifacts(
            _db(tmp_path, will_order_station="KATL"), as_of_utc=AS_OF
        )


def test_pr14_artifacts_are_deterministic(tmp_path: Path) -> None:
    path = _db(tmp_path)
    assert build_pr14_strategy_artifacts(path, as_of_utc=AS_OF) == (
        build_pr14_strategy_artifacts(path, as_of_utc=AS_OF)
    )
