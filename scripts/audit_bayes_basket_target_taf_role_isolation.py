#!/usr/bin/env python3
"""Bounded, read-only audit of the target TAF role boundary.

The target has two deliberately disjoint event contracts:

* ``TAF``/``AMD``/``COR`` are forecast revisions and may only reach the
  ``ForecastRevision`` -> TAF revision -> ``P0-M`` route.
* ``METAR``/``SPECI`` are canonical observations and may only reach the
  canonical observation -> ``P1`` route (and the independent hard-NO lane).

This module does not open a database, import a service, make a network call,
or execute production code.  It combines small in-memory contract probes with
an AST audit of the source route.  All reads and all receipt fields are
bounded.  A receipt is written with a same-directory temporary file followed
by ``os.replace`` when the caller explicitly requests an output path.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.event_contracts import (  # noqa: E402
    CanonicalObservationEvent,
    ForecastRevision,
    validate_canonical_observation_sequence,
    validate_forecast_revision_chain,
)


RECEIPT_SCHEMA = "bayes_basket_target_taf_role_isolation_audit_v1"
DEFAULT_SOURCE_ROOT = ROOT
DEFAULT_OUTPUT = ROOT / "output" / "audits" / (
    "bayes_basket_target_taf_role_isolation_audit.json"
)

# These are deliberately conservative.  The audit must stop before an
# unbounded directory, source file, AST, or receipt can consume the machine.
MAX_FILES = 96
MAX_SOURCE_BYTES_PER_FILE = 768 * 1024
MAX_TOTAL_SOURCE_BYTES = 8 * 1024 * 1024
MAX_AST_NODES_PER_FILE = 120_000
MAX_FINDINGS = 96
MAX_RECEIPT_BYTES = 1024 * 1024

# The source contract is intentionally explicit.  A missing route file is a
# blocker rather than an implicit pass based on the remaining files.
REQUIRED_SOURCE_FILES = (
    "bayes_basket_target/event_contracts.py",
    "bayes_basket_target/taf_revision.py",
    "bayes_basket_target/materialization.py",
    "bayes_basket_target/canonical_ingress.py",
    "bayes_basket_target/canonical_fact_ingress.py",
    "bayes_basket_target/resident_canonical.py",
    "bayes_basket_target/production_worker.py",
    "bayes_basket_target/p1_compiler.py",
    "bayes_basket_target/atomic_ledger.py",
    "scripts/capture_bayes_basket_target_awc_taf_revision.py",
)

_FORECAST_WORDS = frozenset(
    {
        "taf",
        "amd",
        "cor",
        "forecast",
        "forecastrevision",
        "forecast_revision",
        "tafrevision",
        "taf_revision",
    }
)
_CANONICAL_WORDS = frozenset(
    {
        "canonical",
        "canonicalobservation",
        "canonical_observation",
        "metar",
        "speci",
        "observation",
        "observationevent",
        "observation_event",
        "p1",
        "materializep1",
        "materialize_p1",
    }
)
_HARD_NO_WORDS = frozenset(
    {
        "hard_no",
        "hardno",
        "hard-no",
        "station_day_bounds",
        "stationdaybounds",
        "reported_trajectory_no",
        "reportedtrajectoryno",
    }
)

_FORECAST_CALLS = frozenset(
    {
        "forecastrevision",
        "compile_taf_revision_proof",
        "materialize_p0m",
        "validate_forecast_revision_chain",
    }
)
_CANONICAL_CALLS = frozenset(
    {
        "canonicalobservationevent",
        "validate_canonical_observation_sequence",
        "ingest_canonical_observation_evidence",
        "materialize_p1_from_canonical_observation",
        "materialize_p1",
        "load_resident_formal_observations",
    }
)
_HARD_NO_CALLS = frozenset(
    {
        "station_day_bounds",
        "stationdaybounds",
        "commit_blocked_scope_decision",
    }
)
_WRITE_CALLS = frozenset(
    {
        "commit",
        "rollback",
        "insert_probability_envelope",
        "ingest_canonical_observation_evidence",
        "write",
        "write_text",
        "write_bytes",
        "persist",
        "upsert",
        "save",
        "_writer_transaction",
    }
)

_SQL_WRITE_PREFIX = re.compile(
    r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|VACUUM|ATTACH|DETACH|PRAGMA\s+(?:journal_mode|user_version|schema_version))\b",
    re.IGNORECASE,
)


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _compact(value: Any, *, depth: int = 0) -> Any:
    """Bound untrusted AST/error details before they enter a receipt."""

    if depth > 4:
        return "<depth-limit>"
    if isinstance(value, Mapping):
        return {
            str(key)[:120]: _compact(child, depth=depth + 1)
            for key, child in list(value.items())[:32]
        }
    if isinstance(value, (list, tuple)):
        return [_compact(item, depth=depth + 1) for item in list(value)[:32]]
    if isinstance(value, str):
        return value if len(value) <= 640 else value[:640] + "..."
    return value


def _finding(code: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"code": str(code)}
    if details:
        result["details"] = _compact(details)
    return result


def _bounded_findings(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        if len(result) >= MAX_FINDINGS:
            break
        result.append(dict(_compact(dict(item))))
    return result


def _name_text(value: str) -> str:
    return str(value).strip().lower().replace("-", "_")


def _classify_word(value: str) -> set[str]:
    normalized = _name_text(value)
    compact = normalized.replace("_", "")
    result: set[str] = set()
    if (
        normalized in _FORECAST_WORDS
        or normalized.startswith(("taf_", "forecast_"))
        or compact in {
        item.replace("_", "") for item in _FORECAST_WORDS
        }
    ):
        result.add("forecast")
    elif (
        normalized in _CANONICAL_WORDS
        or normalized.startswith(("canonical_", "metar", "speci", "p1_"))
        or compact in {
        item.replace("_", "") for item in _CANONICAL_WORDS
        }
    ):
        result.add("canonical")
    elif (
        normalized in _HARD_NO_WORDS
        or normalized.startswith(("hard_no", "station_day_bounds", "reported_trajectory_no"))
        or compact in {
        item.replace("_", "") for item in _HARD_NO_WORDS
        }
    ):
        result.add("hard_no")
    return result


def _semantic_tokens(node: ast.AST) -> tuple[set[str], set[str]]:
    """Return domain tags and call names, excluding only docstrings."""

    tags: set[str] = set()
    calls: set[str] = set()
    stack: list[ast.AST] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Name):
            tags.update(_classify_word(current.id))
        elif isinstance(current, ast.Attribute):
            tags.update(_classify_word(current.attr))
        elif isinstance(current, ast.arg):
            tags.update(_classify_word(current.arg))
        elif isinstance(current, ast.Constant) and isinstance(current.value, str):
            # Long SQL/text blobs are still inspected for exact route names;
            # truncating their contribution prevents pathological inputs.
            text = current.value[:4096]
            for part in text.replace("\"", " ").replace("'", " ").split():
                tags.update(_classify_word(part))
            for word in (
                "TAF",
                "AMD",
                "COR",
                "METAR",
                "SPECI",
                "ForecastRevision",
                "CanonicalObservationEvent",
                "station_day_bounds",
                "hard_no",
            ):
                if re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(word.lower())}(?![A-Za-z0-9_])",
                    text.lower(),
                ):
                    tags.update(_classify_word(word))
        elif isinstance(current, ast.Call):
            func = current.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if name:
                normalized = _name_text(name)
                calls.add(normalized)
                if normalized in _FORECAST_CALLS:
                    tags.add("forecast")
                if normalized in _CANONICAL_CALLS:
                    tags.add("canonical")
                if normalized in _HARD_NO_CALLS:
                    tags.add("hard_no")
                if (
                    normalized in _WRITE_CALLS
                    or normalized.startswith(("write_", "persist_", "upsert_", "save_"))
                ):
                    calls.add("__writer__")
                if normalized in {"execute", "executemany", "executescript"}:
                    sql_text = ""
                    if current.args and isinstance(current.args[0], ast.Constant):
                        sql_text = str(current.args[0].value or "")
                    elif current.args and isinstance(current.args[0], ast.JoinedStr):
                        sql_text = "".join(
                            str(part.value or "")
                            for part in current.args[0].values
                            if isinstance(part, ast.Constant)
                        )
                    if _SQL_WRITE_PREFIX.search(sql_text):
                        calls.add("__writer__")
        stack.extend(ast.iter_child_nodes(current))
    return tags, calls


def _node_count(tree: ast.AST, limit: int) -> int:
    count = 0
    stack: list[ast.AST] = [tree]
    while stack:
        current = stack.pop()
        count += 1
        if count > limit:
            return count
        stack.extend(ast.iter_child_nodes(current))
    return count


def _function_scopes(tree: ast.AST) -> list[tuple[str, ast.AST, str, int]]:
    result: list[tuple[str, ast.AST, str, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                result.append((name, child, child.name, int(child.lineno)))
                visit(child, name)
            elif isinstance(child, ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                visit(child, name)
            else:
                visit(child, prefix)

    visit(tree, "")
    return result[: MAX_FINDINGS * 4]


def _class_scopes(tree: ast.AST) -> list[tuple[str, ast.ClassDef, str, int]]:
    result: list[tuple[str, ast.ClassDef, str, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                result.append((name, child, child.name, int(child.lineno)))
                visit(child, name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Functions can contain local classes; retain those classes
                # without treating the function itself as a class route.
                visit(child, prefix)
            else:
                visit(child, prefix)

    visit(tree, "")
    return result[: MAX_FINDINGS * 2]


def _is_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return bool(
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    )


def _function_body_without_docstring(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> ast.AST:
    # A temporary Module preserves the body while removing prose that can
    # mention both roles in an explanatory docstring.
    body = list(node.body[1:] if _is_docstring(node) else node.body)
    return ast.Module(body=body, type_ignores=[])


def _function_semantic_tokens(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[set[str], set[str]]:
    """Include typed route arguments while excluding prose docstrings."""

    body = _function_body_without_docstring(node)
    args = ast.Module(body=[node.args], type_ignores=[])
    body_tags, body_calls = _semantic_tokens(body)
    arg_tags, arg_calls = _semantic_tokens(args)
    return body_tags | arg_tags, body_calls | arg_calls


def _string_sets(node: ast.AST) -> list[frozenset[str]]:
    result: list[frozenset[str]] = []
    for current in ast.walk(node):
        if not isinstance(current, ast.Set):
            continue
        values = {
            str(item.value).upper()
            for item in current.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        if values:
            result.append(frozenset(values))
    return result


def _string_constant_assignments(tree: ast.AST) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if not isinstance(node.value.value, str):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = str(node.value.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                result[node.target.id] = str(node.value.value)
    return dict(list(result.items())[:128])


def _string_literals(node: ast.AST) -> set[str]:
    return {
        str(current.value)
        for current in ast.walk(node)
        if isinstance(current, ast.Constant) and isinstance(current.value, str)
    }


def _name_occurs(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(current, ast.Name) and current.id == name
        for current in ast.walk(node)
    )


def _bounded_source_files(root: Path, *, max_files: int) -> tuple[list[Path], list[dict[str, Any]]]:
    """Resolve only declared route files and enforce the route-file cap."""

    files: list[Path] = []
    findings: list[dict[str, Any]] = []
    if not root.is_dir():
        return files, [_finding("source_root_missing", path=str(root))]
    if max_files < len(REQUIRED_SOURCE_FILES):
        return [], [
            _finding(
                "scan_file_limit_exceeded",
                discovered=len(REQUIRED_SOURCE_FILES),
                limit=max_files,
            )
        ]
    for relative in REQUIRED_SOURCE_FILES:
        path = root / relative
        if path.is_file():
            files.append(path)
    return files, findings


def _read_and_parse(
    path: Path,
    *,
    root: Path,
    total_bytes: int,
    max_source_bytes: int,
    max_total_bytes: int,
    max_ast_nodes: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int]:
    relative = str(path.relative_to(root))
    findings: list[dict[str, Any]] = []
    try:
        size = int(path.stat().st_size)
    except OSError as exc:
        return None, [_finding("source_file_unreadable", file=relative, error=str(exc))], total_bytes
    if size > max_source_bytes:
        return None, [
            _finding(
                "source_file_size_limit_exceeded",
                file=relative,
                bytes=size,
                limit=max_source_bytes,
            )
        ], total_bytes
    if total_bytes + size > max_total_bytes:
        return None, [
            _finding(
                "source_total_size_limit_exceeded",
                file=relative,
                projected_bytes=total_bytes + size,
                limit=max_total_bytes,
            )
        ], total_bytes
    try:
        source = path.read_bytes()
    except OSError as exc:
        return None, [_finding("source_file_unreadable", file=relative, error=str(exc))], total_bytes
    if len(source) != size:
        return None, [_finding("source_file_changed_during_read", file=relative)], total_bytes
    digest = _sha256_bytes(source)
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, [_finding("source_encoding_invalid", file=relative, error=str(exc))], total_bytes + size
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError as exc:
        return None, [
            _finding(
                "source_syntax_invalid",
                file=relative,
                line=int(exc.lineno or 0),
                error=str(exc.msg or exc),
            )
        ], total_bytes + size
    nodes = _node_count(tree, max_ast_nodes)
    if nodes > max_ast_nodes:
        return None, [
            _finding(
                "ast_node_limit_exceeded",
                file=relative,
                nodes=nodes,
                limit=max_ast_nodes,
            )
        ], total_bytes + size
    return {
        "path": relative,
        "bytes": size,
        "sha256": digest,
        "tree": tree,
        "source": source,
        "ast_nodes": nodes,
    }, findings, total_bytes + size


def _base_revision(kind: str, sequence: int, supersedes: str | None) -> dict[str, Any]:
    return {
        "schema": "bayes_basket_forecast_revision_v1",
        "revision_id": f"rev-{kind.lower()}-{sequence}",
        "station_id": "TEST",
        "event_date": "2026-07-27",
        "revision_kind": kind,
        "sequence": sequence,
        "issued_at_utc": f"2026-07-27T{10 + sequence:02d}:00:00Z",
        "received_at_utc": f"2026-07-27T{10 + sequence:02d}:05:00Z",
        "valid_from_utc": "2026-07-27T12:00:00Z",
        "valid_until_utc": "2026-07-28T12:00:00Z",
        "source_object_sha256": hashlib.sha256(
            f"source-{kind}-{sequence}".encode("utf-8")
        ).hexdigest(),
        "supersedes_revision_id": supersedes,
    }


def _base_observation(report_type: str, sequence: int, supersedes: str | None) -> dict[str, Any]:
    return {
        "schema": "bayes_basket_canonical_observation_event_v1",
        "observation_id": f"obs-{report_type.lower()}-{sequence}",
        "station_id": "TEST",
        "event_date": "2026-07-27",
        "source_lane": "amsc",
        "report_type": report_type,
        "sequence": sequence,
        "observed_at_utc": f"2026-07-27T{10 + sequence:02d}:00:00Z",
        "received_at_utc": f"2026-07-27T{10 + sequence:02d}:05:00Z",
        "raw_sha256": hashlib.sha256(
            f"raw-{report_type}-{sequence}".encode("utf-8")
        ).hexdigest(),
        "qc_state": "REVIEW_REQUIRED" if supersedes else "ACCEPTED",
        "supersedes_observation_id": supersedes,
    }


def _probe_contracts() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    forecast_results: dict[str, bool] = {}
    forecast_rows: list[ForecastRevision] = []
    predecessor: str | None = None
    for sequence, kind in enumerate(("TAF", "AMD", "COR"), start=1):
        raw = _base_revision(kind, sequence, predecessor)
        try:
            revision = ForecastRevision.from_mapping(raw)
            forecast_rows.append(revision)
            forecast_results[kind] = True
            predecessor = revision.revision_id
        except (ContractError, TypeError, ValueError) as exc:
            forecast_results[kind] = False
            blockers.append(_finding("forecast_revision_contract_rejected", kind=kind, error=str(exc)))
    try:
        validate_forecast_revision_chain(forecast_rows)
        forecast_chain = True
    except (ContractError, TypeError, ValueError) as exc:
        forecast_chain = False
        blockers.append(_finding("forecast_revision_chain_probe_failed", error=str(exc)))

    canonical_results: dict[str, bool] = {}
    canonical_rows: list[CanonicalObservationEvent] = []
    predecessor = None
    for sequence, report_type in enumerate(("METAR", "SPECI"), start=1):
        raw = _base_observation(report_type, sequence, predecessor)
        try:
            observation = CanonicalObservationEvent.from_mapping(raw)
            canonical_rows.append(observation)
            canonical_results[report_type] = True
            predecessor = observation.observation_id
        except (ContractError, TypeError, ValueError) as exc:
            canonical_results[report_type] = False
            blockers.append(_finding("canonical_observation_contract_rejected", report_type=report_type, error=str(exc)))
    try:
        validate_canonical_observation_sequence(canonical_rows)
        canonical_sequence = True
    except (ContractError, TypeError, ValueError) as exc:
        canonical_sequence = False
        blockers.append(_finding("canonical_observation_sequence_probe_failed", error=str(exc)))

    unknown_kind_rejected = True
    for kind in ("UNKNOWN", "METAR", "SPECI", "OBSERVATION"):
        raw = _base_revision(kind, 1, None)
        try:
            ForecastRevision.from_mapping(raw)
        except (ContractError, TypeError, ValueError):
            continue
        unknown_kind_rejected = False
        blockers.append(_finding("unknown_kind_not_rejected", kind=kind, contract="ForecastRevision"))

    taf_not_canonical = True
    for kind in ("TAF", "AMD", "COR"):
        raw = _base_observation(kind, 1, None)
        try:
            CanonicalObservationEvent.from_mapping(raw)
        except (ContractError, TypeError, ValueError):
            continue
        taf_not_canonical = False
        blockers.append(_finding("taf_accepted_as_canonical_observation", report_type=kind))

    metar_not_forecast = True
    for kind in ("METAR", "SPECI"):
        raw = _base_revision(kind, 1, None)
        try:
            ForecastRevision.from_mapping(raw)
        except (ContractError, TypeError, ValueError):
            continue
        metar_not_forecast = False
        blockers.append(_finding("metar_accepted_as_forecast_revision", report_type=kind))

    checks = {
        "forecast_taf_amd_cor_contract": all(forecast_results.values()) and forecast_chain,
        "canonical_metar_speci_contract": all(canonical_results.values()) and canonical_sequence,
        "unknown_kind_rejected": unknown_kind_rejected,
        "taf_not_canonical": taf_not_canonical,
        "metar_speci_not_forecast": metar_not_forecast,
    }
    return {
        "checks": checks,
        "forecast_revision_kinds": forecast_results,
        "forecast_chain_valid": forecast_chain,
        "canonical_report_types": canonical_results,
        "canonical_sequence_valid": canonical_sequence,
        "negative_cases": {
            "unknown_kind_rejected": unknown_kind_rejected,
            "taf_amd_cor_rejected_by_canonical": taf_not_canonical,
            "metar_speci_rejected_by_forecast": metar_not_forecast,
        },
    }, blockers


def _audit_route_functions(
    parsed: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    for item in parsed:
        relative = str(item["path"])
        tree = item["tree"]
        for qualified, node, short_name, line in _function_scopes(tree):
            tags, calls = _function_semantic_tokens(node)  # type: ignore[arg-type]
            is_writer = "__writer__" in calls
            record = {
                "file": relative,
                "function": qualified,
                "line": line,
                "domains": sorted(tags),
                "writer": is_writer,
            }
            functions.append(record)
            if not is_writer:
                continue
            if "forecast" in tags and "canonical" in tags:
                findings.append(
                    _finding(
                        "shared_writer",
                        file=relative,
                        function=qualified,
                        line=line,
                        domains=sorted(tags),
                    )
                )
            if "forecast" in tags and "hard_no" in tags:
                findings.append(
                    _finding(
                        "taf_writes_hard_no_or_station_day_bounds",
                        file=relative,
                        function=qualified,
                        line=line,
                    )
                )
            if "forecast" in tags and "canonical" in tags:
                findings.append(
                    _finding(
                        "taf_writes_canonical_or_p1",
                        file=relative,
                        function=qualified,
                        line=line,
                    )
                )
            if "canonical" in tags and "forecast" in tags:
                findings.append(
                    _finding(
                        "metar_speci_writes_forecast_revision",
                        file=relative,
                        function=qualified,
                        line=line,
                    )
                )
            if "forecast" in tags and not (
                "materialize_p0m" in calls
                or (
                    relative == "scripts/capture_bayes_basket_target_awc_taf_revision.py"
                    and short_name in {"refresh_forward_taf_inbox", "main"}
                )
            ):
                findings.append(
                    _finding(
                        "forecast_writer_outside_taf_revision_or_inbox",
                        file=relative,
                        function=qualified,
                        line=line,
                    )
                )
    functions.sort(key=lambda item: (str(item["file"]), int(item["line"]), str(item["function"])))
    return {
        "function_count": len(functions),
        "writer_function_count": sum(1 for item in functions if item["writer"]),
        "functions": functions[:MAX_FINDINGS],
    }, _bounded_findings(findings)


def _audit_required_routes(parsed: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    by_file = {str(item["path"]): item["tree"] for item in parsed}
    findings: list[dict[str, Any]] = []
    route_requirements = {
        "bayes_basket_target/event_contracts.py": {
            "ForecastRevision": {"forecast"},
            "CanonicalObservationEvent": {"canonical"},
        },
        "bayes_basket_target/taf_revision.py": {
            "compile_taf_revision_proof": {"forecast"},
        },
        "bayes_basket_target/materialization.py": {
            "materialize_p0m": {"forecast"},
            "materialize_p1": {"canonical"},
        },
        "bayes_basket_target/canonical_ingress.py": {
            "materialize_p1_from_canonical_observation": {"canonical"},
        },
        "bayes_basket_target/canonical_fact_ingress.py": {
            "ingest_canonical_observation_evidence": {"canonical"},
        },
        "bayes_basket_target/production_worker.py": {
            "_materialize_resident_p0m": {"forecast"},
            "_materialize_resident_p1": {"canonical"},
        },
        "scripts/capture_bayes_basket_target_awc_taf_revision.py": {
            "capture_forward_awc_taf": {"forecast"},
            "refresh_forward_taf_inbox": {"forecast"},
        },
    }
    route_records: list[dict[str, Any]] = []
    for relative, requirements in route_requirements.items():
        tree = by_file.get(relative)
        if tree is None:
            continue
        scopes = {
            short_name: (qualified, node, line)
            for qualified, node, short_name, line in _function_scopes(tree)
            if short_name in requirements
        }
        for function_name, expected in requirements.items():
            if relative == "bayes_basket_target/event_contracts.py" and function_name in {
                "ForecastRevision",
                "CanonicalObservationEvent",
            }:
                class_found = next(
                    (
                        (qualified, node, line)
                        for qualified, node, short_name, line in _class_scopes(tree)
                        if short_name == function_name
                    ),
                    None,
                )
                if class_found is None:
                    findings.append(
                        _finding("required_route_missing", file=relative, function=function_name)
                    )
                    continue
                qualified, node, line = class_found
                tags, _ = _semantic_tokens(node)
                route_records.append({
                    "file": relative,
                    "function": qualified,
                    "line": line,
                    "domains": sorted(tags),
                    "expected_domains": sorted(expected),
                })
                if not expected.issubset(tags):
                    findings.append(
                        _finding(
                            "required_route_domain_missing",
                            file=relative,
                            function=qualified,
                            expected=sorted(expected),
                            actual=sorted(tags),
                        )
                    )
                continue
            found = scopes.get(function_name)
            if found is None:
                findings.append(_finding("required_route_missing", file=relative, function=function_name))
                continue
            qualified, node, line = found
            tags, calls = _function_semantic_tokens(node)  # type: ignore[arg-type]
            route_records.append({
                "file": relative,
                "function": qualified,
                "line": line,
                "domains": sorted(tags),
                "expected_domains": sorted(expected),
            })
            if not expected.issubset(tags):
                findings.append(
                    _finding(
                        "required_route_domain_missing",
                        file=relative,
                        function=qualified,
                        expected=sorted(expected),
                        actual=sorted(tags),
                    )
                )
            forbidden = set(tags) & ({"canonical", "hard_no"} if "forecast" in expected else {"forecast"})
            # P0-M/P1 functions are pure in-memory compilers and are allowed
            # to call the single probability insert helper only in the
            # resident wrapper; route crossing is handled by the writer pass.
            if forbidden:
                findings.append(
                    _finding(
                        "required_route_domain_crossing",
                        file=relative,
                        function=qualified,
                        forbidden=sorted(forbidden),
                    )
                    )
    contract_allowlists = {
        ("bayes_basket_target/event_contracts.py", "ForecastRevision"): frozenset({"TAF", "AMD", "COR"}),
        ("bayes_basket_target/event_contracts.py", "CanonicalObservationEvent"): frozenset({"METAR", "SPECI"}),
    }
    for (relative, class_name), expected_values in contract_allowlists.items():
        tree = by_file.get(relative)
        if tree is None:
            continue
        class_node = next(
            (
                node
                for _, node, short_name, _ in _class_scopes(tree)
                if short_name == class_name
            ),
            None,
        )
        if class_node is None:
            continue
        observed_sets = _string_sets(class_node)
        if expected_values not in observed_sets:
            findings.append(
                _finding(
                    "contract_kind_allowlist_drift",
                    file=relative,
                    contract=class_name,
                    expected=sorted(expected_values),
                    observed=[sorted(item) for item in observed_sets[:8]],
                )
            )
    producer_file = "scripts/capture_bayes_basket_target_awc_taf_revision.py"
    consumer_file = "bayes_basket_target/production_worker.py"
    producer_tree = by_file.get(producer_file)
    consumer_tree = by_file.get(consumer_file)
    producer_module_literals = _string_literals(producer_tree) if producer_tree is not None else set()
    inbox_schema = (
        _string_constant_assignments(producer_tree or ast.Module(body=[], type_ignores=[]))
        .get("INBOX_SCHEMA")
        or ""
    )
    producer_routes = {
        short_name: node
        for _, node, short_name, _ in _function_scopes(producer_tree)
    } if producer_tree is not None else {}
    consumer_route = next(
        (
            node
            for _, node, short_name, _ in _function_scopes(consumer_tree)
            if short_name == "_materialize_resident_p0m"
        ),
        None,
    ) if consumer_tree is not None else None
    producer_revision_literals = _string_literals(
        producer_routes.get("refresh_forward_taf_inbox", ast.Module(body=[], type_ignores=[]))
    )
    producer_schema_symbol = _name_occurs(
        producer_routes.get("refresh_forward_taf_inbox", ast.Module(body=[], type_ignores=[])),
        "INBOX_SCHEMA",
    )
    producer_checks = {
        "inbox_schema_declared": inbox_schema == "bayes_basket_target_taf_revision_inbox_v1",
        "forecast_revision_schema_declared": "bayes_basket_forecast_revision_v1" in producer_module_literals,
        "inbox_refresh_route_present": "refresh_forward_taf_inbox" in producer_routes,
        "inbox_refresh_uses_schema_constant": producer_schema_symbol,
        "inbox_refresh_writes_bounded_json": (
            "__writer__" in _function_semantic_tokens(producer_routes["refresh_forward_taf_inbox"])[1]
            if "refresh_forward_taf_inbox" in producer_routes else False
        ),
        "inbox_revision_kind_allowlist": {"TAF", "AMD", "COR"}.issubset(
            {value.upper() for value in producer_revision_literals}
        ),
        "capture_route_present": "capture_forward_awc_taf" in producer_routes,
        "capture_emits_taf_kind": "TAF" in {
            value.upper()
            for value in _string_literals(
                producer_routes.get("capture_forward_awc_taf", ast.Module(body=[], type_ignores=[]))
            )
        },
    }
    for name, passed in producer_checks.items():
        if not passed:
            findings.append(_finding("taf_inbox_producer_route_check_failed", check=name))
    consumer_literals = _string_literals(consumer_route) if consumer_route is not None else set()
    consumer_check = {
        "consumer_p0m_route_present": consumer_route is not None,
        "consumer_reads_same_inbox_schema": bool(inbox_schema)
        and inbox_schema in consumer_literals,
        "consumer_validates_forecast_revision": consumer_route is not None
        and _name_occurs(consumer_route, "ForecastRevision"),
    }
    for name, passed in consumer_check.items():
        if not passed:
            findings.append(_finding("taf_inbox_consumer_route_check_failed", check=name))
    return {
        "routes": route_records,
        "route_count": len(route_records),
        "producer_inbox": {
            "file": producer_file,
            "schema": inbox_schema,
            "checks": producer_checks,
        },
        "consumer_inbox": {
            "file": consumer_file,
            "schema": inbox_schema,
            "checks": consumer_check,
        },
    }, _bounded_findings(findings)


def _minimal_receipt(
    *,
    source_root: Path,
    blockers: Sequence[Mapping[str, Any]],
    counts: Mapping[str, Any],
) -> dict[str, Any]:
    identity = {
        "schema": RECEIPT_SCHEMA,
        "status": "BLOCKED",
        "ok": False,
        "source_root": str(source_root),
        "blockers": _bounded_findings(blockers),
        "counts": dict(counts),
        "boundary": {
            "read_only": True,
            "writes_database": False,
            "writes_source": False,
            "network_access": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "receipt_sha256": _sha256_bytes(_canonical_json(identity))}


def _with_hash(identity: Mapping[str, Any]) -> dict[str, Any]:
    core = dict(identity)
    return {**core, "receipt_sha256": _sha256_bytes(_canonical_json(core))}


def _fit_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    payload = _canonical_json(receipt)
    if len(payload) <= MAX_RECEIPT_BYTES:
        return receipt
    compact = _minimal_receipt(
        source_root=Path(str(receipt.get("source_root") or "")),
        blockers=[_finding("receipt_size_limit_exceeded", limit=MAX_RECEIPT_BYTES)],
        counts={"receipt_bytes_before_compaction": len(payload)},
    )
    if len(_canonical_json(compact)) > MAX_RECEIPT_BYTES:
        # This literal is bounded and should never be reached, but keep the
        # hard guarantee even if a future constant changes.
        compact = _with_hash({
            "schema": RECEIPT_SCHEMA,
            "status": "BLOCKED",
            "ok": False,
            "blockers": [{"code": "receipt_size_limit_exceeded"}],
        })
    return compact


def audit_taf_role_isolation(
    source_root: Path | str = DEFAULT_SOURCE_ROOT,
    *,
    project_root: Path | str | None = None,
    output_path: Path | str | None = None,
    max_files: int = MAX_FILES,
    max_source_bytes: int = MAX_SOURCE_BYTES_PER_FILE,
    max_total_bytes: int = MAX_TOTAL_SOURCE_BYTES,
    max_ast_nodes: int = MAX_AST_NODES_PER_FILE,
) -> dict[str, Any]:
    """Build a deterministic receipt without mutating source or databases."""

    if project_root is not None:
        source_root = Path(project_root).expanduser().resolve()
    root = Path(source_root).expanduser().resolve()
    limits = {
        "max_files": max_files,
        "max_source_bytes_per_file": max_source_bytes,
        "max_total_source_bytes": max_total_bytes,
        "max_ast_nodes_per_file": max_ast_nodes,
        "max_findings": MAX_FINDINGS,
        "max_receipt_bytes": MAX_RECEIPT_BYTES,
    }
    blockers: list[dict[str, Any]] = []
    if not (1 <= int(max_files) <= MAX_FILES):
        blockers.append(_finding("scan_file_limit_invalid", value=max_files, maximum=MAX_FILES))
        max_files = MAX_FILES
    if not (1 <= int(max_source_bytes) <= MAX_SOURCE_BYTES_PER_FILE):
        blockers.append(_finding("source_file_limit_invalid", value=max_source_bytes, maximum=MAX_SOURCE_BYTES_PER_FILE))
        max_source_bytes = MAX_SOURCE_BYTES_PER_FILE
    if not (1 <= int(max_total_bytes) <= MAX_TOTAL_SOURCE_BYTES):
        blockers.append(_finding("source_total_limit_invalid", value=max_total_bytes, maximum=MAX_TOTAL_SOURCE_BYTES))
        max_total_bytes = MAX_TOTAL_SOURCE_BYTES
    if not (1 <= int(max_ast_nodes) <= MAX_AST_NODES_PER_FILE):
        blockers.append(_finding("ast_node_limit_invalid", value=max_ast_nodes, maximum=MAX_AST_NODES_PER_FILE))
        max_ast_nodes = MAX_AST_NODES_PER_FILE

    files, walk_findings = _bounded_source_files(root, max_files=max_files)
    blockers.extend(walk_findings)
    by_relative = {str(path.relative_to(root)): path for path in files if path.is_file()}
    for required in REQUIRED_SOURCE_FILES:
        path = root / required
        if not path.is_file():
            blockers.append(_finding("source_file_missing", file=required))
        elif required not in by_relative:
            # This can only occur after a file-cap stop; make the reason
            # explicit instead of silently treating an unscanned route as OK.
            blockers.append(_finding("required_route_not_scanned", file=required))

    parsed: list[dict[str, Any]] = []
    total_bytes = 0
    source_inventory: list[dict[str, Any]] = []
    for path in files[:max_files]:
        item, read_findings, total_bytes = _read_and_parse(
            path,
            root=root,
            total_bytes=total_bytes,
            max_source_bytes=max_source_bytes,
            max_total_bytes=max_total_bytes,
            max_ast_nodes=max_ast_nodes,
        )
        blockers.extend(read_findings)
        if item is None:
            continue
        parsed.append(item)
        source_inventory.append({
            "path": item["path"],
            "bytes": item["bytes"],
            "sha256": item["sha256"],
            "ast_nodes": item["ast_nodes"],
        })

    probe, probe_blockers = _probe_contracts()
    blockers.extend(probe_blockers)
    route, route_blockers = _audit_required_routes(parsed)
    blockers.extend(route_blockers)
    functions, function_blockers = _audit_route_functions(parsed)
    blockers.extend(function_blockers)
    blockers = _bounded_findings(blockers)
    checks = {
        "source_root_present": root.is_dir(),
        "required_source_files_present": all(
            (root / item).is_file() for item in REQUIRED_SOURCE_FILES
        ),
        "source_scan_within_bounds": not any(
            str(item.get("code") or "").endswith("limit_exceeded")
            or str(item.get("code") or "") in {
                "scan_file_limit_exceeded",
                "source_file_size_limit_exceeded",
                "source_total_size_limit_exceeded",
                "ast_node_limit_exceeded",
            }
            for item in blockers
        ),
        "contract_probes_pass": all(probe["checks"].values()),
        "required_routes_pass": not route_blockers,
        "no_cross_domain_writer": not function_blockers,
        "no_unknown_kind_acceptance": probe["checks"]["unknown_kind_rejected"],
    }
    identity = {
        "schema": RECEIPT_SCHEMA,
        "status": "PASS" if all(checks.values()) and not blockers else "BLOCKED",
        "ok": bool(all(checks.values()) and not blockers),
        "source_root": str(root),
        "limits": limits,
        "source_inventory": sorted(source_inventory, key=lambda item: str(item["path"])),
        "counts": {
            "files_discovered": len(files),
            "files_parsed": len(parsed),
            "source_bytes_read": total_bytes,
            "functions_scanned": functions["function_count"],
            "writer_functions_scanned": functions["writer_function_count"],
            "blocker_count": len(blockers),
        },
        "checks": checks,
        "contract_probes": probe,
        "required_routes": route,
        "function_route_audit": functions,
        "blockers": blockers,
        "boundary": {
            "read_only": True,
            "writes_database": False,
            "writes_source": False,
            "network_access": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    receipt = _fit_receipt(_with_hash(identity))
    if output_path is not None:
        write_receipt(Path(output_path).expanduser().resolve(), receipt)
    return receipt


def build_audit(**kwargs: Any) -> dict[str, Any]:
    """Compatibility entry point used by other bounded audit scripts."""

    return audit_taf_role_isolation(**kwargs)


def audit(**kwargs: Any) -> dict[str, Any]:
    """Short compatibility entry point for audit orchestration."""

    return audit_taf_role_isolation(**kwargs)


def build_receipt(**kwargs: Any) -> dict[str, Any]:
    """Explicit receipt-named entry point for CI callers."""

    return audit_taf_role_isolation(**kwargs)


def write_receipt(path: Path | str, receipt: Mapping[str, Any]) -> None:
    """Atomically write a bounded JSON receipt; never writes source/database."""

    destination = Path(path).expanduser().resolve()
    payload = _canonical_json(_fit_receipt(dict(receipt)))
    if len(payload) > MAX_RECEIPT_BYTES:
        raise ValueError("taf_role_isolation_receipt_size_limit_exceeded")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.tmp-",
            dir=str(destination.parent),
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", "--root", dest="source_root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--max-files", type=int, default=MAX_FILES)
    parser.add_argument("--max-source-bytes", type=int, default=MAX_SOURCE_BYTES_PER_FILE)
    parser.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL_SOURCE_BYTES)
    parser.add_argument("--max-ast-nodes", type=int, default=MAX_AST_NODES_PER_FILE)
    args = parser.parse_args(argv)
    receipt = audit_taf_role_isolation(
        args.source_root,
        output_path=args.output,
        max_files=args.max_files,
        max_source_bytes=args.max_source_bytes,
        max_total_bytes=args.max_total_bytes,
        max_ast_nodes=args.max_ast_nodes,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_OUTPUT",
    "DEFAULT_SOURCE_ROOT",
    "MAX_AST_NODES_PER_FILE",
    "MAX_FILES",
    "MAX_RECEIPT_BYTES",
    "MAX_SOURCE_BYTES_PER_FILE",
    "MAX_TOTAL_SOURCE_BYTES",
    "RECEIPT_SCHEMA",
    "REQUIRED_SOURCE_FILES",
    "audit",
    "audit_taf_role_isolation",
    "build_audit",
    "build_receipt",
    "write_receipt",
]
