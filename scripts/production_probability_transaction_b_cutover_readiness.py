#!/usr/bin/env python3
"""Read-only readiness proof for the production Transaction-B cutover.

This script never enables the writer, edits activation JSON, writes SQLite,
stops services, or submits an order.  It separates a safe *pre-cutover*
candidate state from the much stricter state required to switch production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_activation_contract import (  # noqa: E402
    DEFAULT_PATH,
    ProductionProbabilityActivationError,
    load_production_probability_activation,
)
from production_probability_runtime_manifest import (  # noqa: E402
    audit_runtime_manifest_dependency_closure,
    current_retirement_receipt_sha256,
    current_runtime_manifest_sha256,
)
from production_probability_deployment_attestation import (  # noqa: E402
    resolve_retirement_deployment_authorization,
)
from scripts.activate_dynamic_three_leg_execution_schema import build_cutover_plan  # noqa: E402


SCHEMA_VERSION = "production_probability_transaction_b_cutover_readiness_v1"
REQUIRED_TABLES = (
    "strong_observation_updates", "posterior_checkpoints", "realization_states",
    "weak_forecast_evidence_snapshots", "weak_forecast_revision_updates",
    "production_initial_posterior_lineage",
    "posterior_commit_outbox", "posterior_outbox_execution_contracts",
    "event_fact_history", "event_fact_latest", "dynamic_three_leg_transition_plans",
    "dynamic_three_leg_risk_reservations", "posterior_execution_intents", "auto_trade_decisions",
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _tables(db: Path) -> dict[str, bool]:
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            actual = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return {name: False for name in REQUIRED_TABLES}
    return {name: name in actual for name in REQUIRED_TABLES}


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _runtime_monitor_deployment(root_path: Path) -> dict[str, Any]:
    repo_plist = root_path / "launchd/com.weather.monitor.daemon.plist"
    inspect_system = root_path == ROOT.resolve()
    installed_plist = (
        Path("/Library/LaunchDaemons/com.weather.monitor.daemon.plist")
        if inspect_system
        else root_path / "installed/com.weather.monitor.daemon.plist"
    )
    repo_sha, installed_sha = _sha256_file(repo_plist), _sha256_file(installed_plist)
    environment: dict[str, Any] = {}
    if installed_sha:
        try:
            with installed_plist.open("rb") as handle:
                parsed = plistlib.load(handle)
            environment = dict(parsed.get("EnvironmentVariables") or {})
        except (OSError, plistlib.InvalidFileException, ValueError):
            environment = {}
    launchctl_text = ""
    if inspect_system:
        try:
            result = subprocess.run(
                ["launchctl", "print", "system/com.weather.monitor.daemon"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            launchctl_text = str(result.stdout or "")
        except (OSError, subprocess.SubprocessError):
            launchctl_text = ""
    legacy_env_tokens = {
        "AMSC_ZUUU_LEGACY_POSTERIOR_ENQUEUE_ENABLED": "1",
        "PM_AMSC_ZUUU_SPECI_REALTIME_INGRESS_ENABLED": "1",
    }
    installed_legacy = [
        key for key, forbidden in legacy_env_tokens.items()
        if str(environment.get(key) or "").strip() == forbidden
    ]
    resident_legacy = [
        key for key, forbidden in legacy_env_tokens.items()
        if re.search(rf"{re.escape(key)}\s*=>\s*{re.escape(forbidden)}(?:\s|$)", launchctl_text)
    ]
    inspection_available = bool(installed_sha) if inspect_system else bool(installed_sha)
    matches_repo = bool(repo_sha and installed_sha and repo_sha == installed_sha)
    ready = bool(matches_repo and not installed_legacy and not resident_legacy)
    return {
        "inspection_required": inspect_system,
        "inspection_available": inspection_available,
        "repo_plist_sha256": repo_sha,
        "installed_plist": str(installed_plist),
        "installed_plist_sha256": installed_sha,
        "installed_plist_matches_repo": matches_repo,
        "installed_legacy_probability_env": installed_legacy,
        "resident_legacy_probability_env": resident_legacy,
        "monitor_running": "state = running" in launchctl_text,
        "ready": ready if inspect_system else (ready if inspection_available else True),
    }


def build_readiness(db_path: Path | str, *, root: Path | str = ROOT) -> dict[str, Any]:
    root_path, db = Path(root).resolve(), Path(db_path).expanduser().resolve()
    runtime_manifest_dependency_closure = (
        audit_runtime_manifest_dependency_closure(root_path)
        if root_path == ROOT.resolve()
        else {
            "schema": "production_probability_runtime_manifest_dependency_closure_v1",
            "ok": None,
            "state": "not_inspected_for_fixture_root",
            "read_only": True,
        }
    )
    table_state = _tables(db)
    runtime_monitor = _runtime_monitor_deployment(root_path)
    schema_plan = build_cutover_plan(db)
    activation_text = _read(root_path / "config/production_probability_runtime_activation.json")
    activation_default_disabled = '"state": "disabled"' in activation_text and '"places_orders": false' in activation_text and '"authorizes_clob": false' in activation_text
    try:
        activation_payload = json.loads(activation_text)
    except (TypeError, ValueError):
        activation_payload = {}
    activation_orderless = (
        isinstance(activation_payload, Mapping)
        and activation_payload.get("places_orders") is False
        and activation_payload.get("authorizes_clob") is False
    )
    activation_enabled = False
    activation_contract: Mapping[str, Any] = {}
    try:
        activation_contract = load_production_probability_activation(
            root_path / "config/production_probability_runtime_activation.json"
        )
        activation_enabled = True
    except ProductionProbabilityActivationError:
        pass
    current_runtime_sha256 = ""
    current_retirement_sha256 = ""
    if activation_enabled:
        try:
            current_runtime_sha256 = current_runtime_manifest_sha256(root_path)
        except OSError:
            current_runtime_sha256 = ""
        try:
            current_retirement_sha256 = current_retirement_receipt_sha256(root_path)
        except OSError:
            current_retirement_sha256 = ""
    authorized_runtime_sha256 = str(
        activation_payload.get("runtime_manifest_sha256") or ""
    )
    authorized_retirement_sha256 = str(
        activation_payload.get("legacy_writer_retirement_receipt_sha256") or ""
    )
    runtime_manifest_matches_activation = bool(
        activation_enabled
        and current_runtime_sha256
        and current_runtime_sha256 == authorized_runtime_sha256
    )
    retirement_receipt_matches_activation = bool(
        activation_enabled
        and current_retirement_sha256
        and current_retirement_sha256 == authorized_retirement_sha256
    )
    retirement_deployment_authorization = (
        resolve_retirement_deployment_authorization(
            activation=activation_contract,
            current_runtime_manifest_sha256=current_runtime_sha256,
            current_retirement_receipt_sha256=current_retirement_sha256,
            path=root_path / "config/production_probability_deployment_attestation.json",
        )
        if activation_enabled
        else {"authorized": False, "source": "none", "na_reason": "activation_not_enabled"}
    )
    retirement_receipt_authorized = bool(
        retirement_deployment_authorization.get("authorized") is True
    )
    writer = _read(root_path / "production_sequential_bayes_public_ssot_writer.py")
    host = _read(root_path / "production_sequential_bayes_transaction_b_host.py")
    compiler = _read(root_path / "production_sequential_bayes_candidate_compiler.py")
    p0_compiler = _read(root_path / "production_initial_posterior_checkpoint.py")
    p0_writer = _read(root_path / "production_initial_posterior_writer.py")
    activation_contract = _read(root_path / "production_probability_activation_contract.py")
    monitor = _read(root_path / "launchd/com.weather.monitor.daemon.plist")
    regular_poller = _read(root_path / "scripts/amsc_zuuu_metar_taf_poll_once.sh")
    speci_ingress = _read(root_path / "amsc_speci_realtime_ingress.py")
    production_p0_runtime = _read(root_path / "production_probability_p0_runtime.py")
    production_event_runtime = _read(root_path / "production_sequential_bayes_event_runtime.py")
    production_drain = _read(root_path / "production_canonical_event_drain_runtime.py")
    production_archive_ingress = _read(root_path / "production_amsc_archive_probability_ingress.py")
    weak_revision_writer = _read(root_path / "production_weak_forecast_revision_writer.py")
    weak_revision_runtime = _read(root_path / "production_weak_forecast_revision_event_runtime.py")
    speci_watch = _read(root_path / "scripts/watch_zuuu_speci_once.py")
    source_fact_writer_present = "AMSC_ZUUU_APPLY" in monitor and "scripts/polymarket_monitor_daemon.sh" in monitor
    regular_legacy_enqueue_enabled = bool(
        re.search(
            r"AMSC_ZUUU_LEGACY_POSTERIOR_ENQUEUE_ENABLED(?:</key>\s*<string>|\s*[:=]?\s*)1",
            monitor,
        )
    )
    regular_legacy_enqueue_implemented = (
        "LEGACY_POSTERIOR_ENQUEUE_ENABLED" in regular_poller
        and "enqueue_posterior" in regular_poller
    )
    speci_legacy_materializer_present = "materialize_amsc_posterior_task" in speci_ingress
    legacy_probability_writer_active = bool(
        regular_legacy_enqueue_enabled
        or speci_legacy_materializer_present
    )
    candidate_boundary = {
        "writer_rejects_shadow_db": "production_transaction_b_refuses_shadow_database" in writer,
        "writer_owns_single_transaction": "BEGIN IMMEDIATE" in writer and "requires_no_ambient_transaction" in writer,
        "writer_has_no_shadow_import": "sequential_bayes_public_ssot_shadow_writer" not in writer,
        "shared_candidate_math_is_explicitly_production_marked": "compile_production_transaction_b_candidate" in compiler and 'candidate_mode="production"' in compiler,
        "host_requires_explicit_production_candidate": 'value.get("candidate_mode")) != "production"' in host and 'value.get("shadow_only") is not False' in host,
        "host_has_no_network_or_clob_import": all(token not in host for token in ("import requests", "import httpx", "clob_order_service", "from signer", "import signer")),
        "p0_compiler_binds_prior_weak_and_strong": all(token in p0_compiler for token in (
            "activation_prior_artifact_mismatch", "activation_weak_artifact_mismatch",
            "activation_artifact_mismatch",
        )),
        "p0_writer_is_atomic_and_activation_guarded": all(token in p0_writer for token in (
            "BEGIN IMMEDIATE", "production_initial_checkpoint_requires_no_ambient_transaction",
            "validate_production_probability_activation",
        )),
        "activation_binds_formula_schemas_and_all_artifacts": all(token in activation_contract for token in (
            "prior_model_artifact_sha256", "weak_likelihood_artifact_sha256", "strong_lr_artifact_sha256",
            "bayes_formula_id", "composer_schema", "checkpoint_schema", "strong_update_schema",
            "public_ssot_schema", "runtime_manifest_sha256",
        )),
        "transaction_b_rejects_frozen_p0_context_drift": "production_transaction_b_frozen_p0_context_drift" in writer,
        "transaction_b_retry_cannot_rewind_latest": "update_latest=publish_latest" in writer and "latest_repointed" in writer,
        "weak_revision_is_atomic_orderless_replacement": all(token in weak_revision_writer + weak_revision_runtime for token in (
            "BEGIN IMMEDIATE", "compile_weak_forecast_revision_candidate", "places_orders", "weak_forecast_revision_updates",
        )),
        "production_p0_rebuilt_not_shadow_checkpoint_promoted": all(token in production_p0_runtime for token in (
            "build_production_p0_inputs", "compile_production_p0_candidate",
            "bind_full_physical_p0_prior_context_for_production",
            "bind_live_joint_weak_likelihood_for_production",
        )),
        "production_event_runtime_uses_canonical_event_and_transaction_b": all(token in production_event_runtime for token in (
            "load_canonical_observation_event", "build_canonical_event_transaction_b_candidate",
            "ProductionSequentialBayesTransactionBHost",
        )),
        "production_canonical_drain_is_amsc_only_and_orderless": all(token in production_drain for token in (
            "selected_source_id='zuuu_metar_raw_amsc'", "places_orders",
            "load_latest_persisted_checkpoint", "consumed_event_ids",
        )),
        "canonical_fact_precedes_probability_and_survives_probability_block": all(token in production_archive_ingress for token in (
            "apply_p2_canonical_amsc_forward_append", "drain_production_canonical_events",
            "canonical_retained_probability_blocked",
        )),
        "speci_production_probability_ingress_is_the_only_runtime_path": (
            "apply_production_amsc_archive_probability_ingress" in speci_watch
            and "amsc_speci_realtime_ingress" not in speci_watch
            and "--realtime-ingress" not in speci_watch
        ),
        "regular_metar_production_probability_lane_is_wired_default_enabled": all(token in regular_poller for token in (
            'PRODUCTION_PROBABILITY_UPDATE_ENABLED="${AMSC_ZUUU_PRODUCTION_PROBABILITY_UPDATE_ENABLED:-1}"',
            "apply_amsc_probability_fast_lane_from_plan.py",
        )),
    }
    pre_cutover_blockers = [key for key, value in candidate_boundary.items() if value is not True]
    if runtime_manifest_dependency_closure.get("ok") is False:
        pre_cutover_blockers.append("production_probability_runtime_manifest_dependency_closure_incomplete")
    if not activation_orderless:
        pre_cutover_blockers.append("production_probability_activation_must_be_orderless")
    dynamic_schema_ready = schema_plan.get("state") == "already_applied"
    p0_lineage_schema_ready = table_state.get("production_initial_posterior_lineage") is True
    schema_ready = dynamic_schema_ready and p0_lineage_schema_ready and all(table_state.values())
    production_cutover_blockers = list(pre_cutover_blockers)
    if not dynamic_schema_ready:
        production_cutover_blockers.append("dynamic_three_leg_execution_schema_not_applied")
    if not p0_lineage_schema_ready:
        production_cutover_blockers.append("production_initial_posterior_lineage_schema_not_applied")
    if not activation_enabled:
        production_cutover_blockers.append("production_probability_activation_not_enabled")
    elif not runtime_manifest_matches_activation:
        production_cutover_blockers.append("production_probability_runtime_manifest_drift")
    if activation_enabled and not retirement_receipt_authorized:
        production_cutover_blockers.append("production_probability_retirement_receipt_drift")
    if legacy_probability_writer_active:
        production_cutover_blockers.append("legacy_probability_writer_still_active")
    if runtime_monitor.get("ready") is not True:
        production_cutover_blockers.append("installed_or_resident_monitor_still_uses_legacy_probability_deployment")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not pre_cutover_blockers,
        "state": "pre_cutover_ready" if not pre_cutover_blockers else "pre_cutover_blocked",
        "db": str(db), "read_only": True,
        "guards": {"writes_archive_db": False, "writes_files": False, "restarts_services": False, "places_orders": False},
        "candidate_boundary": candidate_boundary,
        "runtime_manifest_dependency_closure": runtime_manifest_dependency_closure,
        "activation": {
            "path": str(root_path / "config/production_probability_runtime_activation.json"),
            "default_disabled": activation_default_disabled,
            "enabled": activation_enabled,
            "authorized_runtime_manifest_sha256": authorized_runtime_sha256,
            "current_runtime_manifest_sha256": current_runtime_sha256,
            "runtime_manifest_matches_current_files": runtime_manifest_matches_activation if activation_enabled else None,
            "authorized_retirement_receipt_sha256": authorized_retirement_sha256,
            "current_retirement_receipt_sha256": current_retirement_sha256,
            "retirement_receipt_matches_current_files": retirement_receipt_matches_activation if activation_enabled else None,
            "retirement_deployment_authorization": retirement_deployment_authorization,
        },
        "schema_migration": {"state": schema_plan.get("state"), "missing_actions": schema_plan.get("actions") or [], "all_required_tables_present": all(table_state.values()), "p0_lineage_schema_applied": p0_lineage_schema_ready, "table_state": table_state},
        "source_fact_ingress": {
            "amsc_source_fact_writer_present": source_fact_writer_present,
            "must_remain_enabled_after_probability_cutover": True,
        },
        "legacy_writer": {
            "regular_metar_legacy_posterior_enqueue_enabled": regular_legacy_enqueue_enabled,
            "regular_metar_legacy_posterior_enqueue_implemented": regular_legacy_enqueue_implemented,
            "speci_legacy_posterior_materializer_present": speci_legacy_materializer_present,
            "legacy_probability_writer_active": legacy_probability_writer_active,
            "amsc_source_fact_writer_is_not_a_probability_writer": True,
        },
        "runtime_monitor_deployment": runtime_monitor,
        "production_cutover": {"ok": not production_cutover_blockers, "blockers": sorted(set(production_cutover_blockers)), "requires_no_order_validation_before_activation": True, "authorizes_orders": False},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = build_readiness(args.db)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.summary else 2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
