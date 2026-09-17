from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from bayes_basket_target.contracts import ContractError, ProbabilityEnvelope
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
)
from bayes_basket_target.materialization import insert_probability_envelope
from bayes_basket_target.regime_materialization import (
    build_regime_artifact,
    insert_regime_artifact,
    rematerialize_resident_regimes,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.fleet import expected_station_count


ROOT = Path(__file__).resolve().parents[1]


def _envelope(
    *,
    station_id="RKPK",
    stage="P0",
    checkpoint_id=None,
    timezone_name=None,
):
    timezone_name = timezone_name or (
        "Asia/Seoul" if station_id == "RKPK" else "America/New_York"
    )
    return ProbabilityEnvelope.from_mapping(
        {
            "stage": stage,
            "checkpoint_id": checkpoint_id or f"{station_id}-{stage}-1",
            "station_id": station_id,
            "event_date": "2026-07-28",
            "settlement_timezone": timezone_name,
            "decision_at_utc": "2026-07-27T16:00:00Z",
            "fresh_until_utc": "2026-07-28T15:00:00Z",
            "bucket_ordinals": {"36": 36, "37": 37, "38": 38},
            "probabilities": {"36": 0.303, "37": 0.31, "38": 0.387},
            "posterior_version": "strict-oos-v1",
            "posterior_sha256": "2" * 64,
            "bridge_sha256": "3" * 64,
            "lineage": {
                "settlement_timezone": timezone_name,
                "model_envelope_lower_bucket": "35",
                "model_envelope_upper_bucket": "39",
                "model_envelope_span_c": "4",
            },
        }
    )


def _seed_old_regime(
    conn: sqlite3.Connection,
    envelope: ProbabilityEnvelope,
) -> None:
    insert_regime_artifact(
        conn,
        build_regime_artifact(envelope, stability_threshold=0.005),
        created_at_utc="2026-07-27T16:00:00Z",
    )


def _upgrade_regime_supersession_schema_for_test(
    conn: sqlite3.Connection,
) -> None:
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS basket_regime_artifact_no_update;
        DROP TRIGGER IF EXISTS basket_regime_artifact_no_delete;
        DROP INDEX IF EXISTS basket_regime_artifact_scope_idx;
        ALTER TABLE basket_regime_artifact RENAME TO basket_regime_artifact_old;
        CREATE TABLE basket_regime_artifact (
          artifact_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          event_date TEXT NOT NULL,
          stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
          checkpoint_id TEXT NOT NULL,
          settlement_timezone TEXT NOT NULL,
          probability_sha256 TEXT NOT NULL CHECK(length(probability_sha256)=64),
          as_of_utc TEXT NOT NULL,
          valid_from_utc TEXT NOT NULL,
          valid_until_utc TEXT NOT NULL,
          artifact_json TEXT NOT NULL,
          content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
          state TEXT NOT NULL
            CHECK(state IN ('COMMITTED','QUARANTINED','SUPERSEDED')),
          created_at TEXT NOT NULL,
          CHECK(valid_from_utc<valid_until_utc)
        );
        INSERT INTO basket_regime_artifact SELECT *
        FROM basket_regime_artifact_old;
        DROP TABLE basket_regime_artifact_old;
        CREATE UNIQUE INDEX basket_regime_artifact_one_committed
        ON basket_regime_artifact(
          station_id,event_date,stage,checkpoint_id,probability_sha256
        ) WHERE state='COMMITTED';
        CREATE INDEX basket_regime_artifact_scope_idx
        ON basket_regime_artifact(
          station_id,event_date,stage,checkpoint_id,probability_sha256,
          state,valid_from_utc,valid_until_utc
        );
        """
    )


def _run(
    conn: sqlite3.Connection,
    envelopes,
    registry,
    *,
    apply=False,
):
    return rematerialize_resident_regimes(
        conn,
        envelopes=envelopes,
        registry_timezones=registry,
        evaluation_at_utc="2026-07-27T18:00:00Z",
        created_at_utc="2026-07-27T18:00:00Z",
        apply_production_no_live=apply,
    )


def test_dry_run_default_then_controlled_apply_and_idempotent_replay():
    envelope = _envelope()
    registry = {("RKPK", "2026-07-28"): "Asia/Seoul"}
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _seed_old_regime(conn, envelope)
        before = conn.total_changes

        dry_one = _run(conn, [envelope], registry)
        dry_two = _run(conn, [envelope], registry)
        assert dry_one == dry_two
        assert dry_one["mode"] == "DRY_RUN"
        assert dry_one["transition_count"] == 1
        assert dry_one["transitions"][0]["action"] == "SUPERSEDE_AND_INSERT"
        assert dry_one["transitions"][0]["probability_center_bucket"] == "38"
        assert dry_one["boundary"]["probability_center_modified"] is False
        assert conn.total_changes == before

        applied = _run(conn, [envelope], registry, apply=True)
        assert applied["mode"] == "APPLY_PRODUCTION_NO_LIVE"
        assert applied["transition_count"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact "
            "WHERE state='SUPERSEDED'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact "
            "WHERE state='COMMITTED'"
        ).fetchone()[0] == 1

        applied_again = _run(conn, [envelope], registry, apply=True)
        dry_again = _run(conn, [envelope], registry)
        assert applied_again["transition_count"] == 0
        assert applied_again["idempotent_count"] == 1
        assert dry_again["transition_count"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact"
        ).fetchone()[0] == 2


def test_apply_failure_rolls_back_all_prior_station_transitions():
    katl = _envelope(
        station_id="KATL",
        checkpoint_id="KATL-P0-old",
        timezone_name="America/New_York",
    )
    rkpk = _envelope(checkpoint_id="RKPK-P0-old")
    registry = {
        ("KATL", "2026-07-28"): "America/New_York",
        ("RKPK", "2026-07-28"): "Asia/Seoul",
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _seed_old_regime(conn, katl)
        _seed_old_regime(conn, rkpk)
        _upgrade_regime_supersession_schema_for_test(conn)
        conn.execute(
            "CREATE TRIGGER force_second_station_failure "
            "BEFORE UPDATE ON basket_regime_artifact "
            "WHEN OLD.station_id='RKPK' "
            "BEGIN SELECT RAISE(ABORT,'forced_second_station_failure'); END"
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="forced_second_station_failure",
        ):
            _run(conn, [rkpk, katl], registry, apply=True)
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact "
            "WHERE state='COMMITTED'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact "
            "WHERE state='SUPERSEDED'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact"
        ).fetchone()[0] == 2


def test_more_than_45_stations_and_stage_rewind_fail_closed():
    envelopes = [
        _envelope(
            station_id=f"K{index:03d}",
            checkpoint_id=f"K{index:03d}-P0",
            timezone_name="America/New_York",
        )
        for index in range(expected_station_count() + 1)
    ]
    registry = {
        (envelope.station_id, envelope.event_date):
        envelope.settlement_timezone
        for envelope in envelopes
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="resident_regime_station_limit_exceeded",
        ):
            _run(conn, envelopes, registry)
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact"
        ).fetchone()[0] == 0

    p1 = _envelope(stage="P1", checkpoint_id="RKPK-P1-old")
    p0 = _envelope(stage="P0", checkpoint_id="RKPK-P0-lower")
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _seed_old_regime(conn, p1)
        with pytest.raises(
            ContractError,
            match="resident_regime_stage_rewind_forbidden",
        ):
            _run(
                conn,
                [p0],
                {("RKPK", "2026-07-28"): "Asia/Seoul"},
            )


def _seed_registry(conn: sqlite3.Connection) -> None:
    apply_target_market_registry_schema(conn)
    snapshot = "a" * 64
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot("
        "snapshot_sha256,source_schema,source_scope,exported_at_utc,"
        "source_database_path,row_count,ingested_at_utc,content_sha256"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            snapshot,
            "reported_trajectory_market_registry_v1",
            "shared_live_registry_export_only",
            "2026-07-27T16:00:00Z",
            "/read-only/source.db",
            1,
            "2026-07-27T16:00:00Z",
            "b" * 64,
        ),
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry("
        "snapshot_sha256,slug,event_date,outcome,city_slug,station_id,"
        "settlement_timezone,settlement_unit,source_lane,source_eligible,"
        "source_block_reason,resolution_url,rules_sha256,yes_token,no_token,"
        "market_active,market_accepting_orders,registry_json,refreshed_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot,
            "rkpk-2026-07-28-38c",
            "2026-07-28",
            "38°C",
            "busan",
            "RKPK",
            "Asia/Seoul",
            "C",
            "awc",
            1,
            "",
            "https://example.invalid/rules",
            "c" * 64,
            "yes",
            "no",
            1,
            1,
            "{}",
            "2026-07-27T16:00:00Z",
        ),
    )

