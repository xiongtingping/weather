from scripts import legacy_awc_signal_anchor_decision_packet as packet


def _fake_replay_resolution() -> dict:
    return {
        "candidate_count": 2,
        "source_counts": {"zuuu_metar_raw_awc": 2},
        "constraint_mode_counts": {"hard_source_lower_bound_constraint": 2},
        "items": [
            {
                "fact_id": 101,
                "slug": "highest-temperature-in-chengdu-on-june-18-2026",
                "event_date": "2026-06-18",
                "hard_source_signal_expected": True,
                "signal_id": "",
                "override": {
                    "source": "zuuu_metar_raw_awc",
                    "constraint_mode": "hard_source_lower_bound_constraint",
                    "target_bucket": "31°C",
                    "value_c": 31.0,
                    "value_at": "2026-06-18T09:00:00+00:00",
                    "latest_at": "2026-06-18T15:00:00+00:00",
                    "fetched_at": "2026-06-18T16:01:09+00:00",
                },
                "no_apply_probe_command": (
                    "python3 scripts/ssot_chain_replay_audit.py --fact-id 101 --max-signals-output 3"
                ),
            },
            {
                "fact_id": 102,
                "slug": "highest-temperature-in-chengdu-on-june-21-2026",
                "event_date": "2026-06-21",
                "hard_source_signal_expected": True,
                "signal_id": "",
                "override": {
                    "source": "zuuu_metar_raw_awc",
                    "constraint_mode": "hard_source_lower_bound_constraint",
                    "target_bucket": "25°C or below",
                    "value_c": 24.0,
                },
                "no_apply_probe_command": (
                    "python3 scripts/ssot_chain_replay_audit.py --fact-id 102 --max-signals-output 3"
                ),
            },
        ],
    }


def test_legacy_awc_anchor_packet_emits_manifest_template_without_writing() -> None:
    result = packet.build_decision_packet_from_replay_resolution(
        _fake_replay_resolution(),
        max_items_output=10,
    )

    assert result["schema"] == "legacy_awc_signal_anchor_decision_packet_v1"
    assert result["read_only"] is True
    assert result["dry_run"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["writes_signal_to_order_timing"] is False
    assert result["boundary"]["automatic_signal_ledger_rebuild_included"] is False
    assert result["candidate_count"] == 2

    template = result["decision_manifest_template"]
    assert template["schema"] == "legacy_awc_signal_anchor_decision_manifest_v1"
    assert template["decision_count"] == 2
    assert template["decisions"][0]["decision"] == "undecided"
    assert "mark_reviewed_legacy_no_signal_anchor" in template["decisions"][0]["allowed_decisions"]

    audit = result["decision_manifest_audit"]
    assert audit["manifest_provided"] is False
    assert audit["decision_required"] is True
    assert audit["missing_decision_count"] == 2
    assert "legacy_awc_signal_anchor_decision_manifest_missing" in audit["hard_blockers"]
    assert result["ok"] is False


def test_reviewed_no_signal_manifest_resolves_acceptance_exception() -> None:
    baseline = packet.build_decision_packet_from_replay_resolution(
        _fake_replay_resolution(),
        max_items_output=10,
    )
    manifest = baseline["decision_manifest_template"]
    for row in manifest["decisions"]:
        row["decision"] = "mark_reviewed_legacy_no_signal_anchor"
        row["review_reason"] = "Legacy AWC lower-bound fact had no live signal ledger in this period."

    result = packet.build_decision_packet_from_replay_resolution(
        _fake_replay_resolution(),
        decision_manifest=manifest,
        max_items_output=10,
    )

    audit = result["decision_manifest_audit"]
    assert result["ok"] is True
    assert audit["manifest_provided"] is True
    assert audit["operator_route_complete"] is True
    assert audit["acceptance_exception_complete"] is True
    assert audit["decision_required"] is False
    assert audit["reviewed_exception_count"] == 2
    assert audit["hard_blockers"] == []


def test_manual_rebuild_manifest_records_route_but_still_requires_dedicated_writer() -> None:
    baseline = packet.build_decision_packet_from_replay_resolution(
        _fake_replay_resolution(),
        max_items_output=10,
    )
    manifest = baseline["decision_manifest_template"]
    for row in manifest["decisions"]:
        row["decision"] = "manual_rebuild_signal_anchor_with_reviewed_source_evidence"
        row["review_reason"] = "Operator wants a dedicated signal-anchor writer before acceptance."

    result = packet.build_decision_packet_from_replay_resolution(
        _fake_replay_resolution(),
        decision_manifest=manifest,
        max_items_output=10,
    )

    audit = result["decision_manifest_audit"]
    assert result["ok"] is False
    assert audit["operator_route_complete"] is True
    assert audit["acceptance_exception_complete"] is False
    assert audit["dedicated_writer_required_count"] == 2
    assert "legacy_awc_signal_anchor_dedicated_writer_required" in audit["hard_blockers"]
