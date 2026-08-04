from __future__ import annotations

import sqlite3
import hashlib
from pathlib import Path

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.correction_review import (
    CLEAR_REFERENCE_ONLY,
    QUARANTINE_STATION_DAY,
)
from scripts.build_awc_correction_review_packet import (
    build_awc_correction_review_packet,
)


DAY = "2026-07-31"
AS_OF = "2026-08-01T12:00:00Z"


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE reported_trajectory_market_registry (
          event_date TEXT NOT NULL,
          source_lane TEXT NOT NULL,
          source_eligible INTEGER NOT NULL,
          station_id TEXT NOT NULL,
          settlement_timezone TEXT NOT NULL
        );
        CREATE TABLE reported_trajectory_observations (
          observation_id TEXT PRIMARY KEY,
          source_lane TEXT NOT NULL,
          station_id TEXT NOT NULL,
          report_type TEXT NOT NULL,
          valid_at TEXT NOT NULL,
          reported_temperature_c INTEGER NOT NULL,
          raw_sha256 TEXT NOT NULL,
          raw_text TEXT NOT NULL,
          canonical_committed_at TEXT NOT NULL,
          correction_suspected INTEGER NOT NULL
        );
        """
    )
    return conn


def _registry(conn: sqlite3.Connection, *, station_id: str, timezone_name: str) -> None:
    conn.execute(
        "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?)",
        (DAY, "awc", 1, station_id, timezone_name),
    )


def _observation(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    station_id: str,
    report_type: str,
    valid_at: str,
    temperature_c: int,
    correction: bool,
    committed_at: str,
    cor_token: bool = True,
) -> None:
    suffix = " COR" if correction and cor_token else ""
    conn.execute(
        "INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            observation_id,
            "awc",
            station_id,
            report_type,
            valid_at,
            temperature_c,
            _sha(observation_id[-1]),
            f"{report_type} {station_id} 311700Z{suffix} TEST",
            committed_at,
            int(correction),
        ),
    )


def _packet_source(path: Path) -> None:
    conn = _db(path)
    _registry(conn, station_id="SAEZ", timezone_name="America/Argentina/Buenos_Aires")
    _registry(conn, station_id="WMKK", timezone_name="Asia/Kuala_Lumpur")
    _registry(conn, station_id="KLAX", timezone_name="America/Los_Angeles")

    # The two whole-hour corrections mirror the special production treatment:
    # no clear decision is permissible without a replacement contract.
    _observation(conn, observation_id="saez-original", station_id="SAEZ", report_type="METAR", valid_at="2026-07-31T17:00:00.000Z", temperature_c=19, correction=False, committed_at="2026-07-31T17:04:00.000Z")
    _observation(conn, observation_id="saez-cor", station_id="SAEZ", report_type="METAR", valid_at="2026-07-31T17:00:00.000Z", temperature_c=19, correction=True, committed_at="2026-07-31T17:09:00.000Z")
    _observation(conn, observation_id="wmkk-original", station_id="WMKK", report_type="METAR", valid_at="2026-07-30T18:00:00.000Z", temperature_c=25, correction=False, committed_at="2026-07-30T18:08:00.000Z")
    _observation(conn, observation_id="wmkk-cor", station_id="WMKK", report_type="METAR", valid_at="2026-07-30T18:00:00.000Z", temperature_c=25, correction=True, committed_at="2026-07-30T19:09:00.000Z")

    # A non-hourly SPECI remains manually reviewable, but is never approved by
    # packet construction even when the original and COR temperatures match.
    _observation(conn, observation_id="klax-original", station_id="KLAX", report_type="SPECI", valid_at="2026-07-31T09:38:00.000Z", temperature_c=20, correction=False, committed_at="2026-07-31T09:39:00.000Z")
    _observation(conn, observation_id="klax-cor", station_id="KLAX", report_type="SPECI", valid_at="2026-07-31T09:38:00.000Z", temperature_c=20, correction=True, committed_at="2026-07-31T09:45:00.000Z")
    conn.commit()
    conn.close()


def test_builds_content_addressed_manual_review_rows_without_source_writes(tmp_path: Path) -> None:
    source = tmp_path / "resident.db"
    _packet_source(source)
    with sqlite3.connect(source) as conn:
        before = conn.execute("SELECT COUNT(*) FROM reported_trajectory_observations").fetchone()[0]

    packet = build_awc_correction_review_packet(
        source_db=source,
        event_date=DAY,
        as_of_utc=AS_OF,
        expected_station_count=3,
    )

    with sqlite3.connect(source) as conn:
        after = conn.execute("SELECT COUNT(*) FROM reported_trajectory_observations").fetchone()[0]
    assert after == before == 6
    assert packet["stats"]["station_count"] == 3
    assert packet["stats"]["correction_observation_count"] == 3
    assert packet["stats"]["same_slot_original_present_count"] == 3
    assert packet["stats"]["temperature_unchanged_count"] == 3
    assert packet["review_boundary"] == {
        "manual_review_required": True,
        "auto_approved": False,
        "writes_database": False,
        "calls_services": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    assert packet["content_sha256"] == content_sha256({
        key: value
        for key, value in packet.items()
        if key not in {"content_sha256", "packet_id"}
    })

    by_station = {row["station_id"]: row for row in packet["observations"]}
    for station_id in ("SAEZ", "WMKK"):
        row = by_station[station_id]
        assert row["whole_hour"] is True
        assert row["allowed_actions"] == [QUARANTINE_STATION_DAY]
        assert row["default_action"] == QUARANTINE_STATION_DAY
        assert row["auto_approved"] is False
    klax = by_station["KLAX"]
    assert klax["is_speci"] is True
    assert klax["whole_hour"] is False
    assert klax["allowed_actions"] == [
        CLEAR_REFERENCE_ONLY,
        QUARANTINE_STATION_DAY,
    ]
    assert klax["default_action"] is None
    assert klax["temperature_changed"] is False
    assert klax["auto_approved"] is False


def test_missing_original_is_explicit_and_never_auto_approved(tmp_path: Path) -> None:
    source = tmp_path / "resident.db"
    conn = _db(source)
    _registry(conn, station_id="KATL", timezone_name="America/New_York")
    _observation(conn, observation_id="katl-cor", station_id="KATL", report_type="SPECI", valid_at="2026-07-31T12:34:00.000Z", temperature_c=31, correction=True, committed_at="2026-07-31T12:36:00.000Z")
    conn.commit()
    conn.close()

    packet = build_awc_correction_review_packet(
        source_db=source,
        event_date=DAY,
        as_of_utc=AS_OF,
        expected_station_count=1,
    )

    row = packet["observations"][0]
    assert row["original_same_slot"] == {"state": "MISSING", "observation": None}
    assert row["temperature_changed"] is None
    assert row["temperature_change_state"] == "ORIGINAL_MISSING"
    assert row["allowed_actions"] == [
        CLEAR_REFERENCE_ONLY,
        QUARANTINE_STATION_DAY,
    ]
    assert row["default_action"] is None
    assert row["auto_approved"] is False


def test_fails_closed_for_missing_cor_token_and_scope_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "resident.db"
    conn = _db(source)
    _registry(conn, station_id="KATL", timezone_name="America/New_York")
    _observation(conn, observation_id="katl-bad", station_id="KATL", report_type="METAR", valid_at="2026-07-31T12:34:00.000Z", temperature_c=31, correction=True, committed_at="2026-07-31T12:36:00.000Z", cor_token=False)
    conn.commit()
    conn.close()

    with pytest.raises(ContractError, match="cor_token_missing"):
        build_awc_correction_review_packet(
            source_db=source,
            event_date=DAY,
            as_of_utc=AS_OF,
            expected_station_count=1,
        )

    conn = sqlite3.connect(source)
    conn.execute("UPDATE reported_trajectory_observations SET raw_text='METAR KATL 311234Z COR TEST'")
    conn.commit()
    conn.close()
    with pytest.raises(ContractError, match="expected_station_count_mismatch"):
        build_awc_correction_review_packet(
            source_db=source,
            event_date=DAY,
            as_of_utc=AS_OF,
            expected_station_count=2,
        )
