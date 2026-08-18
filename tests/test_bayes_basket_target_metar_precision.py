"""The remark group decides a 2F bucket that a whole degree cannot."""

from __future__ import annotations

import math

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.metar_precision import (
    REMARK_RESOLUTION_C,
    WHOLE_DEGREE_RESOLUTION_C,
    decode_remark_tenths_c,
    official_temperature_c,
    remark_temperature_c,
)
from bayes_basket_target.p0_producer import bucket_intervals_from_registry_rows


KATL = (
    "METAR KATL 141652Z 22006KT 10SM FEW045 SCT250 34/21 A3003"
    " RMK AO2 SLP161 T03440206"
)


def test_remark_group_decodes_signed_tenths() -> None:
    assert decode_remark_tenths_c("0344") == pytest.approx(34.4)
    assert decode_remark_tenths_c("1028") == pytest.approx(-2.8)
    assert decode_remark_tenths_c("0000") == pytest.approx(0.0)
    with pytest.raises(ContractError):
        decode_remark_tenths_c("344")
    with pytest.raises(ContractError):
        decode_remark_tenths_c("2344")


def test_remark_group_is_read_only_from_the_remark_section() -> None:
    assert remark_temperature_c(KATL) == pytest.approx(34.4)
    # The same token in the body is not the remark group and must not be read
    # as one.  Measured over every archived AWC report, no body carries it.
    body_only = "METAR KATL 141652Z 22006KT T03440206 34/21 A3003"
    assert remark_temperature_c(body_only) is None
    assert remark_temperature_c("METAR KATL 141652Z 34/21 A3003") is None


def test_official_temperature_falls_back_without_widening_the_error() -> None:
    assert official_temperature_c(KATL, whole_degree_c=34.0) == (
        pytest.approx(34.4),
        REMARK_RESOLUTION_C,
    )
    plain = "METAR ZBAA 141600Z 34/21 Q1008"
    assert official_temperature_c(plain, whole_degree_c=34.0) == (
        34.0,
        WHOLE_DEGREE_RESOLUTION_C,
    )
    # More than half a degree from the body group is not the same reading, so
    # the coarse-but-agreed value stands rather than a finer disputed one.
    contradiction = "METAR KATL 141652Z 34/21 A3003 RMK AO2 T03000206"
    assert official_temperature_c(contradiction, whole_degree_c=34.0) == (
        34.0,
        WHOLE_DEGREE_RESOLUTION_C,
    )


def _fahrenheit_registry_rows(lower_pairs: list[tuple[int, int]]):
    rows = []
    for index, (lower, upper) in enumerate(lower_pairs):
        rows.append({
            "station_id": "KATL",
            "event_date": "2026-08-14",
            "settlement_unit": "F",
            "outcome": "bucket-%d" % index,
            "registry_json": (
                '{"lower": %s, "upper": %s}'
                % (
                    "null" if lower is None else lower,
                    "null" if upper is None else upper,
                )
            ),
        })
    return rows


def _winner(intervals, truth_c: float) -> str:
    matches = [
        bucket
        for bucket, (lower, upper) in intervals.items()
        if (lower is None or truth_c >= lower)
        and (upper is None or truth_c < upper)
    ]
    assert len(matches) == 1
    return matches[0]


def test_whole_degree_truth_is_bucket_ambiguous_and_tenths_are_not() -> None:
    """KATL 2026-08-14: the day that settled 92-93 and belonged in 94-95."""

    intervals = bucket_intervals_from_registry_rows(
        _fahrenheit_registry_rows([
            (None, 89), (90, 91), (92, 93), (94, 95), (96, None),
        ])
    )
    whole, _resolution = official_temperature_c(
        "METAR KATL 141652Z 34/21 A3003", whole_degree_c=34.0
    )
    precise, _resolution = official_temperature_c(KATL, whole_degree_c=34.0)

    # A whole-degree reading is any true value within half a degree of it, and
    # that interval spans a bucket edge: both answers are consistent with what
    # was recorded, and picking one is a coin flip the code cannot see itself
    # making.
    reachable = {
        _winner(intervals, whole + offset / 100.0)
        for offset in range(-49, 50)
    }
    assert reachable == {"bucket-2", "bucket-3"}

    # The tenth resolves it, and resolves it against the settled answer.
    settled = {
        _winner(intervals, precise + offset / 1000.0)
        for offset in range(-49, 50)
    }
    assert settled == {"bucket-3"}
    assert _winner(intervals, whole) == "bucket-2"
    assert math.isclose(precise * 9.0 / 5.0 + 32.0, 93.92)
