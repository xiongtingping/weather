"""The suite-wide guard that keeps worker tests off the resident's scripts.

Found 2026-09-17: worker tests started the real Open-Meteo d2 capture with
``--commit`` through the local proxy.  ``conftest`` now swaps the worker
module's ``subprocess`` for a forwarding namespace that refuses a command
naming a file under ``scripts/``.  Pinned here: a script command is refused
without running, anything else still runs, the global module is untouched,
and a test's own stub on the worker's ``subprocess.run`` still wins.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from bayes_basket_target import production_worker

ROOT = Path(__file__).resolve().parents[1]


def test_a_command_naming_a_resident_script_is_refused_without_running(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    completed = production_worker.subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "capture_open_meteo_d2_ensemble_daily.py"),
         "--output", str(marker), "--commit"],
        capture_output=True, text=True, timeout=5,
    )
    assert completed.returncode == 97
    assert completed.stderr == "test_suite_may_not_launch_resident_scripts"
    assert not marker.exists()


def test_other_commands_still_run_and_the_global_module_is_untouched() -> None:
    completed = production_worker.subprocess.run(
        [sys.executable, "-c", "print('ok')"], capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0 and completed.stdout.strip() == "ok"
    assert production_worker.subprocess is not subprocess
    assert subprocess.run is not production_worker.subprocess.run
    assert production_worker.subprocess.TimeoutExpired is subprocess.TimeoutExpired


def test_a_tests_own_stub_on_the_worker_still_wins(monkeypatch) -> None:
    monkeypatch.setattr(production_worker.subprocess, "run", lambda *a, **k: "stubbed")
    assert production_worker.subprocess.run(["anything"]) == "stubbed"
