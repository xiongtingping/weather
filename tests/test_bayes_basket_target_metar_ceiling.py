from __future__ import annotations

import importlib

import pytest

from bayes_basket_target import metar_ceiling


ORDINALS = {"20°C": 0, "21°C": 1, "22°C": 2, "23°C": 3, "24°C": 4}
# A late-evening posterior of the shape the measurement found: most of the
# mass sitting above the bucket that already holds the day's high.
POSTERIOR = {"20°C": 0.0, "21°C": 0.0, "22°C": 0.30,
             "23°C": 0.45, "24°C": 0.25}
ARTIFACT = {
    "schema": metar_ceiling.SCHEMA,
    "trained_through": "2026-08-01",
    "bins": {
        "0": {"hours_from": 0.0, "hours_to": 6.0,
              "p_still_rose": 0.0, "sample_n": 6277, "backed_off": False},
        "1": {"hours_from": 6.0, "hours_to": 10.0,
              "p_still_rose": 0.20, "sample_n": 5280, "backed_off": False},
        "2": {"hours_from": 10.0, "hours_to": 1e9,
              "p_still_rose": 0.75, "sample_n": 12, "backed_off": True},
    },
}


def _apply(monkeypatch, as_of, enabled=True, max_hours=10.0):
    monkeypatch.setattr(metar_ceiling, "CEILING_ENABLED", enabled)
    monkeypatch.setattr(metar_ceiling, "CEILING_MAX_HOURS_LEFT", max_hours)
    return metar_ceiling.apply_ceiling(
        POSTERIOR,
        ordinals=ORDINALS,
        running_high_bucket="22°C",
        event_date="2026-08-20",
        settlement_timezone="UTC",
        as_of_utc=as_of,
        artifact=ARTIFACT,
    )


def test_disabled_returns_the_posterior_untouched(monkeypatch) -> None:
    """Off has to mean off: every downstream hash depends on it."""

    out, note = _apply(monkeypatch, "2026-08-20T22:00:00.000Z", enabled=False)
    assert out == POSTERIOR
    assert note["metar_ceiling_applied"] == "false"
    assert note["metar_ceiling_skip_reason"] == "DISABLED"


def test_late_evening_moves_the_mass_off_the_dead_side(monkeypatch) -> None:
    """Two hours out, the day cannot warm; the split has to say so."""

    out, note = _apply(monkeypatch, "2026-08-20T22:00:00.000Z")
    assert note["metar_ceiling_applied"] == "true"
    above = out["23°C"] + out["24°C"]
    assert above < 0.01
    assert out["22°C"] > 0.98
    # The shape inside each side is untouched: 23 and 24 keep their ratio.
    assert out["24°C"] / out["23°C"] == pytest.approx(0.25 / 0.45)


def test_too_early_declines_because_the_prior_is_better_there(monkeypatch) -> None:
    """Before the peak the model is already calibrated; measured, correcting
    there costs hit rate instead of buying it."""

    out, note = _apply(monkeypatch, "2026-08-20T06:00:00.000Z")
    assert out == POSTERIOR
    assert note["metar_ceiling_skip_reason"] == "TOO_EARLY"


def test_backed_off_bin_never_speaks(monkeypatch) -> None:
    """A bin that fell back to the pooled rate describes every hour at once."""

    out, note = _apply(monkeypatch, "2026-08-20T08:00:00.000Z", max_hours=99.0)
    assert out == POSTERIOR
    assert note["metar_ceiling_skip_reason"] == "BIN_BACKED_OFF"


def test_event_date_inside_the_fit_is_refused(monkeypatch) -> None:
    """Scoring a day the curve was fitted on is reading its own answer."""

    monkeypatch.setattr(metar_ceiling, "CEILING_ENABLED", True)
    out, note = metar_ceiling.apply_ceiling(
        POSTERIOR, ordinals=ORDINALS, running_high_bucket="22°C",
        event_date="2026-08-01", settlement_timezone="UTC",
        as_of_utc="2026-08-01T22:00:00.000Z", artifact=ARTIFACT,
    )
    assert out == POSTERIOR
    assert note["metar_ceiling_skip_reason"] == "EVENT_DATE_INSIDE_TRAINING"


def test_no_side_is_ever_driven_to_exactly_zero(monkeypatch) -> None:
    """The ceiling is a rate, not a proof about this station-day: a bucket it
    zeroed could not be revived by an observation that did warm."""

    out, _ = _apply(monkeypatch, "2026-08-20T22:00:00.000Z")
    assert all(value > 0.0 for value in (out["23°C"], out["24°C"]))
    assert sum(out.values()) == pytest.approx(1.0)


def test_the_ceiling_speaks_from_ten_local_by_ruling(monkeypatch) -> None:
    """Operator ruling 2026-09-06: the window opens at 14 hours left.

    ZGSZ 2026-09-06 held 80% on 30-32°C through a rainy morning at 25°C with
    the receipt reading TOO_EARLY under the 10-hour window.  The default is
    now 14 (10:00 local); the fitted artifact's 13-16h bin is populated, so
    the layer rescales on it rather than declining.
    """

    importlib.reload(metar_ceiling)
    assert metar_ceiling.CEILING_MAX_HOURS_LEFT == 14.0
    artifact = {
        "schema": metar_ceiling.SCHEMA,
        "trained_through": "2026-08-01",
        "bins": {
            "0": {"hours_from": 0.0, "hours_to": 10.0,
                  "p_still_rose": 0.20, "sample_n": 5280, "backed_off": False},
            "1": {"hours_from": 10.0, "hours_to": 13.0,
                  "p_still_rose": 0.668, "sample_n": 5663, "backed_off": False},
            "2": {"hours_from": 13.0, "hours_to": 16.0,
                  "p_still_rose": 0.859, "sample_n": 3402, "backed_off": False},
            "3": {"hours_from": 16.0, "hours_to": 1e9,
                  "p_still_rose": 0.90, "sample_n": 3918, "backed_off": False},
        },
    }
    monkeypatch.setattr(metar_ceiling, "CEILING_ENABLED", True)
    # 10:30 local on a UTC station: 13.5 hours left -- inside the new window.
    out, note = metar_ceiling.apply_ceiling(
        POSTERIOR, ordinals=ORDINALS, running_high_bucket="22°C",
        event_date="2026-08-20", settlement_timezone="UTC",
        as_of_utc="2026-08-20T10:30:00.000Z", artifact=artifact,
    )
    assert note["metar_ceiling_applied"] == "true"
    assert out["23°C"] + out["24°C"] == pytest.approx(0.859, abs=1e-6)
    # 09:30 local: 14.5 hours left -- still too early.
    out, note = metar_ceiling.apply_ceiling(
        POSTERIOR, ordinals=ORDINALS, running_high_bucket="22°C",
        event_date="2026-08-20", settlement_timezone="UTC",
        as_of_utc="2026-08-20T09:30:00.000Z", artifact=artifact,
    )
    assert note["metar_ceiling_skip_reason"] == "TOO_EARLY"
    assert out == POSTERIOR

