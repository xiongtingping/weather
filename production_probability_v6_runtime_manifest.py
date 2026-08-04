"""Content-addressed closure for the actual weather-basket resident service."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "production_probability_v6_runtime_manifest_v1"
ROOT = Path(__file__).resolve().parent
RUNTIME_ROOTS = ("scripts/weather_basket_yes_daemon.py",)
FORBIDDEN_PRODUCTION_RUNTIME_FILES = frozenset(
    {
        "amsc_raw_history_archive.py",
        "amsc_taf_revision_runtime.py",
        "identifiable_physical_probability_worker.py",
        "production_probability_v6_amsc_archive_ingress.py",
        "production_probability_v6_weak_revision_event_runtime.py",
        "scripts/apply_amsc_taf_forecast_revision_v6_from_plan.py",
        "scripts/apply_current_weak_source_revisions_v6.py",
        "scripts/fetch_zuuu_metar_taf.py",
    }
)
RETIRED_V5_RUNTIME_FILES = frozenset(
    {
        "production_probability_p0_runtime.py",
        "production_reported_x_canonical_event_drain_runtime.py",
        "production_reported_x_event_runtime.py",
        "production_reported_x_weak_revision_event_runtime.py",
        "production_sequential_bayes_event_runtime.py",
        "sequential_bayes_filter_shadow_runtime.py",
        "sequential_bayes_shadow_pipeline.py",
    }
)


def _local_module_index(root: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if any(part in {".git", ".venv", "data", "output", "tests"} for part in relative.parts):
            continue
        if path.name == "__init__.py":
            module = ".".join(relative.parent.parts)
        else:
            module = ".".join(relative.with_suffix("").parts)
        if module:
            index[module] = relative
    return index


def _module_name(relative: Path) -> str:
    if relative.name == "__init__.py":
        return ".".join(relative.parent.parts)
    return ".".join(relative.with_suffix("").parts)


def _reachable_runtime_files(root: Path) -> tuple[str, ...]:
    """Resolve the complete local Python closure from the explicit v6 roots."""

    index = _local_module_index(root)
    seen: set[Path] = set()
    queue = [Path(value) for value in RUNTIME_ROOTS]
    while queue:
        relative = queue.pop()
        if relative in seen:
            continue
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"v6_runtime_file_missing:{relative}")
        seen.add(relative)
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise ValueError(f"v6_runtime_parse_failed:{relative}:{type(exc).__name__}") from exc
        current_module = _module_name(relative)
        current_package = current_module if relative.name == "__init__.py" else current_module.rpartition(".")[0]
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    package_parts = current_package.split(".") if current_package else []
                    keep = max(0, len(package_parts) - node.level + 1)
                    prefix = ".".join(package_parts[:keep])
                    base = ".".join(value for value in (prefix, base) if value)
                if base:
                    candidates.append(base)
                candidates.extend(
                    ".".join(value for value in (base, alias.name) if value)
                    for alias in node.names
                    if alias.name != "*"
                )
            for name in candidates:
                candidate = index.get(name)
                if candidate is not None and candidate not in seen:
                    queue.append(candidate)
                parts = name.split(".")
                for end in range(1, len(parts)):
                    package = index.get(".".join(parts[:end]))
                    if package is not None and package not in seen:
                        queue.append(package)
    return tuple(sorted(str(path) for path in seen))


# Default-workspace resolved closure retained for callers/tests. A manifest
# built for another root resolves that root independently.
RUNTIME_FILES = _reachable_runtime_files(ROOT)


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_v6_runtime_manifest(root: Path = ROOT) -> dict[str, Any]:
    from production_probability_v6_live_route_contract import build_v6_live_route_targets

    rows = []
    runtime_files = _reachable_runtime_files(root)
    retired_reachable = sorted(set(runtime_files) & RETIRED_V5_RUNTIME_FILES)
    if retired_reachable:
        raise ValueError("retired_v5_runtime_reachable:" + ",".join(retired_reachable))
    forbidden_reachable = sorted(set(runtime_files) & FORBIDDEN_PRODUCTION_RUNTIME_FILES)
    if forbidden_reachable:
        raise ValueError(
            "forbidden_weather_basket_runtime_reachable:" + ",".join(forbidden_reachable)
        )
    for relative in runtime_files:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"v6_runtime_file_missing:{relative}")
        rows.append({"path": relative, "sha256": _sha_bytes(path.read_bytes())})
    core = {
        "schema": SCHEMA_VERSION,
        "runtime_files": rows,
        "runtime_root_count": len(RUNTIME_ROOTS),
        "reachable_local_python_file_count": len(runtime_files),
        "dependency_closure_complete": True,
        "retired_v5_runtime_reachable": False,
        "forbidden_runtime_files_reachable": False,
        "resident_service": "com.weather.weather_basket_yes",
        "production_route_targets": [
            {"path": path, "sha256": _sha_bytes(value)}
            for path, value in sorted(build_v6_live_route_targets(root).items())
        ],
        "unique_production_probability_mainline": True,
        "v5_fallback_forbidden": True,
        "shadow_runtime": False,
        "places_orders": False,
    }
    manifest_sha = _sha_bytes(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return {**core, "runtime_manifest_sha256": manifest_sha}


def v6_runtime_manifest_sha256(root: Path = ROOT) -> str:
    return str(build_v6_runtime_manifest(root)["runtime_manifest_sha256"])


def audit_v6_runtime_manifest_dependency_closure(root: Path = ROOT) -> dict[str, Any]:
    try:
        reachable = _reachable_runtime_files(root)
    except (OSError, ValueError) as exc:
        return {
            "schema": "production_probability_v6_runtime_dependency_closure_v1",
            "ok": False,
            "blockers": [str(exc)],
            "read_only": True,
        }
    manifest_paths = tuple(row["path"] for row in build_v6_runtime_manifest(root)["runtime_files"])
    return {
        "schema": "production_probability_v6_runtime_dependency_closure_v1",
        "ok": manifest_paths == reachable,
        "runtime_root_count": len(RUNTIME_ROOTS),
        "reachable_local_python_file_count": len(reachable),
        "missing_runtime_files": sorted(set(reachable) - set(manifest_paths)),
        "read_only": True,
    }


__all__ = [
    "FORBIDDEN_PRODUCTION_RUNTIME_FILES",
    "RUNTIME_FILES",
    "RUNTIME_ROOTS",
    "RETIRED_V5_RUNTIME_FILES",
    "SCHEMA_VERSION",
    "audit_v6_runtime_manifest_dependency_closure",
    "build_v6_runtime_manifest",
    "v6_runtime_manifest_sha256",
]
