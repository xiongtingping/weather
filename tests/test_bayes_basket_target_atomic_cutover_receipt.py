from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from bayes_basket_target.atomic_cutover_receipt import (
    AtomicCutoverReceiptError,
    FORWARD_OOS_BOUNDARY,
    PROTECTED_SERVICES,
    REQUIRED_STAGES,
    STAGE_SCHEMA,
    build_cutover_receipt,
    build_idempotent_replay_receipt,
    build_release_identity,
    content_sha256,
    delete_recovery_after_success,
    finalize_recovery_cleanup_receipt,
    recovery_file_identity,
    seal_stage,
    source_stage_receipt,
    validate_cutover_receipt,
    validate_sealed_receipt,
    write_receipt_if_changed,
)


def _stage(stage: str, *, state: str = "PASS") -> dict:
    facts = {}
    if stage == "writer_quiescence":
        facts = {"quiescent": True}
    elif stage == "resident_restart":
        facts = {"target_writer_restarted": True}
    elif stage == "protected_pid_postcheck":
        facts = {"pid_stability": True}
    return seal_stage(
        stage,
        {
            "schema": f"fixture_{stage}",
            "state": state,
            "production_no_live": True,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            **facts,
        },
    )


def _complete(tmp_path: Path) -> tuple[dict, dict, Path]:
    recovery_path = tmp_path / "bayes_basket_target_ssot.pre_test.backup.db"
    recovery_path.write_bytes(b"bounded-recovery")
    recovery = recovery_file_identity(recovery_path)
    recovery["deleted_after_success"] = True
    stages = {stage: _stage(stage) for stage in REQUIRED_STAGES}
    release = build_release_identity(
        station_ids=("EGLC", "RKPK"),
        schema_sha256="1" * 64,
        activation_sha256="2" * 64,
        artifact_sha256="3" * 64,
    )
    receipt = build_cutover_receipt(
        station_ids=("EGLC", "RKPK"),
        stage_receipts=stages,
        release=release,
        protected_pids_before={service: index + 100 for index, service in enumerate(PROTECTED_SERVICES)},
        protected_pids_after={service: index + 100 for index, service in enumerate(PROTECTED_SERVICES)},
        target_pid_before=200,
        target_pid_after=201,
        recovery=recovery,
        target_database_fingerprint={
            "device": 1,
            "inode": 2,
            "size": 3,
            "mtime_ns": 4,
        },
    )
    return receipt, release, recovery_path


def test_complete_receipt_is_content_addressed_and_forward_oos_is_not_no_live_gate(tmp_path):
    receipt, _, _ = _complete(tmp_path)
    validate_cutover_receipt(receipt)
    assert receipt["forward_oos_boundary"] == FORWARD_OOS_BOUNDARY
    assert receipt["writes"] == 1
    assert receipt["legacy_fallback_used"] is False
    assert receipt["stage_hashes"]

    path = tmp_path / "cutover.json"
    write_receipt_if_changed(path, receipt)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    validate_cutover_receipt(persisted)

    tampered = dict(receipt)
    tampered["station_scope"] = {
        **receipt["station_scope"],
        "station_ids": ["RKPK", "EGLC"],
    }
    with pytest.raises(AtomicCutoverReceiptError, match="content_sha256"):
        validate_cutover_receipt(tampered)


def test_missing_stage_and_protected_pid_drift_fail_closed(tmp_path):
    receipt, release, _ = _complete(tmp_path)
    stages = {
        stage: _stage(stage)
        for stage in REQUIRED_STAGES
        if stage != "wal_checkpoint"
    }
    with pytest.raises(AtomicCutoverReceiptError, match="stage_missing"):
        build_cutover_receipt(
            station_ids=("EGLC", "RKPK"),
            stage_receipts=stages,
            release=release,
            protected_pids_before=receipt["protected_service_pids_before"],
            protected_pids_after=receipt["protected_service_pids_after"],
            target_pid_before=receipt["target_service_pid_before"],
            target_pid_after=receipt["target_service_pid_after"],
            recovery=receipt["recovery"],
        )

    bad_release = dict(release)
    bad_release["activation_sha256"] = "9" * 64
    with pytest.raises(AtomicCutoverReceiptError, match="release_sha256"):
        build_cutover_receipt(
            station_ids=("EGLC", "RKPK"),
            stage_receipts={stage: _stage(stage) for stage in REQUIRED_STAGES},
            release=bad_release,
            protected_pids_before=receipt["protected_service_pids_before"],
            protected_pids_after=receipt["protected_service_pids_after"],
            target_pid_before=receipt["target_service_pid_before"],
            target_pid_after=receipt["target_service_pid_after"],
            recovery=receipt["recovery"],
        )
    with pytest.raises(AtomicCutoverReceiptError, match="pid_drift"):
        build_cutover_receipt(
            station_ids=("EGLC", "RKPK"),
            stage_receipts={stage: _stage(stage) for stage in REQUIRED_STAGES},
            release=release,
            protected_pids_before=receipt["protected_service_pids_before"],
            protected_pids_after={
                **receipt["protected_service_pids_after"],
                "com.weather.market_ws": 999,
            },
            target_pid_before=receipt["target_service_pid_before"],
            target_pid_after=receipt["target_service_pid_after"],
            recovery=receipt["recovery"],
        )


def test_nested_old_fallback_and_forward_oos_boundary_are_rejected():
    with pytest.raises(AtomicCutoverReceiptError, match="boundary"):
        seal_stage(
            "preflight",
            {
                "schema": "fixture",
                "state": "PASS",
                "production_no_live": True,
                "calls_order_gateway": False,
                "live_submit_authorized": False,
                "orders_submitted": 0,
                "nested": {"legacy_fallback_used": True},
            },
        )
    receipt = _stage("preflight")
    receipt["forward_oos_boundary"] = {
        "blocks_production_no_live": True,
        "blocks_formal_promotion_or_live": True,
    }
    receipt["content_sha256"] = content_sha256(
        {key: value for key, value in receipt.items() if key != "content_sha256"}
    )
    with pytest.raises(AtomicCutoverReceiptError, match="forward_oos"):
        validate_sealed_receipt(receipt, schema=STAGE_SCHEMA, stage="preflight")


def test_repeated_same_release_is_deterministic_zero_write(tmp_path):
    receipt, release, _ = _complete(tmp_path)
    replay = build_idempotent_replay_receipt(
        receipt,
        station_ids=("RKPK", "EGLC"),
        release_sha256=release["release_sha256"],
    )
    assert replay["state"] == "NOOP_IDEMPOTENT"
    assert replay["writes"] == 0
    assert replay["idempotent_replay"] is True
    assert replay["release"] == receipt["release"]
    assert replay["target_database_fingerprint_after"] == receipt[
        "target_database_fingerprint_after"
    ]
    assert replay["protected_service_pids_after"] == receipt[
        "protected_service_pids_after"
    ]
    validate_cutover_receipt(replay)
    replay_again = build_idempotent_replay_receipt(
        receipt,
        station_ids=("EGLC", "RKPK"),
        release_sha256=release["release_sha256"],
    )
    assert replay_again == replay
    advanced_fingerprint = {
        **receipt["target_database_fingerprint_after"],
        "size": int(
            receipt["target_database_fingerprint_after"]["size"]
        ) + 4096,
        "mtime_ns": int(
            receipt["target_database_fingerprint_after"]["mtime_ns"]
        ) + 1,
    }
    resident_advanced = build_idempotent_replay_receipt(
        receipt,
        station_ids=("EGLC", "RKPK"),
        release_sha256=release["release_sha256"],
        target_database_fingerprint_after=advanced_fingerprint,
        target_service_pid_after=202,
    )
    assert resident_advanced["writes"] == 0
    assert resident_advanced["target_database_fingerprint_after"] == (
        advanced_fingerprint
    )
    assert resident_advanced["target_service_pid_after"] == 202
    validate_cutover_receipt(resident_advanced)
    with pytest.raises(AtomicCutoverReceiptError, match="release_mismatch"):
        build_idempotent_replay_receipt(
            receipt,
            station_ids=("EGLC", "RKPK"),
            release_sha256="f" * 64,
        )


def test_validator_rejects_resigned_scope_mode_release_and_pid_tamper(tmp_path):
    receipt, _, _ = _complete(tmp_path)
    for mutation in (
        lambda value: value["station_scope"].update({"mode": "single_station"}),
        lambda value: value["release"].update({"artifact_role": "activation"}),
        lambda value: value["protected_service_pids_after"].update(
            {"com.weather.market_ws": 999}
        ),
    ):
        tampered = json.loads(json.dumps(receipt))
        mutation(tampered)
        tampered["content_sha256"] = content_sha256({
            key: value
            for key, value in tampered.items()
            if key != "content_sha256"
        })
        with pytest.raises(AtomicCutoverReceiptError):
            validate_cutover_receipt(tampered)


def test_recovery_cleanup_happens_only_after_durable_retained_receipt(tmp_path):
    receipt, release, recovery_path = _complete(tmp_path)
    retained_recovery = recovery_file_identity(recovery_path)
    retained = build_cutover_receipt(
        station_ids=("EGLC", "RKPK"),
        stage_receipts={stage: _stage(stage) for stage in REQUIRED_STAGES},
        release=release,
        protected_pids_before=receipt["protected_service_pids_before"],
        protected_pids_after=receipt["protected_service_pids_after"],
        target_pid_before=200,
        target_pid_after=201,
        recovery=retained_recovery,
        target_database_fingerprint=receipt[
            "target_database_fingerprint_after"
        ],
    )
    validate_cutover_receipt(retained)
    assert retained["state"] == "COMPLETED_RECOVERY_RETAINED"
    deleted = delete_recovery_after_success(
        recovery_path,
        expected_sha256=retained_recovery["sha256"],
    )
    cleanup_stage = seal_stage(
        "recovery_cleanup",
        {
            "schema": "fixture_cleanup",
            "state": "CLEANED",
            "recovery": deleted,
            "production_no_live": True,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        },
    )
    final = finalize_recovery_cleanup_receipt(
        retained,
        recovery=deleted,
        recovery_stage_receipt=cleanup_stage,
    )
    validate_cutover_receipt(final)
    assert final["state"] == "COMPLETED"


def test_recovery_is_bounded_and_deletes_only_exact_file(tmp_path):
    recovery_path = tmp_path / "bayes_basket_target_ssot.pre_test.backup.db"
    recovery_path.write_bytes(b"recovery")
    other = tmp_path / "keep.db"
    other.write_bytes(b"keep")
    identity = recovery_file_identity(recovery_path)
    deleted = delete_recovery_after_success(
        recovery_path,
        expected_sha256=identity["sha256"],
    )
    assert deleted["deleted_after_success"] is True
    assert not recovery_path.exists()
    assert other.exists()

    link = tmp_path / "recovery-link.db"
    link.symlink_to(other)
    with pytest.raises(AtomicCutoverReceiptError, match="symlink"):
        recovery_file_identity(link)


def test_receipt_write_replay_is_zero_write(tmp_path):
    path = tmp_path / "receipt.json"
    value = _stage("preflight")
    assert write_receipt_if_changed(path, value) is True
    before = path.stat().st_mtime_ns
    assert write_receipt_if_changed(path, value) is False
    assert path.stat().st_mtime_ns == before


def test_source_receipt_hashes_raw_evidence(tmp_path):
    path = tmp_path / "source.json"
    payload = {
        "schema": "source",
        "state": "PASS",
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    receipt = source_stage_receipt("preflight", path)
    validate_sealed_receipt(receipt, schema=STAGE_SCHEMA, stage="preflight")
    assert receipt["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
