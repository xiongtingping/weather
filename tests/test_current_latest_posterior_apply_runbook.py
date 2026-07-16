from pathlib import Path

from scripts import current_latest_posterior_apply_runbook as runbook


def _production_packet(*, writer_quiescent: bool = True, fact_ids: list[int] | None = None) -> dict:
    ids = [11, 22] if fact_ids is None else fact_ids
    return {
        "schema": "daily_review_production_apply_preflight_packet_v1",
        "read_only": True,
        "ready_to_start_controlled_sequence": writer_quiescent,
        "start_blockers": [] if writer_quiescent else ["writer_quiescence_not_satisfied"],
        "maintenance_window": {
            "writer_quiescent": writer_quiescent,
            "required_stop_labels": []
            if writer_quiescent
            else ["gui/$(id -u)/com.xiong.weather.ui"],
            "operator_commands": {
                "suggested_pause_commands": [
                    "scripts/weatherctl.sh stop",
                    "sudo launchctl disable system/com.weather.monitor.daemon && sudo launchctl stop system/com.weather.monitor.daemon",
                ],
                "suggested_resume_commands": [
                    "sudo launchctl enable system/com.weather.monitor.daemon && sudo launchctl kickstart -k system/com.weather.monitor.daemon",
                    "scripts/weatherctl.sh start",
                ],
                "verify_writer_quiescence": (
                    "python3 scripts/controlled_apply_maintenance_window_packet.py --summary"
                ),
            },
            "stop_authorization": {
                "system_monitor_stop_required": not writer_quiescent,
                "can_pause_required_services_now": writer_quiescent,
                "blockers": [] if writer_quiescent else ["system_monitor_daemon_stop_requires_interactive_sudo"],
            },
        },
        "current_latest_posterior_materialization": {
            "candidate_count": len(ids),
            "candidate_fact_ids": ids,
            "projected_ok_count": len(ids),
            "projected_issue_count": 0,
        },
    }


def _confirmation(ids: list[int]) -> dict:
    return {
        "schema": "posterior_ssot_apply_confirmation_packet_v1",
        "ok": True,
        "read_only": True,
        "candidate_count": len(ids),
        "candidates": [
            {
                "fact_id": fact_id,
                "slug": f"highest-temperature-in-chengdu-on-july-{fact_id}-2026",
                "event_date": "2026-07-05",
                "is_latest_fact": True,
                "projected_target_bucket": "37°C",
                "projected_target_probability": 1.0,
            }
            for fact_id in ids
        ],
    }


def _risk_packet(fact_id: int, *, ok: bool = True) -> dict:
    return {
        "schema": "posterior_ssot_apply_preflight_risk_packet_v1",
        "ok": ok,
        "read_only": True,
        "risk_level": "controlled_low_after_operator_confirmation" if ok else "blocked",
        "blockers": [] if ok else ["posterior_ssot_apply_contract_not_ok"],
        "candidate_count": 1,
        "expected_write_summary": {
            "tables": [
                "event_fact_history",
                "event_fact_latest",
                "event_fact_summary_cache",
            ],
            "append_event_fact_history_count": 1,
            "repoint_event_fact_latest_count": 1,
            "upsert_event_fact_summary_cache_count": 1,
        },
    }


def test_runbook_builds_shared_backup_sequence_for_ready_current_latest_candidates(monkeypatch, tmp_path: Path):
    ids = [11, 22]
    monkeypatch.setattr(
        runbook.production_preflight,
        "build_preflight_packet",
        lambda *_args, **_kwargs: _production_packet(writer_quiescent=True, fact_ids=ids),
    )
    monkeypatch.setattr(
        runbook.confirmation_packet,
        "build_confirmation_packet",
        lambda *_args, **_kwargs: _confirmation(ids),
    )
    monkeypatch.setattr(
        runbook.confirmation_packet,
        "compact_confirmation_packet",
        lambda value: {"schema": value["schema"], "candidate_count": value["candidate_count"]},
    )
    monkeypatch.setattr(
        runbook.risk_packet,
        "build_preflight_risk_packet",
        lambda *_args, **kwargs: _risk_packet(int(kwargs["fact_ids"][0])),
    )

    packet = runbook.build_runbook(
        tmp_path / "archive.db",
        generated_at="2026-07-06T09:00:00Z",
        backup_dir=tmp_path / "backups",
    )

    assert packet["read_only"] is True
    assert packet["guards"]["writes_archive_db"] is False
    assert packet["ok"] is True
    assert packet["ready_for_controlled_apply_now"] is True
    assert packet["scope"]["candidate_fact_ids"] == ids
    assert packet["shared_backup"]["path"].endswith(
        "archive.db.current-latest-posterior-runbook-20260706T090000Z.bak"
    )
    temp_drill_steps = [
        row for row in packet["operator_sequence"] if row["step"] == "run_current_latest_temp_drill"
    ]
    assert len(temp_drill_steps) == 1
    assert temp_drill_steps[0]["writes_archive_db"] is False
    assert temp_drill_steps[0]["writes_temp_archive_db"] is True
    assert "scripts/current_latest_posterior_apply_temp_drill.py" in temp_drill_steps[0]["command"]
    assert "--fact-ids 11,22" in temp_drill_steps[0]["command"]
    assert temp_drill_steps[0]["candidate_fact_ids"] == ids
    assert temp_drill_steps[0]["candidate_count"] == len(ids)
    assert "source_runbook.candidate_fact_ids == expected candidate_fact_ids" in temp_drill_steps[0]["pass_condition"]
    apply_steps = [
        row for row in packet["operator_sequence"] if row["step"] == "apply_one_current_latest_fact"
    ]
    assert [row["fact_id"] for row in apply_steps] == ids
    assert all("--backup " in row["command"] for row in apply_steps)
    assert all("--writer-quiescent" in row["command"] for row in apply_steps)
    assert all("APPLY_POSTERIOR_SSOT_FACT_REPAIR" in row["command"] for row in apply_steps)
    assert all(row["enabled_now"] is True for row in apply_steps)
    assert all(row["blocked_until"] == [] for row in apply_steps)
    assert all(
        "run_current_latest_temp_drill returns ok == true for the same candidate_fact_ids"
        in row["preconditions"]
        for row in apply_steps
    )
    assert all(
        "verify_writer_quiescence returns writer_quiescent == true and start_blockers == []"
        in row["preconditions"]
        for row in apply_steps
    )
    assert all(
        "apply_posterior_ssot_fact_repair rechecks controlled_apply_maintenance_window_packet before writing"
        in row["preconditions"]
        for row in apply_steps
    )
    assert all(
        "apply_posterior_ssot_fact_repair verifies the shared backup contains the same candidate fact and latest pointer before writing"
        in row["preconditions"]
        for row in apply_steps
    )
    assert all(row["uses_shared_backup"] for row in packet["single_fact_preflights"])
    exact_acceptance_steps = [
        row for row in packet["operator_sequence"] if row["step"] == "post_apply_exact_candidate_acceptance"
    ]
    assert len(exact_acceptance_steps) == 1
    assert exact_acceptance_steps[0]["writes_archive_db"] is False
    assert exact_acceptance_steps[0]["required"] is True
    assert "scripts/current_latest_posterior_post_apply_acceptance.py" in exact_acceptance_steps[0]["command"]
    assert "--candidate-fact-ids 11,22" in exact_acceptance_steps[0]["command"]
    assert "--expected-projections-json" in exact_acceptance_steps[0]["command"]
    assert '"fact_id":11' in exact_acceptance_steps[0]["command"]
    assert '"target_bucket":"37°C"' in exact_acceptance_steps[0]["command"]
    assert "accepted_count == candidate_count" in exact_acceptance_steps[0]["pass_condition"]
    assert packet["boundary"]["runbook_executes_temp_drill"] is False
    assert packet["boundary"]["temp_drill_writes_production_db"] is False
    assert packet["boundary"]["requires_temp_drill_before_apply"] is True
    assert packet["boundary"]["requires_exact_candidate_acceptance_after_apply"] is True
    assert packet["boundary"]["runbook_executes_apply"] is False


def test_runbook_keeps_writer_quiescence_as_start_blocker(monkeypatch, tmp_path: Path):
    ids = [11]
    monkeypatch.setattr(
        runbook.production_preflight,
        "build_preflight_packet",
        lambda *_args, **_kwargs: _production_packet(writer_quiescent=False, fact_ids=ids),
    )
    monkeypatch.setattr(
        runbook.confirmation_packet,
        "build_confirmation_packet",
        lambda *_args, **_kwargs: _confirmation(ids),
    )
    monkeypatch.setattr(
        runbook.confirmation_packet,
        "compact_confirmation_packet",
        lambda value: {"schema": value["schema"], "candidate_count": value["candidate_count"]},
    )
    monkeypatch.setattr(
        runbook.risk_packet,
        "build_preflight_risk_packet",
        lambda *_args, **kwargs: _risk_packet(int(kwargs["fact_ids"][0])),
    )

    packet = runbook.build_runbook(tmp_path / "archive.db", generated_at="2026-07-06T09:00:00Z")

    assert packet["ok"] is True
    assert packet["ready_for_controlled_apply_now"] is False
    assert packet["start_blockers"] == ["writer_quiescence_not_satisfied"]
    assert packet["maintenance_window"]["writer_quiescent"] is False
    assert packet["maintenance_window"]["required_stop_labels"] == ["gui/$(id -u)/com.xiong.weather.ui"]
    assert packet["next_boundary"] == "resolve_start_or_risk_blockers_before_controlled_apply"
    apply_steps = [
        row for row in packet["operator_sequence"] if row["step"] == "apply_one_current_latest_fact"
    ]
    assert apply_steps[0]["enabled_now"] is False
    assert "writer_quiescence_not_satisfied" in apply_steps[0]["blocked_until"]
    assert "verify_writer_quiescence_passes" in apply_steps[0]["blocked_until"]
    compact = runbook.compact_runbook(packet)
    compact_apply = [
        row for row in compact["operator_sequence"] if row["step"] == "apply_one_current_latest_fact"
    ][0]
    assert compact_apply["enabled_now"] is False
    assert "shared_backup_created" in compact_apply["blocked_until"]


def test_runbook_blocks_when_confirmation_candidate_count_does_not_match(monkeypatch, tmp_path: Path):
    ids = [11, 22]
    monkeypatch.setattr(
        runbook.production_preflight,
        "build_preflight_packet",
        lambda *_args, **_kwargs: _production_packet(writer_quiescent=True, fact_ids=ids),
    )
    monkeypatch.setattr(
        runbook.confirmation_packet,
        "build_confirmation_packet",
        lambda *_args, **_kwargs: _confirmation([11]),
    )
    monkeypatch.setattr(
        runbook.confirmation_packet,
        "compact_confirmation_packet",
        lambda value: {"schema": value["schema"], "candidate_count": value["candidate_count"]},
    )
    monkeypatch.setattr(
        runbook.risk_packet,
        "build_preflight_risk_packet",
        lambda *_args, **kwargs: _risk_packet(int(kwargs["fact_ids"][0])),
    )

    packet = runbook.build_runbook(tmp_path / "archive.db", generated_at="2026-07-06T09:00:00Z")

    assert packet["ok"] is False
    assert packet["ready_for_controlled_apply_now"] is False
    assert "confirmation_candidate_count_mismatch" in packet["risk_blockers"]
    by_fact = {row["fact_id"]: row for row in packet["single_fact_preflights"]}
    assert by_fact[22]["apply_command"] == ""
    assert by_fact[22]["uses_shared_backup"] is False


def test_runbook_uses_confirmation_candidates_when_preflight_scope_is_stale(monkeypatch, tmp_path: Path):
    production_ids: list[int] = []
    confirmation_ids = [106888, 106807]
    monkeypatch.setattr(
        runbook.production_preflight,
        "build_preflight_packet",
        lambda *_args, **_kwargs: _production_packet(writer_quiescent=False, fact_ids=production_ids),
    )
    monkeypatch.setattr(
        runbook.confirmation_packet,
        "build_confirmation_packet",
        lambda *_args, **_kwargs: _confirmation(confirmation_ids),
    )
    monkeypatch.setattr(
        runbook.confirmation_packet,
        "compact_confirmation_packet",
        lambda value: {"schema": value["schema"], "candidate_count": value["candidate_count"]},
    )
    monkeypatch.setattr(
        runbook.risk_packet,
        "build_preflight_risk_packet",
        lambda *_args, **kwargs: _risk_packet(int(kwargs["fact_ids"][0])),
    )

    packet = runbook.build_runbook(tmp_path / "archive.db", generated_at="2026-07-06T09:00:00Z")

    assert packet["ok"] is True
    assert packet["scope"]["apply_required"] is True
    assert packet["scope"]["candidate_fact_ids"] == confirmation_ids
    assert packet["risk_blockers"] == []
    assert [row["fact_id"] for row in packet["single_fact_preflights"]] == confirmation_ids
    assert "--fact-ids 106888,106807" in [
        row["command"] for row in packet["operator_sequence"] if row["step"] == "run_current_latest_temp_drill"
    ][0]
    assert packet["ready_for_controlled_apply_now"] is False


def test_runbook_treats_empty_current_latest_scope_as_noop_ok(monkeypatch, tmp_path: Path):
    ids: list[int] = []
    monkeypatch.setattr(
        runbook.production_preflight,
        "build_preflight_packet",
        lambda *_args, **_kwargs: _production_packet(writer_quiescent=True, fact_ids=ids),
    )
    monkeypatch.setattr(
        runbook.confirmation_packet,
        "build_confirmation_packet",
        lambda *_args, **_kwargs: _confirmation(ids),
    )
    monkeypatch.setattr(
        runbook.confirmation_packet,
        "compact_confirmation_packet",
        lambda value: {"schema": value["schema"], "candidate_count": value["candidate_count"]},
    )
    monkeypatch.setattr(
        runbook.risk_packet,
        "build_preflight_risk_packet",
        lambda *_args, **_kwargs: _risk_packet(0),
    )

    packet = runbook.build_runbook(tmp_path / "archive.db", generated_at="2026-07-06T09:00:00Z")
    compact = runbook.compact_runbook(packet)

    assert packet["ok"] is True
    assert packet["scope"]["apply_required"] is False
    assert packet["ready_for_controlled_apply_now"] is False
    assert packet["risk_blockers"] == []
    assert packet["single_fact_preflights"] == []
    assert not [
        row for row in packet["operator_sequence"] if row["step"] == "run_current_latest_temp_drill"
    ]
    assert compact["apply_required"] is False
    assert packet["next_boundary"] == "no_current_latest_posterior_apply_required"
