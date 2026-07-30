from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target import (
    ProbabilityEnvelope,
    ingest_canonical_observation_evidence,
    materialize_p1_from_canonical_observation,
)
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.p1_compiler import (
    canonical_materialization_packet,
    compile_station_p1_packets,
)
from bayes_basket_target.ssot import apply_production_ssot_schema


def _evidence() -> dict:
    return {
        "schema": "bayes_basket_target_canonical_observation_evidence_v1",
        "state": "EXPORTED",
        "checks": {"strict_timestamp_ordering": True},
        "observations": [{
            "observation_id": "obs-target-1",
            "station_id": "ZUUU",
            "event_date_local": "2026-07-23",
            "station_timezone": "America/New_York",
            "source_lane": "AMSC",
            "report_type": "METAR",
            "raw_sha256": "c" * 64,
            "canonical_committed_at_utc": "2026-07-23T04:10:00.000Z",
            "qc_status": "ACCEPTED",
        }],
    }


def _p0() -> ProbabilityEnvelope:
    return ProbabilityEnvelope.from_mapping({
        "stage": "P0", "checkpoint_id": "p0-fact-test", "station_id": "ZUUU", "event_date": "2026-07-23",
        "settlement_timezone": "America/New_York",
        "decision_at_utc": "2026-07-23T04:20:00Z", "fresh_until_utc": "2026-07-23T05:00:00Z",
        "bucket_ordinals": {"37": 0, "38": 1, "39": 2}, "probabilities": {"37": 0.2, "38": 0.6, "39": 0.2},
        "posterior_version": "p0", "posterior_sha256": "a" * 64, "bridge_sha256": "b" * 64,
        "lineage": {
            "source": "test",
            "settlement_timezone": "America/New_York",
            "season_bin": "summer",
            "settlement_definition": "official_daily_tmax_c",
        },
    })


def _p1_kwargs() -> dict:
    packet = compile_station_p1_packets(
        predecessor=_p0(),
        observations=[{
            "observation_id": "obs-target-1",
            "station_id": "ZUUU",
            "event_date_local": "2026-07-23",
            "reported_temperature_c": 38.2,
            "valid_at_utc": "2026-07-23T04:00:00Z",
            "canonical_committed_at_utc": "2026-07-23T04:10:00Z",
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
        compilation_at_utc="2026-07-23T04:21:00Z",
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


def test_target_fact_ingress_is_append_only_and_p1_reads_target_fact_not_archive() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        apply_target_ledger_schema(conn)
        evidence = _evidence()
        preview = ingest_canonical_observation_evidence(conn, evidence)
        assert preview["state"] == "VALIDATED_NO_WRITE"
        assert conn.execute("SELECT COUNT(*) FROM basket_target_observation_facts").fetchone()[0] == 0
        applied = ingest_canonical_observation_evidence(conn, evidence, commit=True)
        assert applied["inserted_count"] == 1
        replay = ingest_canonical_observation_evidence(conn, evidence, commit=True)
        assert replay["replayed_count"] == 1
        result = materialize_p1_from_canonical_observation(
            conn,
            **_p1_kwargs(),
        )
        assert result["state"] == "VALIDATED_NO_WRITE"


def test_file_backed_target_without_canonical_fact_table_fails_closed(tmp_path) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        apply_production_ssot_schema(conn)
        try:
            materialize_p1_from_canonical_observation(
                conn,
                **{
                    **_p1_kwargs(),
                    "observation_id": "obs-missing",
                },
            )
        except ValueError as exc:
            assert "canonical_observation_missing" in str(exc)
        else:
            raise AssertionError("file-backed target must not use archive-style observation fallback")


def test_canonical_fact_ingress_rejects_future_settlement_day_without_writing() -> None:
    evidence = _evidence()
    evidence["observations"][0] = {
        **evidence["observations"][0],
        "event_date_local": "2026-07-25",
        "canonical_committed_at_utc": "2026-07-24T23:00:00Z",
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        apply_target_ledger_schema(conn)
        with pytest.raises(ContractError, match="future_event_day_not_allowed"):
            ingest_canonical_observation_evidence(conn, evidence, commit=True)
        assert conn.execute("SELECT COUNT(*) FROM basket_target_observation_facts").fetchone()[0] == 0
