"""Charter 86: the served P0 distribution is the calibrated members, nothing else.

Until 2026-09-06 each system's forecast was mixed with a histogram of that
scope's own training-window OUTCOMES -- ``n_eff / (n_eff + 10)`` model, the
rest "climatology" -- on a literal 10 that carried no ruling, no fit and no
measurement.  Rebuilding every served P0 vector from its own members and
lineage receipts over 946 station-days showed the mixture cost 0.070 of NLL
and was the worse arm at 39 of 45 stations, worst exactly where it was meant
to help (thin history, where the pseudo-count dominates).

What these tests hold is the shape of the ruling, not one number: the share is
zero by default, the old behaviour survives only as a research arm, and the
climate samples stay in the artifact because the OOS gate scores against them.
"""

from __future__ import annotations

import importlib
from datetime import date, timedelta

import pytest

from bayes_basket_target import native_ensemble_oos as oos


def _cases(count: int = 40) -> list[dict[str, object]]:
    """A window that alternates cool and hot days, forecast correctly each time.

    This is the SAEZ 2026-09-07 shape in miniature, and it has to be built this
    way to isolate the blend: a scope whose forecasts simply ran cold would be
    corrected by the bias term, which moves the members themselves and is a
    different layer.  Here the model is unbiased -- it called every day right --
    so nothing in the calibration moves the members, and the only thing that can
    put mass on a hot bucket on a cool day is the mixture with the window's own
    outcomes.
    """

    cases = []
    start = date(2026, 6, 1)
    for index in range(count):
        center = 21.0 if index % 2 == 0 else 25.0
        cases.append({
            "event_date": (start + timedelta(days=index)).isoformat(),
            "values": [center - 1.0, center, center + 1.0],
            "actual_tmax_c": center + (0.2 if index % 2 == 0 else -0.2),
        })
    return cases


def test_default_serves_the_members_alone() -> None:
    assert oos.CLIMATE_BLEND_PSEUDO_DAYS == 0.0
    parameters = oos._fit_parameters(_cases())
    assert parameters["reliability_weight"] == 1.0


def test_the_climate_samples_stay_in_the_artifact() -> None:
    """Zero share is not zero evidence.

    The OOS gate admits a scope by beating raw climatology, and the serving
    path refuses a parameter set that carries no samples at all, so the column
    keeps its contents; what changed is that serving no longer mixes them in.
    """

    parameters = oos._fit_parameters(_cases())
    assert parameters["climate_samples_c"]
    assert parameters["train_n_effective"] > 0.0


def test_no_mass_lands_on_a_bucket_no_member_named() -> None:
    """The mechanism the ruling is about, on one distribution.

    Today's members sit at 21C, four degrees of kernel away from 25C, so the
    calibrated ensemble puts nothing there.  The window's hot days do sit
    there, and under the mixture they were served as tomorrow's chance.
    """

    parameters = oos._fit_parameters(_cases())
    served = oos._apply_parameters([20.0, 21.0, 22.0], parameters)
    assert max(served, key=served.get) == "21"
    assert served.get("25", 0.0) == 0.0


def test_the_old_behaviour_survives_only_as_a_research_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Charter 80 (8): a clause that cannot be switched off cannot be audited."""

    monkeypatch.setenv("PM_CLIMATE_BLEND_PSEUDO_DAYS", "10")
    reloaded = importlib.reload(oos)
    try:
        assert reloaded.CLIMATE_BLEND_PSEUDO_DAYS == 10.0
        parameters = reloaded._fit_parameters(_cases())
        assert parameters["reliability_weight"] < 1.0
        served = reloaded._apply_parameters(
            [20.0, 21.0, 22.0], parameters
        )
        # The retired behaviour, stated as the thing it did: 7% on a bucket
        # that carries exactly zero member mass.
        assert served.get("25", 0.0) > 0.05
    finally:
        monkeypatch.delenv("PM_CLIMATE_BLEND_PSEUDO_DAYS", raising=False)
        importlib.reload(oos)


def test_a_negative_pseudo_count_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PM_CLIMATE_BLEND_PSEUDO_DAYS", "-1")
    try:
        with pytest.raises(RuntimeError):
            importlib.reload(oos)
    finally:
        monkeypatch.delenv("PM_CLIMATE_BLEND_PSEUDO_DAYS", raising=False)
        importlib.reload(oos)
