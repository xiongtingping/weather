from __future__ import annotations

import sqlite3

from bayes_basket_target.batch_expansion_gate import (
    MAX_RKPK_GATE_FACTS,
    evaluate_45station_no_live_expansion,
)
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.station_registry import coordinate_registry_manifest
from scripts.check_bayes_basket_target_45station_expansion import (
    apply_runtime_no_live_guards,
    readiness_exit_code,
)


def _packet() -> dict:
    stations = coordinate_registry_manifest()["station_ids"]
    global_prior_id = "global-prior-v1"
    value = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "cash_phase_authority_manifests": [
            {"station_id": station} for station in stations
        ],
        "cash_phase_policies": [{"station_id": station} for station in stations],
        "global_priors": [{"global_prior_id": global_prior_id}],
        "policy_artifacts": [
            {
                "station_id": station,
                "artifact_id": f"{station}-{family}",
            }
            for station in stations
            for family in ("target", "reserve", "cadence")
        ],
        "policy_activations": [
            {
                "station_id": station,
                "stage": stage,
                "global_prior_id": global_prior_id,
                "target_artifact_id": f"{station}-target",
                "reserve_artifact_id": f"{station}-reserve",
                "cadence_artifact_id": f"{station}-cadence",
            }
            for station in stations
            for stage in ("P0", "P0-M", "P1")
        ],
        "execution_mode": "production_no_live",
        "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "promoted": False,
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    value["packet_sha256"] = content_sha256(value)
    return value


def _stores() -> tuple[sqlite3.Connection, sqlite3.Connection]:
    ssot = sqlite3.connect(":memory:")
    ssot.executescript(
        """
        CREATE TABLE basket_target_market_registry(
          station_id TEXT, settlement_timezone TEXT
        );
        CREATE TABLE basket_policy_training_fact(
          fact_id TEXT, decision_id TEXT, station_id TEXT,
          event_date TEXT
        );
        CREATE TABLE basket_policy_training_label(
          label_id TEXT, fact_id TEXT, truth_id TEXT,
          truth_revision_no INTEGER, truth_sha256 TEXT
        );
        CREATE TABLE calibration_station_daily_high_truth(
          truth_id TEXT, station_id TEXT, local_date TEXT,
          revision_no INTEGER, timestamps_complete INTEGER,
          qc_status TEXT, content_sha256 TEXT
        );
        CREATE TABLE auto_trade_decisions(
          decision_id TEXT PRIMARY KEY, details_json TEXT NOT NULL
        );
        CREATE TABLE basket_target_order_intents(
          id TEXT, live_submit_authorized INTEGER NOT NULL
        );
        CREATE TABLE basket_target_order_events(id TEXT);
        CREATE TABLE basket_target_order_reconciliations(id TEXT);
        INSERT INTO basket_target_market_registry
        VALUES('RKPK','Asia/Seoul');
        INSERT INTO basket_policy_training_fact
        VALUES('d2fact_1','decision_1','RKPK','2026-07-29');
        INSERT INTO basket_policy_training_fact
        VALUES('d2fact_2','decision_2','RKPK','2026-07-29');
        INSERT INTO auto_trade_decisions VALUES(
          'decision_1','{"evidence_role":"historical_strict_oos","forward_observation_status":"ELIGIBLE"}'
        );
        INSERT INTO auto_trade_decisions VALUES(
          'decision_2','{"evidence_role":"historical_strict_oos","forward_observation_status":"ELIGIBLE"}'
        );
        INSERT INTO calibration_station_daily_high_truth VALUES(
          'truth_1','RKPK','2026-07-29',1,1,'ACCEPTED',
          'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        );
        """
    )
    forward = sqlite3.connect(":memory:")
    forward.executescript(
        """
        CREATE TABLE forward_observation(
          observation_key TEXT, decision_id TEXT
        );
        CREATE TABLE forward_ingest_state(
          decision_id TEXT, state TEXT
        );
        CREATE TABLE forward_ingest_repair(decision_id TEXT);
        CREATE TABLE forward_settlement_label(
          observation_key TEXT, revision INTEGER, truth_id TEXT,
          truth_sha256 TEXT, source_fact_id TEXT, source_label_id TEXT
        );
        INSERT INTO forward_observation VALUES('obs_1','decision_1');
        INSERT INTO forward_observation VALUES('obs_2','decision_2');
        """
    )
    return ssot, forward


def test_batch_expansion_does_not_wait_for_future_settlement_labels() -> None:
    ssot, forward = _stores()
    result = evaluate_45station_no_live_expansion(
        ssot, forward, _packet()
    )
    assert result["state"] == "READY"
    assert result["blockers"] == []
    assert result["settlement_scorecard_state"] == "PENDING_OR_BLOCKED"
    assert result["settlement_scorecard_blockers"] == [
        "rkpk_same_day_settlement_labels_incomplete"
    ]
    assert (
        result[
            "settlement_labels_block_production_no_live_or_batch_expansion"
        ]
        is False
    )
    assert (
        result["settlement_labels_block_live_or_parameter_promotion"]
        is True
    )
    assert result["rkpk_forward_observation_count"] == 2
    assert result["forward_oos_consulted"] is False
    assert (
        result["forward_oos_blocks_production_no_live_or_batch_expansion"]
        is False
    )
    assert result["resident_materialization_state"] == "PENDING"
    assert result["resident_vertical_chain_station_count"] == 0
    assert result["resident_missing_probability_station_count"] == 45
    assert (
        result[
            "resident_materialization_blocks_expansion_authorization"
        ]
        is False
    )


def test_missing_forward_observation_is_observational_telemetry_only() -> None:
    ssot, forward = _stores()
    forward.execute("DELETE FROM forward_observation")
    result = evaluate_45station_no_live_expansion(
        ssot, forward, _packet()
    )
    assert result["state"] == "READY"
    assert result["blockers"] == []
    assert result["rkpk_forward_observation_count"] == 0
    assert result["future_observation_is_oos"] is False
    assert result["forward_observational_scorecard"][
        "blocks_production_no_live_or_batch_expansion"
    ] is False


def test_unresolved_forward_quarantine_is_observational_telemetry_only() -> None:
    ssot, forward = _stores()
    forward.execute(
        "INSERT INTO forward_ingest_state VALUES('decision-q','QUARANTINED')"
    )
    result = evaluate_45station_no_live_expansion(
        ssot, forward, _packet()
    )
    assert result["state"] == "READY"
    assert result["blockers"] == []
    assert result["rkpk_unresolved_quarantine_count"] == 1
    assert result["future_observation_evidence_role"] == (
        "observational_candidate_not_oos"
    )


def test_future_fact_does_not_enter_historical_strict_oos_scorecard() -> None:
    ssot, forward = _stores()
    ssot.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?)",
        (
            "d2fact_future",
            "decision_future",
            "RKPK",
            "2026-08-03",
        ),
    )
    ssot.execute(
        "INSERT INTO auto_trade_decisions VALUES(?,?)",
        (
            "decision_future",
            '{"evidence_role":"observational_candidate_not_oos",'
            '"forward_observation_status":"ELIGIBLE"}',
        ),
    )
    result = evaluate_45station_no_live_expansion(
        ssot, forward, _packet()
    )
    assert result["state"] == "READY"
    assert result["blockers"] == []
    assert result["rkpk_delta_v2_observational_candidate_not_oos_fact_count"] == 1
    assert result["rkpk_delta_v2_historical_strict_oos_fact_count"] == 2
    assert "2026-08-03" not in result["rkpk_event_dates"]


def test_batch_expansion_becomes_ready_after_labels_and_projection() -> None:
    ssot, forward = _stores()
    ssot.execute(
        "INSERT INTO basket_target_order_intents VALUES('intent-1',0)"
    )
    ssot.executemany(
        "INSERT INTO basket_policy_training_label VALUES(?,?,?,?,?)",
        [
            (
                "label_1",
                "d2fact_1",
                "truth_1",
                1,
                "a" * 64,
            ),
            (
                "label_2",
                "d2fact_2",
                "truth_1",
                1,
                "a" * 64,
            ),
        ],
    )
    forward.executemany(
        "INSERT INTO forward_settlement_label VALUES(?,?,?,?,?,?)",
        [
            (
                "obs_1",
                1,
                "truth_1",
                "a" * 64,
                "d2fact_1",
                "label_1",
            ),
            (
                "obs_2",
                1,
                "truth_1",
                "a" * 64,
                "d2fact_2",
                "label_2",
            ),
        ],
    )
    result = evaluate_45station_no_live_expansion(
        ssot, forward, _packet()
    )
    assert result["state"] == "READY"
    assert result["blockers"] == []
    assert result["station_count"] == 45
    assert result["orders_submitted"] == 0
    assert result["calls_order_gateway"] is False
    assert result["order_counts"]["basket_target_order_intents"] == 1
    assert result["order_counts"]["live_authorized_order_intents"] == 0
    assert result["rkpk_stale_settlement_label_count"] == 0
    assert result["settlement_scorecard_state"] == "READY"
    assert result["settlement_scorecard_blockers"] == []


def test_batch_expansion_rejects_labels_from_stale_truth_revision() -> None:
    ssot, forward = _stores()
    ssot.executemany(
        "INSERT INTO basket_policy_training_label VALUES(?,?,?,?,?)",
        [
            ("label_1", "d2fact_1", "truth_1", 1, "a" * 64),
            ("label_2", "d2fact_2", "truth_1", 1, "a" * 64),
        ],
    )
    ssot.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES("
        "'truth_2','RKPK','2026-07-29',2,1,'ACCEPTED',?)",
        ("b" * 64,),
    )

    result = evaluate_45station_no_live_expansion(
        ssot, forward, _packet()
    )

    assert result["state"] == "READY"
    assert "rkpk_same_day_settlement_labels_incomplete" in result[
        "settlement_scorecard_blockers"
    ]
    assert result["rkpk_settlement_label_count"] == 0
    assert result["rkpk_stale_settlement_label_count"] == 2


def test_batch_expansion_rejects_latest_quarantined_truth() -> None:
    ssot, forward = _stores()
    ssot.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES("
        "'truth_2','RKPK','2026-07-29',2,1,'QUARANTINED',?)",
        ("b" * 64,),
    )

    result = evaluate_45station_no_live_expansion(
        ssot, forward, _packet()
    )

    assert result["state"] == "READY"
    assert "rkpk_latest_settlement_truth_not_accepted" in result[
        "settlement_scorecard_blockers"
    ]
    assert "rkpk_same_day_settlement_labels_incomplete" in result[
        "settlement_scorecard_blockers"
    ]


def test_batch_expansion_fails_closed_on_extra_station_or_order_state() -> None:
    ssot, forward = _stores()
    packet = _packet()
    packet["cash_phase_authority_manifests"][0]["station_id"] = "XXXX"
    packet["packet_sha256"] = content_sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )
    ssot.execute("INSERT INTO basket_target_order_events VALUES('event-1')")
    result = evaluate_45station_no_live_expansion(ssot, forward, packet)
    assert "canonical_45station_authority_manifest_incomplete" in result[
        "blockers"
    ]
    assert "rkpk_no_live_order_boundary_not_zero" in result["blockers"]


def test_batch_expansion_requires_complete_non_model_policy_binding() -> None:
    ssot, forward = _stores()
    packet = _packet()
    packet["policy_activations"][0]["target_artifact_id"] = "missing"
    packet["packet_sha256"] = content_sha256(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )

    result = evaluate_45station_no_live_expansion(
        ssot, forward, packet
    )

    assert "canonical_45station_policy_binding_invalid" in result[
        "blockers"
    ]


def test_batch_expansion_rejects_live_authorized_intent() -> None:
    ssot, forward = _stores()
    ssot.execute(
        "INSERT INTO basket_target_order_intents VALUES('intent-live',1)"
    )
    result = evaluate_45station_no_live_expansion(
        ssot, forward, _packet()
    )
    assert "rkpk_no_live_order_boundary_not_zero" in result["blockers"]
    assert result["order_counts"]["live_authorized_order_intents"] == 1


def test_mutating_caller_can_require_ready_exit_status() -> None:
    assert readiness_exit_code(
        {"state": "READY"},
        require_ready=True,
    ) == 0
    assert readiness_exit_code(
        {"state": "BLOCKED_FAIL_CLOSED"},
        require_ready=True,
    ) == 3
    assert readiness_exit_code(
        {"state": "BLOCKED_FAIL_CLOSED"},
        require_ready=False,
    ) == 0


def test_batch_expansion_fact_scan_is_resource_bounded() -> None:
    ssot, forward = _stores()
    rows = [
        (
            f"d2fact_extra_{index}",
            f"decision_extra_{index}",
            "RKPK",
            "2026-07-29",
        )
        for index in range(MAX_RKPK_GATE_FACTS)
    ]
    ssot.executemany(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?)",
        rows,
    )
    ssot.executemany(
        "INSERT INTO auto_trade_decisions VALUES(?,?)",
        [
            (
                decision_id,
                '{"evidence_role":"historical_strict_oos",'
                '"forward_observation_status":"ELIGIBLE"}',
            )
            for _, decision_id, _, _ in rows
        ],
    )

    result = evaluate_45station_no_live_expansion(
        ssot, forward, _packet()
    )

    assert "rkpk_delta_v2_fact_scope_cap_exceeded" in result[
        "blockers"
    ]
    assert result["rkpk_delta_v2_fact_scope_exceeded"] is True
    assert result["rkpk_delta_v2_total_fact_count"] == (
        MAX_RKPK_GATE_FACTS
    )


def test_runtime_no_live_guard_is_required_before_expansion() -> None:
    base = {"state": "READY", "blockers": []}
    activation = {
        "execution_mode": "production_no_live",
        "state": "production_no_live",
        "live_submit_authorized": False,
        "authorizes_order": False,
    }
    ready = apply_runtime_no_live_guards(
        base,
        activation=activation,
        live_pause_present=True,
    )
    assert ready["state"] == "READY"
    assert ready["live_pause_present"] is True

    blocked = apply_runtime_no_live_guards(
        base,
        activation={**activation, "live_submit_authorized": True},
        live_pause_present=False,
    )
    assert blocked["state"] == "BLOCKED_FAIL_CLOSED"
    assert blocked["blockers"] == [
        "active_no_live_boundary_invalid",
        "live_pause_missing",
    ]
