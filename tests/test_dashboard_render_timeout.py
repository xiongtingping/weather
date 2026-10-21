"""The dashboard render has a deadline (2026-09-23, user ruling 「加超时」).

A render is a reader nothing waits on, and until today that was the reason
it had no timeout.  Then one sat five hours in disk wait under
``taskpolicy -b``, holding its read snapshot of the store: the WAL could not
reset, grew from 64 MB to 14.9 GB, filled the disk, and the fleet produced
nothing for over an hour.  These tests run the real sweep script against a
stub render and check the deadline does what the incident needed: the stub is
killed, the lock is released for the next interval, no redraw follows a kill,
and a render that finishes in time is untouched.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SWEEP = ROOT / "scripts" / "dashboard_render_sweep.sh"
PLIST = Path.home() / "Library" / "LaunchAgents" / "com.weather.dashboard_render.plist"


def _project(tmp_path: Path, *, render_seconds: float) -> Path:
    """A throwaway project tree the sweep resolves itself against."""

    project = tmp_path / "project"
    (project / "scripts" / "lib").mkdir(parents=True)
    (project / "tmp").mkdir()
    shutil.copy(SWEEP, project / "scripts" / SWEEP.name)
    for helper in ("single_instance.sh", "run_with_timeout.sh", "ssot_reader_turn.sh"):
        shutil.copy(ROOT / "scripts" / "lib" / helper, project / "scripts" / "lib" / helper)
    (project / "scripts" / "lib" / "resolve_project_python.sh").write_text(
        "resolve_project_python() { printf '%s\\n' " + repr(sys.executable) + "; }\n",
        encoding="utf-8",
    )
    (project / "scripts" / "calibration_dashboard.py").write_text(
        "import os, sys, time, pathlib\n"
        "here = pathlib.Path(__file__).resolve().parents[1]\n"
        "if '--reinflate' in sys.argv:\n"
        "    (here / 'tmp' / 'reinflated').write_text('yes')\n"
        "    raise SystemExit(0)\n"
        # 2026-10-15: the page is drawn read-only from what the compute kept.
        "if '--html-out' in sys.argv:\n"
        "    (here / 'tmp' / 'cal_dashboard.html').write_text('<html>rendered</html>')\n"
        "    raise SystemExit(0)\n"
        "(here / 'tmp' / 'render.pid').write_text(str(os.getpid()))\n"
        f"time.sleep({render_seconds!r})\n"
        "(here / 'tmp' / 'computed').write_text('yes')\n",
        encoding="utf-8",
    )
    return project


def _run(project: Path, *, timeout_sec: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PM_DASHBOARD_RENDER_TIMEOUT_SEC"] = timeout_sec
    env["TMPDIR"] = str(project / "locks")
    (project / "locks").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(project / "scripts" / SWEEP.name)],
        cwd=str(project), env=env, capture_output=True, text=True, timeout=60,
    )


def _gone(pid: int, *, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_a_render_past_its_deadline_is_killed_and_the_lane_is_released(tmp_path: Path) -> None:
    project = _project(tmp_path, render_seconds=60.0)
    started = time.monotonic()
    result = _run(project, timeout_sec="2")
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert elapsed < 30.0, f"the sweep must return at the deadline, not at the compute's own pace ({elapsed:.1f}s)"
    assert "compute killed after 2s (timeout)" in result.stdout, result.stdout
    pid = int((project / "tmp" / "render.pid").read_text())
    assert _gone(pid), "the stub compute outlived the sweep: the snapshot it holds is exactly the harm"
    assert not (project / "tmp" / "computed").exists()
    # 2026-10-15: a killed compute costs freshness, never the page.
    assert (project / "tmp" / "cal_dashboard.html").read_text() == "<html>rendered</html>"
    assert "page drawn (read-only)" in result.stdout
    assert not list((project / "locks" / "weather-sweep-locks").glob("dashboard_render.lock")), (
        "the lock must go with the sweep, or the next interval is refused as 'already running'"
    )


def test_a_render_that_finishes_in_time_is_untouched(tmp_path: Path) -> None:
    project = _project(tmp_path, render_seconds=0.2)
    result = _run(project, timeout_sec="20")
    assert result.returncode == 0, result.stderr
    assert (project / "tmp" / "cal_dashboard.html").read_text() == "<html>rendered</html>"
    assert "] done" in result.stdout
    assert "timeout" not in result.stdout


def test_the_default_deadline_sits_inside_the_interval_with_a_gap_for_the_wal() -> None:
    text = SWEEP.read_text(encoding="utf-8")
    match = re.search(r'RENDER_TIMEOUT_SEC="\$\{PM_DASHBOARD_RENDER_TIMEOUT_SEC:-(\d+)\}"', text)
    assert match, "the deadline reads PM_DASHBOARD_RENDER_TIMEOUT_SEC with a numeric default"
    default = int(match.group(1))
    interval = 1800
    if PLIST.exists():
        # A regex, not plistlib: the installed plist carries a raw ``&`` in a
        # comment that launchd tolerates and expat does not.
        found = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", PLIST.read_text(encoding="utf-8"))
        if found:
            interval = int(found.group(1))
    assert 600 <= default <= interval - 300, (
        f"default {default}s: above a healthy render (median 10.8 min, max 21.5 min on a healthy day) "
        f"and at least five minutes under the {interval}s StartInterval, so a killed run leaves a gap "
        "with no render holding a snapshot"
    )


def test_a_render_does_not_start_onto_an_oversized_wal(tmp_path: Path) -> None:
    """2026-09-24: the writer reopens with a no-wait truncating checkpoint.

    The resident restarted at 01:24:49Z onto a 976 MB WAL and failed closed
    on ``target_database_wal_checkpoint_busy`` while a render was reading.  A
    render started while the WAL is past the resident's maintenance line is
    the reader that keeps production out, so it is skipped, not started.
    """

    project = _project(tmp_path, render_seconds=0.2)
    (project / "data").mkdir()
    (project / "data" / "bayes_basket_target_ssot.db-wal").write_bytes(b"\0" * 4096)
    env = dict(os.environ)
    env["PM_DASHBOARD_RENDER_MAX_WAL_BYTES"] = "1024"
    # No wait for the resident to truncate it (2026-10-17 added up to 300 s).
    env["PM_DASHBOARD_RENDER_WAL_WAIT_SEC"] = "0"
    env["TMPDIR"] = str(project / "locks")
    (project / "locks").mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", str(project / "scripts" / SWEEP.name)],
        cwd=str(project), env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "compute skipped: SSOT WAL 4096 bytes > 1024" in result.stdout, result.stdout
    assert not (project / "tmp" / "render.pid").exists(), "the compute must not have started"
    assert (project / "tmp" / "cal_dashboard.html").exists(), "the page is still drawn, read-only"
    assert not list((project / "locks" / "weather-sweep-locks").glob("dashboard_render.lock"))

    env["PM_DASHBOARD_RENDER_MAX_WAL_BYTES"] = "8192"
    result = subprocess.run(
        ["bash", str(project / "scripts" / SWEEP.name)],
        cwd=str(project), env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (project / "tmp" / "cal_dashboard.html").read_text() == "<html>rendered</html>"


def test_the_wal_line_is_the_residents_own_maintenance_line() -> None:
    # The line lives in the shared reader-turn library since 2026-10-17.
    text = (ROOT / "scripts" / "lib" / "ssot_reader_turn.sh").read_text(encoding="utf-8")
    match = re.search(r'="\$\{PM_DASHBOARD_RENDER_MAX_WAL_BYTES:-(\d+)\}"', text)
    assert match, "the WAL line reads PM_DASHBOARD_RENDER_MAX_WAL_BYTES with a numeric default"
    daemon = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    found = re.search(r"^TARGET_SSOT_WARN_WAL_BYTES = (\d+) \* 1024 \* 1024$", daemon, re.M)
    assert found, "the resident's WAL maintenance line moved; re-bind the render's"
    assert int(match.group(1)) == int(found.group(1)) * 1024 * 1024


if sys.platform != "darwin":  # pragma: no cover - the sweep is a launchd job on this Mac
    pytest.skip("taskpolicy/nice are the Mac's", allow_module_level=True)


def test_a_wake_inside_the_cadence_reads_nothing(tmp_path: Path) -> None:
    """2026-10-21 (「继续优化渲染周期」): launchd wakes the sweep every minute;
    a wake less than PM_DASHBOARD_RENDER_EVERY_SEC after the previous compute
    STARTED exits at once, and the compute that does start stamps its start."""

    project = _project(tmp_path, render_seconds=0.2)
    (project / "data").mkdir()
    env = dict(os.environ)
    env["TMPDIR"] = str(project / "locks")
    (project / "locks").mkdir(exist_ok=True)
    run = lambda: subprocess.run(  # noqa: E731
        ["bash", str(project / "scripts" / SWEEP.name)],
        cwd=str(project), env=env, capture_output=True, text=True, timeout=60,
    )
    first = run()
    assert first.returncode == 0, first.stderr
    assert "dashboard compute" in first.stdout
    assert (project / "data" / "dashboard_render.compute_started").exists()
    second = run()
    assert second.returncode == 0 and second.stdout == ""
