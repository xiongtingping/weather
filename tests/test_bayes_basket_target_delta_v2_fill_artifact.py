from __future__ import annotations

import json
import sqlite3
import pytest

from bayes_basket_target.basket_kernel.contracts import ContractError, content_sha256
from bayes_basket_target.delta_v2_fill_artifact import (
    FILL_DISTRIBUTION_ARTIFACT_SCHEMA,
    FILL_DISTRIBUTION_PROMOTION_SCHEMA,
    DeltaV2FillDistributionArtifact,
    commit_delta_v2_fill_distribution_artifact,
    resolve_delta_v2_fill_distribution,
)


BASELINE_HASH = "a" * 64
RECEIPT_HASH = "b" * 64


def _maker_distribution(
    *,
    zero: float = 0.1,
    partial: float = 0.3,
    full: float = 0.6,
    late: float = 0.05,
    partial_fill_fraction: float = 0.25,
) -> dict[str, float]:
    return {
        "fill_probability_zero": float(zero),
        "fill_probability_partial": float(partial),
        "fill_probability_full": float(full),
        "late_fill_after_cancel_request_probability": float(late),
        "partial_fill_fraction": float(partial_fill_fraction),
    }


def _fok_distribution() -> dict[str, float]:
    return {
        "fill_probability_zero": 0.0,
        "fill_probability_partial": 0.0,
        "fill_probability_full": 1.0,
        "late_fill_after_cancel_request_probability": 0.0,
        "partial_fill_fraction": 0.0,
    }


def _execution_costs() -> dict[str, float]:
    return {
        "wait_cost": 0.02,
        "queue_risk": 0.015,
        "adverse_selection": 0.01,
        "reprice_risk": 0.005,
        "capital_lock_cost": 0.01,
    }


def _artifact(*, oos_passed: bool) -> dict[str, object]:
    raw: dict[str, object] = {
        "schema": FILL_DISTRIBUTION_ARTIFACT_SCHEMA,
        "artifact_id": "fill-dist-rkpk-p1-v1",
        "station_id": "RKPK",
        "stage": "P1",
        "season_bin": "summer",
        "maturity_bin": "d0",
        "liquidity_bin": "liquid",
        "bucket": "36",
        "price": 0.12,
        "timeout_seconds": 20.0,
        "queue_ahead_min": 0.0,
        "queue_ahead_max": 2.0,
        "spread_min": 0.01,
        "spread_max": 0.02,
        "depth_min": 1.5,
        "depth_max": 2.5,
        "quote_version_scope": "*",
        "maker_distribution": _maker_distribution(),
        "fok_distribution": _fok_distribution(),
        "maker_execution_costs": _execution_costs(),
        "fok_execution_costs": _execution_costs(),
        "train_start_date": "2026-06-01",
        "train_end_date": "2026-06-30",
        "holdout_start_date": "2026-07-01",
        "holdout_end_date": "2026-07-20",
        "sample_n": 200 if oos_passed else 0,
        "holdout_n": 80 if oos_passed else 0,
        "consecutive_forward_windows": 2 if oos_passed else 0,
        "compared_baselines": (
            sorted(["v1", "EV_ONLY", "ZERO_WEIGHT", "HOLD"])
            if oos_passed
            else []
        ),
        "oos_metrics": (
            {"delta_score_lift_vs_v1": 0.12, "resource_failures": 0.0}
            if oos_passed
            else {}
        ),
        "oos_gate_version": "delta-v2-fill-forward-oos-v1",
        "oos_gate_passed": oos_passed,
        "trained_through_utc": "2026-07-20T00:00:00Z",
        "valid_from_utc": "2026-07-21T00:00:00Z",
        "valid_until_utc": "2026-08-21T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE",
        "fill_distribution_artifact_sha256": BASELINE_HASH,
    }
    raw["content_sha256"] = content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )
    return raw


def _promotion(
    artifact: dict[str, object],
    *,
    decision: str,
    decision_seq: int = 1,
) -> dict[str, object]:
    raw: dict[str, object] = {
        "schema": FILL_DISTRIBUTION_PROMOTION_SCHEMA,
        "promotion_id": f"fill-dist-promotion-{artifact['artifact_id']}",
        "artifact_id": artifact["artifact_id"],
        "decision_seq": decision_seq,
        "decision": decision,
        "gate_version": artifact["oos_gate_version"],
        "decided_at_utc": "2026-07-20T12:00:00Z",
        "receipt_sha256": RECEIPT_HASH,
    }
    raw["content_sha256"] = content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )
    return raw


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    # commit path auto-creates schema if not exists; no gateway/network used.
    return conn


def _ensure_row_written(
    conn: sqlite3.Connection,
    artifact: dict[str, object],
    decision: str,
    *,
    seq: int = 1,
) -> dict:
    return commit_delta_v2_fill_distribution_artifact(
        conn,
        artifact=artifact,
        promotion=_promotion(artifact, decision=decision, decision_seq=seq),
    )


def test_oos_gates_require_two_windows_and_four_baselines() -> None:
    artifact = _artifact(oos_passed=True)
    artifact["consecutive_forward_windows"] = 1
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="oos_comparison_gate_incomplete"):
        DeltaV2FillDistributionArtifact.from_mapping(artifact)


def test_preregistered_artifact_can_be_committed_but_not_resolved() -> None:
    conn = _db()
    artifact = _artifact(oos_passed=False)

    first = commit_delta_v2_fill_distribution_artifact(
        conn,
        artifact=artifact,
        promotion=_promotion(artifact, decision="PREREGISTERED"),
    )
    second = commit_delta_v2_fill_distribution_artifact(
        conn,
        artifact=dict(artifact),
        promotion=_promotion(artifact, decision="PREREGISTERED"),
    )

    assert first["rows_written"] == 2
    assert second["rows_written"] == 0
    assert first["state"] == "COMMITTED"
    assert second["state"] == "UNCHANGED_ZERO_WRITE"
    with pytest.raises(ContractError, match="not_promoted"):
        resolve_delta_v2_fill_distribution(
            conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin="d0",
            liquidity_bin="liquid",
            bucket="36",
            price=0.12,
            timeout_seconds=20.0,
            queue_ahead_shares=1.2,
            spread=0.015,
            depth=1.8,
            quote_version="qv202607",
            evaluation_at_utc="2026-07-25T00:00:00Z",
        )


def test_no_live_fill_distribution_runs_before_forward_oos() -> None:
    conn = _db()
    artifact = _artifact(oos_passed=False)
    receipt = _ensure_row_written(
        conn,
        artifact,
        "NO_LIVE_ACTIVATED",
    )
    resolved = resolve_delta_v2_fill_distribution(
        conn,
        station_id="RKPK",
        stage="P1",
        season_bin="summer",
        maturity_bin="d0",
        liquidity_bin="liquid",
        bucket="36",
        price=0.12,
        timeout_seconds=20.0,
        queue_ahead_shares=1.2,
        spread=0.015,
        depth=1.8,
        quote_version="qv202607",
        evaluation_at_utc="2026-07-26T00:00:00Z",
        required_authority_scope="PRODUCTION_NO_LIVE",
        policy_fill_distribution_artifact_sha256=(
            artifact["fill_distribution_artifact_sha256"]
        ),
    )

    assert receipt["rows_written"] == 2
    assert resolved.oos_gate_passed is False
    assert receipt["live_submit_authorized"] is False


def test_no_live_bootstrap_can_use_stage_and_bucket_wildcards() -> None:
    conn = _db()
    artifact = _artifact(oos_passed=False)
    artifact["artifact_id"] = "fill-dist-rkpk-bootstrap"
    artifact["stage"] = "*"
    artifact["bucket"] = "*"
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    _ensure_row_written(conn, artifact, "NO_LIVE_ACTIVATED")

    resolved = resolve_delta_v2_fill_distribution(
        conn,
        station_id="RKPK",
        stage="P0",
        season_bin="summer",
        maturity_bin="d0",
        liquidity_bin="liquid",
        bucket="44°C or higher",
        price=0.12,
        timeout_seconds=20.0,
        queue_ahead_shares=1.2,
        spread=0.015,
        depth=1.8,
        quote_version="qv202607",
        evaluation_at_utc="2026-07-26T00:00:00Z",
        required_authority_scope="PRODUCTION_NO_LIVE",
        policy_fill_distribution_artifact_sha256=BASELINE_HASH,
    )

    assert resolved.artifact_id == "fill-dist-rkpk-bootstrap"
    assert resolved.stage == "*"
    assert resolved.bucket == "*"


def test_exact_fill_artifact_overrides_bootstrap_wildcard() -> None:
    conn = _db()
    wildcard = _artifact(oos_passed=False)
    wildcard["artifact_id"] = "fill-dist-rkpk-bootstrap"
    wildcard["stage"] = "*"
    wildcard["bucket"] = "*"
    wildcard["content_sha256"] = content_sha256(
        {key: value for key, value in wildcard.items() if key != "content_sha256"}
    )
    exact = _artifact(oos_passed=False)
    exact["artifact_id"] = "fill-dist-rkpk-p1-36-exact"
    exact["maker_distribution"] = _maker_distribution(
        zero=0.2,
        partial=0.3,
        full=0.5,
    )
    exact["content_sha256"] = content_sha256(
        {key: value for key, value in exact.items() if key != "content_sha256"}
    )
    _ensure_row_written(conn, wildcard, "NO_LIVE_ACTIVATED")
    _ensure_row_written(conn, exact, "NO_LIVE_ACTIVATED")

    resolved = resolve_delta_v2_fill_distribution(
        conn,
        station_id="RKPK",
        stage="P1",
        season_bin="summer",
        maturity_bin="d0",
        liquidity_bin="liquid",
        bucket="36",
        price=0.12,
        timeout_seconds=20.0,
        queue_ahead_shares=1.2,
        spread=0.015,
        depth=1.8,
        quote_version="qv202607",
        evaluation_at_utc="2026-07-26T00:00:00Z",
        required_authority_scope="PRODUCTION_NO_LIVE",
        policy_fill_distribution_artifact_sha256=BASELINE_HASH,
    )

    assert resolved.artifact_id == "fill-dist-rkpk-p1-36-exact"


def test_promoted_artifact_commit_and_resolve_are_idempotent_and_bindable() -> None:
    conn = _db()
    artifact = _artifact(oos_passed=True)
    first = _ensure_row_written(conn, artifact, "PROMOTED")
    second = _ensure_row_written(conn, artifact, "PROMOTED")
    resolved = resolve_delta_v2_fill_distribution(
        conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin="d0",
            liquidity_bin="liquid",
            bucket="36",
            price=0.12,
            timeout_seconds=20.0,
            queue_ahead_shares=1.2,
            spread=0.015,
            depth=1.8,
            quote_version="qv202607",
            evaluation_at_utc="2026-07-26T00:00:00Z",
            policy_fill_distribution_artifact_sha256=artifact["fill_distribution_artifact_sha256"],
    )

    assert first["rows_written"] == 2
    assert second["rows_written"] == 0
    assert resolved.artifact_id == "fill-dist-rkpk-p1-v1"
    assert resolved.content_sha256 == artifact["content_sha256"]
    with pytest.raises(ContractError, match="policy_binding_mismatch"):
        resolve_delta_v2_fill_distribution(
            conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin="d0",
            liquidity_bin="liquid",
            bucket="36",
            price=0.12,
            timeout_seconds=20.0,
            queue_ahead_shares=1.2,
            spread=0.015,
            depth=1.8,
            quote_version="qv202607",
            evaluation_at_utc="2026-07-26T00:00:00Z",
            policy_fill_distribution_artifact_sha256="f" * 64,
        )


def test_fok_distribution_forbidden_to_have_non_full_or_late_mass() -> None:
    artifact = _artifact(oos_passed=False)
    artifact["fok_distribution"] = {
        "fill_probability_zero": 0.0,
        "fill_probability_partial": 0.1,
        "fill_probability_full": 0.9,
        "late_fill_after_cancel_request_probability": 0.0,
        "partial_fill_fraction": 0.0,
    }
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="fok_partial_forbidden"):
        DeltaV2FillDistributionArtifact.from_mapping(artifact)

    artifact = _artifact(oos_passed=False)
    artifact["fok_distribution"] = {
        "fill_probability_zero": 0.0,
        "fill_probability_partial": 0.0,
        "fill_probability_full": 0.9,
        "late_fill_after_cancel_request_probability": 0.0,
        "partial_fill_fraction": 0.0,
    }
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="fok_mass_must_be_full"):
        DeltaV2FillDistributionArtifact.from_mapping(artifact)


def test_execution_costs_required_for_maker_and_fok() -> None:
    artifact = _artifact(oos_passed=False)
    artifact.pop("maker_execution_costs")
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="execution_cost_fields_missing"):
        DeltaV2FillDistributionArtifact.from_mapping(artifact)

    artifact = _artifact(oos_passed=False)
    artifact["fok_execution_costs"] = _execution_costs()
    artifact["fok_execution_costs"]["wait_cost"] = -0.001
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="must_be_nonnegative"):
        DeltaV2FillDistributionArtifact.from_mapping(artifact)


def test_to_kernel_execution_costs_exports_kernel_fields() -> None:
    artifact = DeltaV2FillDistributionArtifact.from_mapping(_artifact(oos_passed=False))
    assert artifact.to_kernel_execution_costs("MAKER") == {
        "wait": 0.02,
        "queue": 0.015,
        "adverse_selection": 0.01,
        "reprice": 0.005,
        "capital_lock": 0.01,
    }
    assert artifact.to_kernel_execution_costs("fok") == {
        "wait": 0.02,
        "queue": 0.015,
        "adverse_selection": 0.01,
        "reprice": 0.005,
        "capital_lock": 0.01,
    }
    with pytest.raises(ContractError, match="mode_not_supported"):
        artifact.to_kernel_execution_costs("BAD")


def test_maker_distribution_requires_zero_partial_full_and_late_fill_probability() -> None:
    artifact = _artifact(oos_passed=False)
    artifact["maker_distribution"] = _maker_distribution(partial=0.0)
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="maker_mass_or_support_invalid"):
        DeltaV2FillDistributionArtifact.from_mapping(artifact)

    artifact = _artifact(oos_passed=False)
    artifact["maker_distribution"] = _maker_distribution(
        late=0.95, partial_fill_fraction=0.3, partial=0.3, full=0.6
    )
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="late_fill_probability_exceeds_active_fill"):
        DeltaV2FillDistributionArtifact.from_mapping(artifact)


def test_preregistered_cannot_claim_oos_metrics_or_windows() -> None:
    artifact = _artifact(oos_passed=False)
    artifact["sample_n"] = 10
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="preregistered_must_not_claim_oos"):
        DeltaV2FillDistributionArtifact.from_mapping(artifact)


def test_append_only_tables_reject_update_or_delete() -> None:
    conn = _db()
    artifact = _artifact(oos_passed=False)
    _ensure_row_written(conn, artifact, "PREREGISTERED")

    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "UPDATE basket_delta_v2_fill_artifact SET station_id='BAD' "
            "WHERE artifact_id='fill-dist-rkpk-p1-v1'"
        )

    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute("DELETE FROM basket_delta_v2_fill_artifact")


def test_resolution_rejects_out_of_window_query() -> None:
    conn = _db()
    artifact = _artifact(oos_passed=True)
    _ensure_row_written(conn, artifact, "PROMOTED")

    with pytest.raises(ContractError, match="not_active"):
        resolve_delta_v2_fill_distribution(
            conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin="d0",
            liquidity_bin="liquid",
            bucket="36",
            price=0.12,
            timeout_seconds=20.0,
            queue_ahead_shares=1.2,
        spread=0.015,
        depth=1.8,
        quote_version="qv202607",
        evaluation_at_utc="2026-07-20T23:00:00Z",
        )


def test_resolve_matches_interval_ranges_and_quote_version_scope_wildcard() -> None:
    conn = _db()
    artifact = _artifact(oos_passed=True)
    artifact["artifact_id"] = "fill-dist-rkpk-p1-wildcard"
    artifact["quote_version_scope"] = "*"
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    _ensure_row_written(conn, artifact, "PROMOTED")

    resolved = resolve_delta_v2_fill_distribution(
        conn,
        station_id="RKPK",
        stage="P1",
        season_bin="summer",
        maturity_bin="d0",
        liquidity_bin="liquid",
        bucket="36",
        price=0.12,
        timeout_seconds=20.0,
        queue_ahead_shares=0.0,
        spread=0.015,
        depth=1.8,
        quote_version="qv202610",
        evaluation_at_utc="2026-07-26T00:00:00Z",
        policy_fill_distribution_artifact_sha256=artifact["fill_distribution_artifact_sha256"],
    )
    assert resolved.artifact_id == "fill-dist-rkpk-p1-wildcard"


def test_resolve_explicit_quote_version_scope_can_match_only_one_version() -> None:
    conn = _db()
    artifact = _artifact(oos_passed=True)
    artifact["artifact_id"] = "fill-dist-rkpk-p1-explicit"
    artifact["quote_version_scope"] = "qv202607"
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    _ensure_row_written(conn, artifact, "PROMOTED")

    resolved = resolve_delta_v2_fill_distribution(
        conn,
        station_id="RKPK",
        stage="P1",
        season_bin="summer",
        maturity_bin="d0",
        liquidity_bin="liquid",
        bucket="36",
        price=0.12,
        timeout_seconds=20.0,
        queue_ahead_shares=1.0,
        spread=0.015,
        depth=1.8,
        quote_version="qv202607",
        evaluation_at_utc="2026-07-26T00:00:00Z",
        policy_fill_distribution_artifact_sha256=artifact["fill_distribution_artifact_sha256"],
    )
    assert resolved.artifact_id == "fill-dist-rkpk-p1-explicit"

    with pytest.raises(ContractError, match="activation_scope_miss"):
        resolve_delta_v2_fill_distribution(
            conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin="d0",
            liquidity_bin="liquid",
            bucket="36",
            price=0.12,
            timeout_seconds=20.0,
            queue_ahead_shares=1.0,
            spread=0.015,
            depth=1.8,
            quote_version="qv202606",
            evaluation_at_utc="2026-07-26T00:00:00Z",
            policy_fill_distribution_artifact_sha256=artifact["fill_distribution_artifact_sha256"],
        )


def test_resolve_fails_closed_for_multiple_matching_candidates() -> None:
    conn = _db()
    artifact_one = _artifact(oos_passed=True)
    artifact_one["artifact_id"] = "fill-dist-rkpk-p1-dup1"
    artifact_two = _artifact(oos_passed=True)
    artifact_two["artifact_id"] = "fill-dist-rkpk-p1-dup2"
    artifact_one["content_sha256"] = content_sha256(
        {key: value for key, value in artifact_one.items() if key != "content_sha256"}
    )
    artifact_two["content_sha256"] = content_sha256(
        {key: value for key, value in artifact_two.items() if key != "content_sha256"}
    )
    _ensure_row_written(conn, artifact_one, "PROMOTED")
    _ensure_row_written(conn, artifact_two, "PROMOTED")

    with pytest.raises(ContractError, match="activation_ambiguous"):
        resolve_delta_v2_fill_distribution(
            conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin="d0",
            liquidity_bin="liquid",
            bucket="36",
            price=0.12,
            timeout_seconds=20.0,
            queue_ahead_shares=1.0,
            spread=0.015,
            depth=1.8,
            quote_version="qv202607",
            evaluation_at_utc="2026-07-26T00:00:00Z",
            policy_fill_distribution_artifact_sha256=artifact_one["fill_distribution_artifact_sha256"],
        )


def test_interval_contract_requires_valid_order() -> None:
    artifact = _artifact(oos_passed=False)
    artifact["queue_ahead_min"] = 2.0
    artifact["queue_ahead_max"] = 1.0
    artifact["content_sha256"] = content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="invalid_order"):
        DeltaV2FillDistributionArtifact.from_mapping(artifact)
