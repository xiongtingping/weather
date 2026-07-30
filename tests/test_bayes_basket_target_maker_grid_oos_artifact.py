from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import random
import subprocess
import sys

import pytest

from bayes_basket_target.basket_kernel.contracts import ContractError, content_sha256
from bayes_basket_target.maker_grid_oos_artifact import (
    CURVE_FIELDS,
    MAKER_GRID_CURVE_SCHEMA,
    MAKER_GRID_POINT_SCHEMA,
    NO_LIVE_COUNTERFACTUAL_PROXY,
    REALIZED_MAKER_LIFECYCLE,
    build_maker_grid_oos_artifact,
    grid_point_key,
    validate_maker_grid_oos_artifact,
)


AS_OF = "2026-07-20T00:00:00Z"
SCOPE = {
    "station_id": "RKPK",
    "season_bin": "summer",
    "lead_bucket": "d1",
    "issuance": "00Z",
    "liquidity_bin": "liquid",
    "price_distance_bin": "near",
    "queue_bin": "q0_q5",
}


def _curve(value: float = 0.4) -> dict[str, float]:
    return {
        "fill_probability": value,
        "nonfill_opportunity_cost_per_share": 0.02,
        "queue_cost_per_filled_share": 0.003,
        "wait_cost_per_filled_share": 0.004,
        "adverse_selection_cost_per_filled_share": 0.005,
        "cancel_risk": 0.1,
        "late_fill_risk": 0.05,
        "cancel_risk_cost_per_requested_share": 0.03,
        "late_fill_risk_cost_per_requested_share": 0.04,
    }


def _point(i: int, *, price: float = 0.10, timeout: float = 5.0) -> dict:
    scope = dict(SCOPE)
    if i:
        scope["queue_bin"] = f"q{i}_{i + 5}"
    point = {
        **scope,
        "grid_point_id": f"point-{i}",
        "candidate_price": float(price),
        "price_tick": 0.01,
        "timeout_seconds": float(timeout),
        "train_start_utc": "2026-01-01T00:00:00Z",
        "train_end_utc": "2026-06-01T00:00:00Z",
        "holdout_start_utc": "2026-06-01T00:00:00Z",
        "holdout_end_utc": "2026-07-01T00:00:00Z",
        "as_of_utc": AS_OF,
        "training_input_sha256": f"{i + 1:064x}",
        "holdout_input_sha256": f"{i + 1001:064x}",
        "oos_artifact_id": f"point-artifact-{i}",
        "oos_artifact_sha256": f"{i + 2001:064x}",
        "official_ws_snapshot_sha256": f"{i + 3001:064x}",
        "formal_lifecycle_lineage": {
            "evidence_class": REALIZED_MAKER_LIFECYCLE,
            "lifecycle_id": f"lifecycle-{i}",
            "lifecycle_sha256": f"{i + 4001:064x}",
            "reconciliation_id": f"reconciliation-{i}",
            "reconciliation_sha256": f"{i + 5001:064x}",
            "terminal_state": "resolved_filled",
            "live_submit_authorized": True,
            "orders_submitted": 1,
            "reconciled_at_utc": "2026-06-30T23:00:00Z",
        },
        "curve": _curve(0.4 + i * 0.01),
        "n_eff": 80,
        "holdout_n": 80,
    }
    return _refresh_point_bindings(point)


def _refresh_point_bindings(point: dict) -> dict:
    """Rebind a deliberately changed point before testing semantic gates."""

    point["candidate_price"] = float(point["candidate_price"])
    point["price_tick"] = float(point["price_tick"])
    point["timeout_seconds"] = float(point["timeout_seconds"])
    scope = {field: point[field] for field in SCOPE}
    key = content_sha256({
        "schema": "maker_grid_oos_point_key_v1",
        **{field: point[field] for field in (
            "candidate_price", "price_tick", "timeout_seconds", *SCOPE.keys()
        )},
    })
    point_identity = {
        "schema": MAKER_GRID_POINT_SCHEMA,
        "grid_point_id": point["grid_point_id"],
        "grid_point_key": key,
        "candidate_price": point["candidate_price"],
        "price_tick": point["price_tick"],
        "timeout_seconds": point["timeout_seconds"],
        **scope,
        "train_start_utc": point["train_start_utc"],
        "train_end_utc": point["train_end_utc"],
        "holdout_start_utc": point["holdout_start_utc"],
        "holdout_end_utc": point["holdout_end_utc"],
        "as_of_utc": AS_OF,
        "training_input_sha256": point["training_input_sha256"],
        "holdout_input_sha256": point["holdout_input_sha256"],
        "oos_artifact_id": point["oos_artifact_id"],
        "oos_artifact_sha256": point["oos_artifact_sha256"],
        "formal_lifecycle_lineage": point["formal_lifecycle_lineage"],
        "official_ws_snapshot_sha256": point["official_ws_snapshot_sha256"],
        "curve": point["curve"],
        "n_eff": point["n_eff"],
        "holdout_n": point["holdout_n"],
    }
    point["curve_sha256"] = content_sha256({
        "schema": MAKER_GRID_CURVE_SCHEMA,
        **point_identity,
    })
    scope_sha = content_sha256({
        "schema": "maker_grid_oos_exact_scope_v1",
        **scope,
    })
    point["promotion_evidence"] = {
        **point.get("promotion_evidence", {}),
        "decision_id": point.get("promotion_evidence", {}).get(
            "decision_id", f"promotion-{point['grid_point_id']}"
        ),
        "decision": "PROMOTED",
        "gate_version": "maker-grid-gate-v1",
        "decided_at_utc": "2026-07-02T00:00:00Z",
        "receipt_sha256": point.get("promotion_evidence", {}).get(
            "receipt_sha256", content_sha256({"promotion": point["grid_point_id"]})
        ),
        "oos_metrics_sha256": content_sha256({
            "curve": point["curve"],
            "n_eff": point["n_eff"],
            "holdout_n": point["holdout_n"],
        }),
        "grid_point_key": key,
        "curve_sha256": point["curve_sha256"],
        "scope_sha256": scope_sha,
        "as_of_utc": AS_OF,
    }
    return point


def _expected(*points: dict) -> list[dict]:
    return [
        {
            key: point[key]
            for key in (
                "candidate_price", "price_tick", "timeout_seconds", *SCOPE.keys()
            )
        }
        for point in points
    ]


def _valid() -> tuple[list[dict], list[dict]]:
    points = [_point(0, price=0.10, timeout=5), _point(1, price=0.10, timeout=10), _point(2, price=0.20, timeout=5)]
    return points, _expected(*points)


def test_complete_grid_compiles_with_point_and_curve_hashes():
    points, expected = _valid()
    artifact = build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)
    assert artifact["grid_size"] == 3
    assert artifact.formal_activation_eligible is True
    assert len(artifact["content_sha256"]) == 64
    assert len(artifact["curve_sha256"]) == 64
    assert all(len(point["curve_sha256"]) == 64 for point in artifact["points"])
    assert validate_maker_grid_oos_artifact(artifact.to_dict(), expected_grid=expected).to_dict() == artifact.to_dict()


def test_permutations_and_python_hash_seeds_are_deterministic():
    points, expected = _valid()
    baseline = build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF).to_dict()
    for seed in (11, 29, 47, 101):
        shuffled = list(points)
        random.Random(seed).shuffle(shuffled)
        current = build_maker_grid_oos_artifact(shuffled, list(reversed(expected)), artifact_id="grid-v1", as_of_utc=AS_OF).to_dict()
        assert current == baseline

    code = """
from tests.test_bayes_basket_target_maker_grid_oos_artifact import _valid
from bayes_basket_target.maker_grid_oos_artifact import build_maker_grid_oos_artifact
p, e = _valid()
print(build_maker_grid_oos_artifact(p, e, artifact_id='grid-v1', as_of_utc='2026-07-20T00:00:00Z')['content_sha256'])
"""
    values = []
    for seed in ("11", "29", "random"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH="/Users/xiong/weather")
        values.append(subprocess.check_output([sys.executable, "-c", code], env=env, text=True).strip())
    assert len(set(values)) == 1


def test_missing_and_extra_grid_points_fail_closed():
    points, expected = _valid()
    with pytest.raises(ContractError, match="grid_point_missing"):
        build_maker_grid_oos_artifact(points[:2], expected, artifact_id="grid-v1", as_of_utc=AS_OF)
    extra = _point(3, price=0.30, timeout=5)
    with pytest.raises(ContractError, match="grid_point_extra"):
        build_maker_grid_oos_artifact(points + [extra], expected, artifact_id="grid-v1", as_of_utc=AS_OF)


def test_duplicate_expected_and_duplicate_observation_are_rejected():
    points, expected = _valid()
    with pytest.raises(ContractError, match="expected_grid_duplicate"):
        build_maker_grid_oos_artifact(points, expected + [expected[0]], artifact_id="grid-v1", as_of_utc=AS_OF)
    duplicate = deepcopy(points)
    duplicate[1] = deepcopy(duplicate[0])
    with pytest.raises(ContractError, match="duplicate_grid_point"):
        build_maker_grid_oos_artifact(duplicate, expected, artifact_id="grid-v1", as_of_utc=AS_OF)


@pytest.mark.parametrize("field,value", [("candidate_price", 0.11), ("timeout_seconds", 15), ("queue_bin", "far")])
def test_price_timeout_and_scope_are_exact_not_neighbour_fallback(field, value):
    points, expected = _valid()
    tampered = deepcopy(points)
    tampered[0][field] = value
    _refresh_point_bindings(tampered[0])
    with pytest.raises(ContractError, match="grid_point_(missing|extra)"):
        build_maker_grid_oos_artifact(tampered, expected, artifact_id="grid-v1", as_of_utc=AS_OF)


def test_cross_as_of_and_window_leak_are_rejected():
    points, expected = _valid()
    points[0]["as_of_utc"] = "2026-07-21T00:00:00Z"
    with pytest.raises(ContractError, match="cross_as_of_scope"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)
    points, expected = _valid()
    points[0]["holdout_end_utc"] = "2026-07-21T00:00:00Z"
    with pytest.raises(ContractError, match="train_holdout_as_of_window_invalid"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)


def test_no_single_artifact_or_input_lineage_reuse():
    points, expected = _valid()
    points[1]["oos_artifact_id"] = points[0]["oos_artifact_id"]
    _refresh_point_bindings(points[1])
    with pytest.raises(ContractError, match="single_artifact_reuse"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)
    points, expected = _valid()
    points[1]["holdout_input_sha256"] = points[0]["holdout_input_sha256"]
    _refresh_point_bindings(points[1])
    with pytest.raises(ContractError, match="single_artifact_reuse"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)


def test_proxy_and_live_false_can_never_be_formal():
    points, expected = _valid()
    points[0]["formal_lifecycle_lineage"]["evidence_class"] = NO_LIVE_COUNTERFACTUAL_PROXY
    with pytest.raises(ContractError, match="proxy_never_formal_eligible"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)
    points, expected = _valid()
    points[0]["formal_lifecycle_lineage"]["live_submit_authorized"] = False
    with pytest.raises(ContractError, match="formal_lifecycle_live_authority_required"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)


def test_lifecycle_and_reconciliation_are_terminal_and_formally_bound():
    points, expected = _valid()
    points[0]["formal_lifecycle_lineage"]["terminal_state"] = "WORKING"
    with pytest.raises(ContractError, match="terminal_state_required"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)
    points, expected = _valid()
    points[0]["promotion_evidence"]["curve_sha256"] = "f" * 64
    with pytest.raises(ContractError, match="promotion_curve_binding_mismatch"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)


def test_promotion_evidence_and_official_ws_hash_are_exact():
    points, expected = _valid()
    points[0]["promotion_evidence"]["decision"] = "HOLD"
    with pytest.raises(ContractError, match="promotion_not_promoted"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)
    points, expected = _valid()
    points[0]["official_ws_snapshot_sha256"] = "bad"
    with pytest.raises(ContractError, match="official_ws_snapshot_sha256_invalid_sha256"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)


def test_curve_metrics_samples_and_tick_contract_are_bounded():
    points, expected = _valid()
    points[0]["curve"]["fill_probability"] = 1.1
    with pytest.raises(ContractError, match="outside_unit_interval"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)
    points, expected = _valid()
    points[0]["holdout_n"] = 29
    with pytest.raises(ContractError, match="holdout_n_too_small"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)
    points, expected = _valid()
    points[0]["candidate_price"] = 0.105
    with pytest.raises(ContractError, match="candidate_price_not_on_tick"):
        build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)


def test_serialized_content_and_curve_hash_tampering_is_rejected():
    points, expected = _valid()
    artifact = build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF).to_dict()
    bad = deepcopy(artifact)
    bad["points"][0]["curve"]["fill_probability"] = 0.99
    with pytest.raises(ContractError, match="curve_sha256_mismatch"):
        validate_maker_grid_oos_artifact(bad, expected_grid=expected)
    bad = deepcopy(artifact)
    bad["content_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="artifact_content_sha256_mismatch"):
        validate_maker_grid_oos_artifact(bad, expected_grid=expected)


def test_validation_requires_complete_grid_even_if_points_are_valid():
    points, expected = _valid()
    artifact = build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF).to_dict()
    artifact["points"] = artifact["points"][:2]
    with pytest.raises(ContractError, match="grid_point_missing"):
        validate_maker_grid_oos_artifact(artifact, expected_grid=expected)
    artifact = build_maker_grid_oos_artifact(
        points, expected, artifact_id="grid-v1", as_of_utc=AS_OF
    ).to_dict()
    artifact["grid_spec"] = expected[:2]
    with pytest.raises(ContractError, match="grid_spec_mismatch"):
        validate_maker_grid_oos_artifact(artifact, expected_grid=expected)


def test_resource_bound_rejects_unbounded_grid_before_compile():
    point = _point(0)
    expected = _expected(point) * 257
    with pytest.raises(ContractError, match="expected_grid_size_invalid"):
        build_maker_grid_oos_artifact([point], expected, artifact_id="grid-v1", as_of_utc=AS_OF)


def test_scope_key_is_seed_independent_and_does_not_fill_intermediate_points():
    points, expected = _valid()
    artifact = build_maker_grid_oos_artifact(points, expected, artifact_id="grid-v1", as_of_utc=AS_OF)
    keys = {point["grid_point_key"] for point in artifact["points"]}
    assert len(keys) == 3
    assert grid_point_key(points[0]) == (
        points[0]["candidate_price"], points[0]["price_tick"], points[0]["timeout_seconds"],
        *(points[0][field] for field in SCOPE),
    )
    assert set(CURVE_FIELDS) == set(points[0]["curve"])
