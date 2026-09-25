"""Charter 216: the single-system fit refitted by shrunk bucket log loss.

The moments (decayed mean error, residual sigma over member spread) are the
start; a log-loss fit of one centre shift and the spread factor, weighted at
a 15-day half-life, is taken half-way from them.  Only official native
ensembles are refitted.  These pin the arithmetic the research measured.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target import native_ensemble_p0 as p0

ROOT = Path(__file__).resolve().parents[1]


def _cases(count=30, *, members=5, transport="ECMWF_OPEN_DATA_S3"):
    """Native-shaped cases: a warm bias that moves, and a too-narrow spread."""

    start = date(2026, 8, 1)
    out = []
    for index in range(count):
        center = 22.0 + 0.2 * index + (0.5 if index % 3 == 0 else -0.3)
        values = [center + 0.3 * (k - (members - 1) / 2) for k in range(members)]
        out.append({
            "event_date": (start + timedelta(days=index)).isoformat(),
            "values": values,
            "actual_tmax_c": round(center + 1.1 + (1.4 if index % 4 == 0 else -0.9), 1),
            "transport_provider_id": transport,
        })
    return out


def _moment(cases, monkeypatch):
    monkeypatch.setattr(oos, "SINGLE_SYSTEM_FIT_RULE", oos.SINGLE_SYSTEM_FIT_RULE_MOMENT)
    return oos._fit_parameters(cases)


def test_the_served_default_is_the_shrunk_log_loss_fit():
    assert oos.SINGLE_SYSTEM_FIT_RULE == oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS == "logloss_shrunk_v1"
    assert oos.LOGLOSS_REFIT_HALF_LIFE_DAYS == oos.RELIABILITY_HALF_LIFE_DAYS == 15.0
    assert oos.LOGLOSS_REFIT_KAPPA == 0.5
    assert oos.LOGLOSS_REFIT_SPREAD_BOUNDS == (0.5, 5.0)
    assert oos.LOGLOSS_REFIT_DELTA_BOUNDS_C == (-3.0, 3.0)


def test_the_truth_probability_is_the_served_distribution_read_at_one_bucket(monkeypatch):
    cases = _cases()
    params = _moment(cases, monkeypatch)
    for case in cases:  # training cases sit on the tercile cuts: the ULP trap
        served = oos._apply_parameters([float(v) for v in case["values"]], params)
        key = oos._temperature_bucket(float(case["actual_tmax_c"]))
        ours = oos._truth_bucket_probability(
            case["values"], case["actual_tmax_c"], params,
            delta_c=0.0, spread=params["spread_factor"],
        )
        assert ours == pytest.approx(served.get(key, 0.0), abs=1e-12)


def test_the_vectorised_loss_equals_the_per_case_reference(monkeypatch):
    cases = _cases()
    params = _moment(cases, monkeypatch)
    arrays = oos._refit_case_arrays(cases, params)
    for delta, spread in ((0.0, params["spread_factor"]), (0.4, 1.3), (-1.1, 3.0)):
        vec = oos._truth_bucket_probabilities(arrays, delta_c=delta, spread=spread)
        ref = [oos._truth_bucket_probability(c["values"], c["actual_tmax_c"], params,
                                             delta_c=delta, spread=spread) for c in cases]
        assert list(vec) == pytest.approx(ref, abs=1e-15)


def test_the_fit_moves_half_way_from_the_moments(monkeypatch):
    cases = _cases()
    moment = _moment(cases, monkeypatch)
    monkeypatch.setattr(oos, "SINGLE_SYSTEM_FIT_RULE", oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS)
    monkeypatch.setattr(oos, "_logloss_refit", lambda cases, parameters: (0.8, 2.0))
    fitted = oos._fit_parameters(cases)
    assert fitted["fit_rule"] == "logloss_shrunk_v1"
    assert fitted["moment_bias_c"] == pytest.approx(moment["bias_c"])
    assert fitted["logloss_shift_c"] == pytest.approx(0.4)
    assert fitted["bias_c"] == pytest.approx(moment["bias_c"] + 0.4)
    assert fitted["bias_bin_shifts_c"] == pytest.approx([x + 0.4 for x in moment["bias_bin_shifts_c"]])
    assert fitted["bias_bin_cuts_c"] == moment["bias_bin_cuts_c"]
    assert fitted["moment_spread_factor"] == pytest.approx(moment["spread_factor"])
    assert fitted["spread_factor"] == pytest.approx(0.5 * moment["spread_factor"] + 0.5 * 2.0)
    # What the seat weights and the width layer read stays on the moments.
    for key in ("train_rmse_c", "train_residual_sigma_c", "train_mean_spread_c"):
        assert fitted[key] == pytest.approx(moment[key])


def test_the_real_refit_lowers_the_training_log_loss_and_stays_in_bounds(monkeypatch):
    cases = _cases()
    moment = _moment(cases, monkeypatch)
    delta, spread = oos._logloss_refit(cases, moment)
    assert oos.LOGLOSS_REFIT_DELTA_BOUNDS_C[0] <= delta <= oos.LOGLOSS_REFIT_DELTA_BOUNDS_C[1]
    assert oos.LOGLOSS_REFIT_SPREAD_BOUNDS[0] <= spread <= oos.LOGLOSS_REFIT_SPREAD_BOUNDS[1]

    def loss(d, s):
        return -sum(math.log(max(oos._truth_bucket_probability(
            c["values"], c["actual_tmax_c"], moment, delta_c=d, spread=s), 1e-6)) for c in cases)

    assert loss(delta, spread) < loss(0.0, moment["spread_factor"])
    # Deterministic: the same cases give the same certificate.
    assert oos._logloss_refit(cases, moment) == (delta, spread)


@pytest.mark.parametrize("cases", [
    _cases(members=1),                                   # a point forecast
    _cases(transport="OPEN_METEO"),                      # a relay
    [{k: v for k, v in c.items() if k != "transport_provider_id"} for c in _cases()],  # regional lane
])
def test_only_native_ensembles_are_refitted(cases, monkeypatch):
    monkeypatch.setattr(oos, "SINGLE_SYSTEM_FIT_RULE", oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS)
    fitted = oos._fit_parameters(cases)
    assert fitted["fit_rule"] == "moment_v1"
    assert fitted["logloss_shift_c"] == 0.0
    assert fitted["bias_c"] == fitted["moment_bias_c"]
    assert fitted["spread_factor"] == fitted["moment_spread_factor"]


def test_the_research_arm_serves_the_moments(monkeypatch):
    moment = _moment(_cases(), monkeypatch)
    assert moment["fit_rule"] == "moment_v1"
    assert moment["bias_c"] == moment["moment_bias_c"]
    assert moment["spread_factor"] == moment["moment_spread_factor"]


def test_serving_centres_where_the_certificate_was_earned(monkeypatch):
    """The compiler reads the shifted bias and spread from the persisted fields."""

    monkeypatch.setattr(oos, "SINGLE_SYSTEM_FIT_RULE", oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS)
    fitted = oos._fit_parameters(_cases())
    assert fitted["fit_rule"] == "logloss_shrunk_v1"
    values = [23.0, 23.4, 23.9, 24.2, 24.8]
    intervals = {str(k): (k - 0.5, k + 0.5) for k in range(15, 35)}
    intervals["14"] = (None, 14.5)
    intervals["35"] = (34.5, None)
    served = p0._calibrated_system_probability(
        values=values, intervals=intervals, bias_parameters=fitted,
        spread_parameters=fitted, reliability_parameters=fitted,
    )
    gated = oos._apply_parameters(values, fitted, intervals=intervals)
    assert served == gated


def test_charter_216_records_the_rule():
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter.split("\n216. ", 1)[1]
    for needle in ("logloss_shrunk_v1", "PM_SINGLE_SYSTEM_FIT_RULE", "RELIABILITY_HALF_LIFE_DAYS",
                   "v14", "v16", "v25", "裁决人：用户"):
        assert needle in clause
