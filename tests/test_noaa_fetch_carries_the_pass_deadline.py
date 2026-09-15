"""The CONUS lane's fetches stop on the pass clock, not only between runs.

``capture_noaa_conus_forward`` checked its seconds budgets between runs and
between station-days.  One HRRR run is 49 forecast hours of an idx read and
a ranged read each, every one allowed three attempts against a 45-second
between-bytes timeout -- so a slow archive could carry a single run past the
1,800-second subprocess budget.  Measured 2026-09-14: ``timeout_killed`` at
03:15Z with nothing written and the exclusive SSOT domain held for the half
hour.  The MSM lane closed the same gap on 2026-09-11
(test_jma_range_carries_the_pass_deadline); this one had been left on the bet.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import noaa_conus_archive as archive  # noqa: E402
from bayes_basket_target import regional_point_lane as lane  # noqa: E402
from bayes_basket_target.jma_msm_archive import JmaMsmPassDeadlineExceeded  # noqa: E402
from bayes_basket_target.noaa_conus_archive import NoaaConusPassDeadlineExceeded  # noqa: E402
from bayes_basket_target.pass_deadline import (  # noqa: E402
    PassDeadlineExceeded,
    pass_deadline,
    seconds_left,
    set_pass_deadline,
)

URL = "https://example.invalid/hrrr.grib2"


class _Client:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls = 0
        self.raises = raises

    def get(self, url, headers=None):  # noqa: ARG002
        self.calls += 1
        if self.raises:
            raise httpx.ReadTimeout("trickle")
        return httpx.Response(206 if headers else 200, content=b"0123456789")


@pytest.fixture(autouse=True)
def _unarmed_after():
    yield
    set_pass_deadline(None)


@pytest.mark.parametrize("call", [
    lambda: archive._get(URL + ".idx"),
    lambda: archive._range_get(URL, 0, 10),
])
def test_an_unarmed_pass_behaves_exactly_as_before(monkeypatch, call) -> None:
    client = _Client()
    monkeypatch.setattr(archive, "_client", lambda: client)
    assert call() == b"0123456789"
    assert client.calls == 1


@pytest.mark.parametrize("call", [
    lambda: archive._get(URL + ".idx"),
    lambda: archive._range_get(URL, 0, 10),
])
def test_a_spent_clock_refuses_before_dialling(monkeypatch, call) -> None:
    client = _Client()
    monkeypatch.setattr(archive, "_client", lambda: client)
    with pass_deadline(0.0):
        with pytest.raises(NoaaConusPassDeadlineExceeded):
            call()
    assert client.calls == 0


def test_a_failed_attempt_is_not_retried_once_the_clock_is_gone(monkeypatch) -> None:
    client = _Client(raises=True)

    def slow_failure():
        set_pass_deadline(0.0)  # the attempt itself used up the clock
        return client

    monkeypatch.setattr(archive, "_client", slow_failure)
    with pytest.raises(NoaaConusPassDeadlineExceeded):
        archive._range_get(URL, 0, 10)
    assert client.calls == 1


def test_every_lane_raises_the_one_exception_the_shared_loop_stops_on() -> None:
    assert issubclass(NoaaConusPassDeadlineExceeded, PassDeadlineExceeded)
    assert issubclass(JmaMsmPassDeadlineExceeded, PassDeadlineExceeded)


def test_the_p1_loop_stops_on_a_conus_clock_and_does_not_call_it_a_failure(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    run_init = datetime(2026, 9, 14, 0, tzinfo=timezone.utc)
    target = {
        "station_id": "KORD", "event_date": date(2026, 9, 14), "utc_offset_hours": -5,
        "latitude": 41.98, "longitude": -87.9, "settlement_timezone": "America/Chicago",
    }
    monkeypatch.setattr(lane, "plan_p1", lambda *_a, **_k: [
        {"run_init": run_init, "targets": [target]},
        {"run_init": run_init.replace(hour=12), "targets": [target]},
    ])
    calls: list[datetime] = []

    def spent(*, product, run_init, targets):  # noqa: ARG001
        calls.append(run_init)
        raise NoaaConusPassDeadlineExceeded("noaa_conus_archive_pass_deadline_exceeded")

    monkeypatch.setattr(lane, "fetch_noaa_run_for_targets", spent)
    receipt = lane.capture_p1(
        conn, product="HRRR", now=run_init, stations=[], max_runs=3, budget_sec=240.0,
    )
    assert receipt["budget_exhausted"] is True
    assert receipt["failed"] == []
    assert len(calls) == 1


def _script():
    spec = importlib.util.spec_from_file_location(
        "_noaa_capture_under_test", ROOT / "scripts" / "capture_noaa_conus_forward.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_phase_of_the_pass_fetches_under_its_own_armed_budget(tmp_path, monkeypatch, capsys) -> None:
    script = _script()
    monkeypatch.setattr(script, "DB", tmp_path / "ssot.db")
    import bayes_basket_target.native_ensemble_training_facts as facts

    monkeypatch.setattr(facts, "register_regional_point_systems", lambda conn: None)
    monkeypatch.setattr(lane, "ensure_schema", lambda conn: True)
    p1_clocks: list[float | None] = []

    def fake_p1(conn, *, product, **_kwargs):  # noqa: ARG001
        p1_clocks.append(seconds_left())
        return {"runs_planned": 0, "runs_attempted": 0, "written": 0, "existed": 0,
                "empty": 0, "failed": [], "budget_exhausted": False}

    monkeypatch.setattr(lane, "capture_p1", fake_p1)
    monkeypatch.setattr(script, "_transport_is_writable", lambda conn, transport: (True, ""))
    item = {
        "product": "HRRR", "station": "KORD", "target": date(2026, 9, 14), "cycle": "00Z",
        "bucket": "d0", "run": datetime(2026, 9, 14, tzinfo=timezone.utc), "offset_hours": -5,
        "latitude": 41.98, "longitude": -87.9, "system_id": "x", "init": "2026-09-14T00:00:00Z",
    }
    monkeypatch.setattr(script, "_plan", lambda conn, now: ([item, dict(item, station="KMDW")], 0))
    loop_clocks: list[float | None] = []

    def spent_inside(**_kwargs):
        loop_clocks.append(seconds_left())
        raise NoaaConusPassDeadlineExceeded("noaa_conus_archive_pass_deadline_exceeded")

    monkeypatch.setattr(script, "fetch_day_maximum", spent_inside)
    monkeypatch.setattr(sys, "argv", ["capture_noaa_conus_forward.py"])

    assert script.main() == 0

    assert len(p1_clocks) == 2
    for clock in p1_clocks:
        assert clock is not None and script.P1_BUDGET_SEC_PER_PRODUCT - 5 < clock <= script.P1_BUDGET_SEC_PER_PRODUCT
    # The loop's fetch ran under the loop's own budget, and a spent clock
    # inside it ended the pass as a spent budget: one fetch, no failure line.
    assert len(loop_clocks) == 1
    assert script.BUDGET_SEC_PER_PASS - 5 < loop_clocks[0] <= script.BUDGET_SEC_PER_PASS
    out = capsys.readouterr().out
    assert "时间预算用尽" in out
    assert "失败/不全 0" in out


class _Clock:
    """A monotonic clock the fake phases can spend, shared by both modules."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _run_pass_spending(tmp_path, monkeypatch, *, p1_seconds: float):
    """One pass whose P1 phases each spend ``p1_seconds`` of the pass clock."""

    import types

    import bayes_basket_target.native_ensemble_training_facts as facts
    import bayes_basket_target.pass_deadline as pd

    script = _script()
    clock = _Clock()
    fake_time = types.SimpleNamespace(monotonic=clock.monotonic)
    monkeypatch.setattr(script, "time", fake_time)
    monkeypatch.setattr(pd, "time", fake_time)
    monkeypatch.setattr(script, "DB", tmp_path / "ssot.db")
    monkeypatch.setattr(facts, "register_regional_point_systems", lambda conn: None)
    monkeypatch.setattr(lane, "ensure_schema", lambda conn: True)
    p1_budgets: list[float] = []

    def spending_p1(conn, *, product, budget_sec, **_kwargs):  # noqa: ARG001
        p1_budgets.append(budget_sec)
        clock.now += p1_seconds
        return {"runs_planned": 1, "runs_attempted": 1, "written": 0, "existed": 0,
                "empty": 0, "failed": [], "budget_exhausted": True}

    monkeypatch.setattr(lane, "capture_p1", spending_p1)
    monkeypatch.setattr(script, "_transport_is_writable", lambda conn, transport: (True, ""))
    item = {
        "product": "HRRR", "station": "KORD", "target": date(2026, 9, 14), "cycle": "00Z",
        "bucket": "d0", "run": datetime(2026, 9, 14, tzinfo=timezone.utc), "offset_hours": -5,
        "latitude": 41.98, "longitude": -87.9, "system_id": "x", "init": "2026-09-14T00:00:00Z",
    }
    monkeypatch.setattr(script, "_plan", lambda conn, now: ([item], 0))
    loop_clocks: list[float | None] = []

    def spent_inside(**_kwargs):
        loop_clocks.append(seconds_left())
        raise NoaaConusPassDeadlineExceeded("noaa_conus_archive_pass_deadline_exceeded")

    monkeypatch.setattr(script, "fetch_day_maximum", spent_inside)
    monkeypatch.setattr(sys, "argv", ["capture_noaa_conus_forward.py"])
    assert script.main() == 0
    return script, p1_budgets, loop_clocks


def test_the_loop_gets_what_the_p1_phases_left_not_a_fresh_budget(
    tmp_path, monkeypatch, capsys
) -> None:
    """2026-09-15: three phases each counting from zero could spend 1,380s by
    budget alone under an 1,800s kill, and the 03:04Z pass was killed."""

    script, p1_budgets, loop_clocks = _run_pass_spending(
        tmp_path, monkeypatch, p1_seconds=300.0
    )
    assert p1_budgets == [script.P1_BUDGET_SEC_PER_PRODUCT] * 2
    assert len(loop_clocks) == 1
    assert loop_clocks[0] == pytest.approx(script.BUDGET_SEC_PER_PASS - 600.0)


def test_a_pass_the_p1_phases_used_up_starts_no_run(
    tmp_path, monkeypatch, capsys
) -> None:
    script, p1_budgets, loop_clocks = _run_pass_spending(
        tmp_path, monkeypatch, p1_seconds=700.0
    )
    assert len(p1_budgets) == 2          # 440s were left for the second
    assert loop_clocks == []             # 1,400s spent: no run is started
    assert "时间预算用尽" in capsys.readouterr().out


def test_a_phase_with_nothing_left_is_skipped_not_given_a_budget(
    tmp_path, monkeypatch, capsys
) -> None:
    script, p1_budgets, loop_clocks = _run_pass_spending(
        tmp_path, monkeypatch, p1_seconds=1200.0
    )
    assert p1_budgets == [script.P1_BUDGET_SEC_PER_PRODUCT]
    assert "P1 NBM: 本轮时间预算已用尽" in capsys.readouterr().out


def test_the_icon_eu_run_loop_counts_from_the_pass_not_from_its_own_start(
    tmp_path, monkeypatch, capsys
) -> None:
    """Same shape as the NOAA lane: the P1 pass's seconds are the loop's."""

    import types

    import bayes_basket_target.native_ensemble_training_facts as facts

    spec = importlib.util.spec_from_file_location(
        "_icon_eu_capture_under_test",
        ROOT / "scripts" / "capture_dwd_icon_eu_forward.py",
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    clock = _Clock()
    monkeypatch.setattr(script, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(script, "DB", tmp_path / "ssot.db")
    monkeypatch.setattr(facts, "register_regional_point_systems", lambda conn: None)
    monkeypatch.setattr(lane, "ensure_schema", lambda conn: True)
    p1_budgets: list[float] = []

    def spending_p1(conn, *, budget_sec, **_kwargs):  # noqa: ARG001
        p1_budgets.append(budget_sec)
        clock.now += script.BUDGET_SEC_PER_PASS + 1.0
        return {"runs_planned": 1, "runs_attempted": 1, "written": 0, "existed": 0,
                "empty": 0, "failed": [], "budget_exhausted": True}

    monkeypatch.setattr(lane, "capture_p1", spending_p1)
    monkeypatch.setattr(script, "_transport_is_writable", lambda conn: (True, ""))
    monkeypatch.setattr(script, "_held_buckets", lambda conn, station, event: set())
    fetched: list = []
    monkeypatch.setattr(
        script, "fetch_run_day_maxima",
        lambda **kw: fetched.append(kw) or {},
    )
    monkeypatch.setattr(sys, "argv", ["capture_dwd_icon_eu_forward.py"])

    assert script.main() == 0
    assert p1_budgets == [script.P1_BUDGET_SEC]
    assert fetched == []
