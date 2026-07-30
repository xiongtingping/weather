from __future__ import annotations

import sqlite3
import json
from pathlib import Path

from bayes_basket_target.atomic_ledger import initialize_capital_pool
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.ssot import apply_production_ssot_schema
import scripts.bayes_basket_target_production_readiness as readiness_module
from scripts.bayes_basket_target_production_readiness import audit


ROOT = Path(__file__).resolve().parents[1]
ACTIVATION = ROOT / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"


def _audit(tmp_path: Path, *, schema: bool = False) -> dict:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        if schema:
            apply_production_ssot_schema(conn)
            initialize_capital_pool(
                conn,
                balance_usdc=0.0,
                capital_epoch_id="test-epoch",
                updated_at_utc="2026-07-23T00:00:00Z",
            )
    manifest = tmp_path / "market_ws_latest_quotes.json"
    manifest.write_text("{}\n", encoding="utf-8")
    return audit(
        db=db,
        activation_path=ACTIVATION,
        manifest_path=manifest,
        probe_services=False,
    )


def test_exact_scope_contract_fixture_failure_surfaces_reason_codes() -> None:
    with sqlite3.connect(":memory:") as conn:
        inputs = readiness_module._target_inputs(
            conn,
            as_of_utc="2026-07-27T00:00:01Z",
            policy_authority_scope="FORMAL_PROMOTED",
            target_scope={},
            station_ids=["RKPK"],
        )

    exact = inputs["exact_scope_readiness"]
    assert exact["ready"] is False
    assert exact["reason_codes"] == [
        "ContractError:readiness_season_bin_missing",
    ]


def test_forward_scorecard_gate_rejects_gateway_or_order_boundary(
    monkeypatch,
) -> None:
    receipt = {
        "schema": readiness_module.FORWARD_SCORECARD_SCHEMA,
        "production_no_live": True,
        "read_only": True,
        "calls_order_gateway": True,
        "orders_submitted": 1,
        "counts": {
            "fact_rows_evaluated": 1,
            "by_station": {"RKPK": {"facts": 1}},
        },
        "ready_for_cash_scorecard": True,
        "ready_for_station_scorecard": True,
        "blockers": [],
        "content_sha256": "a" * 64,
    }
    monkeypatch.setattr(
        readiness_module,
        "build_forward_scorecard",
        lambda *args, **kwargs: receipt,
    )
    with sqlite3.connect(":memory:") as conn:
        result = readiness_module._forward_scorecard_status(
            conn,
            as_of_utc="2026-07-27T00:00:01Z",
            station_ids=["RKPK"],
        )

    assert result["ready"] is False
    assert result["state"] == "BLOCKED"
    assert result["calls_order_gateway"] is True
    assert "forward_scorecard_gateway_boundary_invalid" in result["blockers"]
    assert "forward_scorecard_order_boundary_invalid" in result["blockers"]


def test_empty_real_shape_fails_closed_with_explicit_blockers(tmp_path: Path) -> None:
    result = _audit(tmp_path)
    assert result["ok"] is False
    assert "target_production_ssot_schema_not_installed" in result["blockers"]
    assert "target_m5_ledger_not_installed" in result["blockers"]
    assert "target_materialized_inputs_incomplete" in result["blockers"]
    assert "target_capital_epoch_not_initialized" in result["blockers"]
    assert result["boundary"]["places_orders"] is False


def test_installed_schema_still_requires_inputs_and_db_activation_alignment(tmp_path: Path) -> None:
    result = _audit(tmp_path, schema=True)
    assert result["ok"] is False
    assert "target_materialized_inputs_incomplete" in result["blockers"]
    assert "file_db_activation_drift" in result["blockers"]
    assert result["capital_pool"]["ready"] is True
    assert result["migration"]["target_ledger_schema"]["ok"] is True
    assert result["official_ws_scope_coverage"]["state"] == "blocked"
    assert result["resource_budget"]["ok"] is True
    codes = {item["code"] for item in result["unfinished_target_inputs"]}
    assert {
        "probability_materialization",
        "official_ws_materialization",
        "q0_inventory_materialization",
        "order_state_materialization",
            "cash_phase_authority",
            "station_policy_authority",
        "calibration_no_live_authority",
    } <= codes


def test_legacy_decision_collision_is_not_hidden_by_other_ready_parts(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE auto_trade_decisions(id INTEGER PRIMARY KEY, will_order INTEGER)")
    manifest = tmp_path / "market_ws_latest_quotes.json"
    manifest.write_text("{}\n", encoding="utf-8")
    result = audit(db=db, activation_path=ACTIVATION, manifest_path=manifest, probe_services=False)
    assert result["migration"]["legacy_decision_table_collision"] is True
    assert "legacy_auto_trade_decisions_collision" in result["blockers"]


def test_missing_database_is_a_single_safe_block_without_sql_side_effect(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"
    manifest = tmp_path / "market_ws_latest_quotes.json"
    manifest.write_text("{}\n", encoding="utf-8")
    result = audit(db=db, activation_path=ACTIVATION, manifest_path=manifest, probe_services=False)
    assert result["ok"] is False
    assert result["database"] == str(db.resolve())
    assert result["boundary"]["read_only_audit"] is True
    assert not db.exists()


def test_default_target_ws_manifest_is_not_the_retired_resident_manifest() -> None:
    assert readiness_module.TARGET_WS_MANIFEST != readiness_module.RESIDENT_WS_MANIFEST
    assert readiness_module.TARGET_WS_MANIFEST.name == "bayes_basket_target_official_ws_manifest.json"
    assert readiness_module.RESIDENT_WS_MANIFEST.name == "market_ws_latest_quotes.json"


def test_p0_preselection_is_optional_after_final_p1_chain_materializes() -> None:
    counts = {
        table: 1 for table in readiness_module.TARGET_INPUT_TABLES
    }
    counts["basket_p0_preselection_plan"] = 0
    counts["calibration_artifact_promotion"] = 0
    counts["calibration_activation"] = 0

    assert readiness_module._required_materialized_counts_ready(counts) is True

    counts["basket_order_state_snapshot"] = 0
    assert readiness_module._required_materialized_counts_ready(counts) is False


def test_future_p0_never_masquerades_as_current_et_final_p1_chain() -> None:
    inputs = {
        "counts": {
            "basket_probability_envelope": 10,
            "basket_official_ws_snapshot": 10,
            "basket_p0_preselection_plan": 10,
        },
        "calibration_no_live_active": True,
        "probability_stage_counts": {"P0": 10},
        "probability_started_settlement_stage_counts": {},
        "station_policy_promoted": True,
        "cash_phase_promoted": True,
                "q0_inventory_materialized": True,
                "order_state_materialized": True,
                "exact_scope_readiness": {
                    "ready": True,
                    "scope_count": 1,
                    "blocked_scope_count": 0,
                },
                "operational_counts": {
            "observation_facts": 1,
            "execution_cycles": 1,
            "decisions": 1,
            "reconciliations": 1,
        },
        "operational_event_day_counts": {
            "execution_cycles": {"started_settlement": 1, "future_settlement": 0},
            "decisions": {"started_settlement": 1, "future_settlement": 0},
        },
    }
    result = readiness_module._main_chain_status(
        inputs,
        calibration_evidence={
            "checks": {"model_provider_set_complete": True},
            "et_history_evidence": {
                "state": "READY_FOR_STRICT_ASOF",
                "joined_station_days": 10,
            },
        },
        native_decode_evidence={"complete_curve_count": 1},
        ws_coverage={
            "complete_scope_count": 1,
            "incomplete_scope_count": 0,
        },
    )

    assert result["preliminary_p0_chain"]["ready"] is True
    assert result["final_p1_chain"]["ready"] is False
    assert result["blocked_at"] == "P0_P1"
    p0_p1 = next(
        stage for stage in result["stages"] if stage["id"] == "P0_P1"
    )
    assert p0_p1["observed"]["started_settlement_p1_count"] == 0


def test_blocked_no_order_decision_closes_reconciliation_stage_without_fake_row() -> None:
    result = readiness_module._main_chain_status(
        {
            "counts": {
                "basket_probability_envelope": 1,
                "basket_official_ws_snapshot": 1,
                "basket_p0_preselection_plan": 1,
                "calibration_model_artifact": 1,
                "calibration_ensemble_artifact": 1,
                "calibration_activation": 1,
            },
            "calibration_no_live_active": True,
            "probability_stage_counts": {"P1": 1},
            "probability_started_settlement_stage_counts": {"P1": 1},
            "station_policy_authorized": True,
            "cash_phase_authorized": True,
            "q0_inventory_materialized": True,
            "order_state_materialized": True,
            "exact_scope_readiness": {
                "ready": True,
                "scope_count": 1,
                "blocked_scope_count": 0,
            },
            "operational_counts": {
                "observation_facts": 1,
                "execution_cycles": 1,
                "decisions": 1,
                "non_blocked_decisions": 0,
                "order_intents": 0,
                "capital_reservations": 0,
                "order_events": 0,
                "reconciliations": 0,
            },
            "operational_event_day_counts": {
                "execution_cycles": {"started_settlement": 1, "future_settlement": 0},
                "decisions": {"started_settlement": 1, "future_settlement": 0},
            },
        },
        calibration_evidence={
            "checks": {"model_provider_set_complete": True},
            "et_history_evidence": {
                "state": "READY_FOR_STRICT_ASOF",
                "joined_station_days": 1,
            },
        },
        native_decode_evidence={"complete_curve_count": 3},
        ws_coverage={"complete_scope_count": 1, "incomplete_scope_count": 0},
    )

    assert result["ready"] is True
    assert result["blocked_at"] is None
    assert result["stages"][-1]["state"] == "NO_RECONCILIATION_REQUIRED"


def test_scoped_ws_coverage_only_reads_the_explicit_station_allowlist(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        readiness_module,
        "load_official_ws_quotes",
        lambda _path: (
            {
                "state": "connected",
                "evidence_only": True,
                "authorizes_order": False,
                "target_local_date": "2026-07-25",
            },
            {"token-awc": {"token_id": "token-awc"}},
        ),
    )

    def _registry(_conn, **kwargs):
        calls.append(kwargs)
        return [{"station_id": "AWC1", "event_date": "2026-07-25", "yes_token": "token-awc"}]

    monkeypatch.setattr(readiness_module, "load_started_registry_rows", _registry)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    with sqlite3.connect(":memory:") as conn:
        result = readiness_module._official_ws_scope_coverage(
            conn,
            manifest,
            station_ids=["awc1"],
        )

    assert calls == [{"station_id": "AWC1", "event_date": "2026-07-25"}]
    assert result["station_scope"]["source_lane"] == "AWC_ONLY"
    assert result["registry_scope_count"] == 1
    assert result["complete_scope_count"] == 1


def _multi_scope_ws_manifest() -> dict:
    quote = {
        "token_id": "token-awc",
        "side": "YES",
        "bids": [{"price": 0.4, "size": 10.0}],
        "asks": [{"price": 0.42, "size": 10.0}],
        "book_observed_at": "2026-07-25T05:00:00Z",
    }
    nested = {
        "schema": "clob_ws_latest_quote_snapshot_v1",
        "state": "connected",
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
        "active_shards": [0],
        "expected_shards": 1,
        "quote_storage": "inline_v1",
        "quotes_by_token": {"token-awc": quote},
        "unmaterialized_tokens": [],
        "station_scope": {
            "mode": "allowlist",
            "station_ids": ["AWC1"],
        },
        "target_local_date": "2026-07-25",
    }
    nested["snapshot_sha256"] = content_sha256(nested)
    summary = {
        "scope_key": "AWC1|2026-07-25",
        "station_id": "AWC1",
        "event_date": "2026-07-25",
        "scope_manifest_sha256": nested["snapshot_sha256"],
    }
    root = {
        "schema": readiness_module.MULTI_SCOPE_WS_MANIFEST_SCHEMA,
        "state": "connected",
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
        "scopes": [summary],
        "scope_manifests": {"AWC1|2026-07-25": nested},
    }
    root["snapshot_sha256"] = content_sha256(root)
    return root


def test_multi_scope_ws_coverage_verifies_exact_nested_scope(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def _registry(_conn, **kwargs):
        calls.append(kwargs)
        return [{
            "station_id": "AWC1",
            "event_date": "2026-07-25",
            "yes_token": "token-awc",
        }]

    monkeypatch.setattr(
        readiness_module,
        "load_started_registry_rows",
        _registry,
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(_multi_scope_ws_manifest()),
        encoding="utf-8",
    )
    with sqlite3.connect(":memory:") as conn:
        result = readiness_module._official_ws_scope_coverage(
            conn,
            manifest,
            station_ids=["AWC1"],
        )

    assert calls == [{
        "station_id": "AWC1",
        "event_date": "2026-07-25",
    }]
    assert result["state"] == "loaded"
    assert result["complete_scope_count"] == 1
    assert result["incomplete_scope_count"] == 0
    assert result["loaded_quote_count"] == 1


def test_multi_scope_ws_coverage_rejects_nested_tamper(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        readiness_module,
        "load_started_registry_rows",
        lambda *_args, **_kwargs: [],
    )
    payload = _multi_scope_ws_manifest()
    payload["scope_manifests"]["AWC1|2026-07-25"][
        "quotes_by_token"
    ]["token-awc"]["asks"][0]["price"] = 0.99
    payload["snapshot_sha256"] = content_sha256({
        key: value
        for key, value in payload.items()
        if key != "snapshot_sha256"
    })
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with sqlite3.connect(":memory:") as conn:
        result = readiness_module._official_ws_scope_coverage(
            conn,
            manifest,
            station_ids=["AWC1"],
        )

    assert result["state"] == "blocked"
    assert "content_sha_mismatch" in result["reason"]


def test_explicit_evidence_path_is_not_replaced_by_latest_receipt(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.json"
    explicit.write_text("{}", encoding="utf-8")
    selected = readiness_module._resolve_latest_valid_evidence(
        explicit,
        default_path=tmp_path / "default.json",
        glob_pattern="*.json",
        parser=lambda _path: {"state": "VALID_BLOCKED_EVIDENCE"},
        valid_states={"VALID_BLOCKED_EVIDENCE"},
    )
    assert selected == explicit.resolve()


def test_rejected_promotion_receipts_do_not_satisfy_promoted_gate(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        apply_production_ssot_schema(conn)
        conn.execute(
            "INSERT INTO basket_policy_promotion VALUES(?,?,?,?,?,?,?,?)",
            ("rejected", "STATION_POLICY", "candidate", 1, "REJECTED", "gate", "2026-07-23T00:00:00Z", "a" * 64),
        )
        conn.commit()
    manifest = tmp_path / "market_ws_latest_quotes.json"
    manifest.write_text("{}\n", encoding="utf-8")
    result = audit(db=db, activation_path=ACTIVATION, manifest_path=manifest, probe_services=False)
    assert result["target_inputs"]["promoted_or_activated_counts"]["station_policy_promotion"] == 0
    assert result["target_inputs"]["station_policy_promoted"] is False


def test_candidate_calibration_evidence_is_reported_but_never_counts_as_ready(tmp_path: Path) -> None:
    evidence = {
        "schema": "bayes_basket_target_calibration_evidence_v1",
        "checks": {
            "promotion_allowed": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
        },
        "boundary": {"live_order_authorized": False},
        "calibration_artifacts": [{"promoted": False}],
        "ensemble_artifacts": [{"promoted": False}],
        "promotion_decisions": [{"decision": "REJECTED"}],
    }
    evidence["receipt_sha256"] = content_sha256(evidence)
    evidence_path = tmp_path / "calibration_evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    db = tmp_path / "archive.db"
    with sqlite3.connect(db):
        pass
    manifest = tmp_path / "market_ws_latest_quotes.json"
    manifest.write_text("{}\n", encoding="utf-8")
    result = audit(
        db=db,
        activation_path=ACTIVATION,
        manifest_path=manifest,
        calibration_evidence_path=evidence_path,
        probe_services=False,
    )
    assert result["calibration_evidence"]["state"] == "VALID_CANDIDATE_EVIDENCE"
    assert result["calibration_evidence"]["promotion_allowed"] is False
    assert "target_materialized_inputs_incomplete" in result["blockers"]


def test_incomplete_three_model_join_is_valid_blocked_evidence(tmp_path: Path) -> None:
    evidence = {
        "schema": "bayes_basket_target_calibration_evidence_v1",
        "checks": {
            "model_provider_set_complete": False,
            "residual_count_positive": False,
            "candidate_artifact_count_positive": False,
            "promotion_allowed": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
        },
        "boundary": {"live_order_authorized": False},
        "calibration_artifacts": [],
        "ensemble_artifacts": [],
        "promotion_decisions": [],
    }
    evidence["receipt_sha256"] = content_sha256(evidence)
    evidence_path = tmp_path / "calibration_evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    db = tmp_path / "archive.db"
    with sqlite3.connect(db):
        pass
    manifest = tmp_path / "market_ws_latest_quotes.json"
    manifest.write_text("{}\n", encoding="utf-8")

    result = audit(
        db=db,
        activation_path=ACTIVATION,
        manifest_path=manifest,
        calibration_evidence_path=evidence_path,
        probe_services=False,
    )

    assert result["calibration_evidence"]["state"] == "VALID_BLOCKED_EVIDENCE"
    assert result["calibration_evidence"]["blocked_without_candidates"] is True
    assert "calibration_evidence_invalid" not in result["blockers"]


def test_normalized_native_decode_projection_preserves_complete_curve_count() -> None:
    items = readiness_module._unfinished_target_inputs(
        {"counts": {}, "probability_materialized": True},
        ws_coverage={"incomplete_scope_count": 0},
        calibration_evidence={"checks": {}},
        native_decode_evidence={
            "state": "VALID_NATIVE_DECODE_EVIDENCE",
            "decoded_point_count": 11,
            "complete_curve_count": 1,
        },
    )

    assert not any(item["code"] == "native_model_curve_materialization" for item in items)


def test_valid_native_decode_suppresses_duplicate_arrival_missing_blocker() -> None:
    items = readiness_module._unfinished_target_inputs(
        {"counts": {}, "probability_materialized": True},
        ws_coverage={"incomplete_scope_count": 0},
        calibration_evidence={
            "checks": {"model_provider_set_complete": False},
            "et_history_evidence": {"state": "BLOCKED_EVIDENCE"},
        },
        native_decode_evidence={
            "state": "VALID_NATIVE_DECODE_EVIDENCE",
            "decoded_point_count": 675,
            "complete_curve_count": 135,
        },
    )

    assert any(item["code"] == "et_history_compatibility" for item in items)
    assert not any(item["code"] == "three_model_arrival_evidence" for item in items)


def test_main_chain_status_stops_at_three_model_before_et_materialization() -> None:
    result = readiness_module._main_chain_status(
        {
            "counts": {
                "basket_probability_envelope": 0,
                "basket_official_ws_snapshot": 0,
                "calibration_model_artifact": 0,
                "calibration_ensemble_artifact": 0,
                "calibration_activation": 0,
            },
            "calibration_no_live_active": False,
            "operational_counts": {
                "observation_facts": 5731,
                "execution_cycles": 0,
                "decisions": 0,
                "reconciliations": 0,
            },
        },
        calibration_evidence={
            "checks": {"model_provider_set_complete": False},
            "et_history_evidence": {"state": "BLOCKED_EVIDENCE", "joined_station_days": 0},
        },
        native_decode_evidence={"complete_curve_count": 135},
        ws_coverage={"complete_scope_count": 0, "incomplete_scope_count": 163},
    )
    assert result["blocked_at"] == "THREE_MODEL"
    assert result["stages"][1]["state"] == "MISSING"
    assert result["stages"][2]["state"] == "BLOCKED_EVIDENCE"
    assert result["stages"][-1]["ready"] is False
    assert result["live_submit_authorized"] is False


def test_provider_local_forward_curves_do_not_satisfy_three_model_stage() -> None:
    result = readiness_module._main_chain_status(
        {
            "counts": {
                "basket_probability_envelope": 0,
                "basket_official_ws_snapshot": 0,
            },
            "calibration_no_live_active": False,
            "operational_counts": {
                "observation_facts": 5731,
                "execution_cycles": 134,
                "decisions": 134,
                "reconciliations": 0,
            },
            "operational_event_day_counts": {
                "execution_cycles": {"started_et": 134, "future_et": 0},
                "decisions": {"started_et": 134, "future_et": 0},
            },
        },
        calibration_evidence={
            "checks": {"model_provider_set_complete": False},
            "et_history_evidence": {"state": "BLOCKED_EVIDENCE", "joined_station_days": 0},
        },
        native_decode_evidence={"complete_curve_count": 135},
        ws_coverage={"complete_scope_count": 0, "incomplete_scope_count": 134},
    )
    assert result["blocked_at"] == "THREE_MODEL"
    assert result["stages"][1]["ready"] is False
    assert result["stages"][6]["ready"] is False
    assert result["stages"][7]["ready"] is False


def test_old_blocked_cycles_cannot_make_basket_stage_ready() -> None:
    result = readiness_module._main_chain_status(
        {
            "counts": {
                "basket_probability_envelope": 0,
                "basket_official_ws_snapshot": 0,
            },
            "calibration_no_live_active": False,
            "operational_counts": {
                "observation_facts": 5731,
                "execution_cycles": 134,
                "decisions": 134,
                "reconciliations": 0,
            },
            "operational_event_day_counts": {
                "execution_cycles": {"started_et": 134, "future_et": 0},
                "decisions": {"started_et": 134, "future_et": 0},
            },
        },
        calibration_evidence={
            "checks": {"model_provider_set_complete": True},
            "et_history_evidence": {"state": "READY_FOR_STRICT_ASOF", "joined_station_days": 1},
        },
        native_decode_evidence={"complete_curve_count": 135},
        ws_coverage={"complete_scope_count": 0, "incomplete_scope_count": 134},
    )
    assert result["blocked_at"] == "CALIBRATION_ENSEMBLE"
    assert result["stages"][6]["ready"] is False
    assert result["stages"][7]["ready"] is False
