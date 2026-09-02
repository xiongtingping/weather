from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
    export_shared_live_registry,
    ingest_target_market_registry,
    load_active_registry_rows,
    load_started_registry_rows,
)


def _source_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE reported_trajectory_market_registry(
                slug TEXT,event_date TEXT,outcome TEXT,city_slug TEXT,station_id TEXT,
                settlement_timezone TEXT,settlement_unit TEXT,source_lane TEXT,
                source_eligible INTEGER,source_block_reason TEXT,resolution_url TEXT,
                rules_sha256 TEXT,yes_token TEXT,no_token TEXT,market_active INTEGER,
                market_accepting_orders INTEGER,registry_json TEXT,refreshed_at TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO reported_trajectory_market_registry VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "highest-temperature-in-test-on-july-23-2026", "2026-07-23", "38°C", "test",
                "TEST", "UTC", "celsius", "amsc", 1, "", "https://example.invalid/rules",
                "a" * 64, "yes-38", "no-38", 1, 1,
                json.dumps({"market_id": "m-38", "lower": 38.0, "upper": 38.0}),
                "2026-07-23T00:00:00Z",
            ),
        )


def test_export_ingest_and_runtime_are_target_local(tmp_path: Path) -> None:
    source = tmp_path / "archive.db"
    snapshot = tmp_path / "registry.json"
    target = tmp_path / "target.db"
    _source_db(source)

    exported = export_shared_live_registry(source_db=source, output_path=snapshot)
    assert exported["row_count"] == 1
    assert exported["boundary"]["writes_shared_live_database"] is False

    with sqlite3.connect(target) as conn:
        apply_target_market_registry_schema(conn)
        checked = ingest_target_market_registry(conn, snapshot_path=snapshot)
        assert checked["state"] == "VALIDATED_NO_WRITE"
        assert conn.execute("SELECT COUNT(*) FROM basket_target_market_registry_snapshot").fetchone()[0] == 0
        committed = ingest_target_market_registry(conn, snapshot_path=snapshot, commit=True)
        assert committed["state"] == "COMMITTED"
        rows = load_active_registry_rows(conn, station_id="TEST", event_date="2026-07-23")
        assert rows[0]["yes_token"] == "yes-38"
        assert rows[0]["settlement_unit"].lower() == "celsius"
        assert rows[0]["source_lane"] == "amsc"
        assert json.loads(rows[0]["registry_json"])["market_id"] == "m-38"
        replay = ingest_target_market_registry(conn, snapshot_path=snapshot, commit=True)
        assert replay["state"] == "ALREADY_COMMITTED"
        assert conn.execute("SELECT COUNT(*) FROM basket_target_market_registry").fetchone()[0] == 1


def test_unchanged_registry_export_preserves_file_mtime(tmp_path: Path) -> None:
    source = tmp_path / "archive.db"
    snapshot = tmp_path / "registry.json"
    _source_db(source)
    first = export_shared_live_registry(
        source_db=source,
        output_path=snapshot,
        as_of_utc="2026-07-23T12:00:00Z",
        include_not_started_for_p0=True,
    )
    mtime = snapshot.stat().st_mtime_ns
    second = export_shared_live_registry(
        source_db=source,
        output_path=snapshot,
        as_of_utc="2026-07-23T12:10:00Z",
        include_not_started_for_p0=True,
    )

    assert first["state"] == "EXPORTED"
    assert second["state"] == "UNCHANGED_NO_WRITE"
    assert second["snapshot_sha256"] == first["snapshot_sha256"]
    assert snapshot.stat().st_mtime_ns == mtime


def test_file_backed_connection_cannot_fallback_to_shared_table(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE reported_trajectory_market_registry(station_id TEXT,event_date TEXT,outcome TEXT,source_eligible INTEGER,market_active INTEGER,market_accepting_orders INTEGER)"
        )
        conn.execute("INSERT INTO reported_trajectory_market_registry VALUES('TEST','2026-07-23','38°C',1,1,1)")
        with pytest.raises(ContractError, match="snapshot_missing"):
            load_active_registry_rows(conn)


def test_snapshot_tamper_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "archive.db"
    snapshot = tmp_path / "registry.json"
    _source_db(source)
    export_shared_live_registry(source_db=source, output_path=snapshot)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["rows"][0]["yes_token"] = "tampered"
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    with sqlite3.connect(":memory:") as conn:
        apply_target_market_registry_schema(conn)
        with pytest.raises(ContractError, match="content_sha_mismatch"):
            ingest_target_market_registry(conn, snapshot_path=snapshot)


def test_started_registry_view_excludes_future_et_day(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        apply_target_market_registry_schema(conn)
        snapshot = "b" * 64
        conn.execute(
            "INSERT INTO basket_target_market_registry_snapshot "
            "(snapshot_sha256,source_schema,source_scope,exported_at_utc,source_database_path,"
            "row_count,ingested_at_utc,content_sha256) VALUES(?,?,?,?,?,?,?,?)",
            (
                snapshot,
                "reported_trajectory_market_registry_v1",
                "shared_live_registry_export_only",
                "2026-07-24T15:00:00Z",
                "/isolated/source.db",
                2,
                "2026-07-24T15:00:00Z",
                snapshot,
            ),
        )
        rows = [
            (
                snapshot,
                "current",
                "2026-07-24",
                "38°C",
                "current",
                "TEST",
                "America/New_York",
                "C",
                "amsc",
                1,
                "",
                "",
                "a" * 64,
                "yes-current",
                "no-current",
                1,
                1,
                '{"market_id":"current"}',
                "2026-07-24T15:00:00Z",
            ),
            (
                snapshot,
                "future",
                "2026-07-25",
                "38°C",
                "future",
                "TEST",
                "America/New_York",
                "C",
                "amsc",
                1,
                "",
                "",
                "c" * 64,
                "yes-future",
                "no-future",
                1,
                1,
                '{"market_id":"future"}',
                "2026-07-24T15:00:00Z",
            ),
        ]
        conn.executemany(
            "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        started = load_started_registry_rows(
            conn,
            as_of_utc="2026-07-24T15:00:00Z",
        )
    assert [(row["station_id"], row["event_date"]) for row in started] == [("TEST", "2026-07-24")]


def test_scoped_registry_read_uses_latest_snapshot_that_owns_requested_date(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        apply_target_market_registry_schema(conn)
        older = "a" * 64
        newer = "b" * 64
        for snapshot, exported_at, ingested_at in (
            (older, "2026-07-24T15:00:00Z", "2026-07-24T15:00:00Z"),
            (newer, "2026-07-25T15:00:00Z", "2026-07-25T15:00:00Z"),
        ):
            conn.execute(
                "INSERT INTO basket_target_market_registry_snapshot "
                "(snapshot_sha256,source_schema,source_scope,exported_at_utc,"
                "source_database_path,row_count,ingested_at_utc,content_sha256) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    snapshot,
                    "reported_trajectory_market_registry_v1",
                    "shared_live_registry_export_only",
                    exported_at,
                    "/isolated/source.db",
                    1,
                    ingested_at,
                    snapshot,
                ),
            )
        rows = [
            (
                older, "day-24", "2026-07-24", "38°C", "test", "TEST",
                "America/New_York", "C", "amsc", 1, "", "", "c" * 64,
                "yes-24", "no-24", 1, 1,
                '{"market_id":"day-24","lower":38.0,"upper":38.0}',
                "2026-07-24T15:00:00Z",
            ),
            (
                newer, "day-25", "2026-07-25", "39°C", "test", "TEST",
                "America/New_York", "C", "amsc", 1, "", "", "d" * 64,
                "yes-25", "no-25", 1, 1,
                '{"market_id":"day-25","lower":39.0,"upper":39.0}',
                "2026-07-25T15:00:00Z",
            ),
        ]
        conn.executemany(
            "INSERT INTO basket_target_market_registry VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        selected = load_active_registry_rows(
            conn,
            station_id="TEST",
            event_date="2026-07-24",
        )
    assert [row["yes_token"] for row in selected] == ["yes-24"]


def test_export_filters_prelisted_future_market_before_target_ssot(tmp_path: Path) -> None:
    source = tmp_path / "archive.db"
    snapshot = tmp_path / "registry.json"
    _source_db(source)
    with sqlite3.connect(source) as conn:
        conn.execute(
            "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "highest-temperature-in-test-on-july-25-2026", "2026-07-25", "38°C", "test",
                "TEST", "America/New_York", "celsius", "amsc", 1, "", "https://example.invalid/rules",
                "b" * 64, "yes-future", "no-future", 1, 1,
                json.dumps({"market_id": "m-future", "lower": 38.0, "upper": 38.0}),
                "2026-07-24T15:00:00Z",
            ),
        )
        conn.commit()

    result = export_shared_live_registry(
        source_db=source,
        output_path=snapshot,
        as_of_utc="2026-07-24T15:00:00Z",
    )
    assert result["row_count"] == 1
    assert result["source_row_count"] == 2
    assert result["excluded_future_row_count"] == 1
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert [row["event_date"] for row in payload["rows"]] == ["2026-07-23"]


def test_forward_p0_export_ingests_catalog_but_started_view_stays_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "archive.db"
    snapshot = tmp_path / "registry.json"
    _source_db(source)
    with sqlite3.connect(source) as conn:
        conn.execute(
            "UPDATE reported_trajectory_market_registry SET event_date='2026-07-25'"
        )
        conn.commit()
    result = export_shared_live_registry(
        source_db=source,
        output_path=snapshot,
        event_date="2026-07-25",
        as_of_utc="2026-07-24T15:00:00Z",
        include_not_started_for_p0=True,
    )
    assert result["row_count"] == 1
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["filters"]["include_not_started_for_p0"] is True
    assert payload["forward_p0_catalog_only"] is True
    with sqlite3.connect(tmp_path / "target.db") as conn:
        apply_target_market_registry_schema(conn)
        committed = ingest_target_market_registry(
            conn,
            snapshot_path=snapshot,
            commit=True,
            as_of_utc="2026-07-24T15:00:00Z",
        )
        assert committed["state"] == "COMMITTED"
        assert len(load_active_registry_rows(conn, event_date="2026-07-25")) == 1
        assert load_started_registry_rows(
            conn,
            event_date="2026-07-25",
            as_of_utc="2026-07-24T15:00:00Z",
        ) == []


def test_current_catalog_keeps_two_listed_days_and_drops_the_third(
    tmp_path: Path,
) -> None:
    """Charter 48(e): markets list at D-2, so the catalog carries D+1 and
    D+2 for every station; the one-listed-day filter dropped the day just
    listed (528 rows, 48 stations, 2026-09-02) before any P0 could see it."""
    source = tmp_path / "archive.db"
    snapshot = tmp_path / "registry.json"
    _source_db(source)
    with sqlite3.connect(source) as conn:
        original = conn.execute(
            "SELECT * FROM reported_trajectory_market_registry"
        ).fetchone()
        assert original is not None
        for event_date, token_suffix in (
            ("2026-07-24", "open"),
            ("2026-07-25", "future-1"),
            ("2026-07-26", "future-2"),
            ("2026-07-27", "future-3"),
        ):
            row = list(original)
            row[0] = f"highest-temperature-in-test-on-{event_date}"
            row[1] = event_date
            row[12] = f"yes-{token_suffix}"
            row[13] = f"no-{token_suffix}"
            conn.execute(
                "INSERT INTO reported_trajectory_market_registry "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
        conn.commit()

    result = export_shared_live_registry(
        source_db=source,
        output_path=snapshot,
        as_of_utc="2026-07-24T12:00:00Z",
        include_not_started_for_p0=True,
        current_catalog_only=True,
    )

    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert [row["event_date"] for row in payload["rows"]] == [
        "2026-07-24",
        "2026-07-25",
        "2026-07-26",
    ]
    assert result["source_row_count"] == 5
    assert result["excluded_closed_row_count"] == 1
    assert result["excluded_additional_future_scope_row_count"] == 1
    assert payload["filters"]["current_catalog_only"] is True


def test_ingest_rejects_future_row_at_target_ssot_boundary(tmp_path: Path) -> None:
    source = tmp_path / "archive.db"
    snapshot = tmp_path / "registry.json"
    _source_db(source)
    export_shared_live_registry(source_db=source, output_path=snapshot)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    future = dict(payload["rows"][0])
    future["event_date"] = "2026-07-25"
    payload["rows"] = [future]
    payload["row_count"] = 1
    payload["source_row_count"] = 1
    payload["filters"]["as_of_utc"] = "2026-07-24T15:00:00Z"
    payload["snapshot_sha256"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in payload.items() if k != "snapshot_sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    with sqlite3.connect(tmp_path / "target.db") as conn:
        apply_target_market_registry_schema(conn)
        with pytest.raises(ContractError, match="future_event_day_not_allowed"):
            ingest_target_market_registry(
                conn,
                snapshot_path=snapshot,
                as_of_utc="2026-07-24T15:00:00Z",
            )
