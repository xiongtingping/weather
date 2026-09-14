"""Charter 134: the stable pool's weight rules."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.stable_pool import (  # noqa: E402
    available_pool_weights,
    family_balanced_weights,
    shrink_toward_baseline,
    stable_pool_weights,
)

FAMILIES = {
    "ECMWF_IFS_ENS_OPEN_PF50": "ECMWF",
    "ECMWF_AIFS_ENS_GLOBAL": "ECMWF",
    "NOAA_GEFS_05": "NOAA_GEFS",
    "ECCC_GEM_GLOBAL_ENS": "ECCC_GEM",
    "DWD_ICON_GLOBAL_EPS": "DWD_ICON_EPS",
}
FOUR = [
    "ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05",
    "ECCC_GEM_GLOBAL_ENS", "DWD_ICON_GLOBAL_EPS",
]


def test_the_baseline_gives_each_family_one_share() -> None:
    weights = family_balanced_weights(FOUR, FAMILIES)
    assert weights == pytest.approx({system: 0.25 for system in FOUR})
    # Two ECMWF products are one family's worth of independent information.
    with_aifs = family_balanced_weights(FOUR + ["ECMWF_AIFS_ENS_GLOBAL"], FAMILIES)
    assert with_aifs["ECMWF_IFS_ENS_OPEN_PF50"] == pytest.approx(0.125)
    assert with_aifs["ECMWF_AIFS_ENS_GLOBAL"] == pytest.approx(0.125)
    assert with_aifs["NOAA_GEFS_05"] == pytest.approx(0.25)
    with pytest.raises(ContractError, match="fewer_than_two_families"):
        family_balanced_weights(
            ["ECMWF_IFS_ENS_OPEN_PF50", "ECMWF_AIFS_ENS_GLOBAL"], FAMILIES
        )


def test_shrinkage_runs_from_skill_to_baseline_and_never_zeroes_a_member() -> None:
    skill = {
        "ECMWF_IFS_ENS_OPEN_PF50": 0.7, "NOAA_GEFS_05": 0.3,
        "ECCC_GEM_GLOBAL_ENS": 0.0, "DWD_ICON_GLOBAL_EPS": 0.0,
    }
    assert stable_pool_weights(skill, FAMILIES, kappa=0.0) == pytest.approx(skill)
    assert stable_pool_weights(skill, FAMILIES, kappa=1.0) == pytest.approx(
        {system: 0.25 for system in FOUR}
    )
    half = stable_pool_weights(skill, FAMILIES, kappa=0.5)
    assert half["ECCC_GEM_GLOBAL_ENS"] == pytest.approx(0.125)
    assert half["ECMWF_IFS_ENS_OPEN_PF50"] == pytest.approx(0.475)
    assert sum(half.values()) == pytest.approx(1.0)
    for bad in (-0.1, 1.1, float("nan")):
        with pytest.raises(ContractError, match="kappa_out_of_range"):
            shrink_toward_baseline(
                skill, family_balanced_weights(FOUR, FAMILIES), kappa=bad
            )


def test_a_member_without_a_run_sits_out_and_the_rest_renormalise() -> None:
    weights = {system: 0.25 for system in FOUR}
    three = available_pool_weights(
        weights,
        ["ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05", "DWD_ICON_GLOBAL_EPS"],
        FAMILIES,
    )
    assert three == pytest.approx({
        "DWD_ICON_GLOBAL_EPS": 1 / 3,
        "ECMWF_IFS_ENS_OPEN_PF50": 1 / 3,
        "NOAA_GEFS_05": 1 / 3,
    })
    # One family left is not a P0.
    assert available_pool_weights(
        weights, ["ECMWF_IFS_ENS_OPEN_PF50"], FAMILIES
    ) is None
    with pytest.raises(ContractError, match="weight_keys_mismatch"):
        shrink_toward_baseline(
            {"NOAA_GEFS_05": 1.0},
            {"NOAA_GEFS_05": 0.5, "ECCC_GEM_GLOBAL_ENS": 0.5},
            kappa=0.5,
        )
