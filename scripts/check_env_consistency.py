#!/usr/bin/env python3
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENV_FILES = [ROOT / ".env", Path.home() / "poly/.env"]
SECRET_MARKERS = ("KEY", "SECRET", "PASSPHRASE", "PRIVATE", "TOKEN")


def _redact(key: str, value: str) -> str:
    upper = key.upper()
    if any(marker in upper for marker in SECRET_MARKERS):
        if not value:
            return ""
        return f"<secret len={len(value)}>"
    return value


def _parse_env(path: Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not path.exists():
        return out
    for line_no, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        out[key].append({"line": line_no, "value": value})
    return out


def main() -> int:
    files: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    for path in ENV_FILES:
        parsed = _parse_env(path)
        duplicates = []
        conflicting = []
        for key, entries in sorted(parsed.items()):
            if len(entries) <= 1:
                continue
            values = [str(item["value"]) for item in entries]
            item = {
                "key": key,
                "lines": [item["line"] for item in entries],
                "effective": _redact(key, values[-1]),
                "values": [_redact(key, value) for value in values],
            }
            duplicates.append(item)
            if len(set(values)) > 1:
                conflicting.append(item)
        if conflicting:
            warnings.append(f"conflicting_env_overrides:{path}")
        files.append(
            {
                "path": str(path),
                "exists": path.exists(),
                "duplicate_count": len(duplicates),
                "conflicting_count": len(conflicting),
                "duplicates": duplicates,
                "conflicting": conflicting,
            }
        )

    output = {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "files": files,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
