"""The stall reading must not live inside the lane it measures.

``nwp_sweep_health`` is published from inside
``run_target_forecast_refresh_cycle``, and that function is reached only when
the forecast lane wins a writer turn against the production cycle and the
hosted sweeps.  So the one state it cannot report is a lane that has stopped
winning turns -- it reports that by being absent, and an absent receipt
contributes no alert at all.

Measured 2026-09-09: the resident restarted at 12:35:22Z and eighteen minutes
later the lane had taken no turn, ``sweep_health`` unpublished and the cursor
unset, while 2026-09-10's 45 packets stayed expired and the decision loop
lost 41 scopes a cycle to ``latest_stage_stale_fail_closed`` three layers
downstream.  The rotation state file said it plainly the whole time:
``updated_at_utc`` had not moved for sixty-one minutes against a ten-minute
nominal poll.

This is the second card in one day to key on a receipt that dies with the
process; the first was the TAF capture card, two hours earlier, and the
remedy is the same -- read the file that outlives it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "calibration_dashboard_for_test", ROOT / "scripts" / "calibration_dashboard.py"
)
dashboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dashboard)


@pytest.fixture()
def rotation_file(tmp_path, monkeypatch):
    path = tmp_path / "rotation_state.json"
    monkeypatch.setattr(dashboard, "NWP_ROTATION_STATE", path)
    return path


def _write(path: Path, minutes_ago: float) -> None:
    stamp = dashboard._now() - timedelta(minutes=minutes_ago)
    path.write_text(json.dumps({
        "updated_at_utc": stamp.isoformat().replace("+00:00", "Z"),
        "catalog_scope_count": 381,
    }))


def test_a_stopped_rotation_is_read_with_no_receipt_at_all(rotation_file):
    """The failure mode: nothing published, because nothing ran."""

    _write(rotation_file, 61.0)
    card = dashboard._nwp_rotation_health({})
    assert card["nwp_rotation_health"] == "red", card
    assert "没有推进" in card["nwp_rotation_note"]
    assert card["nwp_rotation_age_minutes"] > 60.0


def test_a_lane_taking_its_turns_is_green(rotation_file):
    _write(rotation_file, 8.0)
    assert dashboard._nwp_rotation_health({})["nwp_rotation_health"] == "green"


def test_three_missed_turns_is_amber_and_six_is_red(rotation_file):
    _write(rotation_file, 31.0)
    assert dashboard._nwp_rotation_health({})["nwp_rotation_health"] == "amber"
    _write(rotation_file, 61.0)
    assert dashboard._nwp_rotation_health({})["nwp_rotation_health"] == "red"


def test_the_lane_receipt_adds_the_lap_but_is_never_required(rotation_file):
    """The receipt enriches the card; its absence must not silence it."""

    _write(rotation_file, 8.0)
    with_receipt = dashboard._nwp_rotation_health({
        "forecast_prefetch": {"resident_nwp_capture": {"sweep_health": {
            "sweep_achieved_hours": 2.7,
            "p0_freshness_hours": 6.0,
            "contention_factor": 2.7,
            "poll_interval_sample_count": 4,
            "fits_freshness_window": True,
        }}}
    })
    assert "一圈实测 2.7h" in with_receipt["nwp_rotation_note"]
    assert with_receipt["nwp_rotation_health"] == "green"


def test_a_lap_that_no_longer_fits_raises_a_green_card(rotation_file):
    """Turns are being taken, and the lap they add up to is too long."""

    _write(rotation_file, 8.0)
    card = dashboard._nwp_rotation_health({
        "forecast_prefetch": {"resident_nwp_capture": {"sweep_health": {
            "sweep_achieved_hours": 4.5,
            "p0_freshness_hours": 6.0,
            "contention_factor": 4.5,
            "poll_interval_sample_count": 4,
            "fits_freshness_window": False,
        }}}
    })
    assert card["nwp_rotation_health"] == "amber", card


def test_a_missing_rotation_file_says_nothing_rather_than_green(rotation_file):
    """No file is not evidence of health; the card stands down instead."""

    assert dashboard._nwp_rotation_health({}) == {}


def test_a_lap_with_no_samples_is_not_called_measured(rotation_file):
    """With no interval samples the reading falls back to the nominal 600s.

    Presenting that as 实测 would put the declaration where the measurement
    belongs -- the substitution this whole card was built after, and the one
    the receipt itself made for its first two hours.  Samples need two polls
    inside one process, so every restart starts here.
    """

    _write(rotation_file, 8.0)
    card = dashboard._nwp_rotation_health({
        "forecast_prefetch": {"resident_nwp_capture": {"sweep_health": {
            "sweep_achieved_hours": 1.0,
            "p0_freshness_hours": 6.0,
            "contention_factor": 1.0,
            "poll_interval_sample_count": 0,
            "fits_freshness_window": True,
        }}}
    })
    assert "按名义推算 1.0h" in card["nwp_rotation_note"], card
    assert "实测" not in card["nwp_rotation_note"].split("按名义推算")[0][-6:]
    assert "尚无实测轮次间隔" in card["nwp_rotation_note"]


def test_a_fallback_lap_never_raises_the_card(rotation_file):
    """It cannot fail a bound it never measured."""

    _write(rotation_file, 8.0)
    card = dashboard._nwp_rotation_health({
        "forecast_prefetch": {"resident_nwp_capture": {"sweep_health": {
            "sweep_achieved_hours": 9.0,
            "p0_freshness_hours": 6.0,
            "contention_factor": 1.0,
            "poll_interval_sample_count": 0,
            "fits_freshness_window": False,
        }}}
    })
    assert card["nwp_rotation_health"] == "green", card
