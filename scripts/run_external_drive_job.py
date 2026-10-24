"""Run a shell script that lives on the external drive, for a launchd job.

launchd started the WRF jobs as ``/bin/bash ~/apps/run/<job>.sh`` and macOS
refused every run (``Operation not permitted``, exit 126): ``~/apps`` is the
USB drive, and bash, started by launchd as its own responsible process, holds
no removable-volume access.  This Python does (the cold-tier offload has
written that drive from launchd all along), and a child it starts is held
under it, so the script runs as ``python run_external_drive_job.py <script>
[args...]``.  The drive missing is not an error: the job is skipped.

User 2026-10-25「wrf为什么只有最近10-25一天的预报，我要求的是未来三天都要！」:
the three-day forecast had not run from launchd once since 10-22.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DRIVE = Path("/Volumes/weather_data")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: run_external_drive_job.py <script> [args...]", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not DRIVE.is_dir():
        print(f"[{stamp}] external drive missing; skipped {argv[1]}", flush=True)
        return 0
    print(f"[{stamp}] start {' '.join(argv[1:])}", flush=True)
    return subprocess.run(["/bin/bash", *argv[1:]], check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
