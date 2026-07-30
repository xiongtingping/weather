from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.bayes_basket_target_performance_audit import (
    DEFAULT_PERFORMANCE_BUDGET,
    MAX_SAMPLES,
    PerformanceAuditError,
    _metric,
    _read_transaction_acquire,
    _ws_snapshot_evidence,
    audit,
)


def test_metric_reports_p95_and_p99_without_retaining_raw_samples() -> None:
    metric = _metric([1.0, 2.0, 3.0, 4.0, 5.0])

    assert metric["sample_count"] == 5
    assert metric["p95_ms"] == 5.0
    assert metric["p99_ms"] == 5.0
    assert "values" not in metric


def test_performance_audit_rejects_unbounded_sampling() -> None:
    with pytest.raises(PerformanceAuditError, match="samples_out_of_bounds"):
        audit(samples=MAX_SAMPLES + 1)


def test_ws_cache_is_reported_as_evidence_only_and_not_as_materialized_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE basket_official_ws_evidence_cache ("
            "observed_at TEXT, updated_at TEXT, evidence_only INTEGER, authorizes_order INTEGER)"
        )
        conn.execute(
            "CREATE TABLE basket_official_ws_snapshot ("
            "quote_observed_at TEXT, created_at TEXT, state TEXT)"
        )
        conn.execute(
            "INSERT INTO basket_official_ws_evidence_cache VALUES (?, ?, 1, 0)",
            ("2026-07-24T00:00:00Z", "2026-07-24T00:00:01Z"),
        )
        conn.commit()

    result = _ws_snapshot_evidence(
        db,
        now_utc=datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc),
    )

    assert result["materialized_snapshot_count"] == 0
    assert result["evidence_cache_count"] == 1
    assert result["evidence_cache_is_non_authorizing"] is True
    assert result["evidence_cache_is_not_materialized_snapshot"] is True


def test_audit_remains_blocked_when_budget_lock_and_snapshot_evidence_are_missing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE basket_official_ws_evidence_cache ("
            "observed_at TEXT, updated_at TEXT, evidence_only INTEGER, authorizes_order INTEGER)"
        )
        conn.execute(
            "CREATE TABLE basket_official_ws_snapshot ("
            "quote_observed_at TEXT, created_at TEXT, state TEXT)"
        )
        conn.commit()

    result = audit(
        db_path=db,
        raw_root=tmp_path / "raw",
        samples=1,
        now_utc=datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc),
        runtime_runner=lambda: {"ok": True},
        resource_runner=lambda: {"ok": True},
    )

    assert result["state"] == "BLOCKED_EVIDENCE"
    assert "approved_performance_budget_not_declared" not in result["blockers"]
    assert "target_owned_writer_lock_wait_telemetry_missing" in result["blockers"]
    assert "official_ws_materialized_snapshot_missing" in result["blockers"]
    assert result["checks"]["approved_performance_budget_present"] is True
    assert result["approved_budget"]["approval_scope"] == "bounded_read_only_audit_only"
    assert result["boundary"]["orders_submitted"] == 0
    assert result["boundary"]["calls_order_gateway"] is False


def test_reader_accepts_bounded_target_owned_writer_telemetry(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE basket_target_writer_lock_wait_telemetry ("
            "singleton INTEGER PRIMARY KEY, sample_count INTEGER, total_wait_ms REAL, "
            "max_wait_ms REAL, last_wait_ms REAL, last_operation TEXT, "
            "last_acquired_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO basket_target_writer_lock_wait_telemetry VALUES "
            "(1, 2, 3.0, 2.0, 1.5, 'target_test_writer', '2026-07-24T00:00:00Z', '2026-07-24T00:00:01Z')"
        )
        conn.commit()

    result = _read_transaction_acquire(db)

    assert result["writer_lock_wait_sample_count"] == 2
    assert result["writer_lock_wait_telemetry"] == "OBSERVED"
    assert result["writer_lock_wait_last_operation"] == "target_test_writer"


def test_declared_performance_budget_is_target_only_and_below_hard_limits() -> None:
    from bayes_basket_target.performance_budget import load_performance_budget

    budget = load_performance_budget(DEFAULT_PERFORMANCE_BUDGET)

    assert budget["execution_mode"] == "production_no_live"
    assert budget["approval_scope"] == "bounded_read_only_audit_only"
    assert budget["live_submit_authorized"] is False
    assert budget["places_orders"] is False
    assert budget["writes_production_db"] is False
    assert budget["calls_order_gateway"] is False


def test_performance_budget_rejects_symlink_and_outside_target_config(tmp_path: Path) -> None:
    from bayes_basket_target.performance_budget import PerformanceBudgetError, load_performance_budget

    symlink = tmp_path / "budget-link.json"
    symlink.symlink_to(DEFAULT_PERFORMANCE_BUDGET)
    with pytest.raises(PerformanceBudgetError, match="symlink_forbidden"):
        load_performance_budget(symlink)

    outside = tmp_path / "budget.json"
    outside.write_text(DEFAULT_PERFORMANCE_BUDGET.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(PerformanceBudgetError, match="outside_target_config"):
        load_performance_budget(outside)


def test_performance_audit_keeps_boundary_blocked_when_budget_path_is_tampered(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE basket_official_ws_evidence_cache ("
            "observed_at TEXT, updated_at TEXT, evidence_only INTEGER, authorizes_order INTEGER)"
        )
        conn.execute(
            "CREATE TABLE basket_official_ws_snapshot ("
            "quote_observed_at TEXT, created_at TEXT, state TEXT)"
        )
        conn.commit()

    result = audit(
        db_path=db,
        raw_root=tmp_path / "raw",
        samples=1,
        now_utc=datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc),
        performance_budget_path=tmp_path / "outside-budget.json",
        runtime_runner=lambda: {"ok": True},
        resource_runner=lambda: {"ok": True},
    )

    assert "approved_performance_budget_not_declared" in result["blockers"]
    assert result["checks"]["approved_performance_budget_present"] is False
    assert result["boundary"]["live_submit_authorized"] is False
    assert result["boundary"]["calls_order_gateway"] is False
