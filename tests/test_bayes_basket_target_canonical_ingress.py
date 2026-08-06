from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target import ContractError, ProbabilityEnvelope, materialize_p1_from_canonical_observation
from bayes_basket_target.p1_compiler import (
    canonical_materialization_packet,
    compile_station_p1_packets,
)
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.ssot import apply_production_ssot_schema


def _p0() -> ProbabilityEnvelope:
    return ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0-canonical-test",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "America/New_York",
        "decision_at_utc": "2026-07-23T05:00:00Z",
        "fresh_until_utc": "2026-07-24T05:00:00Z",
        "bucket_ordinals": {"37": 0, "38": 1, "39": 2},
        "probabilities": {"37": 0.2, "38": 0.6, "39": 0.2},
        "posterior_version": "p0-test",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "source": "test",
            "season_bin": "summer",
            "settlement_definition": "official_daily_tmax_c",
        },
    })


def _p0m() -> ProbabilityEnvelope:
    p0 = _p0()
    return ProbabilityEnvelope.from_mapping({
        **p0.to_dict(),
        "stage": "P0-M",
        "checkpoint_id": "p0m-canonical-test",
        "decision_at_utc": "2026-07-23T05:05:00Z",
        "posterior_version": "p0m-test",
        "lineage": {
            **p0.lineage,
            "p0_root_checkpoint_id": p0.checkpoint_id,
            "p0_root_content_sha256": p0.content_sha256,
        },
    })


def _observation(
    conn: sqlite3.Connection,
    *,
    source_lane: str = "AWC",
    settlement_timezone: str = "America/New_York",
    canonical_committed_at: str = "2026-07-23T05:10:00Z",
) -> None:
    conn.execute(
        "INSERT INTO basket_target_observation_facts("
        "observation_fact_id,observation_id,station_id,event_date,"
        "settlement_timezone,source_lane,report_type,raw_sha256,"
        "canonical_committed_at,content_sha256,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "obsfact-awc-1", "obs-awc-1", "ZUUU", "2026-07-23",
            settlement_timezone, source_lane, "METAR", "c" * 64,
            canonical_committed_at, "d" * 64, canonical_committed_at,
        ),
    )


def _p1_kwargs(*, decision_at_utc: str = "2026-07-23T05:20:00Z") -> dict:
    root = _p0m()
    packet = compile_station_p1_packets(
        predecessor=root,
        observations=[{
            "observation_id": "obs-awc-1",
            "station_id": "ZUUU",
            "event_date_local": "2026-07-23",
            "settlement_timezone": "America/New_York",
            "reported_temperature_c": 38.2,
            "valid_at_utc": "2026-07-23T05:00:00Z",
            "canonical_committed_at_utc": "2026-07-23T05:10:00Z",
            "raw_sha256": "c" * 64,
            "source_lane": "AWC",
            "report_type": "METAR",
            "evidence_role": "FORMAL",
            "qc_status": "ACCEPTED",
            "correction_suspected": False,
        }],
        registry_rows=[
            {
                "station_id": "ZUUU",
                "event_date": "2026-07-23",
                "settlement_timezone": "America/New_York",
                "settlement_unit": "C",
                "outcome": bucket,
                "registry_json": f'{{"upper":{upper}}}',
            }
            for bucket, upper in (("37", 37), ("38", 38), ("39", 39))
        ],
        compilation_at_utc=decision_at_utc,
    )[0]
    projected = canonical_materialization_packet(packet)
    return {
        key: projected[key]
        for key in (
            "predecessor",
            "prediction_root",
            "observation_id",
            "trajectory_artifact",
            "trajectory_proof",
            "trajectory_observations",
            "bucket_centers_c",
            "bucket_upper_bounds_c",
            "reported_high_advanced",
            "decision_at_utc",
            "fresh_until_utc",
            "created_at_utc",
        )
    }


def test_canonical_event_bridge_is_exactly_once_and_does_not_write_on_preview() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        apply_target_ledger_schema(conn)
        _observation(conn)
        kwargs = _p1_kwargs()
        preview = materialize_p1_from_canonical_observation(conn, **kwargs)
        assert preview["state"] == "VALIDATED_NO_WRITE"
        assert conn.execute("SELECT COUNT(*) FROM basket_probability_envelope").fetchone()[0] == 0
        applied = materialize_p1_from_canonical_observation(conn, **kwargs, commit=True)
        assert applied["state"] == "COMMITTED"
        replay = materialize_p1_from_canonical_observation(conn, **kwargs, commit=True)
        assert replay["receipt"]["wrote"] is False
        assert conn.execute("SELECT COUNT(*) FROM basket_probability_envelope").fetchone()[0] == 1
        lineage = conn.execute("SELECT lineage_json FROM basket_probability_envelope").fetchone()[0]
        assert "reported_temperature_c" not in lineage


def test_p1_canonical_observation_bridge_rejects_amsc() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        apply_target_ledger_schema(conn)
        _observation(conn, source_lane="AMSC")
        with pytest.raises(ContractError, match="source_lane_invalid"):
            materialize_p1_from_canonical_observation(
                conn,
                **_p1_kwargs(),
            )


def test_canonical_event_must_be_committed_before_p1_decision() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        apply_target_ledger_schema(conn)
        _observation(conn, canonical_committed_at="2026-07-23T05:25:00Z")
        with pytest.raises(ContractError, match="not_strict_asof"):
            materialize_p1_from_canonical_observation(
                conn,
                **_p1_kwargs(),
            )


def test_legacy_observation_row_without_timezone_is_left_unbound_and_blocked() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE basket_target_observation_facts("
            "observation_fact_id TEXT PRIMARY KEY,observation_id TEXT NOT NULL UNIQUE,"
            "station_id TEXT NOT NULL,event_date TEXT NOT NULL,"
            "source_lane TEXT NOT NULL,report_type TEXT NOT NULL,"
            "raw_sha256 TEXT NOT NULL,canonical_committed_at TEXT NOT NULL,"
            "content_sha256 TEXT NOT NULL,created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO basket_target_observation_facts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "obsfact-legacy-1", "obs-awc-1", "ZUUU", "2026-07-23",
                "AWC", "METAR", "c" * 64, "2026-07-23T05:10:00Z",
                "d" * 64, "2026-07-23T05:10:00Z",
            ),
        )
        receipt = apply_production_ssot_schema(conn)
        assert receipt["observation_fact_timezone_upgrade"] == {
            "schema": "basket_target_observation_fact_settlement_timezone_v1",
            "state": "added_nullable_without_backfill",
            "legacy_rows_left_unbound": 1,
        }
        assert conn.execute(
            "SELECT settlement_timezone FROM basket_target_observation_facts "
            "WHERE observation_id='obs-awc-1'"
        ).fetchone() == (None,)
        with pytest.raises(ContractError, match="settlement_timezone_missing"):
            materialize_p1_from_canonical_observation(conn, **_p1_kwargs())
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope"
        ).fetchone()[0] == 0


def test_persisted_observation_timezone_mismatch_blocks_p1() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        apply_target_ledger_schema(conn)
        _observation(conn, settlement_timezone="Asia/Shanghai")
        with pytest.raises(ContractError, match="settlement_timezone_mismatch"):
            materialize_p1_from_canonical_observation(conn, **_p1_kwargs())
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope"
        ).fetchone()[0] == 0


def test_invalid_persisted_observation_timezone_blocks_p1() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        apply_target_ledger_schema(conn)
        _observation(conn, settlement_timezone="Invalid/Timezone")
        with pytest.raises(ContractError, match="settlement_timezone_invalid"):
            materialize_p1_from_canonical_observation(conn, **_p1_kwargs())
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope"
        ).fetchone()[0] == 0
