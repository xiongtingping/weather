"""Candidate runtime identity for the report-defined-X successor epoch."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from production_probability_runtime_manifest import RETIREMENT_FILES, RUNTIME_FILES


ROOT = Path(__file__).resolve().parent
SUCCESSOR_EXCLUDED_LEGACY_FILES = frozenset(
    {
        "production_weak_forecast_revision_writer.py",
        "production_weak_forecast_revision_event_runtime.py",
    }
)
SUCCESSOR_FILES = tuple(
    dict.fromkeys(
        (
            *(name for name in RUNTIME_FILES if name not in SUCCESSOR_EXCLUDED_LEGACY_FILES),
            "production_probability_successor_activation_contract.py",
            "production_probability_successor_runtime_manifest.py",
            "report_defined_physical_x_runtime.py",
            "reported_x_successor_checkpoint_runtime.py",
            "reported_x_successor_schema_contract.py",
            "production_reported_x_public_ssot_writer.py",
            "production_reported_x_initial_checkpoint.py",
            "production_reported_x_initial_writer.py",
            "production_reported_x_transaction_host.py",
            "reported_x_successor_checkpoint_reader.py",
            "production_reported_x_event_runtime.py",
            "production_reported_x_weak_revision_writer.py",
            "production_reported_x_weak_revision_event_runtime.py",
            "taf_revision_candidate_selection_runtime.py",
            "production_reported_x_canonical_event_drain_runtime.py",
            "production_reported_x_amsc_archive_probability_ingress.py",
        )
    )
)


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_successor_runtime_manifest(root: Path | str = ROOT) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    return {
        "schema": "production_probability_runtime_manifest_v2_reported_x",
        "files": {
            name: hashlib.sha256((base / name).read_bytes()).hexdigest()
            for name in SUCCESSOR_FILES
        },
    }


def successor_runtime_manifest_sha256(root: Path | str = ROOT) -> str:
    return _sha(build_successor_runtime_manifest(root))


def audit_successor_runtime_manifest_dependency_closure(
    root: Path | str = ROOT,
) -> dict[str, Any]:
    """Prove every local Python dependency reachable by reported-X is bound."""

    base = Path(root).expanduser().resolve()
    registered = {Path(name) for name in SUCCESSOR_FILES}
    modules = {path.stem: path.relative_to(base) for path in base.glob("*.py")}
    scripts = base / "scripts"
    if scripts.is_dir():
        modules.update(
            {
                f"scripts.{path.stem}": path.relative_to(base)
                for path in scripts.glob("*.py")
            }
        )
    seen: set[Path] = set()
    queue = list(registered)
    parse_errors: list[str] = []
    while queue:
        relative = queue.pop()
        if relative in seen:
            continue
        path = base / relative
        if not path.exists() or path.suffix != ".py":
            continue
        seen.add(relative)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError) as exc:
            parse_errors.append(f"{relative}:{type(exc).__name__}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                candidate = modules.get(name) or modules.get(name.split(".")[0])
                if candidate is not None and candidate not in seen:
                    queue.append(candidate)
    missing = sorted(str(path) for path in seen - registered)
    retired_reachable = sorted(
        str(path) for path in seen if str(path) in SUCCESSOR_EXCLUDED_LEGACY_FILES
    )
    return {
        "schema": "production_probability_runtime_manifest_v2_reported_x_dependency_closure",
        "ok": not missing and not parse_errors and not retired_reachable,
        "registered_file_count": len(registered),
        "reachable_local_python_file_count": len(seen),
        "missing_runtime_files": missing,
        "retired_legacy_files_reachable": retired_reachable,
        "parse_errors": sorted(parse_errors),
        "read_only": True,
    }


__all__ = [
    "RETIREMENT_FILES",
    "SUCCESSOR_EXCLUDED_LEGACY_FILES",
    "SUCCESSOR_FILES",
    "build_successor_runtime_manifest",
    "successor_runtime_manifest_sha256",
    "audit_successor_runtime_manifest_dependency_closure",
]
