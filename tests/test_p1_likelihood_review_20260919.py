"""The 2026-09-19 review of P1's joint observation likelihood (charter 150).

1. An open top ("33C or higher") got a finite upper edge of floor + one rung,
   so a day that climbed a rung past it excluded every bucket and P1 refused
   with ``hard_bound_exhausts_support`` (20 station-days, VILK 09-16 among
   them).  The open top's edge is None: unbounded, as P0/P0-M always read it.
"""
from __future__ import annotations

import json

import pytest

from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.metar_trajectory import hard_bound_excluded_buckets
from bayes_basket_target.p1_compiler import _bucket_geometry_c, compile_station_p1_packets
from tests.test_bayes_basket_target_p1_bucket_geometry import F_INTERVALS, F_UPPER_LABELS, _root
from tests.test_bayes_basket_target_materialization import _envelope, _p0m_root
from tests.test_charter_150_p1_joint_likelihood import _artifact, _observation, _registry


def _open_top_root() -> ProbabilityEnvelope:
    base = _p0m_root(_envelope())
    return ProbabilityEnvelope.from_mapping({
        **base.to_dict(),
        "lineage": {
            **base.lineage,
            "bucket_intervals_c_json": json.dumps(
                {"37": [36.5, 37.5], "38": [37.5, 38.5], "39": [38.5, None]}
            ),
        },
    })


@pytest.mark.parametrize("high", [38.6, 39.5, 40.0, 44.0])
def test_a_day_past_the_open_top_keeps_the_top_bucket(high) -> None:
    root = _open_top_root()
    packets = compile_station_p1_packets(
        predecessor=root,
        observations=[_observation(root, "a", "2026-07-23T15:00:00Z", high)],
        registry_rows=_registry(root),
        compilation_at_utc="2026-07-23T15:00:00Z",
        observation_likelihood_artifact=_artifact(),
    )
    assert packets, "P1 refused a day above its open top"
    packet = packets[-1]
    assert packet["bucket_upper_bounds_c"]["39"] is None
    assert sorted(packet["hard_excluded_buckets"]) == ["37", "38"]
    envelope = packet["expected_envelope"]
    assert envelope["probabilities"]["39"] == pytest.approx(1.0)


def test_the_f_ladder_s_open_top_is_unbounded_too() -> None:
    _, uppers = _bucket_geometry_c(
        _root({"bucket_intervals_c_json": json.dumps(F_INTERVALS)}),
        unit="F", upper_by_bucket=F_UPPER_LABELS,
    )
    ordered = list(F_INTERVALS)
    top = ordered[-1]
    assert uppers[top] is None
    # Two rungs past the top's floor: everything but the top is out.
    high = F_INTERVALS[top][0] + 2 * 10.0 / 9.0
    assert hard_bound_excluded_buckets(ordered, uppers, high) == tuple(ordered[:-1])


def test_the_registry_ladder_s_open_top_is_unbounded_too() -> None:
    root = _p0m_root(_envelope())
    bare = ProbabilityEnvelope.from_mapping({
        **root.to_dict(),
        "lineage": {k: v for k, v in root.lineage.items() if k != "bucket_intervals_c_json"},
    })
    _, uppers = _bucket_geometry_c(
        bare, unit="C", upper_by_bucket={"37": 37.0, "38": 38.0, "39": None},
    )
    assert uppers == {"37": 38.0, "38": 39.0, "39": None}
    assert hard_bound_excluded_buckets(["37", "38", "39"], uppers, 45.0) == ("37", "38")
