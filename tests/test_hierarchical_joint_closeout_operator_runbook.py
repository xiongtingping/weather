from scripts import hierarchical_joint_closeout_operator_runbook as runbook


def _step(name, *, command=None, writes=False, manifest=False, backup=False, restart=False, phrase=""):
    return {
        "step": name,
        "phase": "source",
        "category": "archive_db_apply" if writes else "read_only_preflight",
        "command": command or f"cmd:{name}",
        "required": True,
        "read_only_command": not writes and not manifest and not backup and not restart,
        "writes_archive_db": writes,
        "writes_manifest_file": manifest,
        "creates_backup": backup,
        "restarts_launchd": restart,
        "requires_operator_confirmation_phrase": bool(phrase),
        "confirmation_phrase": phrase,
        "blocked_until": [],
        "execution_allowed_now": not writes,
    }


def _awc_runbook(*, ready=False):
    return {
        "schema": "awc_legacy_terminal_closeout_operator_runbook_v1",
        "ok": True,
        "read_only": True,
        "dry_run": True,
        "ready_for_controlled_apply_now": ready,
        "start_blockers": [] if ready else ["awc_legacy_terminal_closeout_decision_pending"],
        "operator_sequence": [
            _step("emit_review_report"),
            _step("emit_decision_manifest_template", manifest=True),
            _step("operator_reviews_decision_manifest", manifest=True),
            _step("audit_reviewed_decision_manifest"),
            _step("pause_writer_services", restart=True),
            _step("verify_writer_quiescence"),
            _step("create_fresh_backup", backup=True),
            _step(
                "apply_awc_legacy_terminal_closeout",
                writes=True,
                phrase="APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT",
            ),
            _step("verify_awc_terminal_coverage"),
            _step("resume_writer_services", restart=True),
        ],
        "confirmation_phrases": {
            "apply_awc_legacy_terminal_closeout": "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT"
        },
        "guards": {"writes_archive_db": False},
    }


def _identity_runbook(*, ready=False):
    return {
        "schema": "production_identity_closeout_operator_runbook_v1",
        "ok": True,
        "read_only": True,
        "dry_run": True,
        "ready_for_controlled_apply_now": ready,
        "start_blockers": [] if ready else ["writer_quiescence_not_satisfied"],
        "operator_sequence": [
            _step("pause_writer_services", restart=True),
            _step("materialize_reviewed_duplicate_manifest", manifest=True),
            _step("audit_reviewed_duplicate_manifest"),
            _step("review_duplicate_apply_contract"),
            _step("review_identity_audit_index_contract"),
            _step("run_identity_audit_index_temp_drill"),
            _step("run_identity_closeout_temp_drill"),
            _step("create_shared_fresh_backup", backup=True),
            _step("verify_writer_quiescence"),
            _step(
                "apply_identity_audit_indexes",
                writes=True,
                phrase="APPLY_PRODUCTION_IDENTITY_AUDIT_INDEXES",
            ),
            _step(
                "apply_duplicate_fact_snapshot_repair",
                writes=True,
                phrase="APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR",
            ),
            _step("verify_duplicate_repair"),
            _step("review_identity_merge_fill_contract_after_duplicate_repair"),
            _step(
                "apply_identity_merge_fill",
                writes=True,
                phrase="APPLY_PRODUCTION_IDENTITY_MERGE_FILL",
            ),
            _step("verify_identity_coverage"),
            _step("verify_full_joint_gate"),
            _step("resume_writer_services", restart=True),
        ],
        "confirmation_phrases": {
            "identity_audit_indexes": "APPLY_PRODUCTION_IDENTITY_AUDIT_INDEXES",
            "duplicate_fact_snapshot_repair": "APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR",
            "identity_merge_fill": "APPLY_PRODUCTION_IDENTITY_MERGE_FILL",
        },
        "guards": {"writes_archive_db": False},
    }


def test_joint_closeout_master_runbook_orders_awc_before_identity_apply(monkeypatch):
    monkeypatch.setattr(runbook.awc_runbook, "build_operator_runbook", lambda *args, **kwargs: _awc_runbook())
    monkeypatch.setattr(runbook.identity_runbook, "build_operator_runbook", lambda *args, **kwargs: _identity_runbook())
    monkeypatch.setattr(runbook.awc_runbook, "compact_operator_runbook", lambda payload: payload)
    monkeypatch.setattr(runbook.identity_runbook, "compact_operator_runbook", lambda payload: payload)

    report = runbook.build_master_runbook("/tmp/archive.db")
    sequence = report["operator_sequence"]
    steps = [row["step"] for row in sequence]
    apply_steps = [row["step"] for row in sequence if row["writes_archive_db"]]

    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert steps.index("audit_reviewed_decision_manifest") < steps.index("pause_writer_services")
    assert steps.index("run_identity_audit_index_temp_drill") < steps.index("run_identity_closeout_temp_drill")
    assert steps.index("run_identity_closeout_temp_drill") < steps.index("pause_writer_services")
    assert apply_steps == [
        "apply_awc_legacy_terminal_closeout",
        "apply_identity_audit_indexes",
        "apply_duplicate_fact_snapshot_repair",
        "apply_identity_merge_fill",
    ]
    assert report["confirmation_phrases"] == {
        "awc_terminal_closeout": "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT",
        "production_identity_audit_indexes": "APPLY_PRODUCTION_IDENTITY_AUDIT_INDEXES",
        "production_duplicate_repair": "APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR",
        "production_identity_merge_fill": "APPLY_PRODUCTION_IDENTITY_MERGE_FILL",
    }
    assert report["ready_for_controlled_apply_now"] is False
    assert "awc_legacy_terminal_closeout_decision_pending" in report["start_blockers"]
    assert "writer_quiescence_not_satisfied" in report["start_blockers"]


def test_joint_closeout_master_runbook_ready_when_source_runbooks_ready(monkeypatch):
    monkeypatch.setattr(runbook.awc_runbook, "build_operator_runbook", lambda *args, **kwargs: _awc_runbook(ready=True))
    monkeypatch.setattr(runbook.identity_runbook, "build_operator_runbook", lambda *args, **kwargs: _identity_runbook(ready=True))
    monkeypatch.setattr(runbook.awc_runbook, "compact_operator_runbook", lambda payload: payload)
    monkeypatch.setattr(runbook.identity_runbook, "compact_operator_runbook", lambda payload: payload)

    report = runbook.build_master_runbook("/tmp/archive.db")
    summary = runbook.compact_master_runbook(report)

    assert report["ok"] is True
    assert report["ready_for_controlled_apply_now"] is True
    assert report["start_blockers"] == []
    assert summary["impact_summary"]["archive_db_apply_steps"] == [
        "apply_awc_legacy_terminal_closeout",
        "apply_identity_audit_indexes",
        "apply_duplicate_fact_snapshot_repair",
        "apply_identity_merge_fill",
    ]


def test_joint_closeout_master_runbook_reuses_precomputed_source_runbooks(monkeypatch):
    def fail_builder(*_args, **_kwargs):
        raise AssertionError("source runbook builder should not be called")

    monkeypatch.setattr(runbook.awc_runbook, "build_operator_runbook", fail_builder)
    monkeypatch.setattr(runbook.identity_runbook, "build_operator_runbook", fail_builder)
    monkeypatch.setattr(runbook.awc_runbook, "compact_operator_runbook", lambda payload: payload)
    monkeypatch.setattr(runbook.identity_runbook, "compact_operator_runbook", lambda payload: payload)

    report = runbook.build_master_runbook(
        "/tmp/archive.db",
        precomputed_awc=_awc_runbook(ready=True),
        precomputed_identity=_identity_runbook(ready=True),
    )

    assert report["ok"] is True
    assert report["ready_for_controlled_apply_now"] is True
    assert report["impact_summary"]["archive_db_apply_steps"] == [
        "apply_awc_legacy_terminal_closeout",
        "apply_identity_audit_indexes",
        "apply_duplicate_fact_snapshot_repair",
        "apply_identity_merge_fill",
    ]
