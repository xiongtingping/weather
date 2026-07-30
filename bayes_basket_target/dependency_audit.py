"""Static dependency audit for the Bayes basket market lane.

The target lane consumes a resident, official CLOB WebSocket manifest.  It
must not grow a second market reader through REST, historical tick tables, or
an exchange client import.  This module is deliberately static: it parses
source files only and never opens a socket or a database connection.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable


SCHEMA = "bayes_basket_target_official_ws_dependency_audit_v1"
DEFAULT_ROOTS = ("bayes_basket_target",)
LEGACY_IMPORT_ROOTS = frozenset({"adaptive_basket_shadow"})
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "aiohttp",
        "http",
        "httpx",
        "requests",
        "urllib",
        "urllib3",
        "websocket",
        "websockets",
        "pycurl",
    }
)
FORBIDDEN_TICK_TOKENS = (
    "event_market_ticks",
    "market_ticks",
    "latest_market_tick",
)
FORBIDDEN_CALL_NAMES = frozenset(
    {
        "get_book",
        "get_order_book",
        "fetch_book",
        "fetch_order_book",
        "fetch_market_ticks",
    }
)
_WORD = re.compile(r"\b(?:event_market_ticks|market_ticks|latest_market_tick)\b")


def _module_name(value: str) -> str:
    return value.split(".", 1)[0].lower()


def _iter_python_files(roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        path = Path(root)
        if path.is_file() and path.suffix == ".py":
            files.append(path)
        elif path.is_dir():
            files.extend(path.rglob("*.py"))
    return sorted(set(files), key=lambda item: str(item))


def audit_official_ws_dependencies(
    *,
    project_root: Path | None = None,
    roots: Iterable[str | Path] | None = None,
) -> dict[str, object]:
    """Return a deterministic static receipt for the target market lane."""

    base = Path(project_root or Path(__file__).resolve().parents[1])
    scan_roots = tuple(roots or DEFAULT_ROOTS)
    files = _iter_python_files(
        path if Path(path).is_absolute() else base / Path(path)
        for path in scan_roots
    )
    auditor_path = Path(__file__).resolve()
    files = [path for path in files if path.resolve() != auditor_path]
    import_hits: list[dict[str, object]] = []
    legacy_import_hits: list[dict[str, object]] = []
    tick_hits: list[dict[str, object]] = []
    call_hits: list[dict[str, object]] = []
    syntax_hits: list[dict[str, object]] = []

    for path in files:
        relative = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            syntax_hits.append({"file": relative, "line": int(exc.lineno or 0), "error": str(exc)})
            continue
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                imported = []
            for module in imported:
                if _module_name(module) in LEGACY_IMPORT_ROOTS:
                    legacy_import_hits.append({"file": relative, "line": int(node.lineno), "module": module})
                if _module_name(module) in FORBIDDEN_IMPORT_ROOTS:
                    import_hits.append({"file": relative, "line": int(node.lineno), "module": module})

            if isinstance(node, ast.Call):
                function = node.func
                name = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
                if name in FORBIDDEN_CALL_NAMES:
                    call_hits.append({"file": relative, "line": int(node.lineno), "call": name})

            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                parent = parents.get(node)
                if (
                    isinstance(parent, (ast.Tuple, ast.List, ast.Set))
                    and isinstance(parents.get(parent), ast.Assign)
                    and any(
                        isinstance(target, ast.Name)
                        and target.id == "PROTECTED_MARKERS"
                        for target in parents[parent].targets
                    )
                ):
                    # A deny-list marker proves rejection of a legacy source;
                    # it is not a historical-tick read dependency.
                    continue
                matches = sorted(set(_WORD.findall(node.value)))
                for token in matches:
                    tick_hits.append({"file": relative, "line": int(node.lineno), "token": token})

    # Keep duplicate hits deterministic when a module contains the same
    # literal in more than one AST node or a repeated import alias.
    for values in (legacy_import_hits, import_hits, tick_hits, call_hits, syntax_hits):
        values[:] = sorted(
            {tuple(sorted(item.items())) for item in values},
            key=lambda item: tuple(str(value) for value in item),
        )
        values[:] = [dict(item) for item in values]

    return {
        "schema": SCHEMA,
        "market_authority": "OFFICIAL_CLOB_WS_MANIFEST",
        "source_contract": "official_polymarket_clob_websocket",
        "roots": [str(Path(path)) for path in scan_roots],
        "files_scanned": [str(path.relative_to(base)) if path.is_relative_to(base) else str(path) for path in files],
        "network_import_hits": import_hits,
        "legacy_import_hits": legacy_import_hits,
        "historical_tick_hits": tick_hits,
        "forbidden_market_call_hits": call_hits,
        "syntax_hits": syntax_hits,
        "rest_reads": bool(import_hits or call_hits),
        "historical_tick_reads": bool(tick_hits),
        "official_ws_only": not (import_hits or tick_hits or call_hits or syntax_hits),
        "target_dependency_closed": not (legacy_import_hits or syntax_hits),
        "authorizes_order": False,
    }


__all__ = ["SCHEMA", "audit_official_ws_dependencies"]
