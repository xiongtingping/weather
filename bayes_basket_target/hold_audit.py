"""Audit that the basket execution closure is hold-to-settlement only.

The basket lane may create YES BUY candidates, but it must never create a
position exit, merge, or selective rebalance action.  This is a static and
pure contract audit: it reads source files and exercises the existing
``assert_basket_hold_only`` guard in memory.  It never opens a database or
contacts an order service.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable

from .execution_reconciliation import ReconciliationError, assert_basket_hold_only


SCHEMA = "bayes_basket_target_hold_closure_audit_v1"
DEFAULT_ROOTS = (
    "bayes_basket_target/production.py",
    "bayes_basket_target/runtime.py",
    "bayes_basket_target/basket_kernel",
)
FORBIDDEN_CALLS = frozenset(
    {
        "sell_position",
        "sell_token",
        "merge_positions",
        "merge_basket",
        "liquidate_position",
        "selective_rebalance",
        "early_exit",
        "exit_position",
    }
)


def _files(base: Path, roots: Iterable[str | Path]) -> list[Path]:
    result: list[Path] = []
    for raw in roots:
        path = Path(raw)
        if not path.is_absolute():
            path = base / path
        if path.is_file() and path.suffix == ".py":
            result.append(path)
        elif path.is_dir():
            result.extend(path.rglob("*.py"))
    return sorted(set(result), key=str)


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def audit_basket_hold_closure(
    *,
    project_root: Path | None = None,
    roots: Iterable[str | Path] | None = None,
) -> dict[str, object]:
    base = Path(project_root or Path(__file__).resolve().parents[1])
    selected = tuple(roots or DEFAULT_ROOTS)
    files = _files(base, selected)
    call_hits: list[dict[str, object]] = []
    action_hits: list[dict[str, object]] = []
    syntax_hits: list[dict[str, object]] = []

    for path in files:
        relative = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except SyntaxError as exc:
            syntax_hits.append({"file": relative, "line": int(exc.lineno or 0), "error": str(exc)})
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in FORBIDDEN_CALLS:
                call_hits.append({"file": relative, "line": int(node.lineno), "call": _call_name(node)})
            if isinstance(node, ast.Dict):
                keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
                values = [value.value for value in node.values if isinstance(value, ast.Constant)]
                if "order_action" in keys and "SELL" in values:
                    action_hits.append({"file": relative, "line": int(node.lineno), "reason": "basket_sell_literal"})
                if "merge" in keys and any(value is True for value in values):
                    action_hits.append({"file": relative, "line": int(node.lineno), "reason": "merge_enabled_literal"})

    allowed = assert_basket_hold_only([
        {"side": "YES", "order_action": "BUY"},
        {"side": "YES", "order_action": "BUY"},
    ])
    rejected_sell = False
    try:
        assert_basket_hold_only([{"side": "YES", "order_action": "SELL"}])
    except ReconciliationError:
        rejected_sell = True
    rejected_merge = False
    try:
        assert_basket_hold_only([{"side": "YES", "order_action": "BUY", "merge": True}])
    except ReconciliationError:
        rejected_merge = True

    for values in (call_hits, action_hits, syntax_hits):
        values[:] = sorted(
            {tuple(sorted(item.items())) for item in values},
            key=lambda item: tuple(str(value) for value in item),
        )
        values[:] = [dict(item) for item in values]

    checks = {
        "source_files_present": bool(files),
        "no_forbidden_executable_calls": not call_hits,
        "no_basket_sell_or_merge_literals": not action_hits,
        "syntax_clean": not syntax_hits,
        "valid_yes_buy_hold_passes": allowed.get("hold_to_settlement") is True,
        "sell_is_rejected": rejected_sell,
        "merge_is_rejected": rejected_merge,
    }
    return {
        "schema": SCHEMA,
        "lane": "basket",
        "hold_policy": {
            "hold_to_settlement": True,
            "merge": False,
            "selective_rebalance": False,
            "early_sell": False,
        },
        "files_scanned": [str(path.relative_to(base)) if path.is_relative_to(base) else str(path) for path in files],
        "forbidden_executable_call_hits": call_hits,
        "forbidden_action_literal_hits": action_hits,
        "syntax_hits": syntax_hits,
        "checks": checks,
        "hold_closure_verified": all(checks.values()),
        "boundary": {
            "read_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA", "audit_basket_hold_closure"]
