"""The fitted calibration must be the calibration that gets served.

A level-dependent ("affine") bias form was fitted here, scored by the OOS
gate and folded into the cross-ensemble weights, but the artifact writer only
ever persisted ``bias_c``.  Serving read the missing key, found nothing and
fell back to the constant form, so every affected scope ran a centre the gate
had never scored -- and ``train_rmse_c``, which is the cross weight, kept
describing the form that was not running.  The form has since been measured
out of sample and removed (native_ensemble_oos._fit_parameters).

These tests guard the shape of that failure rather than the one form that
caused it: a fitted parameter that no column carries is a train/serve split
whatever it is called.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target import native_ensemble_p0 as p0


# The server integrates its kernel over these, so they have to partition the
# whole line -- open-ended at both tails.
_LADDER: dict[str, list[float | None]] = {
    "21": [None, 21.5],
    "22": [21.5, 22.5],
    "23": [22.5, None],
}


def _cases(count: int = 40) -> list[dict[str, object]]:
    """Warming cases with a level-dependent residual.

    The trend is what used to trip the affine branch, so a fit over these is
    the case that most wants to invent a second parameter.
    """

    cases = []
    start = date(2026, 6, 1)
    for index in range(count):
        center = 20.0 + 0.25 * index
        cases.append({
            "event_date": (start + timedelta(days=index)).isoformat(),
            "values": [center - 1.0, center, center + 1.0],
            # Residual grows with the forecast level.
            "actual_tmax_c": center + 0.1 * index - 1.0,
        })
    return cases


# Every key ``_fit_parameters`` returns, and the artifact column that carries
# it.  ``_system_artifact_identity`` writes exactly three parameter columns;
# a key in none of them never reaches the server.
PERSISTED_BY_COLUMN = {
    "bias_parameters_json": {
        "bias_c",
        "correction_form",
        "bias_bin_cuts_c",
        "bias_bin_shifts_c",
    },
    "spread_parameters_json": {
        "spread_factor",
        "train_rmse_c",
        "train_residual_sigma_c",
        "train_mean_spread_c",
    },
    "reliability_parameters_json": {
        "reliability_weight",
        "climate_samples_c",
        "train_n_effective",
        "fit_through_date",
        "fit_case_count",
    },
}


def test_fit_parameter_keys_are_all_persisted() -> None:
    """No fitted key may exist that the artifact writer drops."""

    fitted = set(oos._fit_parameters(_cases()))
    persisted = set().union(*PERSISTED_BY_COLUMN.values())
    orphaned = fitted - persisted
    assert not orphaned, (
        "_fit_parameters returns "
        f"{sorted(orphaned)}, which no artifact column carries; serving "
        "cannot see these, so the OOS gate would score a fit that never "
        "runs.  Persist them or drop them from the fit."
    )
    assert persisted - fitted == set(), (
        "PERSISTED_BY_COLUMN names keys the fit no longer produces: "
        f"{sorted(persisted - fitted)}"
    )


def test_fit_declares_the_binned_form_well_formed() -> None:
    """The fit names its form and ships a complete tercile table."""

    parameters = oos._fit_parameters(_cases())
    assert parameters["correction_form"] == oos.BIAS_CORRECTION_FORM
    cuts = parameters["bias_bin_cuts_c"]
    shifts = parameters["bias_bin_shifts_c"]
    assert len(shifts) == len(cuts) + 1 == oos.BIAS_BIN_COUNT
    assert cuts == sorted(cuts)


def test_evaluator_and_server_center_identically() -> None:
    """One resolver object, so the two centres cannot drift apart."""

    assert p0._resolved_bias_c is oos._resolved_bias_c

    parameters = oos._fit_parameters(_cases())
    values = [21.5, 22.0, 22.5]
    mean = sum(values) / len(values)
    assert oos._corrected_center(mean, parameters) == pytest.approx(
        mean + oos._resolved_bias_c(mean, parameters)
    )


def test_trend_produces_a_level_dependent_fit_that_flat_input_does_not() -> None:
    """The tercile shifts follow the residual, and degrade to the constant.

    The warming cases carry a residual that grows with the forecast level,
    so the high bin must correct more than the low one.  Cases with no
    level-dependence at all must come back with every bin at the pooled
    shift -- the shrinkage's promise that a bin never invents a signal.
    """

    trending = oos._fit_parameters(_cases())
    shifts = trending["bias_bin_shifts_c"]
    assert shifts[-1] > shifts[0]

    flat = []
    start = date(2026, 6, 1)
    for index in range(40):
        flat.append({
            "event_date": (start + timedelta(days=index)).isoformat(),
            "values": [21.0, 22.0, 23.0],
            "actual_tmax_c": 22.5,
        })
    parameters = oos._fit_parameters(flat)
    for shift in parameters["bias_bin_shifts_c"]:
        assert shift == pytest.approx(parameters["bias_c"])


def test_server_accepts_the_binned_form_and_centers_by_level() -> None:
    """Which bin the level lands in decides where the mass goes."""

    bias_parameters = {
        "bias_c": 0.5,
        "correction_form": oos.BIAS_CORRECTION_FORM,
        "bias_bin_cuts_c": [21.0, 23.0],
        "bias_bin_shifts_c": [-1.0, 0.5, 2.0],
    }
    shared = {
        "intervals": _LADDER,
        "spread_parameters": {"spread_factor": 1.0, "train_rmse_c": 1.0},
        "reliability_parameters": {
            "reliability_weight": 1.0,
            "climate_samples_c": [20.0, 21.0, 22.0],
        },
    }
    low = p0._calibrated_system_probability(
        values=[20.0, 20.5, 21.0], bias_parameters=bias_parameters, **shared
    )
    high = p0._calibrated_system_probability(
        values=[23.0, 23.5, 24.0], bias_parameters=bias_parameters, **shared
    )
    # Low levels are pulled down a degree, high levels pushed up two: the
    # cold bucket must dominate the first and the hot bucket the second.
    assert low["21"] > low["23"]
    assert high["23"] > high["21"]


def test_server_refuses_a_malformed_binned_table() -> None:
    """A shift list that does not match its cuts is refused, not padded."""

    with pytest.raises(ContractError) as error:
        p0._calibrated_system_probability(
            values=[21.0, 22.0, 23.0],
            intervals=_LADDER,
            bias_parameters={
                "bias_c": 0.5,
                "correction_form": oos.BIAS_CORRECTION_FORM,
                "bias_bin_cuts_c": [21.0, 23.0],
                "bias_bin_shifts_c": [0.5, 2.0],
            },
            spread_parameters={"spread_factor": 2.0, "train_rmse_c": 1.0},
            reliability_parameters={
                "reliability_weight": 0.8,
                "climate_samples_c": [20.0, 21.0, 22.0],
            },
        )
    assert "binned_bias_malformed" in str(error.value)


def test_server_refuses_an_unsupported_correction_form() -> None:
    """An artifact naming a form this build cannot run is fail-closed.

    The old failure was silent: serving read an unknown form, ignored it and
    substituted its own.  Refusing is what keeps a future re-introduction
    from repeating that.
    """

    with pytest.raises(ContractError) as error:
        p0._calibrated_system_probability(
            values=[21.0, 22.0, 23.0],
            intervals=_LADDER,
            bias_parameters={"bias_c": 0.5, "correction_form": "affine"},
            spread_parameters={"spread_factor": 2.0, "train_rmse_c": 1.0},
            reliability_parameters={
                "reliability_weight": 0.8,
                "climate_samples_c": [20.0, 21.0, 22.0],
            },
        )
    assert "correction_form_unsupported" in str(error.value)


def test_server_accepts_artifacts_without_the_key() -> None:
    """Every artifact in the SSOT omits it; that path must be untouched."""

    probability = p0._calibrated_system_probability(
        values=[21.0, 22.0, 23.0],
        intervals=_LADDER,
        bias_parameters={"bias_c": 0.5},
        spread_parameters={"spread_factor": 2.0, "train_rmse_c": 1.0},
        reliability_parameters={
            "reliability_weight": 0.8,
            "climate_samples_c": [20.0, 21.0, 22.0],
        },
    )
    assert probability
    assert all(value >= 0.0 for value in probability.values())


def test_explicit_constant_form_is_accepted() -> None:
    """Naming the form this build serves is not an error."""

    probability = p0._calibrated_system_probability(
        values=[21.0, 22.0, 23.0],
        intervals=_LADDER,
        bias_parameters={"bias_c": 0.5, "correction_form": "constant"},
        spread_parameters={"spread_factor": 2.0, "train_rmse_c": 1.0},
        reliability_parameters={
            "reliability_weight": 0.8,
            "climate_samples_c": [20.0, 21.0, 22.0],
        },
    )
    assert probability


def test_artifact_identity_carries_the_persisted_columns() -> None:
    """The column names in PERSISTED_BY_COLUMN are the real ones."""

    source = oos._system_artifact_identity.__code__.co_consts
    rendered = json.dumps([c for c in source if isinstance(c, str)])
    for column in PERSISTED_BY_COLUMN:
        assert column in rendered, f"{column} is not written by the artifact"
