"""The served calibration is refit on the whole rolling window; the gate is not.

User ruling 2026-09-02 (option 1).  Measured that day on 361 served
artifacts: the event day sat a median six days past ``train_end_date``
because the gate withholds its holdout from the fit and the row served was
that train-only fit.  Under a five-day half-life that is 56% of the intended
weight missing.  Refitting through the newest closed day cut centre MAE by
7.7% on 13,431 scope-days (tmp/bias_gap_arms.py).

What must stay fixed: the gate's parameters and its holdout metrics -- the
cross weights, the residual series and the selection evidence are built from
them and their bytes must not move.  What changes: the three parameter
columns the compiler reads.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from bayes_basket_target import native_ensemble_oos as oos


def _cases(shift_last: float = 0.0) -> list[dict]:
    """Twenty closed days; the last three run ``shift_last`` warmer than forecast."""

    rows = []
    start = date(2026, 7, 1)
    for index in range(20):
        day = start + timedelta(days=index)
        base = 25.0 + 0.1 * index
        values = [base - 0.5, base, base + 0.5]
        actual = base + 0.4 + (shift_last if index >= 17 else 0.0)
        rows.append({
            "event_date": day.isoformat(),
            "values": values,
            "actual_tmax_c": actual,
            "transport_provider_id": "OPEN_METEO",
            "holdout_eligible": True,
            "aggregator_snapshot_at_utc": f"{day.isoformat()}T00:00:00Z",
            "member_set_sha256": "a" * 64,
            "member_run_content_shas": ["b" * 64],
            "historical_oos_contract_ids": [],
            "evidence_timing_class": oos.HISTORICAL_REPUBLISHER_RECEIPT,
            "historical_closed_day": True,
            "future_event_count": 0,
        })
    return rows


def _evaluate(cases: list[dict]) -> dict:
    return oos._evaluate_system(
        system_id="OPEN_METEO_TEST",
        cases=cases,
        min_train_days=5,
        min_holdout_days=3,
    )


def test_gate_fits_on_train_only_and_serving_refits_through_the_newest_day() -> None:
    cases = _cases(shift_last=3.0)
    result = _evaluate(cases)
    assert result["state"] == "OOS_READY"
    holdout_start = result["holdout_start_date"]
    train = [c for c in cases if c["event_date"] < holdout_start]
    assert result["train_end_date"] == train[-1]["event_date"]
    assert result["parameters"]["bias_c"] == oos._fit_parameters(train)["bias_c"]
    assert result["serving_parameters"]["bias_c"] == oos._fit_parameters(cases)["bias_c"]
    assert result["serving_fit_through_date"] == cases[-1]["event_date"]
    assert result["serving_n"] == len(cases)
    # The shifted holdout days are visible to the serving fit and not to the gate.
    assert result["serving_parameters"]["bias_c"] > result["parameters"]["bias_c"] + 0.2
    assert result["serving_parameters"]["fit_through_date"] == cases[-1]["event_date"]
    assert result["parameters"]["fit_through_date"] == train[-1]["event_date"]


def test_gate_metrics_are_scored_with_the_gate_parameters_not_the_serving_refit() -> None:
    cases = _cases(shift_last=3.0)
    result = _evaluate(cases)
    holdout = [c for c in cases if c["event_date"] >= result["holdout_start_date"]]
    gate = result["parameters"]
    expected = oos._mean_metrics([
        oos._metrics(oos._apply_parameters(c["values"], gate), float(c["actual_tmax_c"]))
        for c in holdout
    ])
    assert result["oos_metrics"] == expected
    assert [row["event_date"] for row in result["predictions"]] == [c["event_date"] for c in holdout]


def test_the_persisted_row_serves_the_refit_and_keeps_the_gate_window() -> None:
    cases = _cases(shift_last=3.0)
    evaluation = _evaluate(cases)
    scope = {
        "station_id": "TEST",
        "season_bin": "summer",
        "lead_bucket": "h00_12",
        "settlement_definition": "official_daily_tmax_c",
        "threshold_profile": "FORWARD_ACCUMULATION_5_TRAIN_3_HOLDOUT",
        "min_train_days": 5,
        "min_holdout_days": 3,
        "formal_promotion_min_train_days": 20,
        "formal_promotion_min_holdout_days": 8,
    }
    row = oos._system_artifact_identity(
        system_id="OPEN_METEO_TEST",
        system_run_cycle="00Z",
        scope=scope,
        evaluation=evaluation,
        activated_at_utc="2026-07-21T00:00:00Z",
        valid_until="2026-07-28T00:00:00Z",
    )
    bias = json.loads(row["bias_parameters_json"])
    reliability = json.loads(row["reliability_parameters_json"])
    assert bias["bias_c"] == evaluation["serving_parameters"]["bias_c"]
    assert bias["bias_c"] != evaluation["parameters"]["bias_c"]
    assert reliability["fit_through_date"] == cases[-1]["event_date"]
    assert reliability["fit_case_count"] == len(cases)
    # The gate window is still what the row describes as training/holdout.
    assert row["train_end_date"] == evaluation["train_end_date"]
    assert row["holdout_start_date"] == evaluation["holdout_start_date"]
    assert row["holdout_end_date"] == cases[-1]["event_date"]


def test_an_evaluation_without_a_serving_fit_still_serves_its_parameters() -> None:
    cases = _cases()
    evaluation = dict(_evaluate(cases))
    evaluation.pop("serving_parameters")
    scope = {
        "station_id": "TEST", "season_bin": "summer", "lead_bucket": "h00_12",
        "settlement_definition": "official_daily_tmax_c",
        "threshold_profile": "FORWARD_ACCUMULATION_5_TRAIN_3_HOLDOUT",
        "min_train_days": 5, "min_holdout_days": 3,
        "formal_promotion_min_train_days": 20, "formal_promotion_min_holdout_days": 8,
    }
    row = oos._system_artifact_identity(
        system_id="OPEN_METEO_TEST", system_run_cycle="00Z", scope=scope,
        evaluation=evaluation, activated_at_utc="2026-07-21T00:00:00Z",
        valid_until="2026-07-28T00:00:00Z",
    )
    assert json.loads(row["bias_parameters_json"])["bias_c"] == evaluation["parameters"]["bias_c"]
