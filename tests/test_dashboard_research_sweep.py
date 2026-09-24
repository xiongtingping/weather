"""The board's research render sweep (user ruling 2026-09-24 「单开研究节渲染」).

Run for real against a stub board, as test_dashboard_render_timeout does for
the page render: it never starts beside the page render, never starts onto a
WAL past the resident's line, and otherwise asks the board for exactly one
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
    for helper in ("single_instance.sh", "run_with_timeout.sh"):
        shutil.copy(ROOT / "scripts" / "lib" / helper, project / "scripts" / "lib" / helper)
    (project / "scripts" / "lib" / "resolve_project_python.sh").write_text(
        "resolve_project_python() { printf '%s\\n' " + repr(sys.executable) + "; }\n",
        encoding="utf-8",
    )
    (project / "scripts" / "calibration_dashboard.py").write_text(
        "import sys, pathlib\n"
        "here = pathlib.Path(__file__).resolve().parents[1]\n"
        "(here / 'data' / 'called').write_text(' '.join(sys.argv[1:]))\n"
        "print('research render: stub computed in 0s')\n",
        encoding="utf-8",
    )
    return project


def _run(project: Path, pattern: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["TMPDIR"] = str(project / "locks")
    env["PM_DASHBOARD_PAGE_RENDER_PATTERN"] = pattern
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


def test_it_does_not_start_onto_an_oversized_wal(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "data" / "bayes_basket_target_ssot.db-wal").write_bytes(b"\0" * 4096)
    env_pattern = "no-such-process-name-xyz"
    os.environ["PM_DASHBOARD_RENDER_MAX_WAL_BYTES"] = "1024"
    try:
        result = _run(project, env_pattern)
    finally:
        os.environ.pop("PM_DASHBOARD_RENDER_MAX_WAL_BYTES", None)
    assert result.returncode == 0 and "skipped: SSOT WAL 4096" in result.stdout
    assert not (project / "data" / "called").exists()


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
