from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from bayes_basket_target.canonical_observation_export import export_canonical_observation_evidence
from bayes_basket_target.calibration_evidence import build_calibration_evidence
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.strict_asof_replay import materialize_model_station_days


SHA = "a" * 64


def _init_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE reported_trajectory_observations(
                observation_id TEXT PRIMARY KEY,
                source_lane TEXT NOT NULL,
                station_id TEXT NOT NULL,
                report_type TEXT NOT NULL,
                valid_at TEXT NOT NULL,
                reported_temperature_c INTEGER NOT NULL,
                raw_sha256 TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                parsed_at TEXT NOT NULL,
                canonical_committed_at TEXT NOT NULL,
                fetch_started_at TEXT NOT NULL,
                fetch_completed_at TEXT NOT NULL,
                source_poll_id TEXT NOT NULL,
                correction_suspected INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE reported_trajectory_source_polls(
                poll_id TEXT PRIMARY KEY,
                source_lane TEXT NOT NULL,
                station_ids_json TEXT NOT NULL,
                request_started_at TEXT NOT NULL,
                response_started_at TEXT,
                response_completed_at TEXT NOT NULL,
                response_status INTEGER,
                duration_ms REAL NOT NULL,
                report_count INTEGER NOT NULL,
                new_report_count INTEGER NOT NULL,
                error_code TEXT,
                poll_json TEXT NOT NULL
            )"""
        )


def _insert(path: Path, *, observation_id: str, lane: str = "amsc", station: str = "ZBAA", day: str = "2026-07-01", correction: int = 0, raw_sha: str = SHA, valid_at: str | None = None, first_seen: str | None = None, canonical: str | None = None, temperature_c: int = 30) -> None:
    valid = valid_at or f"{day}T12:00:00Z"
    first = first_seen or f"{day}T12:01:00Z"
    committed = canonical or f"{day}T12:01:01Z"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO reported_trajectory_source_polls VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"poll-{observation_id}", lane, f'["{station}"]',
                f"{day}T12:00:20Z", f"{day}T12:00:21Z",
                first, 200, 10.0, 1, 1, None,
                f'{{"poll_id":"poll-{observation_id}"}}',
            ),
        )
        conn.execute(
            "INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                observation_id, lane, station, "METAR", valid, temperature_c, raw_sha,
                "raw", first, first, committed, f"{day}T12:00:30Z", first,
                f"poll-{observation_id}", correction,
            ),
        )


def _export(path: Path, *, output: Path | None = None):
    return export_canonical_observation_evidence(
        source_db=path,
        output_path=output,
        station_timezones={"ZBAA": "Asia/Shanghai"},
    )


def test_export_is_read_only_and_binds_canonical_timestamps(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _init_db(db)
    _insert(db, observation_id="obs-1")
    result = _export(db, output=tmp_path / "evidence.json")

    assert result["state"] == "EXPORTED"
    assert result["checks"]["strict_timestamp_ordering"] is True
    assert result["boundary"]["reads_shared_live_database"] is True
    assert result["boundary"]["writes_shared_live_database"] is False
    assert result["source"]["resource_bounds"] == {
        "max_source_rows": 20_000,
        "fetch_batch_rows": 256,
    }
    assert result["truth_rows"][0]["received_at_utc"] == "2026-07-01T12:01:00.000Z"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("x",) * 15)


def test_export_source_rows_are_hard_bounded_before_materialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "bounded.db"
    _init_db(db)
    _insert(db, observation_id="one")
    _insert(db, observation_id="two", raw_sha="b" * 64)
    monkeypatch.setattr(
        "bayes_basket_target.canonical_observation_export.MAX_CANONICAL_SOURCE_ROWS",
        1,
    )

    with pytest.raises(
        ContractError,
        match="canonical_source_row_limit_exceeded",
    ):
        _export(db, output=tmp_path / "must-not-exist.json")

    assert not (tmp_path / "must-not-exist.json").exists()


def test_zuuu_midnight_report_belongs_to_new_local_day_by_valid_time(
    tmp_path: Path,
) -> None:
    db = tmp_path / "zuuu-midnight.db"
    _init_db(db)
    _insert(
        db,
        observation_id="before-midnight",
        station="ZUUU",
        valid_at="2026-07-29T15:00:00Z",
        first_seen="2026-07-29T16:02:00Z",
        canonical="2026-07-29T16:02:01Z",
        temperature_c=27,
    )
    _insert(
        db,
        observation_id="at-midnight",
        station="ZUUU",
        raw_sha="b" * 64,
        valid_at="2026-07-29T16:00:00Z",
        first_seen="2026-07-29T16:03:00Z",
        canonical="2026-07-29T16:03:01Z",
        temperature_c=28,
    )

    result = export_canonical_observation_evidence(
        source_db=db,
        station_timezones={"ZUUU": "Asia/Shanghai"},
        station_lane_map={"ZUUU": "amsc"},
    )

    by_day = {row["event_date"]: row for row in result["truth_rows"]}
    assert by_day["2026-07-29"]["official_tmax_c"] == 27
    assert by_day["2026-07-30"]["official_tmax_c"] == 28
    midnight = next(
        row for row in result["observations"]
        if row["observation_id"] == "at-midnight"
    )
    assert midnight["event_date_local"] == "2026-07-30"
    assert midnight["valid_at_utc"] == "2026-07-29T16:00:00.000Z"


def test_correction_halts_the_whole_station_day(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _init_db(db)
    _insert(db, observation_id="good", day="2026-07-02")
    _insert(db, observation_id="correction", day="2026-07-02", correction=1, raw_sha="b" * 64)
    result = _export(db)

    assert result["state"] == "EXPORTED"
    assert not [row for row in result["truth_rows"] if row["event_date"] == "2026-07-02"]
    quarantined = [row for row in result["quarantined_truth_rows"] if row["event_date"] == "2026-07-02"]
    assert quarantined
    assert result["checks"]["revisions_quarantined"] is True


def test_amsc_half_hour_is_reference_only_and_excluded_from_truth(tmp_path: Path) -> None:
    db = tmp_path / "half-hour.db"
    _init_db(db)
    _insert(db, observation_id="hourly", day="2026-07-03", valid_at="2026-07-03T12:00:00Z")
    _insert(
        db,
        observation_id="half-hour",
        day="2026-07-03",
        raw_sha="b" * 64,
        valid_at="2026-07-03T12:30:00Z",
        first_seen="2026-07-03T12:31:00Z",
        canonical="2026-07-03T12:31:01Z",
    )

    result = _export(db)

    assert result["state"] == "EXPORTED"
    assert result["reference_observation_count"] == 1
    assert result["reference_reason_counts"] == {"amsc_half_hour_reference_only": 1}
    assert [row["report_count"] for row in result["truth_rows"] if row["event_date"] == "2026-07-03"] == [1]
    reference = next(row for row in result["observations"] if row["observation_id"] == "half-hour")
    assert reference["evidence_role"] == "REFERENCE_ONLY"
    assert reference["qc_status"] == "REFERENCE_ONLY"


def test_reference_count_includes_reference_evidence_quarantined_by_revision(tmp_path: Path) -> None:
    db = tmp_path / "half-hour-revision.db"
    _init_db(db)
    _insert(
        db,
        observation_id="half-hour-a",
        day="2026-07-03",
        raw_sha="b" * 64,
        valid_at="2026-07-03T12:30:00Z",
        first_seen="2026-07-03T12:31:00Z",
        canonical="2026-07-03T12:31:01Z",
    )
    _insert(
        db,
        observation_id="half-hour-b",
        day="2026-07-03",
        raw_sha="c" * 64,
        valid_at="2026-07-03T12:30:00Z",
        first_seen="2026-07-03T12:32:00Z",
        canonical="2026-07-03T12:32:01Z",
    )

    result = _export(db)

    assert result["reference_observation_count"] == 2
    assert result["reference_reason_counts"] == {"amsc_half_hour_reference_only": 2}
    assert not result["truth_rows"]


def test_awc_hourly_for_amsc_station_is_reference_only_when_lane_map_is_bound(tmp_path: Path) -> None:
    db = tmp_path / "awc-reference.db"
    _init_db(db)
    _insert(db, observation_id="amsc-hour", day="2026-07-04", raw_sha="a" * 64)
    _insert(
        db,
        observation_id="awc-hour",
        lane="awc",
        day="2026-07-04",
        raw_sha="b" * 64,
        valid_at="2026-07-04T13:00:00Z",
        first_seen="2026-07-04T13:01:00Z",
        canonical="2026-07-04T13:01:01Z",
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE reported_trajectory_observations SET reported_temperature_c=99 WHERE observation_id='awc-hour'"
        )
    result = export_canonical_observation_evidence(
        source_db=db,
        station_timezones={"ZBAA": "Asia/Shanghai"},
        station_lane_map={"ZBAA": "amsc"},
    )
    assert result["state"] == "EXPORTED"
    assert result["accepted_truth_count"] == 1
    assert result["reference_reason_counts"] == {
        "awc_hourly_reference_only_for_amsc_station": 1,
    }
    assert not any("station_source_lane_mixed:ZBAA" in item for item in result["blockers"])


def test_amsc_half_hour_for_awc_station_is_cross_lane_blocked(tmp_path: Path) -> None:
    db = tmp_path / "amsc-half-hour-cross-lane.db"
    _init_db(db)
    _insert(
        db,
        observation_id="amsc-half-hour",
        raw_sha="b" * 64,
        valid_at="2026-07-04T13:30:00Z",
        first_seen="2026-07-04T13:31:00Z",
        canonical="2026-07-04T13:31:01Z",
    )
    result = export_canonical_observation_evidence(
        source_db=db,
        station_timezones={"ZBAA": "Asia/Shanghai"},
        station_lane_map={"ZBAA": "awc"},
    )
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert any("cross_fallback_forbidden" in item for item in result["blockers"])


def test_official_timing_roles_fail_closed_and_preserve_speci(tmp_path: Path) -> None:
    invalid = tmp_path / "amsc-invalid-minute.db"
    _init_db(invalid)
    _insert(
        invalid,
        observation_id="amsc-minute-15",
        valid_at="2026-07-03T12:15:00Z",
        first_seen="2026-07-03T12:16:00Z",
        canonical="2026-07-03T12:17:00Z",
    )
    result = export_canonical_observation_evidence(
        source_db=invalid,
        station_timezones={"ZBAA": "Asia/Shanghai"},
        station_lane_map={"ZBAA": "amsc"},
    )
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert any("amsc_metar_timing_invalid" in item for item in result["blockers"])

    speci = tmp_path / "amsc-speci.db"
    _init_db(speci)
    _insert(
        speci,
        observation_id="amsc-speci-15",
        valid_at="2026-07-03T12:00:42Z",
        first_seen="2026-07-03T12:16:00Z",
        canonical="2026-07-03T12:17:00Z",
    )
    with sqlite3.connect(speci) as conn:
        conn.execute(
            "UPDATE reported_trajectory_observations SET report_type='SPECI' WHERE observation_id=?",
            ("amsc-speci-15",),
        )
    result = export_canonical_observation_evidence(
        source_db=speci,
        station_timezones={"ZBAA": "Asia/Shanghai"},
        station_lane_map={"ZBAA": "amsc"},
    )
    assert result["state"] == "EXPORTED"
    assert result["accepted_truth_count"] == 1


def test_awc_owned_station_admits_routine_native_53_minute_and_rejects_amsc(tmp_path: Path) -> None:
    # KATL's real cadence is :52-:53 with no on-the-hour report at all; the
    # provider's own routine minute is formal evidence.
    native = tmp_path / "awc-native.db"
    _init_db(native)
    _insert(
        native,
        observation_id="katl-awc-53",
        lane="awc",
        station="KATL",
        valid_at="2026-07-03T13:53:00Z",
        first_seen="2026-07-03T13:54:00Z",
        canonical="2026-07-03T13:55:00Z",
    )
    result = export_canonical_observation_evidence(
        source_db=native,
        station_timezones={"KATL": "America/New_York"},
        station_lane_map={"KATL": "awc"},
    )
    assert result["state"] == "EXPORTED"
    assert result["accepted_truth_count"] == 1
    assert result["excluded_policy_observation_count"] == 0
    assert result["policy_exclusion_reason_counts"] == {}
    assert result["observations"][0]["evidence_role"] == "FORMAL"
    assert result["observations"][0]["qc_status"] == "ACCEPTED"

    cross = tmp_path / "awc-cross.db"
    _init_db(cross)
    _insert(
        cross,
        observation_id="katl-amsc-cross",
        lane="amsc",
        station="KATL",
        valid_at="2026-07-03T13:00:00Z",
        first_seen="2026-07-03T13:01:00Z",
        canonical="2026-07-03T13:02:00Z",
    )
    result = export_canonical_observation_evidence(
        source_db=cross,
        station_timezones={"KATL": "America/New_York"},
        station_lane_map={"KATL": "awc"},
    )
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert any("canonical_source_lane_cross_fallback_forbidden" in item for item in result["blockers"])


def test_amsc_hourly_wins_same_slot_over_awc_reference(tmp_path: Path) -> None:
    db = tmp_path / "amsc-primary.db"
    _init_db(db)
    _insert(
        db,
        observation_id="amsc-hour",
        raw_sha="a" * 64,
        valid_at="2026-07-04T13:00:00Z",
        first_seen="2026-07-04T13:01:00Z",
        canonical="2026-07-04T13:01:01Z",
    )
    _insert(
        db,
        observation_id="awc-hour",
        lane="awc",
        raw_sha="b" * 64,
        valid_at="2026-07-04T13:00:00Z",
        first_seen="2026-07-04T13:01:00Z",
        canonical="2026-07-04T13:01:01Z",
    )
    result = export_canonical_observation_evidence(
        source_db=db,
        station_timezones={"ZBAA": "Asia/Shanghai"},
        station_lane_map={"ZBAA": "amsc"},
    )

    assert result["state"] == "EXPORTED"
    assert result["accepted_truth_count"] == 1
    assert result["truth_rows"][0]["official_tmax_c"] == 30
    assert result["truth_rows"][0]["source_lane"] == "AMSC"
    awc = next(item for item in result["observations"] if item["observation_id"] == "awc-hour")
    assert awc["evidence_role"] == "REFERENCE_ONLY"
    assert awc["reference_reason"] == "awc_hourly_reference_only_for_amsc_station"


def test_lane_mixing_duplicate_and_timestamp_rewind_fail_closed(tmp_path: Path) -> None:
    mixed = tmp_path / "mixed.db"
    _init_db(mixed)
    _insert(mixed, observation_id="amsc")
    _insert(mixed, observation_id="awc", lane="awc", raw_sha="b" * 64)
    result = _export(mixed)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "station_source_lane_mixed:ZBAA" in result["blockers"]

    rewind = tmp_path / "rewind.db"
    _init_db(rewind)
    _insert(rewind, observation_id="rewind", first_seen="2026-07-01T12:00:00Z")
    with sqlite3.connect(rewind) as conn:
        conn.execute("UPDATE reported_trajectory_observations SET valid_at=? WHERE observation_id='rewind'", ("2026-07-01T12:02:00Z",))
    result = _export(rewind)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert any("timestamp_rewind" in item for item in result["blockers"])


def test_export_accepts_a_report_seen_seconds_before_its_nominal_minute(tmp_path: Path) -> None:
    early = tmp_path / "early.db"
    _init_db(early)
    # Nominal 12:01:00, on the wire at 12:00:40; the fetch that carried it
    # started at 12:00:30, so every other order still holds.
    _insert(
        early, observation_id="early", lane="awc", raw_sha="b" * 64,
        valid_at="2026-07-01T12:01:00Z", first_seen="2026-07-01T12:00:40Z",
    )
    result = _export(early)
    assert result["state"] == "EXPORTED"
    assert any(item["observation_id"] == "early" for item in result["observations"])

    rewind = tmp_path / "rewind_far.db"
    _init_db(rewind)
    _insert(
        rewind, observation_id="rewind", lane="awc", raw_sha="b" * 64,
        valid_at="2026-07-01T12:04:00Z", first_seen="2026-07-01T12:00:40Z",
    )
    result = _export(rewind)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert any("canonical_observation_timestamp_rewind" in item for item in result["blockers"])


def test_duplicate_identity_is_not_silently_deduplicated(tmp_path: Path) -> None:
    db = tmp_path / "duplicate.db"
    _init_db(db)
    _insert(db, observation_id="one")
    _insert(db, observation_id="two")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE reported_trajectory_observations SET observation_id='two', raw_sha256=? WHERE observation_id='two'", (SHA,))
    result = _export(db)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert any(item.startswith("duplicate_canonical_observation:") for item in result["blockers"])


def test_model_publish_after_receive_and_receive_after_asof_are_rejected() -> None:
    row = {
        "station_id": "ZBAA", "event_date": "2026-07-01", "provider_id": "ecmwf_ifs025_official",
        "model_run_initialization_utc": "2026-06-30T00:00:00Z", "valid_time_utc": "2026-07-01T12:00:00Z",
        "temperature_2m_c": "30", "raw_message_sha256": SHA, "receipt_sha256": SHA,
        "published_at_utc": "2026-06-30T02:00:00Z", "received_at_utc": "2026-06-30T01:00:00Z",
    }
    with pytest.raises(ContractError, match="published_after_received"):
        materialize_model_station_days(
            [row], target_timezone="America/New_York"
        )


def test_canonical_truth_precedes_iem_cross_check(tmp_path: Path) -> None:
    db = tmp_path / "canonical.db"
    _init_db(db)
    models: list[dict[str, str]] = []
    iem: list[dict[str, str]] = []
    for index in range(1, 7):
        target = date(2026, 7, index).isoformat()
        run_day = (date(2026, 7, index) - timedelta(days=1)).isoformat()
        _insert(db, observation_id=f"canonical-{index}", day=target, raw_sha=(hex(index)[2:] * 64)[:64])
        iem.append({
            "station_id": "ZBAA", "event_date": target, "valid_time_utc": f"{target}T12:00:00Z",
            "temperature_c": "99", "raw_sha256": SHA, "source_raw_sha256": SHA,
        })
        for provider, forecast in (
            ("ecmwf_ifs025_official", 29 + index),
            ("gfs_0p25_official", 28 + index),
            ("icon_global_official", 27 + index),
        ):
            models.append({
                "station_id": "ZBAA", "event_date": target, "provider_id": provider,
                "target_timezone": "America/New_York",
                "model_run_initialization_utc": f"{run_day}T00:00:00Z", "valid_time_utc": f"{target}T12:00:00Z",
                "temperature_2m_c": str(forecast), "raw_message_sha256": SHA, "receipt_sha256": SHA,
            })
    canonical = _export(db)
    evidence = build_calibration_evidence(
        models, iem, as_of_utc="2026-07-07T00:00:00Z",
        target_timezone="America/New_York",
        canonical_observation_evidence=canonical,
        station_lane_map={"ZBAA": "amsc"},
    )
    assert evidence["checks"]["truth_source_canonical_ingress"] is True
    assert evidence["truth_evidence"][0]["official_tmax_c"] == 30
    assert evidence["truth_evidence"][0]["source_role"] == "CANONICAL_AMSC_AWC_INGRESS"


def test_calibration_rejects_awc_truth_for_amsc_station(tmp_path: Path) -> None:
    db = tmp_path / "canonical.db"
    _init_db(db)
    _insert(db, observation_id="wrong-lane", lane="awc")
    canonical = _export(db)

    with pytest.raises(ContractError, match="canonical_observation_source_lane_mismatch"):
        build_calibration_evidence(
            [],
            [],
            as_of_utc="2026-07-07T00:00:00Z",
            target_timezone="America/New_York",
            canonical_observation_evidence=canonical,
            station_lane_map={"ZBAA": "amsc"},
        )
