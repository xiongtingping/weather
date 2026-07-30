from __future__ import annotations

from bayes_basket_target.basket_kernel.contracts import content_sha256
from bayes_basket_target.basket_kernel.maker_reservation import solve_maker_reservation
from bayes_basket_target.maker_grid_oos_artifact import (
    MAKER_GRID_CURVE_SCHEMA,
    MAKER_GRID_POINT_SCHEMA,
    REALIZED_MAKER_LIFECYCLE,
    build_maker_grid_oos_artifact,
    grid_point_key,
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


def _curve(
    *,
    fill_probability: float,
    queue_cost_per_filled_share: float,
    nonfill_opportunity_cost_per_share: float,
    adverse_selection_cost_per_filled_share: float,
    wait_cost_per_filled_share: float,
) -> dict[str, float]:
    return {
        "fill_probability": fill_probability,
        "nonfill_opportunity_cost_per_share": nonfill_opportunity_cost_per_share,
        "queue_cost_per_filled_share": queue_cost_per_filled_share,
        "wait_cost_per_filled_share": wait_cost_per_filled_share,
        "adverse_selection_cost_per_filled_share": adverse_selection_cost_per_filled_share,
        "cancel_risk": 0.0,
        "late_fill_risk": 0.0,
        "cancel_risk_cost_per_requested_share": 0.0,
        "late_fill_risk_cost_per_requested_share": 0.0,
    }


def _refresh_point_bindings(point: dict) -> dict:
    point["candidate_price"] = float(point["candidate_price"])
    point["price_tick"] = float(point["price_tick"])
    point["timeout_seconds"] = float(point["timeout_seconds"])
    scope = {field: point[field] for field in SCOPE}
    grid_key = content_sha256({
        "schema": "maker_grid_oos_point_key_v1",
        **{
            field: point[field]
            for field in (
                "candidate_price",
                "price_tick",
                "timeout_seconds",
                *SCOPE.keys(),
            )
        },
    })
    point_identity = {
        "schema": MAKER_GRID_POINT_SCHEMA,
        "grid_point_id": point["grid_point_id"],
        "grid_point_key": grid_key,
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
        "grid_point_key": grid_key,
        "curve_sha256": point["curve_sha256"],
        "scope_sha256": scope_sha,
        "as_of_utc": AS_OF,
    }
    return point


def _point(
    i: int,
    *,
    price: float,
    timeout: float,
    fill_probability: float,
    queue_cost: float,
    nonfill_cost: float,
    adverse_cost: float,
    wait_cost: float,
) -> dict:
    point = {
        **SCOPE,
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
        "holdout_input_sha256": f"{i + 101:064x}",
        "oos_artifact_id": f"artifact-{i}",
        "oos_artifact_sha256": f"{i + 201:064x}",
        "official_ws_snapshot_sha256": f"{i + 501:064x}",
        "formal_lifecycle_lineage": {
            "evidence_class": REALIZED_MAKER_LIFECYCLE,
            "lifecycle_id": f"lifecycle-{i}",
            "lifecycle_sha256": f"{i + 301:064x}",
            "reconciliation_id": f"reconciliation-{i}",
            "reconciliation_sha256": f"{i + 401:064x}",
            "terminal_state": "resolved_filled",
            "live_submit_authorized": True,
            "orders_submitted": 1,
            "reconciled_at_utc": "2026-06-30T23:00:00Z",
        },
        "curve": _curve(
            fill_probability=fill_probability,
            queue_cost_per_filled_share=queue_cost,
            nonfill_opportunity_cost_per_share=nonfill_cost,
            adverse_selection_cost_per_filled_share=adverse_cost,
            wait_cost_per_filled_share=wait_cost,
        ),
        "n_eff": 80,
        "holdout_n": 80,
    }
    return _refresh_point_bindings(point)


def _expected(*points: dict) -> list[dict]:
    return [
        {
            key: point[key]
            for key in (
                "candidate_price",
                "price_tick",
                "timeout_seconds",
                *SCOPE.keys(),
            )
        }
        for point in points
    ]


def _solve(*, formal: bool, artifact=None, **overrides):
    payload = {
        "probabilities": {"35°C": 0.7, "36°C": 0.3},
        "filled_shares_by_bucket": {"35°C": 0.0, "36°C": 0.0},
        "filled_cost_by_bucket": {"35°C": 0.0, "36°C": 0.0},
        "bucket": "35°C",
        "requested_shares": 10.0,
        "protected_buckets": ["35°C", "36°C"],
        "weights": {
            "lambda_profit_floor": 0.0,
            "lambda_downside": 0.0,
            "lambda_shortfall": 0.0,
            "lambda_breakthrough": 0.0,
            "lambda_concentration": 0.0,
            "lambda_execution": 0.20,
        },
        "concentration_scale_usdc": 10.0,
        "price_tick_size": 0.01,
        "best_ask_price": 0.20,
        "maximum_limit_price": 0.99,
        "explicit_price_grid": [0.01, 0.11],
        "timeout_candidates_seconds": [30.0, 60.0],
        "formal_activation_eligible": formal,
        "expected_scope": SCOPE,
        "as_of_utc": AS_OF,
        "artifact": artifact,
        "cost_terms": None if formal else {
            "fill_probability": 0.7,
            "queue_cost_per_filled_share": 0.001,
            "nonfill_opportunity_cost_per_share": 0.02,
            "adverse_selection_cost_per_filled_share": 0.001,
            "wait_cost_per_filled_share": 0.001,
        },
    }
    payload.update(overrides)
    return solve_maker_reservation(**payload)


def _valid_artifact():
    points = [
        _point(
            0,
            price=0.01,
            timeout=30.0,
            fill_probability=1.0,
            queue_cost=0.0,
            nonfill_cost=0.0,
            adverse_cost=0.0,
            wait_cost=0.0,
        ),
        _point(
            1,
            price=0.11,
            timeout=30.0,
            fill_probability=0.60,
            queue_cost=0.002,
            nonfill_cost=0.010,
            adverse_cost=0.001,
            wait_cost=0.001,
        ),
        _point(
            2,
            price=0.01,
            timeout=60.0,
            fill_probability=0.75,
            queue_cost=0.001,
            nonfill_cost=0.020,
            adverse_cost=0.001,
            wait_cost=0.002,
        ),
        _point(
            3,
            price=0.11,
            timeout=60.0,
            fill_probability=0.50,
            queue_cost=0.003,
            nonfill_cost=0.030,
            adverse_cost=0.002,
            wait_cost=0.003,
        ),
    ]
    artifact = build_maker_grid_oos_artifact(
        points,
        _expected(*points),
        artifact_id="grid-v1",
        as_of_utc=AS_OF,
    )
    return points, artifact


def test_formal_exact_grid_artifact_evaluates_exact_points_and_propagates_hashes() -> None:
    points, artifact = _valid_artifact()
    result = _solve(formal=True, artifact=artifact)
    repeated = _solve(formal=True, artifact=artifact)
    artifact_by_key = {
        grid_point_key(point): point
        for point in artifact["points"]
    }

    assert result["status"] == "AUTHORIZED"
    assert result["authorized"] is True
    assert result["formal_activation_eligible"] is True
    assert result["promotion_eligible"] is True
    assert result["artifact_sha256"] == artifact.content_sha256
    assert result["artifact_curve_sha256"] == artifact.curve_sha256
    assert result["content_sha256"] == repeated["content_sha256"]
    assert len(result["evaluations"]) == 4
    assert {tuple(row["grid_point_key"]) for row in result["evaluations"]} == {
        grid_point_key(point) for point in points
    }
    assert all(len(row["grid_point_sha256"]) == 64 for row in result["evaluations"])
    assert all(
        row["curve_sha256"] == artifact_by_key[tuple(row["grid_point_key"])]["curve_sha256"]
        for row in result["evaluations"]
    )
    assert result["selected"]["grid_point_key"] == grid_point_key(points[0])
    assert result["selected"]["curve_sha256"] == points[0]["curve_sha256"]


def test_formal_grid_binds_price_distance_at_each_exact_price_point() -> None:
    points, _ = _valid_artifact()
    for point in points:
        point["price_distance_bin"] = (
            "far" if point["candidate_price"] == 0.01 else "near"
        )
        _refresh_point_bindings(point)
    expected_grid = _expected(*points)
    artifact = build_maker_grid_oos_artifact(
        points,
        expected_grid,
        artifact_id="grid-variable-distance-v1",
        as_of_utc=AS_OF,
    )

    result = _solve(
        formal=True,
        artifact=artifact,
        expected_grid=expected_grid,
    )

    assert result["status"] == "AUTHORIZED"
    assert {
        (row["price"], tuple(row["grid_point_key"])[8])
        for row in result["evaluations"]
    } == {(0.01, "far"), (0.11, "near")}


def test_formal_grid_never_completes_a_missing_price_timeout_point() -> None:
    points, artifact = _valid_artifact()

    result = _solve(
        formal=True,
        artifact=artifact,
        expected_grid=_expected(*points[:-1]),
    )

    assert result["status"] == "HOLD"
    assert result["reason_codes"] == [
        "maker_grid_oos_price_timeout_grid_mismatch"
    ]


def test_formal_exact_grid_absent_or_mismatch_holds_closed() -> None:
    missing = _solve(formal=True, artifact=None)
    assert missing["status"] == "HOLD"
    assert missing["reason_codes"] == ["maker_grid_oos_formal_artifact_required"]

    points, artifact = _valid_artifact()
    tampered = artifact.to_dict()
    tampered["points"][0]["timeout_seconds"] = 45.0
    mismatch = _solve(formal=True, artifact=tampered)
    assert mismatch["status"] == "HOLD"
    assert mismatch["reason_codes"][0].startswith("maker_grid_oos_")


def test_counterfactual_path_remains_unchanged() -> None:
    result = _solve(formal=False)
    assert result["status"] == "COUNTERFACTUAL_ONLY"
    assert result["authorized"] is False
    assert result["will_order"] is False
    assert result["promotion_eligible"] is False
