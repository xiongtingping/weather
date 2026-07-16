from __future__ import annotations

import json
import sqlite3

import physical_x_prior_record_reader_runtime as reader
from resolution_final_source_runtime import PHYSICAL_ROLE, PHYSICAL_SOURCE_ID


def _make_final_label_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE resolution_final_source_observations (
            source_observation_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            role TEXT NOT NULL,
            station_id TEXT NOT NULL,
            event_date TEXT NOT NULL,
            raw_ref TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            observation_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO resolution_final_source_observations VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "physical-1",
            PHYSICAL_SOURCE_ID,
            PHYSICAL_ROLE,
            "ZUUU",
            "2026-07-12",
            "coverage/1.json",
            "a" * 64,
            json.dumps({"payload": {"label_finality": "final", "final_physical_high_c": 33.0}}),
            "2026-07-12T16:00:00Z",
        ),
    )
    conn.commit()
    conn.close()


def test_reader_uses_forward_final_x_over_same_day_historical_auxiliary(tmp_path, monkeypatch) -> None:
    db = tmp_path / "labels.db"
    _make_final_label_db(db)

    monkeypatch.setattr(
        reader,
        "_historical_records",
        lambda *_args, **_kwargs: (
            [
                {
                    "event_date": "2026-07-11",
                    "station_id": "ZUUU",
                    "available_at": "2026-07-12T00:00:00Z",
                    "final_physical_high_c": 31.0,
                    "raw_sha256": "b" * 64,
                    "source_kind": "amsc_historical_complete",
                },
                {
                    "event_date": "2026-07-12",
                    "station_id": "ZUUU",
                    "available_at": "2026-07-12T00:00:00Z",
                    "final_physical_high_c": 32.0,
                    "raw_sha256": "c" * 64,
                    "source_kind": "amsc_historical_complete",
                },
            ],
            {},
        ),
    )
    result = reader.load_physical_x_prior_records(final_label_db=db, historical_archive_dir=tmp_path / "history")

    assert result["ok"] is True
    assert result["deduplicated_record_count"] == 2
    by_day = {row["event_date"]: row for row in result["records"]}
    assert by_day["2026-07-12"]["source_kind"] == "amsc_physical_final"
    assert by_day["2026-07-12"]["final_physical_high_c"] == 33.0
    assert result["boundary"]["settlement_market_forecast_d0_observation_and_orders_excluded"] is True
