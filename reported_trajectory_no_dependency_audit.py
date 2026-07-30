"""Static dependency wall audit for the reported-temperature hard-NO lane."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable


SCHEMA = "reported_trajectory_no_dependency_audit_v1"
DEFAULT_ROOTS = (
    "reported_trajectory_no_runtime.py",
    "reported_trajectory_no_engine.py",
    "reported_trajectory_sources.py",
    "reported_trajectory_country_sources.py",
)
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "adaptive_basket_shadow",
        "bayes_basket_target",
        "copy_follow_optional_forecasts",
        "identifiable_physical_state_bayes",
        "physical_state_bayes_v6",
        "reported_trajectory_snapshot_materializer",
        "reported_trajectory_taf_sources",
        "sequential_bayes_schema_contract",
        "weak_forecast_evidence_snapshot_runtime",
    }
)
FORBIDDEN_CALL_NAMES = frozenset(
    {
        "fetch_amsc_taf_reports",
        "fetch_forecast",
        "fetch_nwp",
        "fetch_regime",
        "load_probability",
        "load_posterior",
    }
)


def _module_root(value: str) -> str:
    return value.split(".", 1)[0].lower()


def audit_reported_trajectory_no_dependencies(
    *,
    project_root: Path | None = None,
    roots: Iterable[str | Path] | None = None,
) -> dict[str, object]:
    base = Path(project_root or Path(__file__).resolve().parent)
    selected = tuple(roots or DEFAULT_ROOTS)
    files = [value if Path(value).is_absolute() else base / Path(value) for value in selected]
    import_hits: list[dict[str, object]] = []
    call_hits: list[dict[str, object]] = []
    syntax_hits: list[dict[str, object]] = []
    missing_files: list[str] = []
    for path in sorted(set(files), key=lambda item: str(item)):
        relative = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
        if not path.is_file():
            missing_files.append(relative)
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except SyntaxError as exc:
            syntax_hits.append({"file": relative, "line": int(exc.lineno or 0), "error": str(exc)})
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                modules = []
            for module in modules:
                if _module_root(module) in FORBIDDEN_IMPORT_ROOTS:
                    import_hits.append({"file": relative, "line": int(node.lineno), "module": module})
            if isinstance(node, ast.Call):
                function = node.func
                name = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
                if name in FORBIDDEN_CALL_NAMES:
                    call_hits.append({"file": relative, "line": int(node.lineno), "call": name})

    for values in (import_hits, call_hits, syntax_hits):
        values[:] = sorted(
            {tuple(sorted(item.items())) for item in values},
            key=lambda item: tuple(str(value) for value in item),
        )
        values[:] = [dict(item) for item in values]
    return {
        "schema": SCHEMA,
        "lane": "reported_trajectory_no",
        "authority": "OFFICIAL_METAR_SPECI_REPORTED_TEMPERATURE_ONLY",
        "files_scanned": [
            str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
            for path in files if path.is_file()
        ],
        "missing_files": sorted(missing_files),
        "forbidden_import_hits": import_hits,
        "forbidden_call_hits": call_hits,
        "syntax_hits": syntax_hits,
        "bayes_probability_consumed": False,
        "taf_consumed": False,
        "nwp_consumed": False,
        "regime_consumed": False,
        "hard_bound_source_only": "official_metar_speci_main_temperature",
        "isolated": not (import_hits or call_hits or syntax_hits or missing_files),
    }


__all__ = ["SCHEMA", "audit_reported_trajectory_no_dependencies"]
