#!/usr/bin/env python3
"""Build a content-addressed, fail-closed target cutover manifest.

This is a release *packet*, not an activation command.  It records the
read-only readiness result and offline evidence hashes while refusing to
claim readiness when the real database, artifacts, capital epoch, or service
domain are incomplete.  It never changes SQLite, launchd, activation state,
or order/gateway state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.database import DEFAULT_TARGET_DB
from bayes_basket_target import FORWARD_SCORECARD_SCHEMA
from scripts.bayes_basket_target_production_readiness import ROOT, audit


SCHEMA = "bayes_basket_target_release_manifest_v1"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_release_manifest.json"
EVIDENCE_FILES = (
    "bayes_basket_target/__init__.py",
    "bayes_basket_target/basket_kernel/contracts.py",
    "bayes_basket_target/basket_kernel/support.py",
    "bayes_basket_target/basket_kernel/delta_v2.py",
    "bayes_basket_target/runtime.py",
    "bayes_basket_target/delta_v2_runtime.py",
    "bayes_basket_target/delta_v2_training.py",
    "bayes_basket_target/delta_v2_settlement_label.py",
    "bayes_basket_target/forward_training_validation.py",
    "bayes_basket_target/execution_transition_runtime.py",
    "bayes_basket_target/readiness_scope.py",
    "bayes_basket_target/dependency_audit.py",
    "bayes_basket_target/model_arrival.py",
    "bayes_basket_target/calibration_evidence.py",
    "bayes_basket_target/et_history.py",
    "bayes_basket_target/resource_bounds.py",
    "bayes_basket_target/performance_budget.py",
    "bayes_basket_target/runtime_config.py",
    "bayes_basket_target/production_worker.py",
    "bayes_basket_target/native_model_decode.py",
    "bayes_basket_target/station_registry.py",
    "scripts/decode_bayes_basket_target_native_model_arrivals.py",
    "scripts/build_bayes_basket_target_model_arrival_evidence.py",
    "scripts/build_bayes_basket_target_calibration_evidence.py",
    "scripts/build_bayes_basket_target_et_history.py",
    "scripts/refresh_bayes_basket_target_evidence.py",
    "scripts/bayes_basket_target_post_et_refresh.py",
    "scripts/bayes_basket_target_production_facts_baseline.py",
    "config/trading/bayes_basket_target_station_lane_map_v1.json",
    "scripts/bayes_basket_target_resource_audit.py",
    "scripts/bayes_basket_target_performance_audit.py",
    "bayes_basket_target/activation.py",
    "config/trading/bayes_basket_target_production_no_live_activation_v1.json",
    "config/bayes_basket_target/performance_budget_v1.json",
    "config/bayes_basket_target/no_live_runtime_v1.json",
    "config/bayes_basket_target/model_arrival_capture_v1.json",
    "docs/architecture/BAYES_BASKET_TARGET_PRODUCTION_NO_LIVE.md",
    "scripts/audit_cash_phase_promotion_gate.py",
    "scripts/apply_bayes_basket_target_delta_v2_schema.py",
    "scripts/apply_delta_basket_score_v2_no_live_bootstrap.py",
    "scripts/accept_delta_basket_score_v2_no_live.py",
    "scripts/cutover_delta_basket_score_v2_no_live.sh",
    "scripts/bayes_basket_target_production_ssot_apply.py",
    "scripts/bayes_basket_target_migration_rehearsal.py",
    "scripts/probe_bayes_basket_target_writer_quiescence.py",
    "scripts/bayes_basket_target_hold_closure_audit.py",
    "scripts/bayes_basket_target_ingress_replay.py",
    "scripts/bayes_basket_target_promotion_boundary_audit.py",
    "scripts/bayes_basket_target_roadmap_compliance.py",
    "tests/test_bayes_basket_target_resource_bounds.py",
    "tests/test_bayes_basket_target_resource_audit.py",
    "tests/test_bayes_basket_target_performance_audit.py",
    "tests/test_bayes_basket_target_native_model_decode.py",
    "tests/test_bayes_basket_target_model_arrival.py",
    "tests/test_bayes_basket_target_model_arrival_evidence.py",
    "tests/test_bayes_basket_target_calibration_evidence.py",
    "tests/test_bayes_basket_target_et_history.py",
    "tests/test_bayes_basket_target_post_et_refresh.py",
    "tests/test_bayes_basket_target_production_facts_baseline.py",
    "tests/test_bayes_basket_target_execution_vector_repair.py",
    "tests/test_materialize_bayes_basket_target_market_cli.py",
    "scripts/reported_trajectory_no_dependency_audit.py",
    "bayes_basket_target/canonical_event_replay.py",
    "bayes_basket_target/hold_audit.py",
    "bayes_basket_target/promotion_boundary.py",
    "bayes_basket_target/forward_scorecard.py",
    "bayes_basket_target/forward_observation_store.py",
    "bayes_basket_target/execution_vector_repair.py",
    "bayes_basket_target/migration_quiescence.py",
    "scripts/bayes_basket_target_production_readiness.py",
    "scripts/materialize_bayes_basket_target_market.py",
    "output/audits/bayes_basket_target_production_readiness.json",
    "output/audits/bayes_basket_target_resource_audit.json",
    "output/audits/bayes_basket_target_native_model_arrival_decode.json",
    "output/audits/bayes_basket_target_model_arrival_evidence.json",
    "output/audits/bayes_basket_target_calibration_evidence.json",
    "output/audits/bayes_basket_target_et_history_evidence.json",
    "output/audits/bayes_basket_target_production_facts_baseline.json",
    "scripts/bayes_basket_target_production_chain_rehearsal.py",
    "output/audits/bayes_basket_target_fault_matrix.json",
    "output/audits/bayes_basket_target_strict_asof_replay.json",
    "output/audits/bayes_basket_target_time_replay.json",
    "output/audits/bayes_basket_target_migration_rehearsal.json",
    "output/audits/bayes_basket_target_current_ssot_v4_rehearsal.json",
    "output/audits/bayes_basket_target_cash_manifest_candidate_validation.json",
    "output/audits/bayes_basket_target_writer_quiescence.json",
    "output/audits/bayes_basket_target_hold_closure_audit.json",
    "output/audits/bayes_basket_target_ingress_replay.json",
    "output/audits/bayes_basket_target_promotion_boundary_audit.json",
    "output/audits/bayes_basket_target_roadmap_compliance.json",
    "output/audits/bayes_basket_target_focused_test_receipt.json",
    "output/audits/bayes_basket_target_production_closure_audit.json",
    "config/bayes_basket_target/runtime_config_isolation_v1.json",
    "output/audits/reported_trajectory_no_dependency_audit.json",
    "output/audits/station_policy_transfer_gate_m8_12.json",
    "output/audits/cash_phase_promotion_gate_m8_13.json",
    "output/audits/station_policy_candidate_bundle_v1.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relative in EVIDENCE_FILES:
        path = root / relative
        rows.append({
            "path": relative,
            "exists": path.is_file(),
            "sha256": _sha256(path) if path.is_file() else None,
        })
    return rows


def _manifest_readiness_snapshot(readiness: dict[str, Any]) -> dict[str, Any]:
    """Remove host-volatile telemetry from the content-addressed packet.

    The standalone readiness receipt retains exact free-space telemetry.  A
    release packet must remain reproducible for identical inputs, so the
    byte-count probe is not copied into the manifest hash basis.
    """

    snapshot = json.loads(json.dumps(readiness, ensure_ascii=False, sort_keys=True))
    budget = snapshot.get("resource_budget")
    if isinstance(budget, dict):
        budget.pop("free_bytes", None)
        # RSS is host/process volatile telemetry. Keep it in the standalone
        # readiness and resource receipts, but never let it destabilize a
        # content-addressed release packet for identical inputs.
        budget.pop("process_rss_bytes", None)
    # The audit clock is useful in the standalone readiness receipt, but it is
    # host-time telemetry rather than release input.  Exclude it from the
    # content-addressed packet so two builds with the same explicit inputs and
    # generation time remain byte-for-byte stable.
    target_inputs = snapshot.get("target_inputs")
    if isinstance(target_inputs, dict):
        target_inputs.pop("decision_clock_utc", None)
    # Future-day residue is a live-clock audit.  Its classification is useful
    # for the standalone readiness receipt, but both the clock and the
    # receipt's self-hash are derived from that clock.  Keeping either in the
    # content-addressed release basis makes two builds with identical inputs
    # produce different manifests milliseconds apart.
    future_residue = snapshot.get("future_day_residue")
    if isinstance(future_residue, dict):
        future_residue.pop("as_of_utc", None)
        future_residue.pop("audit_sha256", None)
    return snapshot


def _forward_scorecard_release_ready(value: Any) -> bool:
    """Require a self-hashed, non-empty public forward scorecard receipt."""

    if not isinstance(value, dict):
        return False
    if value.get("schema") != "bayes_basket_target_forward_scorecard_gate_v1":
        return False
    if value.get("ready") is not True:
        return False
    if value.get("ready_for_cash_scorecard") is not True:
        return False
    if value.get("ready_for_station_scorecard") is not True:
        return False
    receipt = value.get("receipt")
    if not isinstance(receipt, dict):
        return False
    if receipt.get("schema") != FORWARD_SCORECARD_SCHEMA:
        return False
    if receipt.get("production_no_live") is not True:
        return False
    if receipt.get("read_only") is not True:
        return False
    if receipt.get("calls_order_gateway") is not False:
        return False
    if int(receipt.get("orders_submitted") or 0) != 0:
        return False
    counts = receipt.get("counts")
    if not isinstance(counts, dict) or int(counts.get("fact_rows_evaluated") or 0) <= 0:
        return False
    if receipt.get("blockers"):
        return False
    supplied = str(receipt.get("content_sha256") or "")
    if len(supplied) != 64:
        return False
    identity = {key: item for key, item in receipt.items() if key != "content_sha256"}
    return supplied == content_sha256(identity)


def build_manifest(
    *,
    root: Path = ROOT,
    db: Path | None = None,
    activation: Path | None = None,
    manifest: Path | None = None,
    generated_at_utc: str = "",
    probe_services: bool = True,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    db_path = (db or (root / "data" / "bayes_basket_target_ssot.db")).expanduser().resolve()
    activation_path = (
        activation
        or root / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
    ).expanduser().resolve()
    ws_manifest_path = (
        manifest or root / "data/bayes_basket_target_official_ws_manifest.json"
    ).expanduser().resolve()
    generated = generated_at_utc.strip() or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    readiness = audit(
        db=db_path,
        activation_path=activation_path,
        manifest_path=ws_manifest_path,
        probe_services=probe_services,
        as_of_utc=generated,
    )
    forward_scorecard = readiness.get("forward_scorecard")
    blockers = list(readiness.get("blockers") or [])
    formal_promotion_blockers = list(
        readiness.get("formal_promotion_blockers") or []
    )
    if not _forward_scorecard_release_ready(forward_scorecard):
        if "target_forward_scorecard_incomplete" not in formal_promotion_blockers:
            formal_promotion_blockers.append(
                "target_forward_scorecard_incomplete"
            )
    production_no_live_ready = bool(readiness["ok"] and not blockers)
    formal_promotion_ready = bool(
        production_no_live_ready and not formal_promotion_blockers
    )
    body = {
        "schema": SCHEMA,
        "release_name": "bayes_basket_target_production_no_live",
        "generated_at_utc": generated,
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "production_mutation": False,
        "release_state": (
            "READY_FOR_OPERATOR_CUTOVER"
            if production_no_live_ready
            else "BLOCKED_NOT_RELEASE"
        ),
        "readiness_ok": production_no_live_ready,
        "production_no_live_ready": production_no_live_ready,
        "formal_promotion_ready": formal_promotion_ready,
        "blockers": sorted(set(blockers)),
        "formal_promotion_blockers": sorted(
            set(formal_promotion_blockers)
        ),
        "readiness_receipt": _manifest_readiness_snapshot(readiness),
        "forward_scorecard": forward_scorecard,
        "forward_scorecard_receipt_sha256": (
            forward_scorecard.get("receipt_content_sha256")
            if isinstance(forward_scorecard, dict)
            else None
        ),
        "evidence": _evidence(root),
        "operator_gates": {
            "explicit_pause_marker_required": True,
            "sqlite_online_backup_required": True,
            "legacy_auto_trade_decisions_must_be_renamed_and_retained": True,
            "capital_balance_and_epoch_must_be_explicitly_paired": True,
            "system_domain_three_service_cutover_required": True,
            "live_submit_authorization_forbidden": True,
        },
        "required_before_mutation": [
            "resolve_readiness_blockers",
            "operator_supplies_pause_window_and_capital_epoch",
            "run_controlled_target_ssot_migration_with_new_backup_path",
            "materialize_promoted_target_artifacts_and_first_inputs",
            "run_real_database_production_no_live_acceptance_twice",
        ],
    }
    body["manifest_sha256"] = content_sha256(body)
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--activation", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--generated-at-utc", default="")
    parser.add_argument("--no-service-probe", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build_manifest(
        root=args.root,
        db=args.db,
        activation=args.activation,
        manifest=args.manifest,
        generated_at_utc=args.generated_at_utc,
        probe_services=not args.no_service_probe,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema": result["schema"],
        "release_state": result["release_state"],
        "readiness_ok": result["readiness_ok"],
        "blocker_count": len(result["blockers"]),
        "manifest_sha256": result["manifest_sha256"],
        "output": str(output),
        "live_submit_authorized": result["live_submit_authorized"],
        "places_orders": result["places_orders"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["readiness_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
