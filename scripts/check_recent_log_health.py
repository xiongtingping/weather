#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOG_DIRS = [ROOT / "data" / "logs", ROOT / "logs"]
RECENT_MTIME_SEC = int(os.environ.get("PM_LOG_HEALTH_RECENT_MTIME_SEC", "7200"))
TAIL_BYTES = int(os.environ.get("PM_LOG_HEALTH_TAIL_BYTES", "65536"))

ERROR_RE = re.compile(
    r"\b(Traceback|CRITICAL|fatal|database is locked|no such table|readonly database|permission denied|unauthorized|401|403)\b",
    re.IGNORECASE,
)
WARNING_RE = re.compile(r"(?:\bERROR\b|\bException\b|\bTimeoutError\b|\btimed out\b|\btimeout after\b|\brate limit\b)")
DOWNGRADE_WARNING_RE = re.compile(
    r"task_queue unavailable .* downgrade polling",
    re.IGNORECASE,
)


def _tail_text(path: Path) -> str:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > TAIL_BYTES:
            fh.seek(size - TAIL_BYTES)
        text = fh.read().decode("utf-8", errors="replace")
    marker = "\n---- "
    idx = text.rfind(marker)
    if idx >= 0:
        text = text[idx + 1 :]
    yield_idx = text.rfind(" yield to ")
    if yield_idx >= 0:
        line_start = text.rfind("\n", 0, yield_idx)
        return text[line_start + 1 :]
    return text


def _is_warning_line(line: str) -> bool:
    if DOWNGRADE_WARNING_RE.search(line):
        return True
    if WARNING_RE.search(line):
        return True
    text = line.strip()
    if text.startswith("{") and '"ok"' in text:
        try:
            obj = json.loads(text)
        except Exception:
            return False
        return isinstance(obj, dict) and obj.get("ok") is False
    return False


def _matches(path: Path, now: float) -> dict[str, Any] | None:
    st = path.stat()
    age = int(max(0, now - st.st_mtime))
    if age > RECENT_MTIME_SEC or st.st_size == 0:
        return None
    text = _tail_text(path)
    blockers = [
        line[-240:]
        for line in text.splitlines()
        if ERROR_RE.search(line) and not DOWNGRADE_WARNING_RE.search(line)
    ][-5:]
    warnings = [line[-240:] for line in text.splitlines() if _is_warning_line(line)][-5:]
    if not blockers and not warnings:
        return None
    return {
        "path": str(path),
        "mtime_age_sec": age,
        "size_bytes": st.st_size,
        "blocker_lines": blockers,
        "warning_lines": warnings,
    }


def main() -> int:
    now = time.time()
    blockers: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {"recent_mtime_sec": RECENT_MTIME_SEC, "files": []}

    for log_dir in LOG_DIRS:
        if not log_dir.exists():
            continue
        for path in sorted(log_dir.glob("*.log")) + sorted(log_dir.glob("*.err.log")):
            item = _matches(path, now)
            if not item:
                continue
            details["files"].append(item)
            if item["blocker_lines"]:
                blockers.append(f"recent_log_blocker:{path.name}")
            elif item["warning_lines"]:
                warnings.append(f"recent_log_warning:{path.name}")

    output = {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "details": details,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
