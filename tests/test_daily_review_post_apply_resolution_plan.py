from scripts import daily_review_post_apply_resolution_plan as plan


def test_resolution_plan_reuses_prebuilt_master_packet_without_rebuilding(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.touch()

    def fail_master_build(*_args, **_kwargs):
        raise AssertionError("prebuilt master packet must be reused")

    monkeypatch.setattr(plan.master_packet, "build_confirmation_packet", fail_master_build)
    monkeypatch.setattr(
        plan.batch_plan,
        "build_batch_plan",
        lambda *_args, **_kwargs: {
            "ok": True,
            "items": [],
            "settled_event_count": 0,
            "effective_settled_event_count": 0,
            "excluded_settled_event_count": 0,
            "planned_review_count": 0,
            "planned_calibration_record_count": 0,
            "can_apply_after_schema_and_confirmation": False,
            "apply_blockers": [],
            "blockers": [],
            "lineage_decision_apply_readiness": {},
        },
    )
    monkeypatch.setattr(
        plan.daily_review_post_apply_acceptance_audit,
        "build_report",
        lambda *_args, **_kwargs: {
            "schema": "daily_review_post_apply_acceptance_audit_v1",
            "ok": True,
            "blockers": [],
            "warnings": [],
            "coverage": {
                "raw_settled_event_total_count": 0,
                "settled_event_count": 0,
                "excluded_settled_event_count": 0,
                "coverage_scope_complete": True,
                "coverage_scope_truncated_count": 0,
                "reviewed_event_count": 0,
                "missing_review_count": 0,
                "chain_ok_count": 0,
                "chain_blocked_count": 0,
                "item_blocker_counts": {},
            },
            "item_blocker_samples": [],
        },
    )

    result = plan.build_resolution_plan(
        db_path,
        master_confirmation_packet={
            "schema": "daily_review_controlled_apply_confirmation_packet_v1",
            "proposed_manifest_json": {"decisions": []},
            "proposed_manifest_decision_counts": {},
        },
    )

    assert result["read_only"] is True
    assert result["ok"] is True
    assert result["operator_resolution_surface_ready"] is True
    assert result["source_counts"]["settled_event_count"] == 0
    assert result["lineage_resolution"]["proposed_manifest_decision_counts"] == {}
    assert result["acceptance_summary"]["ok"] is True


def test_resolution_plan_surfaces_ready_missing_daily_reviews(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.touch()

    monkeypatch.setattr(
        plan.master_packet,
        "build_confirmation_packet",
        lambda *_args, **_kwargs: {
            "schema": "daily_review_controlled_apply_confirmation_packet_v1",
            "proposed_manifest_json": {"decisions": []},
            "proposed_manifest_decision_counts": {},
        },
    )
    monkeypatch.setattr(
        plan.batch_plan,
        "build_batch_plan",
        lambda *_args, **_kwargs: {
            "ok": True,
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                    "event_date": "2026-07-04",
                    "event_run_id": 15922,
                    "review_id": "rev_ready",
                    "fact_snapshot_id": "fact_ready",
                    "lineage_apply_blockers": [],
                    "persistence_row_counts": {
                        "daily_weather_reviews": 1,
                        "daily_calibration_records": 1,
                    },
                    "fact": {},
                }
            ],
            "settled_event_count": 1,
            "effective_settled_event_count": 1,
            "excluded_settled_event_count": 0,
            "planned_review_count": 1,
            "planned_calibration_record_count": 1,
            "can_apply_after_schema_and_confirmation": True,
            "apply_blockers": ["explicit_user_confirmation_required"],
            "blockers": [],
            "lineage_apply_blocker_counts": {},
            "lineage_decision_apply_readiness": {},
        },
    )
    monkeypatch.setattr(
        plan.daily_review_post_apply_acceptance_audit,
        "build_report",
        lambda *_args, **_kwargs: {
            "schema": "daily_review_post_apply_acceptance_audit_v1",
            "ok": False,
            "blockers": ["daily_review_missing"],
            "warnings": [],
            "coverage": {
                "raw_settled_event_total_count": 1,
                "settled_event_count": 1,
                "excluded_settled_event_count": 0,
                "coverage_scope_complete": True,
                "coverage_scope_truncated_count": 0,
                "reviewed_event_count": 0,
                "missing_review_count": 1,
                "chain_ok_count": 0,
                "chain_blocked_count": 1,
                "item_blocker_counts": {"daily_review_missing": 1},
            },
            "item_blocker_samples": [
                {
                    "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                    "event_date": "2026-07-04",
                    "blockers": ["daily_review_missing"],
                    "latest_fact": {
                        "available": True,
                        "fact_id": 106867,
                        "fact_snapshot_id": "fact_ready",
                        "posterior_run_id": "post_ready",
                    },
                }
            ],
        },
    )

    result = plan.build_resolution_plan(db_path)

    assert result["ok"] is False
    assert result["resolution_complete"] is False
    assert result["operator_resolution_surface_ready"] is True
    assert result["acceptance_summary"]["blockers"] == ["daily_review_missing"]
    assert result["batch_plan_summary"]["can_apply_after_schema_and_confirmation"] is True
    assert result["daily_review_missing_resolution"]["candidate_count"] == 1
    assert result["daily_review_missing_resolution"]["blocked_count"] == 0
    assert result["daily_review_missing_resolution"]["items"][0]["state"] == (
        "ready_for_controlled_persistence_apply"
    )
    assert result["next_boundary"] == "run_controlled_daily_review_persistence_apply_after_confirmation"


def test_posterior_rematerialization_probe_uses_exact_candidate_fact_ids(monkeypatch) -> None:
    captured = {}

    def fake_build_plan(db_path, **kwargs):
        captured["db_path"] = db_path
        captured["kwargs"] = kwargs
        return {
            "schema": "posterior_ssot_rematerialization_plan_v1",
            "read_only": True,
            "guards": {"writes_archive_db": False},
            "forward_write_acceptance": {
                "checked_fact_count": 2,
                "projected_ok_count": 2,
                "projected_issue_count": 0,
                "can_forward_writer_fix_all_candidates": True,
            },
            "historical_rematerialization_plan": {
                "candidate_count": 2,
                "action_counts": {
                    "controlled_append_or_repoint_with_fact_writer_after_operator_confirmation": 2
                },
                "issue_family_counts": {"missing_posterior_ssot_fact": 2},
                "current_issue_counts": {"posterior_ssot_fact_missing": 2},
            },
            "candidates": [{"fact_id": 103}, {"fact_id": 104}],
            "candidates_truncated": True,
            "next_boundary": "review_candidates_then_run_temp_drill_or_explicit_controlled_apply_plan",
        }

    monkeypatch.setattr(plan.posterior_ssot_rematerialization_plan, "build_plan", fake_build_plan)

    result = plan._posterior_rematerialization_probe(
        "/tmp/archive.db",
        fact_ids=[103, "", 104, 103, "bad"],
        max_items_output=1,
    )

    assert captured["db_path"] == "/tmp/archive.db"
    assert captured["kwargs"]["fact_ids"] == [103, 104]
    assert captured["kwargs"]["since_hours"] == 0
    assert captured["kwargs"]["limit"] == 2
    assert captured["kwargs"]["latest_only"] is False
    assert captured["kwargs"]["max_items_output"] == 1
    assert result["available"] is True
    assert result["fact_ids"] == [103, 104]
    assert result["checked_fact_count"] == 2
    assert result["candidate_count"] == 2
    assert result["can_forward_writer_fix_all_candidates"] is True
    assert result["items"] == [{"fact_id": 103}]
    assert result["items_truncated"] is True


def test_posterior_rematerialization_probe_empty_scope_is_noop(monkeypatch) -> None:
    def fail_build_plan(*_args, **_kwargs):
        raise AssertionError("empty scope must not call rematerialization planner")

    monkeypatch.setattr(plan.posterior_ssot_rematerialization_plan, "build_plan", fail_build_plan)

    result = plan._posterior_rematerialization_probe(
        "/tmp/archive.db",
        fact_ids=["", 0, "bad"],
        max_items_output=5,
    )

    assert result["available"] is False
    assert result["reason"] == "posterior_candidate_fact_ids_empty"
    assert result["checked_fact_count"] == 0
    assert result["candidate_count"] == 0
    assert result["items"] == []


def test_settlement_final_lock_override_is_not_legacy_awc_signal_candidate() -> None:
    result = plan._replay_anchor_resolution_for_payload(
        payload={
            "hard_source_posterior_override": {
                "source": "event_runs.settlement_actual",
                "constraint_mode": "final_bucket_lock",
                "final_bucket_lock": True,
                "final_lock_reason": "settlement_actual_known",
                "target_bucket": "37°C",
            }
        },
        meta={
            "id": 101,
            "slug": "highest-temperature-in-chengdu-on-july-5-2026",
            "event_date": "2026-07-05",
        },
    )

    assert result["candidate"] is False
    assert result["hard_source_signal_expected"] is False
    assert result["recommended_action"] == "none"
    assert result["no_apply_probe_command"] == ""


def test_legacy_awc_lower_bound_without_signal_is_resolution_candidate() -> None:
    result = plan._replay_anchor_resolution_for_payload(
        payload={
            "hard_source_posterior_override": {
                "source": "zuuu_metar_raw_awc",
                "constraint_mode": "hard_source_lower_bound_constraint",
                "target_bucket": "37°C",
                "value_c": 37.0,
            }
        },
        meta={
            "id": 102,
            "slug": "highest-temperature-in-chengdu-on-july-5-2026",
            "event_date": "2026-07-05",
        },
    )

    assert result["candidate"] is True
    assert result["hard_source_signal_expected"] is True
    assert result["recommended_action"] == (
        "manual_legacy_awc_signal_anchor_rebuild_or_mark_as_reviewed_legacy_no_signal"
    )
    assert result["no_apply_probe_command"] == (
        "python3 scripts/ssot_chain_replay_audit.py --fact-id 102 --max-signals-output 3"
    )


def test_posterior_resolution_reports_ssot_fact_missing_as_no_apply_probe() -> None:
    result = plan._posterior_resolution_for_payload(
        payload={
            "slug": "highest-temperature-in-chengdu-on-july-5-2026",
            "event_date": "2026-07-05",
            "fact_snapshot_id": "fact_missing_ssot",
            "posterior_run_id": "post_missing_ssot",
            "phase": "D0_awc_hard_signal",
            "phase_reason": "unit_test",
            "beta_weak": 0.0,
            "beta_strong": 1.0,
            "hard_mask_reason": "observed_today_max_c",
            "posterior_temperature_distribution": [
                {"bucket": "37°C", "probability": 1.0},
            ],
        },
        meta={
            "id": 103,
            "slug": "highest-temperature-in-chengdu-on-july-5-2026",
            "event_date": "2026-07-05",
            "fact_as_of": "2026-07-05T10:00:00Z",
        },
    )

    assert result["ok"] is False
    assert "posterior_ssot_fact_missing" in result["issues"]
    assert result["recommended_action"] == (
        "run_posterior_ssot_rematerialization_plan_then_controlled_fact_writer_apply"
    )
    assert result["no_apply_probe_command"] == (
        "python3 scripts/posterior_ssot_rematerialization_plan.py --fact-id 103 --max-items-output 1"
    )
