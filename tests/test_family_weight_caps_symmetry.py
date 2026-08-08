"""Family weights come from measured skill, not from a per-provider constant.

Each family's share is its summed system score over the total; a cap only binds
when that share would otherwise exceed it, and exists to preserve ensemble
diversity -- a property of the combination, not of any one provider.

ECMWF previously carried a hardcoded 0.45 while every other family used the
0.75 default.  At ZUUU its measured share exceeded 0.45, so it was clipped to
exactly the cap and the remainder fell to GEFS: the 0.45/0.55 split described
the cap rather than relative skill, and with only two candidate systems that
asymmetry set the weights outright.
"""

import pytest

from bayes_basket_target.native_ensemble_contract import (
    validate_cross_native_ensemble_weights,
)
from bayes_basket_target.native_ensemble_oos import DEFAULT_FAMILY_WEIGHT_CAPS


def test_no_family_carries_a_bespoke_default_cap() -> None:
    # Any entry here singles out one provider; the diversity bound must apply
    # to every family equally.
    assert DEFAULT_FAMILY_WEIGHT_CAPS == {}


def test_ecmwf_specifically_is_not_pre_capped() -> None:
    assert "ECMWF" not in DEFAULT_FAMILY_WEIGHT_CAPS


def test_cap_mechanism_itself_is_retained() -> None:
    # Removing the bespoke value must not remove the ability to cap: an
    # explicit cap passed by a caller still binds.
    weights = validate_cross_native_ensemble_weights(
        system_weights={"A_SYS": 0.9, "B_SYS": 0.1},
        systems_by_id={
            "A_SYS": {"independence_family_id": "A_FAM"},
            "B_SYS": {"independence_family_id": "B_FAM"},
        },
        family_weight_caps={"A_FAM": 0.9, "B_FAM": 0.9},
    )

    assert weights["family_weights"]["A_FAM"] == pytest.approx(0.9)


def test_skill_ordering_survives_without_the_bespoke_cap() -> None:
    # With symmetric caps the stronger family keeps the larger share instead of
    # being clipped down to a constant.
    weights = validate_cross_native_ensemble_weights(
        system_weights={"A_SYS": 0.7, "B_SYS": 0.3},
        systems_by_id={
            "A_SYS": {"independence_family_id": "A_FAM"},
            "B_SYS": {"independence_family_id": "B_FAM"},
        },
        family_weight_caps={"A_FAM": 0.75, "B_FAM": 0.75},
    )
    families = weights["family_weights"]

    assert families["A_FAM"] > families["B_FAM"]
