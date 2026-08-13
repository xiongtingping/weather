from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.ssot import (
    _migrate_historical_taf_truth_label_authority,
    apply_target_ssot_schema,
)


LEGACY_TABLE = "calibration_historical_taf_capture_retired_temporary_awc_v1"
FORMAL = "FORMAL_STATION_LANE_TRUTH"
RETIRED = "TEMPORARY_AWC_CALIBRATION_LABEL"

# The pre-retirement production DDL: the CHECK still admits the retired
# temporary label, exactly as data/bayes_basket_target_ssot.db was created.
_PERMISSIVE_DDL = """
CREATE TABLE calibration_historical_taf_capture (
  historical_taf_capture_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  decision_anchor_utc TEXT NOT NULL,
  issued_at_utc TEXT NOT NULL,
  provider_available_at_utc TEXT NOT NULL,
  tx_c REAL NOT NULL,
  tx_valid_at_utc TEXT NOT NULL,
  cloud_cover_fraction REAL NOT NULL
    CHECK(cloud_cover_fraction>=0 AND cloud_cover_fraction<=1),
  precipitation_probability REAL NOT NULL
    CHECK(precipitation_probability>=0 AND
          precipitation_probability<=1),
  wind_speed_mps REAL NOT NULL CHECK(wind_speed_mps>=0),
  raw_taf_sha256 TEXT NOT NULL CHECK(length(raw_taf_sha256)=64),
  taf_response_sha256 TEXT NOT NULL
    CHECK(length(taf_response_sha256)=64),
  source_sample_sha256 TEXT NOT NULL
    CHECK(length(source_sample_sha256)=64),
  source_class TEXT NOT NULL CHECK(
    source_class='official_date_addressed_historical_replay'
  ),
  truth_label_authority TEXT NOT NULL CHECK(
    truth_label_authority IN (
      'FORMAL_STATION_LANE_TRUTH',
      'TEMPORARY_AWC_CALIBRATION_LABEL'
    )
  ),
  authority_scope TEXT NOT NULL CHECK(
    authority_scope='CALIBRATION_TRAIN_ONLY'
  ),
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  CHECK(issued_at_utc<=provider_available_at_utc),
  CHECK(provider_available_at_utc<=decision_anchor_utc),
  UNIQUE(station_id,event_date,source_sample_sha256)
);
CREATE INDEX calibration_historical_taf_capture_scope_idx
  ON calibration_historical_taf_capture(
    station_id,event_date,provider_available_at_utc);
CREATE TRIGGER calibration_historical_taf_capture_no_update
  BEFORE UPDATE ON calibration_historical_taf_capture
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_historical_taf_capture_no_delete
  BEFORE DELETE ON calibration_historical_taf_capture
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


def _row(
    *,
    label: str,
    marker: str,
    station: str = "ZBAA",
    event_date: str = "2026-07-01",
    tx_c: float = 30.0,
) -> tuple:
    return (
        f"historical-taf-{marker}",
        station,
        event_date,
        "Asia/Shanghai",
        "2026-06-30T17:00:00Z",
        "2026-06-30T15:05:00Z",
        "2026-06-30T16:00:31Z",
        tx_c,
        "2026-07-01T07:00:00Z",
        0.0,
        0.0,
        4.115552,
        "a" * 64,
        marker[0] * 64,
        (marker[0] + marker[-1]).ljust(64, marker[-1])[:64],
        "official_date_addressed_historical_replay",
        label,
        "CALIBRATION_TRAIN_ONLY",
        marker.ljust(64, "0")[:64],
    )


_INSERT = (
    "INSERT INTO calibration_historical_taf_capture VALUES("
    + ",".join("?" for _ in range(19))
    + ")"
)


def _permissive_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_PERMISSIVE_DDL)
    return conn


def test_retires_replaced_temporary_rows_and_installs_strict_check() -> None:
    conn = _permissive_db()
    conn.execute(_INSERT, _row(label=RETIRED, marker="1a"))
    conn.execute(_INSERT, _row(label=FORMAL, marker="2b"))
    conn.execute(
        _INSERT, _row(label=FORMAL, marker="3c", event_date="2026-07-02")
    )

    result = _migrate_historical_taf_truth_label_authority(conn)

    assert result["retired_temporary_awc_rows_quarantined"] == 1
    assert result["unlabelled_rows"] == 0
    labels = {
        row[0]
        for row in conn.execute(
            "SELECT truth_label_authority "
            "FROM calibration_historical_taf_capture"
        )
    }
    assert labels == {FORMAL}
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM calibration_historical_taf_capture"
        ).fetchone()[0]
        == 2
    )
    archived = conn.execute(
        f"SELECT truth_label_authority, COUNT(*) FROM {LEGACY_TABLE}"
    ).fetchone()
    assert archived == (RETIRED, 1)
    # The rebuilt table carries the canonical FORMAL-only CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, _row(label=RETIRED, marker="4d"))
    # Both tables stay append-only.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM calibration_historical_taf_capture")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {LEGACY_TABLE}")
    # Canonical scope index survives the rebuild.
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND "
            "name='calibration_historical_taf_capture_scope_idx'"
        ).fetchone()
        is not None
    )


def test_second_run_is_a_no_op() -> None:
    conn = _permissive_db()
    conn.execute(_INSERT, _row(label=RETIRED, marker="1a"))
    conn.execute(_INSERT, _row(label=FORMAL, marker="2b"))
    _migrate_historical_taf_truth_label_authority(conn)

    result = _migrate_historical_taf_truth_label_authority(conn)

    assert result["retired_temporary_awc_rows_quarantined"] == 0
    assert (
        conn.execute(f"SELECT COUNT(*) FROM {LEGACY_TABLE}").fetchone()[0]
        == 1
    )


def test_unreplaced_temporary_row_blocks_retirement() -> None:
    conn = _permissive_db()
    conn.execute(_INSERT, _row(label=RETIRED, marker="1a"))
    # Same station day but a different payload is not a replacement.
    conn.execute(_INSERT, _row(label=FORMAL, marker="2b", tx_c=31.0))

    with pytest.raises(RuntimeError) as excinfo:
        _migrate_historical_taf_truth_label_authority(conn)
    assert "unreplaced" in str(excinfo.value)
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (LEGACY_TABLE,),
        ).fetchone()
        is None
    )


def test_other_invalid_label_still_raises() -> None:
    conn = sqlite3.connect(":memory:")
    # The ALTER-added variant has no CHECK, so arbitrary labels can exist.
    conn.executescript(
        _PERMISSIVE_DDL.replace(
            """truth_label_authority TEXT NOT NULL CHECK(
    truth_label_authority IN (
      'FORMAL_STATION_LANE_TRUTH',
      'TEMPORARY_AWC_CALIBRATION_LABEL'
    )
  )""",
            "truth_label_authority TEXT",
        )
    )
    conn.execute(_INSERT, _row(label="BOGUS_LABEL", marker="1a"))

    with pytest.raises(RuntimeError) as excinfo:
        _migrate_historical_taf_truth_label_authority(conn)
    assert "invalid_rows" in str(excinfo.value)


def test_target_ssot_apply_runs_the_retirement() -> None:
    conn = _permissive_db()
    conn.execute(_INSERT, _row(label=RETIRED, marker="1a"))
    conn.execute(_INSERT, _row(label=FORMAL, marker="2b"))

    status = apply_target_ssot_schema(conn)

    migration = status["historical_taf_truth_label_migration"]
    assert migration["retired_temporary_awc_rows_quarantined"] == 1
    assert (
        conn.execute(f"SELECT COUNT(*) FROM {LEGACY_TABLE}").fetchone()[0]
        == 1
    )
