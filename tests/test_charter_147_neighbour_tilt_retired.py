"""Charter 147 (2026-09-18): the neighbour-direction tilt is retired.

Operator ruling: 去掉并下线，不删代码 -- a hand-fitted constant stamped into
code is not a rule.  Identity by default on every path; the research arm
restores the table; a new certificate no longer names the tag.
"""

from __future__ import annotations

import pytest

from bayes_basket_target import cross_consensus
from bayes_basket_target.cross_consensus import (
    NEIGHBOUR_DIRECTION_METHOD_TAG,
    NEIGHBOUR_DIRECTION_RETIRED_STATE,
    apply_neighbour_direction_tilt,
    neighbour_direction_method_tag_suffix,
    neighbour_direction_serves,
)

GRID = [f"{t}°C" for t in range(20, 31)]
ORDINALS = {key: index for index, key in enumerate(GRID)}
VECTOR = {key: 0.0 for key in GRID}
VECTOR.update({"23°C": 0.08, "24°C": 0.14, "25°C": 0.27, "26°C": 0.28, "27°C": 0.14, "28°C": 0.09})


def test_default_mode_is_off() -> None:
    assert cross_consensus.NEIGHBOUR_DIRECTION_MODE == "off"


@pytest.mark.parametrize("center_bucket", ["25°C", "26°C"])
@pytest.mark.parametrize("latitude", [None, 30.576, 55.0])
def test_retired_tilt_is_identity_whatever_the_anchor_or_band(
    center_bucket: str, latitude: float | None
) -> None:
    served, receipt = apply_neighbour_direction_tilt(
        VECTOR, ordinals=ORDINALS, center_bucket=center_bucket, latitude=latitude
    )
    assert served == pytest.approx(VECTOR, abs=1e-15)
    assert receipt["state"] == NEIGHBOUR_DIRECTION_RETIRED_STATE
    assert receipt["applied_factors"] == {}
    assert receipt["method_tag"] == NEIGHBOUR_DIRECTION_METHOD_TAG
    assert receipt["center_bucket"] == center_bucket


def test_retired_identity_still_renormalises_an_unnormalised_vector() -> None:
    doubled = {key: value * 2.0 for key, value in VECTOR.items()}
    served, _receipt = apply_neighbour_direction_tilt(
        doubled, ordinals=ORDINALS, center_bucket="26°C", latitude=30.0
    )
    assert sum(served.values()) == pytest.approx(1.0, abs=1e-12)
    assert served == pytest.approx(VECTOR, abs=1e-15)


def test_research_arm_restores_the_charter_43_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cross_consensus, "NEIGHBOUR_DIRECTION_MODE", "on")
    served, receipt = apply_neighbour_direction_tilt(
        VECTOR, ordinals=ORDINALS, center_bucket="26°C", latitude=30.576
    )
    assert receipt["state"] == "APPLIED"
    assert receipt["applied_factors"] == {"24°C": 0.867, "25°C": 0.980, "27°C": 1.030}
    # the -2 bucket lost mass, the +1 bucket gained it, the anchor is untouched
    assert served["24°C"] < VECTOR["24°C"]
    assert served["27°C"] > VECTOR["27°C"]
    assert sum(served.values()) == pytest.approx(1.0, abs=1e-12)


def test_retired_p0_runs_the_receipt_for_every_artifact() -> None:
    assert neighbour_direction_serves("") is True
    assert neighbour_direction_serves("bridge_v1::radiation_shift_sigma=0.15") is True


def test_research_arm_keys_on_the_artifact_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cross_consensus, "NEIGHBOUR_DIRECTION_MODE", "on")
    assert neighbour_direction_serves("") is False
    assert neighbour_direction_serves("x::" + NEIGHBOUR_DIRECTION_METHOD_TAG + "::y") is True


def test_certificate_names_the_tag_only_on_the_research_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert neighbour_direction_method_tag_suffix() == ""
    monkeypatch.setattr(cross_consensus, "NEIGHBOUR_DIRECTION_MODE", "on")
    assert neighbour_direction_method_tag_suffix() == "::" + NEIGHBOUR_DIRECTION_METHOD_TAG
