from __future__ import annotations

from scripts import train_ncei_zuuu_hierarchical_bayes_v41 as v41
from scripts import train_ncei_zuuu_synoptic_bayes_v3 as v3


def test_v41_update_removes_raw_synoptic_and_seasonal_duplicates() -> None:
    assert not (set(v41.V41_NUMERIC) & set(v3.SYNOPTIC_DIRECT))
    assert not (set(v41.V41_NUMERIC) & set(v3.SYNOPTIC_ENGINEERED))
    assert not (set(v41.V41_NUMERIC) & v41.CONTEXT_DUPLICATES_REMOVED)
    assert set(v41.PRIOR_STATE_NUMERIC) <= set(v41.V41_NUMERIC)


def test_v41_group_penalties_shrink_interactions_more_than_core() -> None:
    names = [
        "intercept",
        "temperature_c",
        "context_prior_z_gap_to_observed_max",
        "prior_z_x_cloud",
        "temperature_c__missing",
        "current_precipitation_reported",
    ]
    penalties = v41._penalty_vector(names, 12.0)
    by_name = dict(zip(names, penalties))
    assert by_name["intercept"] == 0.01
    assert by_name["temperature_c"] == 12.0
    assert by_name["prior_z_x_cloud"] > by_name["context_prior_z_gap_to_observed_max"]
    assert by_name["temperature_c__missing"] > by_name["temperature_c"]


def test_context_prior_crossfit_holds_out_whole_local_dates(monkeypatch) -> None:
    rows = [
        {
            "event_date_local": f"2021-01-{day:02d}",
            "observed_max_to_time_c": 20.0,
        }
        for day in range(1, 21)
        for _ in range(2)
    ]

    def fake_fit(fit_rows, holdout_rows):
        fit_days = {row["event_date_local"] for row in fit_rows}
        holdout_days = {row["event_date_local"] for row in holdout_rows}
        assert not (fit_days & holdout_days)
        augmented = [
            {
                **row,
                "context_prior_mean_final_c": 22.0,
                "context_prior_sigma_c": 2.0,
            }
            for row in holdout_rows
        ]
        return {"residual_sigma_c": 2.0}, [], augmented, [], []

    monkeypatch.setattr(v41.v4, "_fit_context_prior", fake_fit)
    augmented, audit = v41._crossfit_context_prior(rows, folds=5)
    assert len(augmented) == len(rows)
    assert audit["all_rows_covered_once"] is True
    assert audit["same_day_leakage_count"] == 0
    assert all(row["context_prior_z_gap_to_observed_max"] == 1.0 for row in augmented)


def _prediction(day: str, *, nll: float, rps: float, expected: float, prob: float):
    return {
        "event_date_local": day,
        "test_year": int(day[:4]),
        "local_hour": 12.0,
        "final_daily_max_c": 30.0,
        "actual_continue": True,
        "expected_final_max_c": expected,
        "predicted_continue_probability": prob,
        "bucket_nll": nll,
        "bucket_rps": rps,
    }


def test_paired_validation_selects_candidate_only_when_primary_ci_is_better() -> None:
    baseline = []
    candidate = []
    for year in (2022, 2023, 2024, 2025):
        for day in range(1, 11):
            date = f"{year}-01-{day:02d}"
            baseline.append(
                _prediction(date, nll=1.0, rps=0.02, expected=29.0, prob=0.6)
            )
            candidate.append(
                _prediction(date, nll=0.9, rps=0.01, expected=29.5, prob=0.8)
            )
    report, day_rows = v41._paired_validation(
        baseline, candidate, resamples=500, seed=123
    )
    assert len(day_rows) == 40
    assert report["replace_v3_on_accuracy"] is True
    assert report["selected_accuracy_version"] == "v4.1"
    assert report["metrics"]["nll"]["ci95_upper"] < 0.0


def test_paired_validation_keeps_v3_when_candidate_regresses_secondary_metric() -> None:
    baseline = []
    candidate = []
    for year in (2022, 2023, 2024, 2025):
        for day in range(1, 6):
            date = f"{year}-02-{day:02d}"
            baseline.append(
                _prediction(date, nll=1.0, rps=0.02, expected=30.0, prob=0.8)
            )
            candidate.append(
                _prediction(date, nll=0.9, rps=0.01, expected=28.0, prob=0.8)
            )
    report, _ = v41._paired_validation(
        baseline, candidate, resamples=300, seed=456
    )
    assert report["primary_metrics_significantly_better"] is True
    assert report["all_point_metrics_better"] is False
    assert report["replace_v3_on_accuracy"] is False
    assert report["selected_accuracy_version"] == "v3"
