"""The board's view of the resident's maintenance lanes.

Added 2026-09-10 because there was none.  That day three lanes were
sitting at 2.3x to 2.5x their own interval with ``last_state: "ok"``
beside every one -- the clock file has a state for "failed" and none for
"starved", so a lane that never got a turn reads exactly like a healthy
one.  Finding it out meant reading a JSON file by hand and dividing two
numbers, and nothing on the only page anyone looks at said a word.

Three things this section has to get right, all pinned here:

* Lateness is counted in the LANE'S OWN intervals.  The contracts run from
  ten minutes to a day, so a day-long lane nine hours behind is a quarter
  of the way through its window and a three-hour lane nine hours behind
  has missed three turns.  One clock for both is how a healthy lane gets
  reported as a stall.
* Starved and stuck are different findings with opposite repairs.  Not
  attempted = losing the queue, usually to restart churn, and it drains
  itself once a generation lives long enough.  Attempted but not
  completing = taking the slot and failing, which under an exclusive write
  domain holds up everyone behind it (2026-09-09: 72 hours of one capture
  lane doing exactly that).
* The colour agrees with the number printed beside it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

HOUR = 3600.0


def _clock(now, *, completed_h: float, attempted_h: float | None = None,
           state: str = "ok") -> dict:
    from datetime import timedelta

    def stamp(hours: float) -> str:
        return (now - timedelta(hours=hours)).isoformat().replace(
            "+00:00", "Z"
        )

    return {
        "completed_at_utc": stamp(completed_h),
        "attempted_at_utc": stamp(
            completed_h if attempted_h is None else attempted_h
        ),
        "last_state": state,
    }


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """The collector, pointed at a clock file and a status we control."""

    from datetime import datetime, timezone

    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(C, "_now", lambda: now)

    contracts = [
        # name, interval, domain
        ("slow_daily", 24 * HOUR, "ssot"),
        ("three_hourly", 3 * HOUR, "ssot"),
        ("stuck_lane", 3 * HOUR, "ssot"),
        ("hourly_none", HOUR, "none"),
    ]
    monkeypatch.setattr(
        C, "_hosted_sweep_contracts",
        lambda: (
            [
                {"name": n, "script": f"{n}.sh", "interval_sec": i,
                 "timeout_sec": 900.0, "domain": d}
                for n, i, d in contracts
            ],
            "",
        ),
    )
    clocks = {
        # Nine hours behind on a DAY-long contract: 0.38 of a turn.
        "slow_daily": _clock(now, completed_h=9.0),
        # Nine hours behind on a three-hour contract: three turns missed,
        # and never attempted since -- starved.
        "three_hourly": _clock(now, completed_h=9.0),
        # Completed nine hours ago, attempted twenty minutes ago: it is
        # getting its turn and failing.
        "stuck_lane": _clock(
            now, completed_h=9.0, attempted_h=0.33, state="timeout_killed"
        ),
        "hourly_none": _clock(now, completed_h=0.2),
    }
    path = tmp_path / "hosted_sweep_clocks.json"
    path.write_text(json.dumps(clocks), encoding="utf-8")
    monkeypatch.setattr(C, "HOSTED_SWEEP_CLOCKS", path)

    status = tmp_path / "status.json"
    status.write_text(json.dumps({
        "started_at": "2026-09-10T02:00:00Z",
        "hosted_sweeps": {"running": ["hourly_none"], "results": {
            "hourly_none": {"state": "ok", "duration_sec": 0.4,
                            "returncode": 0},
        }},
        "production": {"target_cycle_runtime": {
            "started_at_utc": "2026-09-10T11:52:00Z",
            "elapsed_ms": 200000.0,
        }},
        "production_inflight": {"submitted_at_utc": "2026-09-10T11:58:00Z"},
        "production_wait": {"blocked_by": ["forecast_prefetch"],
                            "waited_sec": 160.0},
    }), encoding="utf-8")
    monkeypatch.setattr(C, "STATUS_JSON", status)
    return C.collect_hosted_sweeps(sqlite3.connect(":memory:"))


def _lane(board, name):
    return next(row for row in board["lanes"] if row["name"] == name)


def test_lateness_is_counted_in_the_lanes_own_intervals(board):
    assert board["available"] is True
    # Same nine hours, two verdicts, because the contracts differ.
    assert _lane(board, "slow_daily")["overdue_intervals"] == pytest.approx(
        9.0 / 24.0
    )
    assert _lane(board, "slow_daily")["health"] == "green"
    assert _lane(board, "three_hourly")["overdue_intervals"] == pytest.approx(
        3.0
    )
    assert _lane(board, "three_hourly")["health"] == "red"


def test_starved_and_stuck_are_told_apart(board):
    starved = _lane(board, "three_hourly")
    stuck = _lane(board, "stuck_lane")
    assert starved["stuck"] is False
    assert "回合" in starved["note"]
    assert stuck["stuck"] is True
    assert stuck["health"] == "red"
    # The failing state is what gets named, because it is the actionable
    # half: a lane that is being killed needs looking at, not waiting out.
    assert "timeout_killed" in stuck["note"]
    # And the pair of ages is what shows it: attempted far newer than
    # completed.
    assert stuck["since_attempted_sec"] < stuck["since_completed_sec"] / 10


def test_the_colour_agrees_with_the_number_beside_it(board, monkeypatch):
    """1.995 renders as "2.00x"; it must not render green next to it."""

    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    stamp = (now - timedelta(seconds=3 * HOUR * 1.995)).isoformat().replace(
        "+00:00", "Z"
    )
    path = Path(str(C.HOSTED_SWEEP_CLOCKS))
    path.write_text(json.dumps({
        "three_hourly": {"completed_at_utc": stamp,
                         "attempted_at_utc": stamp, "last_state": "ok"},
    }), encoding="utf-8")
    again = C.collect_hosted_sweeps(sqlite3.connect(":memory:"))
    lane = _lane(again, "three_hourly")
    assert round(lane["overdue_intervals"], 2) == 2.0
    assert lane["health"] == "amber"


def test_the_queue_arithmetic_is_published_beside_the_symptoms(board):
    # Demand counts SSOT members only -- a lane that writes nothing does
    # not queue for the one exclusive writer.
    assert board["ssot_demand_per_hour"] == pytest.approx(
        1 / 24 + 1 / 3 + 1 / 3
    )
    # Supply is start-to-start, six minutes here.
    assert board["cycle_cadence_sec"] == pytest.approx(360.0)
    assert board["ssot_supply_per_hour"] == pytest.approx(10.0)
    assert board["waiting_on"] == ["forecast_prefetch"]
    # And the bar the generation age is judged against is the slowest
    # contract that is actually late, not a constant.
    assert board["slowest_late_interval_sec"] == pytest.approx(3 * HOUR)


def test_this_generation_is_reported_apart_from_the_clock(board):
    # The clock survives restarts; the results do not.  A lane that has not
    # run in THIS generation says so rather than reading as failed.
    assert board["generation_age_sec"] == pytest.approx(10 * HOUR)
    assert _lane(board, "hourly_none")["this_generation"]["state"] == "ok"
    assert _lane(board, "three_hourly")["this_generation"] is None
    assert board["running_now"] == ["hourly_none"]


def test_the_lanes_reach_the_health_lamp_and_can_say_they_cannot_read(
    monkeypatch,
):
    """The lanes are a lamp now (user ruling, 2026-09-11).

    The eight-column table is gone; the colour and the red/amber counts
    ride on one light, and which lane is starved or stuck rides in its
    tooltip.  What has to stay true is that the collector still ships and
    the lamp board still reads it.
    """

    page = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    assert '[healthLights,"系统状态灯"]' in page
    assert "D.hosted_sweeps" in page
    assert '"hosted_sweeps": hosted_sweeps,' in page
    # A board never fails closed on one section.
    monkeypatch.setattr(
        C, "_hosted_sweep_contracts", lambda: ([], "常驻模块读不出来")
    )
    out = C.collect_hosted_sweeps(sqlite3.connect(":memory:"))
    assert out["available"] is False
    assert out["lanes"] == []
