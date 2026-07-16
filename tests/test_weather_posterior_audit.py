import weather_posterior_audit as audit


def _reports(*, ready: bool = True):
    manifest = {
        "schema": "production_probability_runtime_manifest_v2_reported_x_dependency_closure",
        "ok": True,
    }
    readiness = {
        "ok": ready,
        "blockers": [] if ready else ["successor_runtime_manifest_drift"],
        "evidence": {
            "runtime_manifest_dependency_closure": manifest,
            "pending_canonical_event_ids": [] if ready else ["event-1"],
            "posterior_checkpoint_id": "checkpoint-1",
            "reported_x_lower_bound_c": 38,
            "strong_likelihood_update_identity": {
                "likelihood_update_kind": "cumulative_evidence_replacement_delta_log_lr_v1"
            },
        },
    }
    uniqueness = {
        "ok": ready,
        "blockers": [] if ready else [{"blocker": "reported_x_successor_runtime_manifest_drift"}],
        "checks": {
            "reported_x_single_production_mainline": True,
            "reported_x_runtime_identity_aligned": ready,
        },
    }
    sequential = {
        "ok": ready,
        "blockers": [] if ready else ["forward_canonical_event_uncommitted_after_three_seconds:event-1"],
        "evidence": {"pending_canonical_event_ids": [] if ready else ["event-1"]},
        "forward_three_second_slo": {"state": "passed" if ready else "failed"},
    }
    documents = {"ok": True, "blockers": []}
    return readiness, uniqueness, sequential, documents


def test_weather_posterior_audit_accepts_only_current_reported_x_chain():
    readiness, uniqueness, sequential, documents = _reports()
    report = audit.build_report(
        readiness=readiness,
        uniqueness=uniqueness,
        sequential=sequential,
        document_authority=documents,
    )

    assert report["ok"] is True
    assert report["state"] == "ready"
    assert report["checks"]["reported_x_single_production_mainline"] is True
    assert report["checks"]["cumulative_replacement_identity"] is True
    assert report["reported_x_lower_bound_c"] == 38
    assert report["guards"]["places_orders"] is False


def test_weather_posterior_audit_blocks_manifest_drift_and_pending_canonical():
    readiness, uniqueness, sequential, documents = _reports(ready=False)
    report = audit.build_report(
        readiness=readiness,
        uniqueness=uniqueness,
        sequential=sequential,
        document_authority=documents,
    )

    assert report["ok"] is False
    assert report["state"] == "blocked"
    assert report["pending_canonical_event_ids"] == ["event-1"]
    assert "reported_x_successor_runtime_manifest_drift" in report["blockers"]
    assert report["checks"]["activation_runtime_identity_aligned"] is False
