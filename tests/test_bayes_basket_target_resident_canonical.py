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
from weather_basket_yes_schema import (
    apply_weather_basket_yes_schema,
)


def _source() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reported_trajectory_observations("
        "observation_id TEXT,source_lane TEXT,station_id TEXT,"
        "report_type TEXT,valid_at TEXT,reported_temperature_c REAL,"
        "raw_sha256 TEXT,raw_text TEXT,canonical_committed_at TEXT,"
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
    source_lane: str = "awc",
    station_id: str = "RKPK",
) -> None:
    conn.execute(
        "INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            observation_id,
            source_lane,
            station_id,
            report_type,
            valid_at,
            temperature_c,
            "a" * 64,
            f"{report_type} {station_id} 261500Z 18005KT CAVOK "
            f"{int(temperature_c):02d}/20 Q1008",
            committed_at,
            correction,
        ),
    )


def test_awc_formal_scope_reads_the_same_evidence_settlement_reads() -> None:
    """A sub-hourly routine METAR must not move P1's running high.

    This test used to assert the opposite -- that the :30 report was formal --
    and production disproved it: LFPB 2026-07-31 published 14:30Z/15:30Z
    reports at 29°C while the eligible :00 maximum settled at 28°C.  P1's
    running high crossed the settled truth, its anchor moved one bucket high,
    and one report further would have hard-excluded the bucket that settled
    true.  Settlement skips those reports; P1 must skip exactly the same ones.
    """

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
        observation_id="half-hour-reference-only",
        valid_at="2026-07-26T15:30:00Z",
        committed_at="2026-07-26T15:31:00Z",
        temperature_c=30,
    )
    # An effective SPECI keeps its official minute -- the timing rule is about
    # routine cadence, not about the minute itself.
    _insert(
        conn,
        observation_id="speci-keeps-minute",
        valid_at="2026-07-26T15:47:00Z",
        committed_at="2026-07-26T15:48:00Z",
        temperature_c=31,
        report_type="SPECI",
    )

    rows = load_resident_formal_observations(
        conn,
        registry_rows=_registry(),
        station_id="RKPK",
        event_date="2026-07-27",
        as_of_utc="2026-07-26T16:00:00Z",
    )

    assert [item["observation_id"] for item in rows] == [
        "hourly",
        "speci-keeps-minute",
    ]
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


def test_resident_adapter_rejects_retired_or_mixed_amsc_registry_lane() -> None:
    conn = _source()
    rows = [
        *_registry(),
        {**_registry()[0], "source_lane": "amsc"},
    ]
    with pytest.raises(
        ContractError,
        match="resident_p1_source_lane_not_awc",
    ):
        load_resident_formal_observations(
            conn,
            registry_rows=rows,
            station_id="RKPK",
            event_date="2026-07-27",
            as_of_utc="2026-07-26T16:00:00Z",
        )


def test_resident_adapter_rejects_retired_amsc_registry_lane() -> None:
    conn = _source()
    with pytest.raises(
        ContractError,
        match="resident_p1_source_lane_not_awc",
    ):
        load_resident_formal_observations(
            conn,
            registry_rows=[{
                **_registry()[0],
                "source_lane": "amsc",
            }],
            station_id="RKPK",
            event_date="2026-07-27",
            as_of_utc="2026-07-26T16:00:00Z",
        )
