from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target.resident_canonical import (
    load_resident_formal_observations,
)
from bayes_basket_target.correction_review import (
    CLEAR_REFERENCE_ONLY,
    append_correction_review,
)
from reported_trajectory_no_schema import (
    apply_reported_trajectory_no_schema,
)


def _source() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reported_trajectory_observations("
        "observation_id TEXT,source_lane TEXT,station_id TEXT,"
        "report_type TEXT,valid_at TEXT,reported_temperature_c REAL,"
        "raw_sha256 TEXT,canonical_committed_at TEXT,"
        "correction_suspected INTEGER)"
    )
    return conn


def _registry() -> list[dict]:
    return [{
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "source_lane": "awc",
    }]


def _insert(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    valid_at: str,
    committed_at: str,
    temperature_c: float,
    correction: int = 0,
    report_type: str = "METAR",
) -> None:
    conn.execute(
        "INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?,?,?)",
        (
            observation_id,
            "awc",
            "RKPK",
            report_type,
            valid_at,
            temperature_c,
            "a" * 64,
            committed_at,
            correction,
        ),
    )


def test_resident_adapter_uses_registry_window_and_only_hourly_truth() -> None:
    conn = _source()
    _insert(
        conn,
        observation_id="hourly",
        valid_at="2026-07-26T15:00:00Z",
        committed_at="2026-07-26T15:01:00Z",
        temperature_c=29,
    )
    _insert(
        conn,
        observation_id="half-hour-reference",
        valid_at="2026-07-26T15:30:00Z",
        committed_at="2026-07-26T15:31:00Z",
        temperature_c=30,
    )

    rows = load_resident_formal_observations(
        conn,
        registry_rows=_registry(),
        station_id="RKPK",
        event_date="2026-07-27",
        as_of_utc="2026-07-26T16:00:00Z",
    )

    assert [item["observation_id"] for item in rows] == ["hourly"]
    assert rows[0]["settlement_timezone"] == "Asia/Seoul"
    assert rows[0]["evidence_role"] == "FORMAL"


def test_resident_adapter_rejects_correction_in_scope() -> None:
    conn = _source()
    _insert(
        conn,
        observation_id="correction",
        valid_at="2026-07-26T15:00:00Z",
        committed_at="2026-07-26T15:01:00Z",
        temperature_c=29,
        correction=1,
    )
    with pytest.raises(
        ContractError,
        match="resident_p1_correction_requires_manual_review",
    ):
        load_resident_formal_observations(
            conn,
            registry_rows=_registry(),
            station_id="RKPK",
            event_date="2026-07-27",
            as_of_utc="2026-07-26T16:00:00Z",
        )


def test_non_hourly_cor_speci_halts_before_hourly_truth_filter() -> None:
    conn = _source()
    _insert(
        conn,
        observation_id="new-hourly-high",
        valid_at="2026-07-26T15:00:00Z",
        committed_at="2026-07-26T15:01:00Z",
        temperature_c=35,
    )
    _insert(
        conn,
        observation_id="cor-speci",
        valid_at="2026-07-26T15:15:00Z",
        committed_at="2026-07-26T15:16:00Z",
        temperature_c=34,
        correction=1,
        report_type="SPECI",
    )
    with pytest.raises(
        ContractError,
        match="resident_p1_correction_requires_manual_review",
    ):
        load_resident_formal_observations(
            conn,
            registry_rows=_registry(),
            station_id="RKPK",
            event_date="2026-07-27",
            as_of_utc="2026-07-26T16:00:00Z",
        )


def test_reviewed_nonhourly_cor_remains_reference_only_and_releases_p1() -> None:
    conn = sqlite3.connect(":memory:")
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
            "rkpk-high-2026-07-27", "2026-07-27", "35°C",
            "busan", "RKPK", "Asia/Seoul", "celsius", "awc", 1,
            "", "https://example.invalid", "a" * 64,
            "yes", "no", 1, 1, "{}", "2026-07-26T14:00:00Z",
        ),
    )
    values = [
        (
            "new-hourly-high", "METAR", "2026-07-26T15:00:00Z",
            35, "a" * 64, "METAR RKPK", 0,
        ),
        (
            "cor-speci", "SPECI", "2026-07-26T15:15:00Z",
            34, "b" * 64, "SPECI RKPK COR", 1,
        ),
    ]
    for observation_id, report_type, valid_at, temperature, raw_sha, raw, correction in values:
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
                observation_id, "awc", "RKPK", report_type, valid_at,
                temperature, raw_sha, raw,
                "2026-07-26T15:25:00Z",
                "2026-07-26T15:25:01Z",
                "2026-07-26T15:25:02Z",
                "2026-07-26T15:24:59Z",
                "2026-07-26T15:25:00Z",
                "poll-1", correction,
            ),
        )
    review = append_correction_review(
        conn,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-27",
        triggering_observation_id="cor-speci",
        decision=CLEAR_REFERENCE_ONLY,
        reviewer="operator-xiong",
        rationale="The corrected non-hourly SPECI is reference-only.",
        reviewed_at="2026-07-26T15:30:00Z",
    )

    rows = load_resident_formal_observations(
        conn,
        registry_rows=_registry(),
        station_id="RKPK",
        event_date="2026-07-27",
        as_of_utc="2026-07-27T16:00:00Z",
    )

    assert [item["observation_id"] for item in rows] == [
        "new-hourly-high"
    ]
    assert rows[0]["correction_review_ids"] == [review["review_id"]]


def test_resident_adapter_rejects_cross_lane_registry() -> None:
    conn = _source()
    rows = [
        *_registry(),
        {**_registry()[0], "source_lane": "amsc"},
    ]
    with pytest.raises(
        ContractError,
        match="resident_p1_source_lane_not_unique",
    ):
        load_resident_formal_observations(
            conn,
            registry_rows=rows,
            station_id="RKPK",
            event_date="2026-07-27",
            as_of_utc="2026-07-26T16:00:00Z",
        )
