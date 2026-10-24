"""The launchd wrapper for scripts on the external drive (user 2026-10-25
「wrf为什么只有最近10-25一天的预报，我要求的是未来三天都要！」): bash under
launchd may not read the drive, so Python starts it as its child."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_external_drive_job", ROOT / "scripts" / "run_external_drive_job.py")
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)


def test_the_script_runs_under_bash_with_its_arguments_and_its_exit_code(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(job, "DRIVE", tmp_path)
    script = tmp_path / "s.sh"
    out = tmp_path / "out.txt"
    script.write_text(f'echo "$1 $2" > {out}\nexit 3\n')
    assert job.main(["x", str(script), "a", "b"]) == 3
    assert out.read_text().strip() == "a b"


def test_a_missing_drive_skips_the_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(job, "DRIVE", tmp_path / "absent")
    assert job.main(["x", str(tmp_path / "never.sh")]) == 0
