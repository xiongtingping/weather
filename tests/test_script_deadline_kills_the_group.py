"""A script's deadline binds every process the script starts.

2026-09-24: the observation-identity pass runs ``pr12_awc_observation_sweep.sh``
under a 240 s deadline.  ``subprocess.run(timeout=)`` killed bash and left its
Python step orphaned, holding the captured pipes, so the call returned only
when the orphan finished -- the production cycle waited 19 minutes and more,
and no basket decision was written from 14:11Z.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bayes_basket_target.production_worker import run_script_group_bounded


def _script(tmp_path: Path) -> tuple[Path, Path]:
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "slow.sh"
    script.write_text(
        "#!/bin/bash\n"
        f"{sys.executable} -c \"import os,time;"
        f"open('{pid_file}','w').write(str(os.getpid()));time.sleep(30)\"\n"
        "echo done\n"
    )
    return script, pid_file


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_the_deadline_returns_on_time_and_leaves_no_orphan(tmp_path: Path) -> None:
    script, pid_file = _script(tmp_path)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_script_group_bounded(["/bin/bash", str(script)], cwd=str(tmp_path), timeout=1.5)
    assert time.monotonic() - started < 10.0
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5.0
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild)


def test_a_script_that_finishes_returns_its_output(tmp_path: Path) -> None:
    script = tmp_path / "quick.sh"
    script.write_text("#!/bin/bash\necho out\necho err >&2\nexit 3\n")
    completed = run_script_group_bounded(["/bin/bash", str(script)], cwd=str(tmp_path), timeout=10)
    assert completed.returncode == 3
    assert completed.stdout.strip() == "out" and completed.stderr.strip() == "err"


def test_the_identity_pass_runs_its_script_through_the_bounded_runner() -> None:
    import ast
    import inspect
    import textwrap

    from bayes_basket_target.production_worker import TargetProductionWorker

    tree = ast.parse(textwrap.dedent(inspect.getsource(
        TargetProductionWorker._identify_live_observations
    )))
    called = {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "run_script_group_bounded" in called
    assert "subprocess.run" not in called


def test_the_suite_guard_refuses_resident_scripts_through_popen_too() -> None:
    """The conftest guard covers both entry points the worker can launch a
    resident script through.  Probed with a path that does not exist, so a
    guard that ever stops refusing fails this test without launching
    anything real."""

    import bayes_basket_target.production_worker as pw

    root = Path(pw.__file__).resolve().parents[1]
    completed = run_script_group_bounded(
        ["/bin/bash", str(root / "scripts" / "__guard_probe_missing__.sh")],
        cwd=str(root), timeout=5,
    )
    assert completed.returncode == 97
    assert "test_suite_may_not_launch_resident_scripts" in completed.stderr
