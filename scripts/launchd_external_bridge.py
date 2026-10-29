#!/usr/bin/env python3
"""Rewrite a LaunchAgent so launchd itself never touches the external disk.

2026-10-29 the project moved to the external disk (user 「将整个项目全部移动至
外置硬盘」; /Users/xiong/weather is a symlink to /Volumes/weather_data/weather).
launchd cannot set a WorkingDirectory or StandardOut/ErrorPath on that
volume -- every job exited 78 before it started -- and TCC refuses the volume
to a job whose own executable is /bin/bash.  The granted Python is, so each
job becomes: that Python running ``scripts/launchd_exec.py``, which opens the
job's own log files, moves into its own working directory and runs its own
command as a child.  launchd keeps only internal paths.

Idempotent: a plist already bridged is returned unchanged.  ``bridge(data,
home=..., root=...)`` is what the installer calls; run as a script it
rewrites installed plists in place (``--apply``) or prints what it would do.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import sys
from pathlib import Path

BRIDGE_PYTHON = os.environ.get(
    "PM_LAUNCHD_BRIDGE_PYTHON",
    "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13",
)


def bridge(data: dict, *, home: str, root: str) -> dict:
    program = list(data.get("ProgramArguments") or ([data["Program"]] if data.get("Program") else []))
    launcher = str(Path(root) / "scripts" / "launchd_exec.py")
    if len(program) > 1 and program[1] == launcher:
        return data
    out = str(data.pop("StandardOutPath", "/dev/null"))
    err = str(data.pop("StandardErrorPath", out))
    cwd = str(data.get("WorkingDirectory") or "/")
    data.pop("Program", None)
    data["ProgramArguments"] = [
        BRIDGE_PYTHON, launcher, "--cwd", cwd, "--out", out, "--err", err, "--", *program,
    ]
    data["WorkingDirectory"] = home
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plists", nargs="+", type=Path)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    home = str(Path.home())
    for path in args.plists:
        with path.open("rb") as f:
            data = plistlib.load(f)
        before = list(data.get("ProgramArguments") or [])
        data = bridge(data, home=home, root=args.root)
        changed = data["ProgramArguments"] != before
        print(f"{'bridged' if changed else 'already'}\t{path.name}")
        if changed and args.apply:
            with path.open("wb") as f:
                plistlib.dump(data, f, fmt=plistlib.FMT_XML, sort_keys=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
