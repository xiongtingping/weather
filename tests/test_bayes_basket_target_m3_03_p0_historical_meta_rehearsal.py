"""M3-03 P0 historical-meta isolated rehearsal contract tests."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, ProbabilityEnvelope
from bayes_basket_target.p0_historical_meta import (
    compile_p0_historical_zero_tilt_proof,
)
from bayes_basket_target.probability_bridge import (
    install_probability_bridge_schema,
    materialize_probability_bridge,
)
from bayes_basket_target.probability_evidence_store import (
    apply_probability_evidence_schema,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from scripts.rehearse_bayes_basket_target_m3_03_p0_historical_meta import (
    _commit_envelope_and_proof,
    _counts,
    _older_envelope,
    _validate_source_clock,
    run_rehearsal,
)


def _envelope() -> ProbabilityEnvelope:
    probabilities = {"35": 0.2, "36": 0.5, "37": 0.3}
    return ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "m303-p0-root",
        "station_id": "M303",
        "event_date": "2026-07-30",
        "settlement_timezone": "UTC",
        "decision_at_utc": "2026-07-29T15:08:46Z",
        "fresh_until_utc": "2026-07-29T21:08:46Z",
        "bucket_ordinals": {"35": 0, "36": 1, "37": 2},
        "probabilities": probabilities,
        "posterior_version": "p0_nwp_historical_meta_anchor_v4",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "settlement_definition": "official_daily_tmax_c",
            "bucket_intervals_c_json": (
                '{"35":[null,35.5],"36":[35.5,36.5],"37":[36.5,null]}'
            ),
        },
    })


def _proof(envelope: ProbabilityEnvelope) -> dict[str, object]:
    proof = compile_p0_historical_zero_tilt_proof(
        station_id=envelope.station_id,
        target_local_date=envelope.event_date,
        settlement_timezone=envelope.settlement_timezone,
        settlement_definition="official_daily_tmax_c",
        season_bin="summer",
        lead_bucket="d2",
        run_cycle="00Z",
        as_of_utc=envelope.decision_at_utc,
        model_probabilities=envelope.probabilities,
        bucket_centers_c={"35": 35.0, "36": 36.0, "37": 37.0},
        ensemble_artifact_sha256="c" * 64,
        ensemble_selection_evidence_sha256="d" * 64,
        ensemble_activation_sha256="e" * 64,
    )
    return proof.to_dict()


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    apply_probability_evidence_schema(conn)
    install_probability_bridge_schema(conn)
    return conn


def test_source_clock_boundary_rejects_expired_or_mismatched_state() -> None:
    decode = {
        "as_of_utc": "2026-07-29T15:08:46Z",
        "receipt_sha256": "a" * 64,
    }
    with pytest.raises(ContractError, match="receipt_mismatch"):
        _validate_source_clock(
            decode,
            {
                "native_decode_receipt_sha256": "b" * 64,
                "materialized_at_utc": "2026-07-29T15:08:46Z",
                "fresh_until_utc": "2026-07-29T21:08:46Z",
            },
        )
    with pytest.raises(ContractError, match="freshness_expired"):
        _validate_source_clock(
            decode,
            {
                "native_decode_receipt_sha256": "a" * 64,
                "materialized_at_utc": "2026-07-29T15:08:46Z",
                "fresh_until_utc": "2026-07-29T15:08:46Z",
            },
        )


def test_proof_envelope_are_atomic_duplicate_is_zero_and_failures_rollback() -> None:
    conn = _connection()
    envelope = _envelope()
    proof = _proof(envelope)
    before = _counts(conn)

    first = _commit_envelope_and_proof(conn, envelope, proof)
    assert first["wrote"] is True
    after_first = _counts(conn)
    assert after_first["basket_probability_envelope"] == 1
    assert after_first["basket_probability_bridge_history"] == 1
    assert after_first["basket_probability_update_proof"] == 1

    duplicate = _commit_envelope_and_proof(conn, envelope, proof)
    assert duplicate["wrote"] is False
    assert _counts(conn) == after_first

    with pytest.raises(RuntimeError, match="after_proof"):
        _commit_envelope_and_proof(
            conn, envelope, proof, fail_after_proof=True
        )
    assert _counts(conn) == after_first

    def fail_at_history(point: str) -> None:
        if point == "history":
            raise RuntimeError("history")

    with pytest.raises(RuntimeError, match="history"):
        _commit_envelope_and_proof(
            conn,
            envelope,
            proof,
            failure_injector=fail_at_history,
        )
    assert _counts(conn) == after_first
    assert before["basket_probability_update_proof"] == 0


def test_out_of_order_older_p0_does_not_lower_latest_authority() -> None:
    conn = _connection()
    envelope = _envelope()
    proof = _proof(envelope)
    _commit_envelope_and_proof(conn, envelope, proof)
    older = _older_envelope(envelope)

    conn.execute("BEGIN IMMEDIATE")
    try:
        receipt = materialize_probability_bridge(
            conn, older, created_at_utc=older.decision_at_utc
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    assert receipt["latest_wrote"] is False
    latest = conn.execute(
        "SELECT checkpoint_id,decision_at_utc FROM "
        "basket_probability_bridge_latest WHERE station_id=? AND event_date=?",
        (envelope.station_id, envelope.event_date),
    ).fetchone()
    assert latest == (
        envelope.checkpoint_id,
        envelope.decision_at_utc,
    )


def test_real_native_decode_registry_rehearsal_closes_m3_03() -> None:
    result = run_rehearsal()
    assert result["state"] == "REHEARSED"
    assert result["posterior_version"] == "p0_nwp_historical_meta_anchor_v4"
    assert result["theta_zero_fallback"] is True
    assert result["probabilities_before_after_equal"] is True
    assert result["model_count"] >= 2
    assert abs(sum(result["weights"].values()) - 1.0) <= 1e-9
    assert result["transaction"]["second_replay"]["wrote"] is False
    assert result["transaction"]["failure_after_proof_rolled_back"] is True
    assert result["transaction"]["failure_at_history_rolled_back"] is True
    assert result["transaction"]["out_of_order_lower_stage_preserved_latest"] is True
    assert result["temporary_cleanup_confirmed"] is True
    assert result["boundary"]["writes_real_database"] is False
    assert result["boundary"]["calls_order_gateway"] is False
