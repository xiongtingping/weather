import sqlite3
from pathlib import Path

from scripts import reported_trajectory_no_daemon as daemon


def test_resident_forecast_prefetch_has_no_second_target_writer() -> None:
    source = Path(daemon.__file__).read_text(encoding="utf-8")

    assert "forecast_commit_conn" not in source
    assert source.count("open_target_writer_if_enabled(") == 2
    forecast_worker = source.split(
        "forecast_worker = TargetProductionWorker(", 1
    )[1].split("\n        )", 1)[0]
    assert "commit_conn=None" in forecast_worker
    assert "station_allowlist=" not in forecast_worker
    assert "RKPK" not in forecast_worker


def test_forecast_cycle_keeps_optional_taf_capture_inside_existing_service(
    tmp_path,
) -> None:
    import sqlite3
    from pathlib import Path

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
        "state TEXT,stage TEXT,decision_at_utc TEXT,fresh_until_utc TEXT)"
    )

    class Worker:
        commit_conn = conn
        taf_artifact_path = (
            Path(__file__).resolve().parents[1]
            / "config/artifacts/bayes_basket_target_taf_revision_active.json"
        )
        taf_inbox_path = tmp_path / "taf-inbox.json"

        def run(self):
            return {
                "ok": True,
                "calls_order_gateway": False,
                "places_orders": False,
            }

    worker = Worker()
    first = daemon.run_target_forecast_refresh_cycle(worker)
    second = daemon.run_target_forecast_refresh_cycle(worker)
    assert first["forward_taf_capture"]["state"] == "no_active_p0m_scope"
    assert second["forward_taf_capture"]["state"] == "not_due"
    assert first["calls_order_gateway"] is False


def test_forecast_cycle_uses_read_only_target_connection_for_taf_scope(
    tmp_path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
        "state TEXT,stage TEXT,decision_at_utc TEXT,fresh_until_utc TEXT)"
    )

    class Worker:
        commit_conn = None
        taf_artifact_path = (
            Path(__file__).resolve().parents[1]
            / "config/artifacts/bayes_basket_target_taf_revision_active.json"
        )
        taf_inbox_path = tmp_path / "taf-inbox.json"

        def __init__(self) -> None:
            self.conn = conn

        def refresh_resident_nwp(self, *, as_of_utc: str):
            return {
                "state": "unchanged_zero_write",
                "as_of_utc": as_of_utc,
                "calls_order_gateway": False,
                "places_orders": False,
            }

    result = daemon.run_target_forecast_refresh_cycle(Worker())

    assert result["forward_taf_capture"]["state"] == "no_active_p0m_scope"
    assert result["calls_order_gateway"] is False
    assert result["places_orders"] is False


def test_forecast_taf_scope_failure_does_not_block_later_date(
    tmp_path,
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
        "state TEXT,stage TEXT,decision_at_utc TEXT,fresh_until_utc TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_probability_envelope VALUES("
        "'RKPK',?,'Asia/Seoul','COMMITTED','P0',"
        "'2026-07-29T00:00:00Z','2099-01-01T00:00:00Z')",
        [("2026-07-29",), ("2026-07-30",)],
    )
    attempted: list[str] = []

    def capture(**kwargs):
        event_date = str(kwargs["event_date"])
        attempted.append(event_date)
        if event_date == "2026-07-29":
            raise RuntimeError("tx_missing")
        return {
            "station_id": "RKPK",
            "event_date": event_date,
            "forecast_revision": {
                "source_object_sha256": "a" * 64,
            },
        }

    monkeypatch.setattr(daemon, "capture_forward_awc_taf", capture)
    monkeypatch.setattr(
        daemon,
        "refresh_forward_taf_inbox",
        lambda **_kwargs: {"content_sha256": "b" * 64},
    )

    class Worker:
        commit_conn = None
        skip_nwp_refresh = True
        taf_artifact_path = (
            Path(__file__).resolve().parents[1]
            / "config/artifacts/bayes_basket_target_taf_revision_active.json"
        )
        taf_inbox_path = tmp_path / "taf-inbox.json"

        def __init__(self, read_conn) -> None:
            self.conn = read_conn

    result = daemon.run_target_forecast_refresh_cycle(Worker(conn))
    taf = result["forward_taf_capture"]

    assert attempted == ["2026-07-29", "2026-07-30"]
    assert taf["state"] == "captured_with_scope_failures"
    assert taf["capture_count"] == 1
    assert taf["failure_count"] == 1
    assert taf["failures"][0]["event_date"] == "2026-07-29"


def test_only_explicit_sqlite_disk_io_is_recoverable() -> None:
    assert daemon.is_recoverable_sqlite_io_error(sqlite3.OperationalError("disk I/O error"))
    assert not daemon.is_recoverable_sqlite_io_error(sqlite3.OperationalError("database is locked"))
    assert not daemon.is_recoverable_sqlite_io_error(RuntimeError("disk I/O error"))


def test_serialized_target_io_recovery_ignores_historical_decisions() -> None:
    assert daemon.target_result_has_recoverable_sqlite_io_error({
        "reason": "OperationalError:disk I/O error",
    })
    assert daemon.target_result_has_recoverable_sqlite_io_error({
        "market_ingress": {"reason": "OperationalError:disk I/O error"},
    })
    assert daemon.target_result_has_recoverable_sqlite_io_error({
        "resident_nwp_capture": {
            "reason": "OperationalError:disk I/O error",
        },
    }, forecast=True)
    assert not daemon.target_result_has_recoverable_sqlite_io_error({
        "reason": "cadence_gate_blocked",
        "durable_blocked_decisions": [{
            "reason": "OperationalError:disk I/O error",
        }],
    })
    assert not daemon.target_result_has_recoverable_sqlite_io_error({
        "resident_nwp_capture": {
            "reason": "ensemble_artifact_weight_method_invalid",
        },
        "forward_taf_capture": {"state": "not_due"},
    }, forecast=True)


def test_archive_storage_guard_fails_closed_on_wal_or_free_space(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "archive.db"
    db.write_bytes(b"")
    Path(str(db) + "-wal").write_bytes(b"x" * 17)
    monkeypatch.setattr(daemon, "MAX_ARCHIVE_WAL_BYTES", 16)
    monkeypatch.setattr(
        daemon.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 1024})(),
    )
    monkeypatch.setattr(daemon, "MIN_ARCHIVE_FREE_BYTES", 2048)
    result = daemon.archive_storage_guard(db)
    assert result["ok"] is False
    assert result["violations"] == [
        "archive_wal_bytes_exceeded",
        "archive_free_bytes_below_minimum",
    ]


def test_forecast_cycle_refreshes_nwp_once_without_running_consumer() -> None:
    class Worker:
        def __init__(self) -> None:
            self.refresh_calls = 0
            self.run_calls = 0
            self.call_order = []
            self.commit_conn = None
            self.taf_artifact_path = Path("/nonexistent")
            self.taf_inbox_path = Path("/nonexistent")

        def refresh_resident_nwp(self, *, as_of_utc: str):
            self.refresh_calls += 1
            self.call_order.append("refresh")
            return {
                "state": "unchanged_zero_write",
                "as_of_utc": as_of_utc,
                "calls_order_gateway": False,
                "places_orders": False,
            }

        def run(self):
            self.run_calls += 1
            self.call_order.append("run")
            return {
                "ok": True,
                "calls_order_gateway": False,
                "places_orders": False,
            }

    worker = Worker()
    first = daemon.run_target_forecast_refresh_cycle(worker)
    second = daemon.run_target_forecast_refresh_cycle(worker)

    assert worker.refresh_calls == 1
    assert worker.run_calls == 0
    assert worker.call_order == ["refresh"]
    assert first["resident_nwp_capture"]["state"] == "unchanged_zero_write"
    assert second["resident_nwp_capture"]["state"] == "not_due"
    assert "resident_p0_ingress" not in first


def test_forecast_cycle_can_reuse_just_materialized_exact_scope_probability() -> None:
    class Worker:
        skip_nwp_refresh = True
        commit_conn = None
        taf_artifact_path = Path("/nonexistent")
        taf_inbox_path = Path("/nonexistent")

        def refresh_resident_nwp(self, *, as_of_utc: str):
            raise AssertionError("network NWP refresh must be skipped")

        def run(self):
            return {
                "ok": True,
                "calls_order_gateway": False,
                "places_orders": False,
            }

    result = daemon.run_target_forecast_refresh_cycle(Worker())
    assert result["resident_nwp_capture"] == {
        "state": "skipped_exact_scope_reuse",
        "reason": "reuse_materialized_probability_requested",
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def test_target_production_cycle_only_consumes_durable_evidence() -> None:
    class Worker:
        def __init__(self) -> None:
            self.run_calls = 0

        def refresh_resident_nwp(self, *, as_of_utc: str):
            raise AssertionError("production consumer must never perform network refresh")

        def run(self):
            self.run_calls += 1
            return {
                "ok": True,
                "state": "durable_evidence_consumed",
                "calls_order_gateway": False,
                "places_orders": False,
            }

    worker = Worker()
    result = daemon.run_target_production_cycle(worker)

    assert worker.run_calls == 1
    assert result == {
        "ok": True,
        "state": "durable_evidence_consumed",
        "calls_order_gateway": False,
        "places_orders": False,
    }


def test_lane_worker_reopen_replaces_connection_and_engine(monkeypatch, tmp_path: Path) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    old_connection = FakeConnection()
    new_connection = FakeConnection()
    new_engine = object()
    monkeypatch.setattr(daemon, "_open_runtime", lambda _path: new_connection)
    monkeypatch.setattr(daemon, "ReportedTrajectoryNoEngine", lambda _conn, *, config: new_engine)

    worker = object.__new__(daemon.LaneWorker)
    worker.db_path = tmp_path / "archive.db"
    worker.conn = old_connection
    worker.config = {}
    worker.engine = object()
    worker.stations = ["ZUUU"]
    worker.next_station_refresh = 123.0

    worker.reopen()

    assert old_connection.closed is True
    assert worker.conn is new_connection
    assert worker.engine is new_engine
    assert worker.stations == []
    assert worker.next_station_refresh == 0.0
