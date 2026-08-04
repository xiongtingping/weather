from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import ContractError
from weather_basket_yes_schema import apply_weather_basket_yes_schema
import scripts.apply_reported_trajectory_correction_review_schema as migration


def _legacy_database(tmp_path: Path) -> Path:
    path = tmp_path / "archive.db"
    with sqlite3.connect(path) as conn:
        apply_weather_basket_yes_schema(conn)
        conn.execute(
            "DROP TRIGGER reported_trajectory_halt_reviews_no_update"
        )
        conn.execute(
            "DROP TRIGGER reported_trajectory_halt_reviews_no_delete"
        )
        conn.execute(
            "DROP TABLE reported_trajectory_station_halt_reviews"
        )
        conn.execute(
            """
            INSERT INTO reported_trajectory_observations(
              observation_id,source_lane,station_id,report_type,valid_at,
              reported_temperature_c,raw_sha256,raw_text,first_seen_at,
              parsed_at,canonical_committed_at,fetch_started_at,
              fetch_completed_at,source_poll_id,correction_suspected
            ) VALUES(
              'existing','awc','RKPK','METAR','2026-07-28T03:00:00Z',
              35,?,'METAR RKPK','2026-07-28T03:05:00Z',
              '2026-07-28T03:05:01Z','2026-07-28T03:05:02Z',
              '2026-07-28T03:04:59Z','2026-07-28T03:05:00Z','poll-1',0
            )
            """,
            ("a" * 64,),
        )
    return path


def test_narrow_migration_preserves_data_and_installs_append_only_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _legacy_database(tmp_path)
    monkeypatch.setattr(migration, "_service_running", lambda: False)

    result = migration.migrate(
        db_path=path,
        confirmed_stopped=True,
    )

    assert result["ok"] is True
    assert result["quick_check"] == "ok"
    assert result["performs_legacy_backfill"] is False
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT observation_id FROM reported_trajectory_observations"
        ).fetchone()[0] == "existing"
        assert conn.execute(
            "SELECT COUNT(*) "
            "FROM reported_trajectory_station_halt_reviews"
        ).fetchone()[0] == 0


def test_migration_refuses_running_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _legacy_database(tmp_path)
    monkeypatch.setattr(migration, "_service_running", lambda: True)

    with pytest.raises(
        ContractError,
        match="writer_still_running",
    ):
        migration.migrate(
            db_path=path,
            confirmed_stopped=True,
        )
