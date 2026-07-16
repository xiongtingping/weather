from __future__ import annotations

import json

from amsc_p10_shadow_source_sidecar import capture_amsc_p10_shadow_source_attempt
from p10_amsc_source_sidecar_acceptance import audit_p10_amsc_source_sidecar


PAYLOAD = {
    "code": 200,
    "data": [
        {"cccc": "ZUUU", "content": "METAR ZUUU 051430Z 18003MPS CAVOK 32/20 Q1001 NOSIG="},
    ],
}


def test_acceptance_audit_waits_cleanly_before_first_forward_bundle(tmp_path) -> None:
    result = audit_p10_amsc_source_sidecar(
        raw_archive_dir=tmp_path / "raw",
        inbox_dir=tmp_path / "inbox",
        status_path=tmp_path / "status.json",
    )

    assert result["ok"] is True
    assert result["state"] == "waiting_first_forward_capture"
    assert result["forward_evidence_ready"] is False


def test_acceptance_audit_revalidates_manifest_bound_forward_bundle(tmp_path) -> None:
    raw = tmp_path / "raw"
    inbox = tmp_path / "inbox"
    status = tmp_path / "status.json"
    captured = capture_amsc_p10_shadow_source_attempt(
        payload=PAYLOAD,
        transport={
            "poll_started_at": "2026-07-05T14:30:55Z",
            "response_started_at": "2026-07-05T14:30:56Z",
            "response_completed_at": "2026-07-05T14:31:00Z",
        },
        raw_archive_dir=raw,
        inbox_dir=inbox,
        report_count=6,
    )
    status.write_text(json.dumps(captured), encoding="utf-8")

    result = audit_p10_amsc_source_sidecar(
        raw_archive_dir=raw,
        inbox_dir=inbox,
        status_path=status,
    )

    assert captured["state"] == "published"
    assert result["ok"] is True
    assert result["state"] == "forward_evidence_ready"
    assert result["accepted_bundle_count"] == 1
    assert result["latest_sidecar_status_binding_state"] == "matched"
    candidate = result["accepted_bundles"][0]["arrival_candidates"][0]
    assert candidate["first_actionable_seen_at"] == "2026-07-05T14:31:00Z"
    assert result["boundary"]["creates_arrival"] is False


def test_acceptance_audit_keeps_raw_root_mismatched_status_as_diagnostic_only(tmp_path) -> None:
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps({"state": "published", "raw_archive_dir": "/legacy/data/amsc_raw_history"}),
        encoding="utf-8",
    )

    result = audit_p10_amsc_source_sidecar(
        raw_archive_dir=tmp_path / "p10-raw",
        inbox_dir=tmp_path / "inbox",
        status_path=status,
    )

    assert result["latest_sidecar_status_binding_state"] == "mismatched_raw_archive"
    assert result["latest_sidecar_status_for_current_raw_archive"] is False
    assert result["latest_sidecar_status"] == {}
    assert result["unbound_latest_sidecar_status"]["state"] == "published"
