from __future__ import annotations

import sqlite3
from pathlib import Path

from bayes_basket_target.correction_review import (
    CLEAR_REFERENCE_ONLY,
    QUARANTINE_STATION_DAY,
)
from reported_trajectory_no_schema import (
    apply_reported_trajectory_no_schema,
)
from scripts.review_reported_trajectory_station_halt import apply_review


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "archive.db"
    with sqlite3.connect(path) as conn:
        apply_reported_trajectory_no_schema(conn)
        conn.execute(
            """
            INSERT INTO reported_trajectory_market_registry(
              slug,event_date,outcome,city_slug,station_id,
              settlement_timezone,settlement_unit,source_lane,
              source_eligible,source_block_reason,resolution_url,rules_sha256,
              yes_token,no_token,market_active,market_accepting_orders,
              registry_json,refreshed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "rkpk-high-2026-07-28", "2026-07-28", "35°C",
                "busan", "RKPK", "Asia/Seoul", "celsius", "awc", 1,
                "", "https://example.invalid", "a" * 64,
                "yes", "no", 1, 1, "{}", "2026-07-27T14:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO reported_trajectory_observations(
              observation_id,source_lane,station_id,report_type,valid_at,
              reported_temperature_c,raw_sha256,raw_text,first_seen_at,
              parsed_at,canonical_committed_at,fetch_started_at,
              fetch_completed_at,source_poll_id,correction_suspected
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "cor-0315", "awc", "RKPK", "SPECI",
                "2026-07-28T03:15:00Z", 34, "b" * 64,
                "SPECI RKPK COR", "2026-07-28T03:25:00Z",
                "2026-07-28T03:25:01Z", "2026-07-28T03:25:02Z",
                "2026-07-28T03:24:59Z", "2026-07-28T03:25:00Z",
                "poll-1", 1,
            ),
        )
        conn.execute(
            """
            INSERT INTO reported_trajectory_station_halts VALUES(
              'awc','RKPK','2026-07-28','active',
              'metar_correction_requires_manual_review','cor-0315',
              '2026-07-28T03:25:02Z',NULL
            )
            """
        )
    return path


def test_clear_review_atomically_releases_only_derived_halt_cache(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)

    result = apply_review(
        db_path=path,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=CLEAR_REFERENCE_ONLY,
        reviewer="operator-xiong",
        rationale="The corrected non-hourly SPECI remains reference-only.",
        reviewed_at="2026-07-28T04:30:00Z",
    )

    assert result["resolution"]["state"] == "CLEARED_REFERENCE_ONLY"
    assert result["calls_order_gateway"] is False
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT state,reason_code FROM reported_trajectory_station_halts"
        ).fetchone() == (
            "cleared", "manual_review_clear_reference_only"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM reported_trajectory_station_halt_reviews"
        ).fetchone()[0] == 1


def test_quarantine_review_keeps_station_day_halted(tmp_path: Path) -> None:
    path = _database(tmp_path)

    result = apply_review(
        db_path=path,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=QUARANTINE_STATION_DAY,
        reviewer="operator-xiong",
        rationale="The corrected report makes the station day ambiguous.",
        reviewed_at="2026-07-28T04:30:00Z",
    )

    assert result["resolution"]["state"] == "QUARANTINED_STATION_DAY"
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT state,reason_code FROM reported_trajectory_station_halts"
        ).fetchone() == (
            "active", "manual_review_quarantine_station_day"
        )
