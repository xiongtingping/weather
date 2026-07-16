from pathlib import Path

from scripts import hierarchical_probability_release_gate as gate


def _audit_report(schema: str, *, ok: bool = True, checked: int = 1, issue_counts=None, blockers=None):
    return {
        "schema": schema,
        "ok": ok,
        "read_only": True,
        "coverage_mode": "forward_writer_projection_no_apply",
        "coverage": {
            "checked_fact_count": checked,
            "ok_fact_count": checked if ok else 0,
            "issue_fact_count": 0 if ok else max(1, checked),
        },
        "issue_counts": dict(issue_counts or {}),
        "blockers": list(blockers or []),
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
        "next_boundary": f"{schema}_next",
    }


def _chain_report(*, ok: bool = True, posterior_entrypoint: bool = True, blockers=None):
    return {
        "schema": "production_chain_uniqueness_audit_v1",
        "ok": ok,
        "read_only": True,
        "checks": {
            "legacy_evidence_not_production": ok,
            "ui_no_frontend_recompute": ok,
            "fact_writer_single_canonical_owner": ok,
            "reported_x_single_production_mainline": posterior_entrypoint,
            "monitor_signal_no_bot_signal_alias": ok,
        },
        "blockers": list(blockers or []),
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _unit_stage(stage: str, *, ok: bool = True, blockers=None):
    return {
        "schema": "hierarchical_probability_unit_stage_contract_v1",
        "stage": stage,
        "ok": ok,
        "read_only": True,
        "coverage_mode": "runtime_unit_contract_fixtures_no_db_writes",
        "coverage": {
            "checked_fact_count": 1,
            "checked_fixture_count": 1,
            "ok_fixture_count": 1 if ok else 0,
            "issue_fixture_count": 0 if ok else 1,
        },
        "checks": {f"{stage}_fixture": ok},
        "issue_counts": {} if ok else {f"{stage}_fixture": 1},
        "blockers": list(blockers or []),
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _unit_report(*, prior_ok=True, likelihood_ok=True, posterior_ok=True):
    return {
        "schema": "hierarchical_probability_unit_contract_audit_v1",
        "ok": all((prior_ok, likelihood_ok, posterior_ok)),
        "read_only": True,
        "stage_reports": {
            "prior": _unit_stage(
                "prior",
                ok=prior_ok,
                blockers=[] if prior_ok else [{"blocker": "prior_unit_contract_failed"}],
            ),
            "likelihood": _unit_stage(
                "likelihood",
                ok=likelihood_ok,
                blockers=[] if likelihood_ok else [{"blocker": "likelihood_unit_contract_failed"}],
            ),
            "posterior": _unit_stage(
                "posterior",
                ok=posterior_ok,
                blockers=[] if posterior_ok else [{"blocker": "posterior_unit_contract_failed"}],
            ),
        },
        "blockers": [],
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _writer_apply_report(*, ok: bool = True):
    return {
        "schema": "fact_writer_persisted_apply_contract_audit_v1",
        "ok": ok,
        "read_only": True,
        "coverage_mode": "writer_apply_result_summary_temp_persisted_fixture",
        "coverage": {
            "checked_fact_count": 2,
            "checked_fixture_count": 2,
            "ok_fixture_count": 2 if ok else 1,
            "issue_fixture_count": 0 if ok else 1,
        },
        "issue_counts": {} if ok else {"persisted_summary_post_apply_verification_not_ok": 1},
        "blockers": []
        if ok
        else [
            {
                "blocker": "writer_apply_result_summary_fixture_failed",
                "fixture": "persisted_verified",
            }
        ],
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_daily_weather_reviews": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _ui_lineage_report(*, ok: bool = True, checked: int = 1, blockers=None):
    return {
        "schema": "ui_snapshot_lineage_audit_v1",
        "ok": ok,
        "read_only": True,
        "snapshot": {
            "snapshot_id": "uisnap_test",
            "fact_lineage_complete": True,
            "lineage_placeholder": False,
        },
        "counts": {
            "fact_items": checked,
            "lineage_items": checked,
            "source_fact_ids": checked,
            "checked_facts": checked,
            "replayed_facts": checked,
        },
        "blockers": list(blockers or []),
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_daily_weather_reviews": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _ui_lineage_busy_placeholder_report():
    return {
        **_ui_lineage_report(
            ok=False,
            checked=0,
            blockers=[
                "desktop_first_screen_fact_item_missing",
                "desktop_first_screen_bayes_posterior_ssot_section_missing",
            ],
        ),
        "snapshot": {
            "snapshot_id": "uisnap_busy",
            "snapshot_freshness_state": "blocked",
            "fact_lineage_complete": False,
            "lineage_placeholder": True,
        },
    }


def _replay_report(*, ok: bool = True, signal_count: int = 1, blockers=None, warnings=None):
    return {
        "schema": "ssot_chain_replay_audit_v1",
        "ok": ok,
        "read_only": True,
        "signal_count": signal_count,
        "blockers": list(blockers or []),
        "warnings": list(warnings or []),
        "source_counts": {"signal_to_order_timing": signal_count} if signal_count else {},
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_daily_weather_reviews": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _parameter_governance_report(*, ok: bool = True, blockers=None, warnings=None):
    return {
        "schema": "parameter_governance_surface_audit_v1",
        "ok": ok,
        "read_only": True,
        "blockers": list(blockers or []),
        "warnings": list(warnings or []),
        "guards": {
            "writes_archive_db": False,
            "writes_config_files": False,
            "writes_parameter_change_events": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _source_acquisition_report(*, ok: bool = True, blockers=None):
    return {
        "schema": "source_acquisition_contract_audit_v1",
        "ok": ok,
        "read_only": True,
        "coverage": {
            "checked_fact_count": 1,
            "sample_trace_count": 4,
            "sample_source_observation_count": 9,
            "catalog_required_source_count": 20,
            "network_file_count": 26,
        },
        "blockers": list(blockers or []),
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
        "next_boundary": "source_acquisition_contract_ok_continue_to_fact_writer",
    }


def _stub_all_clean(monkeypatch):
    monkeypatch.setattr(
        gate.unit_contract_audit,
        "build_hierarchical_probability_unit_contract_audit",
        lambda: _unit_report(),
    )
    monkeypatch.setattr(
        gate.source_acquisition_audit,
        "build_source_acquisition_contract_audit",
        lambda **kwargs: _source_acquisition_report(),
    )
    monkeypatch.setattr(
        gate.prior_audit,
        "build_prior_layer_contract_audit",
        lambda **kwargs: _audit_report("prior_layer_contract_audit_v1"),
    )
    monkeypatch.setattr(
        gate.forecast_audit,
        "build_forecast_weak_likelihood_contract_audit",
        lambda **kwargs: _audit_report("forecast_weak_likelihood_contract_audit_v1"),
    )
    monkeypatch.setattr(
        gate.metar_audit,
        "build_metar_strong_likelihood_contract_audit",
        lambda **kwargs: _audit_report("metar_strong_likelihood_contract_audit_v1"),
    )
    monkeypatch.setattr(
        gate.posterior_ssot_audit,
        "audit",
        lambda *args, **kwargs: _audit_report("posterior_ssot_fact_coverage_audit_v1"),
    )
    monkeypatch.setattr(
        gate.writer_apply_audit,
        "build_fact_writer_persisted_apply_contract_audit",
        lambda: _writer_apply_report(),
    )
    monkeypatch.setattr(
        gate.canonical_persisted_audit,
        "build_canonical_writer_persisted_coverage_audit",
        lambda **kwargs: _audit_report("canonical_writer_persisted_coverage_audit_v1"),
    )
    monkeypatch.setattr(
        gate.chain_audit,
        "build_production_chain_uniqueness_audit",
        lambda *args, **kwargs: _chain_report(),
    )
    monkeypatch.setattr(
        gate.ui_lineage_audit,
        "load_ui_snapshot",
        lambda **kwargs: {"ok": True, "snapshot_id": "uisnap_test"},
    )
    monkeypatch.setattr(
        gate.ui_lineage_audit,
        "audit_payload",
        lambda *args, **kwargs: _ui_lineage_report(),
    )
    monkeypatch.setattr(
        gate.replay_audit,
        "audit",
        lambda *args, **kwargs: _replay_report(),
    )
    monkeypatch.setattr(
        gate.parameter_governance_audit,
        "build_audit",
        lambda *args, **kwargs: _parameter_governance_report(),
    )


def test_hierarchical_probability_release_gate_passes_when_all_stage_gates_pass(monkeypatch, tmp_path):
    _stub_all_clean(monkeypatch)

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
        since_hours=24,
        latest_only=True,
        project_forward=True,
    )

    assert report["ok"] is True
    assert report["gate_order"] == [
        "prior_gate",
        "likelihood_gate",
        "posterior_gate",
        "posterior_ssot_gate",
        "production_chain_uniqueness_gate",
        "parameter_governance_surface_gate",
        "ui_snapshot_lineage_gate",
        "ssot_chain_replay_gate",
    ]


def test_hierarchical_probability_release_gate_retries_ui_lineage_busy_placeholder(monkeypatch, tmp_path):
    _stub_all_clean(monkeypatch)
    load_calls = []
    reports = [_ui_lineage_busy_placeholder_report(), _ui_lineage_report()]

    def fake_load_ui_snapshot(**kwargs):
        load_calls.append(dict(kwargs))
        return {"ok": True, "snapshot_id": "uisnap_retry"}

    def fake_audit_payload(*args, **kwargs):
        return reports.pop(0)

    monkeypatch.setattr(gate.ui_lineage_audit, "load_ui_snapshot", fake_load_ui_snapshot)
    monkeypatch.setattr(gate.ui_lineage_audit, "audit_payload", fake_audit_payload)

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is True
    assert [call["force_rebuild"] for call in load_calls] == [False, True]
    ui_gate = next(item for item in report["gates"] if item["gate"] == "ui_snapshot_lineage_gate")
    assert ui_gate["reports"]["ui_snapshot_lineage"]["warnings"] == [
        "ui_snapshot_lineage_force_rebuild_retry_after_busy_placeholder"
    ]
    assert all(report["checks"].values())
    assert report["guards"]["writes_facts_sqlite"] is False
    assert report["guards"]["places_orders"] is False
    assert report["next_actions"] == [
        "probability_release_gates_ok_continue_to_downstream_read_only_consumers",
        "keep_prior_likelihood_posterior_gates_in_release_checklist",
    ]


def test_hierarchical_probability_release_gate_blocks_likelihood_failure(monkeypatch, tmp_path):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.forecast_audit,
        "build_forecast_weak_likelihood_contract_audit",
        lambda **kwargs: _audit_report(
            "forecast_weak_likelihood_contract_audit_v1",
            ok=False,
            issue_counts={"weak_source_hard_lock_detected": 1},
        ),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["prior_gate"] is True
    assert report["checks"]["likelihood_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "likelihood_gate_forecast_weak_likelihood_contract_not_ok" in blocker_ids


def test_hierarchical_probability_release_gate_blocks_source_acquisition_contract_failure(
    monkeypatch,
    tmp_path,
):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.source_acquisition_audit,
        "build_source_acquisition_contract_audit",
        lambda **kwargs: _source_acquisition_report(
            ok=False,
            blockers=["source_observation_required_field_missing:raw_ref"],
        ),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["likelihood_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "likelihood_gate_source_acquisition_contract_not_ok" in blocker_ids


def test_hierarchical_probability_release_gate_blocks_empty_prior_scope(monkeypatch, tmp_path):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.prior_audit,
        "build_prior_layer_contract_audit",
        lambda **kwargs: _audit_report("prior_layer_contract_audit_v1", checked=0),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
        min_checked_facts=1,
    )

    assert report["ok"] is False
    assert report["checks"]["prior_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "prior_gate_prior_layer_contract_insufficient_fact_scope" in blocker_ids


def test_hierarchical_probability_release_gate_blocks_missing_posterior_entrypoint(
    monkeypatch,
    tmp_path,
):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.chain_audit,
        "build_production_chain_uniqueness_audit",
        lambda *args, **kwargs: _chain_report(ok=True, posterior_entrypoint=False),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=Path(tmp_path / "archive.db"),
    )

    assert report["ok"] is False
    assert report["checks"]["posterior_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "posterior_gate_posterior_entrypoint_not_ok" in blocker_ids


def test_hierarchical_probability_release_gate_blocks_failed_posterior_unit_contract(
    monkeypatch,
    tmp_path,
):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.unit_contract_audit,
        "build_hierarchical_probability_unit_contract_audit",
        lambda: _unit_report(posterior_ok=False),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=Path(tmp_path / "archive.db"),
    )

    assert report["ok"] is False
    assert report["checks"]["prior_gate"] is True
    assert report["checks"]["likelihood_gate"] is True
    assert report["checks"]["posterior_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "posterior_gate_posterior_unit_contract_not_ok" in blocker_ids


def test_hierarchical_probability_release_gate_blocks_failed_writer_apply_contract(
    monkeypatch,
    tmp_path,
):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.writer_apply_audit,
        "build_fact_writer_persisted_apply_contract_audit",
        lambda: _writer_apply_report(ok=False),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=Path(tmp_path / "archive.db"),
    )

    assert report["ok"] is False
    assert report["checks"]["posterior_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "posterior_ssot_gate_writer_persisted_apply_contract_not_ok" in blocker_ids


def test_hierarchical_probability_release_gate_blocks_failed_canonical_persisted_coverage(
    monkeypatch,
    tmp_path,
):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.canonical_persisted_audit,
        "build_canonical_writer_persisted_coverage_audit",
        lambda **kwargs: _audit_report(
            "canonical_writer_persisted_coverage_audit_v1",
            ok=False,
            issue_counts={"posterior_ssot_fact_target_below_hard_temperature_floor": 1},
            blockers=["canonical_writer_marked_persisted_coverage_incomplete"],
        ),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=Path(tmp_path / "archive.db"),
    )

    assert report["ok"] is False
    assert report["checks"]["posterior_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "posterior_ssot_gate_canonical_writer_persisted_coverage_not_ok" in blocker_ids


def test_hierarchical_probability_release_gate_blocks_ui_lineage_failure(monkeypatch, tmp_path):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.ui_lineage_audit,
        "audit_payload",
        lambda *args, **kwargs: _ui_lineage_report(
            ok=False,
            blockers=["ui_snapshot_source_observation_id_not_in_fact_json"],
        ),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=Path(tmp_path / "archive.db"),
    )

    assert report["ok"] is False
    assert report["checks"]["ui_snapshot_lineage_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "ui_snapshot_lineage_gate_ui_snapshot_lineage_not_ok" in blocker_ids


def test_hierarchical_probability_release_gate_blocks_parameter_governance_failure(monkeypatch, tmp_path):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.parameter_governance_audit,
        "build_audit",
        lambda *args, **kwargs: _parameter_governance_report(
            ok=False,
            blockers=["legacy_optimizer_auto_apply_enabled_in_launchd"],
        ),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["checks"]["parameter_governance_surface_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "parameter_governance_surface_gate_parameter_governance_surface_not_ok" in blocker_ids


def test_hierarchical_probability_release_gate_blocks_replay_failure(monkeypatch, tmp_path):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.replay_audit,
        "audit",
        lambda *args, **kwargs: _replay_report(
            ok=False,
            blockers=["accepted_submitted_signal_missing_decision_source"],
        ),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=Path(tmp_path / "archive.db"),
    )

    assert report["ok"] is False
    assert report["checks"]["ssot_chain_replay_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "ssot_chain_replay_gate_ssot_chain_replay_not_ok" in blocker_ids


def test_hierarchical_probability_release_gate_downgrades_missing_recent_awc_sample_by_default(
    monkeypatch,
    tmp_path,
):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.replay_audit,
        "audit",
        lambda *args, **kwargs: _replay_report(
            ok=False,
            signal_count=0,
            blockers=["no_awc_hard_source_signals_found"],
        ),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=Path(tmp_path / "archive.db"),
    )

    assert report["ok"] is True
    assert report["checks"]["ssot_chain_replay_gate"] is True
    replay_summary = report["gates"][-1]["reports"]["ssot_chain_replay"]
    assert replay_summary["warnings"] == ["release_gate_no_recent_awc_hard_source_signal_sample"]


def test_hierarchical_probability_release_gate_can_require_awc_sample(monkeypatch, tmp_path):
    _stub_all_clean(monkeypatch)
    monkeypatch.setattr(
        gate.replay_audit,
        "audit",
        lambda *args, **kwargs: _replay_report(
            ok=False,
            signal_count=0,
            blockers=["no_awc_hard_source_signals_found"],
        ),
    )

    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=Path(tmp_path / "archive.db"),
        require_awc_signal_sample=True,
    )

    assert report["ok"] is False
    assert report["checks"]["ssot_chain_replay_gate"] is False


def test_forecast_weight_release_gate_requires_and_receives_ablation(monkeypatch, tmp_path):
    _stub_all_clean(monkeypatch)
    captured = {}

    def fake_weight_gate(**kwargs):
        captured.update(kwargs)
        return _audit_report("forecast_source_weight_release_gate_v1")

    monkeypatch.setattr(gate.forecast_weight_audit, "build_forecast_source_weight_release_gate", fake_weight_gate)
    ablation = {"schema": "forecast_source_contextual_ablation_v1", "selected_specification": "global"}
    report = gate.build_hierarchical_probability_release_gate(
        root=tmp_path,
        db_path=Path(tmp_path / "archive.db"),
        include_forecast_source_weight_gate=True,
        forecast_source_weight_artifact={"artifact_sha256": "candidate"},
        forecast_source_weight_shadow={"evaluation_manifest_sha256": "evaluation"},
        forecast_source_weight_ablation=ablation,
    )

    assert report["ok"] is True
    assert captured["ablation"] == ablation
    assert captured["require_ablation"] is True
