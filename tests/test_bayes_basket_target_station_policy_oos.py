from __future__ import annotations

import copy
import random

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.station_policy_artifact import ARTIFACT_SCHEMAS, POLICY_FAMILIES, StationPolicyArtifact
from bayes_basket_target.station_policy_oos import assert_station_policy_oos_boundary, evaluate_station_policy_oos


STATIONS = ("AAA", "BBB", "CCC")


def _rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    # The first 12 days are train; the last 18 days are test.  Four season
    # labels and every 3/7/15/30-day test window are represented explicitly.
    for station_index, station in enumerate(STATIONS):
        for day in range(1, 31):
            rows.append(
                {
                    "row_id": f"{station}-{day}",
                    "station_id": station,
                    "event_date": f"2026-01-{day:02d}",
                    "decision_at_utc": f"2026-01-{day:02d}T00:00:00Z",
                    "truth_frozen": True,
                    "truth_frozen_at_utc": f"2026-03-{day:02d}T00:00:00Z",
                    "settlement_timezone": "Asia/Shanghai",
                    "season_bin": ("spring", "summer", "autumn", "winter")[day % 4],
                    "lead_bucket": "d1",
                    "issuance_cycle": "00Z",
                    "stage": "P0",
                    "candidate_score": 0.80 + station_index * 0.005,
                    "baseline_score": 0.60,
                    "theta": {"concentration": 0.10 + station_index * 0.01},
                    "event_cluster_id": f"{station}-{day}",
                }
            )
    return rows


def _evaluate(rows: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
    promotion_requested = bool(kwargs.pop("promotion_requested", False))
    return evaluate_station_policy_oos(
        rows,
        station_ids=STATIONS,
        train_end_utc="2026-01-17T00:00:00Z",
        test_start_utc="2026-01-17T00:00:00Z",
        test_end_utc="2026-02-01T00:00:00Z",
        promotion_requested=promotion_requested,
        limits={
            "min_cross_seasons": 4,
            "min_window_n_eff": 2,
            "min_station_sample_n": 3,
            "min_leave_one_station_n_eff": 2,
            "parameter_change_limit": 0.25,
            **kwargs,
        },
    )


def test_each_family_and_station_has_independent_oos_artifact_and_metrics() -> None:
    receipt = _evaluate(_rows())
    assert_station_policy_oos_boundary(receipt)
    assert len(receipt["reports"]) == len(STATIONS) * 4 * len(POLICY_FAMILIES)
    assert len(receipt["artifacts"]) == len(STATIONS) * 4 * len(POLICY_FAMILIES)
    assert {item["family"] for item in receipt["artifacts"]} == set(POLICY_FAMILIES)
    assert {item["schema"] for item in receipt["artifacts"]} == set(ARTIFACT_SCHEMAS.values())
    for report in receipt["reports"]:
        assert report["checks"]["station_independent_fit"] is True
        assert report["checks"]["strict_temporal_split"] is True
        assert report["checks"]["cross_season"] is True
        assert report["checks"]["windows_3_7_15_30"] is True
        assert report["checks"]["scope_exact"] is True
        assert report["scope"]["season_bin"] in {"spring", "summer", "autumn", "winter"}
        assert report["metrics"]["n_eff"] >= 4
        assert report["metrics"]["leave_one_station_out_n_eff"] >= 2
        assert set(report["metrics"]["windows"]) == {"3", "7", "15", "30"}


def test_promotion_is_evidence_driven_and_never_runtime_authority() -> None:
    candidate_receipt = _evaluate(_rows())
    assert candidate_receipt["promotion_eligible"] is True
    assert candidate_receipt["promotion_permitted"] is False
    assert all(item["promoted"] is False for item in candidate_receipt["promotion_decisions"])

    receipt = _evaluate(_rows(), promotion_requested=True)
    assert receipt["promotion_eligible"] is True
    assert receipt["promotion_permitted"] is True
    # The artifact remains an offline candidate; the decision records whether
    # a caller requested activation separately from eligibility.
    assert all(item["promoted"] is True for item in receipt["promotion_decisions"])
    assert all(item["live_order_authorized"] is False for item in receipt["promotion_decisions"])
    assert all(item["writes_production_db"] is False for item in receipt["promotion_decisions"])
    assert all(item["calls_order_gateway"] is False for item in receipt["promotion_decisions"])


def test_station_evidence_cannot_be_replaced_by_aggregate_top1() -> None:
    rows = [row for row in _rows() if row["station_id"] != "BBB"]
    receipt = _evaluate(rows)
    reports = [item for item in receipt["reports"] if item["station_id"] == "BBB"]
    assert len(reports) == len(POLICY_FAMILIES)
    assert all("station_train_sample_missing" in item["blockers"] for item in reports)
    assert receipt["promotion_permitted"] is False


def test_truth_freeze_and_future_feature_leakage_fail_closed() -> None:
    rows = _rows()
    rows[13]["truth_frozen"] = False
    with pytest.raises(ContractError, match="truth_not_frozen"):
        _evaluate(rows)
    rows = _rows()
    rows[13]["feature_as_of_utc"] = "2026-01-15T00:00:00Z"
    with pytest.raises(ContractError, match="future_feature_leakage"):
        _evaluate(rows)


def test_timezone_mismatch_blocks_promotion() -> None:
    rows = _rows()
    rows[13]["settlement_timezone"] = "America/New_York"
    receipt = _evaluate(rows)
    assert receipt["promotion_permitted"] is False
    assert any("settlement_timezone_inconsistent" in report["blockers"] for report in receipt["reports"])


def test_conflicting_duplicate_fails_closed_but_exact_repetition_is_idempotent() -> None:
    rows = _rows()
    repeated = rows + [copy.deepcopy(rows[0])]
    shuffled = list(repeated)
    random.Random(31337).shuffle(shuffled)
    first = _evaluate(repeated)
    second = _evaluate(shuffled)
    assert first["evidence_sha256"] == second["evidence_sha256"]

    conflicting = copy.deepcopy(rows)
    conflicting[1]["row_id"] = rows[0]["row_id"]
    conflicting[1]["candidate_score"] = 0.01
    with pytest.raises(ContractError, match="duplicate_row_conflict"):
        _evaluate(conflicting)


def test_low_high_low_repeated_evaluation_has_no_state_leakage() -> None:
    low = _rows()
    first = _evaluate(low)
    high = copy.deepcopy(low)
    for row in high:
        row["candidate_score"] = 0.99
    _evaluate(high)
    second = _evaluate(low)
    assert first["evidence_sha256"] == second["evidence_sha256"]


def test_resource_bound_rejects_more_than_configured_station_scope() -> None:
    rows = _rows()
    rows.append(dict(rows[0], row_id="TOO-MANY-1", station_id="DDD"))
    with pytest.raises(ContractError, match="input_station_limit_exceeded"):
        _evaluate(rows, max_stations=3)


def test_artifact_content_hash_and_parameter_change_gate_are_immutable() -> None:
    receipt = _evaluate(_rows(), parameter_change_limit=0.01)
    assert receipt["promotion_permitted"] is False
    candidate = receipt["artifacts"][0]
    artifact = StationPolicyArtifact.from_mapping(candidate)
    tampered = dict(candidate)
    tampered["theta_post"] = {"concentration": 99.0}
    with pytest.raises(ContractError, match="content_sha256_mismatch"):
        StationPolicyArtifact.from_mapping(tampered)
    assert artifact.promoted is False
