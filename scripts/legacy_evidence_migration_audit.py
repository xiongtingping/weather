#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from legacy_evidence_migration_registry import (  # noqa: E402
    SCHEMA_VERSION as REGISTRY_SCHEMA_VERSION,
    classify_legacy_evidence_field,
    legacy_evidence_registry_summary,
    registry_rows,
)


SCHEMA_VERSION = "legacy_evidence_migration_audit_v2"

SCAN_SUFFIXES = {
    ".py",
    ".sh",
    ".mjs",
    ".js",
    ".html",
}

EXCLUDE_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "data",
    "docs",
    "node_modules",
    "tests",
}

EXCLUDE_FILENAME_MARKERS = (
    ".head_backup_",
    ".backup",
    ".bak",
)

LEGACY_FIELD_RE = re.compile(
    r"\b(?:"
    r"q_bayes_(?:model|wu_forecast|weather_com_forecast|taf_peak|observed_nowcast|nowcast|temperature_trajectory|physical_likelihood|independent_physics_residual|climo_regime|post_cool_rebound_[a-z_]+)_evidence(?:_[a-z_]+)?"
    r"|(?:model|wu_forecast|weather_com_forecast|taf_peak|observed_nowcast|nowcast|temperature_trajectory|physical_likelihood|independent_physics_residual|climo_regime|post_cool_rebound_[a-z_]+)_evidence(?:_[a-z_]+)?"
    r")\b"
)

PY_FUNCTION_RE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
PY_TOP_LEVEL_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=")
JS_FUNCTION_RE = re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")

USAGE_ROLE_MIGRATION_GOVERNANCE = "migration_governance"
USAGE_ROLE_PRODUCTION_PROBABILITY_RUNTIME = "production_probability_runtime"
USAGE_ROLE_PRODUCTION_COMPAT_SURFACE = "production_compat_surface"
USAGE_ROLE_UI_PRIMARY_SURFACE = "ui_primary_surface"
USAGE_ROLE_UI_RETIRED_COMPAT_SURFACE = "ui_retired_compat_surface"
USAGE_ROLE_DIAGNOSTIC_OR_BACKTEST = "diagnostic_or_backtest"
USAGE_ROLE_COMPAT_REPORT = "compat_report"
USAGE_ROLE_PRIOR_HELPER = "prior_helper"
USAGE_ROLE_UNKNOWN = "unknown_source"

MIGRATION_GOVERNANCE_FILES = {
    "legacy_evidence_migration_registry.py",
    "scripts/legacy_evidence_migration_audit.py",
    "scripts/phase0_ssot_inventory.py",
}

PRODUCTION_PROBABILITY_RUNTIME_FILES = {
    "posterior_pipeline_entry_runtime.py",
}

LEGACY_ADDITIVE_COMPAT_RUNTIME_FILES = {
    "bayesian_probability_runtime.py",
    "legacy_additive_bayesian_probability_runtime.py",
}

BAYESIAN_PROBABILITY_RETIRED_COMPAT_SCOPES = {
    "_apply_legacy_additive_bayesian_probability_update",
    "_build_physical_d0_explanation",
    "_physical_likelihood_evidence_scale",
    "_post_cool_rebound_channel_payload",
    "_post_cool_rebound_channel_probs",
    "_post_cool_rebound_risk_channel_probs",
    "_post_cool_rebound_risk_payload",
    "_temperature_trajectory_channel_probs",
}

PRODUCTION_COMPAT_SURFACE_FILES = {
    "scan_weather_market.py",
    "scripts/hard_source_direct_poller.py",
    "scripts/materialize_ctp_latest_facts.py",
}

PRODUCTION_DROP_ONLY_COMPAT_SCOPES = {
    ("scripts/hard_source_direct_poller.py", "_compact_hard_source_fact_payload_for_writer"),
}

UI_PRIMARY_SURFACE_FILES = {
    "ui/index.html",
    "ui/server.py",
}

UI_SERVER_RETIRED_COMPAT_SCOPES = {
    "_bayes_chain_for_distribution_row",
    "_legacy_bayes_chain_for_distribution_row",
    "_compact_bayes_chain_for_bucket_series",
    "_compact_distribution_row_for_ui_snapshot",
    "_FAST_TRADE_ROW_DROP_KEYS",
    "_ui_snapshot_soft_forecast_channels",
    "_ui_temperature_trajectory_status_from_payload",
}

UI_INDEX_RETIRED_COMPAT_SCOPES = {
    "bayesChainForDistributionRow",
    "firstPhysicalLikelihoodRow",
    "legacyPhysicalLikelihoodSnapshotCompatFromItem",
    "legacyDistributionLikelihoodCompatForRow",
}

COMPAT_REPORT_FILES = {
    "chengdu_intraday_summary.py",
    "bayesian_data_role_contract.py",
}

PRIOR_HELPER_FILES = {
    "climo_regime_runtime.py",
}

DIAGNOSTIC_OR_BACKTEST_MARKERS = (
    "audit",
    "backtest",
    "batch",
    "calibrate",
    "check_",
    "diagnostic",
    "drill",
    "monitor",
    "plan",
    "probe",
    "replay",
    "review",
    "summary",
    "test_",
)


def classify_legacy_evidence_usage(rel_path: str, line: str = "", *, scope: str = "") -> dict[str, Any]:
    """Classify the source-code role of a legacy evidence reference.

    Field-level migration status is not enough: the same old field can be safe
    in a migration registry, risky in UI primary explanation, and blocking in
    the old additive posterior runtime. This function is intentionally static
    and conservative; it never infers runtime behavior from data.
    """

    rel = str(rel_path or "").strip().lstrip("./")
    lowered = rel.lower()
    text = str(line or "")

    if rel in MIGRATION_GOVERNANCE_FILES:
        return {
            "role": USAGE_ROLE_MIGRATION_GOVERNANCE,
            "production_blocker": False,
            "ui_primary_blocker": False,
            "warning": False,
            "allowed": True,
            "reason": "migration_registry_or_audit_reference_allowed",
        }
    if rel in LEGACY_ADDITIVE_COMPAT_RUNTIME_FILES:
        return {
            "role": USAGE_ROLE_PRODUCTION_COMPAT_SURFACE,
            "production_blocker": False,
            "ui_primary_blocker": False,
            "warning": True,
            "allowed": True,
            "reason": "legacy_additive_runtime_retired_compat_module",
        }
    if rel in PRODUCTION_PROBABILITY_RUNTIME_FILES and str(scope or "") in BAYESIAN_PROBABILITY_RETIRED_COMPAT_SCOPES:
        return {
            "role": USAGE_ROLE_PRODUCTION_COMPAT_SURFACE,
            "production_blocker": False,
            "ui_primary_blocker": False,
            "warning": True,
            "allowed": True,
            "reason": f"bayesian_probability_retired_legacy_compat_scope:{scope}",
        }
    if rel in PRODUCTION_PROBABILITY_RUNTIME_FILES:
        return {
            "role": USAGE_ROLE_PRODUCTION_PROBABILITY_RUNTIME,
            "production_blocker": True,
            "ui_primary_blocker": False,
            "warning": False,
            "allowed": False,
            "reason": "old_additive_evidence_runtime_still_builds_production_posterior",
        }
    if rel == "ui/server.py" and str(scope or "") in UI_SERVER_RETIRED_COMPAT_SCOPES:
        return {
            "role": USAGE_ROLE_UI_RETIRED_COMPAT_SURFACE,
            "production_blocker": False,
            "ui_primary_blocker": False,
            "warning": True,
            "allowed": True,
            "reason": f"ui_server_retired_compat_scope:{scope}",
        }
    if rel == "ui/index.html" and str(scope or "") in UI_INDEX_RETIRED_COMPAT_SCOPES:
        return {
            "role": USAGE_ROLE_UI_RETIRED_COMPAT_SURFACE,
            "production_blocker": False,
            "ui_primary_blocker": False,
            "warning": True,
            "allowed": True,
            "reason": f"ui_index_retired_compat_scope:{scope}",
        }
    if rel in UI_PRIMARY_SURFACE_FILES:
        reason = "ui_primary_or_api_surface_references_legacy_evidence"
        if "role" in text and "diagnostic" in text:
            reason = "ui_diagnostic_reference_requires_explicit_retired_compat_marker"
        return {
            "role": USAGE_ROLE_UI_PRIMARY_SURFACE,
            "production_blocker": False,
            "ui_primary_blocker": True,
            "warning": False,
            "allowed": False,
            "reason": reason,
        }
    if (rel, str(scope or "")) in PRODUCTION_DROP_ONLY_COMPAT_SCOPES:
        return {
            "role": USAGE_ROLE_PRODUCTION_COMPAT_SURFACE,
            "production_blocker": False,
            "ui_primary_blocker": False,
            "warning": False,
            "allowed": True,
            "reason": f"production_drop_only_legacy_field_compaction_scope:{scope}",
        }
    if rel in PRODUCTION_COMPAT_SURFACE_FILES:
        return {
            "role": USAGE_ROLE_PRODUCTION_COMPAT_SURFACE,
            "production_blocker": False,
            "ui_primary_blocker": False,
            "warning": True,
            "allowed": True,
            "reason": "production_payload_or_materializer_compat_reference_must_not_gate_decision",
        }
    if rel in PRIOR_HELPER_FILES:
        return {
            "role": USAGE_ROLE_PRIOR_HELPER,
            "production_blocker": False,
            "ui_primary_blocker": False,
            "warning": False,
            "allowed": True,
            "reason": "prior_helper_reference_allowed_until_temperature_prior_runtime_migration",
        }
    if rel in COMPAT_REPORT_FILES:
        return {
            "role": USAGE_ROLE_COMPAT_REPORT,
            "production_blocker": False,
            "ui_primary_blocker": False,
            "warning": True,
            "allowed": True,
            "reason": "report_or_contract_compat_reference_must_be_retired_after_migration",
        }
    if lowered.startswith("scripts/") and any(marker in lowered for marker in DIAGNOSTIC_OR_BACKTEST_MARKERS):
        return {
            "role": USAGE_ROLE_DIAGNOSTIC_OR_BACKTEST,
            "production_blocker": False,
            "ui_primary_blocker": False,
            "warning": False,
            "allowed": True,
            "reason": "diagnostic_or_backtest_reference_allowed_read_only",
        }
    return {
        "role": USAGE_ROLE_UNKNOWN,
        "production_blocker": False,
        "ui_primary_blocker": False,
        "warning": False,
        "allowed": False,
        "reason": "legacy_evidence_reference_in_unclassified_code_path",
    }


def _iter_files(root: Path, *, include_tests: bool) -> Iterable[Path]:
    excluded = set(EXCLUDE_DIRS)
    if include_tests:
        excluded.discard("tests")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in excluded]
        directory = Path(dirpath)
        for filename in filenames:
            path = directory / filename
            if any(marker in path.name for marker in EXCLUDE_FILENAME_MARKERS):
                continue
            if path.suffix not in SCAN_SUFFIXES:
                continue
            if not path.is_file():
                continue
            yield path


def _match_context(line: str) -> str:
    return " ".join(str(line or "").strip().split())[:260]


def build_legacy_evidence_migration_audit(
    root: Path,
    *,
    include_tests: bool = False,
    max_samples_per_field: int = 20,
) -> dict[str, Any]:
    repo_root = Path(root).expanduser().resolve()
    max_samples = max(0, int(max_samples_per_field))
    files_scanned = 0
    bytes_scanned = 0
    file_errors: list[dict[str, Any]] = []
    by_field: dict[str, dict[str, Any]] = {}
    usage_by_role: dict[str, int] = {}
    usage_blockers: list[dict[str, Any]] = []
    production_blocking_references: list[dict[str, Any]] = []
    ui_primary_blocking_references: list[dict[str, Any]] = []
    warning_references: list[dict[str, Any]] = []
    allowed_references: list[dict[str, Any]] = []
    unknown_usage_references: list[dict[str, Any]] = []
    reference_by_file = Counter()
    warning_reference_by_file = Counter()
    production_blocking_reference_by_file = Counter()
    ui_primary_blocking_reference_by_file = Counter()
    unknown_usage_reference_by_file = Counter()

    for path in sorted(_iter_files(repo_root, include_tests=include_tests)):
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            file_errors.append({"file": str(path.relative_to(repo_root)), "error": str(exc)})
            continue
        bytes_scanned += len(text.encode("utf-8", errors="ignore"))
        rel = str(path.relative_to(repo_root))
        current_scope = ""
        for lineno, line in enumerate(text.splitlines(), start=1):
            if path.suffix == ".py":
                function_match = PY_FUNCTION_RE.match(line)
                if function_match:
                    current_scope = function_match.group(1)
                elif line and not line.startswith((" ", "\t")):
                    assignment_match = PY_TOP_LEVEL_ASSIGN_RE.match(line)
                    if assignment_match:
                        current_scope = assignment_match.group(1)
            elif path.suffix in {".html", ".js", ".mjs"}:
                function_match = JS_FUNCTION_RE.match(line)
                if function_match:
                    current_scope = function_match.group(1)
            for match in LEGACY_FIELD_RE.finditer(line):
                field = str(match.group(0))
                classification = classify_legacy_evidence_field(field)
                usage = classify_legacy_evidence_usage(rel, line, scope=current_scope)
                canonical = str(classification.get("canonical_field") or field)
                item = by_field.setdefault(
                    canonical,
                    {
                        "field": canonical,
                        "classification": classification,
                        "reference_count": 0,
                        "files": {},
                        "usage": {
                            "by_role": {},
                            "production_blocking_reference_count": 0,
                            "ui_primary_blocking_reference_count": 0,
                            "warning_reference_count": 0,
                            "allowed_reference_count": 0,
                            "unknown_usage_reference_count": 0,
                        },
                        "samples": [],
                        "truncated": False,
                    },
                )
                item["reference_count"] += 1
                files = item["files"]
                files[rel] = int(files.get(rel) or 0) + 1
                role = str(usage.get("role") or USAGE_ROLE_UNKNOWN)
                usage_by_role[role] = usage_by_role.get(role, 0) + 1
                field_usage = item["usage"]
                field_usage["by_role"][role] = int(field_usage["by_role"].get(role) or 0) + 1
                reference = {
                    "file": rel,
                    "line": lineno,
                    "field": field,
                    "canonical_field": canonical,
                    "usage_role": role,
                    "usage_reason": str(usage.get("reason") or ""),
                    "scope": current_scope,
                    "text": _match_context(line),
                }
                reference_by_file[rel] += 1
                if bool(usage.get("production_blocker")):
                    field_usage["production_blocking_reference_count"] += 1
                    production_blocking_references.append(reference)
                    usage_blockers.append(reference)
                    production_blocking_reference_by_file[rel] += 1
                if bool(usage.get("ui_primary_blocker")):
                    field_usage["ui_primary_blocking_reference_count"] += 1
                    ui_primary_blocking_references.append(reference)
                    usage_blockers.append(reference)
                    ui_primary_blocking_reference_by_file[rel] += 1
                if bool(usage.get("warning")):
                    field_usage["warning_reference_count"] += 1
                    warning_references.append(reference)
                    warning_reference_by_file[rel] += 1
                if bool(usage.get("allowed")):
                    field_usage["allowed_reference_count"] += 1
                    allowed_references.append(reference)
                if role == USAGE_ROLE_UNKNOWN:
                    field_usage["unknown_usage_reference_count"] += 1
                    unknown_usage_references.append(reference)
                    unknown_usage_reference_by_file[rel] += 1
                if len(item["samples"]) < max_samples:
                    item["samples"].append(
                        {
                            "file": rel,
                            "line": lineno,
                            "field": field,
                            "usage_role": role,
                            "usage_reason": str(usage.get("reason") or ""),
                            "scope": current_scope,
                            "text": _match_context(line),
                        }
                    )
                else:
                    item["truncated"] = True

    fields = sorted(by_field.values(), key=lambda row: row["field"])
    by_status: dict[str, int] = {}
    by_target_layer: dict[str, int] = {}
    unclassified_fields: list[str] = []
    production_allowed_fields: list[str] = []
    ui_primary_allowed_fields: list[str] = []
    production_blocking_fields: list[str] = []
    ui_primary_blocking_fields: list[str] = []
    for row in fields:
        classification = row["classification"]
        status = str(classification.get("status") or "")
        target_layer = str(classification.get("target_layer") or "")
        by_status[status] = by_status.get(status, 0) + 1
        by_target_layer[target_layer] = by_target_layer.get(target_layer, 0) + 1
        if not bool(classification.get("known")):
            unclassified_fields.append(str(row["field"]))
        if bool(classification.get("production_allowed")):
            production_allowed_fields.append(str(row["field"]))
        if bool(classification.get("ui_primary_allowed")):
            ui_primary_allowed_fields.append(str(row["field"]))
        if int((row.get("usage") or {}).get("production_blocking_reference_count") or 0) > 0:
            production_blocking_fields.append(str(row["field"]))
        if int((row.get("usage") or {}).get("ui_primary_blocking_reference_count") or 0) > 0:
            ui_primary_blocking_fields.append(str(row["field"]))

    usage_gate = {
        "schema": "legacy_evidence_usage_gate_v1",
        "ok": not production_blocking_references and not ui_primary_blocking_references and not unknown_usage_references,
        "policy": {
            "production_probability_runtime": "forbidden_until_migrated_to_hierarchical_layers",
            "ui_primary_surface": "forbidden_until_marked_retired_compat_or_replaced_by_layer_contributions",
            "production_compat_surface": "allowed_temporarily_as_details_only_not_decision_gate",
            "ui_retired_compat_surface": "allowed_temporarily_when_marked_retired_compat_not_primary",
            "diagnostic_or_backtest": "allowed_read_only",
            "migration_governance": "allowed",
        },
        "by_usage_role": dict(sorted(usage_by_role.items())),
        "production_blocking_reference_count": len(production_blocking_references),
        "ui_primary_blocking_reference_count": len(ui_primary_blocking_references),
        "warning_reference_count": len(warning_references),
        "allowed_reference_count": len(allowed_references),
        "unknown_usage_reference_count": len(unknown_usage_references),
        "reference_by_file": dict(sorted(reference_by_file.items())),
        "warning_reference_by_file": dict(sorted(warning_reference_by_file.items())),
        "production_blocking_reference_by_file": dict(sorted(production_blocking_reference_by_file.items())),
        "ui_primary_blocking_reference_by_file": dict(sorted(ui_primary_blocking_reference_by_file.items())),
        "unknown_usage_reference_by_file": dict(sorted(unknown_usage_reference_by_file.items())),
        "production_blocking_fields": sorted(set(production_blocking_fields)),
        "ui_primary_blocking_fields": sorted(set(ui_primary_blocking_fields)),
        "production_blocking_files": sorted({item["file"] for item in production_blocking_references}),
        "ui_primary_blocking_files": sorted({item["file"] for item in ui_primary_blocking_references}),
        "warning_files": sorted({item["file"] for item in warning_references}),
        "unknown_usage_files": sorted({item["file"] for item in unknown_usage_references}),
        "blocker_samples": usage_blockers[:max_samples],
        "warning_samples": warning_references[:max_samples],
        "unknown_usage_samples": unknown_usage_references[:max_samples],
    }
    next_actions: list[str] = []
    if unclassified_fields:
        next_actions.append("register_or_remove_unclassified_legacy_evidence_fields")
    if production_allowed_fields:
        next_actions.append("set_legacy_evidence_fields_production_allowed_false")
    if ui_primary_allowed_fields:
        next_actions.append("set_legacy_evidence_fields_ui_primary_allowed_false")
    if production_blocking_references:
        next_actions.append("migrate_production_probability_runtime_to_hierarchical_layers")
    if ui_primary_blocking_references:
        next_actions.append("replace_ui_primary_legacy_evidence_with_layer_contributions_or_retired_compat")
    if unknown_usage_references:
        next_actions.append("classify_unknown_legacy_evidence_usage_scopes")
    warning_files = {str(item["file"]) for item in warning_references}
    if "legacy_additive_bayesian_probability_runtime.py" in warning_files:
        next_actions.append("retire_or_delete_legacy_additive_compat_module_after_report_migration")
    remaining_warning_files = sorted(warning_files - {"legacy_additive_bayesian_probability_runtime.py"})
    if remaining_warning_files:
        next_actions.append("mark_remaining_legacy_warning_files_retired_compat_or_migrate_to_layers")

    return {
        "schema": SCHEMA_VERSION,
        "ok": (
            not unclassified_fields
            and not production_allowed_fields
            and not ui_primary_allowed_fields
            and bool(usage_gate["ok"])
        ),
        "read_only": True,
        "root": str(repo_root),
        "registry_schema": REGISTRY_SCHEMA_VERSION,
        "scan": {
            "files_scanned": files_scanned,
            "bytes_scanned": bytes_scanned,
            "file_errors": file_errors,
            "excluded_dirs": sorted(EXCLUDE_DIRS),
            "include_tests": bool(include_tests),
            "suffixes": sorted(SCAN_SUFFIXES),
            "max_samples_per_field": max_samples,
        },
        "registry_summary": legacy_evidence_registry_summary(),
        "field_reference_count": sum(int(row["reference_count"]) for row in fields),
        "classified_field_count": len(fields),
        "by_status": dict(sorted(by_status.items())),
        "by_target_layer": dict(sorted(by_target_layer.items())),
        "unclassified_fields": sorted(set(unclassified_fields)),
        "production_allowed_fields": sorted(set(production_allowed_fields)),
        "ui_primary_allowed_fields": sorted(set(ui_primary_allowed_fields)),
        "usage_gate": usage_gate,
        "fields": fields,
        "registry_rows": registry_rows(),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
        "next_actions": next_actions,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only audit for legacy additive evidence migration.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--include-tests", action="store_true")
    parser.add_argument("--max-samples-per-field", type=int, default=20)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_legacy_evidence_migration_audit(
        Path(args.root),
        include_tests=bool(args.include_tests),
        max_samples_per_field=max(0, int(args.max_samples_per_field or 0)),
    )
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "root": report["root"],
            "registry_schema": report["registry_schema"],
            "scan": report["scan"],
            "registry_summary": report["registry_summary"],
            "field_reference_count": report["field_reference_count"],
            "classified_field_count": report["classified_field_count"],
            "by_status": report["by_status"],
            "by_target_layer": report["by_target_layer"],
            "unclassified_fields": report["unclassified_fields"],
            "production_allowed_fields": report["production_allowed_fields"],
            "ui_primary_allowed_fields": report["ui_primary_allowed_fields"],
            "usage_gate": report["usage_gate"],
            "guards": report["guards"],
            "next_actions": report["next_actions"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
