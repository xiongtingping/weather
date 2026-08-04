from __future__ import annotations

from bayes_basket_target.aifs_deterministic_oos import (
    MIN_HOLDOUT_DAYS,
    MIN_TRAIN_DAYS,
    build_aifs_deterministic_training_facts,
)
from scripts.build_aifs_deterministic_strict_oos import (
    build_strict_oos_runner_receipt,
)
from tests.test_aifs_deterministic_oos import _inputs


def test_shortfall_receipt_keeps_aifs_native_oos_non_authorizing() -> None:
    curves, arrivals, truths = _inputs(count=1)
    batch = build_aifs_deterministic_training_facts(
        curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
    )

    receipt = build_strict_oos_runner_receipt(
        batch, as_of_utc="2026-07-31T00:00:00Z"
    )

    assert receipt["state"] == "PENDING_STRICT_OOS_SAMPLES"
    assert receipt["strict_oos_report"] is None
    assert receipt["candidate_count"] == 0
    assert receipt["scope_shortfalls"] == [{
        "station_id": "KATL",
        "target_timezone": "America/New_York",
        "run_cycle": "18Z",
        "season_bin": "summer",
        "lead_bucket": "d2",
        "available_case_count": 1,
        "required_train_case_count": MIN_TRAIN_DAYS,
        "required_strict_holdout_case_count": MIN_HOLDOUT_DAYS,
        "required_total_case_count": MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS,
        "missing_case_count": MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS - 1,
        "event_date_start": "2026-06-01",
        "event_date_end": "2026-06-01",
    }]
    assert receipt["formal_p0_eligible"] is False
    assert receipt["boundary"]["downloads_raw"] is False
    assert receipt["boundary"]["writes_production_database"] is False
    assert receipt["boundary"]["calls_order_gateway"] is False


def test_runner_returns_strict_report_only_after_formal_20_plus_8_scope() -> None:
    curves, arrivals, truths = _inputs(count=MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS + 1)
    batch = build_aifs_deterministic_training_facts(
        curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
    )

    receipt = build_strict_oos_runner_receipt(
        batch, as_of_utc="2026-07-31T00:00:00Z"
    )

    assert receipt["state"] == "STRICT_OOS_CANDIDATES_NOT_BINDING"
    assert receipt["candidate_count"] == 1
    assert receipt["strict_oos_report"]["candidate_count"] == 1
    assert receipt["scope_shortfalls"] == []
    assert receipt["formal_p0_eligible"] is False
