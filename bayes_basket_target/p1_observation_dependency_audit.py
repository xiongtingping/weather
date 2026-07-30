"""Static audit against station/source hard-coding in the formal P1 chain."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable


SCHEMA = "bayes_basket_target_p1_observation_dependency_audit_v1"
DEFAULT_FILES = (
    "bayes_basket_target/canonical_event_replay.py",
    "bayes_basket_target/canonical_ingress.py",
    "bayes_basket_target/materialization.py",
    "bayes_basket_target/p1_compiler.py",
    "bayes_basket_target/production_io.py",
    "bayes_basket_target/resident_canonical.py",
)
_LANES = frozenset({"AMSC", "AWC"})
_SQL_SINGLE_LANE = re.compile(
    r"\b(?:source_lane|lane)\b\s*(?:=|==|!=|<>|IS|IS NOT)\s*"
    r"[\"'](?:AMSC|AWC)[\"']",
    re.IGNORECASE,
)


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id.lower()
    if isinstance(node, ast.Attribute):
        return node.attr.lower()
    if isinstance(node, ast.Subscript):
        value = node.slice
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value.lower()
    return ""


def _lane_literals(node: ast.AST) -> set[str]:
    values: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            value = child.value.strip().upper()
            if value in _LANES:
                values.add(value)
    return values


def audit_p1_observation_dependencies(
    *,
    project_root: Path | None = None,
    files: Iterable[str | Path] | None = None,
) -> dict[str, object]:
    """Prove that formal P1 is registry-scoped, never ZUUU/one-lane scoped."""

    base = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    requested = tuple(files or DEFAULT_FILES)
    paths = tuple(
        Path(value) if Path(value).is_absolute() else base / Path(value)
        for value in requested
    )
    station_hits: list[dict[str, object]] = []
    single_lane_hits: list[dict[str, object]] = []
    syntax_hits: list[dict[str, object]] = []
    missing_files: list[str] = []
    scanned: list[str] = []

    for path in paths:
        relative = (
            str(path.resolve().relative_to(base))
            if path.resolve().is_relative_to(base)
            else str(path.resolve())
        )
        if not path.is_file():
            missing_files.append(relative)
            continue
        scanned.append(relative)
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            syntax_hits.append({
                "file": relative,
                "line": int(exc.lineno or 0),
                "error": str(exc),
            })
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value.strip()
                if value.upper() == "ZUUU":
                    station_hits.append({
                        "file": relative,
                        "line": int(getattr(node, "lineno", 0)),
                        "station_id": "ZUUU",
                    })
                if _SQL_SINGLE_LANE.search(value):
                    single_lane_hits.append({
                        "file": relative,
                        "line": int(getattr(node, "lineno", 0)),
                        "expression": value,
                    })
            if not isinstance(node, ast.Compare):
                continue
            compared_names = {
                _name(node.left),
                *(_name(value) for value in node.comparators),
            }
            if not any(
                "lane" in name or "source" in name
                for name in compared_names
            ):
                continue
            literals = _lane_literals(node)
            # Validation against the complete formal lane set is required.
            # A comparison naming only one lane is the forbidden hard-coded
            # AMSC/AWC inclusion or exclusion gate.
            if len(literals) == 1:
                single_lane_hits.append({
                    "file": relative,
                    "line": int(getattr(node, "lineno", 0)),
                    "expression": ast.unparse(node),
                })

    def _dedupe(values: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            dict(row)
            for row in sorted(
                {tuple(sorted(item.items())) for item in values},
                key=lambda row: tuple(str(value) for value in row),
            )
        ]

    station_hits = _dedupe(station_hits)
    single_lane_hits = _dedupe(single_lane_hits)
    syntax_hits = _dedupe(syntax_hits)
    closed = not (
        station_hits
        or single_lane_hits
        or syntax_hits
        or missing_files
    )
    return {
        "schema": SCHEMA,
        "files_scanned": scanned,
        "missing_files": sorted(missing_files),
        "hardcoded_station_hits": station_hits,
        "single_lane_filter_hits": single_lane_hits,
        "syntax_hits": syntax_hits,
        "registry_scoped_p1": closed,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "authorizes_order": False,
    }


__all__ = ["SCHEMA", "audit_p1_observation_dependencies"]
