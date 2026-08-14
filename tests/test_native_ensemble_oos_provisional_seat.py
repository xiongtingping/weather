"""A provisional cross-ensemble seat is bounded, visible and family-scoped."""

from __future__ import annotations

import pytest

from bayes_basket_target.native_ensemble_oos import (
    PROVISIONAL_CROSS_ENSEMBLE_SYSTEM_ID,
    _provisional_family_share_admission,
)


def _admit(**overrides):
    kwargs = dict(
        system_rows={
            PROVISIONAL_CROSS_ENSEMBLE_SYSTEM_ID: [("00Z", {"transport_provider_id": "DWD"})]
        },
        weights={"ECMWF_IFS_ENS_OPEN_PF50": 0.75, "NOAA_GEFS_05": 0.25},
        family_by_system={
            "ECMWF_IFS_ENS_OPEN_PF50": "ECMWF",
            "NOAA_GEFS_05": "NOAA_GEFS",
        },
        systems={
            PROVISIONAL_CROSS_ENSEMBLE_SYSTEM_ID: {
                "independence_family_id": "DWD_ICON_EPS"
            }
        },
        selected_ready_rows=[
            ("ECMWF_IFS_ENS_OPEN_PF50", "00Z", {}),
            ("NOAA_GEFS_05", "00Z", {}),
        ],
    )
    kwargs.update(overrides)
    return _provisional_family_share_admission(**kwargs)


def test_the_seat_is_one_equal_family_share() -> None:
    weights, _families, _rows, provisional = _admit()

    assert provisional == [PROVISIONAL_CROSS_ENSEMBLE_SYSTEM_ID]
    # Three families seated, so the provisional one takes exactly a third.
    assert weights[PROVISIONAL_CROSS_ENSEMBLE_SYSTEM_ID] == pytest.approx(1 / 3)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_scored_systems_keep_the_weighting_they_earned() -> None:
    # The holdout is the only external validation in the blend.  A system that
    # has none must not cause the systems that do have one to be re-ranked --
    # their relative weighting is scaled into the remainder, not recomputed.
    weights, _families, _rows, _provisional = _admit()

    assert weights["ECMWF_IFS_ENS_OPEN_PF50"] / weights["NOAA_GEFS_05"] == (
        pytest.approx(0.75 / 0.25)
    )


def test_a_family_already_seated_earns_no_provisional_share() -> None:
    # An unscored seat is justified only by the independence it adds.  A second
    # system inside a seated family adds none.
    assert _admit(
        family_by_system={"ECMWF_IFS_ENS_OPEN_PF50": "DWD_ICON_EPS"},
        weights={"ECMWF_IFS_ENS_OPEN_PF50": 1.0},
        selected_ready_rows=[("ECMWF_IFS_ENS_OPEN_PF50", "00Z", {})],
    ) is None


def test_a_system_that_earned_its_seat_is_not_re_admitted() -> None:
    assert _admit(
        weights={PROVISIONAL_CROSS_ENSEMBLE_SYSTEM_ID: 0.4, "NOAA_GEFS_05": 0.6},
        family_by_system={
            PROVISIONAL_CROSS_ENSEMBLE_SYSTEM_ID: "DWD_ICON_EPS",
            "NOAA_GEFS_05": "NOAA_GEFS",
        },
    ) is None


def test_no_seat_without_a_scope_row_to_name_its_cycle() -> None:
    # The activation identity is keyed on the per-system cycle mapping, so a
    # seat with no readable cycle would corrupt it.  Fail closed instead.
    assert _admit(system_rows={}) is None
    assert _admit(
        system_rows={PROVISIONAL_CROSS_ENSEMBLE_SYSTEM_ID: [("nonsense", {})]}
    ) is None


def test_the_exception_is_scoped_to_one_named_family() -> None:
    # Every other system still earns its seat on the shared holdout; the
    # constant is the whole surface of the exception.
    assert PROVISIONAL_CROSS_ENSEMBLE_SYSTEM_ID == "DWD_ICON_GLOBAL_EPS"
