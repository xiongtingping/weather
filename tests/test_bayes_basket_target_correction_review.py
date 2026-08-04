from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target.correction_review import (
    CLEAR_REFERENCE_ONLY,
    QUARANTINE_STATION_DAY,
    append_correction_review,
    assert_station_day_corrections_resolved,
)
from weather_basket_yes_schema import (
    apply_reported_trajectory_correction_review_schema,
    apply_weather_basket_yes_schema,
)


START = "2026-07-27T15:00:00Z"
END = "2026-07-28T15:00:00Z"
AS_OF = "2026-07-28T05:00:00Z"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
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
    return conn


def _observation(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    valid_at: str,
    correction: int,
) -> None:
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
            observation_id, "awc", "RKPK",
            "SPECI" if correction else "METAR",
            valid_at, 34 if correction else 35,
            ("b" if correction else "a") * 64,
            "SPECI RKPK COR" if correction else "METAR RKPK",
            "2026-07-28T03:25:00Z",
            "2026-07-28T03:25:01Z",
            "2026-07-28T03:25:02Z",
            "2026-07-28T03:24:59Z",
            "2026-07-28T03:25:00Z",
            "poll-1",
            correction,
        ),
    )


def _state(conn: sqlite3.Connection) -> dict:
    return assert_station_day_corrections_resolved(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        window_start_utc=START,
        window_end_utc=END,
        as_of_utc=AS_OF,
    )


def test_unreviewed_correction_blocks_and_clear_receipt_is_idempotent() -> None:
    conn = _db()
    _observation(
        conn,
        observation_id="cor-0315",
        valid_at="2026-07-28T03:15:00Z",
        correction=1,
    )
    with pytest.raises(
        ContractError,
        match="correction_review_required:cor-0315",
    ):
        _state(conn)

    first = append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=CLEAR_REFERENCE_ONLY,
        reviewer="operator-xiong",
        rationale="COR applies only to the non-hourly SPECI; hourly METAR remains formal.",
        reviewed_at="2026-07-28T04:30:00Z",
    )
    repeated = append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=CLEAR_REFERENCE_ONLY,
        reviewer="operator-xiong",
        rationale="COR applies only to the non-hourly SPECI; hourly METAR remains formal.",
        reviewed_at="2026-07-28T04:30:00Z",
    )
    resolved = _state(conn)

    assert first["state"] == "committed"
    assert repeated["state"] == "idempotent"
    assert resolved["state"] == "CLEARED_REFERENCE_ONLY"
    assert resolved["review_ids"] == [first["review_id"]]


def test_latest_quarantine_supersedes_clear_and_blocks_station_day() -> None:
    conn = _db()
    _observation(
        conn,
        observation_id="cor-0315",
        valid_at="2026-07-28T03:15:00Z",
        correction=1,
    )
    append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=CLEAR_REFERENCE_ONLY,
        reviewer="operator-xiong",
        rationale="Initial review.",
        reviewed_at="2026-07-28T04:20:00Z",
    )
    append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=QUARANTINE_STATION_DAY,
        reviewer="operator-xiong",
        rationale="Subsequent evidence makes the station day ambiguous.",
        reviewed_at="2026-07-28T04:30:00Z",
    )

    with pytest.raises(
        ContractError,
        match="correction_review_quarantines_station_day",
    ):
        _state(conn)


def test_hourly_correction_cannot_be_cleared_without_replacement_contract() -> None:
    conn = _db()
    _observation(
        conn,
        observation_id="cor-0300",
        valid_at="2026-07-28T03:00:00Z",
        correction=1,
    )

    with pytest.raises(
        ContractError,
        match="hourly_clear_requires_replacement_contract",
    ):
        append_correction_review(
            conn,
            source_lane="awc",
            station_id="RKPK",
            event_date="2026-07-28",
            triggering_observation_id="cor-0300",
            decision=CLEAR_REFERENCE_ONLY,
            reviewer="operator-xiong",
            rationale="Invalid attempt to clear an hourly correction.",
            reviewed_at="2026-07-28T04:30:00Z",
        )


def test_review_rows_are_append_only() -> None:
    conn = _db()
    _observation(
        conn,
        observation_id="cor-0315",
        valid_at="2026-07-28T03:15:00Z",
        correction=1,
    )
    receipt = append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=CLEAR_REFERENCE_ONLY,
        reviewer="operator-xiong",
        rationale="Reviewed.",
        reviewed_at="2026-07-28T04:30:00Z",
    )

    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "UPDATE reported_trajectory_station_halt_reviews "
            "SET rationale='changed' WHERE review_id=?",
            (receipt["review_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "DELETE FROM reported_trajectory_station_halt_reviews "
            "WHERE review_id=?",
            (receipt["review_id"],),
        )


def test_additive_schema_migration_preserves_existing_observations() -> None:
    conn = _db()
    conn.execute("DROP TRIGGER reported_trajectory_halt_reviews_no_update")
    conn.execute("DROP TRIGGER reported_trajectory_halt_reviews_no_delete")
    conn.execute("DROP TABLE reported_trajectory_station_halt_reviews")
    _observation(
        conn,
        observation_id="existing-cor",
        valid_at="2026-07-28T03:15:00Z",
        correction=1,
    )

    status = apply_reported_trajectory_correction_review_schema(conn)

    assert status["ok"] is True
    assert status["performs_legacy_backfill"] is False
    assert conn.execute(
        "SELECT observation_id FROM reported_trajectory_observations"
    ).fetchone()[0] == "existing-cor"
    assert conn.execute(
        "SELECT COUNT(*) FROM reported_trajectory_station_halt_reviews"
    ).fetchone()[0] == 0
