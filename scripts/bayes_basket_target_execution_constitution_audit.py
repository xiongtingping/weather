#!/usr/bin/env python3
"""Bounded static gate for the target execution constitution (M1-11).

The audit deliberately scans a small, explicit production runtime scope.  It
does not search the repository, documentation, tests, generated receipts, or
legacy production lines.  Python comments and module/class/function
docstrings are removed from semantic scanning, so explanatory negations do
not become execution violations.

This is a static contract gate, not an order or database probe.  It performs
no imports of the target runtime, opens no database, accesses no network, and
does not call an order gateway.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import tokenize
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "bayes_basket_target_execution_constitution_audit_v1"
CONSTITUTION_VERSION = "m1-11-execution-constitution-v1"

# This is intentionally an explicit allowlist.  It is the target decision and
# execution closure, not a repository-wide search.  In particular, docs,
# tests, data, output, source-account replay code, and old production lanes
# are outside this gate.
DEFAULT_RUNTIME_SCOPE: tuple[str, ...] = (
    "scripts/reported_trajectory_no_daemon.py",
    "bayes_basket_target/production_worker.py",
    "bayes_basket_target/delta_v2_runtime.py",
    "bayes_basket_target/delta_v2_training.py",
    "bayes_basket_target/delta_v2_settlement_label.py",
    "bayes_basket_target/forward_training_validation.py",
    "bayes_basket_target/production.py",
    "bayes_basket_target/production_io.py",
    "bayes_basket_target/runtime.py",
    "bayes_basket_target/atomic_ledger.py",
    "bayes_basket_target/execution_reconciliation.py",
    "bayes_basket_target/execution_transition_runtime.py",
    "bayes_basket_target/order_state_producer.py",
    "bayes_basket_target/execution_version_vector.py",
    "bayes_basket_target/basket_kernel/__init__.py",
    "bayes_basket_target/basket_kernel/contracts.py",
    "bayes_basket_target/basket_kernel/execution_gates.py",
    "bayes_basket_target/basket_kernel/maker_simulator.py",
    "bayes_basket_target/basket_kernel/support.py",
    "bayes_basket_target/basket_kernel/delta_v2.py",
)

MAX_FILES = 64
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 8_000_000
MAX_VIOLATIONS = 128
MAX_EXCERPT_BYTES = 240
_EXCLUDED_SCOPE_DIRS = frozenset({"docs", "tests", "data", "output"})

_FIXED_STAGE_TEXT = re.compile(
    r"(?:4\s*(?:→|->|to)\s*3\s*(?:→|->|to)\s*2|"
    r"4\s*,\s*3\s*,\s*2|4\s+3\s+2)",
    re.IGNORECASE,
)
_EQUAL_SHARE_TEXT = re.compile(
    r"(?:equal[_ -]?share(?:s)?|equal[_ -]?split|等份)",
    re.IGNORECASE,
)
_ALL_LEG_FOK_TEXT = re.compile(
    r"(?:immediate_stage_all_legs_fok_required|"
    r"all[_ -]?legs?[_ -]?fok[_ -]?required|"
    r"multi[_ -]?leg[_ -]?fok|full[_ -]?basket[_ -]?fok|"
    r"execute[_ -]?all[_ -]?legs)",
    re.IGNORECASE,
)
_SINGLE_BUCKET_FOK_TEXT = re.compile(
    r"(?:single[_ -]?(?:bucket|taker)[_ -]?fok|fok[_ -]?bucket)",
    re.IGNORECASE,
)
_RECOMPUTE_AFTER_FILL_TEXT = re.compile(
    r"(?:recompute[_ -]?(?:after[_ -]?)?(?:fill|execution)|"
    r"recalculate[_ -]?(?:after[_ -]?)?(?:fill|execution)|"
    r"post[_ -]?fill[_ -]?recompute)",
    re.IGNORECASE,
)


def _inside(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def _excerpt(source: str, line: int) -> str:
    lines = source.splitlines()
    value = lines[line - 1].strip() if 0 < line <= len(lines) else ""
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_EXCERPT_BYTES:
        value = encoded[:MAX_EXCERPT_BYTES].decode("utf-8", errors="ignore")
    return value


def _docstring_positions(tree: ast.AST) -> set[tuple[int, int]]:
    positions: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            positions.add((int(value.lineno), int(value.col_offset)))
    return positions


def _active_tokens(source: str, tree: ast.AST) -> list[tokenize.TokenInfo]:
    """Return tokens that can represent executable semantics.

    Comments and docstrings are deliberately excluded.  Runtime string
    literals remain because reason codes and contract constants are executable
    inputs to the target state machine.
    """

    doc_positions = _docstring_positions(tree)
    result: list[tokenize.TokenInfo] = []
    try:
        stream = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in stream:
            if token.type in {
                tokenize.ENCODING,
                tokenize.ENDMARKER,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.COMMENT,
            }:
                continue
            if token.type == tokenize.STRING and token.start in doc_positions:
                continue
            result.append(token)
    except (IndentationError, tokenize.TokenError):
        # AST parsing already records syntax errors.  Keeping an empty token
        # stream makes the receipt deterministic and bounded for that file.
        return []
    return result


def _token_value(token: tokenize.TokenInfo) -> str:
    if token.type == tokenize.STRING:
        try:
            value = ast.literal_eval(token.string)
        except (SyntaxError, ValueError):
            return token.string
        return value if isinstance(value, str) else token.string
    return token.string


def _active_blob(tokens: Sequence[tokenize.TokenInfo]) -> str:
    return " ".join(_token_value(token) for token in tokens)


def _function_nodes_without_nested_functions(node: ast.AST) -> Iterable[ast.AST]:
    """Walk one function body without attributing nested functions to parents."""

    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _function_nodes_without_nested_functions(child)


def _function_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for item in _function_nodes_without_nested_functions(node):
        if isinstance(item, ast.Name):
            names.add(item.id)
        elif isinstance(item, ast.Attribute):
            names.add(item.attr)
        elif isinstance(item, ast.Constant) and isinstance(item.value, str):
            names.add(item.value)
    return names


def _function_has_attribute(node: ast.AST, attribute: str) -> bool:
    return any(
        isinstance(item, ast.Attribute) and item.attr == attribute
        for item in _function_nodes_without_nested_functions(node)
    )


def _function_has_confirmed_cancel_before_fok(node: ast.AST) -> bool:
    """Require a lexical cancel-confirmation gate before the FOK candidate.

    This is deliberately a conservative static contract check.  It does not
    attempt to prove every branch, but it prevents a router from passing merely
    because it mentions both a cancel flag and ``TAKER_FOK`` somewhere in the
    same function.  A branch-sensitive runtime gate remains the implementation
    responsibility; any ambiguous/unknown state is separately required to
    expose ``reconcile_required``.
    """

    cancel_names = {
        "cancel_acknowledged",
        "confirmed_cancel",
        "cancel_confirmed",
    }
    cancel_positions: list[tuple[int, int]] = []
    fok_positions: list[tuple[int, int]] = []
    for item in _function_nodes_without_nested_functions(node):
        position = (int(getattr(item, "lineno", 0)), int(getattr(item, "col_offset", 0)))
        if isinstance(item, ast.Name) and item.id in cancel_names:
            cancel_positions.append(position)
        elif isinstance(item, ast.Attribute) and item.attr in cancel_names:
            cancel_positions.append(position)
        elif isinstance(item, ast.Name) and item.id == "TAKER_FOK":
            fok_positions.append(position)
        elif isinstance(item, ast.Attribute) and item.attr == "TAKER_FOK":
            fok_positions.append(position)
    return bool(cancel_positions and fok_positions) and min(cancel_positions) < min(fok_positions)


def _literal_sequence(node: ast.AST) -> tuple[Any, ...] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    values: list[Any] = []
    for item in node.elts:
        if not isinstance(item, ast.Constant) or isinstance(item.value, bool):
            return None
        values.append(item.value)
    return tuple(values)


def _append_hit(
    hits: list[dict[str, Any]],
    *,
    code: str,
    relative: str,
    line: int,
    source: str,
    evidence: str,
    kind: str = "forbidden_semantics",
) -> None:
    if len(hits) >= MAX_VIOLATIONS:
        return
    item = {
        "code": code,
        "kind": kind,
        "file": relative,
        "line": int(line),
        "evidence": evidence,
        "excerpt": _excerpt(source, line),
    }
    identity = (item["code"], item["file"], item["line"], item["evidence"])
    if not any(
        (existing["code"], existing["file"], existing["line"], existing["evidence"])
        == identity
        for existing in hits
    ):
        hits.append(item)


def _scan_file(relative: str, source: str, tree: ast.AST) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    tokens = _active_tokens(source, tree)

    for token in tokens:
        value = _token_value(token)
        if _FIXED_STAGE_TEXT.search(value):
            _append_hit(
                hits,
                code="forbidden_fixed_stage_sequence",
                relative=relative,
                line=token.start[0],
                source=source,
                evidence=value,
            )
        if _EQUAL_SHARE_TEXT.search(value):
            _append_hit(
                hits,
                code="forbidden_equal_share_semantics",
                relative=relative,
                line=token.start[0],
                source=source,
                evidence=value,
            )
        if _ALL_LEG_FOK_TEXT.search(value):
            _append_hit(
                hits,
                code="forbidden_all_legs_fok_semantics",
                relative=relative,
                line=token.start[0],
                source=source,
                evidence=value,
            )

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in {
                "_apply_immediate_execution_stage_gate",
                "require_all_legs_fok",
                "execute_all_legs_fok",
            }:
                _append_hit(
                    hits,
                    code="forbidden_all_legs_fok_function",
                    relative=relative,
                    line=node.lineno,
                    source=source,
                    evidence=node.name,
                )
            names = _function_names(node)
            is_execution_router = (
                _function_has_attribute(node, "TAKER_FOK")
                and ("execution" in names or "execution_candidate" in names)
                and node.name != "simulate_taker_fok"
                and not node.name.startswith("build_target_taker_candidate")
            )
            if is_execution_router and not _function_has_confirmed_cancel_before_fok(node):
                _append_hit(
                    hits,
                    code="fok_without_confirmed_cancel_gate",
                    relative=relative,
                    line=node.lineno,
                    source=source,
                    evidence=node.name,
                )

        sequence = _literal_sequence(node)
        if sequence in {(4, 3, 2), ("4", "3", "2")}:
            _append_hit(
                hits,
                code="forbidden_fixed_stage_sequence",
                relative=relative,
                line=getattr(node, "lineno", 1),
                source=source,
                evidence=repr(sequence),
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "range":
            args = tuple(
                item.value
                for item in node.args
                if isinstance(item, ast.Constant) and not isinstance(item.value, bool)
            )
            if args == (4, 1, -1):
                _append_hit(
                    hits,
                    code="forbidden_fixed_stage_sequence",
                    relative=relative,
                    line=node.lineno,
                    source=source,
                    evidence="range(4, 1, -1)",
                )

    return hits


def _required_contract_checks(
    *,
    source_by_file: Mapping[str, str],
    tree_by_file: Mapping[str, ast.AST],
    active_by_file: Mapping[str, Sequence[tokenize.TokenInfo]],
) -> dict[str, bool]:
    blob = "\n".join(_active_blob(tokens) for tokens in active_by_file.values())
    all_function_names: set[str] = set()
    fok_router_has_cancel = False
    for tree in tree_by_file.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                all_function_names.add(node.name)
                names = _function_names(node)
                if (
                    _function_has_attribute(node, "TAKER_FOK")
                    and ("execution" in names or "execution_candidate" in names)
                    and _function_has_confirmed_cancel_before_fok(node)
                ):
                    fok_router_has_cancel = True

    live_false = False
    for tree in tree_by_file.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in {"live_authorized", "live_submit_authorized"}
                    and isinstance(value, ast.Constant)
                    and value.value is False
                ):
                    live_false = True

    return {
        "target_inventory_contract_present": (
            "target_shares" in blob and "delta_shares" in blob
        ),
        "post_only_maker_contract_present": (
            "MAKER" in blob
            and ("maker_post_only" in blob or "post_only" in blob)
        ),
        "confirmed_cancel_precedes_fok_contract_present": (
            fok_router_has_cancel
            or (
                "confirmed_cancel_for_fok" in blob
                and "cancel_acknowledged" in blob
                and "FOK" in blob
            )
        ),
        "single_bucket_fok_contract_present": bool(
            _SINGLE_BUCKET_FOK_TEXT.search(blob)
            or {"single_bucket_fok", "single_bucket"} & all_function_names
        ),
        "recompute_after_fill_contract_present": bool(
            _RECOMPUTE_AFTER_FILL_TEXT.search(blob)
            or any(
                "recompute" in name.lower() and "fill" in name.lower()
                for name in all_function_names
            )
        ),
        "unknown_or_unconfirmed_cancel_reconcile_contract_present": (
            "reconcile_required" in blob
            and "unknown" in blob
            and "cancel_acknowledged" in blob
        ),
        "production_no_live_live_authorized_false": (
            "production_no_live" in blob and live_false
        ),
        "runtime_sources_parsed": bool(source_by_file) and bool(tree_by_file),
    }


def audit(
    *,
    project_root: Path = ROOT,
    scope_files: Sequence[str] | None = None,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    """Audit the bounded target execution scope without side effects."""

    root = project_root.expanduser().resolve()
    requested = tuple(scope_files or DEFAULT_RUNTIME_SCOPE)
    scope_resource_hits: list[dict[str, Any]] = []
    if len(requested) > max_files:
        scope_resource_hits.append({
            "code": "resource_file_count_exceeded",
            "requested": len(requested),
            "maximum": int(max_files),
        })
        requested = requested[: max(0, int(max_files))]

    source_by_file: dict[str, str] = {}
    tree_by_file: dict[str, ast.AST] = {}
    active_by_file: dict[str, Sequence[tokenize.TokenInfo]] = {}
    violations: list[dict[str, Any]] = []
    total_bytes = 0
    for raw in requested:
        raw_path = Path(raw)
        path = raw_path if raw_path.is_absolute() else root / raw_path
        resolved = path.resolve()
        relative = str(resolved.relative_to(root)) if _inside(root, resolved) else str(raw)
        if not _inside(root, resolved):
            scope_resource_hits.append({
                "code": "scope_path_outside_project",
                "file": str(raw),
            })
            continue
        relative_parts = Path(relative).parts
        if any(part in _EXCLUDED_SCOPE_DIRS for part in relative_parts[:-1]):
            continue
        if not resolved.is_file():
            scope_resource_hits.append({
                "code": "scope_file_missing",
                "file": relative,
            })
            continue
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            scope_resource_hits.append({
                "code": "scope_file_stat_failed",
                "file": relative,
                "error": str(exc),
            })
            continue
        if size > max_file_bytes:
            scope_resource_hits.append({
                "code": "resource_file_bytes_exceeded",
                "file": relative,
                "bytes": size,
                "maximum": int(max_file_bytes),
            })
            continue
        if total_bytes + size > max_total_bytes:
            scope_resource_hits.append({
                "code": "resource_total_bytes_exceeded",
                "file": relative,
                "bytes": total_bytes + size,
                "maximum": int(max_total_bytes),
            })
            continue
        try:
            source = resolved.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, UnicodeError, SyntaxError) as exc:
            scope_resource_hits.append({
                "code": "scope_file_parse_failed",
                "file": relative,
                "error": str(exc),
            })
            continue
        total_bytes += size
        source_by_file[relative] = source
        tree_by_file[relative] = tree
        active_by_file[relative] = _active_tokens(source, tree)
        violations.extend(_scan_file(relative, source, tree))

    if len(violations) > MAX_VIOLATIONS:
        violations = violations[:MAX_VIOLATIONS]

    checks = _required_contract_checks(
        source_by_file=source_by_file,
        tree_by_file=tree_by_file,
        active_by_file=active_by_file,
    )
    checks.update({
        "forbidden_fixed_stage_sequence_absent": not any(
            item["code"] == "forbidden_fixed_stage_sequence" for item in violations
        ),
        "forbidden_equal_share_semantics_absent": not any(
            item["code"] == "forbidden_equal_share_semantics" for item in violations
        ),
        "forbidden_all_legs_fok_semantics_absent": not any(
            item["code"] in {
                "forbidden_all_legs_fok_semantics",
                "forbidden_all_legs_fok_function",
            }
            for item in violations
        ),
        "scope_resource_bounds_ok": not scope_resource_hits,
    })
    identity: dict[str, Any] = {
        "schema": SCHEMA,
        "constitution_version": CONSTITUTION_VERSION,
        "execution_contract": (
            "target_inventory->post_only_maker->confirmed_cancel->"
            "bounded_single_bucket_fok->recompute_after_fill"
        ),
        "runtime_mode": "production_no_live",
        "scope": {
            "project_root": str(root),
            "included_files": sorted(source_by_file),
            "requested_files": list(requested),
            "excluded_classes": [
                "docs",
                "tests",
                "data",
                "output",
                "legacy_or_source_account_lanes",
            ],
            "max_files": int(max_files),
            "max_file_bytes": int(max_file_bytes),
            "max_total_bytes": int(max_total_bytes),
            "read_bytes": total_bytes,
        },
        "checks": checks,
        "violations": violations,
        "scope_resource_hits": scope_resource_hits,
        "boundary": {
            "read_only_audit": True,
            "opens_database": False,
            "network_access": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_authorized": False,
        },
    }
    receipt_sha = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **identity,
        "ok": not scope_resource_hits and all(checks.values()),
        "receipt_sha256": receipt_sha,
    }


def _write_bounded_json(path: Path, value: Mapping[str, Any], *, root: Path) -> None:
    resolved = path.expanduser().resolve()
    if not _inside(root, resolved):
        raise ValueError("audit_output_outside_project")
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(rendered.encode("utf-8")) > 512_000:
        raise ValueError("audit_output_resource_limit_exceeded")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-files", type=int, default=MAX_FILES)
    parser.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL_BYTES)
    args = parser.parse_args()
    if min(args.max_files, args.max_file_bytes, args.max_total_bytes) <= 0:
        raise SystemExit("resource limits must be positive")
    root = args.root.expanduser().resolve()
    result = audit(
        project_root=root,
        max_files=args.max_files,
        max_file_bytes=args.max_file_bytes,
        max_total_bytes=args.max_total_bytes,
    )
    if args.output:
        _write_bounded_json(args.output, result, root=root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
