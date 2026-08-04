from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import scripts.build_bayes_basket_target_release_manifest as release_module
from bayes_basket_target import FORWARD_SCORECARD_SCHEMA, build_forward_scorecard
from scripts.build_bayes_basket_target_release_manifest import build_manifest


ROOT = Path(__file__).resolve().parents[1]
ACTIVATION = ROOT / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"


def test_release_manifest_is_blocked_and_never_authorizes_orders(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db):
        pass
    ws = tmp_path / "quotes.json"
    ws.write_text("{}\n", encoding="utf-8")
    result = build_manifest(
        root=ROOT,
        db=db,
        activation=ACTIVATION,
        manifest=ws,
        generated_at_utc="2026-07-23T00:00:00Z",
        probe_services=False,
    )
    assert result["release_state"] == "BLOCKED_NOT_RELEASE"
    assert result["readiness_ok"] is False
    assert result["live_submit_authorized"] is False
    assert result["places_orders"] is False
    assert result["calls_order_gateway"] is False
    assert result["forward_scorecard"]["state"] == "BLOCKED"
    assert "target_forward_scorecard_incomplete" not in result["blockers"]
    assert "target_forward_scorecard_incomplete" not in (
        result["formal_promotion_blockers"]
    )
    assert result["formal_promotion_ready"] is False
    assert result["manifest_sha256"]


def test_release_manifest_hash_is_stable_for_same_inputs_and_time(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db):
        pass
    ws = tmp_path / "quotes.json"
    ws.write_text("{}\n", encoding="utf-8")
    kwargs = {
        "root": ROOT,
        "db": db,
        "activation": ACTIVATION,
        "manifest": ws,
        "generated_at_utc": "2026-07-23T00:00:00Z",
        "probe_services": False,
    }
    first = build_manifest(**kwargs)
    second = build_manifest(**kwargs)
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_release_manifest_carries_offline_evidence_but_not_as_live_authority(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db):
        pass
    ws = tmp_path / "quotes.json"
    ws.write_text("{}\n", encoding="utf-8")
    result = build_manifest(
        root=ROOT,
        db=db,
        activation=ACTIVATION,
        manifest=ws,
        generated_at_utc="2026-07-23T00:00:00Z",
        probe_services=False,
    )
    evidence = {row["path"]: row for row in result["evidence"]}
    assert evidence["bayes_basket_target/basket_kernel/contracts.py"]["exists"] is True
    assert evidence["bayes_basket_target/dependency_audit.py"]["exists"] is True
    assert evidence["bayes_basket_target/native_model_decode.py"]["exists"] is True
    assert evidence["bayes_basket_target/model_arrival.py"]["exists"] is True
    assert evidence["bayes_basket_target/calibration_evidence.py"]["exists"] is True
    assert evidence["scripts/decode_bayes_basket_target_native_model_arrivals.py"]["exists"] is True
    assert evidence["scripts/build_bayes_basket_target_model_arrival_evidence.py"]["exists"] is True
    assert evidence["scripts/build_bayes_basket_target_calibration_evidence.py"]["exists"] is True
    assert evidence["output/audits/bayes_basket_target_native_model_arrival_decode.json"]["exists"] is True
    assert evidence["output/audits/bayes_basket_target_model_arrival_evidence.json"]["exists"] is True
    assert evidence["output/audits/bayes_basket_target_fault_matrix.json"]["exists"] is True
    assert evidence["output/audits/bayes_basket_target_strict_asof_replay.json"]["exists"] is True
    assert evidence["output/audits/bayes_basket_target_resource_audit.json"]["exists"] is True
    assert evidence["scripts/bayes_basket_target_migration_rehearsal.py"]["exists"] is True
    assert evidence["output/audits/bayes_basket_target_migration_rehearsal.json"]["exists"] is True
    assert evidence["output/audits/bayes_basket_target_current_ssot_v4_rehearsal.json"]["exists"] is True
    assert evidence["output/audits/bayes_basket_target_cash_manifest_candidate_validation.json"]["exists"] is True
    assert evidence["bayes_basket_target/forward_scorecard.py"]["exists"] is True
    assert evidence["bayes_basket_target/forward_observation_store.py"]["exists"] is True
    assert evidence["bayes_basket_target/execution_vector_repair.py"]["exists"] is True
    assert evidence["tests/test_bayes_basket_target_execution_vector_repair.py"]["exists"] is True
    assert "output/audits/bayes_basket_target_focused_test_receipt.json" in evidence
    assert evidence["output/audits/bayes_basket_target_production_closure_audit.json"]["exists"] is True
    assert evidence["config/bayes_basket_target/runtime_config_isolation_v1.json"]["exists"] is True
    assert result["operator_gates"]["live_submit_authorization_forbidden"] is True
    assert result["operator_gates"][
        "user_domain_three_service_cutover_required"
    ] is True
    assert result["operator_gates"]["system_domain_residue_forbidden"] is True
    assert result["operator_gates"][
        "passwordless_daily_service_control_required"
    ] is True
    assert result["operator_gates"][
        "existing_capital_balance_and_epoch_must_be_bound"
    ] is True
    assert "capital_balance_and_epoch_must_be_explicitly_paired" not in (
        result["operator_gates"]
    )
    assert "system_domain_three_service_cutover_required" not in (
        result["operator_gates"]
    )
    assert "verify_existing_live_pause_and_capital_epoch" in (
        result["required_before_mutation"]
    )
    assert "operator_supplies_pause_window_and_capital_epoch" not in (
        result["required_before_mutation"]
    )


def test_forward_scorecard_is_exported_from_package_public_interface() -> None:
    from bayes_basket_target.forward_scorecard import SCHEMA

    assert FORWARD_SCORECARD_SCHEMA == SCHEMA
    assert build_forward_scorecard.__name__ == "build_forward_scorecard"


def test_release_manifest_no_live_can_pass_before_forward_oos(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db):
        pass
    ws = tmp_path / "quotes.json"
    ws.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(
        release_module,
        "audit",
        lambda **_: {
            "ok": True,
            "blockers": [],
            "forward_scorecard": {
                "schema": "bayes_basket_target_forward_scorecard_gate_v1",
                "state": "BLOCKED",
                "ready": False,
                "ready_for_cash_scorecard": False,
                "ready_for_station_scorecard": False,
                "receipt": None,
            },
        },
    )

    result = build_manifest(
        root=ROOT,
        db=db,
        activation=ACTIVATION,
        manifest=ws,
        generated_at_utc="2026-07-27T00:00:00Z",
        probe_services=False,
    )

    assert result["readiness_ok"] is True
    assert result["production_no_live_ready"] is True
    assert result["release_state"] == "READY_FOR_OPERATOR_CUTOVER"
    assert "target_forward_scorecard_incomplete" not in result["blockers"]
    assert result["formal_promotion_ready"] is True
    assert "target_forward_scorecard_incomplete" not in (
        result["formal_promotion_blockers"]
    )


def test_release_manifest_keeps_gateway_violation_as_no_live_blocker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db):
        pass
    ws = tmp_path / "quotes.json"
    ws.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        release_module,
        "audit",
        lambda **_: {
            "ok": False,
            "blockers": ["forward_scorecard_gateway_boundary_invalid"],
            "formal_promotion_blockers": [],
            "forward_scorecard": {
                "schema": "bayes_basket_target_forward_scorecard_gate_v1",
                "state": "BLOCKED",
                "ready": False,
                "ready_for_cash_scorecard": False,
                "ready_for_station_scorecard": False,
                "calls_order_gateway": True,
                "orders_submitted": 0,
                "receipt": None,
            },
        },
    )

    result = build_manifest(
        root=ROOT,
        db=db,
        activation=ACTIVATION,
        manifest=ws,
        generated_at_utc="2026-07-27T00:00:00Z",
        probe_services=False,
    )

    assert result["production_no_live_ready"] is False
    assert result["release_state"] == "BLOCKED_NOT_RELEASE"
    assert "forward_scorecard_gateway_boundary_invalid" in result["blockers"]
