from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target import ContractError, ProbabilityEnvelope, materialize_p1_from_canonical_observation
from bayes_basket_target.p1_compiler import (
    canonical_materialization_packet,
    compile_station_p1_packets,
)
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


def _observation(conn: sqlite3.Connection, *, correction: int = 0) -> None:
    conn.execute("""CREATE TABLE reported_trajectory_observations(
        observation_id TEXT PRIMARY KEY, source_lane TEXT, station_id TEXT,
        report_type TEXT, raw_sha256 TEXT, canonical_committed_at TEXT,
        correction_suspected INTEGER)""")
    conn.execute("INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?)", (
        "obs-amsc-1", "amsc", "ZUUU", "METAR", "c" * 64,
        "2026-07-23T05:10:00Z", correction,
    ))


def _p1_kwargs(*, decision_at_utc: str = "2026-07-23T05:20:00Z") -> dict:
    root = _p0()
    packet = compile_station_p1_packets(
        predecessor=root,
        observations=[{
            "observation_id": "obs-amsc-1",
            "station_id": "ZUUU",
            "event_date_local": "2026-07-23",
            "reported_temperature_c": 38.2,
            "valid_at_utc": "2026-07-23T05:00:00Z",
            "canonical_committed_at_utc": "2026-07-23T05:10:00Z",
            "raw_sha256": "c" * 64,
            "source_lane": "AMSC",
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


def test_correction_is_quarantined_and_no_p1_is_written() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _observation(conn, correction=1)
        with pytest.raises(ContractError, match="correction_requires_review"):
            materialize_p1_from_canonical_observation(
                conn,
                **_p1_kwargs(),
            )


def test_canonical_event_must_be_committed_before_p1_decision() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _observation(conn)
        conn.execute(
            "UPDATE reported_trajectory_observations "
            "SET canonical_committed_at=? WHERE observation_id=?",
            ("2026-07-23T05:25:00Z", "obs-amsc-1"),
        )
        with pytest.raises(ContractError, match="not_strict_asof"):
            materialize_p1_from_canonical_observation(
                conn,
                **_p1_kwargs(),
            )
