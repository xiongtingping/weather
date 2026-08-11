from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target import delta_v2_settlement_label as label_module
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.delta_v2_settlement_label import (
    _bucket_intervals_for_envelope,
    materialize_delta_v2_settled_training_labels,
)
from bayes_basket_target.settlement_truth import (
    CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1,
    materialize_settled_station_truth,
    materialize_settled_training_truths,
)
from bayes_basket_target.correction_review import (
    CLEAR_REFERENCE_ONLY,
    append_correction_review,
)
from weather_basket_yes_schema import (
    apply_weather_basket_yes_schema,
)


def _target() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE basket_target_market_registry(
          station_id TEXT,event_date TEXT,settlement_timezone TEXT,
          source_lane TEXT,source_eligible INTEGER,
          snapshot_sha256 TEXT,refreshed_at TEXT
        );
        CREATE TABLE calibration_station_registry(
          station_id TEXT PRIMARY KEY,timezone_name TEXT
        );
        CREATE TABLE calibration_station_daily_high_truth(
          truth_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          local_date TEXT NOT NULL,
          settlement_timezone TEXT,
          official_tmax_c REAL NOT NULL,
          source_lane TEXT NOT NULL,
          settlement_definition TEXT NOT NULL,
          revision_no INTEGER NOT NULL,
          published_at_utc TEXT,
          received_at_utc TEXT,
          canonical_committed_at_utc TEXT,
          timestamps_complete INTEGER NOT NULL,
          qc_status TEXT NOT NULL,
          source_role TEXT NOT NULL,
          source_payload_sha256 TEXT NOT NULL,
          observation_ids_json TEXT NOT NULL,
          authority_scope TEXT NOT NULL,
          content_sha256 TEXT NOT NULL,
          UNIQUE(station_id,local_date,settlement_definition,revision_no)
        );
        CREATE TABLE canonical_ssot_fact(
          fact_id TEXT PRIMARY KEY,
          fact_type TEXT NOT NULL,
          scope_key TEXT NOT NULL,
          content_sha256 TEXT NOT NULL,
          fact_json TEXT NOT NULL,
          lineage_json TEXT NOT NULL,
          ingested_at_utc TEXT NOT NULL,
          UNIQUE(fact_type,content_sha256),
          UNIQUE(fact_type,scope_key)
        );
        CREATE TABLE basket_policy_training_fact(
          fact_id TEXT,station_id TEXT,event_date TEXT
        );
        """
    )
    # Deliberately stale: settlement truth must ignore this timezone.
    conn.execute(
        "INSERT INTO calibration_station_registry VALUES(?,?)",
        ("RKPK", "America/New_York"),
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-28", "Asia/Seoul", "awc", 1,
            "f" * 64, "2026-07-27T14:00:00Z",
        ),
    )
    return conn


def _label_target() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE basket_target_market_registry(
          station_id TEXT,event_date TEXT,settlement_timezone TEXT,
          source_lane TEXT,source_eligible INTEGER,
          snapshot_sha256 TEXT,refreshed_at TEXT
        );
        CREATE TABLE basket_policy_training_fact(
          fact_id TEXT,
          station_id TEXT,
          event_date TEXT,
          settlement_timezone TEXT,
          probability_envelope_id TEXT,
          policy_version_vector_json TEXT,
          feature_json TEXT,
          cycle_id TEXT,
          decision_at_utc TEXT,
          authority_scope TEXT,
          content_sha256 TEXT,
          posterior_sha256 TEXT,
          ws_snapshot_id TEXT,
          quote_sha256 TEXT,
          inventory_snapshot_id TEXT,
          inventory_sha256 TEXT,
          order_state_snapshot_id TEXT,
          order_state_sha256 TEXT,
          decision_id TEXT,
          decision_sha256 TEXT,
          plan_sha256 TEXT
        );
        CREATE TABLE calibration_station_daily_high_truth(
          truth_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          local_date TEXT NOT NULL,
          official_tmax_c REAL NOT NULL,
          source_lane TEXT NOT NULL,
          settlement_definition TEXT NOT NULL,
          revision_no INTEGER NOT NULL,
          published_at_utc TEXT,
          received_at_utc TEXT,
          canonical_committed_at_utc TEXT,
          timestamps_complete INTEGER NOT NULL,
          qc_status TEXT NOT NULL,
          source_role TEXT NOT NULL,
          source_payload_sha256 TEXT NOT NULL,
          observation_ids_json TEXT NOT NULL,
          authority_scope TEXT NOT NULL,
          content_sha256 TEXT NOT NULL,
          UNIQUE(station_id, local_date, settlement_definition, revision_no)
        );
        CREATE TABLE basket_probability_envelope(
          envelope_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          event_date TEXT NOT NULL,
          state TEXT NOT NULL,
          lineage_json TEXT NOT NULL
        );
        CREATE TABLE basket_policy_training_label(
          label_id TEXT PRIMARY KEY,
          fact_id TEXT NOT NULL,
          truth_id TEXT NOT NULL,
          truth_revision_no INTEGER NOT NULL,
          truth_sha256 TEXT NOT NULL,
          official_tmax_c REAL NOT NULL,
          winning_bucket TEXT NOT NULL,
          settlement_closed_at_utc TEXT NOT NULL,
          labeled_at_utc TEXT NOT NULL,
          outcome_json TEXT NOT NULL,
          authority_scope TEXT NOT NULL,
          supersedes_label_id TEXT,
          content_sha256 TEXT NOT NULL
        );
        CREATE TABLE basket_target_order_intents(
          order_intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
          cycle_id TEXT NOT NULL,
          bucket TEXT NOT NULL,
          requested_size REAL NOT NULL,
          requested_notional_usdc REAL NOT NULL
        );
        """
    )
    return conn


def _label_envelope(
    conn: sqlite3.Connection,
    *,
    envelope_id: str,
    station_id: str,
    event_date: str,
) -> None:
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?)",
        (
            envelope_id,
            station_id,
            event_date,
            "COMMITTED",
            json.dumps(
                {
                    "settlement_definition": "official_daily_tmax_c",
                    "bucket_intervals_c_json": json.dumps(
                        {
                            "35": [None, 35.5],
                            "36": [35.5, None],
                        }
                    ),
                }
            ),
        ),
    )


def test_label_bucket_intervals_resolve_from_content_addressed_p0_root() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "envelope_id TEXT,station_id TEXT,event_date TEXT,"
        "settlement_timezone TEXT,stage TEXT,state TEXT,"
        "lineage_json TEXT,probabilities_json TEXT)"
    )
    intervals = {"35": [None, 35.5], "36": [35.5, None]}
    probabilities = {"35": 0.4, "36": 0.6}
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?)",
        (
            "p0-root",
            "RKPK",
            "2026-07-29",
            "Asia/Seoul",
            "P0",
            "COMMITTED",
            json.dumps({
                "bucket_intervals_c_json": json.dumps(intervals),
            }),
            json.dumps(probabilities),
        ),
    )
    envelope = {
        "station_id": "RKPK",
        "event_date": "2026-07-29",
        "settlement_timezone": "Asia/Seoul",
        "probabilities_json": json.dumps(probabilities),
    }

    resolved = _bucket_intervals_for_envelope(
        conn,
        envelope=envelope,
        lineage={"p0_root_checkpoint_id": "p0-root"},
    )

    assert resolved == intervals


def _label_fact(
    conn: sqlite3.Connection,
    fact_id: str,
    *,
    station_id: str = "RKPK",
    event_date: str = "2026-07-29",
    settlement_timezone: str = "Asia/Seoul",
    envelope_id: str = "env-1",
    cycle_id: str = "cycle-1",
    authority_scope: str = "PRODUCTION_NO_LIVE_FORWARD",
    policy_schema: str = "delta_basket_score_policy_version_vector_v2",
    feature_schema: str = "delta_basket_score_forward_feature_vector_v2",
) -> None:
    conn.execute(
        "INSERT INTO basket_policy_training_fact("
        "fact_id,station_id,event_date,settlement_timezone,"
        "probability_envelope_id,policy_version_vector_json,feature_json,"
        "cycle_id,decision_at_utc,authority_scope,content_sha256,"
        "posterior_sha256,ws_snapshot_id,quote_sha256,"
        "inventory_snapshot_id,inventory_sha256,"
        "order_state_snapshot_id,order_state_sha256,"
        "decision_id,decision_sha256,plan_sha256"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fact_id,
            station_id,
            event_date,
            settlement_timezone,
            envelope_id,
            json.dumps({"schema": policy_schema}, sort_keys=True),
            json.dumps({"schema": feature_schema}, sort_keys=True),
            cycle_id,
            "2026-07-29T10:00:00Z",
            authority_scope,
            "a" * 64,
            "b" * 64,
            "ws-" + fact_id,
            "c" * 64,
            "inventory-" + fact_id,
            "d" * 64,
            "order-state-" + fact_id,
            "e" * 64,
            "decision-" + fact_id,
            "f" * 64,
            "0" * 64,
        ),
    )


def _label_truth(
    conn: sqlite3.Connection,
    truth_id: str,
    *,
    station_id: str,
    event_date: str,
    revision_no: int,
    official_tmax_c: float,
    committed_at: str,
    qc_status: str = "ACCEPTED",
    source_lane: str = "AWC",
    source_role: str = CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1,
) -> None:
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth(" \
        "truth_id,station_id,local_date,official_tmax_c,source_lane," \
        "settlement_definition,revision_no,published_at_utc,received_at_utc," \
        "canonical_committed_at_utc,timestamps_complete,qc_status,source_role," \
        "source_payload_sha256,observation_ids_json,authority_scope,content_sha256" \
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            truth_id,
            station_id,
            event_date,
            official_tmax_c,
            source_lane,
            "official_daily_tmax_c",
            revision_no,
            "2026-07-29T10:00:00Z",
            "2026-07-29T10:01:00Z",
            committed_at,
            1,
            qc_status,
            source_role,
            "a" * 64,
            "{}",
            "FORMAL_NATIVE_ELIGIBLE",
            content_sha256(
                {
                    "truth_id": truth_id,
                    "revision_no": revision_no,
                }
            ),
        ),
    )


def _label_intent(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    bucket: str,
    requested_size: float,
    requested_notional_usdc: float,
) -> None:
    conn.execute(
        "INSERT INTO basket_target_order_intents(" \
        "cycle_id,bucket,requested_size,requested_notional_usdc)"
        " VALUES(?,?,?,?)",
        (cycle_id, bucket, requested_size, requested_notional_usdc),
    )


def _canonical() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reported_trajectory_observations("
        "observation_id TEXT,source_lane TEXT,station_id TEXT,"
        "report_type TEXT,valid_at TEXT,reported_temperature_c REAL,"
        "raw_sha256 TEXT,canonical_committed_at TEXT,"
        "correction_suspected INTEGER)"
    )
    return conn


def _observation(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    valid_at: str,
    temperature_c: float,
    committed_at: str | None = None,
    correction: int = 0,
    report_type: str = "METAR",
    station_id: str = "RKPK",
) -> None:
    conn.execute(
        "INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?,?,?)",
        (
            observation_id,
            "awc",
            station_id,
            report_type,
            valid_at,
            temperature_c,
            observation_id[0].lower() * 64,
            committed_at or valid_at,
            correction,
        ),
    )


def test_open_registry_settlement_day_is_zero_write() -> None:
    target = _target()
    source = _canonical()
    _observation(
        source,
        observation_id="a-hourly",
        valid_at="2026-07-27T15:00:00Z",
        temperature_c=31,
        committed_at="2026-07-27T15:01:00Z",
    )

    result = materialize_settled_station_truth(
        target,
        source,
        station_id="RKPK",
        event_date="2026-07-28",
        as_of_utc="2026-07-28T14:59:59Z",
    )

    assert result["state"] == "skipped_open_settlement_day"
    assert result["settlement_timezone"] == "Asia/Seoul"
    assert target.execute(
        "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
    ).fetchone()[0] == 0


def test_settlement_truth_rejects_future_asof_before_any_write() -> None:
    target = _target()
    source = _canonical()

    with pytest.raises(
        ContractError,
        match="settlement_truth_as_of_utc_in_future",
    ):
        materialize_settled_station_truth(
            target,
            source,
            station_id="RKPK",
            event_date="2026-07-28",
            as_of_utc="2999-01-01T00:00:00Z",
        )

    assert target.execute(
        "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
    ).fetchone()[0] == 0


def test_closed_day_uses_registry_timezone_and_admits_every_routine_minute() -> None:
    target = _target()
    target.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-28", "America/New_York", "awc", 1,
            "e" * 64, "2026-07-29T00:00:00Z",
        ),
    )
    source = _canonical()
    _observation(
        source,
        observation_id="a-hourly",
        valid_at="2026-07-27T15:00:00Z",
        temperature_c=31,
        committed_at="2026-07-27T15:01:00Z",
    )
    _observation(
        source,
        observation_id="b-hourly",
        valid_at="2026-07-28T03:00:00Z",
        temperature_c=30,
        committed_at="2026-07-28T03:01:00Z",
    )
    _observation(
        source,
        observation_id="b-half",
        valid_at="2026-07-28T03:30:00Z",
        temperature_c=39,
        committed_at="2026-07-28T03:31:00Z",
    )
    _observation(
        source,
        observation_id="c-hourly",
        valid_at="2026-07-28T04:00:00Z",
        temperature_c=35,
        committed_at="2026-07-28T04:05:00Z",
        report_type="SPECI",
    )

    result = materialize_settled_station_truth(
        target,
        source,
        station_id="RKPK",
        event_date="2026-07-28",
        as_of_utc="2026-07-28T19:00:00Z",
    )

    assert result["state"] == "staged_in_caller_transaction"
    assert result["settlement_timezone"] == "Asia/Seoul"
    # Every routine minute is formal, so the hot 03:30Z report sets tmax.
    assert result["official_tmax_c"] == 39
    row = target.execute(
        "SELECT official_tmax_c,observation_ids_json "
        "FROM calibration_station_daily_high_truth"
    ).fetchone()
    assert row[0] == 39
    assert "b-half" in row[1]
    assert '"settlement_timezone":"Asia/Seoul"' in row[1]
    assert f'"registry_snapshot_sha256":"{"f" * 64}"' in row[1]


def test_closed_day_yields_truth_for_a_cadence_that_never_lands_on_the_hour() -> None:
    """:20/:50-style stations settle from their own routine reports.

    This is the EDDM/EFHK/EGLC/EHAM/LIMC/LTAC/KBKF case plus the ten United
    States stations, all of which settled to nothing while the contract
    required a literal :00.
    """

    target = _target()
    target.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-28", "America/New_York", "awc", 1,
            "e" * 64, "2026-07-29T00:00:00Z",
        ),
    )
    source = _canonical()
    _observation(
        source,
        observation_id="a-twenty",
        valid_at="2026-07-28T03:20:00Z",
        temperature_c=39,
        committed_at="2026-07-28T03:21:00Z",
    )
    _observation(
        source,
        observation_id="0warmfill",
        valid_at="2026-07-28T05:00:00Z",
        temperature_c=10,
    )
    _observation(
        source,
        observation_id="1warmfill",
        valid_at="2026-07-28T06:00:00Z",
        temperature_c=10,
    )
    _observation(
        source,
        observation_id="2warmfill",
        valid_at="2026-07-28T07:00:00Z",
        temperature_c=10,
    )
    _observation(
        source,
        observation_id="b-fifty",
        valid_at="2026-07-28T03:50:00Z",
        temperature_c=33,
        committed_at="2026-07-28T03:51:00Z",
    )

    result = materialize_settled_station_truth(
        target,
        source,
        station_id="RKPK",
        event_date="2026-07-28",
        as_of_utc="2026-07-28T19:00:00Z",
    )

    assert result["official_tmax_c"] == 39
    row = target.execute(
        "SELECT official_tmax_c,observation_ids_json "
        "FROM calibration_station_daily_high_truth"
    ).fetchone()
    assert row[0] == 39
    assert "a-twenty" in row[1]
    assert "b-fifty" in row[1]


def test_truth_writer_excludes_amsc_and_legacy_role_revisions() -> None:
    target = _target()
    _label_truth(
        target,
        "truth-legacy-amsc",
        station_id="RKPK",
        event_date="2026-07-28",
        revision_no=9,
        official_tmax_c=39.0,
        committed_at="2026-07-28T14:00:00Z",
        source_lane="AMSC",
        source_role=CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1,
    )
    _label_truth(
        target,
        "truth-awc-legacy-role",
        station_id="RKPK",
        event_date="2026-07-28",
        revision_no=8,
        official_tmax_c=38.0,
        committed_at="2026-07-28T14:00:00Z",
        source_role="CANONICAL_AMSC_AWC_INGRESS",
    )
    source = _canonical()
    _observation(
        source,
        observation_id="a-awc-hourly",
        valid_at="2026-07-28T03:00:00Z",
        temperature_c=35.0,
        committed_at="2026-07-28T03:01:00Z",
    )
    _observation(
        source,
        observation_id="0warmfill",
        valid_at="2026-07-28T05:00:00Z",
        temperature_c=10,
    )
    _observation(
        source,
        observation_id="1warmfill",
        valid_at="2026-07-28T06:00:00Z",
        temperature_c=10,
    )
    _observation(
        source,
        observation_id="2warmfill",
        valid_at="2026-07-28T07:00:00Z",
        temperature_c=10,
    )

    result = materialize_settled_station_truth(
        target,
        source,
        station_id="RKPK",
        event_date="2026-07-28",
        as_of_utc="2026-07-28T15:00:00Z",
    )

    assert result["state"] == "staged_in_caller_transaction"
    assert result["revision_no"] == 0
    assert target.execute(
        "SELECT source_lane,source_role FROM "
        "calibration_station_daily_high_truth WHERE truth_id=?",
        (result["truth_id"],),
    ).fetchone() == (
        "AWC",
        CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1,
    )


def test_wsss_truth_admits_half_hour_awc_metar_into_the_daily_maximum() -> None:
    # Deliberate policy decision (2026-08-10): every routine report at the
    # provider's official minute is formal, including half-hour extras at
    # :00/:30 hubs.  This reverses the earlier reading of the WSSS 2026-07-30
    # case, where the 05:30Z=31C report had been treated as reference-only.
    target = _target()
    target.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "WSSS", "2026-07-30", "Asia/Singapore", "awc", 1,
            "e" * 64, "2026-07-29T00:00:00Z",
        ),
    )
    source = _canonical()
    _observation(
        source,
        observation_id="e-wsss-0200",
        station_id="WSSS",
        valid_at="2026-07-30T02:00:00Z",
        temperature_c=30,
        committed_at="2026-07-30T02:01:00Z",
    )
    _observation(
        source,
        observation_id="f-wsss-0500",
        station_id="WSSS",
        valid_at="2026-07-30T05:00:00Z",
        temperature_c=30,
        committed_at="2026-07-30T05:01:00Z",
    )
    _observation(
        source,
        observation_id="a-wsss-0530",
        station_id="WSSS",
        valid_at="2026-07-30T05:30:00Z",
        temperature_c=31,
        committed_at="2026-07-30T05:31:00Z",
    )

    result = materialize_settled_station_truth(
        target,
        source,
        station_id="WSSS",
        event_date="2026-07-30",
        as_of_utc="2026-07-30T17:00:00Z",
    )

    assert result["state"] == "staged_in_caller_transaction"
    assert result["official_tmax_c"] == 31
    lineage = json.loads(target.execute(
        "SELECT observation_ids_json FROM calibration_station_daily_high_truth"
    ).fetchone()[0])
    assert [item["observation_id"] for item in lineage["observations"]] == [
        "e-wsss-0200",
        "f-wsss-0500",
        "a-wsss-0530",
    ]


def test_nonhourly_corrected_speci_blocks_entire_station_day() -> None:
    target = _target()
    source = _canonical()
    _observation(
        source,
        observation_id="a-hourly",
        valid_at="2026-07-28T03:00:00Z",
        temperature_c=35,
        committed_at="2026-07-28T03:05:00Z",
    )
    _observation(
        source,
        observation_id="b-cor",
        valid_at="2026-07-28T03:15:00Z",
        temperature_c=34,
        committed_at="2026-07-28T03:25:00Z",
        correction=1,
        report_type="SPECI",
    )

    with pytest.raises(
        ContractError,
        match="settlement_truth_correction_requires_manual_review",
    ):
        materialize_settled_station_truth(
            target,
            source,
            station_id="RKPK",
            event_date="2026-07-28",
            as_of_utc="2026-07-28T15:00:00Z",
        )
    assert target.execute(
        "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
    ).fetchone()[0] == 0


def test_conflicting_hourly_revision_is_zero_write() -> None:
    target = _target()
    source = _canonical()
    _observation(
        source,
        observation_id="a-first",
        valid_at="2026-07-28T03:00:00Z",
        temperature_c=34,
        committed_at="2026-07-28T03:05:00Z",
    )
    _observation(
        source,
        observation_id="b-conflict",
        valid_at="2026-07-28T03:00:00Z",
        temperature_c=35,
        committed_at="2026-07-28T03:06:00Z",
    )

    with pytest.raises(
        ContractError,
        match="settlement_truth_hourly_revision_ambiguous",
    ):
        materialize_settled_station_truth(
            target,
            source,
            station_id="RKPK",
            event_date="2026-07-28",
            as_of_utc="2026-07-28T15:00:00Z",
        )
    assert target.execute(
        "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
    ).fetchone()[0] == 0


def test_identical_replay_is_idempotent_and_late_hourly_observation_revises() -> None:
    target = _target()
    source = _canonical()
    _observation(
        source,
        observation_id="a-hourly",
        valid_at="2026-07-28T03:00:00Z",
        temperature_c=34,
        committed_at="2026-07-28T03:05:00Z",
    )
    _observation(
        source,
        observation_id="0warmfill",
        valid_at="2026-07-28T05:00:00Z",
        temperature_c=10,
    )
    _observation(
        source,
        observation_id="1warmfill",
        valid_at="2026-07-28T06:00:00Z",
        temperature_c=10,
    )
    _observation(
        source,
        observation_id="2warmfill",
        valid_at="2026-07-28T07:00:00Z",
        temperature_c=10,
    )
    first = materialize_settled_station_truth(
        target,
        source,
        station_id="RKPK",
        event_date="2026-07-28",
        as_of_utc="2026-07-28T15:00:00Z",
    )
    same = materialize_settled_station_truth(
        target,
        source,
        station_id="RKPK",
        event_date="2026-07-28",
        as_of_utc="2026-07-28T15:00:00Z",
    )
    _observation(
        source,
        observation_id="b-late",
        valid_at="2026-07-28T04:00:00Z",
        temperature_c=35,
        committed_at="2026-07-28T15:05:00Z",
    )
    revised = materialize_settled_station_truth(
        target,
        source,
        station_id="RKPK",
        event_date="2026-07-28",
        as_of_utc="2026-07-28T15:06:00Z",
    )

    assert first["revision_no"] == 0
    assert same["state"] == "idempotent"
    assert revised["revision_no"] == 1
    assert revised["official_tmax_c"] == 35
    assert target.execute(
        "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
    ).fetchone()[0] == 2


def test_batch_returns_correction_blocker_without_truth_write() -> None:
    target = _target()
    target.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?)",
        ("fact-1", "RKPK", "2026-07-28"),
    )
    source = _canonical()
    _observation(
        source,
        observation_id="a-cor",
        valid_at="2026-07-28T03:15:00Z",
        temperature_c=34,
        committed_at="2026-07-28T03:25:00Z",
        correction=1,
        report_type="SPECI",
    )

    result = materialize_settled_training_truths(
        target,
        source,
        as_of_utc="2026-07-28T15:00:00Z",
    )

    assert result["state"] == "blocked_fail_closed"
    assert "settlement_truth_correction_requires_manual_review" in (
        result["blockers"][0]["reason"]
    )
    assert target.execute(
        "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
    ).fetchone()[0] == 0


def test_truth_batch_pages_more_than_45_scopes_without_global_block() -> None:
    target = _target()
    source = _canonical()
    for index in range(46):
        target.execute(
            "INSERT INTO basket_policy_training_fact VALUES(?,?,?)",
            (
                f"fact-{index:02d}",
                f"K{index:03d}",
                "2026-07-29",
            ),
        )
        target.execute(
            "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
            (
                f"K{index:03d}", "2026-07-29", "Asia/Seoul", "awc",
                1, f"{index + 1:064x}", "2026-07-28T14:00:00Z",
            ),
        )

    first = materialize_settled_training_truths(
        target,
        source,
        as_of_utc="2026-07-29T14:00:00Z",
        max_scopes=45,
    )
    second = materialize_settled_training_truths(
        target,
        source,
        as_of_utc="2026-07-29T14:00:00Z",
        max_scopes=45,
        after_fact_rowid=first["source_cursor_after"],
    )

    assert first["state"] == "complete"
    assert first["scope_count"] == 45
    assert first["has_more"] is True
    assert first["cursor_wrapped"] is False
    assert second["scope_count"] == 1
    assert second["has_more"] is False
    assert second["cursor_wrapped"] is False
    assert second["source_cursor_after"] > first["source_cursor_after"]


def test_truth_batch_bootstraps_scope_before_basket_training_fact() -> None:
    target = _target()
    source = _canonical()
    _observation(
        source,
        observation_id="bootstrap-00",
        valid_at="2026-07-28T00:00:00Z",
        temperature_c=31,
        committed_at="2026-07-28T00:00:10Z",
    )
    _observation(
        source,
        observation_id="0warmfill",
        valid_at="2026-07-28T05:00:00Z",
        temperature_c=10,
    )
    _observation(
        source,
        observation_id="1warmfill",
        valid_at="2026-07-28T06:00:00Z",
        temperature_c=10,
    )
    _observation(
        source,
        observation_id="2warmfill",
        valid_at="2026-07-28T07:00:00Z",
        temperature_c=10,
    )

    result = materialize_settled_training_truths(
        target,
        source,
        as_of_utc="2026-07-28T15:00:00Z",
        additional_scopes=[("RKPK", "2026-07-28")],
    )

    assert result["state"] == "complete"
    assert result["scope_count"] == 1
    assert result["additional_scope_count"] == 1
    assert result["training_fact_scope_count"] == 0
    assert result["written_count"] == 1
    assert target.execute(
        "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
    ).fetchone()[0] == 1


def test_truth_batch_commits_unrelated_scope_when_one_station_is_halted() -> None:
    target = _target()
    source = _canonical()
    target.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "VILK", "2026-07-28", "Asia/Kolkata", "awc", 1,
            "e" * 64, "2026-07-27T14:00:00Z",
        ),
    )
    _observation(
        source,
        observation_id="a-good-hourly",
        valid_at="2026-07-28T00:00:00Z",
        temperature_c=31,
        committed_at="2026-07-28T00:00:10Z",
    )
    _observation(
        source,
        observation_id="0warmfill",
        valid_at="2026-07-28T05:00:00Z",
        temperature_c=10,
    )
    _observation(
        source,
        observation_id="1warmfill",
        valid_at="2026-07-28T06:00:00Z",
        temperature_c=10,
    )
    _observation(
        source,
        observation_id="2warmfill",
        valid_at="2026-07-28T07:00:00Z",
        temperature_c=10,
    )
    source.execute(
        "INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "halted-hourly",
            "awc",
            "VILK",
            "METAR",
            "2026-07-28T00:00:00Z",
            32,
            "d" * 64,
            "2026-07-28T00:00:10Z",
            1,
        ),
    )

    result = materialize_settled_training_truths(
        target,
        source,
        as_of_utc="2026-07-28T19:00:00Z",
        additional_scopes=[
            ("RKPK", "2026-07-28"),
            ("VILK", "2026-07-28"),
        ],
    )

    assert result["state"] == "partial_fail_closed"
    assert result["written_count"] == 1
    assert result["blockers"][0]["station_id"] == "VILK"
    assert target.execute(
        "SELECT station_id FROM calibration_station_daily_high_truth"
    ).fetchall() == [("RKPK",)]


def test_truth_batch_cursor_wraps_and_revisits_old_scope() -> None:
    target = _target()
    source = _canonical()
    target.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?)",
        ("fact-1", "RKPK", "2026-07-28"),
    )
    first = materialize_settled_training_truths(
        target,
        source,
        as_of_utc="2026-07-28T14:00:00Z",
    )
    wrapped = materialize_settled_training_truths(
        target,
        source,
        as_of_utc="2026-07-28T14:00:00Z",
        after_fact_rowid=first["source_cursor_after"],
    )

    assert first["scope_count"] == 1
    assert wrapped["scope_count"] == 1
    assert wrapped["cursor_wrapped"] is True
    assert wrapped["source_cursor_after"] == first["source_cursor_after"]


def test_truth_batch_uses_latest_fact_rowid_once_per_scope_per_wrap() -> None:
    target = _target()
    source = _canonical()
    target.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?)",
        ("fact-a-1", "AAAA", "2026-07-29"),
    )
    for index in range(45):
        target.execute(
            "INSERT INTO basket_policy_training_fact VALUES(?,?,?)",
            (
                f"fact-{index:02d}",
                f"K{index:03d}",
                "2026-07-29",
            ),
        )
    target.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?)",
        ("fact-a-2", "AAAA", "2026-07-29"),
    )
    target.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?)",
        ("fact-a-3", "AAAA", "2026-07-29"),
    )

    first = materialize_settled_training_truths(
        target,
        source,
        as_of_utc="2026-07-29T14:00:00Z",
        max_scopes=45,
    )
    second = materialize_settled_training_truths(
        target,
        source,
        as_of_utc="2026-07-29T14:00:00Z",
        max_scopes=45,
        after_fact_rowid=first["source_cursor_after"],
    )

    first_scopes = {
        (item["station_id"], item["event_date"])
        for item in first["results"] + first["blockers"]
    }
    second_scopes = {
        (item["station_id"], item["event_date"])
        for item in second["results"] + second["blockers"]
    }
    assert first_scopes.isdisjoint(second_scopes)
    assert len(first_scopes | second_scopes) == 46


def test_truth_batch_rejects_invalid_cursor() -> None:
    target = _target()
    source = _canonical()
    with pytest.raises(
        ContractError,
        match="settlement_truth_cursor_invalid",
    ):
        materialize_settled_training_truths(
            target,
            source,
            as_of_utc="2026-07-28T14:00:00Z",
            after_fact_rowid=-1,
        )


def test_reviewed_nonhourly_correction_allows_hourly_truth_with_review_lineage() -> None:
    target = _target()
    source = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(source)
    source.execute(
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
    for values in (
        (
            "hourly-35", "METAR", "2026-07-28T03:00:00Z",
            35, "a" * 64, "METAR RKPK", 0,
        ),
        (
            "warmfill-0130", "METAR", "2026-07-28T01:30:00Z",
            10, "c" * 64, "METAR RKPK WF0", 0,
        ),
        (
            "warmfill-0230", "METAR", "2026-07-28T02:30:00Z",
            10, "d" * 64, "METAR RKPK WF1", 0,
        ),
        (
            "cor-0315", "SPECI", "2026-07-28T03:15:00Z",
            34, "b" * 64, "SPECI RKPK COR", 1,
        ),
    ):
        observation_id, report_type, valid_at, temperature, raw_sha, raw, correction = values
        source.execute(
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
                "2026-07-28T03:25:00Z",
                "2026-07-28T03:25:01Z",
                "2026-07-28T03:25:02Z",
                "2026-07-28T03:24:59Z",
                "2026-07-28T03:25:00Z",
                "poll-1", correction,
            ),
        )
    review = append_correction_review(
        source,
        source_lane="awc",
        station_id="RKPK",
        event_date="2026-07-28",
        triggering_observation_id="cor-0315",
        decision=CLEAR_REFERENCE_ONLY,
        reviewer="operator-xiong",
        rationale="The corrected non-hourly SPECI remains reference-only.",
        reviewed_at="2026-07-28T04:30:00Z",
    )

    result = materialize_settled_station_truth(
        target,
        source,
        station_id="RKPK",
        event_date="2026-07-28",
        as_of_utc="2026-07-28T15:00:00Z",
    )

    assert result["state"] == "staged_in_caller_transaction"
    assert result["official_tmax_c"] == 35
    lineage = target.execute(
        "SELECT observation_ids_json "
        "FROM calibration_station_daily_high_truth"
    ).fetchone()[0]
    assert review["review_id"] in lineage
    assert "cor-0315" not in [
        item["observation_id"]
        for item in json.loads(lineage)["observations"]
    ]


def test_label_formal_truth_selection_requires_awc_and_new_ingress_role() -> None:
    conn = _label_target()
    _label_envelope(
        conn,
        envelope_id="env-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-29", "Asia/Seoul", "awc", 1,
            "f" * 64, "2026-07-28T14:00:00Z",
        ),
    )
    _label_fact(
        conn,
        fact_id="d2fact_rkpk",
        cycle_id="cycle-rkpk",
        envelope_id="env-rkpk",
    )
    _label_intent(
        conn,
        cycle_id="cycle-rkpk",
        bucket="35",
        requested_size=1.0,
        requested_notional_usdc=0.5,
    )
    _label_truth(
        conn,
        "truth-amsc-high-revision",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=9,
        official_tmax_c=36.0,
        committed_at="2026-07-29T15:00:00Z",
        source_lane="AMSC",
    )
    _label_truth(
        conn,
        "truth-awc-legacy-role",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=8,
        official_tmax_c=36.0,
        committed_at="2026-07-29T15:00:00Z",
        source_role="CANONICAL_AMSC_AWC_INGRESS",
    )
    _label_truth(
        conn,
        "truth-awc-canonical",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=0,
        official_tmax_c=35.0,
        committed_at="2026-07-29T15:00:00Z",
    )

    result = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:01:00Z",
    )

    assert result["state"] == "committed"
    assert result["appended_count"] == 1
    assert conn.execute(
        "SELECT truth_id,truth_revision_no FROM "
        "basket_policy_training_label"
    ).fetchone() == ("truth-awc-canonical", 0)


def test_label_formal_truth_selection_fails_closed_without_canonical_awc_truth() -> None:
    conn = _label_target()
    _label_envelope(
        conn,
        envelope_id="env-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-29", "Asia/Seoul", "awc", 1,
            "f" * 64, "2026-07-28T14:00:00Z",
        ),
    )
    _label_fact(
        conn,
        fact_id="d2fact_rkpk",
        cycle_id="cycle-rkpk",
        envelope_id="env-rkpk",
    )
    _label_intent(
        conn,
        cycle_id="cycle-rkpk",
        bucket="35",
        requested_size=1.0,
        requested_notional_usdc=0.5,
    )
    _label_truth(
        conn,
        "truth-amsc",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=9,
        official_tmax_c=36.0,
        committed_at="2026-07-29T15:00:00Z",
        source_lane="AMSC",
    )
    _label_truth(
        conn,
        "truth-awc-legacy-role",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=8,
        official_tmax_c=36.0,
        committed_at="2026-07-29T15:00:00Z",
        source_role="CANONICAL_AMSC_AWC_INGRESS",
    )

    result = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:01:00Z",
    )

    assert result["state"] == "no_new_final_labels"
    assert result["appended_count"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_policy_training_label"
    ).fetchone()[0] == 0


def test_label_keyset_pages_fixed_rkpk_and_day_cursor_wraps() -> None:
    conn = _label_target()
    _label_envelope(
        conn,
        envelope_id="env-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-29", "Asia/Seoul", "amsc", 1,
            "f" * 64, "2026-07-28T14:00:00Z",
        ),
    )
    for index in range(3):
        _label_fact(
            conn,
            fact_id=f"d2fact_rkpk_{index}",
            cycle_id=f"cycle-rkpk-{index}",
            envelope_id="env-rkpk",
        )
        _label_intent(
            conn,
            cycle_id=f"cycle-rkpk-{index}",
            bucket="35",
            requested_size=1.0,
            requested_notional_usdc=0.5,
        )

    _label_truth(
        conn,
        "truth-accepted-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=0,
        official_tmax_c=35.0,
        committed_at="2026-07-29T15:00:00Z",
    )

    first = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:01:00Z",
        max_facts=2,
    )
    second = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:01:00Z",
        max_facts=2,
        after_fact_rowid=first["source_cursor_after"],
    )
    third = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:01:00Z",
        max_facts=2,
        after_fact_rowid=second["source_cursor_after"],
    )

    assert first["state"] == "committed"
    assert first["appended_count"] == 2
    assert first["has_more"] is True
    assert first["source_cursor_after"] == 2
    assert first["cursor_wrapped"] is False

    assert second["state"] == "committed"
    assert second["appended_count"] == 1
    assert second["has_more"] is False
    assert second["source_cursor_after"] == 3
    assert second["cursor_wrapped"] is False

    assert third["state"] == "no_new_final_labels"
    assert third["appended_count"] == 0
    assert third["cursor_wrapped"] is True


def test_label_materialization_keeps_partial_success_when_one_fact_breaks() -> None:
    conn = _label_target()
    _label_envelope(
        conn,
        envelope_id="env-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-29", "Asia/Seoul", "amsc", 1,
            "f" * 64, "2026-07-28T14:00:00Z",
        ),
    )
    _label_fact(
        conn,
        "d2fact_good",
        cycle_id="cycle-good",
        envelope_id="env-rkpk",
    )
    _label_fact(
        conn,
        "d2fact_bad",
        cycle_id="cycle-bad",
        envelope_id="env-rkpk",
        policy_schema="bad-schema",
    )
    _label_intent(
        conn,
        cycle_id="cycle-good",
        bucket="35",
        requested_size=1.0,
        requested_notional_usdc=0.5,
    )
    _label_intent(
        conn,
        cycle_id="cycle-bad",
        bucket="35",
        requested_size=1.0,
        requested_notional_usdc=0.5,
    )
    _label_truth(
        conn,
        "truth-accepted-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=0,
        official_tmax_c=35.0,
        committed_at="2026-07-29T15:00:00Z",
    )

    result = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:01:00Z",
        max_facts=2,
    )

    assert result["state"] == "blocked_fail_closed"
    assert result["appended_count"] == 1
    assert len(result["blockers"]) == 1
    assert "d2fact_bad" in result["blockers"][0]["fact_id"]
    assert "delta_v2_label_policy_vector_schema_invalid" in (
        result["blockers"][0]["reason"]
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_policy_training_label"
    ).fetchone()[0] == 1


def test_label_retry_lane_revisits_bad_low_row_while_high_water_advances() -> None:
    conn = _label_target()
    _label_envelope(
        conn,
        envelope_id="env-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-29", "Asia/Seoul", "amsc", 1,
            "f" * 64, "2026-07-28T14:00:00Z",
        ),
    )
    _label_fact(
        conn,
        "d2fact_bad_low",
        cycle_id="cycle-bad-low",
        envelope_id="env-rkpk",
        policy_schema="bad-schema",
    )
    _label_fact(
        conn,
        "d2fact_good_middle",
        cycle_id="cycle-good-middle",
        envelope_id="env-rkpk",
    )
    for cycle_id in ("cycle-bad-low", "cycle-good-middle"):
        _label_intent(
            conn,
            cycle_id=cycle_id,
            bucket="35",
            requested_size=1.0,
            requested_notional_usdc=0.5,
        )
    _label_truth(
        conn,
        "truth-accepted-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=0,
        official_tmax_c=35.0,
        committed_at="2026-07-29T15:00:00Z",
    )

    first = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:01:00Z",
        max_facts=2,
    )
    assert first["source_cursor_after"] == 2
    assert first["appended_count"] == 1

    conn.execute(
        "UPDATE basket_policy_training_fact "
        "SET policy_version_vector_json=? WHERE fact_id=?",
        (
            json.dumps({
                "schema": "delta_basket_score_policy_version_vector_v2"
            }),
            "d2fact_bad_low",
        ),
    )
    _label_fact(
        conn,
        "d2fact_good_high",
        cycle_id="cycle-good-high",
        envelope_id="env-rkpk",
    )
    _label_intent(
        conn,
        cycle_id="cycle-good-high",
        bucket="35",
        requested_size=1.0,
        requested_notional_usdc=0.5,
    )

    repaired = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:02:00Z",
        after_fact_rowid=first["source_cursor_after"],
        max_facts=2,
    )

    assert repaired["state"] == "committed"
    assert repaired["retry_candidate_count"] == 1
    assert repaired["new_candidate_count"] == 1
    assert repaired["source_cursor_after"] == 3
    assert repaired["appended_count"] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_policy_training_label"
    ).fetchone()[0] == 3


def test_label_retry_cursor_rotates_beyond_first_retry_page() -> None:
    conn = _label_target()
    _label_envelope(
        conn,
        envelope_id="env-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-29", "Asia/Seoul", "amsc", 1,
            "f" * 64, "2026-07-28T14:00:00Z",
        ),
    )
    for index in range(34):
        _label_fact(
            conn,
            f"d2fact_bad_{index:02d}",
            cycle_id=f"cycle-bad-{index:02d}",
            envelope_id="env-rkpk",
            policy_schema="bad-schema",
        )
    _label_truth(
        conn,
        "truth-accepted-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=0,
        official_tmax_c=35.0,
        committed_at="2026-07-29T15:00:00Z",
    )
    first = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:01:00Z",
        max_facts=34,
    )
    cursor = first["source_cursor_after"]
    cursor_state = first["cursor_state_after"]
    retried: set[str] = set()

    for _ in range(18):
        page = materialize_delta_v2_settled_training_labels(
            conn,
            as_of_utc="2026-07-29T15:02:00Z",
            after_fact_rowid=cursor,
            cursor_state=cursor_state,
            max_facts=4,
        )
        retried.update(item["fact_id"] for item in page["blockers"])
        cursor = page["source_cursor_after"]
        cursor_state = page["cursor_state_after"]

    assert retried == {
        f"d2fact_bad_{index:02d}" for index in range(34)
    }


def test_label_source_page_bytes_fail_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _label_target()
    _label_envelope(
        conn,
        envelope_id="env-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-29", "Asia/Seoul", "amsc", 1,
            "f" * 64, "2026-07-28T14:00:00Z",
        ),
    )
    for index in range(2):
        _label_fact(
            conn,
            f"d2fact_page_{index}",
            cycle_id=f"cycle-page-{index}",
            envelope_id="env-rkpk",
        )
        _label_intent(
            conn,
            cycle_id=f"cycle-page-{index}",
            bucket="35",
            requested_size=1.0,
            requested_notional_usdc=0.5,
        )
    _label_truth(
        conn,
        "truth-accepted-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=0,
        official_tmax_c=35.0,
        committed_at="2026-07-29T15:00:00Z",
    )
    monkeypatch.setattr(
        label_module,
        "MAX_LABEL_PAGE_SOURCE_JSON_BYTES",
        1,
    )
    changes_before = conn.total_changes

    result = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:01:00Z",
        max_facts=2,
    )

    assert conn.total_changes == changes_before
    assert result["state"] == "blocked_fail_closed"
    assert result["resource_blocker_count"] == 2
    assert result["appended_count"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_policy_training_label"
    ).fetchone()[0] == 0


def test_label_single_row_batch_persists_lane_rotation() -> None:
    conn = _label_target()
    _label_envelope(
        conn,
        envelope_id="env-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-29", "Asia/Seoul", "amsc", 1,
            "f" * 64, "2026-07-28T14:00:00Z",
        ),
    )
    _label_fact(
        conn,
        "d2fact_bad_single",
        cycle_id="cycle-bad-single",
        envelope_id="env-rkpk",
        policy_schema="bad-schema",
    )
    _label_truth(
        conn,
        "truth-accepted-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=0,
        official_tmax_c=35.0,
        committed_at="2026-07-29T15:00:00Z",
    )
    first = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:01:00Z",
        max_facts=1,
    )
    _label_fact(
        conn,
        "d2fact_good_single",
        cycle_id="cycle-good-single",
        envelope_id="env-rkpk",
    )
    second = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:02:00Z",
        after_fact_rowid=first["source_cursor_after"],
        cursor_state=first["cursor_state_after"],
        max_facts=1,
    )
    third = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:03:00Z",
        after_fact_rowid=second["source_cursor_after"],
        cursor_state=second["cursor_state_after"],
        max_facts=1,
    )

    assert first["blockers"][0]["fact_id"] == "d2fact_bad_single"
    assert second["appended_count"] == 1
    assert second["source_cursor_after"] == 2
    assert third["blockers"][0]["fact_id"] == "d2fact_bad_single"


def test_label_late_revision_updates_superceded_label_and_quarantine_blocks() -> None:
    conn = _label_target()
    _label_envelope(
        conn,
        envelope_id="env-rkpk",
        station_id="RKPK",
        event_date="2026-07-29",
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
        (
            "RKPK", "2026-07-29", "Asia/Seoul", "amsc", 1,
            "f" * 64, "2026-07-28T14:00:00Z",
        ),
    )
    _label_fact(
        conn,
        "d2fact_rkpk",
        cycle_id="cycle-rkpk",
        envelope_id="env-rkpk",
    )
    _label_intent(
        conn,
        cycle_id="cycle-rkpk",
        bucket="35",
        requested_size=1.0,
        requested_notional_usdc=0.5,
    )
    _label_truth(
        conn,
        "truth-r0",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=0,
        official_tmax_c=35.0,
        committed_at="2026-07-29T15:00:00Z",
        qc_status="ACCEPTED",
    )

    first = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:01:00Z",
    )
    late = conn.execute(
        "SELECT truth_revision_no,supersedes_label_id FROM "
        "basket_policy_training_label ORDER BY rowid"
    ).fetchall()

    _label_truth(
        conn,
        "truth-r1",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=1,
        official_tmax_c=36.0,
        committed_at="2026-07-29T15:30:00Z",
        qc_status="ACCEPTED",
    )
    second = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:40:00Z",
    )

    _label_truth(
        conn,
        "truth-r2",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=2,
        official_tmax_c=37.0,
        committed_at="2026-07-29T15:50:00Z",
        qc_status="QUARANTINED",
    )
    quarantined = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T15:55:00Z",
    )

    _label_truth(
        conn,
        "truth-r3",
        station_id="RKPK",
        event_date="2026-07-29",
        revision_no=3,
        official_tmax_c=36.5,
        committed_at="2026-07-29T16:00:00Z",
        qc_status="ACCEPTED",
    )
    accepted = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-29T16:01:00Z",
    )

    assert first["state"] == "committed"
    assert first["appended_count"] == 1
    assert len(late) == 1
    assert late[0][0] == 0

    assert second["state"] == "committed"
    assert second["appended_count"] == 1

    assert quarantined["state"] == "no_new_final_labels"
    assert quarantined["appended_count"] == 0

    assert accepted["state"] == "committed"
    assert accepted["appended_count"] == 1
    labels = conn.execute(
        "SELECT truth_revision_no,supersedes_label_id FROM "
        "basket_policy_training_label ORDER BY rowid"
    ).fetchall()
    assert len(labels) == 3
    assert labels[1][0] == 1 and labels[2][0] == 3
    assert labels[2][1] is not None
