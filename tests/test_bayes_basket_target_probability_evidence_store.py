from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.probability_evidence_store import (
    TABLES,
    apply_probability_evidence_schema,
    insert_probability_evidence_activation,
    insert_probability_evidence_artifact,
    insert_probability_evidence_promotion,
    insert_probability_update_proof,
    metar_trajectory_scope_key,
    probability_evidence_schema_status,
    resolve_active_probability_evidence_artifact,
)
from bayes_basket_target.metar_trajectory import (
    METAR_TRAJECTORY_AUTHORITY_SCOPE,
    METAR_TRAJECTORY_METHOD_VERSION,
    build_metar_trajectory_likelihood_artifact,
    compile_metar_trajectory_update_proof,
)
from bayes_basket_target.contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from scripts.ingest_bayes_basket_probability_evidence import (
    SCHEMA as INGRESS_SCHEMA,
    ingest,
)


def _trajectory_artifact(
    *,
    activation_id: str = "fixture-activation",
    formal: bool = False,
):
    return build_metar_trajectory_likelihood_artifact({
        "activation_id": activation_id,
        "station_id": "RKPK",
        "source_lane": "awc",
        "season_bin": "JJA",
        "local_time_bucket": "AFTERNOON",
        "maturity_bin": "D0",
        "settlement_definition": "station_local_calendar_day_tmax",
        "settlement_timezone": "Asia/Seoul",
        "feature_definitions": {"running_high": "fixture"},
        "missing_observation_policy": "DEGRADE_STRENGTH",
        "expected_interval_minutes": 60,
        "long_interval_minutes": 90,
        "max_missing_intervals": 1,
        "missing_interval_strength_factor": 0.5,
        "long_interval_strength_factor": 0.5,
        "remaining_warming_distribution": {
            "0": 0.35,
            "1": 0.65,
        },
        "hard_exclusion_baseline_distribution": {
            "0": 0.65,
            "1": 0.35,
        },
        "base_strength": 0.5 if formal else 0.0,
        "maturity_strength": 0.8 if formal else 0.0,
        "likelihood_ratio_floor": 0.25 if formal else 1.0,
        "likelihood_ratio_ceiling": 4.0 if formal else 1.0,
        "sample_n": 40 if formal else 1,
        "effective_sample_n": 32.0 if formal else 1.0,
        "oos_metrics": {
            "brier_score": 0.12 if formal else 0.0,
            "nll": 0.42 if formal else 0.0,
            "crps": 0.55 if formal else 0.0,
        },
        "tail_calibration_error": 0.04 if formal else 0.0,
        "trained_through_utc": "2026-07-01T00:00:00Z",
        "valid_from_utc": "2026-07-20T00:00:00Z",
        "valid_until_utc": "2026-08-20T00:00:00Z",
        "promotion_state": (
            "FORMAL_PROMOTED" if formal else "CANDIDATE_ONLY"
        ),
        "authority_scope": METAR_TRAJECTORY_AUTHORITY_SCOPE,
        "activated": formal,
        "method_version": METAR_TRAJECTORY_METHOD_VERSION,
    })


def test_schema_is_narrow_idempotent_and_zero_write() -> None:
    conn = sqlite3.connect(":memory:")
    first = apply_probability_evidence_schema(conn)
    second = apply_probability_evidence_schema(conn)

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["rows_written"] == second["rows_written"] == 0
    assert first["performs_legacy_backfill"] is False
    assert first["promotes_artifacts"] is False
    assert probability_evidence_schema_status(conn)["missing_tables"] == []
    for table in TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_formal_artifact_requires_oos_gate() -> None:
    conn = sqlite3.connect(":memory:")
    apply_probability_evidence_schema(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO basket_probability_evidence_artifact VALUES("
            "?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "artifact",
                "P0_HISTORICAL_META",
                "RKPK",
                "Asia/Seoul",
                "scope",
                "2026-07-01T00:00:00Z",
                "2026-07-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
                0,
                "FORMAL_PROMOTED",
                "{}",
                "a" * 64,
            ),
        )


def test_artifact_and_proof_rows_are_append_only() -> None:
    conn = sqlite3.connect(":memory:")
    apply_probability_evidence_schema(conn)
    conn.execute(
        "INSERT INTO basket_probability_evidence_artifact VALUES("
        "?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "artifact",
            "P0_HISTORICAL_META",
            "RKPK",
            "Asia/Seoul",
            "scope",
            "2026-07-01T00:00:00Z",
            "2026-07-01T00:00:00Z",
            "2026-08-01T00:00:00Z",
            0,
            "PRODUCTION_NO_LIVE_CANDIDATE",
            "{}",
            "a" * 64,
        ),
    )
    conn.execute(
        "INSERT INTO basket_probability_update_proof VALUES("
        "?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "proof",
            "P0_HISTORICAL_TILT",
            "RKPK",
            "2026-07-30",
            "Asia/Seoul",
            "p0-root",
            "artifact",
            "activation",
            "NONE",
            "2026-07-29T00:00:00Z",
            "{}",
            "b" * 64,
        ),
    )

    for table in (
        "basket_probability_evidence_artifact",
        "basket_probability_update_proof",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(f"UPDATE {table} SET content_sha256=content_sha256")
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(f"DELETE FROM {table}")


def test_writer_reader_are_idempotent_and_scope_exact() -> None:
    conn = sqlite3.connect(":memory:")
    apply_probability_evidence_schema(conn)
    sealed = _trajectory_artifact()
    artifact = sealed.to_dict()
    scope_key = metar_trajectory_scope_key(
        source_lane="awc",
        season_bin="JJA",
        local_time_bucket="AFTERNOON",
        maturity_bin="D0",
        settlement_definition="station_local_calendar_day_tmax",
    )
    artifact_row = {
        "artifact_id": sealed.artifact_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
        "scope_key": scope_key,
        "trained_through_utc": "2026-07-01T00:00:00Z",
        "valid_from_utc": "2026-07-20T00:00:00Z",
        "valid_until_utc": "2026-08-20T00:00:00Z",
        "oos_gate_passed": False,
        "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "artifact": artifact,
        "content_sha256": sealed.content_sha256,
    }
    assert insert_probability_evidence_artifact(
        conn, artifact_row
    )["wrote"] is True
    assert insert_probability_evidence_artifact(
        conn, artifact_row
    )["wrote"] is False
    activation_identity = {
        "activation_id": "fixture-activation",
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "artifact_id": sealed.artifact_id,
        "station_id": "RKPK",
        "scope_key": scope_key,
        "generation": 1,
        "valid_from_utc": "2026-07-20T00:00:00Z",
        "valid_until_utc": "2026-08-20T00:00:00Z",
        "activated_at_utc": "2026-07-20T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
    }
    activation = {
        **activation_identity,
        "content_sha256": content_sha256(activation_identity),
    }
    assert insert_probability_evidence_activation(
        conn, activation
    )["wrote"] is True
    resolved = resolve_active_probability_evidence_artifact(
        conn,
        artifact_kind="METAR_TRAJECTORY_LIKELIHOOD",
        station_id="RKPK",
        scope_key=scope_key,
        as_of_utc="2026-07-30T00:00:00Z",
        required_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
    )
    assert resolved is not None
    assert resolved["artifact"] == artifact

    root = ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0-root",
        "station_id": "RKPK",
        "event_date": "2026-07-30",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-30T05:00:00Z",
        "fresh_until_utc": "2026-07-30T15:00:00Z",
        "bucket_ordinals": {"34": 0, "35": 1},
        "probabilities": {"34": 0.4, "35": 0.6},
        "posterior_version": "fixture",
        "posterior_sha256": "d" * 64,
        "bridge_sha256": "e" * 64,
        "lineage": {
            "season_bin": "JJA",
            "settlement_definition": "station_local_calendar_day_tmax",
        },
    })
    proof_value = compile_metar_trajectory_update_proof(
        root,
        observations=[{
            "event": {
                "observation_id": "obs-1",
                "station_id": "RKPK",
                "event_date": "2026-07-30",
                # Required on a canonical observation since b0dcbbd5: the
                # station-day is local, and a report cannot be placed in it
                # without the zone it is local to.
                "settlement_timezone": "Asia/Seoul",
                "source_lane": "awc",
                "report_type": "METAR",
                "sequence": 1,
                "observed_at_utc": "2026-07-30T05:30:00Z",
                "received_at_utc": "2026-07-30T05:31:00Z",
                "raw_sha256": "f" * 64,
                "qc_state": "ACCEPTED",
                "supersedes_observation_id": None,
            },
            "canonical_committed_at_utc": "2026-07-30T05:31:00Z",
            "temperature_c": 34.0,
        }],
        artifact=sealed,
        bucket_centers_c={"34": 34.0, "35": 35.0},
        bucket_upper_bounds_c={"34": 34.49, "35": 35.49},
        maturity_bin="D0",
        as_of_utc="2026-07-30T06:00:00Z",
    )
    proof = proof_value.to_dict()
    proof_row = {
        "proof_id": proof_value.proof_id,
        "proof_kind": "METAR_TRAJECTORY_UPDATE",
        "station_id": "RKPK",
        "event_date": "2026-07-30",
        "settlement_timezone": "Asia/Seoul",
        "prediction_root_checkpoint_id": "p0-root",
        "artifact_id_or_none": sealed.artifact_id,
        "activation_id_or_none": "fixture-activation",
        "canonical_sequence_sha256_or_none": (
            proof_value.canonical_observation_sequence_sha256
        ),
        "decision_at_utc": "2026-07-30T06:00:00Z",
        "proof": proof,
        "content_sha256": proof_value.content_sha256,
    }
    assert insert_probability_update_proof(
        conn, proof_row
    )["wrote"] is True
    assert insert_probability_update_proof(
        conn, proof_row
    )["wrote"] is False
    with pytest.raises(
        ContractError,
        match=(
            "probability_update_proof_"
            "canonical_sequence_sha256_or_none_binding_mismatch"
        ),
    ):
        insert_probability_update_proof(
            conn,
            {
                **proof_row,
                "proof_id": proof_value.proof_id + "-drift",
                "canonical_sequence_sha256_or_none": "0" * 64,
            },
        )


def test_formal_activation_requires_current_promotion_and_exact_scope() -> None:
    conn = sqlite3.connect(":memory:")
    apply_probability_evidence_schema(conn)
    artifact = _trajectory_artifact(
        activation_id="formal-activation",
        formal=True,
    )
    scope_key = metar_trajectory_scope_key(
        source_lane="awc",
        season_bin="JJA",
        local_time_bucket="AFTERNOON",
        maturity_bin="D0",
        settlement_definition="station_local_calendar_day_tmax",
    )
    artifact_row = {
        "artifact_id": artifact.artifact_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
        "scope_key": scope_key,
        "trained_through_utc": artifact.trained_through_utc,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "oos_gate_passed": True,
        "authority_scope": "FORMAL_PROMOTED",
        "artifact": artifact.to_dict(),
        "content_sha256": artifact.content_sha256,
    }
    assert insert_probability_evidence_artifact(
        conn, artifact_row
    )["wrote"] is True
    with pytest.raises(
        ContractError,
        match="formal_promotion_receipt_missing",
    ):
        identity = {
            "activation_id": artifact.activation_id,
            "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
            "artifact_id": artifact.artifact_id,
            "station_id": "RKPK",
            "scope_key": scope_key,
            "generation": 1,
            "valid_from_utc": artifact.valid_from_utc,
            "valid_until_utc": artifact.valid_until_utc,
            "activated_at_utc": artifact.valid_from_utc,
            "authority_scope": "FORMAL_PROMOTED",
        }
        insert_probability_evidence_activation(
            conn,
            {
                **identity,
                "content_sha256": content_sha256(identity),
            },
        )
    promotion_identity = {
        "promotion_id": "promotion-1",
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "artifact_id": artifact.artifact_id,
        "decision_seq": 1,
        "decision": "PROMOTED",
        "gate_version": "strict-forward-oos-v1",
        "metrics_sha256": "1" * 64,
        "decided_at_utc": "2026-07-19T00:00:00Z",
        "receipt_sha256": "2" * 64,
    }
    assert insert_probability_evidence_promotion(
        conn,
        {
            **promotion_identity,
            "content_sha256": content_sha256(promotion_identity),
        },
    )["wrote"] is True
    activation_identity = {
        "activation_id": artifact.activation_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "artifact_id": artifact.artifact_id,
        "station_id": "RKPK",
        "scope_key": scope_key,
        "generation": 1,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "activated_at_utc": artifact.valid_from_utc,
        "authority_scope": "FORMAL_PROMOTED",
    }
    assert insert_probability_evidence_activation(
        conn,
        {
            **activation_identity,
            "content_sha256": content_sha256(activation_identity),
        },
    )["wrote"] is True
    assert resolve_active_probability_evidence_artifact(
        conn,
        artifact_kind="METAR_TRAJECTORY_LIKELIHOOD",
        station_id="RKPK",
        scope_key=scope_key,
        as_of_utc="2026-07-30T00:00:00Z",
        required_authority_scope="FORMAL_PROMOTED",
    )["artifact_id"] == artifact.artifact_id

    supersession_identity = {
        **promotion_identity,
        "promotion_id": "promotion-2",
        "decision_seq": 2,
        "decision": "SUPERSEDED",
        "decided_at_utc": "2026-07-31T00:00:00Z",
    }
    insert_probability_evidence_promotion(
        conn,
        {
            **supersession_identity,
            "content_sha256": content_sha256(supersession_identity),
        },
    )
    with pytest.raises(
        ContractError,
        match="formal_activation_not_currently_promoted",
    ):
        resolve_active_probability_evidence_artifact(
            conn,
            artifact_kind="METAR_TRAJECTORY_LIKELIHOOD",
            station_id="RKPK",
            scope_key=scope_key,
            as_of_utc="2026-08-01T00:00:00Z",
            required_authority_scope="FORMAL_PROMOTED",
        )


def test_formal_ingress_is_artifact_then_promotion_then_activation_atomic() -> None:
    conn = sqlite3.connect(":memory:")
    apply_probability_evidence_schema(conn)
    artifact = _trajectory_artifact(
        activation_id="ingress-formal-activation",
        formal=True,
    )
    scope_key = metar_trajectory_scope_key(
        source_lane="awc",
        season_bin="JJA",
        local_time_bucket="AFTERNOON",
        maturity_bin="D0",
        settlement_definition="station_local_calendar_day_tmax",
    )
    artifact_row = {
        "artifact_id": artifact.artifact_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "station_id": artifact.station_id,
        "settlement_timezone": artifact.settlement_timezone,
        "scope_key": scope_key,
        "trained_through_utc": artifact.trained_through_utc,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "oos_gate_passed": True,
        "authority_scope": "FORMAL_PROMOTED",
        "artifact": artifact.to_dict(),
        "content_sha256": artifact.content_sha256,
    }
    promotion_identity = {
        "promotion_id": "ingress-formal-promotion",
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "artifact_id": artifact.artifact_id,
        "decision_seq": 1,
        "decision": "PROMOTED",
        "gate_version": "strict-forward-oos-v1",
        "metrics_sha256": "3" * 64,
        "decided_at_utc": "2026-07-19T00:00:00Z",
        "receipt_sha256": "4" * 64,
    }
    activation_identity = {
        "activation_id": artifact.activation_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "artifact_id": artifact.artifact_id,
        "station_id": artifact.station_id,
        "scope_key": scope_key,
        "generation": 1,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "activated_at_utc": artifact.valid_from_utc,
        "authority_scope": "FORMAL_PROMOTED",
    }
    packet_identity = {
        "schema": INGRESS_SCHEMA,
        "artifacts": [artifact_row],
        "promotions": [{
            **promotion_identity,
            "content_sha256": content_sha256(promotion_identity),
        }],
        "activations": [{
            **activation_identity,
            "content_sha256": content_sha256(activation_identity),
        }],
    }
    packet = {
        **packet_identity,
        "packet_sha256": content_sha256(packet_identity),
    }

    with pytest.raises(
        ContractError,
        match="candidate_activation_forbidden",
    ):
        ingest(
            conn,
            packet,
            commit=True,
            expected_station_id="RKPK",
            require_no_activations=True,
        )
    assert all(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table in TABLES
    )

    preview = ingest(conn, packet, commit=False)
    assert preview["state"] == "VALIDATED_NO_WRITE"
    assert all(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table in TABLES
    )
    conn.execute("BEGIN IMMEDIATE")
    staged = ingest(
        conn,
        packet,
        commit=True,
        finalize_transaction=False,
    )
    assert staged["state"] == "STAGED_IN_CALLER_TRANSACTION"
    assert conn.in_transaction
    assert all(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        for table in TABLES[:3]
    )
    conn.rollback()
    assert all(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table in TABLES
    )
    applied = ingest(conn, packet, commit=True)
    replay = ingest(conn, packet, commit=True)
    assert applied["state"] == replay["state"] == "COMMITTED"
    assert applied["row_count"] == 3
    assert applied["artifact_receipts"][0]["wrote"] is True
    assert applied["promotion_receipts"][0]["wrote"] is True
    assert applied["activation_receipts"][0]["wrote"] is True
    assert replay["artifact_receipts"][0]["wrote"] is False
    assert replay["promotion_receipts"][0]["wrote"] is False
    assert replay["activation_receipts"][0]["wrote"] is False


def test_scoped_ingress_rejects_cross_station_artifact_before_write() -> None:
    conn = sqlite3.connect(":memory:")
    apply_probability_evidence_schema(conn)
    artifact = _trajectory_artifact(
        activation_id="scope-mismatch-activation",
        formal=False,
    )
    artifact_row = {
        "artifact_id": artifact.artifact_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "station_id": "ZUUU",
        "settlement_timezone": artifact.settlement_timezone,
        "scope_key": metar_trajectory_scope_key(
            source_lane="awc",
            season_bin="JJA",
            local_time_bucket="AFTERNOON",
            maturity_bin="D0",
            settlement_definition="station_local_calendar_day_tmax",
        ),
        "trained_through_utc": artifact.trained_through_utc,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "oos_gate_passed": False,
        "authority_scope": "CANDIDATE_ONLY",
        "artifact": artifact.to_dict(),
        "content_sha256": artifact.content_sha256,
    }
    identity = {
        "schema": INGRESS_SCHEMA,
        "artifacts": [artifact_row],
        "promotions": [],
        "activations": [],
    }
    packet = {**identity, "packet_sha256": content_sha256(identity)}

    with pytest.raises(
        ContractError,
        match="packet_station_scope_mismatch",
    ):
        ingest(
            conn,
            packet,
            commit=True,
            expected_station_id="RKPK",
        )
    assert all(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table in TABLES
    )


def test_scoped_ingress_rejects_reference_to_out_of_packet_artifact() -> None:
    conn = sqlite3.connect(":memory:")
    apply_probability_evidence_schema(conn)
    artifact = _trajectory_artifact(
        activation_id="scope-reference-activation",
        formal=False,
    )
    artifact_row = {
        "artifact_id": artifact.artifact_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "station_id": artifact.station_id,
        "settlement_timezone": artifact.settlement_timezone,
        "scope_key": metar_trajectory_scope_key(
            source_lane="awc",
            season_bin="JJA",
            local_time_bucket="AFTERNOON",
            maturity_bin="D0",
            settlement_definition="station_local_calendar_day_tmax",
        ),
        "trained_through_utc": artifact.trained_through_utc,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "oos_gate_passed": False,
        "authority_scope": "CANDIDATE_ONLY",
        "artifact": artifact.to_dict(),
        "content_sha256": artifact.content_sha256,
    }
    promotion_identity = {
        "promotion_id": "scope-reference-promotion",
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "artifact_id": "not-present-in-scoped-packet",
        "decision_seq": 1,
        "decision": "REJECTED",
        "gate_version": "strict-forward-oos-v1",
        "metrics_sha256": "3" * 64,
        "decided_at_utc": "2026-07-19T00:00:00Z",
        "receipt_sha256": "4" * 64,
    }
    identity = {
        "schema": INGRESS_SCHEMA,
        "artifacts": [artifact_row],
        "promotions": [{
            **promotion_identity,
            "content_sha256": content_sha256(promotion_identity),
        }],
        "activations": [],
    }
    packet = {**identity, "packet_sha256": content_sha256(identity)}

    with pytest.raises(
        ContractError,
        match="scoped_reference_outside_packet",
    ):
        ingest(
            conn,
            packet,
            commit=True,
            expected_station_id="RKPK",
        )
    assert all(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table in TABLES
    )
