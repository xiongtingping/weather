"""A cross cohort forms on a shared holdout window, not only an identical one.

Every system's own holdout is its latest few settled days, and the cohort rule
required every member's set to be the same days.  One missing capture shifts
a set by one, so on a three-day window four systems seldom lined up: measured
2026-09-10 across 45 stations, 88 of 118 four-system groups failed the rule
while sharing four or five settled days in their latest six.  That is why the
contest seats a pair 92-97% of the time, and why 41% of the sets in service
were rows from the last day their systems happened to line up.

Each system now also exposes a longer window predicted from a fit trained
before all of it, and a cohort forms when members share at least
``min_holdout_days`` exposed days.  Strictly additive: exact cohorts are
offered first and unchanged, and a member scored on a day older than its own
holdout is scored from the longer fit, never from the one that trained on it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402

N = 3
SYSTEMS = {
    "A": {"independence_family_id": "fa"},
    "B": {"independence_family_id": "fb"},
    "C": {"independence_family_id": "fc"},
    "D": {"independence_family_id": "fd"},
}


def _pred(day: str, tag: str) -> dict:
    return {"event_date": day, "probability": {"20": 1.0}, "tag": tag}


def _evaluation(own: list[str], exposed: list[str] | None, *, provider="ECMWF_OPEN_DATA_S3") -> dict:
    ev = {
        "state": "OOS_READY",
        "transport_provider_id": provider,
        "predictions": [_pred(d, "gate") for d in own],
        "parameters": {"train_rmse_c": 1.0, "train_residual_sigma_c": 0.9},
        "train_end_date": "2026-09-05",
        "train_n": 20,
        "cross_predictions": None,
        "cross_parameters": None,
        "cross_train_end_date": None,
        "cross_train_n": 0,
    }
    if exposed is not None:
        ev["cross_predictions"] = [_pred(d, "window") for d in exposed]
        ev["cross_parameters"] = {"train_rmse_c": 1.2, "train_residual_sigma_c": 1.1}
        ev["cross_train_end_date"] = "2026-09-02"
        ev["cross_train_n"] = 17
    return ev


def _row(system: str, own: list[str], exposed: list[str] | None):
    return (system, "00Z", _evaluation(own, exposed))


D = [f"2026-09-{day:02d}" for day in range(1, 10)]   # 09-01 .. 09-09


def test_an_identical_window_still_forms_the_exact_cohort_first() -> None:
    rows = [_row("A", D[6:9], D[3:9]), _row("B", D[6:9], D[3:9])]
    cohorts = oos._eligible_exact_shared_holdout_cohorts(
        ready_rows=rows, systems=SYSTEMS, min_holdout_days=N,
    )
    assert cohorts[0][0] == tuple(D[6:9])
    assert sorted(r[0] for r in cohorts[0][1]) == ["A", "B"]


def test_one_missing_day_no_longer_splits_the_group() -> None:
    """The KAUS shape: ECCC lacks 09-08, everyone else has it."""

    rows = [
        _row("A", D[6:9], D[3:9]),                   # 07 08 09
        _row("B", D[6:9], D[3:9]),
        _row("C", D[6:9], D[3:9]),
        _row("D", [D[5], D[6], D[8]], [D[2], D[3], D[4], D[5], D[6], D[8]]),  # 06 07 09
    ]
    cohorts = oos._eligible_exact_shared_holdout_cohorts(
        ready_rows=rows, systems=SYSTEMS, min_holdout_days=N,
    )
    shapes = [(sorted(r[0] for r in c[1]), c[0]) for c in cohorts]
    # The exact cohort of the three aligned systems comes first, unchanged.
    assert shapes[0] == (["A", "B", "C"], tuple(D[6:9]))
    # And all four form on their shared window.  A, B, C expose 04..09 and
    # D exposes 03, 04, 05, 06, 07, 09; common is 04, 05, 06, 07, 09, and
    # the cohort holds out the newest three of those: 06, 07, 09.
    quad = [s for s in shapes if s[0] == ["A", "B", "C", "D"]]
    assert quad, shapes
    assert quad[0][1] == (D[5], D[6], D[8])


def test_the_window_holdout_is_the_newest_common_days() -> None:
    rows = [
        _row("A", D[6:9], D[3:9]),          # exposed 04..09
        _row("B", D[6:9], D[3:9]),
        _row("C", [D[4], D[5], D[8]], [D[1], D[2], D[3], D[4], D[5], D[8]]),  # exposed 02..06, 09
    ]
    cohorts = oos._eligible_exact_shared_holdout_cohorts(
        ready_rows=rows, systems=SYSTEMS, min_holdout_days=N,
    )
    triple = [c for c in cohorts if sorted(r[0] for r in c[1]) == ["A", "B", "C"]]
    assert triple
    # common exposed: 04, 05, 06, 09 -> newest three: 05, 06, 09
    assert triple[0][0] == (D[4], D[5], D[8])


def test_without_exposure_the_rule_is_exactly_the_old_one() -> None:
    rows = [
        _row("A", D[6:9], None),
        _row("B", D[6:9], None),
        _row("C", [D[5], D[6], D[8]], None),
    ]
    cohorts = oos._eligible_exact_shared_holdout_cohorts(
        ready_rows=rows, systems=SYSTEMS, min_holdout_days=N,
    )
    assert [sorted(r[0] for r in c[1]) for c in cohorts] == [["A", "B"]]


def test_a_member_whose_own_window_covers_the_holdout_is_scored_as_is() -> None:
    ev = _evaluation(D[6:9], D[3:9])
    view = oos._cohort_view(ev, tuple(D[6:9]))
    assert view is ev


def test_a_member_scored_on_an_older_day_uses_the_walk_forward_fit() -> None:
    ev = _evaluation(D[6:9], D[3:9])
    view = oos._cohort_view(ev, (D[4], D[5], D[6]))
    assert view is not ev
    assert {p["tag"] for p in view["predictions"]} == {"window"}
    assert view["parameters"]["train_rmse_c"] == 1.2
    assert view["train_end_date"] == "2026-09-02"
    assert view["train_n"] == 17
    assert view["cohort_fit_basis"] == "walk_forward_window"
    # The original is untouched: it is what a winner persists.
    assert ev["parameters"]["train_rmse_c"] == 1.0


def test_a_day_no_fit_predates_fails_closed() -> None:
    ev = _evaluation(D[6:9], D[3:9])
    with pytest.raises(ContractError, match="cohort_day_not_exposed"):
        oos._cohort_view(ev, (D[0], D[1], D[2]))


def test_the_pool_is_bounded() -> None:
    assert oos.MAX_WINDOW_COHORT_SYSTEMS <= 10
    assert oos.CROSS_HOLDOUT_SLACK_DAYS == 3
