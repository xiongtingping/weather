"""Content identity for the retired historical probability runtime.

This module is retained only so old receipts remain reproducible.  It is not
an activation or production-source authority for the weather basket service.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
AUTHORITY_SCOPE = "RETIRED_HISTORY_ONLY"
PRODUCTION_RUNTIME_AUTHORIZED = False
RUNTIME_FILES = (
    "production_probability_runtime_manifest.py",
    "production_probability_activation_contract.py",
    "archive_db_write_queue.py",
    "physical_temperature_prior_context_runtime.py",
    "physical_latent_state_contract.py",
    "physical_latent_state_prior_runtime.py",
    "physical_x_bucket_contract_runtime.py",
    "temperature_prior_runtime.py",
    "joint_weak_live_evidence_runtime.py",
    "amsc_taf_revision_runtime.py",
    "independent_weak_live_source_runtime.py",
    "joint_forecast_weak_likelihood_runtime.py",
    "joint_weak_baseline_artifact_runtime.py",
    "joint_weak_bias_calibration_runtime.py",
    "weak_likelihood_source_set_runtime.py",
    "external_forecast_snapshot_runtime.py",
    "weak_forecast_revision_bayes_runtime.py",
    "weak_forecast_evidence_snapshot_runtime.py",
    "forecast_background_context_runtime.py",
    "weak_forecast_revision_schema_contract.py",
    "weak_forecast_revision_transaction_runtime.py",
    "production_weak_forecast_revision_writer.py",
    "production_weak_forecast_revision_event_runtime.py",
    "scripts/strict_asof_forecast_capture_readiness.py",
    "weather_source_acquisition_manager.py",
    "source_observation_runtime.py",
    "source_observation_replay_eligibility_runtime.py",
    "strict_asof_forecast_capture_runtime.py",
    "strict_asof_forecast_candidate_scan.py",
    "strict_asof_forecast_p1_ingress_writer.py",
    "strict_asof_forecast_source_observation_runtime.py",
    "initial_posterior_checkpoint_shadow.py",
    "peak_timing_state_runtime.py",
    "temperature_trajectory_feature_runtime.py",
    "strong_observation_feature_contract.py",
    "strong_observation_lr_artifact_contract.py",
    "strong_observation_lr_evaluator.py",
    "strong_observation_lr_runtime_registry.py",
    "strong_physical_evidence_projection_runtime.py",
    "strong_observation_lr_operational_physics_baseline.py",
    "strong_observation_lr_operational_state_baseline.py",
    "production_probability_p0_runtime.py",
    "production_initial_posterior_checkpoint.py",
    "production_initial_posterior_writer.py",
    "lineage_identity_runtime.py",
    "phase_weighting_runtime.py",
    "resolution_authority_contract_runtime.py",
    "resolution_bridge_runtime.py",
    "temperature_posterior_runtime.py",
    "sequential_bayes_schema_contract.py",
    "sequential_bayes_checkpoint_reader.py",
    "sequential_bayes_filter_shadow_runtime.py",
    "sequential_bayes_shadow_pipeline.py",
    "sequential_bayes_shadow_writer.py",
    "sequential_bayes_public_ssot_shadow_writer.py",
    "realization_state_projection_runtime.py",
    "production_sequential_bayes_candidate_compiler.py",
    "production_sequential_bayes_public_ssot_writer.py",
    "production_sequential_bayes_transaction_b_host.py",
    "production_sequential_bayes_event_runtime.py",
    "production_canonical_event_drain_runtime.py",
    "scripts/archive_db_wal_keeper.py",
    "canonical_observation_runtime.py",
    "canonical_observation_writer.py",
    "weather_fact_snapshot_writer.py",
    "zuuu_metar_reporting_schedule.py",
    "production_probability_successor_activation_contract.py",
    "production_probability_successor_runtime_manifest.py",
    "report_defined_physical_x_runtime.py",
    "reported_x_successor_checkpoint_runtime.py",
    "reported_x_successor_schema_contract.py",
    "production_reported_x_public_ssot_writer.py",
    "production_reported_x_initial_checkpoint.py",
    "production_reported_x_initial_writer.py",
    "production_reported_x_transaction_host.py",
    "reported_x_successor_checkpoint_reader.py",
    "production_reported_x_event_runtime.py",
    "production_reported_x_weak_revision_writer.py",
    "production_reported_x_weak_revision_event_runtime.py",
    "taf_revision_candidate_selection_runtime.py",
    "production_reported_x_canonical_event_drain_runtime.py",
)
RETIRED_NETWORK_ENTRYPOINTS = (
    "scripts/fetch_zuuu_metar_taf.py",
)
RETIRED_HISTORY_ONLY_FILES = (
    "amsc_raw_history_archive.py",
    "amsc_taf_revision_runtime.py",
)
RETIREMENT_FILES = (
    "scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh",
    "scripts/amsc_zuuu_metar_taf_poll_once.sh",
    "scripts/watch_zuuu_speci_once.py",
    "scripts/polymarket_monitor_daemon.sh",
    "launchd/com.weather.monitor.daemon.plist",
)
LEGACY_PROBABILITY_WRITER_TOKENS = (
    "AMSC_ZUUU_LEGACY_POSTERIOR_ENQUEUE_ENABLED",
    "amsc_speci_realtime_ingress",
    "--realtime-ingress",
    "com.weather.task_queue_worker",
)
PRODUCTION_PROBABILITY_INGRESS_TOKENS = (
    "AMSC_ZUUU_PRODUCTION_PROBABILITY_UPDATE_ENABLED",
    "PM_AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED",
    "apply_amsc_probability_fast_lane_from_plan.py",
    "--production-probability-ingress",
)


def _sha(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_current_runtime_manifest(root: Path | str = ROOT) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    return {
        "schema": "retired_probability_runtime_manifest_v1",
        "authority_scope": AUTHORITY_SCOPE,
        "production_runtime_authorized": PRODUCTION_RUNTIME_AUTHORIZED,
        "network_entrypoints": [],
        "files": {
            name: hashlib.sha256((base / name).read_bytes()).hexdigest()
            for name in RUNTIME_FILES
        },
    }


def current_runtime_manifest_sha256(root: Path | str = ROOT) -> str:
    return _sha(build_current_runtime_manifest(root))


def audit_runtime_manifest_dependency_closure(root: Path | str = ROOT) -> dict[str, Any]:
    """Prove every reachable local Python dependency is activation-bound."""

    base = Path(root).expanduser().resolve()
    registered = {Path(name) for name in RUNTIME_FILES}
    modules = {path.stem: path.relative_to(base) for path in base.glob("*.py")}
    scripts = base / "scripts"
    if scripts.is_dir():
        modules.update({
            f"scripts.{path.stem}": path.relative_to(base)
            for path in scripts.glob("*.py")
        })
    seen: set[Path] = set()
    queue = list(registered)
    parse_errors: list[str] = []
    while queue:
        relative = queue.pop()
        if relative in seen:
            continue
        path = base / relative
        if not path.exists() or path.suffix != ".py":
            continue
        seen.add(relative)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError) as exc:
            parse_errors.append(f"{relative}:{type(exc).__name__}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                candidate = modules.get(name) or modules.get(name.split(".")[0])
                if candidate is not None and candidate not in seen:
                    queue.append(candidate)
    missing = sorted(str(path) for path in seen - registered)
    return {
        "schema": "production_probability_runtime_manifest_dependency_closure_v1",
        "ok": not missing and not parse_errors,
        "registered_file_count": len(registered),
        "reachable_local_python_file_count": len(seen),
        "missing_runtime_files": missing,
        "parse_errors": sorted(parse_errors),
        "read_only": True,
    }


def build_current_retirement_receipt(root: Path | str = ROOT) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    existing = [name for name in RETIREMENT_FILES if (base / name).exists()]
    return {
        "schema": "legacy_probability_writer_retirement_receipt_v2",
        "authority_scope": AUTHORITY_SCOPE,
        "files": {
            name: (
                hashlib.sha256((base / name).read_bytes()).hexdigest()
                if (base / name).is_file()
                else None
            )
            for name in RETIREMENT_FILES
        },
        "existing_retired_entrypoints": sorted(existing),
        "legacy_probability_writer_absent": not existing,
        "production_probability_ingress_enabled_by_deployment": False,
        "production_runtime_authorized": False,
    }


def current_retirement_receipt_sha256(root: Path | str = ROOT) -> str:
    return _sha(build_current_retirement_receipt(root))


__all__ = [
    "AUTHORITY_SCOPE",
    "PRODUCTION_RUNTIME_AUTHORIZED",
    "RUNTIME_FILES",
    "RETIREMENT_FILES",
    "RETIRED_HISTORY_ONLY_FILES",
    "RETIRED_NETWORK_ENTRYPOINTS",
    "audit_runtime_manifest_dependency_closure",
    "build_current_runtime_manifest",
    "build_current_retirement_receipt",
    "current_runtime_manifest_sha256",
    "current_retirement_receipt_sha256",
]
