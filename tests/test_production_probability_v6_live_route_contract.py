from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from scripts.weather_basket_yes_daemon import TargetProductionWorker
from bayes_basket_target.production import PRODUCTION_EXECUTION_MODE
from bayes_basket_target.database import SHARED_LIVE_DB
from scripts.weather_basket_yes_daemon import (
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
        ROOT / "scripts/install_weather_basket_yes_user_services.sh",
        tmp_path / "scripts/install_weather_basket_yes_user_services.sh",
    )
    return tmp_path


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
    # The activation follows the mode -- one constant decides both, so the
    # worker cannot run one and validate the other.  It became the live
    # contract at the 2026-08-30 go-live migration; what this test is really
    # about, and what has not changed, is the line below: the target lane
    # never borrows the shared live database.
    assert DEFAULT_TARGET_ACTIVATION.name == (
        "bayes_basket_target_production_live_activation_v1.json"
        if PRODUCTION_EXECUTION_MODE == "production_live"
        else "bayes_basket_target_production_no_live_activation_v1.json"
    )
    with pytest.raises(ValueError, match="must_not_be_shared_live"):
        TargetProductionWorker(SHARED_LIVE_DB)
