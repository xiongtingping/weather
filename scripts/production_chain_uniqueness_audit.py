#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[1]
for import_path in (SCRIPT_DIR, ROOT):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import fact_write_migration_plan
import document_authority_audit
import legacy_additive_test_contract_audit
import phase0_schema_probe
import phase0_ssot_inventory
from production_probability_v6_activation_contract import (
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    SCHEMA_VERSION as V6_ACTIVATION_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_runtime_manifest import (
    audit_v6_runtime_manifest_dependency_closure,
    v6_runtime_manifest_sha256,
)
from bayes_basket_target.activation import load_target_activation


SCHEMA_VERSION = "production_chain_uniqueness_audit_v1"
SOURCE_FACT_TARGET_OWNER = "weather_fact_snapshot_writer.py"
FACT_TABLES = ("event_fact_history", "event_fact_latest", "event_fact_summary_cache")
READONLY_WATCHER_BOUNDARY_FILES = (
    "scripts/auto_trade_watcher.py",
    "scripts/live_signal_order_watch.py",
    "launchd/com.weather.auto_trade_watcher.plist",
)
ORDER_SUBMITTER_BOUNDARY_FILES = (
    "scripts/clob_order_service.mjs",
    "scripts/clob_order_service.sh",
    "scripts/polymarket_order.mjs",
    "scripts/update_order_status.py",
    "launchd/com.weather.clob_order_service.plist",
)
TRADE_ENGINE_BOUNDARY_FILES = (
    "scripts/trade_engine_loop.sh",
    "launchd/com.weather.trade_engine.plist",
)
READONLY_WATCHER_FORBIDDEN_PATTERNS = (
    ("production_scanner_import", r"\b(?:from\s+scan_weather_market\s+import|import\s+scan_weather_market)\b|scan_weather_market\.py"),
    ("source_acquisition_import", r"\bweather_source_acquisition_manager\b|\bsource_payloads_from_refresh_result\b"),
    ("external_weather_fetch", r"\brequests\.(?:get|post)\b|\bhttpx\.|\baiohttp\b|\burllib\.request\b|\baxios\b|\bcurl\s+"),
    (
        "posterior_recompute",
        r"\bapply_bayesian_probability_update\b|\bbuild_hierarchical_posterior_pipeline\b|"
        r"\bbuild_hierarchical_temperature_posterior\b|\bbuild_sequential_remaining_warming_posterior\b|"
        r"\bbuild_forecast_weak_likelihood\b|\bbuild_metar_strong_likelihood\b",
    ),
    (
        "fact_write_surface",
        r"\bweather_fact_snapshot_writer\b|\bbuild_fact_snapshot_write_plan\b|\bapply_fact_snapshot_write_plan\b|"
        r"\bINSERT\s+INTO\s+event_fact_|\bUPDATE\s+event_fact_",
    ),
)
ORDER_SUBMITTER_FORBIDDEN_PATTERNS = (
    ("production_scanner_import", r"\b(?:from\s+scan_weather_market\s+import|import\s+scan_weather_market)\b|scan_weather_market\.py"),
    ("source_acquisition_import", r"\bweather_source_acquisition_manager\b|\bsource_payloads_from_refresh_result\b"),
    (
        "posterior_recompute",
        r"\bapply_bayesian_probability_update\b|\bbuild_hierarchical_posterior_pipeline\b|"
        r"\bbuild_hierarchical_temperature_posterior\b|\bbuild_sequential_remaining_warming_posterior\b|"
        r"\bbuild_forecast_weak_likelihood\b|\bbuild_metar_strong_likelihood\b",
    ),
    (
        "weather_fact_write_surface",
        r"\bweather_fact_snapshot_writer\b|\bbuild_fact_snapshot_write_plan\b|\bapply_fact_snapshot_write_plan\b|"
        r"\bevent_fact_history\b|\bevent_fact_latest\b|\bevent_fact_summary_cache\b",
    ),
)
TRADE_ENGINE_FORBIDDEN_PATTERNS = (
    ("source_acquisition_import", r"\bweather_source_acquisition_manager\b|\bsource_payloads_from_refresh_result\b"),
    ("external_weather_fetch", r"\brequests\.(?:get|post)\b|\bhttpx\.|\baiohttp\b|\burllib\.request\b|\baxios\b|\bcurl\s+"),
    (
        "posterior_recompute",
        r"\bapply_bayesian_probability_update\b|\bbuild_hierarchical_posterior_pipeline\b|"
        r"\bbuild_hierarchical_temperature_posterior\b|\bbuild_sequential_remaining_warming_posterior\b|"
        r"\bbuild_forecast_weak_likelihood\b|\bbuild_metar_strong_likelihood\b",
    ),
    (
        "weather_fact_write_surface",
        r"\bweather_fact_snapshot_writer\b|\bbuild_fact_snapshot_write_plan\b|\bapply_fact_snapshot_write_plan\b|"
        r"\bINSERT\s+INTO\s+event_fact_|\bUPDATE\s+event_fact_",
    ),
    (
        "weather_scan_or_enqueue_mode",
        r"\b--scan-weather-today\b|\b--auto-trade-enqueue\b|\b--auto-trade-once\b|\b--refresh-awc\b|\b--scan-weather\b",
    ),
)
TRAJECTORY_BREAKOUT_BOUNDARY_FILES = (
    "scan_weather_market.py",
    "scripts/hard_source_direct_poller.py",
    "chengdu_intraday_summary.py",
    "ui/server.py",
    "ui/index.html",
)
TRAJECTORY_BREAKOUT_FORBIDDEN_PATTERNS = (
    (
        "posterior_target_direct_path_follow_not_diagnostic",
        r"posterior_target_direct_path_follow_diagnostic_only[\"'\]\)\s]*(?:=|:)\s*False\b",
    ),
    (
        "posterior_target_direct_path_follow_flag_initialized_false",
        r"posterior_target_path_diagnostic_only\s*=\s*False\b",
    ),
    (
        "trajectory_candidate_applied_to_target_true",
        r"trajectory_candidate_applied_to_target[\"'\]\)\s]*(?:=|:)\s*True\b",
    ),
)


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "executes_sql": False,
    }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _blocker(blocker: str, *, severity: str = "high", details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "blocker": blocker,
        "severity": severity,
        "details": dict(details or {}),
    }


def _warning(warning: str, *, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"warning": warning, "details": dict(details or {})}


def _lane_by_id(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(row.get("lane") or ""): row for row in plan.get("lanes") or [] if isinstance(row, Mapping)}


def _entrypoint_by_id(inventory: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(row.get("id") or ""): row
        for row in inventory.get("entrypoint_inventory") or []
        if isinstance(row, Mapping)
    }


def _probability_runtime_identity_gate(root: Path) -> dict[str, Any]:
    # The target basket lane is the current production decision authority.  A
    # legacy v6 probability activation may still exist as an offline release
    # receipt, but it must not remain a production gate once the resident
    # target route is explicitly enabled in production_no_live mode.  This
    # branch intentionally performs no activation/database mutation.
    target_activation_path = root / "config" / "trading" / "bayes_basket_target_production_no_live_activation_v1.json"
    if target_activation_path.exists():
        try:
            from production_probability_v6_live_route_contract import audit_v6_live_routes

            target_activation = load_target_activation(target_activation_path)
            target_blockers: list[dict[str, Any]] = []
            if target_activation.get("state") not in {"enabled", "production_no_live"}:
                target_blockers.append(_blocker("target_basket_activation_not_enabled"))
            if target_activation.get("execution_mode") != "production_no_live":
                target_blockers.append(_blocker("target_basket_activation_not_no_live"))
            if target_activation.get("live_submit_authorized") is not False:
                target_blockers.append(_blocker("target_basket_live_submit_authorized"))
            if target_activation.get("places_orders") is not False:
                target_blockers.append(_blocker("target_basket_places_orders_enabled"))
            if target_activation.get("calls_order_gateway") is not False:
                target_blockers.append(_blocker("target_basket_gateway_call_enabled"))
            route = audit_v6_live_routes(root)
            target_blockers.extend(
                _blocker(str(item)) for item in route.get("blockers") or []
            )
            return {
                "ok": not target_blockers,
                "blockers": target_blockers,
                "warnings": [],
                "activation_schema": target_activation.get("schema"),
                "target_activation_contract_sha256": target_activation.get("activation_contract_sha256"),
                "resident_route": route,
                "legacy_probability_activation_not_authoritative": True,
                "legacy_v6_manifest_checked": False,
                "read_only": True,
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "blockers": [_blocker("target_production_identity_invalid", details={"error": str(exc)})],
                "warnings": [],
                "read_only": True,
            }
    path = root / "config" / "production_probability_runtime_activation.json"
    if not path.exists():
        return {
            "ok": True,
            "blockers": [],
            "warnings": [],
            "not_checked_reason": "production_activation_missing_in_fixture_root",
        }
    blockers: list[dict[str, Any]] = []
    try:
        activation = validate_v6_activation(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "blockers": [
                _blocker(
                    "production_v6_activation_invalid",
                    details={"error": str(exc)},
                )
            ],
            "warnings": [],
        }
    current_manifest = v6_runtime_manifest_sha256(root)
    closure = audit_v6_runtime_manifest_dependency_closure(root)
    if str(activation.get("schema") or "") != V6_ACTIVATION_SCHEMA:
        blockers.append(_blocker("production_probability_activation_not_v6"))
    if str(activation.get("runtime_manifest_sha256") or "") != current_manifest:
        blockers.append(
            _blocker(
                "production_v6_runtime_manifest_drift",
                details={
                    "authorized": activation.get("runtime_manifest_sha256"),
                    "current": current_manifest,
                },
            )
        )
    if closure.get("ok") is not True:
        blockers.append(
            _blocker(
                "production_v6_runtime_dependency_closure_incomplete",
                details=dict(closure),
            )
        )
    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": [],
        "activation_schema": activation.get("schema"),
        "bayes_formula_id": activation.get("bayes_formula_id"),
        "composer_schema": activation.get("composer_schema"),
        "checkpoint_schema": activation.get("checkpoint_schema"),
        "public_ssot_schema": activation.get("public_ssot_schema"),
        "runtime_manifest_sha256": current_manifest,
        "dependency_closure": closure,
    }


def _table_writer_gate(inventory: Mapping[str, Any], fact_plan: Mapping[str, Any]) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    table_inventory = inventory.get("table_write_inventory") if isinstance(inventory.get("table_write_inventory"), Mapping) else {}
    file_entries = {
        str(row.get("file") or ""): row
        for row in fact_plan.get("file_entries") or []
        if isinstance(row, Mapping)
    }

    def is_allowed_non_live_surface(path: str) -> bool:
        entry = file_entries.get(path) or {}
        lane = str(entry.get("lane") or "")
        status = str(entry.get("status") or "")
        return (
            lane in {"controlled_identity_repair", "read_only_contract_surface", "isolated_shadow_ssot_writer", "production_transaction_b_cutover_candidate", "retired_probability_writer_surface"}
            and status
            in {
                "controlled_identity_repair_apply_boundary_present",
                "read_only_contract_surface_no_apply_boundary_present",
                "isolated_shadow_ssot_writer_manifest_boundary_present",
                "production_transaction_b_cutover_candidate_default_disabled",
                "production_probability_ssot_writer_partition_active",
                "retired_probability_writer_unreachable_from_active_epoch",
            }
        )

    table_states: dict[str, Any] = {}
    for table in FACT_TABLES:
        row = table_inventory.get(table) if isinstance(table_inventory.get(table), Mapping) else {}
        writer_files = [str(item or "") for item in row.get("writer_files") or [] if str(item or "")]
        non_live = [str(item or "") for item in row.get("non_live_writer_files") or [] if str(item or "")]
        unexpected = [
            item
            for item in writer_files
            if item != SOURCE_FACT_TARGET_OWNER and not is_allowed_non_live_surface(item)
        ]
        table_states[table] = {
            "writer_files": writer_files,
            "non_live_writer_files": non_live,
            "allowed_controlled_non_live_writer_files": [
                item for item in writer_files if item != SOURCE_FACT_TARGET_OWNER and is_allowed_non_live_surface(item)
            ],
            "unexpected_writer_files": unexpected,
        }
        if unexpected:
            blockers.append(
                _blocker(
                    "fact_table_live_writer_not_canonical",
                    details={"table": table, "unexpected_writer_files": unexpected},
                )
            )

    lanes = _lane_by_id(fact_plan)
    unknown_lane = lanes.get("unknown_fact_writer_surface") or {}
    unknown_files = [str(item or "") for item in unknown_lane.get("files") or [] if str(item or "")]
    if unknown_files:
        blockers.append(_blocker("unknown_fact_writer_surface_present", details={"files": unknown_files}))
    pending_count = int(fact_plan.get("pending_migration_file_count") or 0)
    if pending_count:
        blockers.append(
            _blocker(
                "fact_writer_migration_pending",
                details={"pending_migration_file_count": pending_count, "next_actions": fact_plan.get("next_actions") or []},
            )
        )

    if any(row.get("gaps") for row in inventory.get("ssot_matrix") or [] if isinstance(row, Mapping) and row.get("layer") == "fact_snapshot"):
        warnings.append(_warning("fact_snapshot_helper_surface_still_spans_multiple_files_but_live_writers_are_canonical"))

    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "tables": table_states,
        "fact_write_plan": {
            "pending_migration_file_count": pending_count,
            "unknown_files": unknown_files,
            "next_actions": fact_plan.get("next_actions") or [],
            "target_owner": fact_plan.get("target_owner"),
        },
    }


def _legacy_evidence_gate(
    inventory: Mapping[str, Any],
    *,
    legacy_test_report: Mapping[str, Any],
) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    gate = inventory.get("legacy_evidence_usage_gate") if isinstance(inventory.get("legacy_evidence_usage_gate"), Mapping) else {}
    usage_gate = gate.get("usage_gate") if isinstance(gate.get("usage_gate"), Mapping) else {}
    if gate.get("checked") is not True:
        blockers.append(_blocker("legacy_evidence_usage_gate_not_checked"))
    if gate.get("ok") is not True or usage_gate.get("ok") is not True:
        blockers.append(
            _blocker(
                "legacy_additive_evidence_usage_gate_blocked",
                details={
                    "production_blocking_files": usage_gate.get("production_blocking_files") or [],
                    "ui_primary_blocking_files": usage_gate.get("ui_primary_blocking_files") or [],
                    "unknown_usage_files": usage_gate.get("unknown_usage_files") or [],
                },
            )
        )
    if legacy_test_report.get("ok") is not True:
        blockers.append(
            _blocker(
                "legacy_additive_test_contract_blocked",
                details={"blocking_test_count": legacy_test_report.get("blocking_test_count")},
            )
        )
    return {
        "ok": not blockers,
        "blockers": blockers,
        "field_reference_count": gate.get("field_reference_count"),
        "usage_gate": usage_gate,
        "legacy_test_contract": {
            "ok": bool(legacy_test_report.get("ok")),
            "blocking_test_count": legacy_test_report.get("blocking_test_count"),
        },
    }


def _ui_gate(inventory: Mapping[str, Any]) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    gate = inventory.get("ui_local_derivation_gate") if isinstance(inventory.get("ui_local_derivation_gate"), Mapping) else {}
    if gate.get("ok") is not True:
        blockers.append(
            _blocker(
                "ui_frontend_recomputes_bayes_or_evidence",
                details={
                    "blocking_files": gate.get("blocking_files") or [],
                    "blocking_reference_count": gate.get("blocking_reference_count"),
                    "blocker_samples": gate.get("blocker_samples") or [],
                },
            )
        )
    return {
        "ok": not blockers,
        "blockers": blockers,
        "allowed_reference_count": gate.get("allowed_reference_count"),
        "blocking_reference_count": gate.get("blocking_reference_count"),
        "allowed_symbols": gate.get("allowed_symbols") or [],
    }


def _ui_read_path_boundary_gate(root: Path) -> dict[str, Any]:
    path = root / "ui" / "server.py"
    if not path.exists():
        return {
            "ok": True,
            "blockers": [],
            "not_checked_reason": "ui_server_missing_in_fixture_root",
        }
    text = _read_text(path)
    start = text.find("def auto_trade_trace(")
    end = text.find("\ndef compute_global_order_list", start + 1) if start >= 0 else -1
    trace_block = text[start:end] if start >= 0 and end > start else ""
    refresh_start = trace_block.find("    def _refresh_async(")
    refresh_end = trace_block.find("\n    now_ts = time.time()", refresh_start + 1)
    refresh_block = (
        trace_block[refresh_start:refresh_end]
        if refresh_start >= 0 and refresh_end > refresh_start
        else ""
    )
    violations: list[str] = []
    if "_auto_trade_cleanup_stale_runs(" in trace_block:
        violations.append("ui_trace_repairs_execution_state")
    if not refresh_block:
        violations.append("auto_trade_trace_async_refresh_boundary_missing")
    else:
        if "_trace_refresh_market_ticks_for_blocked(" in refresh_block:
            violations.append("ui_trace_refreshes_market_ticks")
        if "start_auto_trade_run(" in refresh_block:
            violations.append("ui_trace_starts_auto_trade_run")
        if '"na_reason": "ui_trace_read_only_no_market_refresh"' not in refresh_block:
            violations.append("ui_trace_read_only_boundary_marker_missing")
    blockers = (
        [
            _blocker(
                "ui_read_path_has_archive_or_execution_side_effect",
                details={"file": "ui/server.py", "violations": violations},
            )
        ]
        if violations
        else []
    )
    return {
        "ok": not blockers,
        "blockers": blockers,
        "checked_file": "ui/server.py",
        "violations": violations,
        "boundary": "ui_trace_and_snapshot_reads_do_not_refresh_market_or_start_execution",
    }


def _posterior_entrypoint_gate(root: Path, inventory: Mapping[str, Any]) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    activation_text = _read_text(root / "production_probability_v6_activation_contract.py")
    ingress_text = _read_text(root / "production_probability_v6_amsc_archive_ingress.py")
    drain_text = _read_text(root / "production_probability_v6_canonical_event_drain_runtime.py")
    weak_revision_text = _read_text(root / "production_probability_v6_weak_revision_event_runtime.py")
    public_writer_text = _read_text(root / "production_probability_v6_public_ssot_writer.py")
    event_text = _read_text(root / "production_probability_v6_event_runtime.py")
    candidate_text = _read_text(root / "production_probability_v6_candidate_compiler.py")
    core_text = _read_text(root / "physical_state_bayes_v6" / "core.py")
    base_text_by_file = {
        "production_probability_v6_activation_contract.py": activation_text,
        "production_probability_v6_amsc_archive_ingress.py": ingress_text,
        "production_probability_v6_canonical_event_drain_runtime.py": drain_text,
        "production_probability_v6_weak_revision_event_runtime.py": weak_revision_text,
        "production_probability_v6_public_ssot_writer.py": public_writer_text,
        "production_probability_v6_event_runtime.py": event_text,
        "production_probability_v6_candidate_compiler.py": candidate_text,
        "physical_state_bayes_v6/core.py": core_text,
    }
    physical_composer = False
    try:
        active_activation = json.loads(
            (root / "config/production_probability_runtime_activation.json").read_text(
                encoding="utf-8"
            )
        )
        physical_composer = (
            str(active_activation.get("composer_schema") or "")
            == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass

    if physical_composer:
        physical_composer_text = _read_text(
            root / "production_probability_physical_state_composer.py"
        )
        physical_core_text = _read_text(
            root / "identifiable_physical_state_bayes" / "core.py"
        )
        text_by_file = {
            **base_text_by_file,
            "production_probability_physical_state_composer.py": physical_composer_text,
            "identifiable_physical_state_bayes/core.py": physical_core_text,
        }
        required_by_file = {
            "production_probability_v6_activation_contract.py": (
                "IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA",
                "IDENTIFIABLE_PHYSICAL_FORMULA_ID",
                "physical_state_vector",
                "frozen_reporting_operator_enabled",
            ),
            "production_probability_v6_amsc_archive_ingress.py": (
                "apply_v6_amsc_archive_probability_ingress",
                "drain_v6_canonical_events",
                "prewarm_v6_market_inputs",
                '"canonical_fact_commits_before_probability": True',
                '"v5_fallback_forbidden": True',
            ),
            "production_probability_v6_canonical_event_drain_runtime.py": (
                "pending_v6_canonical_event_ids",
                "commit_v6_candidate_transaction",
                "IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA",
            ),
            "production_probability_v6_weak_revision_event_runtime.py": (
                "apply_v6_taf_revision_from_source_observations",
                "commit_v6_weak_revision_transaction",
            ),
            "production_probability_v6_public_ssot_writer.py": (
                "commit_v6_public_physical_transaction",
                "validate_v6_activation",
                "BEGIN IMMEDIATE",
            ),
            "production_probability_v6_event_runtime.py": (
                "compose_physical_canonical_event_extension",
                "build_identifiable_physical_transaction_b_candidate",
                "IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA",
            ),
            "production_probability_v6_candidate_compiler.py": (
                "build_identifiable_physical_transaction_b_candidate",
                "physical_candidate_reported_x_hard_bound_violation",
                "no_post_emission_transition",
            ),
            "production_probability_physical_state_composer.py": (
                "compose_physical_canonical_event_extension",
                "predict_to_event",
                "build_joint_observation_likelihood",
                "condition_pre_event_state",
            ),
            "identifiable_physical_state_bayes/core.py": (
                "def predict_to_event(",
                "def condition_pre_event_state(",
                "no_post_emission_transition",
            ),
        }
        text_by_file.update(
            {
                "production_probability_v6_activation_contract.py": activation_text,
                "production_probability_v6_amsc_archive_ingress.py": ingress_text,
                "production_probability_v6_canonical_event_drain_runtime.py": drain_text,
                "production_probability_v6_weak_revision_event_runtime.py": weak_revision_text,
                "production_probability_v6_public_ssot_writer.py": public_writer_text,
                "production_probability_v6_event_runtime.py": event_text,
                "production_probability_v6_candidate_compiler.py": candidate_text,
            }
        )
    else:
        required_by_file = {
            "production_probability_v6_activation_contract.py": (
                "p0_weak_conditioned_continuous_y_x_8_state_then_pre_event_controlled_predict_",
                'COMPOSER_SCHEMA = "physical_state_bayes_v6_pre_event_predict_update"',
                'PUBLIC_SSOT_SCHEMA = "sequential_temperature_posterior_ssot_v4_reported_x"',
                'CHECKPOINT_SCHEMA = "posterior_checkpoint_v4_physical_state_bayes_v6"',
            ),
            "production_probability_v6_amsc_archive_ingress.py": (
                "apply_v6_amsc_archive_probability_ingress",
                "drain_v6_canonical_events",
                '"canonical_fact_commits_before_probability": True',
                '"v5_fallback_forbidden": True',
            ),
            "production_probability_v6_canonical_event_drain_runtime.py": (
                "pending_v6_canonical_event_ids",
                "commit_v6_public_physical_transaction",
            ),
            "production_probability_v6_weak_revision_event_runtime.py": (
                "apply_v6_taf_revision_from_source_observations",
                "commit_v6_weak_revision_transaction",
            ),
            "production_probability_v6_public_ssot_writer.py": (
                "commit_v6_public_physical_transaction",
                "validate_v6_activation",
                "BEGIN IMMEDIATE",
            ),
            "production_probability_v6_event_runtime.py": (
                "predict_to_event",
                "build_joint_observation_emission",
                "build_v6_transaction_b_candidate",
            ),
            "production_probability_v6_candidate_compiler.py": (
                "update_with_observation",
                "project_reported_x",
                '"no_post_emission_state_transition": True',
            ),
            "physical_state_bayes_v6/core.py": (
                "def predict_to_event(",
                "def update_with_observation(",
                '"no_post_emission_state_transition": True',
            ),
        }
        text_by_file = base_text_by_file
    missing_contract: dict[str, list[str]] = {}
    for filename, required_tokens in required_by_file.items():
        missing = [token for token in required_tokens if token not in text_by_file[filename]]
        if missing:
            missing_contract[filename] = missing
    if missing_contract:
        blockers.append(
            _blocker(
                "production_v6_mainline_contract_missing",
                details={"missing_by_file": missing_contract},
            )
        )

    if (root / "bayesian_probability_runtime.py").exists():
        blockers.append(_blocker("retired_bayesian_probability_runtime_file_present"))
    if (root / "legacy_additive_bayesian_probability_runtime.py").exists():
        blockers.append(_blocker("legacy_additive_runtime_file_present"))
    if (root / "hierarchical_temperature_posterior_runtime.py").exists():
        blockers.append(_blocker("retired_bucket_product_runtime_file_present"))
    if (root / "ncei_zuuu_synoptic_v3_runtime.py").exists():
        blockers.append(_blocker("retired_v3_probability_runtime_file_present"))
    forbidden_successor_tokens = (
        "production_reported_x_event_runtime",
        "production_sequential_bayes_event_runtime",
        "sequential_bayes_filter_shadow_runtime",
        "posterior_pipeline_entry_runtime",
        "build_hierarchical_posterior_pipeline",
        "build_sequential_remaining_warming_posterior",
    )
    fallback_hits = [
        token
        for token in forbidden_successor_tokens
        if token in ingress_text or token in drain_text or token in weak_revision_text or token in public_writer_text
    ]
    if fallback_hits:
        blockers.append(
            _blocker(
                "retired_probability_model_fallback_present",
                details={"tokens": fallback_hits},
            )
        )

    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": [],
        "mainline_contract_complete": not missing_contract,
        "active_composer": (
            IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA if physical_composer else "legacy_v6"
        ),
        "retired_successor_imports_absent": not fallback_hits,
        "required_by_file": {key: list(value) for key, value in required_by_file.items()},
    }


def _monitor_signal_gate(root: Path) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    launchd_dir = root / "launchd"
    weather_plist = launchd_dir / "com.weather.monitor.daemon.plist"
    weather_plist_text = _read_text(weather_plist)
    if weather_plist.exists() and "com.weather.monitor.daemon" not in weather_plist_text:
        blockers.append(_blocker("weather_monitor_launchd_label_missing", details={"file": str(weather_plist)}))

    forbidden_samples: list[dict[str, Any]] = []
    scan_files = list(launchd_dir.glob("*.plist")) if launchd_dir.exists() else []
    for rel in ("scripts/install_monitor_launchdaemon.sh", "scripts/uninstall_launchdaemon.sh", "scripts/install_launchdaemon.sh"):
        path = root / rel
        if path.exists():
            scan_files.append(path)
    for path in scan_files:
        text = _read_text(path)
        if "com.polymarket.monitor.daemon" in text:
            forbidden_samples.append({"file": str(path.relative_to(root)), "token": "com.polymarket.monitor.daemon"})
    if forbidden_samples:
        blockers.append(_blocker("weather_repo_references_forbidden_polymarket_monitor_daemon", details={"samples": forbidden_samples}))

    health_text = _read_text(root / "scripts" / "check_launchd_weather.py")
    required_tokens = (
        "MONITOR_SIGNAL_LOG",
        "POLY_SIGNAL_PATH",
        "monitor_signal_log_equals_poly_signal_path",
        "monitor_signal_log_pointing_to_poly_signal_path",
    )
    missing_tokens = [token for token in required_tokens if token not in health_text]
    if missing_tokens:
        blockers.append(_blocker("monitor_signal_log_boundary_guard_missing", details={"missing": missing_tokens}))

    env_assignment_pattern = re.compile(r"MONITOR_SIGNAL_LOG\s*=.*POLY_SIGNAL_PATH|POLY_SIGNAL_PATH\s*=.*MONITOR_SIGNAL_LOG")
    assignment_samples: list[dict[str, Any]] = []
    for rel in ("scripts/install_monitor_launchdaemon.sh", "launchd/com.weather.monitor.daemon.plist"):
        path = root / rel
        text = _read_text(path)
        for idx, line in enumerate(text.splitlines(), start=1):
            if env_assignment_pattern.search(line):
                assignment_samples.append({"file": rel, "line": idx, "text": line.strip()})
    if assignment_samples:
        blockers.append(_blocker("monitor_signal_log_points_to_poly_signal_path_static_assignment", details={"samples": assignment_samples}))

    retired_resolution_capture_plists = (
        "launchd/com.weather.target_rule_shadow_capture.plist",
        "launchd/com.weather.market_finality_shadow_capture.plist",
        "launchd/com.weather.physical_final_source_shadow_capture.plist",
        "launchd/com.weather.settlement_final_source_shadow_capture.plist",
    )
    present_retired_plists = [rel for rel in retired_resolution_capture_plists if (root / rel).exists()]
    if present_retired_plists:
        blockers.append(
            _blocker(
                "retired_resolution_shadow_capture_launchdaemon_present",
                details={"files": present_retired_plists},
            )
        )
    monitor_script = root / "scripts" / "polymarket_monitor_daemon.sh"
    installer = root / "scripts" / "install_monitor_launchdaemon.sh"
    required_capture_tokens = (
        "start_resolution_final_shadow_capture",
        "capture_target_rule_shadow.py",
        "capture_market_finality_shadow.py",
        "capture_physical_final_source_shadow.py",
        "capture_settlement_final_source_shadow.py",
    )
    if monitor_script.exists():
        monitor_text = _read_text(monitor_script)
        missing_capture_tokens = [token for token in required_capture_tokens if token not in monitor_text]
        if missing_capture_tokens:
            blockers.append(
                _blocker(
                    "monitor_resolution_final_shadow_capture_contract_missing",
                    details={"missing": missing_capture_tokens},
                )
            )
    required_retirement_tokens = (
        "Library/LaunchAgents",
        "gui/${UID_VALUE}",
        "monitor_user_domain_migration.enabled",
    )
    if installer.exists():
        installer_text = _read_text(installer)
        missing_retirement_tokens = [token for token in required_retirement_tokens if token not in installer_text]
        if missing_retirement_tokens:
            blockers.append(
                _blocker(
                    "monitor_user_domain_single_owner_contract_missing",
                    details={"missing": missing_retirement_tokens},
                )
            )
    if not weather_plist.exists():
        warnings.append(_warning("weather_monitor_launchd_plist_missing_in_scanned_root"))

    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checked_files": [str(path.relative_to(root)) for path in scan_files if path.exists()],
    }


def _pattern_samples(
    root: Path,
    *,
    rel_files: Sequence[str],
    forbidden_patterns: Sequence[tuple[str, str]],
    max_samples: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    samples: list[dict[str, Any]] = []
    missing: list[str] = []
    compiled = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in forbidden_patterns]
    for rel in rel_files:
        path = root / rel
        if not path.exists():
            missing.append(rel)
            continue
        text = _read_text(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern_name, pattern in compiled:
                if pattern.search(line):
                    samples.append(
                        {
                            "file": rel,
                            "line": line_no,
                            "pattern": pattern_name,
                            "text": line.strip()[:240],
                        }
                    )
                    if len(samples) >= max(1, int(max_samples or 1)):
                        return samples, missing
    return samples, missing


def _consumer_boundary_gate(root: Path, *, max_samples_per_pattern: int = 20) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    sample_limit = max(1, int(max_samples_per_pattern or 1))
    watcher_samples, watcher_missing = _pattern_samples(
        root,
        rel_files=READONLY_WATCHER_BOUNDARY_FILES,
        forbidden_patterns=READONLY_WATCHER_FORBIDDEN_PATTERNS,
        max_samples=sample_limit,
    )
    submitter_samples, submitter_missing = _pattern_samples(
        root,
        rel_files=ORDER_SUBMITTER_BOUNDARY_FILES,
        forbidden_patterns=ORDER_SUBMITTER_FORBIDDEN_PATTERNS,
        max_samples=sample_limit,
    )
    trade_engine_samples, trade_engine_missing = _pattern_samples(
        root,
        rel_files=TRADE_ENGINE_BOUNDARY_FILES,
        forbidden_patterns=TRADE_ENGINE_FORBIDDEN_PATTERNS,
        max_samples=sample_limit,
    )
    trade_engine_mode_samples: list[dict[str, Any]] = []
    trade_engine_file = root / "scripts/trade_engine_loop.sh"
    if trade_engine_file.exists():
        text = _read_text(trade_engine_file)
        for line_no, line in enumerate(text.splitlines(), start=1):
            if "scan_weather_market.py" not in line:
                continue
            if "--trade-sync-only" in line or "--auto-trade-job-worker" in line:
                continue
            trade_engine_mode_samples.append(
                {
                    "file": "scripts/trade_engine_loop.sh",
                    "line": line_no,
                    "pattern": "scan_weather_market_invocation_not_sync_or_job_worker",
                    "text": line.strip()[:240],
                }
            )
            if len(trade_engine_mode_samples) >= sample_limit:
                break
    readonly_db_samples: list[dict[str, Any]] = []
    for rel in READONLY_WATCHER_BOUNDARY_FILES:
        path = root / rel
        text = _read_text(path)
        if "sqlite3.connect" in text and "mode=ro" not in text:
            for line_no, line in enumerate(text.splitlines(), start=1):
                if "sqlite3.connect" in line:
                    readonly_db_samples.append(
                        {
                            "file": rel,
                            "line": line_no,
                            "pattern": "watcher_sqlite_connection_not_read_only",
                            "text": line.strip()[:240],
                        }
                    )
                    break
    if watcher_samples:
        blockers.append(
            _blocker(
                "readonly_watcher_reads_external_weather_or_recomputes_posterior",
                details={"samples": watcher_samples},
            )
        )
    if readonly_db_samples:
        blockers.append(
            _blocker(
                "readonly_watcher_db_connection_not_read_only",
                details={"samples": readonly_db_samples},
            )
        )
    if submitter_samples:
        blockers.append(
            _blocker(
                "order_submitter_writes_weather_facts_or_recomputes_posterior",
                details={"samples": submitter_samples},
            )
        )
    if trade_engine_samples or trade_engine_mode_samples:
        blockers.append(
            _blocker(
                "trade_engine_runs_weather_scan_or_recomputes_posterior",
                details={"samples": trade_engine_samples + trade_engine_mode_samples},
            )
        )
    if watcher_missing:
        warnings.append(_warning("readonly_watcher_boundary_file_missing", details={"files": watcher_missing}))
    if submitter_missing:
        warnings.append(_warning("order_submitter_boundary_file_missing", details={"files": submitter_missing}))
    if trade_engine_missing:
        warnings.append(_warning("trade_engine_boundary_file_missing", details={"files": trade_engine_missing}))

    readonly_watcher_ok = not watcher_samples and not readonly_db_samples
    order_submitter_ok = not submitter_samples
    trade_engine_ok = not trade_engine_samples and not trade_engine_mode_samples
    return {
        "ok": readonly_watcher_ok and order_submitter_ok and trade_engine_ok,
        "readonly_watcher_ok": readonly_watcher_ok,
        "order_submitter_ok": order_submitter_ok,
        "trade_engine_ok": trade_engine_ok,
        "blockers": blockers,
        "warnings": warnings,
        "readonly_watcher": {
            "checked_files": [rel for rel in READONLY_WATCHER_BOUNDARY_FILES if (root / rel).exists()],
            "missing_files": watcher_missing,
            "forbidden_sample_count": len(watcher_samples) + len(readonly_db_samples),
            "boundary": "read_logs_or_read_only_fact_snapshot_only_no_external_weather_no_posterior_recompute",
        },
        "order_submitter": {
            "checked_files": [rel for rel in ORDER_SUBMITTER_BOUNDARY_FILES if (root / rel).exists()],
            "missing_files": submitter_missing,
            "forbidden_sample_count": len(submitter_samples),
            "boundary": "order_status_or_clob_submit_only_no_weather_fact_write_no_posterior_recompute",
        },
        "trade_engine": {
            "checked_files": [rel for rel in TRADE_ENGINE_BOUNDARY_FILES if (root / rel).exists()],
            "missing_files": trade_engine_missing,
            "forbidden_sample_count": len(trade_engine_samples) + len(trade_engine_mode_samples),
            "boundary": "trade_sync_or_auto_trade_job_worker_only_no_weather_scan_no_posterior_recompute_no_fact_write",
            "allowed_scan_weather_market_modes": ["--trade-sync-only", "--auto-trade-job-worker"],
        },
    }


def _trajectory_breakout_diagnostic_only_gate(root: Path, *, max_samples_per_pattern: int = 20) -> dict[str, Any]:
    sample_limit = max(1, int(max_samples_per_pattern or 1))
    samples, missing = _pattern_samples(
        root,
        rel_files=TRAJECTORY_BREAKOUT_BOUNDARY_FILES,
        forbidden_patterns=TRAJECTORY_BREAKOUT_FORBIDDEN_PATTERNS,
        max_samples=sample_limit,
    )
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if samples:
        blockers.append(
            _blocker(
                "trajectory_breakout_or_path_follow_not_diagnostic_only",
                details={"samples": samples},
            )
        )
    if missing:
        warnings.append(_warning("trajectory_breakout_boundary_file_missing", details={"files": missing}))
    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checked_files": [rel for rel in TRAJECTORY_BREAKOUT_BOUNDARY_FILES if (root / rel).exists()],
        "missing_files": missing,
        "forbidden_sample_count": len(samples),
        "boundary": (
            "trajectory_projected_bucket_breakout_and_path_follow_may_be_displayed_or_logged_"
            "only_as_diagnostic_not_as_entry_target_or_posterior_source"
        ),
    }


def _collect_blockers(*gates: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for gate in gates:
        out.extend([dict(item) for item in gate.get("blockers") or [] if isinstance(item, Mapping)])
    return out


def _collect_warnings(*gates: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for gate in gates:
        out.extend([dict(item) for item in gate.get("warnings") or [] if isinstance(item, Mapping)])
    return out


def build_production_chain_uniqueness_audit(
    root: Path = ROOT,
    *,
    db_path: Path | None = None,
    immutable: bool = False,
    max_samples_per_pattern: int = 20,
) -> dict[str, Any]:
    repo_root = Path(root).expanduser().resolve()
    inventory = phase0_ssot_inventory.build_inventory(
        repo_root,
        db_path=db_path,
        immutable=immutable,
        max_samples_per_pattern=max(0, int(max_samples_per_pattern or 0)),
    )
    fact_plan = fact_write_migration_plan.build_fact_write_migration_plan(
        repo_root,
        db_path=db_path,
        immutable=immutable,
        sample_limit=max(1, min(10, int(max_samples_per_pattern or 1))),
        prebuilt_inventory=inventory,
    )
    test_file = repo_root / "tests" / "test_bayesian_probability_runtime.py"
    legacy_test_report = (
        legacy_additive_test_contract_audit.build_legacy_additive_test_contract_audit(test_file)
        if test_file.exists()
        else {
            "schema": "legacy_additive_test_contract_audit_v1",
            "ok": True,
            "blocking_test_count": 0,
            "not_checked_reason": "test_file_missing_in_scanned_root",
        }
    )

    legacy_gate = _legacy_evidence_gate(inventory, legacy_test_report=legacy_test_report)
    ui_gate = _ui_gate(inventory)
    ui_read_path_gate = _ui_read_path_boundary_gate(repo_root)
    fact_gate = _table_writer_gate(inventory, fact_plan)
    posterior_gate = _posterior_entrypoint_gate(repo_root, inventory)
    monitor_gate = _monitor_signal_gate(repo_root)
    consumer_gate = _consumer_boundary_gate(repo_root, max_samples_per_pattern=max_samples_per_pattern)
    trajectory_gate = _trajectory_breakout_diagnostic_only_gate(
        repo_root,
        max_samples_per_pattern=max_samples_per_pattern,
    )
    probability_identity_gate = _probability_runtime_identity_gate(repo_root)
    if (repo_root / "docs" / "README.md").exists():
        document_report = document_authority_audit.build_audit(repo_root)
        document_gate = {
            "ok": bool(document_report.get("ok")),
            "blockers": [
                _blocker("document_authority_contract_failed", details=dict(item))
                for item in document_report.get("violations") or []
                if isinstance(item, Mapping)
            ],
            "warnings": [],
            "report": document_report,
        }
    else:
        document_gate = {
            "ok": True,
            "blockers": [],
            "warnings": [],
            "not_checked_reason": "docs_authority_index_missing_in_fixture_root",
        }
    blockers = _collect_blockers(
        legacy_gate,
        ui_gate,
        ui_read_path_gate,
        fact_gate,
        posterior_gate,
        monitor_gate,
        consumer_gate,
        trajectory_gate,
        probability_identity_gate,
        document_gate,
    )
    warnings = _collect_warnings(
        fact_gate,
        posterior_gate,
        monitor_gate,
        consumer_gate,
        trajectory_gate,
        probability_identity_gate,
        document_gate,
    )

    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "production_chain_uniqueness_read_only_audit",
        "root": str(repo_root),
        "guards": _guards(),
        "definition": {
            "production_posterior_ssot_fact_chain": [
                "immutable AMSC raw archive",
                "source_observations -> canonical_observation_events",
                "P0(prior + joint weak exactly once) -> joint continuous Y x 8-state",
                "pre-event controlled Predict -> current-event joint emission -> regular-METAR hard condition",
                "normalize -> clocks/history -> reported-X projection",
                "posterior_checkpoint_v4_physical_state_bayes_v6 -> reported physical X SSOT",
                "independent X->S->B bridge or explicit market_projection_na",
                "target realization -> auto_trade_decisions",
                "auto_trade_jobs/auto_orders",
                "ui_snapshot/daily_review read-only consumers",
            ],
            "weak_likelihood_layer": "joint_forecast_weak_likelihood_runtime",
            "strong_likelihood_layer": "physical_state_bayes_v6 current-event joint emission",
            "live_posterior_composer": probability_identity_gate.get("composer_schema"),
            "live_posterior_formula": probability_identity_gate.get("bayes_formula_id"),
            "hard_source_boundary": "AMSC scheduled regular METAR hard X bound; AMSC SPECI soft strong evidence",
        },
        "checks": {
            "legacy_evidence_not_production": legacy_gate["ok"],
            "ui_no_frontend_recompute": ui_gate["ok"],
            "ui_read_path_no_archive_or_execution_side_effect": ui_read_path_gate["ok"],
            "fact_writer_single_canonical_owner": fact_gate["ok"],
            "v6_single_production_mainline": posterior_gate["ok"],
            "monitor_signal_no_bot_signal_alias": monitor_gate["ok"],
            "readonly_watcher_no_external_weather_or_posterior": consumer_gate["readonly_watcher_ok"],
            "order_submitter_no_weather_fact_or_posterior_write": consumer_gate["order_submitter_ok"],
            "trade_engine_only_sync_or_job_worker": consumer_gate["trade_engine_ok"],
            "trajectory_breakout_diagnostic_only": trajectory_gate["ok"],
            "v6_runtime_identity_aligned": probability_identity_gate["ok"],
            "document_authority_aligned": document_gate["ok"],
        },
        "blockers": blockers,
        "warnings": warnings,
        "gates": {
            "legacy_evidence": legacy_gate,
            "ui_local_derivation": ui_gate,
            "ui_read_path_boundary": ui_read_path_gate,
            "fact_writer": fact_gate,
            "posterior_entrypoint": posterior_gate,
            "monitor_signal": monitor_gate,
            "consumer_boundaries": consumer_gate,
            "trajectory_breakout_diagnostic_only": trajectory_gate,
            "v6_runtime_identity": probability_identity_gate,
            "document_authority": document_gate,
        },
        "inventory_summary": {
            "phase0_schema": inventory.get("schema"),
            "phase0_fork_risks": inventory.get("fork_risks") or [],
            "phase0_next_actions": inventory.get("next_actions") or [],
            "fact_write_migration_schema": fact_plan.get("schema"),
            "fact_write_next_actions": fact_plan.get("next_actions") or [],
        },
        "next_actions": (
            [
                "fix_blockers_until_production_chain_uniqueness_ok",
                "rerun_scripts/production_chain_uniqueness_audit.py",
                "rerun_scripts/production_probability_v6_live_acceptance.py_for_active_live_chain",
            ]
            if blockers
            else [
                "run_production_probability_v6_readiness_and_live_acceptance",
                "verify_next_forward_canonical_event_within_three_seconds",
            ]
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only audit for the posterior SSOT production chain boundary.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--db", default=str(phase0_schema_probe.DEFAULT_DB))
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument("--immutable", action="store_true")
    parser.add_argument("--max-samples-per-pattern", type=int, default=20)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_production_chain_uniqueness_audit(
        Path(args.root),
        db_path=None if args.no_db else Path(args.db),
        immutable=bool(args.immutable),
        max_samples_per_pattern=max(0, int(args.max_samples_per_pattern or 0)),
    )
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "execution_mode": report["execution_mode"],
            "checks": report["checks"],
            "blockers": report["blockers"],
            "warnings": report["warnings"],
            "guards": report["guards"],
            "next_actions": report["next_actions"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
