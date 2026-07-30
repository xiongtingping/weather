from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import (
    ContractError,
    GlobalBasketPriorArtifact,
    StationBasketPolicyActivation,
    resolve_station_policy,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
import scripts.apply_bayes_basket_target_target_v2_station_policy_no_live as station_policy


MANIFEST = station_policy.DEFAULT_MANIFEST
DECISION = "2026-07-28T23:32:23Z"
MATURITIES = ("d1", "d2", "d2_fallback")


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "target.sqlite3"
    with sqlite3.connect(path) as conn:
        apply_production_ssot_schema(conn)
    return path


def _manifest_copy(tmp_path: Path, **changes: object) -> Path:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    value.update(changes)
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("strategy_version", "target-v1", "boundary_invalid"),
        ("stages", ["P0", "P0-M"], "boundary_invalid"),
        ("execution_mode", "live", "boundary_invalid"),
        ("live_submit_authorized", True, "boundary_invalid"),
        ("calls_order_gateway", True, "boundary_invalid"),
        ("places_orders", True, "boundary_invalid"),
        ("orders_submitted", 1, "boundary_invalid"),
        ("default_policy", {"total_budget_usdc": 13.0}, "default_invalid"),
    ],
)
def test_manifest_hard_boundary_rejects_non_target_v2_or_live_values(
    tmp_path: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=f"target_v2_station_policy_{error}"):
        station_policy.load_manifest(_manifest_copy(tmp_path, **{field: value}))


def test_manifest_malformed_cadence_fails_closed(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["family_parameters"]["cadence"] = {
        "gate_seconds_intercept": 1.0,
        "unexpected_runtime_override": 0.2,
    }
    path = tmp_path / "malformed-cadence.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="target_v2_station_policy_family_invalid"):
        station_policy.load_manifest(path)


def test_manifest_missing_native_d2_fails_closed(tmp_path: Path) -> None:
    path = _manifest_copy(tmp_path, maturity_bins=["d1", "d2_fallback"])

    with pytest.raises(ValueError, match="target_v2_station_policy_boundary_invalid"):
        station_policy.load_manifest(path)


def test_dry_run_resolves_p0_p1_across_all_six_maturity_scopes(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)

    result = station_policy.apply(
        database=db,
        manifest_path=MANIFEST,
        commit=False,
    )

    assert result["state"] == "VALIDATED_NO_WRITE"
    assert result["resolved_scope_count"] == 6
    assert {
        (row["stage"], row["maturity_bin"])
        for row in result["resolved_scopes"]
    } == {
        (stage, maturity)
        for stage in ("P0", "P1")
        for maturity in MATURITIES
    }
    assert result["execution_mode"] == "production_no_live"
    assert result["live_submit_authorized"] is False
    assert result["calls_order_gateway"] is False
    assert result["places_orders"] is False
    assert result["orders_submitted"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_global_prior_artifact"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_policy_activation"
        ).fetchone()[0] == 0


def test_target_v1_rows_cannot_resolve_as_target_v2() -> None:
    prior = GlobalBasketPriorArtifact.create(
        global_prior_id="target-v1-test-prior",
        strategy_version="target-v1",
        family_parameters={"target": {}, "reserve": {}, "cadence": {}},
        default_policy={"total_budget_usdc": 12.0},
        valid_from_utc="2026-07-28T00:00:00Z",
        valid_until_utc="2026-08-31T00:00:00Z",
        promoted=False,
    )
    activation = StationBasketPolicyActivation.create(
        activation_id="target-v1-test-p0-d2",
        station_id="RKPK",
        stage="P0",
        season_bin="summer",
        maturity_bin="d2",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        global_prior_id=prior.global_prior_id,
        target_artifact_id=None,
        reserve_artifact_id=None,
        cadence_artifact_id=None,
        fallback_families=["target", "reserve", "cadence"],
        generation=1,
        valid_from_utc="2026-07-28T00:00:00Z",
        valid_until_utc="2026-08-31T00:00:00Z",
        activated_at_utc="2026-07-28T00:00:00Z",
        promoted=False,
    )

    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        resolve_station_policy(
            activations=[activation],
            global_priors={prior.global_prior_id: prior},
            artifacts={},
            station_id="RKPK",
            stage="P0",
            season_bin="summer",
            maturity_bin="d2",
            liquidity_bin="liquid",
            strategy_version="target-v2",
            decision_at_utc=DECISION,
            required_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
        )


def test_apply_is_no_live_idempotent_and_second_apply_writes_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _database(tmp_path)
    monkeypatch.setattr(station_policy, "_service_running", lambda: False)

    first = station_policy.apply(
        database=db,
        manifest_path=MANIFEST,
        commit=True,
    )
    second = station_policy.apply(
        database=db,
        manifest_path=MANIFEST,
        commit=True,
    )

    assert first["state"] == "COMMITTED"
    assert first["rows_written"] == 7
    assert first["counts"]["counts"]["global_priors"] == 1
    assert first["counts"]["counts"]["policy_activations"] == 6
    assert second["state"] == "UNCHANGED_ZERO_WRITE"
    assert second["rows_written"] == 0
    assert second["counts"]["counts"]["global_priors"] == 0
    assert second["counts"]["counts"]["policy_activations"] == 0
    for result in (first, second):
        assert result["execution_mode"] == "production_no_live"
        assert result["live_submit_authorized"] is False
        assert result["calls_order_gateway"] is False
        assert result["places_orders"] is False
        assert result["orders_submitted"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_global_prior_artifact"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_policy_activation"
        ).fetchone()[0] == 6
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_nonproduction_copy_can_be_materialized_while_writer_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _database(tmp_path)
    monkeypatch.setattr(station_policy, "_service_running", lambda: True)

    dry_run = station_policy.apply(
        database=db,
        manifest_path=MANIFEST,
        commit=False,
    )
    assert dry_run["state"] == "VALIDATED_NO_WRITE"
    assert dry_run["live_submit_authorized"] is False
    assert dry_run["calls_order_gateway"] is False
    assert dry_run["orders_submitted"] == 0

    committed = station_policy.apply(
        database=db,
        manifest_path=MANIFEST,
        commit=True,
    )
    assert committed["state"] == "COMMITTED"
    assert committed["live_submit_authorized"] is False
    assert committed["calls_order_gateway"] is False

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_policy_activation"
        ).fetchone()[0] == 6


def test_generation_two_replaces_fixed_one_usdc_floor(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    v1_manifest = (
        station_policy.ROOT
        / "config/trading/"
        "bayes_basket_target_target_v2_station_policy_no_live_v1.json"
    )
    v2_manifest = (
        station_policy.ROOT
        / "config/trading/"
        "bayes_basket_target_target_v2_station_policy_no_live_v2.json"
    )
    station_policy.apply(
        database=db,
        manifest_path=v1_manifest,
        commit=True,
    )
    upgraded = station_policy.apply(
        database=db,
        manifest_path=v2_manifest,
        commit=True,
    )
    replay = station_policy.apply(
        database=db,
        manifest_path=v2_manifest,
        commit=True,
    )

    assert upgraded["state"] == "COMMITTED"
    assert upgraded["rows_written"] == 7
    assert replay["state"] == "UNCHANGED_ZERO_WRITE"
    assert replay["rows_written"] == 0
    with sqlite3.connect(db) as conn:
        bundle = station_policy.load_production_artifacts(
            conn,
            station_id="RKPK",
            evaluation_at_utc="2026-07-29T01:16:00Z",
            require_calibration=False,
            require_policy=False,
        )
        resolved = resolve_station_policy(
            activations=bundle.policy_activations,
            global_priors=bundle.global_priors,
            artifacts=bundle.policy_artifacts,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin="d2",
            liquidity_bin="liquid",
            strategy_version="target-v2",
            decision_at_utc="2026-07-29T01:16:00Z",
            required_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
        )
        policy = resolved.to_adaptive_policy_overrides()
        assert resolved.activation_id.endswith("-g2")
        assert policy["clip_min_notional_usdc"] == pytest.approx(0.01)
        assert policy["clip_max_notional_usdc"] == pytest.approx(2.0)


def test_generation_three_reuses_p0_policy_for_p0m_and_matches_twenty_usdc_epoch(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)

    applied = station_policy.apply(
        database=db,
        manifest_path=MANIFEST,
        commit=True,
    )

    assert applied["state"] == "COMMITTED"
    assert applied["rows_written"] == 7
    assert applied["resolved_scope_count"] == 6
    with sqlite3.connect(db) as conn:
        bundle = station_policy.load_production_artifacts(
            conn,
            station_id="RKPK",
            evaluation_at_utc="2026-07-29T09:56:00Z",
            require_calibration=False,
            require_policy=False,
        )
        resolved = resolve_station_policy(
            activations=bundle.policy_activations,
            global_priors=bundle.global_priors,
            artifacts=bundle.policy_artifacts,
            station_id="RKPK",
            stage="P0-M",
            season_bin="summer",
            maturity_bin="d2",
            liquidity_bin="liquid",
            strategy_version="target-v2",
            decision_at_utc="2026-07-29T09:56:00Z",
            required_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
        )
        policy = resolved.to_adaptive_policy_overrides()
        assert resolved.activation_id == "target-v2-rkpk-no-live-p0-d2-g3"
        assert policy["total_budget_usdc"] == pytest.approx(20.0)
