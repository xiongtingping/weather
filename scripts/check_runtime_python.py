#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROJECT_PY = ROOT / ".venv/bin/python"
REQUIRED_IMPORTS = ("numpy", "requests", "websockets")


def _run(args: list[str], timeout: float = 10.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    blockers: list[str] = []
    warnings: list[str] = []
    project_python = str(PROJECT_PY)
    shell_python = shutil.which("python3") or ""

    version = _run([project_python, "-V"]) if PROJECT_PY.exists() else {"ok": False}
    if not PROJECT_PY.exists() or not version.get("ok"):
        blockers.append("project_python_unavailable")

    imports = _run(
        [
            project_python,
            "-c",
            "import " + ",".join(REQUIRED_IMPORTS) + "; print('ok')",
        ]
    ) if PROJECT_PY.exists() else {"ok": False}
    if not imports.get("ok"):
        blockers.append("project_python_missing_required_imports")

    shell_version = _run([shell_python, "-V"]) if shell_python else {"ok": False}
    if shell_python and Path(shell_python).resolve() != PROJECT_PY.resolve():
        warnings.append("shell_python3_differs_from_project_python")

    output = {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "project_python": project_python,
        "project_python_version": version,
        "project_python_imports": imports,
        "shell_python3": shell_python,
        "shell_python3_version": shell_version,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
