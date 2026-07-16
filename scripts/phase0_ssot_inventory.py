#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, NamedTuple

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[1]
for import_path in (SCRIPT_DIR, ROOT):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import phase0_schema_probe
import legacy_evidence_migration_audit
import ssot_chain_replay_audit
from parameter_change_governance import parameter_change_governance_contract
from phase_weighting_runtime import phase_weighting_contract
from weather_source_acquisition_manager import source_acquisition_catalog
from awc_hard_source_signal_state_machine import awc_hard_source_acceptance_contract
from daily_weather_review_runtime import daily_review_input_contract


SCHEMA_VERSION = "phase0_ssot_inventory_v1"


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

EXCLUDE_RELATIVE_FILES = {
    "scripts/phase0_ssot_inventory.py",
    "scripts/fact_write_migration_plan.py",
}


class PatternSpec(NamedTuple):
    id: str
    layer: str
    kind: str
    pattern: str
    table: str = ""
    target_owner: str = ""
    risk: str = ""


LAYER_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "layer": "market_contract",
        "target_ssot": "market_contract / weather_source_fit",
        "target_write_owner": "market discovery / source fit writer",
        "target_consumers": ["posterior", "decision", "ui_snapshot"],
    },
    {
        "layer": "source_acquisition",
        "target_ssot": "raw cache + source_observations",
        "target_write_owner": "weather_source_acquisition_manager",
        "target_consumers": ["parser", "Bayes", "audit", "daily_review"],
    },
    {
        "layer": "normalized_source_observation",
        "target_ssot": "source_observations.source_observation_id",
        "target_write_owner": "weather_source_acquisition_manager",
        "target_consumers": ["fact_snapshot_writer", "posterior", "audit"],
    },
    {
        "layer": "fact_snapshot",
        "target_ssot": "event_fact_history + event_fact_latest",
        "target_write_owner": "weather_fact_snapshot_writer",
        "target_consumers": ["hierarchical_posterior", "auto_trade_decisions", "ui_snapshot", "daily_review"],
    },
    {
        "layer": "hierarchical_posterior",
        "target_ssot": "posterior_ssot_fact + posterior_temperature_distribution in fact snapshot",
        "target_write_owner": "hierarchical_posterior_pipeline_runtime -> weather_fact_snapshot_writer",
        "target_consumers": ["auto_trade_decisions", "ui_snapshot", "daily_review"],
    },
    {
        "layer": "auto_trade_decisions",
        "target_ssot": "auto_trade_decisions",
        "target_write_owner": "auto_trade_decision_writer",
        "target_consumers": ["execution", "order_worker", "ui_snapshot", "watcher"],
    },
    {
        "layer": "awc_hard_source_signal",
        "target_ssot": "awc_hard_source_signals + signal_to_order_timing",
        "target_write_owner": "awc_hard_source_signal_state_machine",
        "target_consumers": ["decision", "execution", "daily_review"],
    },
    {
        "layer": "order_execution",
        "target_ssot": "auto_orders / order ledger",
        "target_write_owner": "auto_trade_job_writer / order submitter / sync worker",
        "target_consumers": ["ui_snapshot", "daily_review", "risk_audit"],
    },
    {
        "layer": "ui_snapshot",
        "target_ssot": "/api/ui_snapshot",
        "target_write_owner": "ui snapshot presenter",
        "target_consumers": ["browser render only"],
    },
    {
        "layer": "daily_review",
        "target_ssot": "daily_weather_reviews",
        "target_write_owner": "daily_weather_review_runtime",
        "target_consumers": ["bias_correction_candidates", "manual_review"],
    },
    {
        "layer": "bias_correction_candidates",
        "target_ssot": "bias_correction_candidates + parameter_change_events",
        "target_write_owner": "calibration runtime / parameter governance",
        "target_consumers": ["manual approval", "parameter apply"],
    },
)


AWC_SIGNAL_TO_ORDER_CONTRACT: dict[str, Any] = {
    "schema": "awc_signal_to_order_contract_v1",
    "optimization_target": "accepted_awc_hard_source_signal_to_order_submit_within_3000ms",
    "signal_boundary": "awc_hard_source_signal_at_after_awc_acceptance_window",
    "pre_signal_boundary": "hour_boundary_to_awc_pre_signal_deadline_is_preparation_only_no_orders",
    "default_pre_signal_required_components": [
        "market_prefetch",
        "market_mapping",
        "orderbook",
        "capital_caps",
    ],
    "optional_pre_signal_components": [
        "open_orders",
        "positions",
    ],
    "timing_table": "signal_to_order_timing",
    "required_timing_columns": [
        "signal_id",
        "decision_id",
        "order_intent_id",
        "awc_hard_source_signal_at",
        "fact_snapshot_written_at",
        "posterior_written_at",
        "auto_trade_decision_written_at",
        "execution_consumer_seen_decision_at",
        "order_job_enqueued_at",
        "order_submitted_at",
        "signal_to_order_slo_violation_reason",
        "timing_json",
        "created_at",
    ],
    "required_timing_json_fields": [
        "hour_boundary_at",
        "preparation_started_at",
        "awc_pre_signal_deadline_at",
        "awc_poll_window_started_at",
        "awc_poll_window_ended_at",
        "awc_release_to_signal_seen_ms",
        "awc_hard_source_signal_at",
        "pre_signal_prepare_state",
        "pre_signal_prepare_reason",
        "pre_signal_prepared_at",
        "pre_signal_prepare_required_components",
        "pre_signal_prepare_missing_components",
        "pre_signal_prepare_complete",
        "fact_written_at",
        "posterior_written_at",
        "decision_written_at",
        "job_enqueued_at",
        "order_submitted_at",
        "terminal_state",
        "terminal_reason",
        "signal_to_order_slo_ms",
        "signal_to_order_slo_met",
        "signal_to_order_slo_violation_reason",
    ],
    "terminal_states": [
        "submitted",
        "blocked",
        "stale",
        "skipped",
        "no_action",
        "error",
    ],
    "hard_guards": [
        "pre_signal_places_orders=false",
        "bypasses_auto_trade_decisions=false",
        "bypasses_orderbook_or_risk=false",
        "terminal_submitted_not_downgraded_by_lower_rank_state",
    ],
}

UI_LOCAL_DERIVATION_ALLOWED_SYMBOLS = {
    "bayesChainForDistributionRow",
    "legacyPhysicalLikelihoodSnapshotCompatFromItem",
    "legacyDistributionLikelihoodCompatForRow",
}

UI_LOCAL_DERIVATION_BLOCKING_SYMBOLS = {
    "componentProbability",
    "chainEvidenceValue",
}

POSTERIOR_CONSUMPTION_FIELD_SPECS: tuple[dict[str, str], ...] = (
    {"field": "posterior_ssot_fact", "category": "ssot_primary"},
    {"field": "posterior_temperature_distribution", "category": "retired_compat_distribution"},
    {"field": "posterior_target_bucket_selection", "category": "retired_compat_selection"},
    {"field": "target_bucket_selection", "category": "retired_compat_selection"},
    {"field": "q_temperature_bucket_posterior", "category": "retired_compat_distribution"},
    {"field": "target_bucket", "category": "ssot_primary"},
    {"field": "target_probability", "category": "ssot_primary"},
    {"field": "target_probability_field", "category": "ssot_primary"},
    {"field": "fact_snapshot_id", "category": "lineage"},
    {"field": "posterior_run_id", "category": "lineage"},
    {"field": "phase_reason", "category": "phase_weight"},
    {"field": "beta_weak", "category": "phase_weight"},
    {"field": "beta_strong", "category": "phase_weight"},
    {"field": "hard_mask_reason", "category": "phase_weight"},
    {"field": "probability_pre_hard_mask", "category": "diagnostic_pre_hard_mask"},
)

IDENTITY_FIELD_NAMES: tuple[str, ...] = (
    "source_event_id",
    "source_observation_id",
    "fact_snapshot_id",
    "posterior_run_id",
    "decision_id",
    "signal_id",
    "order_intent_id",
    "order_id",
    "auto_order_id",
    "job_id",
    "run_id",
    "review_id",
    "daily_calibration_record_id",
    "calibration_candidate_id",
    "correlation_id",
)

POSTERIOR_CONSUMER_ROLE_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "role": "posterior_runtime",
        "boundary": "produce posterior_ssot_fact and posterior lineage inside fact payload; no orders or decisions",
    },
    {
        "role": "fact_writer",
        "boundary": "persist fact snapshot and merge-fill lineage; no frontend or trading recompute",
    },
    {
        "role": "auto_trade_decision_writer",
        "boundary": "consume posterior_ssot_fact/hard-source target and write auto_trade_decisions as the sole execution blocker source",
    },
    {
        "role": "auto_trade_candidate_worker",
        "boundary": "prepare and enqueue decisions from backend SSOT; no UI-derived probability or second target bucket",
    },
    {
        "role": "execution_worker",
        "boundary": "consume auto_trade_decisions/jobs/orders; no posterior recompute or weather fact mutation",
    },
    {
        "role": "readonly_watcher",
        "boundary": "observe decisions/orders and emit diagnostics only; no weather source fetch, posterior recompute, or bot signal writes",
    },
    {
        "role": "ui_presenter",
        "boundary": "render /api/ui_snapshot fields only; no browser-side target/probability recompute",
    },
    {
        "role": "daily_review",
        "boundary": "read the same fact snapshot/posterior lineage and produce review/calibration candidates; no direct parameter apply",
    },
    {
        "role": "audit",
        "boundary": "read-only diagnostics and replay; no production mutation",
    },
    {
        "role": "compat_migration",
        "boundary": "read legacy fields for migration or retirement plans only; no production candidate trigger",
    },
)

DECISION_LEGACY_COMPAT_BLOCKING_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "legacy_distribution_as_decision_ssot",
        r"decision_ssot\b.*posterior_temperature_distribution",
    ),
    (
        "legacy_distribution_as_default_target_source",
        r"target_source\s*=\s*target_source\s+or\s+[\"']posterior_temperature_distribution[\"']",
    ),
    (
        "legacy_distribution_as_probability_field_fallback",
        r"target_probability_field\b.*posterior_temperature_distribution",
    ),
    (
        "legacy_selection_as_canonical_target_source",
        r"_canonical_target_from_payload\b.*(?:posterior_target_bucket_selection|target_bucket_selection|q_temperature_bucket_posterior)",
    ),
)


PATTERNS: tuple[PatternSpec, ...] = (
    PatternSpec(
        id="write_event_fact_history",
        layer="fact_snapshot",
        kind="db_write",
        table="event_fact_history",
        target_owner="weather_fact_snapshot_writer",
        pattern=r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?event_fact_history\b",
        risk="fact_snapshot_write_not_single_owner",
    ),
    PatternSpec(
        id="write_event_fact_latest",
        layer="fact_snapshot",
        kind="db_write",
        table="event_fact_latest",
        target_owner="weather_fact_snapshot_writer",
        pattern=r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?event_fact_latest\b",
        risk="latest_pointer_write_not_single_owner",
    ),
    PatternSpec(
        id="write_event_fact_summary_cache",
        layer="fact_snapshot",
        kind="db_write",
        table="event_fact_summary_cache",
        target_owner="weather_fact_snapshot_writer",
        pattern=r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?event_fact_summary_cache\b",
        risk="summary_cache_write_not_single_owner",
    ),
    PatternSpec(
        id="write_auto_trade_decisions",
        layer="auto_trade_decisions",
        kind="db_write",
        table="auto_trade_decisions",
        target_owner="auto_trade_decision_writer",
        pattern=r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?auto_trade_decisions\b",
        risk="decision_write_not_single_owner",
    ),
    PatternSpec(
        id="write_auto_trade_jobs",
        layer="order_execution",
        kind="db_write",
        table="auto_trade_jobs",
        target_owner="auto_trade_job_writer",
        pattern=r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?auto_trade_jobs\b",
        risk="execution_job_write_not_single_owner",
    ),
    PatternSpec(
        id="write_auto_orders",
        layer="order_execution",
        kind="db_write",
        table="auto_orders",
        target_owner="auto_order_ledger_writer",
        pattern=r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?auto_orders\b",
        risk="order_ledger_write_not_single_owner",
    ),
    PatternSpec(
        id="write_edge_trades",
        layer="order_execution",
        kind="legacy_exec_write",
        table="edge_trades",
        target_owner="edge_trade_ledger_writer",
        pattern=r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?edge_trades\b",
        risk="legacy_execution_ledger_write_surface",
    ),
    PatternSpec(
        id="fact_writer_helper",
        layer="fact_snapshot",
        kind="entrypoint",
        target_owner="weather_fact_snapshot_writer",
        pattern=r"\b(?:weather_fact_snapshot_writer|build_fact_snapshot_write_plan|build_hierarchical_posterior_fact_snapshot_write_plan|build_hierarchical_posterior_fact_payload|merge_fill_fact_payload|def\s+(?:ensure_fact_tables|_insert_event_fact_history|_upsert_event_fact_latest|_insert_fact_snapshot))\b",
        risk="fact_writer_helper_outside_target_module",
    ),
    PatternSpec(
        id="source_fetch_entrypoint",
        layer="source_acquisition",
        kind="entrypoint",
        target_owner="weather_source_acquisition_manager",
        pattern=r"\b(?:weather_source_acquisition|source_acquisition_trace|fetch_zuuu_metar_raw_awc_features|_refresh_awc_fact|_refresh_wu_twc_fact|external_source_delay|peak_window_evidence|AviationWeather|aviationweather|Weather\.com|weather\.com|twc_current|wu_page_api|wu_display|awc_cache|metar|taf_peak|ecmwf|gfs|icon|meteoblue|TWC|WU|METAR|TAF|ECMWF|GFS|ICON|Meteoblue)\b",
        risk="source_fetch_outside_manager",
    ),
    PatternSpec(
        id="normalized_source_observation_entrypoint",
        layer="normalized_source_observation",
        kind="entrypoint",
        target_owner="weather_source_acquisition_manager",
        pattern=r"\b(?:SourceObservation|source_observations|source_latency_trace|build_source_observation|observation_from_source_payload|build_source_acquisition_trace)\b",
        risk="normalized_source_observation_boundary_missing",
    ),
    PatternSpec(
        id="legacy_evidence_field",
        layer="hierarchical_posterior",
        kind="legacy_evidence",
        target_owner="legacy_evidence_migration_registry",
        pattern=r"\b(?:q_bayes_(?:model|wu_forecast|weather_com_forecast|taf_peak|observed_nowcast|nowcast|temperature_trajectory|physical_likelihood|independent_physics_residual|climo_regime|post_cool_rebound_[a-z_]+)_evidence|temperature_trajectory_evidence|physical_likelihood_evidence|climo_regime_evidence)\b",
        risk="legacy_additive_evidence_or_ui_compat_surface",
    ),
    PatternSpec(
        id="legacy_evidence_classification_entrypoint",
        layer="hierarchical_posterior",
        kind="migration_registry",
        target_owner="legacy_evidence_migration_registry",
        pattern=r"\b(?:legacy_evidence_migration_registry|legacy_evidence_migration_audit|LEGACY_EVIDENCE_MIGRATIONS|classify_legacy_evidence_field|legacy_evidence_registry_summary)\b",
        risk="legacy_evidence_classification_missing",
    ),
    PatternSpec(
        id="posterior_runtime_entrypoint",
        layer="hierarchical_posterior",
        kind="entrypoint",
        target_owner="sequential_remaining_warming_bayes_runtime",
        pattern=r"\b(?:sequential_remaining_warming_bayes_runtime|build_sequential_remaining_warming_posterior|hierarchical_temperature_posterior_runtime|build_hierarchical_temperature_posterior|layer_contributions|hard_constraints|beta_weak|beta_strong|posterior_pipeline_entry_runtime|temperature_posterior_runtime|apply_hard_source_posterior_override|posterior_temperature_distribution|q_temperature_bucket_posterior)\b",
        risk="posterior_runtime_not_single_entry",
    ),
    PatternSpec(
        id="phase_weighting_entrypoint",
        layer="hierarchical_posterior",
        kind="entrypoint",
        target_owner="phase_weighting_runtime",
        pattern=r"\b(?:phase_weighting_runtime|build_phase_weighting_snapshot|DEFAULT_PHASE_BETA_SCHEDULE|awc_release_window_for_time)\b",
        risk="phase_weighting_runtime_missing",
    ),
    PatternSpec(
        id="forecast_weak_likelihood_entrypoint",
        layer="hierarchical_posterior",
        kind="entrypoint",
        target_owner="forecast_weak_likelihood_runtime",
        pattern=r"\b(?:forecast_weak_likelihood_runtime|build_forecast_weak_likelihood|DEFAULT_GROUP_CONFIG|source_group_shrinkage|precision_cap_enabled|forecast_weak_likelihood_map)\b",
        risk="forecast_weak_likelihood_runtime_missing",
    ),
    PatternSpec(
        id="metar_strong_likelihood_entrypoint",
        layer="hierarchical_posterior",
        kind="entrypoint",
        target_owner="metar_strong_likelihood_runtime",
        pattern=r"\b(?:metar_strong_likelihood_runtime|build_metar_strong_likelihood|infer_environment_state_probs|remaining_warming_params|metar_strong_likelihood_map|state_probs|remaining_warming_by_state|dedupe_trace)\b",
        risk="metar_strong_likelihood_runtime_missing",
    ),
    PatternSpec(
        id="hierarchical_posterior_pipeline_entrypoint",
        layer="hierarchical_posterior",
        kind="entrypoint",
        target_owner="hierarchical_posterior_pipeline_runtime",
        pattern=r"\b(?:hierarchical_posterior_pipeline_runtime|build_hierarchical_posterior_pipeline|single_read_only_pipeline_entry|old_additive_evidence_pool_not_used)\b",
        risk="hierarchical_posterior_pipeline_runtime_missing",
    ),
    PatternSpec(
        id="awc_signal_entrypoint",
        layer="awc_hard_source_signal",
        kind="entrypoint",
        target_owner="awc_hard_source_signal_state_machine",
        pattern=r"\b(?:awc_hard_source_signal|signal_to_order_timing|awc_hard_source_signal_at|build_awc_hard_source_signal_candidate|_awc_release_watch_interval)\b",
        risk="awc_signal_state_machine_not_single_entry",
    ),
    PatternSpec(
        id="ui_snapshot_entrypoint",
        layer="ui_snapshot",
        kind="entrypoint",
        target_owner="ui snapshot presenter",
        pattern=r"\b(?:def\s+ui_snapshot|/api/ui_snapshot|ui_snapshot_schema_version|build_auto_trade_criteria_pills|/api/auto-trade-criteria)\b",
        risk="ui_snapshot_surface",
    ),
    PatternSpec(
        id="ui_local_derivation",
        layer="ui_snapshot",
        kind="ui_compat_or_derivation",
        target_owner="ui snapshot presenter",
        pattern=r"\b(?:q_bayes_.*_evidence|componentProbability|chainEvidenceValue|bayesChainForDistributionRow|legacyPhysicalLikelihoodSnapshotCompatFromItem|legacyDistributionLikelihoodCompatForRow)\b",
        risk="browser_may_render_legacy_or_derived_fields",
    ),
    PatternSpec(
        id="daily_review_entrypoint",
        layer="daily_review",
        kind="entrypoint",
        target_owner="daily_weather_review_runtime",
        pattern=r"\b(?:daily_weather_review|daily_weather_reviews|settlement_closed_loop_audit|weather_decision_core_audit|first_bad_link|bias_correction_candidate|calibration_candidate)\b",
        risk="daily_review_not_single_runtime",
    ),
    PatternSpec(
        id="parameter_governance_entrypoint",
        layer="bias_correction_candidates",
        kind="entrypoint",
        target_owner="parameter_change_governance",
        pattern=r"\b(?:parameter_change_events|calibration_candidate_id|rollback_plan|approved|applied|rolled_back)\b",
        risk="parameter_governance_not_single_runtime",
    ),
)


def _iter_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in EXCLUDE_DIRS]
        directory = Path(dirpath)
        for filename in filenames:
            path = directory / filename
            rel = str(path.relative_to(root))
            if rel in EXCLUDE_RELATIVE_FILES:
                continue
            if any(marker in path.name for marker in EXCLUDE_FILENAME_MARKERS):
                continue
            if path.suffix not in SCAN_SUFFIXES:
                continue
            if not path.is_file():
                continue
            yield path

def _line_match(line: str, spec: PatternSpec) -> bool:
    return re.search(spec.pattern, line, flags=re.IGNORECASE) is not None


def _pattern_applies_to_file(spec: PatternSpec, rel_path: str) -> bool:
    if spec.id != "ui_local_derivation":
        return True
    rel = str(rel_path or "").strip()
    if not rel.startswith("ui/"):
        return False
    return Path(rel).suffix in {".html", ".js", ".mjs"}


def _match_context(line: str) -> str:
    text = " ".join(str(line or "").strip().split())
    return text[:260]


def _file_boundary_flags(text: str) -> dict[str, bool]:
    return {
        "candidate_archive_copy_boundary": all(
            token in text
            for token in (
                "build_archive_rebuild_candidate_copy_boundary",
                "candidate_db_table_copy_not_fact_snapshot_mutation",
                "writes_candidate_archive_db",
                "replaces_source_db",
                "not_applicable_for_candidate_copy",
            )
        ),
        "temporary_drill_db_boundary": all(
            token in text
            for token in (
                "build_posterior_target_direct_latency_drill_boundary",
                "temporary_db_copy_fake_live_drill_not_production_writer",
                "writes_temporary_archive_db_copy",
                "writes_temporary_auto_trade_decisions",
                "uses_fake_order_runner",
            )
        ),
        "isolated_shadow_ssot_boundary": all(
            token in text
            for token in (
                "build_isolated_public_ssot_shadow_boundary",
                "isolated_shadow_only_temp_db_never_production_archive",
                "writes_production_archive",
                "writes_production_auto_trade_decisions",
                "SHADOW_MANIFEST_TABLE",
                "_assert_isolated_shadow_db",
            )
        ),
    }


def _scan_files(root: Path, *, max_samples_per_pattern: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pattern_map = {spec.id: spec for spec in PATTERNS}
    compiled_patterns = {
        spec.id: re.compile(spec.pattern, flags=re.IGNORECASE)
        for spec in PATTERNS
    }
    any_pattern = re.compile(
        "|".join(f"(?:{spec.pattern})" for spec in PATTERNS),
        flags=re.IGNORECASE,
    )
    by_pattern: dict[str, dict[str, Any]] = {
        spec.id: {
            "id": spec.id,
            "layer": spec.layer,
            "kind": spec.kind,
            "table": spec.table,
            "target_owner": spec.target_owner,
            "risk": spec.risk,
            "match_count": 0,
            "files": {},
            "samples": [],
            "truncated": False,
        }
        for spec in PATTERNS
    }
    files_scanned = 0
    bytes_scanned = 0
    file_boundaries: dict[str, dict[str, bool]] = {}
    file_errors: list[dict[str, Any]] = []
    for path in sorted(_iter_files(root)):
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            file_errors.append({"file": str(path.relative_to(root)), "error": str(exc)})
            continue
        bytes_scanned += len(text.encode("utf-8", errors="ignore"))
        rel = str(path.relative_to(root))
        file_boundaries[rel] = _file_boundary_flags(text)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any_pattern.search(line) is None:
                continue
            for spec_id, spec in pattern_map.items():
                if not _pattern_applies_to_file(spec, rel):
                    continue
                if compiled_patterns[spec_id].search(line) is None:
                    continue
                item = by_pattern[spec_id]
                item["match_count"] += 1
                files = item["files"]
                files[rel] = int(files.get(rel, 0)) + 1
                if len(item["samples"]) < max_samples_per_pattern:
                    item["samples"].append(
                        {
                            "file": rel,
                            "line": lineno,
                            "text": _match_context(line),
                        }
                    )
                else:
                    item["truncated"] = True
    return (
        {
            "files_scanned": files_scanned,
            "bytes_scanned": bytes_scanned,
            "file_errors": file_errors,
            "file_boundaries": file_boundaries,
            "patterns": by_pattern,
        },
        list(by_pattern.values()),
    )


def _files_for(patterns: dict[str, Any], *pattern_ids: str) -> list[str]:
    files: set[str] = set()
    for pattern_id in pattern_ids:
        entry = patterns.get(pattern_id) or {}
        files.update(str(path) for path in (entry.get("files") or {}).keys())
    return sorted(files)


def _non_live_writer_files(table: str, files: Iterable[str], file_boundaries: dict[str, dict[str, bool]]) -> list[str]:
    out: set[str] = set()
    for path in files:
        rel = str(path)
        flags = file_boundaries.get(rel) or {}
        if bool(flags.get("temporary_drill_db_boundary")):
            out.add(rel)
            continue
        if (
            table in {"event_fact_history", "event_fact_latest", "event_fact_summary_cache"}
            and bool(flags.get("isolated_shadow_ssot_boundary"))
        ):
            out.add(rel)
            continue
        if (
            table in {"event_fact_history", "event_fact_latest", "event_fact_summary_cache"}
            and bool(flags.get("candidate_archive_copy_boundary"))
        ):
            out.add(rel)
    return sorted(out)


def _table_write_inventory(patterns: dict[str, Any], file_boundaries: dict[str, dict[str, bool]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    boundaries = file_boundaries or {}
    for spec in PATTERNS:
        if not spec.table:
            continue
        entry = patterns.get(spec.id) or {}
        raw_writer_files = sorted((entry.get("files") or {}).keys())
        non_live_writer_files = _non_live_writer_files(spec.table, raw_writer_files, boundaries)
        writer_files = [path for path in raw_writer_files if path not in set(non_live_writer_files)]
        out[spec.table] = {
            "pattern_id": spec.id,
            "layer": spec.layer,
            "kind": spec.kind,
            "target_owner": spec.target_owner,
            "write_match_count": int(entry.get("match_count") or 0),
            "writer_files": writer_files,
            "non_live_writer_files": non_live_writer_files,
            "risk": spec.risk,
        }
    return out


def _legacy_usage_gate_clean(legacy_usage_report: dict[str, Any] | None) -> bool:
    if not legacy_usage_report:
        return False
    usage_gate = legacy_usage_report.get("usage_gate")
    if not isinstance(usage_gate, dict):
        return False
    return bool(legacy_usage_report.get("ok")) and bool(usage_gate.get("ok")) and not legacy_usage_report.get("next_actions")


def _ui_local_derivation_usage_report(root: Path) -> dict[str, Any]:
    spec = next(item for item in PATTERNS if item.id == "ui_local_derivation")
    references: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    for path in sorted(_iter_files(root)):
        rel = str(path.relative_to(root))
        if not _pattern_applies_to_file(spec, rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not _line_match(line, spec):
                continue
            symbols = [str(value) for value in re.findall(spec.pattern, line, flags=re.IGNORECASE)]
            has_raw_legacy_evidence = any(symbol.startswith("q_bayes_") for symbol in symbols)
            has_blocking_symbol = any(symbol in UI_LOCAL_DERIVATION_BLOCKING_SYMBOLS for symbol in symbols)
            allowed_symbols = [symbol for symbol in symbols if symbol in UI_LOCAL_DERIVATION_ALLOWED_SYMBOLS]
            is_allowed = bool(allowed_symbols) and not has_raw_legacy_evidence and not has_blocking_symbol
            reference = {
                "file": rel,
                "line": lineno,
                "symbols": symbols,
                "text": _match_context(line),
                "usage_role": "ui_retired_compat_surface" if is_allowed else "ui_local_derivation_blocker",
                "reason": (
                    "allowed_retired_compat_function_reference"
                    if is_allowed
                    else "browser_probability_or_legacy_evidence_derivation_reference"
                ),
            }
            references.append(reference)
            if is_allowed:
                allowed.append(reference)
            else:
                blockers.append(reference)
    return {
        "schema": "ui_local_derivation_usage_gate_v1",
        "ok": not blockers,
        "reference_count": len(references),
        "allowed_reference_count": len(allowed),
        "blocking_reference_count": len(blockers),
        "files": sorted({item["file"] for item in references}),
        "blocking_files": sorted({item["file"] for item in blockers}),
        "allowed_symbols": sorted(UI_LOCAL_DERIVATION_ALLOWED_SYMBOLS),
        "blocking_symbols": sorted(UI_LOCAL_DERIVATION_BLOCKING_SYMBOLS),
        "blocker_samples": blockers[:20],
        "allowed_samples": allowed[:20],
    }


def _ui_local_derivation_gate_clean(ui_derivation_report: dict[str, Any] | None) -> bool:
    return bool(ui_derivation_report) and bool(ui_derivation_report.get("ok"))


def _consumer_role_for_file(rel_path: str) -> str:
    rel = str(rel_path or "").strip()
    name = Path(rel).name
    if name in {
        "sequential_remaining_warming_bayes_runtime.py",
        "hierarchical_posterior_pipeline_runtime.py",
        "posterior_pipeline_entry_runtime.py",
        "phase_weighting_runtime.py",
        "forecast_weak_likelihood_runtime.py",
        "metar_strong_likelihood_runtime.py",
        "temperature_prior_runtime.py",
    }:
        return "posterior_runtime"
    if name == "weather_fact_snapshot_writer.py":
        return "fact_writer"
    if name == "auto_trade_decision_writer.py":
        return "auto_trade_decision_writer"
    if name in {"scan_weather_market.py", "hard_source_direct_poller.py", "hard_source_direct_copy_acceptance.py"}:
        return "auto_trade_candidate_worker"
    if name in {
        "auto_trade_job_writer.py",
        "auto_order_ledger_writer.py",
        "clob_order_service.mjs",
        "update_order_status.py",
        "posterior_target_live_job_audit.py",
    }:
        return "execution_worker"
    if name in {"auto_trade_watcher.py", "weather_decision_core_audit.py", "exit_retry_watch.py"} or "watcher" in name:
        return "readonly_watcher"
    if rel.startswith("ui/"):
        return "ui_presenter"
    if "daily_review" in rel or name in {"daily_weather_review_runtime.py", "weather_bias_calibration_runtime.py"}:
        return "daily_review"
    if "legacy_evidence" in rel or "rematerialization" in rel or "migration" in rel:
        return "compat_migration"
    if name.endswith("_audit.py") or "audit" in name:
        return "audit"
    return "other"


def _field_regex(field: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(field)}(?![A-Za-z0-9_])")


def _scan_field_references(
    root: Path,
    *,
    field_specs: Iterable[dict[str, str]],
    max_samples: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    specs = [dict(spec) for spec in field_specs]
    compiled = [(spec, _field_regex(str(spec.get("field") or ""))) for spec in specs if spec.get("field")]
    literal_fields = tuple(str(spec.get("field") or "") for spec in specs if spec.get("field"))
    references: list[dict[str, Any]] = []
    by_field: dict[str, dict[str, Any]] = {
        str(spec.get("field")): {
            "field": str(spec.get("field")),
            "category": str(spec.get("category") or ""),
            "reference_count": 0,
            "files": set(),
            "roles": set(),
        }
        for spec in specs
    }
    by_role: dict[str, dict[str, Any]] = {}
    for path in sorted(_iter_files(root)):
        rel = str(path.relative_to(root))
        role = _consumer_role_for_file(rel)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if literal_fields and not any(field in line for field in literal_fields):
                continue
            for spec, pattern in compiled:
                field = str(spec.get("field") or "")
                if not pattern.search(line):
                    continue
                category = str(spec.get("category") or "")
                field_entry = by_field[field]
                field_entry["reference_count"] = int(field_entry["reference_count"]) + 1
                field_entry["files"].add(rel)
                field_entry["roles"].add(role)
                role_entry = by_role.setdefault(
                    role,
                    {
                        "role": role,
                        "reference_count": 0,
                        "files": set(),
                        "fields": set(),
                        "categories": set(),
                    },
                )
                role_entry["reference_count"] = int(role_entry["reference_count"]) + 1
                role_entry["files"].add(rel)
                role_entry["fields"].add(field)
                role_entry["categories"].add(category)
                if len(references) < max(0, int(max_samples)):
                    references.append(
                        {
                            "file": rel,
                            "line": lineno,
                            "role": role,
                            "field": field,
                            "category": category,
                            "text": _match_context(line),
                        }
                    )
    field_rows: list[dict[str, Any]] = []
    for field, raw in sorted(by_field.items()):
        field_rows.append(
            {
                "field": field,
                "category": raw["category"],
                "reference_count": int(raw["reference_count"]),
                "files": sorted(raw["files"]),
                "roles": sorted(raw["roles"]),
            }
        )
    role_rows: dict[str, dict[str, Any]] = {}
    for role, raw in sorted(by_role.items()):
        role_rows[role] = {
            "role": role,
            "reference_count": int(raw["reference_count"]),
            "files": sorted(raw["files"]),
            "fields": sorted(raw["fields"]),
            "categories": sorted(raw["categories"]),
        }
    return references, {row["field"]: row for row in field_rows}, role_rows


def _posterior_consumption_inventory(root: Path, *, max_samples: int = 80) -> dict[str, Any]:
    samples, by_field, by_role = _scan_field_references(
        root,
        field_specs=POSTERIOR_CONSUMPTION_FIELD_SPECS,
        max_samples=max_samples,
    )
    decision_legacy_gate = _decision_legacy_compat_gate(root, max_samples=max_samples)
    decision_roles = {"auto_trade_decision_writer", "auto_trade_candidate_worker"}
    decision_fields = sorted(
        {
            field
            for role in decision_roles
            for field in (by_role.get(role) or {}).get("fields", [])
        }
    )
    decision_files = sorted(
        {
            file
            for role in decision_roles
            for file in (by_role.get(role) or {}).get("files", [])
        }
    )
    legacy_decision_fields = [
        field
        for field in decision_fields
        if str((by_field.get(field) or {}).get("category") or "").startswith("retired_compat")
    ]
    legacy_blocking_fields = list(decision_legacy_gate.get("blocking_fields") or [])
    required_decision_fields = {
        "posterior_ssot_fact",
        "target_bucket",
        "target_probability",
        "fact_snapshot_id",
        "posterior_run_id",
    }
    missing_decision_fields = sorted(required_decision_fields - set(decision_fields)) if decision_files else []
    role_contracts = [
        {
            **contract,
            "observed": str(contract["role"]) in by_role,
            "files": list((by_role.get(str(contract["role"])) or {}).get("files", [])),
            "fields": list((by_role.get(str(contract["role"])) or {}).get("fields", [])),
        }
        for contract in POSTERIOR_CONSUMER_ROLE_CONTRACTS
    ]
    return {
        "schema": "posterior_consumption_inventory_v1",
        "read_only": True,
        "field_count": len(by_field),
        "referenced_field_count": sum(1 for item in by_field.values() if int(item.get("reference_count") or 0) > 0),
        "reference_count": sum(int(item.get("reference_count") or 0) for item in by_field.values()),
        "fields": by_field,
        "roles": by_role,
        "role_contracts": role_contracts,
        "decision_posterior_field_chain": {
            "schema": "auto_trade_decision_posterior_field_chain_v1",
            "roles": sorted(decision_roles),
            "files": decision_files,
            "fields": decision_fields,
            "required_fields": sorted(required_decision_fields),
            "missing_required_fields": missing_decision_fields,
            "legacy_compat_fields_present": legacy_decision_fields,
            "legacy_compat_blocking_fields": legacy_blocking_fields,
            "legacy_compat_blocking_reference_count": int(
                decision_legacy_gate.get("blocking_reference_count") or 0
            ),
            "legacy_compat_allowed_reference_count": int(
                decision_legacy_gate.get("allowed_reference_count") or 0
            ),
            "status": (
                "not_observed"
                if not decision_files
                else "ready"
                if not missing_decision_fields and not legacy_blocking_fields
                else "gap"
            ),
            "boundary": "auto_trade_decisions may consume backend posterior_ssot_fact lineage, but must not derive a second target from legacy distribution fields",
        },
        "decision_legacy_compat_gate": decision_legacy_gate,
        "samples": samples,
    }


def _identity_field_inventory(root: Path, *, max_samples: int = 80) -> dict[str, Any]:
    specs = ({"field": field, "category": "identity"} for field in IDENTITY_FIELD_NAMES)
    samples, by_field, by_role = _scan_field_references(root, field_specs=specs, max_samples=max_samples)
    return {
        "schema": "identity_field_inventory_v1",
        "read_only": True,
        "required_identity_fields": list(IDENTITY_FIELD_NAMES),
        "available_identity_fields": [
            field
            for field, item in sorted(by_field.items())
            if int(item.get("reference_count") or 0) > 0
        ],
        "missing_identity_fields": [
            field
            for field, item in sorted(by_field.items())
            if int(item.get("reference_count") or 0) <= 0
        ],
        "fields": by_field,
        "roles": by_role,
        "samples": samples,
    }


def _decision_legacy_compat_gate(root: Path, *, max_samples: int = 80) -> dict[str, Any]:
    legacy_fields = {
        str(spec["field"])
        for spec in POSTERIOR_CONSUMPTION_FIELD_SPECS
        if str(spec.get("category") or "").startswith("retired_compat")
    }
    blocking_patterns = [
        (reason, re.compile(pattern))
        for reason, pattern in DECISION_LEGACY_COMPAT_BLOCKING_PATTERNS
    ]
    legacy_field_names = tuple(sorted(legacy_fields))
    legacy_field_patterns = [(field, _field_regex(field)) for field in legacy_field_names]
    blockers: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    reference_count = 0
    for path in sorted(_iter_files(root)):
        rel = str(path.relative_to(root))
        role = _consumer_role_for_file(rel)
        if role not in {"auto_trade_candidate_worker", "auto_trade_decision_writer"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if legacy_field_names and not any(field in line for field in legacy_field_names):
                continue
            matched_fields = [
                field
                for field, pattern in legacy_field_patterns
                if pattern.search(line)
            ]
            if not matched_fields:
                continue
            reference_count += len(matched_fields)
            matched_blockers = [
                reason
                for reason, pattern in blocking_patterns
                if pattern.search(line)
            ]
            item = {
                "file": rel,
                "line": lineno,
                "role": role,
                "fields": matched_fields,
                "text": _match_context(line),
            }
            if matched_blockers:
                item["reasons"] = matched_blockers
                if len(blockers) < max(0, int(max_samples)):
                    blockers.append(item)
            else:
                item["reason"] = "legacy_compat_or_diagnostic_reference_not_used_as_decision_target_source"
                if len(allowed) < max(0, int(max_samples)):
                    allowed.append(item)
    blocking_fields = sorted({field for item in blockers for field in item.get("fields", [])})
    allowed_fields = sorted({field for item in allowed for field in item.get("fields", [])})
    return {
        "schema": "decision_legacy_compat_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "reference_count": reference_count,
        "blocking_reference_count": len(blockers),
        "allowed_reference_count": len(allowed),
        "blocking_fields": blocking_fields,
        "allowed_fields": allowed_fields,
        "blocking_samples": blockers,
        "allowed_samples": allowed,
        "blocking_patterns": [
            {"reason": reason, "pattern": pattern}
            for reason, pattern in DECISION_LEGACY_COMPAT_BLOCKING_PATTERNS
        ],
    }


def _layer_matrix(
    patterns: dict[str, Any],
    legacy_usage_report: dict[str, Any] | None = None,
    ui_derivation_report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    layer_to_patterns: dict[str, list[str]] = {}
    for spec in PATTERNS:
        layer_to_patterns.setdefault(spec.layer, []).append(spec.id)

    matrix = []
    for contract in LAYER_CONTRACTS:
        layer = str(contract["layer"])
        pattern_ids = layer_to_patterns.get(layer, [])
        files = _files_for(patterns, *pattern_ids)
        match_count = sum(int((patterns.get(pattern_id) or {}).get("match_count") or 0) for pattern_id in pattern_ids)
        gaps: list[str] = []
        status = "observed"
        if not files:
            status = "missing_current_entrypoint"
            gaps.append("no_current_entrypoint_detected")
        target_owner = str(contract.get("target_write_owner") or "")
        if target_owner in {
            "weather_source_acquisition_manager",
            "weather_fact_snapshot_writer",
            "auto_trade_decision_writer",
            "auto_trade_job_writer",
            "auto_order_ledger_writer",
            "sequential_remaining_warming_bayes_runtime",
            "daily_weather_review_runtime",
            "parameter_change_governance",
        }:
            expected_filename = target_owner + ".py"
            if expected_filename not in files:
                gaps.append(f"target_owner_module_missing:{expected_filename}")
        if layer == "fact_snapshot" and len(files) > 1:
            gaps.append("current_write_or_helper_surface_spans_multiple_files")
        if (
            layer == "hierarchical_posterior"
            and int((patterns.get("legacy_evidence_field") or {}).get("match_count") or 0) > 0
            and not _legacy_usage_gate_clean(legacy_usage_report)
        ):
            gaps.append("legacy_evidence_fields_still_present")
        if (
            layer == "ui_snapshot"
            and int((patterns.get("ui_local_derivation") or {}).get("match_count") or 0) > 0
            and not _ui_local_derivation_gate_clean(ui_derivation_report)
        ):
            gaps.append("ui_legacy_or_local_derivation_surface_present")
        matrix.append(
            {
                **contract,
                "current_files": files,
                "current_match_count": match_count,
                "status": "gap" if gaps else status,
                "gaps": gaps,
            }
        )
    return matrix


def _fork_risks(
    patterns: dict[str, Any],
    schema_report: dict[str, Any] | None,
    *,
    file_boundaries: dict[str, dict[str, bool]] | None = None,
    legacy_usage_report: dict[str, Any] | None = None,
    ui_derivation_report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    table_inventory = _table_write_inventory(patterns, file_boundaries=file_boundaries)
    for table, item in table_inventory.items():
        writer_files = item.get("writer_files") or []
        if len(writer_files) > 1:
            risks.append(
                {
                    "risk": item.get("risk"),
                    "severity": "high" if table in {"event_fact_history", "event_fact_latest", "auto_trade_decisions"} else "medium",
                    "table": table,
                    "writer_files": writer_files,
                    "reason": "multiple_current_write_surfaces_detected",
                }
            )
    legacy_entry = patterns.get("legacy_evidence_field") or {}
    if int(legacy_entry.get("match_count") or 0) > 0:
        usage_gate = legacy_usage_report.get("usage_gate") if isinstance(legacy_usage_report, dict) else {}
        if _legacy_usage_gate_clean(legacy_usage_report):
            pass
        elif isinstance(usage_gate, dict) and usage_gate.get("ok") is False:
            risks.append(
                {
                    "risk": "legacy_additive_evidence_usage_gate_blocked",
                    "severity": "high",
                    "files": sorted(
                        set(usage_gate.get("production_blocking_files") or [])
                        | set(usage_gate.get("ui_primary_blocking_files") or [])
                        | set(usage_gate.get("unknown_usage_files") or [])
                    ),
                    "reason": "legacy_evidence_reference_blocks_production_or_ui_primary_usage_gate",
                    "usage_gate": {
                        "production_blocking_reference_count": usage_gate.get("production_blocking_reference_count"),
                        "ui_primary_blocking_reference_count": usage_gate.get("ui_primary_blocking_reference_count"),
                        "unknown_usage_reference_count": usage_gate.get("unknown_usage_reference_count"),
                    },
                }
            )
        else:
            risks.append(
                {
                    "risk": "legacy_additive_evidence_fields_still_present",
                    "severity": "medium",
                    "files": sorted((legacy_entry.get("files") or {}).keys()),
                    "reason": "legacy_evidence_fields_present_but_usage_gate_not_available_or_has_followups",
                }
            )
    ui_entry = patterns.get("ui_local_derivation") or {}
    if int(ui_entry.get("match_count") or 0) > 0:
        if _ui_local_derivation_gate_clean(ui_derivation_report):
            pass
        else:
            risks.append(
                {
                    "risk": "ui_local_or_compat_derivation_surface_present",
                    "severity": "medium",
                    "files": (
                        list(ui_derivation_report.get("blocking_files") or [])
                        if isinstance(ui_derivation_report, dict)
                        else sorted((ui_entry.get("files") or {}).keys())
                    ),
                    "reason": "browser_must_render_ui_snapshot_without_recomputing_probability_blocker_or_target",
                    "ui_local_derivation_gate": {
                        "blocking_reference_count": (
                            ui_derivation_report.get("blocking_reference_count")
                            if isinstance(ui_derivation_report, dict)
                            else None
                        ),
                        "allowed_reference_count": (
                            ui_derivation_report.get("allowed_reference_count")
                            if isinstance(ui_derivation_report, dict)
                            else None
                        ),
                    },
                }
            )
    if schema_report and not bool(schema_report.get("phase0_ready")):
        risks.append(
            {
                "risk": "phase0_schema_not_ready",
                "severity": "high",
                "missing_count": len(schema_report.get("missing") or []),
                "reason": "lineage_columns_planned_tables_or_fact_json_contract_missing",
            }
        )
    return risks


def _next_actions(matrix: list[dict[str, Any]], risks: list[dict[str, Any]], patterns: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if any("target_owner_module_missing:weather_source_acquisition_manager.py" in row.get("gaps", []) for row in matrix):
        actions.append("create_weather_source_acquisition_manager_read_only_trace")
    if any("target_owner_module_missing:weather_fact_snapshot_writer.py" in row.get("gaps", []) for row in matrix):
        actions.append("extract_fact_snapshot_writer_dry_run_boundary")
    fact_write_risks = {
        "fact_snapshot_write_not_single_owner",
        "latest_pointer_write_not_single_owner",
        "summary_cache_write_not_single_owner",
    }
    if any("current_write_or_helper_surface_spans_multiple_files" in row.get("gaps", []) for row in matrix) and any(
        risk.get("risk") in fact_write_risks for risk in risks
    ):
        actions.append("migrate_fact_writes_to_weather_fact_snapshot_writer")
    if any("target_owner_module_missing:sequential_remaining_warming_bayes_runtime.py" in row.get("gaps", []) for row in matrix):
        actions.append("create_sequential_remaining_warming_bayes_runtime_entrypoint")
    if any(
        risk.get("risk") in {
            "legacy_additive_evidence_fields_still_present",
            "legacy_additive_evidence_usage_gate_blocked",
        }
        for risk in risks
    ):
        has_classification_registry = bool((patterns.get("legacy_evidence_classification_entrypoint") or {}).get("files"))
        if has_classification_registry:
            actions.append("migrate_or_retire_legacy_evidence_fields_according_to_registry")
        else:
            actions.append("classify_legacy_evidence_fields_as_retired_compat_or_migrate")
    if any(risk.get("risk") == "phase0_schema_not_ready" for risk in risks):
        actions.append("prepare_confirmed_schema_apply_window_before_forward_write")
    if any(risk.get("risk") == "ui_local_or_compat_derivation_surface_present" for risk in risks):
        actions.append("replace_ui_local_derivation_with_backend_ui_snapshot_or_retired_compat")
    if not actions:
        actions.append("phase0_ssot_inventory_clean")
    return actions


def _production_chain_map(matrix: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    previous_layer = ""
    for index, row in enumerate(matrix, start=1):
        layer = str(row.get("layer") or "")
        out.append(
            {
                "position": index,
                "layer": layer,
                "upstream_layer": previous_layer,
                "target_ssot": row.get("target_ssot"),
                "target_write_owner": row.get("target_write_owner"),
                "target_consumers": list(row.get("target_consumers") or []),
                "current_status": row.get("status"),
                "current_files": list(row.get("current_files") or []),
                "gaps": list(row.get("gaps") or []),
            }
        )
        previous_layer = layer
    return out


def _entrypoint_inventory(patterns: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for spec in PATTERNS:
        entry = patterns.get(spec.id) or {}
        files = sorted((entry.get("files") or {}).keys())
        entries.append(
            {
                "id": spec.id,
                "layer": spec.layer,
                "kind": spec.kind,
                "table": spec.table,
                "target_owner": spec.target_owner,
                "risk": spec.risk,
                "observed": bool(files),
                "match_count": int(entry.get("match_count") or 0),
                "files": files,
                "status": "observed" if files else "missing",
            }
        )
    return entries


def _cleanup_inventory(
    *,
    risks: list[dict[str, Any]],
    table_inventory: dict[str, Any],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for table, entry in sorted(table_inventory.items()):
        writer_files = list(entry.get("writer_files") or [])
        expected_owner_file = f"{entry.get('target_owner')}.py" if entry.get("target_owner") else ""
        if len(writer_files) != 1 or (expected_owner_file and expected_owner_file not in writer_files):
            items.append(
                {
                    "id": f"write_surface:{table}",
                    "category": "write_surface",
                    "severity": "high" if table in {"event_fact_history", "event_fact_latest", "auto_trade_decisions"} else "medium",
                    "target": table,
                    "target_owner": entry.get("target_owner"),
                    "current_files": writer_files,
                    "risk": entry.get("risk"),
                    "cleanup_action": "migrate_or_wrap_writes_under_target_owner",
                }
            )
    for risk in risks:
        risk_id = str(risk.get("risk") or "")
        if risk_id == "legacy_additive_evidence_fields_still_present":
            items.append(
                {
                    "id": "legacy_evidence_fields",
                    "category": "legacy_probability_surface",
                    "severity": risk.get("severity", "high"),
                    "target_owner": "sequential_remaining_warming_bayes_runtime",
                    "current_files": list(risk.get("files") or []),
                    "risk": risk_id,
                    "cleanup_action": "retire_or_map_legacy_fields_via_legacy_evidence_migration_registry",
                }
            )
        elif risk_id == "ui_local_or_compat_derivation_surface_present":
            items.append(
                {
                    "id": "ui_local_derivation_surface",
                    "category": "ui_ssot",
                    "severity": risk.get("severity", "medium"),
                    "target_owner": "ui snapshot presenter",
                    "current_files": list(risk.get("files") or []),
                    "risk": risk_id,
                    "cleanup_action": "move_probability_target_blocker_derivation_to_backend_ui_snapshot",
                }
            )
        elif risk_id == "phase0_schema_not_ready":
            items.append(
                {
                    "id": "phase0_schema_surface",
                    "category": "schema",
                    "severity": risk.get("severity", "high"),
                    "target_owner": "phase0_schema_apply",
                    "risk": risk_id,
                    "missing_count": risk.get("missing_count"),
                    "cleanup_action": "run_confirmed_schema_apply_window_after_backup_and_quiescence",
                }
            )
    return {
        "schema": "phase0_cleanup_inventory_v1",
        "item_count": len(items),
        "items": items,
    }


def _checklist_summary(
    *,
    matrix: list[dict[str, Any]],
    entrypoints: list[dict[str, Any]],
    cleanup: dict[str, Any],
    risks: list[dict[str, Any]],
    schema_report: dict[str, Any] | None,
    source_catalog: dict[str, Any] | None,
    awc_acceptance_contract: dict[str, Any] | None,
    daily_review_contract: dict[str, Any] | None,
    phase_contract: dict[str, Any] | None,
    replay_contract: dict[str, Any] | None,
    parameter_contract: dict[str, Any] | None,
    posterior_consumption: dict[str, Any] | None,
    identity_inventory: dict[str, Any] | None,
) -> dict[str, Any]:
    layers_with_gaps = [str(row.get("layer") or "") for row in matrix if row.get("status") == "gap"]
    entrypoint_missing = [str(item.get("id") or "") for item in entrypoints if not item.get("observed")]
    risk_ids = [str(item.get("risk") or "") for item in risks]
    items = [
        {
            "id": "production_chain_graph",
            "available": bool(matrix),
            "status": "ready" if matrix else "missing",
            "reason": "production_chain_map_present" if matrix else "matrix_missing",
        },
        {
            "id": "ssot_matrix",
            "available": bool(matrix),
            "status": "gap" if layers_with_gaps else "ready",
            "gap_layers": layers_with_gaps,
        },
        {
            "id": "entrypoint_inventory",
            "available": bool(entrypoints),
            "status": "gap" if entrypoint_missing else "ready",
            "missing_entrypoints": entrypoint_missing,
        },
        {
            "id": "fork_risk_inventory",
            "available": True,
            "status": "gap" if risks else "ready",
            "risk_ids": risk_ids,
        },
        {
            "id": "field_cleanup_inventory",
            "available": True,
            "status": "gap" if int(cleanup.get("item_count") or 0) else "ready",
            "cleanup_item_count": int(cleanup.get("item_count") or 0),
        },
        {
            "id": "awc_signal_to_order_contract",
            "available": True,
            "status": "ready",
            "optimization_target": AWC_SIGNAL_TO_ORDER_CONTRACT["optimization_target"],
        },
        {
            "id": "awc_hard_source_acceptance_contract",
            "available": bool(awc_acceptance_contract),
            "status": "ready" if awc_acceptance_contract else "missing",
            "accepted_source_ids": awc_acceptance_contract.get("accepted_source_ids") if awc_acceptance_contract else [],
            "acceptance_states": awc_acceptance_contract.get("acceptance_states") if awc_acceptance_contract else [],
        },
        {
            "id": "source_acquisition_catalog",
            "available": bool(source_catalog),
            "status": "ready" if source_catalog and int(source_catalog.get("source_count") or 0) > 0 else "missing",
            "source_count": source_catalog.get("source_count") if source_catalog else 0,
            "alias_count": source_catalog.get("alias_count") if source_catalog else 0,
        },
        {
            "id": "daily_review_input_contract",
            "available": bool(daily_review_contract),
            "status": "ready" if daily_review_contract else "missing",
            "input_layer_count": len(daily_review_contract.get("required_input_layers") or []) if daily_review_contract else 0,
        },
        {
            "id": "phase_weighting_contract",
            "available": bool(phase_contract),
            "status": "ready" if phase_contract else "missing",
            "required_output_fields": phase_contract.get("required_output_fields") if phase_contract else [],
            "schedule_count": len(phase_contract.get("schedule") or []) if phase_contract else 0,
        },
        {
            "id": "ssot_chain_replay_input_contract",
            "available": bool(replay_contract),
            "status": "ready" if replay_contract else "missing",
            "output_sections": replay_contract.get("output_sections") if replay_contract else [],
        },
        {
            "id": "parameter_change_governance_contract",
            "available": bool(parameter_contract),
            "status": "ready" if parameter_contract else "missing",
            "states": (
                ((parameter_contract.get("state_machine") or {}).get("states") or [])
                if parameter_contract
                else []
            ),
        },
        {
            "id": "posterior_consumption_inventory",
            "available": bool(posterior_consumption),
            "status": (
                "ready"
                if posterior_consumption
                and (posterior_consumption.get("decision_posterior_field_chain") or {}).get("status")
                in {"ready", "not_observed"}
                else "gap"
                if posterior_consumption
                else "missing"
            ),
            "decision_chain_status": (
                (posterior_consumption.get("decision_posterior_field_chain") or {}).get("status")
                if posterior_consumption
                else ""
            ),
            "referenced_field_count": posterior_consumption.get("referenced_field_count") if posterior_consumption else 0,
        },
        {
            "id": "identity_field_inventory",
            "available": bool(identity_inventory),
            "status": (
                "ready"
                if identity_inventory
                and not identity_inventory.get("missing_identity_fields")
                else "gap"
                if identity_inventory
                else "missing"
            ),
            "available_identity_fields": identity_inventory.get("available_identity_fields") if identity_inventory else [],
            "missing_identity_fields": identity_inventory.get("missing_identity_fields") if identity_inventory else [],
        },
        {
            "id": "lineage_schema_surface",
            "available": schema_report is not None,
            "status": (
                "ready"
                if schema_report and bool(schema_report.get("phase0_ready"))
                else "gap"
                if schema_report
                else "not_checked"
            ),
            "missing_count": len(schema_report.get("missing") or []) if schema_report else None,
        },
    ]
    return {
        "schema": "phase0_development_checklist_summary_v1",
        "ready_count": sum(1 for item in items if item.get("status") == "ready"),
        "gap_count": sum(1 for item in items if item.get("status") == "gap"),
        "not_checked_count": sum(1 for item in items if item.get("status") == "not_checked"),
        "items": items,
    }


def build_inventory(
    root: Path,
    *,
    db_path: Path | None = None,
    immutable: bool = False,
    max_samples_per_pattern: int = 80,
    include_legacy_usage_audit: bool = True,
) -> dict[str, Any]:
    repo_root = Path(root).expanduser().resolve()
    scan_meta, entries = _scan_files(repo_root, max_samples_per_pattern=max(0, int(max_samples_per_pattern)))
    patterns = scan_meta["patterns"]
    file_boundaries = scan_meta.get("file_boundaries") or {}
    schema_report: dict[str, Any] | None = None
    if db_path is not None:
        db = Path(db_path).expanduser().resolve()
        if db.exists():
            try:
                schema_report = phase0_schema_probe.run_probe(db, immutable=immutable)
            except sqlite3.DatabaseError as exc:
                wal_path = Path(str(db) + "-wal")
                if not immutable:
                    raise
                schema_report = phase0_schema_probe.run_probe(db, immutable=False)
                schema_report["immutable_fallback_used"] = True
                schema_report["immutable_fallback_reason"] = f"{exc.__class__.__name__}: {exc}"
                schema_report["immutable_fallback_wal_exists"] = wal_path.exists()
                schema_report["immutable_fallback_wal_size"] = wal_path.stat().st_size if wal_path.exists() else 0
    legacy_usage_report: dict[str, Any] | None = None
    if include_legacy_usage_audit:
        legacy_usage_report = legacy_evidence_migration_audit.build_legacy_evidence_migration_audit(
            repo_root,
            max_samples_per_field=0,
        )
    ui_derivation_report = _ui_local_derivation_usage_report(repo_root)
    source_catalog = source_acquisition_catalog()
    awc_acceptance_contract = awc_hard_source_acceptance_contract()
    daily_review_contract = daily_review_input_contract()
    phase_contract = phase_weighting_contract()
    replay_contract = ssot_chain_replay_audit.ssot_chain_replay_input_contract()
    parameter_contract = parameter_change_governance_contract()
    posterior_consumption = _posterior_consumption_inventory(
        repo_root,
        max_samples=max(0, int(max_samples_per_pattern)),
    )
    identity_inventory = _identity_field_inventory(
        repo_root,
        max_samples=max(0, int(max_samples_per_pattern)),
    )
    matrix = _layer_matrix(
        patterns,
        legacy_usage_report=legacy_usage_report,
        ui_derivation_report=ui_derivation_report,
    )
    risks = _fork_risks(
        patterns,
        schema_report,
        file_boundaries=file_boundaries,
        legacy_usage_report=legacy_usage_report,
        ui_derivation_report=ui_derivation_report,
    )
    table_inventory = _table_write_inventory(patterns, file_boundaries=file_boundaries)
    entrypoints = _entrypoint_inventory(patterns)
    cleanup = _cleanup_inventory(risks=risks, table_inventory=table_inventory)
    checklist = _checklist_summary(
        matrix=matrix,
        entrypoints=entrypoints,
        cleanup=cleanup,
        risks=risks,
        schema_report=schema_report,
        source_catalog=source_catalog,
        awc_acceptance_contract=awc_acceptance_contract,
        daily_review_contract=daily_review_contract,
        phase_contract=phase_contract,
        replay_contract=replay_contract,
        parameter_contract=parameter_contract,
        posterior_consumption=posterior_consumption,
        identity_inventory=identity_inventory,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "read_only": True,
        "root": str(repo_root),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
        "scan": {
            "files_scanned": scan_meta["files_scanned"],
            "bytes_scanned": scan_meta["bytes_scanned"],
            "file_errors": scan_meta["file_errors"],
            "excluded_dirs": sorted(EXCLUDE_DIRS),
            "excluded_filename_markers": list(EXCLUDE_FILENAME_MARKERS),
            "suffixes": sorted(SCAN_SUFFIXES),
            "max_samples_per_pattern": int(max_samples_per_pattern),
        },
        "file_boundaries": {
            path: flags
            for path, flags in sorted(file_boundaries.items())
            if any(bool(value) for value in flags.values())
        },
        "production_chain_map": _production_chain_map(matrix),
        "ssot_matrix": matrix,
        "table_write_inventory": table_inventory,
        "entrypoint_inventory": entrypoints,
        "source_acquisition_catalog": source_catalog,
        "awc_hard_source_acceptance_contract": awc_acceptance_contract,
        "daily_review_input_contract": daily_review_contract,
        "phase_weighting_contract": phase_contract,
        "ssot_chain_replay_input_contract": replay_contract,
        "parameter_change_governance_contract": parameter_contract,
        "posterior_consumption_inventory": posterior_consumption,
        "identity_field_inventory": identity_inventory,
        "cleanup_inventory": cleanup,
        "awc_signal_to_order_contract": dict(AWC_SIGNAL_TO_ORDER_CONTRACT),
        "development_checklist": checklist,
        "pattern_inventory": entries,
        "fork_risks": risks,
        "legacy_evidence_usage_gate": {
            "checked": legacy_usage_report is not None,
            "ok": bool(legacy_usage_report.get("ok")) if legacy_usage_report else None,
            "field_reference_count": legacy_usage_report.get("field_reference_count") if legacy_usage_report else None,
            "usage_gate": legacy_usage_report.get("usage_gate") if legacy_usage_report else None,
            "next_actions": legacy_usage_report.get("next_actions") if legacy_usage_report else [],
        },
        "ui_local_derivation_gate": ui_derivation_report,
        "schema_probe": {
            "checked": schema_report is not None,
            "phase0_ready": bool(schema_report.get("phase0_ready")) if schema_report else None,
            "missing_count": len(schema_report.get("missing") or []) if schema_report else None,
            "immutable_fallback_used": bool(schema_report.get("immutable_fallback_used")) if schema_report else False,
            "immutable_fallback_reason": str(schema_report.get("immutable_fallback_reason") or "") if schema_report else "",
            "immutable_fallback_wal_exists": bool(schema_report.get("immutable_fallback_wal_exists")) if schema_report else False,
            "immutable_fallback_wal_size": int(schema_report.get("immutable_fallback_wal_size") or 0) if schema_report else 0,
        },
        "next_actions": _next_actions(matrix, risks, patterns),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Phase 0 SSOT and write-surface inventory.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or phase0_schema_probe.DEFAULT_DB))
    parser.add_argument("--no-db", action="store_true", help="Skip read-only archive DB schema probe.")
    parser.add_argument("--immutable", action="store_true", help="Open SQLite in immutable read-only mode.")
    parser.add_argument("--max-samples-per-pattern", type=int, default=80)
    parser.add_argument("--skip-legacy-usage-audit", action="store_true")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print compact matrix/risk/table summary instead of full pattern samples.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_inventory(
        Path(args.root),
        db_path=None if args.no_db else Path(args.db),
        immutable=bool(args.immutable),
        max_samples_per_pattern=max(0, int(args.max_samples_per_pattern or 0)),
        include_legacy_usage_audit=not bool(args.skip_legacy_usage_audit),
    )
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "guards": report["guards"],
            "scan": report["scan"],
            "production_chain_map": report["production_chain_map"],
            "ssot_matrix": report["ssot_matrix"],
            "table_write_inventory": report["table_write_inventory"],
            "entrypoint_inventory": report["entrypoint_inventory"],
            "source_acquisition_catalog": report["source_acquisition_catalog"],
            "awc_hard_source_acceptance_contract": report["awc_hard_source_acceptance_contract"],
            "daily_review_input_contract": report["daily_review_input_contract"],
            "phase_weighting_contract": report["phase_weighting_contract"],
            "ssot_chain_replay_input_contract": report["ssot_chain_replay_input_contract"],
            "parameter_change_governance_contract": report["parameter_change_governance_contract"],
            "posterior_consumption_inventory": report["posterior_consumption_inventory"],
            "identity_field_inventory": report["identity_field_inventory"],
            "cleanup_inventory": report["cleanup_inventory"],
            "awc_signal_to_order_contract": report["awc_signal_to_order_contract"],
            "development_checklist": report["development_checklist"],
            "fork_risks": report["fork_risks"],
            "legacy_evidence_usage_gate": report["legacy_evidence_usage_gate"],
            "ui_local_derivation_gate": report["ui_local_derivation_gate"],
            "schema_probe": report["schema_probe"],
            "next_actions": report["next_actions"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
