from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.database import SHARED_LIVE_DB
from bayes_basket_target.market_registry import apply_target_market_registry_schema
from bayes_basket_target.market_registry import load_market_registry_snapshot
from scripts.export_bayes_basket_target_local_registry_scope import export_scope


def test_cli_entrypoint_loads_package_from_project_root() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/export_bayes_basket_target_local_registry_scope.py"),
            "--help",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "--event-date" in result.stdout


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot: str,
    event_date: str,
    station_id: str,
    ingested_at: str,
) -> None:
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot "
        "(snapshot_sha256,source_schema,source_scope,exported_at_utc,"
        "source_database_path,row_count,ingested_at_utc,content_sha256) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            snapshot,
            "reported_trajectory_market_registry_v1",
            "shared_live_registry_export_only",
            ingested_at,
            "/isolated/source.db",
            1,
            ingested_at,
            snapshot,
        ),
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot,
            f"{station_id.lower()}-{event_date}",
            event_date,
            "30°C",
            station_id.lower(),
            station_id,
            "America/New_York",
            "C",
            "awc",
            1,
            "",
            "https://example.invalid/rules",
            "c" * 64,
            f"yes-{station_id}-{event_date}",
            f"no-{station_id}-{event_date}",
            1,
            1,
            json.dumps({"market_id": f"{station_id}-{event_date}"}),
            ingested_at,
        ),
    )


def test_export_scope_uses_latest_snapshot_that_owns_requested_date(
    tmp_path: Path,
) -> None:
    target_db = tmp_path / "target.db"
    output = tmp_path / "scope.json"
    with sqlite3.connect(target_db) as conn:
        apply_target_market_registry_schema(conn)
        _insert_snapshot(
            conn,
            snapshot="a" * 64,
            event_date="2026-07-26",
            station_id="OLD",
            ingested_at="2026-07-26T12:00:00Z",
        )
        _insert_snapshot(
            conn,
            snapshot="b" * 64,
            event_date="2026-07-27",
            station_id="NEW",
            ingested_at="2026-07-26T13:00:00Z",
        )

    payload = export_scope(
        db_path=target_db,
        event_date="2026-07-26",
        output=output,
    )

    assert payload["source_snapshot_sha256"] == "a" * 64
    assert payload["row_count"] == 1
    assert {row["event_date"] for row in payload["rows"]} == {"2026-07-26"}
    assert {row["station_id"] for row in payload["rows"]} == {"OLD"}
    assert Path(payload["source_database_path"]).resolve() == target_db.resolve()
    assert Path(payload["source_database_path"]).resolve() != SHARED_LIVE_DB
    assert payload["boundary"] == {
        "target_only": True,
        "reads_target_database": True,
        "writes_target_database": False,
        "reads_shared_live_database": False,
        "writes_shared_live_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
    }
    stored = json.loads(output.read_text(encoding="utf-8"))
    supplied_hash = stored.pop("snapshot_sha256")
    assert supplied_hash == content_sha256(stored)
    assert supplied_hash == payload["snapshot_sha256"]
    assert load_market_registry_snapshot(output)["snapshot_sha256"] == supplied_hash


def test_export_scope_rejects_shared_live_database(tmp_path: Path) -> None:
    output = tmp_path / "scope.json"

    try:
        export_scope(
            db_path=SHARED_LIVE_DB,
            event_date="2026-07-26",
            output=output,
        )
    except ValueError as exc:
        assert str(exc) == "target_database_must_not_be_shared_live_database"
    else:
        raise AssertionError("shared live database must be rejected")
    assert not output.exists()
