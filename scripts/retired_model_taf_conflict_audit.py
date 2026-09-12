#!/usr/bin/env python3
"""Fail if the retired model-vs-TAF conflict special case re-enters runtime.

Historical fact payloads remain immutable and may contain legacy fields.  This
audit intentionally scans only code that can compute, project, persist or
render *new* probability facts; it never rewrites history.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
# The fact snapshot writer and ui/server.py were removed with the pre-basket
# lane.  A file that no longer exists cannot carry a retired token, and the
# scan below records an unreadable path as a violation, so they are dropped
# here rather than left to fail closed on their own absence.
RUNTIME_FILES = (
    ROOT / "forecast_weak_likelihood_runtime.py",
    ROOT / "hierarchical_posterior_pipeline_runtime.py",
)
RETIRED_TOKENS = (
    "MODEL_TAF_CONFLICT",
    "_model_taf_conflict_pool",
    "forecast_model_taf_conflict",
    "model_taf_conflict",
    "scenario_mixture_decision",
)


def build_retired_model_taf_conflict_audit() -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    for path in RUNTIME_FILES:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            violations.append({"path": str(path), "token": "<unreadable>", "error": type(exc).__name__})
            continue
        for line_number, line in enumerate(lines, start=1):
            for token in RETIRED_TOKENS:
                if token in line:
                    violations.append({"path": str(path), "line": line_number, "token": token})
    return {
        "schema": "retired_model_taf_conflict_audit_v1",
        "ok": not violations,
        "state": "ready" if not violations else "blocked",
        "runtime_files": [str(path) for path in RUNTIME_FILES],
        "retired_tokens": list(RETIRED_TOKENS),
        "violations": violations,
        "guards": {
            "writes_archive_db": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_services": False,
        },
    }


def main() -> int:
    report = build_retired_model_taf_conflict_audit()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
