"""The board's research render sweep (user ruling 2026-09-24 「单开研究节渲染」).

Run for real against a stub board, as test_dashboard_render_timeout does for
the page render: it never starts beside the page render, waits for the WAL to come
under the resident's line, and otherwise asks the board for exactly one
research section.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SWEEP = ROOT / "scripts" / "dashboard_research_sweep.sh"

if sys.platform != "darwin":  # pragma: no cover - a launchd job on this Mac
    pytest.skip("taskpolicy/pgrep are the Mac's", allow_module_level=True)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "scripts" / "lib").mkdir(parents=True)
    (project / "data").mkdir()
    shutil.copy(SWEEP, project / "scripts" / SWEEP.name)
    for helper in ("single_instance.sh", "run_with_timeout.sh", "ssot_reader_turn.sh"):
        shutil.copy(ROOT / "scripts" / "lib" / helper, project / "scripts" / "lib" / helper)
    (project / "scripts" / "lib" / "resolve_project_python.sh").write_text(
        "resolve_project_python() { printf '%s\\n' " + repr(sys.executable) + "; }\n",
        encoding="utf-8",
    )
    (project / "scripts" / "calibration_dashboard.py").write_text(
        "import os, sys, pathlib\n"
        "here = pathlib.Path(__file__).resolve().parents[1]\n"
        "if '--research-due' in sys.argv:\n"
        "    print(os.environ.get('STUB_RESEARCH_DUE', '2')); raise SystemExit(0)\n"
        "(here / 'data' / 'called').write_text(' '.join(sys.argv[1:]))\n"
        "print('research render: stub computed in 0s')\n",
        encoding="utf-8",
    )
    return project


def _run(project: Path, pattern: str, wait_sec: int = 1) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["TMPDIR"] = str(project / "locks")
    env["PM_DASHBOARD_PAGE_RENDER_PATTERN"] = pattern
    env["PM_DASHBOARD_RESEARCH_WAIT_SEC"] = str(wait_sec)
    env["PM_DASHBOARD_RESEARCH_WAIT_POLL_SEC"] = "1"
    (project / "locks").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(project / "scripts" / SWEEP.name)],
        cwd=str(project), env=env, capture_output=True, text=True, timeout=60,
    )


def test_it_asks_the_board_for_one_research_section(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = _run(project, "no-such-process-name-xyz")
    assert result.returncode == 0, result.stderr
    assert (project / "data" / "called").read_text() == "--refresh-research"
    assert "] done" in result.stdout


def test_a_wake_with_nothing_due_exits_before_any_wait(tmp_path: Path) -> None:
    # 2026-10-27: woken every 300 s, a wake with nothing due reads nothing,
    # waits for nothing and says nothing.
    project = _project(tmp_path)
    marker = f"fake-page-render-{os.getpid()}-{time.time_ns()}"
    page_render = subprocess.Popen(["bash", "-c", f"exec -a {marker} sleep 30"])
    os.environ["STUB_RESEARCH_DUE"] = "0"
    try:
        time.sleep(0.3)
        result = _run(project, marker, wait_sec=20)
    finally:
        os.environ.pop("STUB_RESEARCH_DUE", None)
        page_render.kill()
        page_render.wait()
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not (project / "data" / "called").exists()


def test_the_wake_interval_is_five_minutes() -> None:
    plist = (ROOT / "launchd" / "com.weather.dashboard_research_render.plist").read_text(encoding="utf-8")
    assert "<key>StartInterval</key>\n  <integer>300</integer>" in plist


def test_it_waits_for_an_oversized_wal_to_be_truncated(tmp_path: Path) -> None:
    # Since 2026-10-17 (scripts/lib/ssot_reader_turn.sh) it waits the WAL out
    # instead of skipping the hour, and starts once the resident truncates it.
    project = _project(tmp_path)
    wal = project / "data" / "bayes_basket_target_ssot.db-wal"
    wal.write_bytes(b"\0" * 4096)
    os.environ["PM_DASHBOARD_RENDER_MAX_WAL_BYTES"] = "1024"
    os.environ["PM_SSOT_READER_POLL_SEC"] = "1"
    try:
        truncate = subprocess.Popen(["bash", "-c", f"sleep 2; : > '{wal}'"])
        result = _run(project, "no-such-process-name-xyz")
        truncate.wait()
    finally:
        os.environ.pop("PM_DASHBOARD_RENDER_MAX_WAL_BYTES", None)
        os.environ.pop("PM_SSOT_READER_POLL_SEC", None)
    assert result.returncode == 0, result.stderr
    assert "for the SSOT WAL to come under 1024" in result.stdout
    assert (project / "data" / "called").read_text() == "--refresh-research"


def test_it_does_not_start_beside_the_page_render(tmp_path: Path) -> None:
    project = _project(tmp_path)
    marker = f"fake-page-render-{os.getpid()}-{time.time_ns()}"
    page_render = subprocess.Popen(["bash", "-c", f"exec -a {marker} sleep 30"])
    try:
        time.sleep(0.3)
        result = _run(project, marker)
    finally:
        page_render.kill()
        page_render.wait()
    assert result.returncode == 0 and "the page render is running" in result.stdout
    assert not (project / "data" / "called").exists()


def test_it_waits_out_a_page_render_that_finishes(tmp_path: Path) -> None:
    """2026-10-01: seven of fourteen hourly starts were skipped because they
    landed inside a page render, and the pages only this render refreshes
    sat 10-25 hours old on the board."""

    project = _project(tmp_path)
    marker = f"fake-page-render-{os.getpid()}-{time.time_ns()}"
    page_render = subprocess.Popen(["bash", "-c", f"exec -a {marker} sleep 2"])
    try:
        time.sleep(0.3)
        result = _run(project, marker, wait_sec=30)
    finally:
        page_render.kill()
        page_render.wait()
    assert result.returncode == 0, result.stderr
    assert "waited" in result.stdout and "for the page render to finish" in result.stdout
    assert (project / "data" / "called").read_text() == "--refresh-research"
