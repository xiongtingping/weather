#!/usr/bin/env python3
"""Read-only closure gate for the Bayes Basket Target production entry.

This gate answers a narrower question than the execution-constitution gate:
can the target lane be reached through exactly one resident production
consumer, with exactly three same-domain services, and without a legacy
strategy or data-source fallback?

The audit is intentionally static and bounded.  It reads source, JSON and
launchd manifests only; it never imports target runtime code, opens a
database, connects to a network, calls a gateway, or changes service state.
When ``probe_services`` is enabled, ``launchctl print`` is used only as a
read-only observation.  An unavailable or incomplete probe is fail closed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import plistlib
import re
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "bayes_basket_target_production_closure_audit_v1"
CLOSURE_VERSION = "target-production-entry-closure-v1"

TARGET_SERVICE_LABELS = (
    "com.weather.reported_trajectory_no",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
)
TARGET_SERVICE_DOMAINS = {label: "user" for label in TARGET_SERVICE_LABELS}

# The legacy trajectory engine is deliberately scanned as an authority input,
# but is not part of the target source-closure manifest.  It owns the
# reported-observation hard-NO writer and must remain bound to the resident
# archive database.  Keeping this as a separate scope prevents the target
# isolation manifest from accidentally making the legacy writer a target
# consumer.
HARD_NO_AUTHORITY_SOURCE = "reported_trajectory_no_engine.py"
AUTHORITY_SOURCE_SCOPE: tuple[str, ...] = (HARD_NO_AUTHORITY_SOURCE,)
ARCHIVE_DB_PATH = "data/archive.db"
TARGET_DB_PATH = "data/bayes_basket_target_ssot.db"

# These are the only files which form the target production closure.  The
# broad repository is deliberately not scanned: docs, tests, historical
# output and old source-account lanes must not create semantic false positives.
DEFAULT_SOURCE_SCOPE: tuple[str, ...] = (
    "scripts/reported_trajectory_no_daemon.py",
    "scripts/reported_trajectory_no_daemon.sh",
    "scripts/market_ws_listener.py",
    "scripts/market_ws_listener.sh",
    "scripts/clob_order_service.mjs",
    "scripts/clob_order_service.sh",
    "bayes_basket_target/production_worker.py",
    "bayes_basket_target/market_ingress.py",
    "bayes_basket_target/delta_v2_runtime.py",
    "bayes_basket_target/delta_v2_training.py",
    "bayes_basket_target/delta_v2_settlement_label.py",
    "bayes_basket_target/settlement_truth.py",
    "bayes_basket_target/forward_training_validation.py",
    "bayes_basket_target/resident_nwp_producer.py",
    "bayes_basket_target/production.py",
    "bayes_basket_target/production_io.py",
    "bayes_basket_target/runtime.py",
    "bayes_basket_target/cash_phase_forward_observation.py",
    "bayes_basket_target/forward_observation_store.py",
    "bayes_basket_target/order_state_producer.py",
    "bayes_basket_target/atomic_ledger.py",
    "bayes_basket_target/execution_reconciliation.py",
    "bayes_basket_target/execution_transition_runtime.py",
    "bayes_basket_target/basket_kernel/__init__.py",
    "bayes_basket_target/basket_kernel/contracts.py",
    "bayes_basket_target/basket_kernel/execution_gates.py",
    "bayes_basket_target/basket_kernel/maker_simulator.py",
    "bayes_basket_target/basket_kernel/support.py",
    "bayes_basket_target/basket_kernel/delta_v2.py",
    "config/trading/bayes_basket_target_production_no_live_activation_v1.json",
)

RUNTIME_ISOLATION_MANIFEST = "config/bayes_basket_target/runtime_config_isolation_v1.json"
TARGET_ACTIVATION = "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
PROTECTED_LEGACY_CONFIG = "config/trading/basket_plus_hard_no_v1.json"
TARGET_RUNTIME_OWNER = "bayes_basket_target"
PROTECTED_LEGACY_OWNER = "protected_l0_legacy"
TARGET_RUNTIME_CONSUMER = "target_runtime"
PROTECTED_LEGACY_CONSUMER = "legacy_l0_runtime"
RUNTIME_ISOLATION_SCHEMA = "bayes_basket_target_runtime_config_isolation_v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_production_closure_audit.json"

MAX_FILES = 96
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 12_000_000
MAX_CANDIDATES = 128
MAX_GRAPH_EDGES = 256
MAX_HITS = 256
MAX_RECEIPT_BYTES = 768_000

EXCLUDED_DIRS = frozenset({
    ".git",
    "__pycache__",
    "docs",
    "tests",
    "data",
    "output",
    "tmp",
    "_moved_from_old_root",
})

# These names are operationally old or explicitly forbidden by the target
# constitution.  Matching is done only in the bounded runtime scope and in
# launchd manifests, never in historical prose or generated receipts.
LEGACY_RUNTIME_MARKERS = (
    "target_realization",
    "dynamic_three_leg",
    "adaptive_basket_shadow",
    "posterior_market_projection",
    "production_probability_v6",
    "physical_state_bayes_v6",
    "sequential_bayes",
    "ui_server",
    "model_scan",
    "market_refresh",
    "task_queue_worker",
    "auto_trade_worker",
    "auto_trade_watcher",
    "trade_engine",
    "trade_sync_loop",
    "q_calibrated_writeback",
    "polymarket_monitor",
)
LEGACY_SERVICE_MARKERS = LEGACY_RUNTIME_MARKERS + (
    "feedback_loop",
    "daily_scan",
    "forecast_source_weight_research",
)
TARGETISH_MARKERS = (
    "target",
    "basket",
    "reported_trajectory_no",
    "market_ws",
    "clob_order_service",
)
SAFE_CANDIDATE_SUFFIXES = frozenset({".py", ".sh", ".mjs", ".plist"})

LIVE_FLAG_NAMES = (
    "live_submit_authorized",
    "live_order_authorized",
    "live_decision_write_authorized",
    "authorizes_order",
    "places_orders",
    "calls_order_gateway",
    "writes_production_db",
    "production_mutation",
)

_LIVE_TRUE_RE = re.compile(
    r"(?i)(?:\"|')?(?:"
    + "|".join(re.escape(item) for item in LIVE_FLAG_NAMES)
    + r")(?:\"|')?\s*(?:[:=]|\s+in\s+)\s*(?:true|1|yes)\b"
)
_SYSTEM_DOMAIN_RE = re.compile(
    r"(?i)(?:WEATHER_SERVICE_DOMAIN|service_domain|launchctl[^\n]{0,80})[^\n]{0,120}"
    r"(?:system/|LaunchDaemon|[\"']system[\"'])"
)
_GATEWAY_CALL_RE = re.compile(
    r"(?i)\b(?:place[_ -]?order|submit[_ -]?order|"
    r"sign(?:ed)?[_ -]?order|exchange[_ -]?post|send[_ -]?order)\s*\("
)
_CLOB_REST_MARKER_RE = re.compile(
    r"(?i)(?:market[_ -]?refresh|refresh[_ -]?market|discover[_ -]?weather[_ -]?markets|"
    r"fetch[_ -]?(?:book|order[_ -]?book|market[_ -]?ticks)|event_market_ticks|"
    r"market_ticks|latest_market_tick)"
)
_GAMMA_REGISTRY_MARKER_RE = re.compile(
    r"(?i)(?:fetch[_ -]?gamma[_ -]?(?:event|events|page|window)|"
    r"gamma[_ -]?(?:event|registry)|market[_ -]?registry|registry[_ -]?(?:row|metadata|refresh))"
)
_HARD_NO_ENGINE_CALL_RE = re.compile(
    r"(?is)reportedtrajectorynoengine\s*\(\s*([^,)]*)"
)
_TARGET_WRITER_CALL_RE = re.compile(
    r"(?is)(?:targetproductionworker|open_target_writer_if_enabled)\s*\(\s*([^,)]*)"
)


def _inside(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_sha(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _excerpt(source: str, line: int, limit: int = 240) -> str:
    lines = source.splitlines()
    value = lines[line - 1].strip() if 0 < line <= len(lines) else ""
    raw = value.encode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore")


def _docstring_positions(tree: ast.AST) -> set[tuple[int, int]]:
    result: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            result.add((int(first.lineno), int(first.col_offset)))
    return result


def _python_semantics(source: str) -> tuple[str, ast.AST | None, list[dict[str, Any]]]:
    """Return executable-ish Python text, AST and bounded parse hits."""

    hits: list[dict[str, Any]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        hits.append({"code": "python_syntax_error", "line": int(exc.lineno or 0), "error": str(exc)})
        return "", None, hits
    doc_positions = _docstring_positions(tree)
    values: list[str] = []
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
            if token.type == tokenize.STRING:
                try:
                    decoded = ast.literal_eval(token.string)
                except (SyntaxError, ValueError):
                    decoded = token.string
                values.append(decoded if isinstance(decoded, str) else token.string)
            else:
                values.append(token.string)
    except (IndentationError, tokenize.TokenError) as exc:
        hits.append({"code": "python_tokenize_error", "line": 0, "error": str(exc)})
    return " ".join(values), tree, hits


def _generic_semantics(source: str) -> str:
    """Remove full-line comments and block comments from shell/JS text."""

    lines: list[str] = []
    in_block = False
    for line in source.splitlines():
        stripped = line.strip()
        if in_block:
            if "*/" in line:
                in_block = False
            continue
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        if stripped.startswith("/*"):
            in_block = "*/" not in stripped
            continue
        lines.append(line)
    return "\n".join(lines)


def _read_bounded(path: Path, *, root: Path, max_file_bytes: int) -> tuple[bytes | None, dict[str, Any] | None]:
    resolved = path.expanduser().resolve()
    relative = str(resolved.relative_to(root)) if _inside(root, resolved) else str(path)
    if not _inside(root, resolved):
        return None, {"code": "scope_path_outside_project", "file": relative}
    if any(part in EXCLUDED_DIRS for part in Path(relative).parts[:-1]):
        return None, {"code": "scope_path_excluded", "file": relative}
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        return None, {"code": "scope_file_stat_failed", "file": relative, "error": str(exc)}
    if size > max_file_bytes:
        return None, {
            "code": "resource_file_bytes_exceeded",
            "file": relative,
            "bytes": int(size),
            "maximum": int(max_file_bytes),
        }
    try:
        return resolved.read_bytes(), None
    except OSError as exc:
        return None, {"code": "scope_file_read_failed", "file": relative, "error": str(exc)}


def _append_hit(hits: list[dict[str, Any]], *, code: str, evidence: Any, file: str | None = None, line: int | None = None) -> None:
    if len(hits) >= MAX_HITS:
        return
    item: dict[str, Any] = {"code": code, "evidence": evidence}
    if file is not None:
        item["file"] = file
    if line is not None:
        item["line"] = int(line)
    key = _canonical_json(item)
    if not any(_canonical_json(existing) == key for existing in hits):
        hits.append(item)


def _relative(root: Path, path: Path) -> str:
    resolved = path.resolve()
    return str(resolved.relative_to(root)) if _inside(root, resolved) else str(path)


def _parse_source_scope(
    *,
    root: Path,
    source_files: Sequence[str],
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[dict[str, str], dict[str, Any], list[dict[str, Any]], int]:
    scope_hits: list[dict[str, Any]] = []
    semantic: dict[str, str] = {}
    parsed: dict[str, Any] = {}
    total = 0
    requested = tuple(source_files)
    if len(requested) > max_files:
        scope_hits.append({"code": "resource_file_count_exceeded", "requested": len(requested), "maximum": max_files})
        requested = requested[:max_files]
    for raw in requested:
        path = Path(raw) if Path(raw).is_absolute() else root / raw
        data, error = _read_bounded(path, root=root, max_file_bytes=max_file_bytes)
        if error:
            # Explicit docs/tests/output paths are ignored by design; an
            # absent required runtime file remains a hard scope failure.
            if error["code"] != "scope_path_excluded":
                scope_hits.append(error)
            continue
        assert data is not None
        relative = _relative(root, path)
        if total + len(data) > max_total_bytes:
            scope_hits.append({
                "code": "resource_total_bytes_exceeded",
                "file": relative,
                "bytes": total + len(data),
                "maximum": max_total_bytes,
            })
            continue
        total += len(data)
        try:
            source = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            scope_hits.append({"code": "scope_utf8_decode_failed", "file": relative, "error": str(exc)})
            continue
        suffix = path.suffix.lower()
        if suffix == ".py":
            active, tree, parse_hits = _python_semantics(source)
            parsed[relative] = tree
            for hit in parse_hits:
                hit["file"] = relative
                scope_hits.append(hit)
        elif suffix == ".json":
            try:
                value = json.loads(source)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                scope_hits.append({"code": "json_parse_failed", "file": relative, "error": str(exc)})
                value = {}
            parsed[relative] = value
            active = _canonical_json(value)
        else:
            active = _generic_semantics(source)
            parsed[relative] = None
        semantic[relative] = active
    return semantic, parsed, scope_hits, total


def _python_import_hits(semantic: Mapping[str, str], parsed: Mapping[str, Any]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for relative, tree in parsed.items():
        if not isinstance(tree, ast.AST):
            continue
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.Import):
                module = ",".join(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = str(node.module or "")
            lowered = module.lower()
            matched = next((marker for marker in LEGACY_RUNTIME_MARKERS if marker in lowered), None)
            if matched:
                hits.append({"code": "legacy_runtime_import", "file": relative, "line": int(node.lineno), "marker": matched, "module": module})
    return hits


def _semantic_marker_hits(semantic: Mapping[str, str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for relative, active in semantic.items():
        lower = active.lower()
        for marker in LEGACY_RUNTIME_MARKERS:
            if marker in lower:
                _append_hit(hits, code="legacy_runtime_dependency", file=relative, evidence=marker)
        # Gamma is an official registry metadata path.  It is deliberately
        # not treated as a CLOB order-book REST fallback.  Only explicit book,
        # tick, market-refresh, or market-discovery markers are execution
        # boundary violations.
        if _CLOB_REST_MARKER_RE.search(active) and not (
            _GAMMA_REGISTRY_MARKER_RE.search(active)
            and not any(marker in lower for marker in ("order_book", "market_ticks", "event_market_ticks"))
        ):
            _append_hit(
                hits,
                code="forbidden_market_rest_or_tick_dependency",
                file=relative,
                evidence="clob-book-or-tick-rest-marker",
            )
        if _LIVE_TRUE_RE.search(active):
            _append_hit(hits, code="live_or_mutating_flag_true", file=relative, evidence="live/mutation flag enabled")
        if _SYSTEM_DOMAIN_RE.search(active):
            _append_hit(
                hits,
                code="system_domain_runtime_fallback",
                file=relative,
                evidence="system-domain branch",
            )
    return hits


def _retired_gateway_contract(
    semantic: Mapping[str, str],
) -> tuple[dict[str, bool], list[dict[str, Any]], dict[str, Any]]:
    relative = "scripts/clob_order_service.mjs"
    active = semantic.get(relative, "")
    denial_tokens = (
        "RETIRED_STRATEGY_IDS",
        "weather_copy_follow_v1",
        "retired_strategy_forbidden",
        "exchange_post_started: false",
    )
    denial_present = all(token in active for token in denial_tokens)
    retired_parallel_lane_absent = all(
        token not in active
        for token in (
            "copy_follow_parallel_submission",
            "copy_follow_parallel",
        )
    )
    checks = {
        "copy_follow_gateway_denied_before_submit": denial_present,
        "copy_follow_parallel_gateway_lane_absent": (
            retired_parallel_lane_absent
        ),
    }
    hits: list[dict[str, Any]] = []
    for name, passed in checks.items():
        if not passed:
            _append_hit(
                hits,
                code=f"retired_gateway_check_failed:{name}",
                file=relative,
                evidence=name,
            )
    return checks, hits, {
        "gateway_source": relative,
        "retired_strategy_id": "weather_copy_follow_v1",
        "denial_error": "retired_strategy_forbidden",
        "denial_tokens_present": denial_present,
        "retired_parallel_lane_absent": retired_parallel_lane_absent,
    }


def _scan_gateway_owner(semantic: Mapping[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    hits: list[dict[str, Any]] = []
    owners: list[str] = []
    for relative, active in semantic.items():
        is_gateway_owner = relative.endswith("scripts/clob_order_service.mjs") and all(
            marker in active for marker in ("/place-order", "exchange_post_started")
        )
        if is_gateway_owner:
            owners.append(relative)
        elif _GATEWAY_CALL_RE.search(active):
            _append_hit(hits, code="non_gateway_runtime_contains_gateway_action", file=relative, evidence="gateway call syntax")
    return hits, sorted(set(owners))


def _plist_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _plist_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _plist_values(item)


def _parse_launchd(
    *,
    root: Path,
    launchd_dir: Path,
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    manifests: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []
    total = 0
    resolved_dir = launchd_dir if launchd_dir.is_absolute() else root / launchd_dir
    try:
        files = sorted(resolved_dir.glob("*.plist"), key=lambda p: str(p))
    except OSError as exc:
        return [], [{"code": "launchd_manifest_scan_failed", "error": str(exc)}], total
    if len(files) > MAX_FILES:
        hits.append({"code": "launchd_manifest_count_exceeded", "count": len(files), "maximum": MAX_FILES})
        files = files[:MAX_FILES]
    for path in files:
        data, error = _read_bounded(path, root=root, max_file_bytes=max_file_bytes)
        if error:
            hits.append(error)
            continue
        assert data is not None
        relative = _relative(root, path)
        if total + len(data) > max_total_bytes:
            hits.append({"code": "resource_total_bytes_exceeded", "file": relative, "maximum": max_total_bytes})
            continue
        total += len(data)
        try:
            value = plistlib.loads(data)
        except (plistlib.InvalidFileException, ValueError, TypeError) as exc:
            hits.append({"code": "launchd_plist_parse_failed", "file": relative, "error": str(exc)})
            continue
        if not isinstance(value, Mapping):
            hits.append({"code": "launchd_plist_root_invalid", "file": relative})
            continue
        label = str(value.get("Label") or "")
        args = tuple(str(item) for item in value.get("ProgramArguments", ()) if isinstance(item, (str, int, float)))
        env_raw = value.get("EnvironmentVariables", {})
        env = {str(key): str(item) for key, item in env_raw.items()} if isinstance(env_raw, Mapping) else {}
        manifests.append({"file": relative, "label": label, "program_arguments": list(args), "environment": env})
    return manifests, hits, total


def _launchd_contract(manifests: Sequence[Mapping[str, Any]], service_snapshot: Mapping[str, Sequence[str]] | None) -> tuple[dict[str, bool], list[dict[str, Any]], dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    labels = [str(item.get("label") or "") for item in manifests]
    duplicate_labels = sorted({label for label in labels if label and labels.count(label) > 1})
    if duplicate_labels:
        _append_hit(hits, code="duplicate_launchd_service_label", evidence=duplicate_labels)
    by_label = {str(item.get("label") or ""): item for item in manifests if item.get("label")}
    for label in TARGET_SERVICE_LABELS:
        item = by_label.get(label)
        if item is None:
            _append_hit(hits, code="target_service_manifest_missing", evidence=label)
            continue
        args_blob = " ".join(str(x) for x in item.get("program_arguments", ()))
        if label == "com.weather.reported_trajectory_no" and "reported_trajectory_no_daemon.sh" not in args_blob:
            _append_hit(hits, code="target_service_program_mismatch", evidence={"label": label, "args": args_blob})
        if label == "com.weather.market_ws" and "market_ws_listener.sh" not in args_blob:
            _append_hit(hits, code="target_service_program_mismatch", evidence={"label": label, "args": args_blob})
        if label == "com.weather.clob_order_service" and "clob_order_service.sh" not in args_blob:
            _append_hit(hits, code="target_service_program_mismatch", evidence={"label": label, "args": args_blob})
        if str(item.get("environment", {}).get("WEATHER_SERVICE_DOMAIN") or "") != "user":
            _append_hit(
                hits,
                code="target_service_user_domain_manifest_missing",
                evidence=label,
            )

    forbidden_declarations: list[dict[str, Any]] = []
    for item in manifests:
        label = str(item.get("label") or "")
        args_blob = " ".join(str(x) for x in item.get("program_arguments", ()))
        haystack = f"{label} {args_blob}".lower()
        marker = next((candidate for candidate in LEGACY_SERVICE_MARKERS if candidate in haystack), None)
        if marker:
            forbidden_declarations.append({"label": label, "file": item.get("file"), "marker": marker})
            _append_hit(hits, code="forbidden_service_declaration_residue", evidence=forbidden_declarations[-1])
        elif any(marker in haystack for marker in TARGETISH_MARKERS) and label not in TARGET_SERVICE_LABELS:
            _append_hit(hits, code="unexpected_target_service_manifest", evidence={"label": label, "file": item.get("file")})

    snapshot_ok = service_snapshot is not None
    loaded_system: set[str] = set()
    loaded_user: set[str] = set()
    forbidden_loaded_services: list[str] = []
    if service_snapshot is not None:
        loaded_system = {str(item) for item in service_snapshot.get("system", ())}
        loaded_user = {str(item) for item in service_snapshot.get("user", ())}
        for label in sorted(loaded_system & set(TARGET_SERVICE_LABELS)):
            _append_hit(
                hits,
                code="target_service_system_domain_residue",
                evidence=label,
            )
        for label in sorted(set(TARGET_SERVICE_LABELS) - loaded_user):
            _append_hit(
                hits,
                code="target_service_not_loaded_in_user_domain",
                evidence=label,
            )
        forbidden_loaded_services = sorted(
            label for label in (loaded_system | loaded_user)
            if any(marker in label.lower() for marker in LEGACY_SERVICE_MARKERS)
        )
        for label in forbidden_loaded_services:
            _append_hit(hits, code="forbidden_loaded_service", evidence=label)
    checks = {
        "target_service_labels_unique": not duplicate_labels,
        "target_service_manifests_present": all(label in by_label for label in TARGET_SERVICE_LABELS),
        "forbidden_service_declarations_absent": not forbidden_declarations,
        "forbidden_loaded_services_absent": snapshot_ok and not forbidden_loaded_services,
        "service_probe_or_snapshot_complete": snapshot_ok,
        "target_services_user_only": (
            snapshot_ok
            and loaded_user == set(TARGET_SERVICE_LABELS)
            and not (loaded_system & set(TARGET_SERVICE_LABELS))
        ),
    }
    evidence = {
        "declared_label_count": len([label for label in labels if label]),
        "target_labels": list(TARGET_SERVICE_LABELS),
        "loaded_system": sorted(loaded_system),
        "loaded_user": sorted(loaded_user),
        "declared_service_count": len(manifests),
        "forbidden_declaration_count": len(forbidden_declarations),
        "forbidden_declaration_sample": forbidden_declarations[:16],
        "forbidden_loaded_service_count": len(forbidden_loaded_services),
        "forbidden_loaded_service_sample": forbidden_loaded_services[:16],
    }
    return checks, hits, evidence


def _probe_services(labels: Sequence[str]) -> tuple[dict[str, list[str]] | None, list[dict[str, Any]]]:
    """Read-only launchctl probe.  No bootstrap, bootout, kickstart or sudo."""

    uid = str(os.getuid())
    snapshot = {"system": [], "user": []}
    errors: list[dict[str, Any]] = []
    for label in labels:
        for domain, prefix in (("system", "system"), ("user", f"gui/{uid}")):
            target = f"{prefix}/{label}"
            try:
                completed = subprocess.run(
                    ["/bin/launchctl", "print", target],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=3,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append({"code": "service_probe_failed", "target": target, "error": str(exc)})
                continue
            if completed.returncode == 0:
                snapshot[domain].append(label)
    return snapshot, errors


def _config_contract(parsed: Mapping[str, Any], semantic: Mapping[str, str]) -> tuple[dict[str, bool], list[dict[str, Any]], dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    configs = {name: value for name, value in parsed.items() if name.endswith(".json") and isinstance(value, Mapping)}
    flags: dict[str, bool] = {}
    target_only_values: list[Any] = []
    for name, value in configs.items():
        for key in LIVE_FLAG_NAMES:
            if key in value:
                flags[f"{name}:{key}"] = value.get(key) is False
                if value.get(key) is True:
                    _append_hit(hits, code="config_live_or_mutating_flag_true", file=name, evidence=key)
        if "target_only" in value:
            target_only_values.append(value.get("target_only"))
        basket = value.get("basket")
        if isinstance(basket, Mapping):
            for key in LIVE_FLAG_NAMES:
                if key in basket:
                    flags[f"{name}:basket.{key}"] = basket.get(key) is False
                    if basket.get(key) is True:
                        _append_hit(hits, code="config_basket_live_or_mutating_flag_true", file=name, evidence=key)
            if basket.get("convergence_widths") in ([4, 3, 2], (4, 3, 2)):
                _append_hit(hits, code="config_forbidden_fixed_stage_sequence", file=name, evidence="convergence_widths=4,3,2")
            if basket.get("all_legs_required") is True:
                _append_hit(hits, code="config_forbidden_all_legs_fok", file=name, evidence="all_legs_required=true")
        three_leg = value.get("three_leg_strategy")
        if isinstance(three_leg, Mapping) and three_leg.get("state") not in {None, "disabled"}:
            _append_hit(hits, code="config_three_leg_not_disabled", file=name, evidence=three_leg.get("state"))
        if "strategy_id" in value and "target_realization" in str(value.get("strategy_id")).lower():
            _append_hit(hits, code="config_legacy_strategy_id", file=name, evidence=value.get("strategy_id"))
    checks = {
        "config_target_only": bool(target_only_values) and all(item is True for item in target_only_values),
        "config_live_mutation_flags_false": bool(flags) and all(flags.values()),
        "config_no_fixed_stage_or_all_leg_fok": not any(item["code"].startswith("config_forbidden_") for item in hits),
    }
    return checks, hits, {"config_files": sorted(configs), "flag_checks": flags, "target_only_values": target_only_values}


def _manifest_digest(value: Mapping[str, Any]) -> str:
    """Hash a manifest without its self-address field.

    A self-referential hash cannot be represented directly.  The manifest is
    therefore content-addressed by the canonical JSON payload with
    ``manifest_sha256`` omitted.  File entries below it are independently
    addressed by the raw bytes of each file.
    """

    unsigned = dict(value)
    unsigned.pop("manifest_sha256", None)
    return _content_sha(unsigned)


def _manifest_entry(
    value: Any,
    *,
    role: str,
    hits: list[dict[str, Any]],
) -> tuple[str, str, str, tuple[str, ...]] | None:
    """Validate and normalize one resource declaration.

    ``owners`` is intentionally rejected unless it contains exactly the
    singular ``owner`` value.  This prevents a list of owners from silently
    turning an isolation boundary into shared ownership.
    """

    if not isinstance(value, Mapping):
        _append_hit(hits, code="runtime_isolation_manifest_entry_invalid", evidence={"role": role})
        return None
    path = str(value.get("path") or "").strip()
    sha256 = str(value.get("sha256") or "").strip().lower()
    owner = str(value.get("owner") or "").strip()
    raw_consumers = value.get("consumers")
    if not path or not _SHA256_RE.fullmatch(sha256) or not owner:
        _append_hit(
            hits,
            code="runtime_isolation_manifest_entry_invalid",
            evidence={"role": role, "path": path, "owner": owner, "sha256": sha256},
        )
        return None
    if not isinstance(raw_consumers, list) or not raw_consumers or any(
        not isinstance(item, str) or not item.strip() for item in raw_consumers
    ):
        _append_hit(
            hits,
            code="runtime_isolation_manifest_consumers_invalid",
            evidence={"role": role, "path": path},
        )
        return None
    consumers = tuple(str(item).strip() for item in raw_consumers)
    if len(set(consumers)) != len(consumers):
        _append_hit(
            hits,
            code="runtime_isolation_manifest_duplicate_consumer",
            evidence={"role": role, "path": path, "consumers": list(consumers)},
        )
    if "owners" in value:
        raw_owners = value.get("owners")
        if not isinstance(raw_owners, list) or raw_owners != [owner]:
            _append_hit(
                hits,
                code="runtime_isolation_manifest_multiple_owner",
                evidence={"role": role, "path": path, "owners": raw_owners, "owner": owner},
            )
    return path, sha256, owner, consumers


def _validate_runtime_config_isolation(
    *,
    root: Path,
    manifest_path: str,
    requested_source_files: Sequence[str],
    required_source_names: Sequence[str],
    semantic: Mapping[str, str],
    max_file_bytes: int,
) -> tuple[dict[str, bool], list[dict[str, Any]], dict[str, Any]]:
    """Prove that target sources and the protected L0 config are disjoint.

    This is deliberately independent of the generic config contract.  The
    protected file is not a target configuration input; it is a separately
    addressed legacy resource.  Its contents may contain retired policy
    values, but those values only remain a target blocker when the target
    source scope or reference graph actually crosses into that resource.
    """

    hits: list[dict[str, Any]] = []
    manifest_relative = str(manifest_path)
    manifest_file = Path(manifest_path) if Path(manifest_path).is_absolute() else root / manifest_path
    raw_manifest, manifest_error = _read_bounded(
        manifest_file,
        root=root,
        max_file_bytes=max_file_bytes,
    )
    if manifest_error:
        _append_hit(hits, code="runtime_isolation_manifest_missing", evidence=manifest_error)
        return (
            {
                "manifest_present": False,
                "manifest_schema_valid": False,
                "manifest_content_addressed": False,
                "target_source_scope_content_addressed": False,
                "protected_legacy_config_content_addressed": False,
                "target_source_reference_graph_closed": False,
                "single_owner_per_resource": False,
                "forbidden_cross_read_rules_complete": False,
            },
            hits,
            {
                "manifest_path": manifest_relative,
                "manifest_sha256": None,
                "target_activation": None,
                "target_source_closure": [],
                "protected_legacy_config": None,
                "target_reference_graph": {"nodes": [], "edges": [], "edge_count": 0},
            },
        )
    assert raw_manifest is not None
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _append_hit(hits, code="runtime_isolation_manifest_parse_failed", evidence=str(exc))
        return (
            {
                "manifest_present": True,
                "manifest_schema_valid": False,
                "manifest_content_addressed": False,
                "target_source_scope_content_addressed": False,
                "protected_legacy_config_content_addressed": False,
                "target_source_reference_graph_closed": False,
                "single_owner_per_resource": False,
                "forbidden_cross_read_rules_complete": False,
            },
            hits,
            {"manifest_path": manifest_relative, "manifest_sha256": _sha256_bytes(raw_manifest)},
        )
    if not isinstance(manifest, Mapping):
        _append_hit(hits, code="runtime_isolation_manifest_root_invalid", evidence=type(manifest).__name__)
        return (
            {
                "manifest_present": True,
                "manifest_schema_valid": False,
                "manifest_content_addressed": False,
                "target_source_scope_content_addressed": False,
                "protected_legacy_config_content_addressed": False,
                "target_source_reference_graph_closed": False,
                "single_owner_per_resource": False,
                "forbidden_cross_read_rules_complete": False,
            },
            hits,
            {"manifest_path": manifest_relative, "manifest_sha256": _sha256_bytes(raw_manifest)},
        )

    manifest_schema_ok = (
        manifest.get("schema") == RUNTIME_ISOLATION_SCHEMA
        and manifest.get("manifest_version") == 1
    )
    if not manifest_schema_ok:
        _append_hit(
            hits,
            code="runtime_isolation_manifest_schema_invalid",
            evidence={"schema": manifest.get("schema"), "manifest_version": manifest.get("manifest_version")},
        )

    manifest_self_hash = str(manifest.get("manifest_sha256") or "").lower()
    manifest_hash_ok = bool(_SHA256_RE.fullmatch(manifest_self_hash)) and manifest_self_hash == _manifest_digest(manifest)
    if not manifest_hash_ok:
        _append_hit(
            hits,
            code="runtime_isolation_manifest_hash_mismatch",
            evidence={"declared": manifest_self_hash, "computed": _manifest_digest(manifest)},
        )

    target = manifest.get("target")
    if not isinstance(target, Mapping):
        _append_hit(hits, code="runtime_isolation_manifest_target_section_invalid", evidence=target)
        target = {}
    target_owner = str(target.get("owner") or "")
    if target_owner != TARGET_RUNTIME_OWNER:
        _append_hit(hits, code="runtime_isolation_manifest_target_owner_invalid", evidence=target_owner)
    activation = _manifest_entry(target.get("activation"), role="target.activation", hits=hits)
    raw_closure = target.get("source_closure")
    if not isinstance(raw_closure, list) or not raw_closure:
        _append_hit(hits, code="runtime_isolation_manifest_source_closure_invalid", evidence=raw_closure)
        raw_closure = []
    closure: list[tuple[str, str, str, tuple[str, ...]]] = []
    for item in raw_closure:
        normalized = _manifest_entry(item, role="target.source_closure", hits=hits)
        if normalized is not None:
            closure.append(normalized)
    protected_raw = manifest.get("protected_legacy_config")
    if protected_raw is None:
        protected_raw = manifest.get("protected_legacy")
    if not isinstance(protected_raw, list) or not protected_raw:
        _append_hit(hits, code="runtime_isolation_manifest_protected_resource_missing", evidence=protected_raw)
        protected_raw = []
    protected: list[tuple[str, str, str, tuple[str, ...]]] = []
    for item in protected_raw:
        normalized = _manifest_entry(item, role="protected_legacy_config", hits=hits)
        if normalized is not None:
            protected.append(normalized)

    declarations: dict[str, set[str]] = {}
    declaration_roles: dict[str, list[str]] = {}
    all_entries: list[tuple[str, str, str, tuple[str, ...], str]] = []
    if activation is not None:
        if activation[0] != TARGET_ACTIVATION:
            _append_hit(
                hits,
                code="runtime_isolation_target_activation_invalid",
                evidence={"declared": activation[0], "expected": TARGET_ACTIVATION},
            )
        all_entries.append((*activation, "target.activation"))
    all_entries.extend((*item, "target.source_closure") for item in closure)
    all_entries.extend((*item, "protected_legacy_config") for item in protected)
    for path, _sha, owner, _consumers, role in all_entries:
        declarations.setdefault(path, set()).add(owner)
        declaration_roles.setdefault(path, []).append(role)
    for path, owners in sorted(declarations.items()):
        if len(owners) != 1 or len(declaration_roles[path]) != 1:
            _append_hit(
                hits,
                code="runtime_isolation_manifest_multiple_owner",
                evidence={"path": path, "owners": sorted(owners), "roles": declaration_roles[path]},
            )

    target_entries = ([] if activation is None else [activation]) + closure
    target_paths = [item[0] for item in target_entries]
    requested = {str(Path(item)) for item in requested_source_files}
    requested = {
        str(Path(item).relative_to(root)) if Path(item).is_absolute() and _inside(root, Path(item).resolve()) else item
        for item in requested
    }
    target_scope_ok = bool(activation) and set(target_paths) == requested and len(target_paths) == len(set(target_paths))
    if not target_scope_ok:
        _append_hit(
            hits,
            code="runtime_isolation_target_source_scope_mismatch",
            evidence={"manifest": sorted(set(target_paths)), "requested": sorted(requested)},
        )
    required_set = {str(Path(item)) for item in required_source_names}
    required_set = {
        str(Path(item).relative_to(root)) if Path(item).is_absolute() and _inside(root, Path(item).resolve()) else item
        for item in required_set
    }
    if not required_set.issubset(set(target_paths)):
        _append_hit(
            hits,
            code="runtime_isolation_required_source_outside_manifest",
            evidence=sorted(required_set - set(target_paths)),
        )

    source_hashes: dict[str, str] = {}
    target_hashes_ok = True
    for path, declared_sha, owner, consumers in target_entries:
        if owner != TARGET_RUNTIME_OWNER or consumers != (TARGET_RUNTIME_CONSUMER,):
            _append_hit(
                hits,
                code="runtime_isolation_target_owner_consumer_invalid",
                evidence={"path": path, "owner": owner, "consumers": list(consumers)},
            )
        data, error = _read_bounded(root / path, root=root, max_file_bytes=max_file_bytes)
        if error or data is None:
            target_hashes_ok = False
            _append_hit(hits, code="runtime_isolation_target_resource_missing", evidence=error or {"path": path})
            continue
        actual = _sha256_bytes(data)
        source_hashes[path] = actual
        if actual != declared_sha:
            target_hashes_ok = False
            _append_hit(
                hits,
                code="runtime_isolation_target_resource_hash_mismatch",
                evidence={"path": path, "declared": declared_sha, "actual": actual},
            )

    protected_ok = len(protected) == 1
    protected_entry = protected[0] if len(protected) == 1 else None
    if protected_entry is None or protected_entry[0] != PROTECTED_LEGACY_CONFIG:
        protected_ok = False
        _append_hit(
            hits,
            code="runtime_isolation_protected_resource_declaration_invalid",
            evidence=[item[0] for item in protected],
        )
    if protected_entry is not None:
        path, declared_sha, owner, consumers = protected_entry
        if owner != PROTECTED_LEGACY_OWNER or consumers != (PROTECTED_LEGACY_CONSUMER,):
            protected_ok = False
            _append_hit(
                hits,
                code="runtime_isolation_protected_owner_consumer_invalid",
                evidence={"path": path, "owner": owner, "consumers": list(consumers)},
            )
        data, error = _read_bounded(root / path, root=root, max_file_bytes=max_file_bytes)
        if error or data is None:
            protected_ok = False
            _append_hit(hits, code="runtime_isolation_protected_resource_missing", evidence=error or {"path": path})
        elif _sha256_bytes(data) != declared_sha:
            protected_ok = False
            _append_hit(
                hits,
                code="runtime_isolation_protected_resource_hash_mismatch",
                evidence={"path": path, "declared": declared_sha, "actual": _sha256_bytes(data)},
            )

    forbidden_rules = manifest.get("forbidden_cross_read")
    if not isinstance(forbidden_rules, list):
        forbidden_rules = []
    expected_rule = {
        "consumer_owner": TARGET_RUNTIME_OWNER,
        "resource_path": PROTECTED_LEGACY_CONFIG,
    }
    normalized_rules: set[tuple[str, str]] = set()
    for rule in forbidden_rules:
        if not isinstance(rule, Mapping):
            _append_hit(hits, code="runtime_isolation_forbidden_cross_read_rule_invalid", evidence=rule)
            continue
        consumer = str(rule.get("consumer_owner") or rule.get("consumer") or "")
        resource = str(rule.get("resource_path") or rule.get("resource") or "")
        normalized_rules.add((consumer, resource))
    rules_ok = (expected_rule["consumer_owner"], expected_rule["resource_path"]) in normalized_rules
    if not rules_ok:
        _append_hit(hits, code="runtime_isolation_forbidden_cross_read_rule_missing", evidence=expected_rule)

    target_reference_edges: list[dict[str, str]] = []
    protected_basename = Path(PROTECTED_LEGACY_CONFIG).name.lower()
    protected_markers = (PROTECTED_LEGACY_CONFIG.lower(), protected_basename, Path(PROTECTED_LEGACY_CONFIG).stem.lower())
    for path in target_paths:
        active = semantic.get(path, "")
        lower = active.lower()
        matched = next((marker for marker in protected_markers if marker in lower), None)
        if matched:
            edge = {
                "from": path,
                "to": PROTECTED_LEGACY_CONFIG,
                "kind": "target_source_cross_read",
                "evidence": matched,
            }
            target_reference_edges.append(edge)
            _append_hit(hits, code="target_source_cross_read_protected_l0", file=path, evidence=edge)
    graph_nodes = [
        {"path": path, "sha256": source_hashes.get(path), "owner": TARGET_RUNTIME_OWNER}
        for path in sorted(target_paths)
    ]
    reference_graph = {
        "nodes": graph_nodes,
        "protected_resource": PROTECTED_LEGACY_CONFIG,
        "edges": target_reference_edges,
        "edge_count": len(target_reference_edges),
    }
    graph_closed = not target_reference_edges
    owner_closed = not any(item["code"] == "runtime_isolation_manifest_multiple_owner" for item in hits)
    checks = {
        "manifest_present": True,
        "manifest_schema_valid": manifest_schema_ok,
        "manifest_content_addressed": manifest_hash_ok,
        "target_source_scope_content_addressed": target_scope_ok and target_hashes_ok,
        "protected_legacy_config_content_addressed": protected_ok,
        "target_source_reference_graph_closed": graph_closed,
        "single_owner_per_resource": owner_closed,
        "forbidden_cross_read_rules_complete": rules_ok,
    }
    evidence = {
        "manifest_path": manifest_relative,
        "manifest_sha256": manifest_self_hash,
        "computed_manifest_sha256": _manifest_digest(manifest),
        "target_activation": None if activation is None else {
            "path": activation[0], "sha256": activation[1], "owner": activation[2], "consumers": list(activation[3]),
        },
        "target_source_closure": [
            {"path": path, "sha256": sha, "owner": owner, "consumers": list(consumers)}
            for path, sha, owner, consumers in closure
        ],
        "protected_legacy_config": None if protected_entry is None else {
            "path": protected_entry[0], "sha256": protected_entry[1], "owner": protected_entry[2], "consumers": list(protected_entry[3]),
        },
        "forbidden_cross_read": forbidden_rules,
        "target_reference_graph": reference_graph,
    }
    return checks, hits, evidence


def _data_lane_contract(semantic: Mapping[str, str]) -> tuple[dict[str, bool], list[dict[str, Any]], dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    basket_items = {name: text for name, text in semantic.items() if "basket_kernel/" in name or name.endswith("/runtime.py")}
    basket_blob = "\n".join(basket_items.values()).lower()
    hard_blob = "\n".join(text for name, text in semantic.items() if any(token in name for token in ("atomic_ledger", "production.py", "reported_trajectory_no_daemon"))).lower()
    forbidden_basket = (
        "taf",
        "nwp",
        "metar",
        "speci",
        "hard_lower_bound",
        "hard_no",
    )
    # Match direct weather/hard-NO identifiers or fields, not immutable
    # protocol/version labels such as ``p0_nwp_historical_meta_anchor_v4``.
    # The old substring scan made a content-addressed posterior version look
    # like a raw NWP read and blocked an otherwise closed basket lane.
    forbidden_basket_hits = {
        token
        for token in forbidden_basket
        if re.search(
            rf"(?<![a-z0-9_]){re.escape(token)}"
            rf"(?:_[a-z0-9]+)*(?![a-z0-9_])",
            basket_blob,
            re.IGNORECASE,
        )
    }
    for token in sorted(forbidden_basket_hits):
        _append_hit(
            hits,
            code="basket_lane_reads_forbidden_weather_or_hard_no_field",
            evidence=token,
        )
    hard_probability = re.search(
        r"hard[_ -]?no[_ .:-]*(?:probability|prior|likelihood|taf|nwp|regime)",
        hard_blob,
        re.IGNORECASE,
    )
    if hard_probability:
        _append_hit(hits, code="hard_no_lane_reads_probability_or_weather_field", evidence=hard_probability.group(0)[:240])
    checks = {
        "basket_consumes_materialized_probability_and_regime": all(token in basket_blob for token in ("materialized", "probability", "regime")),
        "basket_consumes_official_ws": "official" in basket_blob and ("ws" in basket_blob or "clob" in basket_blob),
        "hard_no_uses_official_reported_hard_bound": all(token in hard_blob for token in ("official", "reported", "hard_lower_bound")),
        "hard_no_probability_isolation": not bool(hard_probability),
    }
    return checks, hits, {
        "basket_scope_files": sorted(basket_items),
        "basket_forbidden_tokens": list(forbidden_basket),
        "basket_forbidden_tokens_observed": sorted(
            forbidden_basket_hits
        ),
        "hard_no_scope": "atomic_ledger+production+reported_trajectory_no_daemon",
    }


def _first_call_argument(active: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(active)
    return match.group(1).strip() if match else None


def _all_call_arguments(active: str, pattern: re.Pattern[str]) -> list[str]:
    return [match.group(1).strip() for match in pattern.finditer(active)]


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def _writer_authority_contract(
    *,
    semantic: Mapping[str, str],
    authority_semantic: Mapping[str, str],
) -> tuple[dict[str, bool], list[dict[str, Any]], dict[str, Any]]:
    """Classify writer authorities without opening a database.

    The daemon is an orchestrator: its archive connection is supplied to the
    trajectory engine while its target connection is supplied to the target
    worker.  Treating the daemon as one undifferentiated writer was the source
    of the previous audit blind spot.  This contract therefore records the
    two bindings separately and only fails on an explicit write-path crossing.
    All checks are bounded string/AST-scope checks; no module import, SQLite
    connection, network call, or service operation occurs here.
    """

    hits: list[dict[str, Any]] = []
    engine = authority_semantic.get(HARD_NO_AUTHORITY_SOURCE, "")
    daemon = semantic.get("scripts/reported_trajectory_no_daemon.py", "")
    atomic = semantic.get("bayes_basket_target/atomic_ledger.py", "")
    target_worker = semantic.get("bayes_basket_target/production_worker.py", "")
    target_production = semantic.get("bayes_basket_target/production.py", "")

    hard_no_writer_detected = _contains_any(
        engine,
        ("_write_decision", "strategy_lane", "hard_no", "conn.execute"),
    )
    hard_no_engine_target_markers = _contains_any(
        engine,
        (TARGET_DB_PATH, "bayes_basket_target_ssot.db", "default_target_db", "commit_conn", "atomic_ledger"),
    )
    hard_no_call_arguments = _all_call_arguments(daemon, _HARD_NO_ENGINE_CALL_RE)
    hard_no_call_argument = hard_no_call_arguments[0] if hard_no_call_arguments else None
    hard_no_call_crosses_target = any(
        _contains_any(
            argument,
            ("default_target_db", "bayes_basket_target_ssot.db", "commit_conn", "target_db"),
        )
        for argument in hard_no_call_arguments
    )
    archive_binding = (
        _contains_any(daemon, (ARCHIVE_DB_PATH, "default_db"))
        and _contains_any(daemon, ("reportedtrajectorynoengine", "run_once", "lane_workers"))
        and _contains_any(daemon, ("parser", "args.db", "open_runtime"))
    )

    if hard_no_writer_detected and hard_no_engine_target_markers:
        _append_hit(
            hits,
            code="hard_no_writer_target_database_reference",
            file=HARD_NO_AUTHORITY_SOURCE,
            evidence="hard-NO engine contains target database/target writer reference",
        )
    if hard_no_call_crosses_target:
        _append_hit(
            hits,
            code="target_runtime_calls_hard_no_engine_with_target_database",
            file="scripts/reported_trajectory_no_daemon.py",
            evidence={"first_argument": hard_no_call_argument},
        )
    if hard_no_writer_detected and not archive_binding:
        _append_hit(
            hits,
            code="hard_no_writer_archive_database_binding_missing",
            file=HARD_NO_AUTHORITY_SOURCE,
            evidence={"expected": ARCHIVE_DB_PATH, "orchestrator": "scripts/reported_trajectory_no_daemon.py"},
        )

    target_atomic_writer_detected = _contains_any(
        atomic,
        ("commit_target_atomic_bundle", "conn.execute", "basket_target_execution_cycles"),
    )
    target_binding_call_arguments = _all_call_arguments(daemon, _TARGET_WRITER_CALL_RE)
    target_binding_call_argument = target_binding_call_arguments[0] if target_binding_call_arguments else None
    target_binding_is_target = any(
        _contains_any(
            argument,
            ("default_target_db", TARGET_DB_PATH, "bayes_basket_target_ssot.db"),
        )
        for argument in target_binding_call_arguments
    )
    target_writer_archive_argument = any(
        _contains_any(
            argument,
            ("default_db", ARCHIVE_DB_PATH, "args.db", "archive_db"),
        )
        for argument in target_binding_call_arguments
    )
    target_runtime_calls_hard_no = any(
        _contains_any(text, ("reportedtrajectorynoengine", "reported_trajectory_no_engine"))
        for name, text in semantic.items()
        if name.startswith("bayes_basket_target/")
    )
    target_source_archive_markers = any(
        _contains_any(text, (ARCHIVE_DB_PATH, "archive.db"))
        for name, text in semantic.items()
        if name.startswith("bayes_basket_target/")
        and name != "bayes_basket_target/database.py"
    )

    if target_writer_archive_argument or target_source_archive_markers:
        _append_hit(
            hits,
            code="basket_writer_archive_database_reference",
            file="scripts/reported_trajectory_no_daemon.py" if target_writer_archive_argument else "bayes_basket_target",
            evidence={
                "target_writer_first_argument": target_binding_call_argument,
                "target_source_archive_marker": target_source_archive_markers,
            },
        )
    if target_runtime_calls_hard_no:
        _append_hit(
            hits,
            code="target_runtime_calls_hard_no_engine",
            evidence="target runtime imports or invokes reported_trajectory_no_engine",
        )
    if target_atomic_writer_detected and not target_binding_is_target:
        _append_hit(
            hits,
            code="basket_atomic_ledger_target_database_binding_missing",
            file="bayes_basket_target/atomic_ledger.py",
            evidence={"expected": TARGET_DB_PATH, "orchestrator": "scripts/reported_trajectory_no_daemon.py"},
        )
    if target_production and target_atomic_writer_detected and not _contains_any(
        target_production, ("commit_target_atomic_bundle", "strategy_lane", "basket")
    ):
        _append_hit(
            hits,
            code="basket_production_atomic_ledger_route_unproven",
            file="bayes_basket_target/production.py",
            evidence="production.py does not expose the basket atomic commit route",
        )

    hard_no_ok = hard_no_writer_detected and archive_binding and not any(
        item["code"] in {
            "hard_no_writer_target_database_reference",
            "target_runtime_calls_hard_no_engine_with_target_database",
        }
        for item in hits
    )
    basket_ok = target_atomic_writer_detected and target_binding_is_target and not any(
        item["code"] in {
            "basket_writer_archive_database_reference",
            "target_runtime_calls_hard_no_engine",
            "basket_atomic_ledger_target_database_binding_missing",
            "basket_production_atomic_ledger_route_unproven",
        }
        for item in hits
    )
    checks = {
        "authority_hard_no_engine_scanned": bool(engine),
        "hard_no_writer_archive_database_only": hard_no_ok,
        "basket_atomic_ledger_target_database_only": basket_ok,
        "cross_lane_writer_isolation": not hits,
    }
    evidence = {
        "authority_sources": [HARD_NO_AUTHORITY_SOURCE] if engine else [],
        "writer_authorities": {
            "hard_no": {
                "source": HARD_NO_AUTHORITY_SOURCE,
                "database": ARCHIVE_DB_PATH,
                "binding": "daemon archive connection (DEFAULT_DB/args.db)",
                "writer_detected": hard_no_writer_detected,
            },
            "basket": {
                "source": "bayes_basket_target/atomic_ledger.py",
                "database": TARGET_DB_PATH,
                "binding": "target worker commit connection",
                "writer_detected": target_atomic_writer_detected,
            },
        },
        "orchestrator": {
            "source": "scripts/reported_trajectory_no_daemon.py",
            "hard_no_first_argument": hard_no_call_argument,
            "hard_no_call_arguments": hard_no_call_arguments,
            "target_writer_first_argument": target_binding_call_argument,
            "target_writer_call_arguments": target_binding_call_arguments,
            "hard_no_archive_binding": archive_binding,
            "target_database_binding": target_binding_is_target,
        },
        "allowed_databases": {
            "hard_no": ARCHIVE_DB_PATH,
            "basket": TARGET_DB_PATH,
        },
    }
    return checks, hits, evidence


def _candidate_inventory(
    *,
    root: Path,
    manifests: Sequence[Mapping[str, Any]],
    semantic: Mapping[str, str],
    service_snapshot: Mapping[str, Sequence[str]] | None,
    max_file_bytes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    candidates: list[Path] = []
    hits: list[dict[str, Any]] = []
    scanned = 0
    try:
        # ``Path.rglob`` can spend an unbounded amount of time descending into
        # retained artifact trees before yielding a candidate.  Prune known
        # historical/derived directories at the directory-walk boundary and
        # stop after a hard candidate cap.
        for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in EXCLUDED_DIRS and name != "artifacts" and not name.startswith(".")
            )
            for filename in sorted(filenames):
                if len(candidates) >= MAX_CANDIDATES * 2:
                    break
                path = current_path / filename
                if path.suffix.lower() not in SAFE_CANDIDATE_SUFFIXES:
                    continue
                relative = _relative(root, path)
                lowered = relative.lower()
                if not any(marker in lowered for marker in LEGACY_RUNTIME_MARKERS + ("feedback_loop", "daily_scan", "forecast_source_weight_research")):
                    continue
                candidates.append(path)
                scanned += 1
                try:
                    if path.stat().st_size > max_file_bytes:
                        hits.append({"code": "candidate_file_bytes_exceeded", "file": relative})
                except OSError as exc:
                    hits.append({"code": "candidate_file_stat_failed", "file": relative, "error": str(exc)})
            if len(candidates) >= MAX_CANDIDATES * 2:
                break
    except OSError as exc:
        hits.append({"code": "candidate_scan_failed", "error": str(exc)})
    candidates = sorted(set(candidates), key=lambda p: str(p))[:MAX_CANDIDATES]

    graph: dict[str, list[str]] = {}
    haystacks = {name: text.lower() for name, text in semantic.items()}
    for item in manifests:
        name = str(item.get("file") or "")
        haystacks[name] = _canonical_json(item).lower()
    for candidate in candidates:
        relative = _relative(root, candidate)
        basename = candidate.name.lower()
        stem = candidate.stem.lower()
        needles = [relative.lower(), basename]
        if len(stem) >= 8:
            needles.append(stem)
        refs: list[str] = []
        for source_name, text in haystacks.items():
            if source_name == relative:
                continue
            if any(needle in text for needle in needles):
                refs.append(source_name)
        graph[relative] = sorted(set(refs))[:MAX_GRAPH_EDGES]
    loaded_labels = set()
    if service_snapshot:
        loaded_labels = {str(x) for values in service_snapshot.values() for x in values}
    inventory: list[dict[str, Any]] = []
    for candidate in candidates:
        relative = _relative(root, candidate)
        refs = graph.get(relative, [])
        matching_loaded = [label for label in loaded_labels if candidate.stem.lower() in label.lower() or candidate.name.lower() in label.lower()]
        inventory.append({
            "path": relative,
            "category": next((marker for marker in LEGACY_SERVICE_MARKERS if marker in relative.lower()), "legacy_runtime"),
            "production_closure_referenced_by": refs,
            "loaded_service_matches": sorted(matching_loaded),
            # This bounded audit proves only that a retired file is outside
            # the active production closure.  It deliberately does not scan
            # the complete repository, tests, historical rebuild tools or
            # user worktree state, so it must never authorize deletion.
            "production_closure_unreferenced": not refs and not matching_loaded,
            "safe_to_delete_without_operator_review": False,
            "deletion_authority": "NONE_AUDIT_ONLY",
        })
    return inventory, hits, graph


def audit(
    *,
    project_root: Path = ROOT,
    source_files: Sequence[str] | None = None,
    launchd_dir: Path | str = "launchd",
    service_snapshot: Mapping[str, Sequence[str]] | None = None,
    probe_services: bool = False,
    required_source_names: Sequence[str] | None = None,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    """Build a deterministic, fail-closed closure receipt without side effects."""

    root = project_root.expanduser().resolve()
    requested = tuple(source_files or DEFAULT_SOURCE_SCOPE)
    semantic, parsed, scope_hits, source_bytes = _parse_source_scope(
        root=root,
        source_files=requested,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    # Authority sources are scanned under the same byte/file budget but are
    # intentionally kept outside the target isolation-manifest scope.  This
    # is what lets the audit inspect the archive hard-NO writer without
    # declaring that legacy writer to be a target consumer.
    authority_semantic, _authority_parsed, authority_scope_hits, authority_bytes = _parse_source_scope(
        root=root,
        source_files=AUTHORITY_SOURCE_SCOPE,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max(0, max_total_bytes - source_bytes),
    )
    scope_hits.extend(authority_scope_hits)
    manifests, launchd_hits, launchd_bytes = _parse_launchd(
        root=root,
        launchd_dir=Path(launchd_dir),
        max_file_bytes=max_file_bytes,
        max_total_bytes=max(0, max_total_bytes - source_bytes - authority_bytes),
    )
    scope_hits.extend(launchd_hits)
    if probe_services and service_snapshot is None:
        # Probe every repository-declared service as well as the three target
        # labels.  Probing only TARGET_SERVICE_LABELS makes a dormant but still
        # registered legacy LaunchDaemon invisible and can falsely certify the
        # system domain as target-only.
        declared_labels = tuple(
            sorted(
                {
                    str(item.get("label") or "")
                    for item in manifests
                    if str(item.get("label") or "")
                }
            )
        )
        probe_labels = tuple(sorted(set(TARGET_SERVICE_LABELS) | set(declared_labels)))
        service_snapshot, probe_hits = _probe_services(probe_labels)
        scope_hits.extend(probe_hits)

    violations: list[dict[str, Any]] = []
    import_hits = _python_import_hits(semantic, parsed)
    violations.extend(import_hits)
    violations.extend(_semantic_marker_hits(semantic))
    gateway_hits, gateway_owners = _scan_gateway_owner(semantic)
    violations.extend(gateway_hits)
    retired_gateway_checks, retired_gateway_hits, retired_gateway_evidence = (
        _retired_gateway_contract(semantic)
    )
    violations.extend(retired_gateway_hits)
    service_checks, service_hits, service_evidence = _launchd_contract(manifests, service_snapshot)
    violations.extend(service_hits)
    config_checks, config_hits, config_evidence = _config_contract(parsed, semantic)
    violations.extend(config_hits)
    required = tuple(required_source_names or DEFAULT_SOURCE_SCOPE)
    isolation_checks, isolation_hits, isolation_evidence = _validate_runtime_config_isolation(
        root=root,
        manifest_path=RUNTIME_ISOLATION_MANIFEST,
        requested_source_files=requested,
        required_source_names=required,
        semantic=semantic,
        max_file_bytes=max_file_bytes,
    )
    violations.extend(isolation_hits)
    data_checks, data_hits, data_evidence = _data_lane_contract(semantic)
    violations.extend(data_hits)
    for check_name, passed in data_checks.items():
        if not passed:
            _append_hit(violations, code=f"data_lane_check_failed:{check_name}", evidence=check_name)
    authority_checks, authority_hits, authority_evidence = _writer_authority_contract(
        semantic=semantic,
        authority_semantic=authority_semantic,
    )
    violations.extend(authority_hits)
    for check_name, passed in authority_checks.items():
        if not passed:
            _append_hit(violations, code=f"writer_authority_check_failed:{check_name}", evidence=check_name)

    included = set(semantic)
    missing_required = sorted(name for name in required if name not in included)
    if missing_required:
        _append_hit(violations, code="target_runtime_scope_incomplete", evidence=missing_required)

    target_entry_names = [name for name in semantic if name.endswith("reported_trajectory_no_daemon.py")]
    target_consumer_count = len(target_entry_names)
    target_consumer_check = target_consumer_count == 1 and not any(
        any(marker in name.lower() for marker in LEGACY_RUNTIME_MARKERS) for name in target_entry_names
    )
    if not target_consumer_check:
        _append_hit(violations, code="target_production_consumer_not_unique", evidence={"entries": target_entry_names, "count": target_consumer_count})

    source_blob = "\n".join(semantic.values()).lower()
    blocking_check = "auto_trade_decisions" in source_blob and all(token in source_blob for token in ("will_order", "decided_state"))
    if not blocking_check:
        _append_hit(violations, code="blocking_authority_not_auto_trade_decisions", evidence="auto_trade_decisions/will_order/decided_state")
    no_live_check = "production_no_live" in source_blob and not _LIVE_TRUE_RE.search(source_blob)
    if not no_live_check:
        _append_hit(violations, code="runtime_no_live_boundary_not_closed", evidence="production_no_live + all mutation flags false")
    gateway_unique_check = gateway_owners == ["scripts/clob_order_service.mjs"]
    if not gateway_unique_check:
        _append_hit(violations, code="gateway_boundary_not_unique", evidence=gateway_owners)

    candidates, candidate_hits, reference_graph = _candidate_inventory(
        root=root,
        manifests=manifests,
        semantic=semantic,
        service_snapshot=service_snapshot,
        max_file_bytes=max_file_bytes,
    )
    scope_hits.extend(candidate_hits)

    violation_summary: dict[str, int] = {}
    for item in violations:
        code = str(item.get("code") or "unknown")
        violation_summary[code] = violation_summary.get(code, 0) + 1
    candidate_digest = _content_sha(candidates)
    reference_graph_digest = _content_sha(reference_graph)
    candidate_sample = candidates[:10]
    safe_deletion_candidates: list[dict[str, Any]] = []

    checks: dict[str, bool] = {
        "target_runtime_scope_complete": not missing_required and bool(semantic),
        "target_only_consumer_unique": target_consumer_check,
        "legacy_runtime_dependency_absent": not any(item["code"].startswith("legacy_runtime") for item in violations),
        "no_forbidden_market_rest_or_tick_fallback": not any(item["code"] == "forbidden_market_rest_or_tick_dependency" for item in violations),
        "production_no_live_and_mutation_flags_closed": no_live_check and config_checks["config_live_mutation_flags_false"],
        "gateway_boundary_unique": gateway_unique_check,
        "retired_gateway_entry_closed": all(
            retired_gateway_checks.values()
        ),
        "blocking_authority_is_auto_trade_decisions": blocking_check,
        "basket_data_dependencies_closed": all(data_checks.values()),
        "writer_authorities_and_database_bindings_closed": all(authority_checks.values()),
        "runtime_config_isolation_closed": all(isolation_checks.values()),
        "service_domain_and_manifest_closed": all(service_checks.values()),
        "config_contract_closed": all(config_checks.values()),
        "bounded_scan_closed": not scope_hits,
    }

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "closure_version": CLOSURE_VERSION,
        "status": "PASS" if all(checks.values()) and not violations else "BLOCKED",
        "ok": all(checks.values()) and not violations,
        "checks": checks,
        "violation_summary": dict(sorted(violation_summary.items())),
        "violations": violations[:32],
        "scope": {
            "project_root": str(root),
            "source_files_requested": list(requested),
            "source_files_included": sorted(semantic),
            "authority_source_files_requested": list(AUTHORITY_SOURCE_SCOPE),
            "authority_source_files_included": sorted(authority_semantic),
            "launchd_dir": _relative(root, (root / launchd_dir) if not Path(launchd_dir).is_absolute() else Path(launchd_dir)),
            "source_bytes": source_bytes,
            "authority_source_bytes": authority_bytes,
            "launchd_bytes": launchd_bytes,
            "max_files": int(max_files),
            "max_file_bytes": int(max_file_bytes),
            "max_total_bytes": int(max_total_bytes),
            "excluded_directories": sorted(EXCLUDED_DIRS),
            "resource_hits": {
                "count": len(scope_hits),
                "content_sha256": _content_sha(scope_hits),
                "sample": scope_hits[:16],
            },
        },
        "service_closure": {
            "target_services": list(TARGET_SERVICE_LABELS),
            "required_domain": "user",
            "checks": service_checks,
            "evidence": service_evidence,
            "declared_manifest_count": len(manifests),
        },
        "production_boundary": {
            "execution_mode": "production_no_live",
            "live_submit_authorized": False,
            "authorizes_order": False,
            "places_orders": False,
            "calls_order_gateway": False,
            "gateway_owner": "scripts/clob_order_service.mjs",
            "gateway_owners_observed": gateway_owners,
            "blocking_authority": "auto_trade_decisions",
        },
        "retired_gateway_closure": {
            "checks": retired_gateway_checks,
            "evidence": retired_gateway_evidence,
        },
        "data_lane_closure": {
            "checks": data_checks,
            "evidence": data_evidence,
        },
        "writer_authority_closure": {
            "checks": authority_checks,
            "evidence": authority_evidence,
        },
        "runtime_config_isolation": {
            "checks": isolation_checks,
            "evidence": isolation_evidence,
        },
        "configuration_closure": config_evidence,
        "retired_runtime_candidates": {
            "count": len(candidates),
            "content_sha256": candidate_digest,
            "sample": candidate_sample,
            "deletion_authorized": False,
            "deletion_gate": (
                "full_repository_dependency_and_worktree_audit_required"
            ),
        },
        "safe_deletion_candidates": {
            "count": len(safe_deletion_candidates),
            "content_sha256": _content_sha(safe_deletion_candidates),
            "sample": safe_deletion_candidates,
            "deletion_authorized": False,
            "reason": (
                "production_closure_nonreference_is_not_deletion_proof"
            ),
        },
        "reference_graph": {
            "content_sha256": reference_graph_digest,
            "edge_count": sum(len(edges) for edges in reference_graph.values()),
            "node_count": len(reference_graph),
        },
        "read_only_boundary": {
            "opens_database": False,
            "network_access": False,
            "calls_order_gateway": False,
            "changes_launchd": False,
            "deletes_files": False,
            "writes_production_state": False,
        },
    }
    payload["receipt_sha256"] = _content_sha({key: value for key, value in payload.items() if key != "receipt_sha256"})
    return payload


def _write_bounded_json(path: Path, value: Mapping[str, Any], *, root: Path) -> None:
    resolved = path.expanduser().resolve()
    if not _inside(root, resolved):
        raise ValueError("closure_audit_output_outside_project")
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(rendered.encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise ValueError("closure_audit_receipt_resource_limit_exceeded")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(rendered, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--probe-services", action="store_true", help="read launchctl state; never changes it")
    parser.add_argument("--max-files", type=int, default=MAX_FILES)
    parser.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL_BYTES)
    args = parser.parse_args(argv)
    if min(args.max_files, args.max_file_bytes, args.max_total_bytes) <= 0:
        raise SystemExit("resource limits must be positive")
    root = args.root.expanduser().resolve()
    result = audit(
        project_root=root,
        probe_services=args.probe_services,
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
