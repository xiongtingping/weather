from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from scripts.reported_trajectory_no_daemon import TargetProductionWorker
from bayes_basket_target.database import SHARED_LIVE_DB
from scripts.reported_trajectory_no_daemon import (
    DEFAULT_TARGET_ACTIVATION,
    DEFAULT_TARGET_DB,
)
from production_probability_v6_live_route_contract import (
    ROUTE_FILES,
    activate_v6_live_routes,
    audit_v6_live_routes,
    build_v6_live_route_targets,
)


ROOT = Path(__file__).resolve().parents[1]


def _fixture_root(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    for relative in ROUTE_FILES:
        shutil.copy2(ROOT / relative, tmp_path / relative)
    shutil.copy2(
        ROOT / "scripts/install_reported_trajectory_no_system_services.sh",
        tmp_path / "scripts/install_reported_trajectory_no_system_services.sh",
    )
    return tmp_path


def test_resident_route_attestation_is_deterministic_and_non_mutating(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    before = build_v6_live_route_targets(root)
    result = activate_v6_live_routes(root)
    after = build_v6_live_route_targets(root)

    assert result["ok"] is True
    assert result["writes_files"] is False
    assert result["activates_probability_epoch"] is False
    assert before == after
    assert audit_v6_live_routes(root)["ok"] is True


def test_resident_route_replaces_monitor_fanout_and_forbids_fourth_service(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    targets = build_v6_live_route_targets(root)
    text = "\n".join(value.decode("utf-8") for value in targets.values())
    audit = audit_v6_live_routes(root)

    assert "TargetProductionWorker" in text
    assert "compile_production_cycle_from_ssot" in text
    assert "IdentifiablePhysicalProbabilityWorker" not in text
    assert "physical_probability_worker.run" not in text
    assert "watch_zuuu_speci_once.py" not in text
    assert "polymarket_monitor_daemon.sh" not in text
    assert audit["no_fourth_service_definition"] is True
    assert audit["resident_service"] == "com.weather.reported_trajectory_no"


def test_target_production_worker_uses_an_enforced_read_only_connection(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE marker(value TEXT)")
        conn.commit()

    worker = TargetProductionWorker(db)
    try:
        assert worker.conn is not None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            worker.conn.execute("CREATE TABLE forbidden(value TEXT)")
        result = worker.run()
        assert result["ok"] is False
        assert result["places_orders"] is False
        assert result["calls_order_gateway"] is False
    finally:
        worker.close()


def test_target_worker_defaults_are_isolated_from_live_activation_and_database() -> None:
    assert DEFAULT_TARGET_DB.name == "bayes_basket_target_ssot.db"
    assert DEFAULT_TARGET_DB != SHARED_LIVE_DB
    assert DEFAULT_TARGET_ACTIVATION.name == (
        "bayes_basket_target_production_no_live_activation_v1.json"
    )
    with pytest.raises(ValueError, match="must_not_be_shared_live"):
        TargetProductionWorker(SHARED_LIVE_DB)
