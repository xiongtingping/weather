from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.database import SHARED_LIVE_DB, resolve_target_database
from bayes_basket_target.migration_quiescence import validate_writer_quiescence_receipt
from scripts.bayes_basket_target_no_live_activation_apply import (
    _online_no_live_cas_guard,
    apply_activation,
)
from scripts import bayes_basket_target_no_live_activation_apply as activation_apply
from scripts.bayes_basket_target_production_readiness import audit
from scripts.initialize_bayes_basket_target_ssot import initialize


ROOT = Path(__file__).resolve().parents[1]
ACTIVATION = ROOT / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"


def test_shared_live_database_is_rejected_before_sqlite_open() -> None:
    with pytest.raises(ValueError, match="must_not_be_shared_live"):
        resolve_target_database(SHARED_LIVE_DB)


def test_initialize_isolated_target_db_is_idempotent_and_no_live(tmp_path: Path) -> None:
    db = tmp_path / "bayes-target.db"
    first = initialize(db=db, activation_path=ACTIVATION)
    second = initialize(db=db, activation_path=ACTIVATION)

    assert first["state"] == "initialized"
    assert second["state"] == "already_initialized"
    assert first["database_scope"] == "isolated_target_no_live"
    assert first["boundary"]["writes_shared_live_database"] is False
    assert first["capital_pool"]["balance_usdc"] == 0.0
    assert first["activation"]["state"] == "enabled"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM basket_strategy_activation_state").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM basket_target_capital_pool").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0


def test_isolated_activation_apply_does_not_require_stopping_live_writers(tmp_path: Path) -> None:
    db = tmp_path / "bayes-target.db"
    initialize(db=db, activation_path=ACTIVATION)
    backup = tmp_path / "bayes-target.backup.db"
    result = apply_activation(db=db, activation_path=ACTIVATION, backup_path=backup)

    assert result["generation"] == 2
    assert result["orders_submitted"] == 0
    assert backup.is_file()
    assert result["backup"]["quick_check"] == "ok"
    assert result["backup"]["bytes"] == backup.stat().st_size
    validated = validate_writer_quiescence_receipt(result["writer_quiescence"], db_path=db)
    assert validated["probe_mode"] == "isolated_target_database_scope"


def test_online_no_live_activation_cas_preserves_blocked_cycle(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bayes-target.db"
    initialize(db=db, activation_path=ACTIVATION)
    held = sqlite3.connect(db)
    try:
        held.execute(
            "INSERT INTO basket_target_execution_cycles VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "cycle-1", "plan-1", "RKPK", "2026-07-27", "Asia/Seoul",
                "basket", "a" * 64, 0.0, "prepared",
                "2026-07-27T00:00:00Z", "2026-07-27T00:00:00Z",
            ),
        )
        held.execute(
            "INSERT INTO auto_trade_decisions VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "decision-1", "cycle-1", "plan-1", "RKPK", "2026-07-27",
                "Asia/Seoul", "basket", "blocked", 0, None, '["no_live"]', "{}",
                "b" * 64, "2026-07-27T00:00:00Z",
            ),
        )
        held.commit()
        result = apply_activation(
            db=db,
            activation_path=ACTIVATION,
            backup_path=tmp_path / "bayes-target.backup.db",
            allow_online_no_live_cas=True,
        )
    finally:
        held.close()

    assert result["activation_update_mode"] == "online_no_live_compare_and_swap"
    assert result["online_no_live_guard"]["uncovered_cycle_count"] == 0
    assert result["online_no_live_guard"]["unsafe_execution_counts"] == {
        "basket_target_order_intents": 0,
        "basket_target_capital_reservations": 0,
        "basket_target_order_events": 0,
    }
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions"
        ).fetchone()[0] == 1


def test_online_no_live_activation_guard_rejects_order_residue(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bayes-target.db"
    initialize(db=db, activation_path=ACTIVATION)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO basket_target_order_events VALUES"
            "(?,?,?,?,?,?,?,?,?)",
            (
                "event-1", "intent-1", None, "POST_ACK",
                "2026-07-27T00:00:00Z", 0.0, 0.0, "{}", "c" * 64,
            ),
        )
    activation = json.loads(ACTIVATION.read_text(encoding="utf-8"))
    with pytest.raises(
        RuntimeError, match="online_activation_execution_state_not_empty"
    ):
        _online_no_live_cas_guard(db, next_activation=activation)


def test_activation_backup_resource_failure_leaves_no_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "bayes-target.db"
    backup = tmp_path / "bayes-target.partial.db"
    initialize(db=db, activation_path=ACTIVATION)
    monkeypatch.setattr(activation_apply, "MAX_TARGET_DB_BYTES", 0)

    with pytest.raises(
        RuntimeError,
        match="activation_backup_source_bytes_exceeded",
    ):
        apply_activation(
            db=db,
            activation_path=ACTIVATION,
            backup_path=backup,
        )
    assert not backup.exists()


def test_readiness_rejects_explicit_shared_live_db_without_opening_it(tmp_path: Path) -> None:
    manifest = tmp_path / "market_ws_latest_quotes.json"
    manifest.write_text("{}\n", encoding="utf-8")
    result = audit(
        db=SHARED_LIVE_DB,
        activation_path=ACTIVATION,
        manifest_path=manifest,
        probe_services=False,
    )
    assert result["ok"] is False
    assert result["blockers"] == ["target_database_must_not_be_shared_live_database"]
