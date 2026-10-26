import sqlite3
import tempfile
from pathlib import Path

from bayes_basket_target.nwp_rotation_state import build_rotation_state
from bayes_basket_target.station_registry import coordinate_registry_manifest
from scripts import weather_basket_yes_daemon as daemon


def _create_active_registry_fixture(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE reported_trajectory_market_registry("
        "slug TEXT,event_date TEXT,station_id TEXT,"
        "settlement_timezone TEXT,settlement_unit TEXT,outcome TEXT,"
        "yes_token TEXT,registry_json TEXT,source_lane TEXT,"
        "source_eligible INTEGER,market_active INTEGER,"
        "market_accepting_orders INTEGER)"
    )


def _insert_registry_scope(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
) -> None:
    conn.execute(
        "INSERT INTO reported_trajectory_market_registry VALUES("
        "?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"{station_id.lower()}-{event_date}",
            event_date,
            station_id,
            settlement_timezone,
            "celsius",
            "30",
            f"yes-{station_id}-{event_date}",
            "{}",
            "awc",
            1,
            1,
            1,
        ),
    )


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
    tmp_path, monkeypatch,
) -> None:
    import sqlite3
    from pathlib import Path

    # The poll gate is stamped from the clock read at *entry*, so a cycle that
    # itself takes longer than the interval is due again the moment it
    # returns.  With the shipped 60s that made the "second cycle stays
    # not-due" assertion a claim about how fast the machine is: measured
    # 2026-09-09 under load 22, the first call took 134 seconds and the
    # second was legitimately due.  Widening the window keeps the assertion
    # on the gate, which is what it was written to check.
    monkeypatch.setattr(daemon, "TARGET_TAF_POLL_INTERVAL_SEC", 3600.0)

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
        "state TEXT,stage TEXT,decision_at_utc TEXT,fresh_until_utc TEXT)"
    )
    _create_active_registry_fixture(conn)

    class Worker:
        commit_conn = conn
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
    assert (
        first["forward_taf_capture"]["state"]
        == "no_active_registry_scope"
    )
    assert first["forward_taf_capture"]["scope_source"] == (
        "target_local_active_market_registry"
    )
    assert second["forward_taf_capture"]["state"] == "not_due"
    assert first["forward_taf_capture"]["last_attempt"]["state"] == (
        "no_active_registry_scope"
    )
    assert first["forward_taf_capture"]["last_attempt"]["capture_count"] == 0
    assert first["calls_order_gateway"] is False

    # Repeated heartbeat reads must retain a flat, bounded receipt rather than
    # recursively embedding the prior aggregate state in ``last_attempt``.
    third = daemon.run_target_forecast_refresh_cycle(worker)
    attempt = third["forward_taf_capture"]["last_attempt"]
    assert "last_attempt" not in attempt


def test_forecast_cycle_uses_read_only_target_connection_for_taf_scope(
    tmp_path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
        "state TEXT,stage TEXT,decision_at_utc TEXT,fresh_until_utc TEXT)"
    )
    _create_active_registry_fixture(conn)

    class Worker:
        commit_conn = None
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

    assert (
        result["forward_taf_capture"]["state"]
        == "no_active_registry_scope"
    )
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
    _create_active_registry_fixture(conn)
    _insert_registry_scope(
        conn,
        station_id="RKPK",
        event_date="2026-07-31",
        settlement_timezone="Asia/Seoul",
    )
    _insert_registry_scope(
        conn,
        station_id="RKSI",
        event_date="2026-08-01",
        settlement_timezone="Asia/Seoul",
    )
    attempted: list[str] = []

    def capture(**kwargs):
        event_date = str(kwargs["event_date"])
        attempted.append(event_date)
        if event_date == "2026-08-01":
            raise RuntimeError("tx_missing")
        return {
            "station_id": str(kwargs["station_id"]),
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
    monkeypatch.setattr(
        daemon,
        "utc_now",
        lambda: "2026-07-31T00:30:00Z",
    )

    class Worker:
        commit_conn = None
        skip_nwp_refresh = True
        taf_inbox_path = tmp_path / "taf-inbox.json"

        def __init__(self, read_conn) -> None:
            self.conn = read_conn

    result = daemon.run_target_forecast_refresh_cycle(Worker(conn))
    taf = result["forward_taf_capture"]

    assert attempted == ["2026-08-01", "2026-07-31"]
    assert taf["state"] == "captured_with_scope_failures"
    assert taf["catalog_scope_count"] == 2
    assert taf["selected_scope_count"] == 2
    assert taf["capture_count"] == 1
    assert taf["failure_count"] == 1
    assert taf["failures"][0]["event_date"] == "2026-08-01"
    assert taf["last_attempt"]["state"] == "captured_with_scope_failures"
    assert taf["last_attempt"]["capture_count"] == 1


def test_only_explicit_sqlite_disk_io_is_recoverable() -> None:
    assert daemon.is_recoverable_sqlite_io_error(sqlite3.OperationalError("disk I/O error"))
    assert not daemon.is_recoverable_sqlite_io_error(sqlite3.OperationalError("database is locked"))
    assert not daemon.is_recoverable_sqlite_io_error(RuntimeError("disk I/O error"))


def test_target_sqlite_lock_classification_ignores_historical_receipts() -> None:
    assert daemon.is_target_sqlite_lock_error(
        sqlite3.OperationalError("database is locked")
    )
    assert daemon.is_target_sqlite_lock_error(
        sqlite3.OperationalError("database table is locked: x")
    )
    assert not daemon.is_target_sqlite_lock_error(
        sqlite3.OperationalError("disk I/O error")
    )
    assert daemon.target_result_has_sqlite_lock_contention({
        "market_ingress": {"reason": "OperationalError:database is locked"},
    })
    assert not daemon.target_result_has_sqlite_lock_contention({
        "durable_blocked_decisions": [{
            "reason": "OperationalError:database is locked",
        }],
    })


def test_target_worker_lock_budget_is_connection_local_and_has_no_schema_write() -> None:
    conn = sqlite3.connect(":memory:")
    worker = type("Worker", (), {"conn": conn, "commit_conn": conn})()

    receipt = daemon.configure_target_worker_sqlite_lock_budget(worker)

    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == (
        daemon.TARGET_SQLITE_LOCK_TIMEOUT_MS
    )
    assert receipt == {
        "busy_timeout_ms": daemon.TARGET_SQLITE_LOCK_TIMEOUT_MS,
        "configured_connections": ["conn"],
        "schema_mutation": False,
    }
    conn.close()


def test_serialized_target_io_recovery_ignores_historical_decisions() -> None:
    assert daemon.target_result_has_recoverable_sqlite_io_error({
        "reason": "OperationalError:disk I/O error",
    })
    assert daemon.target_result_has_recoverable_sqlite_io_error({
        "market_ingress": {"reason": "OperationalError:disk I/O error"},
    })
    assert daemon.target_result_has_recoverable_sqlite_io_error({
        "reason": (
            "target_writer_open_failed:ResourceBoundError:"
            "target_database_wal_checkpoint_failed"
        ),
    })
    assert daemon.target_result_has_recoverable_sqlite_io_error({
        "reason": (
            "target_writer_reopen_failed:ResourceBoundError:"
            "target_database_wal_checkpoint_busy"
        ),
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


def test_forecast_cycle_advances_rotation_without_running_consumer() -> None:
    class Worker:
        def __init__(self) -> None:
            self.refresh_calls = 0
            self.run_calls = 0
            self.call_order = []
            self.commit_conn = None
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

    # One call advances the rotation by one scope; a poll advances several so
    # the sweep finishes inside the P0 freshness TTL.
    assert worker.refresh_calls == daemon.TARGET_NWP_SCOPES_PER_POLL
    assert worker.call_order == ["refresh"] * daemon.TARGET_NWP_SCOPES_PER_POLL
    # The invariants this test exists for: refreshing never runs the consumer,
    # and a second cycle inside the same poll window stays not-due.
    assert worker.run_calls == 0
    assert first["resident_nwp_capture"]["state"] == "unchanged_zero_write"
    assert first["resident_nwp_capture"]["scopes_advanced_this_poll"] == (
        daemon.TARGET_NWP_SCOPES_PER_POLL
    )
    assert second["resident_nwp_capture"]["state"] == "not_due"
    assert "resident_p0_ingress" not in first


def test_forecast_cycle_can_reuse_just_materialized_exact_scope_probability() -> None:
    class Worker:
        skip_nwp_refresh = True
        commit_conn = None
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


def test_resident_nwp_in_flight_receipt_reuses_validated_canonical_rotation() -> None:
    stations = tuple(
        coordinate_registry_manifest()["station_ids"]
    )
    # ``lead_days`` is part of the scope key (a station-day is captured once
    # per lead, from a different run each time).  It has been required since
    # 2026-08-09; this fixture predates that and was building scopes the
    # canonical key refuses.
    scopes = [
        {
            "station_ids": [station],
            "event_date": "2026-07-30",
            "settlement_timezone": "UTC",
            "lead_days": 1,
        }
        for station in stations
    ]
    rotation = build_rotation_state(
        station_ids=stations,
        scopes=scopes,
        selected_scopes=scopes[:4],
        cursor_by_priority={0: 4},
        outcomes=[
            {
                "scope_key": (
                    f"{row['station_ids'][0]}|2026-07-30|UTC|d1"
                ),
                "state": (
                    "CAPTURED_CANDIDATE"
                    if index == 0
                    else "BLOCKED_FAIL_CLOSED"
                ),
                "candidate_only": index == 0,
            }
            for index, row in enumerate(scopes[:4])
        ],
        previous=None,
        updated_at_utc="2026-07-30T07:00:00Z",
    )
    worker = type("Worker", (), {
        "station_allowlist": stations,
        "_nwp_rotation_state": rotation,
    })()

    receipt = daemon.resident_nwp_in_flight_receipt(
        worker,
        scheduled_at_utc="2026-07-30T07:01:00Z",
    )

    assert receipt["state"] == "in_flight_previous_rotation"
    assert receipt["completion_claimed"] is False
    assert receipt["configured_station_ids"] == list(stations)
    assert receipt["discovered_station_ids"] == list(stations)
    assert receipt["scope_count"] == len(stations)
    assert receipt["selected_scope_count"] == 4
    assert receipt["candidate_prefetch_attempt_count"] == 1
    assert receipt["calls_order_gateway"] is False


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


def test_target_production_cycle_classifies_direct_sqlite_lock_fail_closed() -> None:
    class Worker:
        commit_conn = sqlite3.connect(":memory:")

        def run(self):
            raise sqlite3.OperationalError("database is locked")

    worker = Worker()
    result = daemon.run_target_production_cycle(worker)

    assert result["state"] == "blocked_fail_closed"
    assert result["reason"] == (
        "sqlite_lock_contention:OperationalError:database is locked"
    )
    assert result["target_cycle_runtime"]["sqlite_lock_contention"] is True
    assert result["places_orders"] is False
    worker.commit_conn.close()


def test_lane_worker_reopen_replaces_connection_and_canonical_ingress(monkeypatch, tmp_path: Path) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    old_connection = FakeConnection()
    new_connection = FakeConnection()
    new_ingress = object()
    monkeypatch.setattr(daemon, "_open_runtime", lambda _path: new_connection)
    monkeypatch.setattr(daemon, "ReportedTrajectoryAwcIngress", lambda _conn, *, config: new_ingress)

    worker = object.__new__(daemon.LaneWorker)
    worker.db_path = tmp_path / "archive.db"
    worker.conn = old_connection
    worker.config = {}
    worker.ingress = object()
    worker.stations = ["ZUUU"]
    worker.next_station_refresh = 123.0

    worker.reopen()

    assert old_connection.closed is True
    assert worker.conn is new_connection
    assert worker.ingress is new_ingress
    assert worker.stations == []
    assert worker.next_station_refresh == 0.0

def test_stage_policy_lineage_guard_reports_a_split_chain() -> None:
    """The three stages are one chain, so one lineage is the invariant.

    The 2026-07-30 target-v2 rollout landed P0 and P1 and left P0-M on
    target-v1.  Nothing complained: the activation table has no cross-stage
    constraint, the resolver reads one stage at a time, and the ingress never
    checked that a packet carried all three.  The chain ran split for most of a
    month and the only symptom was stations quietly producing no opening plan.
    """

    import sqlite3
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from weather_basket_yes_daemon import stage_policy_lineage_guard

    window = ("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")

    def build(tmp: Path, rows: list[tuple]) -> Path:
        path = tmp / "activations.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE basket_policy_activation("
            " activation_id TEXT PRIMARY KEY, station_id TEXT, stage TEXT,"
            " strategy_version TEXT, generation INTEGER, global_prior_id TEXT,"
            " valid_from_utc TEXT, valid_until_utc TEXT);"
        )
        conn.executemany(
            "INSERT INTO basket_policy_activation VALUES(?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        conn.close()
        return path

    def rows_for(lineage: dict[str, tuple[str, int, str]]) -> list[tuple]:
        return [
            (f"a{index}", "KATL", stage, *value, *window)
            for index, (stage, value) in enumerate(lineage.items())
        ]

    healthy = {
        "P0": ("target-v2", 5, "g5"),
        "P0-M": ("target-v2", 5, "g5"),
        "P1": ("target-v2", 5, "g5"),
    }
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        assert stage_policy_lineage_guard(build(tmp, rows_for(healthy)))["state"] == (
            "one_lineage"
        )

    split = dict(healthy, **{"P0-M": ("target-v1", 4, "design-v3")})
    with tempfile.TemporaryDirectory() as raw:
        result = stage_policy_lineage_guard(build(Path(raw), rows_for(split)))
    assert result["violations"] == ["stage_policy_lineage_split"]
    assert result["split_station_count"] == 1

    # A stage that was never rolled out at all is the same fault wearing a
    # different face, and it must not read as healthy either.
    absent = {key: value for key, value in healthy.items() if key != "P0-M"}
    with tempfile.TemporaryDirectory() as raw:
        result = stage_policy_lineage_guard(build(Path(raw), rows_for(absent)))
    assert result["violations"] == ["stage_policy_lineage_incomplete"]



def test_serving_arithmetic_is_stamped_at_the_start_that_served_it() -> None:
    """The floor wants the start that ran the code, not the observer's clock.

    Before this the stamp came from whichever refit first noticed, which runs
    daily: the 2026-08-25 charter batch landed at 13:49:50Z and was stamped
    17:02:34Z, three hours of an eight-day wait spent on nothing.
    """

    conn = sqlite3.connect(":memory:")
    try:
        first = daemon._stamp_serving_arithmetic(
            conn, started_at_utc="2026-08-26T10:00:00.000Z"
        )
        assert first["state"] == "ok"
        assert sorted(first["stages"]) == ["P0", "P1"]  # P0-M retired (charter 283)
        assert all(first["stages"][stage] for stage in first["stages"])

        # A restart on unchanged code must not move the boundary forward.
        later = daemon._stamp_serving_arithmetic(
            conn, started_at_utc="2026-08-26T12:00:00.000Z"
        )
        assert later["state"] == "ok"
        stamps = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT stage,first_seen_utc FROM calibration_serving_arithmetic"
            )
        }
        assert set(stamps.values()) == {"2026-08-26T10:00:00.000Z"}
    finally:
        conn.close()


def test_serving_arithmetic_stamp_is_inert_without_a_writer() -> None:
    """A resident that could not open the target database stamps nothing."""

    assert daemon._stamp_serving_arithmetic(
        None, started_at_utc="2026-08-26T10:00:00.000Z"
    ) == {"state": "no_writer", "stages": {}}


def test_a_transient_resource_bound_at_writer_open_is_recoverable() -> None:
    """A momentary shortage must not need a person to restart the daemon.

    The writer is opened once, before the loop, so a failure there sets
    ``commit_error`` for the life of the process and every cycle afterwards
    publishes blocked_fail_closed with nothing retrying it.  Measured
    2026-09-01: free disk dipped under the 512 MiB floor at 12:13Z, the open
    failed, and production stayed down for twenty-six minutes -- with 23 GB
    free and the guard passing when asked directly.  Only a restart cleared it.

    Each of these bounds clears on its own, so each one gets the reopen the
    daemon already performs for a SQLite I/O fault.
    """

    for code in (
        "target_database_min_free_space_guard",
        "target_database_wal_bytes_exceeded",
        # Raised twice on 2026-09-01, and each raise needed a restart.
        "target_database_bytes_exceeded",
    ):
        assert daemon.target_result_has_recoverable_sqlite_io_error({
            "reason": f"target_writer_open_failed:ResourceBoundError:{code}",
        }), code
        assert daemon.target_result_has_recoverable_sqlite_io_error({
            "reason": f"target_writer_reopen_failed:ResourceBoundError:{code}",
        }), code


def test_a_store_that_becomes_ready_after_the_open_is_recoverable() -> None:
    """A table a lane installs after the writer opened must not need a restart.

    2026-09-23 (user ruling 「加」): a restart at 13:17Z loaded another
    session's mid-edit ``ssot.py``, which listed
    ``calibration_native_ensemble_certificate_renewal`` among the required
    tables before the lane that creates it had run.  The lane created it
    minutes later; the writer, opened once before the loop, published
    ``target_writer_schema_not_ready`` for seventeen minutes while the store
    answered ``ok`` to a direct probe, until a second restart.
    """

    payload = (
        '{"missing_tables": ["calibration_native_ensemble_certificate_renewal"],'
        ' "ok": false}'
    )
    for prefix in ("target_writer_open_failed", "target_writer_reopen_failed"):
        assert daemon.target_result_has_recoverable_sqlite_io_error({
            "reason": f"{prefix}:RuntimeError:target_writer_schema_not_ready:{payload}",
        }), prefix
    # The same refusal buried one level down is still the writer's open.
    assert daemon.target_result_has_recoverable_sqlite_io_error({
        "market_ingress": {
            "reason": "target_writer_open_failed:RuntimeError:target_writer_schema_not_ready:{}",
        },
    })
    # A store that is not in WAL mode is a misconfiguration, not a wait.
    assert not daemon.target_result_has_recoverable_sqlite_io_error({
        "reason": "target_writer_open_failed:RuntimeError:target_writer_journal_mode_not_wal:delete",
    })


def test_a_misconfigured_writer_target_is_not_retried_away() -> None:
    """Pointing the target lane at the shared live database is never transient.

    Retrying it every cycle would bury the one blocker an operator has to see,
    behind a connection_recovery receipt that reads like progress.
    """

    assert not daemon.target_result_has_recoverable_sqlite_io_error({
        "reason": (
            "target_writer_open_failed:ResourceBoundError:"
            "target_resource_guard_shared_live_database"
        ),
    })
    # And an ordinary business refusal is still not a connection fault.
    assert not daemon.target_result_has_recoverable_sqlite_io_error({
        "reason": "ContractError:production_quote_expired",
    })
    assert not daemon.target_result_has_recoverable_sqlite_io_error({
        "reason": "materialized_probability_envelope_missing",
    })
