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
    for index in range(count):
        center = 20.0 + 0.25 * index
        cases.append({
            "event_date": f"2026-06-{index + 1:02d}",
            "values": [center - 1.0, center, center + 1.0],
            # Residual grows with the forecast level.
            "actual_tmax_c": center + 0.1 * index - 1.0,
        })
    return cases


# Every key ``_fit_parameters`` returns, and the artifact column that carries
# it.  ``_system_artifact_identity`` writes exactly three parameter columns;
# a key in none of them never reaches the server.
PERSISTED_BY_COLUMN = {
    "bias_parameters_json": {"bias_c"},
    "spread_parameters_json": {
        "spread_factor",
        "train_rmse_c",
        "train_mean_spread_c",
    },
    "reliability_parameters_json": {
        "reliability_weight",
        "climate_samples_c",
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


def test_fit_declares_no_correction_form() -> None:
    """One form only, so nothing about the form needs persisting."""

    assert "correction_form" not in oos._fit_parameters(_cases())


def test_evaluator_and_server_center_identically() -> None:
    """The two centres are the same arithmetic on the same fields."""

    parameters = oos._fit_parameters(_cases())
    values = [21.5, 22.0, 22.5]
    mean = sum(values) / len(values)

    evaluator_center = oos._corrected_center(mean, parameters)
    server_center = mean + float(parameters["bias_c"])

    assert evaluator_center == pytest.approx(server_center)


def test_trend_does_not_produce_a_level_dependent_fit() -> None:
    """A strongly level-dependent residual still fits one constant.

    Guards the removal itself: this input is what the affine branch existed
    for, and it must now come back as a plain shift.
    """

    parameters = oos._fit_parameters(_cases())
    assert set(parameters) == set().union(*PERSISTED_BY_COLUMN.values())
    assert isinstance(parameters["bias_c"], float)


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
