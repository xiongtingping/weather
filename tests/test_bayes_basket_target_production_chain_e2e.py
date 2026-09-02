from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.pr12_atomic_test_support import awc_contract, install_canonical_v12

from bayes_basket_target.atomic_ledger import (
    AtomicLedgerError,
    apply_target_ledger_schema,
    close_target_cycle,
    commit_target_atomic_bundle,
    initialize_capital_pool,
    transition_target_cycle_state,
)
from bayes_basket_target.contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.delta_v2_artifact import (
    ARTIFACT_SCHEMA as DELTA_V2_ARTIFACT_SCHEMA,
    DeltaV2PolicyArtifact,
)
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
)
from bayes_basket_target.production import commit_production_candidate_atomic, compile_production_plan
from bayes_basket_target.production_io import target_atomic_facts_from_cycle
from bayes_basket_target.production_worker import TargetProductionWorker
from bayes_basket_target.regime_materialization import build_regime_artifact
from bayes_basket_target.ssot import apply_production_ssot_schema
from tests.bayes_basket_target_e2e_fixtures import (
    DECISION,
    _cash_input,
    _cash_policy,
    _envelope,
    _market_snapshot,
    _policy_inputs,
)


def _inputs() -> tuple[list[ProbabilityEnvelope], dict, dict, list, dict, object]:
    _contract, revision_sha = awc_contract(
        station_id="ZUUU", observation_id="obs-e2e-1",
        event_date="2026-07-23", settlement_timezone="Asia/Shanghai",
        observed_at_utc="2026-07-23T04:00:00Z", raw_object_sha256="a" * 64,
    )
    prior, artifacts, activations = _policy_inputs()
    # Make the local rehearsal exercise all three legs.  This is a test-only
    # policy; it is not an activation or a production default.
    default_policy = dict(prior.default_policy)
    default_policy.update({
        "base_coverage": 0.95,
        "maximum_coverage": 1.0,
        "total_budget_usdc": 12.0,
        "clip_min_notional_usdc": 0.1,
    })
    prior = prior.create(
        global_prior_id=prior.global_prior_id,
        strategy_version=prior.strategy_version,
        family_parameters=prior.family_parameters,
        default_policy=default_policy,
        valid_from_utc=prior.valid_from_utc,
        valid_until_utc=prior.valid_until_utc,
    )
    envelopes: list[ProbabilityEnvelope] = []
    for stage, checkpoint in (("P0", "e2e-p0"), ("P0-M", "e2e-p0m"), ("P1", "e2e-p1")):
        raw = _envelope(stage, checkpoint).to_dict()
        raw["lineage"] = {
            **raw["lineage"],
            "canonical_observation_event_id": "obs-e2e-1",
            "truth_observation_revision_content_sha256": revision_sha,
            "canonical_observation_committed_at": (
                awc_contract(
                    station_id="ZUUU", observation_id="obs-e2e-1",
                    event_date="2026-07-23", settlement_timezone="Asia/Shanghai",
                    observed_at_utc="2026-07-23T04:00:00Z", raw_object_sha256="a" * 64,
                )[0]["revision_facts"][0]["canonical_committed_at_utc"]
            ),
            "settlement_timezone": "Asia/Shanghai",
            "hard_bound_id": "bound-e2e-1",
            "reported_high_c": "37",
            "reported_hard_lower_bound": "37",
            "reported_high_bucket": "38",
            "reported_high_ordinal": "38",
            "settlement_unit": "C",
            "source_lane": "AWC",
            "report_type": "METAR",
            "hard_excluded_buckets": "37",
            "model_envelope_lower_bucket": "37",
            "model_envelope_upper_bucket": "39",
            "model_envelope_span_c": "2",
        }
        envelopes.append(ProbabilityEnvelope.from_mapping(raw))
    market = _market_snapshot()
    for row in market["buckets"]:
        row["yes_token"] = "yes-" + str(row["bucket"])
        row["filled_shares"] = 0.0
        row["pending_shares"] = 0.0
    cash_policy = _cash_policy("e2e-cash-heavy-v1", 6.0)
    return envelopes, {prior.global_prior_id: prior}, {item.artifact_id: item for item in artifacts}, activations, market, cash_policy


def _delta_v2_policy() -> DeltaV2PolicyArtifact:
    raw = {
        "schema": DELTA_V2_ARTIFACT_SCHEMA,
        "artifact_id": "delta-v2-e2e-v1",
        "station_id": "ZUUU",
        "stage": "P1",
        "season_bin": "summer",
        "maturity_bin": "d1",
        "liquidity_bin": "liquid",
        "strategy_version": "target-v1",
        "value_weights": {
            "lambda_profit_floor": 0.0,
            "lambda_cvar": 0.0,
            "lambda_shortfall": 0.0,
            "lambda_breakthrough": 0.0,
            "lambda_overfill": 0.0,
            "lambda_excess_hhi": 0.0,
            "cvar_alpha": 0.1,
            "hhi_max": 1.0,
            "expected_pnl_scale": 1.0,
            "profit_floor_scale": 1.0,
            "cvar_scale": 1.0,
            "coverage_scale": 1.0,
            "overfill_scale": 1.0,
            "excess_hhi_scale": 1.0,
        },
        "execution_weights": {
            "epsilon_score": 0.000001,
            "lambda_wait": 0.0,
            "lambda_queue": 0.0,
            "lambda_adverse_selection": 0.0,
            "lambda_reprice": 0.0,
            "lambda_capital_lock": 0.0,
            "wait_scale": 1.0,
            "queue_scale": 1.0,
            "adverse_selection_scale": 1.0,
            "reprice_scale": 1.0,
            "capital_lock_scale": 1.0,
            "maximum_candidates": 64,
        },
        "cash_clip_grid": [0.5],
        "price_grid": [0.1],
        "timeout_grid_seconds": [30.0],
        "fill_distribution_artifact_sha256": "1" * 64,
        "training_evidence_sha256": "2" * 64,
        "train_start_date": "2026-05-01",
        "train_end_date": "2026-05-31",
        "holdout_start_date": "2026-06-01",
        "holdout_end_date": "2026-06-30",
        "sample_n": 200,
        "holdout_n": 80,
        "consecutive_forward_windows": 2,
        "compared_baselines": ["EV_ONLY", "HOLD", "ZERO_WEIGHT", "v1"],
        "oos_metrics": {
            "delta_score_lift_vs_v1": 0.1,
            "profit_floor_lift": 0.0,
            "resource_failures": 0.0,
        },
        "oos_gate_version": "delta-v2-e2e-oos-v1",
        "oos_gate_passed": True,
        "trained_through_utc": "2026-07-20T00:00:00Z",
        "valid_from_utc": "2026-07-21T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE",
    }
    raw["content_sha256"] = content_sha256(raw)
    return DeltaV2PolicyArtifact.from_mapping(raw)


def _fill_resolver(**_kwargs: object) -> dict[str, object]:
    return {
        "fill_distribution": [
            {"probability": 0.1, "fill_fraction": 0.0},
            {"probability": 0.2, "fill_fraction": 0.5},
            {"probability": 0.7, "fill_fraction": 1.0},
        ],
        "execution_costs": {
            "wait": 0.0,
            "queue": 0.0,
            "adverse_selection": 0.0,
            "reprice": 0.0,
            "capital_lock": 0.0,
        },
        "fill_artifact_content_sha256": "3" * 64,
    }


def test_p1_to_plan_to_multileg_atomic_ledger_is_deterministic_and_no_live(
    tmp_path: Path,
) -> None:
    envelopes, priors, artifacts, activations, market, cash_policy = _inputs()
    delta_policy = _delta_v2_policy()
    compiled = compile_production_plan(
        probability_envelopes=envelopes,
        market_snapshot=market,
        policy_activations=activations,
        global_priors=priors,
        policy_artifacts=artifacts,
        evaluation_at_utc=DECISION,
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        base_policy={"clip_max_notional_usdc": 3.0},
        delta_v2_policy=delta_policy,
        delta_v2_fill_resolver=_fill_resolver,
        regime_artifact=build_regime_artifact(envelopes[-1]),
        cash_phase_policy=cash_policy,
        cash_phase_input=_cash_input(cash_policy),
        resource_probe_ok=True,
    )
    compiled["production_inputs"] = {
        "strategy_version": "target-v1",
        "season_bin": "summer",
        "probability_maturity_bin": "d1",
        "liquidity_bin": "liquid",
        "cash_phase_policy_version": cash_policy.policy_version,
        "cash_phase_policy_sha256": cash_policy.content_sha256,
        "delta_v2_artifact_id": delta_policy.artifact_id,
        "delta_v2_artifact_sha256": delta_policy.content_sha256,
        "delta_v2_fill_distribution_artifact_sha256": (
            delta_policy.fill_distribution_artifact_sha256
        ),
        "delta_v2_resolution_blocker": None,
    }
    assert compiled["status"] == "ready_no_live"
    assert compiled["execution_mode"] == "production_no_live"
    assert compiled["live_submit_authorized"] is False
    assert compiled["plan"]["delta_v2_audit"]["authorized"] is True
    eligible_count = len([
        row for row in compiled["order_sizing_decisions"]
        if row["eligible"] and row["candidate_present"]
    ])
    assert eligible_count == len(
        compiled["plan"]["execution_gap_support"]
    )
    assert eligible_count > 1

    contract, _revision_sha = awc_contract(
        station_id="ZUUU", observation_id="obs-e2e-1",
        event_date="2026-07-23", settlement_timezone="Asia/Shanghai",
        observed_at_utc="2026-07-23T04:00:00Z", raw_object_sha256="a" * 64,
    )
    compiled["awc_observation_contract"] = contract

    facts = target_atomic_facts_from_cycle(compiled)
    incomplete = json.loads(json.dumps(compiled))
    incomplete["order_sizing_decisions"] = incomplete[
        "order_sizing_decisions"
    ][:-1]
    with sqlite3.connect(":memory:") as incomplete_conn:
        with pytest.raises(
            ContractError,
            match=(
                "production_candidate_complete_basket_intents_incomplete"
            ),
        ):
            commit_production_candidate_atomic(
                incomplete_conn,
                compiled_plan=incomplete,
                market_snapshot=market,
                awc_observation_contract=facts["awc_observation_contract"],
                truth_observation_revision_content_sha256=facts["truth_observation_revision_content_sha256"],
                bound=facts["bound"],
                created_at_utc=DECISION,
            )
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        install_canonical_v12(conn)
        apply_target_market_registry_schema(conn)
        conn.execute(
            "INSERT INTO basket_target_market_registry_snapshot "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "1" * 64, "reported_trajectory_market_registry_v1",
                "shared_live_registry_export_only", DECISION,
                "/read-only/e2e.db", 1, DECISION, "2" * 64,
            ),
        )
        conn.execute(
            """
            INSERT INTO basket_target_market_registry(
              snapshot_sha256,slug,event_date,outcome,city_slug,station_id,
              settlement_timezone,settlement_unit,source_lane,source_eligible,
              source_block_reason,resolution_url,rules_sha256,yes_token,
              no_token,market_active,market_accepting_orders,registry_json,
              refreshed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "1" * 64, "e2e-market", "2026-07-23", "38", "chengdu",
                "ZUUU", "Asia/Shanghai", "C", "awc", 1, "", "",
                "3" * 64, "yes-38", "no-38", 1, 1, "{}", DECISION,
            ),
        )
        p1 = envelopes[-1]
        p1_raw = p1.to_dict()
        conn.execute(
            """
            INSERT INTO basket_probability_envelope VALUES(
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                p1.checkpoint_id, p1.stage, p1.checkpoint_id,
                p1.station_id, p1.event_date, p1.settlement_timezone,
                p1.decision_at_utc, p1.fresh_until_utc,
                json.dumps(p1_raw["bucket_ordinals"]),
                json.dumps(p1_raw["probabilities"]),
                p1.posterior_version, p1.posterior_sha256,
                p1.bridge_sha256, json.dumps(p1_raw["lineage"]),
                p1.content_sha256, "COMMITTED", DECISION,
            ),
        )
        conn.execute(
                """
                INSERT INTO basket_official_ws_snapshot VALUES(
                  ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
            (
                market["snapshot_id"], market["market_id"],
                market["station_id"], market["event_date"],
                    market["settlement_timezone"], market["sequence"],
                    market["observed_at"], market["quote_version"],
                    market["quote_observed_at"], market["quote_expires_at"],
                    "7" * 64, "OFFICIAL_CLOB_WS",
                    market["inventory_version"], market["order_state_version"],
                market["remaining_budget_usdc"],
                json.dumps(market["buckets"]),
                json.dumps(_cash_input(cash_policy).to_dict()),
                "4" * 64, "COMMITTED", DECISION,
            ),
        )
        conn.execute(
            "INSERT INTO basket_confirmed_inventory_snapshot "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "e2e-inventory-snapshot", "ZUUU", "2026-07-23",
                market["inventory_version"], market["observed_at"], 1,
                "{}", "5" * 64, "COMMITTED", DECISION,
            ),
        )
        conn.execute(
            "INSERT INTO basket_order_state_snapshot "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "e2e-order-state-snapshot", "ZUUU", "2026-07-23",
                market["order_state_version"], market["observed_at"], 1,
                "{}", "6" * 64, "COMMITTED", DECISION,
            ),
        )
        initialize_capital_pool(
            conn, balance_usdc=30.0, capital_epoch_id="e2e-capital-v1", updated_at_utc=DECISION
        )
        conn.execute(
            """
            CREATE TRIGGER inject_production_training_fact_failure
            BEFORE INSERT ON basket_policy_training_fact
            BEGIN
              SELECT RAISE(ABORT, 'injected_production_training_fact_failure');
            END
            """
        )
        with pytest.raises(
            AtomicLedgerError,
            match="training_fact_canonical_write_failed",
        ):
            commit_production_candidate_atomic(
                conn,
                compiled_plan=compiled,
                market_snapshot=market,
                awc_observation_contract=facts["awc_observation_contract"],
                truth_observation_revision_content_sha256=facts["truth_observation_revision_content_sha256"],
                bound=facts["bound"],
                created_at_utc=DECISION,
            )
        for table in (
            "basket_target_observation_facts",
            "basket_target_bound_facts",
            "basket_target_execution_cycles",
            "auto_trade_decisions",
            "basket_target_order_intents",
            "basket_target_execution_version_vectors",
            "basket_target_capital_reservations",
            "basket_policy_training_fact",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0] == 0
        conn.execute("DROP TRIGGER inject_production_training_fact_failure")

        first = commit_production_candidate_atomic(
            conn,
            compiled_plan=compiled,
            market_snapshot=market,
            awc_observation_contract=facts["awc_observation_contract"],
            truth_observation_revision_content_sha256=facts["truth_observation_revision_content_sha256"],
            bound=facts["bound"],
            created_at_utc=DECISION,
        )
        assert first["state"] == "committed"
        assert first["receipt"]["intent_count"] == eligible_count
        assert first["receipt"]["training_fact_state"] == "committed"
        assert first["delta_v2_training_fact"]["state"] == "committed"
        assert first["orders_submitted"] == 0
        assert first["places_orders"] is False
        assert first["calls_order_gateway"] is False
        forward = first["forward_observation_receipt"]
        assert forward["cycle_id"] == first["receipt"]["cycle_id"]
        assert forward["decision_id"] == first["receipt"]["decision_id"]
        assert forward["decision_at_utc"] == DECISION
        assert forward["forward_evidence_binding"]["probability_decision_at_utc"] <= DECISION
        assert forward["forward_evidence_binding"]["ws_quote_observed_at_utc"] <= DECISION
        assert forward["forward_evidence_binding"]["cash_observed_at_utc"] <= DECISION
        assert forward["calls_order_gateway"] is False
        assert forward["authorizes_order"] is False

        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents"
        ).fetchone()[0] == eligible_count
        assert conn.execute("SELECT will_order FROM auto_trade_decisions").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents WHERE live_submit_authorized=0"
        ).fetchone()[0] == eligible_count
        execution_candidates = {
            json.loads(str(row[0])).get("execution_candidate")
            for row in conn.execute(
                "SELECT intent_json FROM basket_target_order_intents"
            ).fetchall()
        }
        assert execution_candidates == {"MAKER"}
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors "
            "WHERE vector_policy='EXECUTION'"
        ).fetchone()[0] == eligible_count
        assert conn.execute(
            "SELECT COUNT(DISTINCT target_price_id) "
            "FROM basket_target_execution_version_vectors "
            "WHERE vector_policy='EXECUTION'"
        ).fetchone()[0] == eligible_count
        fact_id = conn.execute(
            "SELECT fact_id FROM basket_policy_training_fact"
        ).fetchone()[0]
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors "
            "WHERE vector_policy='EXECUTION' AND training_fact_id=?",
            (fact_id,),
        ).fetchone()[0] == eligible_count
        reserved = conn.execute(
            "SELECT reserved_usdc FROM basket_target_capital_pool"
        ).fetchone()[0]
        assert reserved == pytest.approx(first["receipt"]["reserved_amount_usdc"])

        replay = commit_production_candidate_atomic(
            conn,
            compiled_plan=compiled,
            market_snapshot=market,
            awc_observation_contract=facts["awc_observation_contract"],
            truth_observation_revision_content_sha256=facts["truth_observation_revision_content_sha256"],
            bound=facts["bound"],
            created_at_utc=DECISION,
        )
        assert replay["state"] == "already_committed"
        assert replay["delta_v2_training_fact"]["state"] == "already_committed"
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents"
        ).fetchone()[0] == eligible_count
        assert conn.execute("SELECT version FROM basket_target_capital_pool").fetchone()[0] == 1

        blocked = json.loads(json.dumps(compiled))
        blocked.update(
            {
                "status": "blocked",
                "cadence_allows_candidate": False,
                "cadence_decision": "HOLD",
                "reason_codes": ["test_zero_intent"],
            }
        )
        blocked["plan"] = {**blocked["plan"], "state": "BLOCKED"}
        zero = commit_production_candidate_atomic(
            conn,
            compiled_plan=blocked,
            market_snapshot=market,
            awc_observation_contract=facts["awc_observation_contract"],
            truth_observation_revision_content_sha256=facts["truth_observation_revision_content_sha256"],
            bound=facts["bound"],
            # The binding normalizer emits six fractional digits while the
            # append-only ledger retains this three-digit source spelling.
            # They are the same instant and must exact-join semantically.
            created_at_utc="2026-07-23T04:30:01.123Z",
        )
        assert zero["candidate_will_order"] is False
        zero_fact_id = zero["delta_v2_training_fact"]["fact_id"]
        zero_vector = conn.execute(
            "SELECT vector_policy,order_intent_id,training_fact_id,vector_json "
            "FROM basket_target_execution_version_vectors WHERE training_fact_id=?",
            (zero_fact_id,),
        ).fetchone()
        assert zero_vector[0] == "NO_EXECUTION"
        assert zero_vector[1] is None
        assert zero_vector[2] == zero_fact_id
        assert '"policy":"NO_EXECUTION"' in zero_vector[3]
        assert zero["forward_observation_receipt"]["cycle_id"] == zero["receipt"]["cycle_id"]
        assert (
            zero["forward_observation_receipt"]["forward_evidence_binding"]["cash_phase"]
            == compiled["plan"]["lineage"]["cash_phase"]
        )
        decision_details = json.loads(
            conn.execute(
                "SELECT details_json FROM auto_trade_decisions WHERE cycle_id=?",
                (zero["receipt"]["cycle_id"],),
            ).fetchone()[0]
        )
        assert decision_details["current_core_buckets"] == (
            blocked["plan"]["current_core_buckets"]
        )
        assert decision_details["core_buckets"] == (
            blocked["plan"]["core_buckets"]
        )
        assert decision_details["core_buckets"]
        assert decision_details["direction"] == blocked["plan"]["direction"]
        assert decision_details["current_direction"] == (
            blocked["plan"]["current_direction"]
        )
        assert decision_details["core_transition"] == (
            blocked["plan"]["core_transition"]
        )
        assert decision_details["core_transition"][
            "direction_transition"
        ] in {"INITIALIZE", "MIGRATE", "NEUTRALIZE", "KEEP", "HOLD"}
        assert (
            decision_details["forward_evidence_binding"]["content_sha256"]
            == zero["forward_observation_receipt"]["forward_evidence_binding"]["content_sha256"]
        )
        assert conn.execute(
            "SELECT intent_count FROM basket_policy_training_fact WHERE fact_id=?",
            (zero_fact_id,),
        ).fetchone()[0] == 0
        worker = TargetProductionWorker.__new__(TargetProductionWorker)
        worker.commit_conn = conn
        worker.cash_phase_forward_store_root = tmp_path
        worker.cash_phase_forward_store_path = (
            tmp_path / "cash-phase-forward.sqlite3"
        )
        forward_flush = worker._flush_cash_phase_forward_observations()
        assert forward_flush["state"] == "committed"
        assert forward_flush["observation_count"] == 2
        assert forward_flush["calls_order_gateway"] is False
        forward_replay = worker._flush_cash_phase_forward_observations()
        assert forward_replay["state"] == "already_current"
        assert forward_replay["observation_count"] == 2

        cycle_id = first["receipt"]["cycle_id"]
        transition_target_cycle_state(
            conn, cycle_id=cycle_id, expected_state="prepared", new_state="reconcile_required", updated_at_utc=DECISION
        )
        closed = close_target_cycle(
            conn,
            cycle_id=cycle_id,
            outcome="released",
            closed_at_utc=DECISION,
            reason="e2e_rehearsal",
        )
        assert closed["state"] == "settled"
        assert conn.execute("SELECT reserved_usdc FROM basket_target_capital_pool").fetchone()[0] == 0.0
        # The production close helper is exercised separately in the atomic
        # ledger matrix; this direct assertion makes the rehearsal receipt
        # explicitly prove that no order/outbox tables are touched.
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('orders','outbox')").fetchone()[0] == 0

        tamper_cases = [
            (
                "auto_trade_decisions",
                "UPDATE auto_trade_decisions SET decision_sha256=? "
                "WHERE cycle_id=?",
                ("0" * 64, zero["receipt"]["cycle_id"]),
                "decision_content_address_mismatch",
            ),
            (
                "basket_probability_envelope",
                "UPDATE basket_probability_envelope SET station_id='DRIFT' "
                "WHERE envelope_id=(SELECT probability_envelope_id "
                "FROM basket_policy_training_fact WHERE fact_id=?)",
                (zero_fact_id,),
                "probability_binding_drift",
            ),
            (
                "basket_confirmed_inventory_snapshot",
                "UPDATE basket_confirmed_inventory_snapshot SET complete=0 "
                "WHERE inventory_snapshot_id=(SELECT inventory_snapshot_id "
                "FROM basket_policy_training_fact WHERE fact_id=?)",
                (zero_fact_id,),
                "inventory_ssot_drift",
            ),
            (
                "basket_policy_training_fact",
                "UPDATE basket_policy_training_fact SET feature_json='{}' "
                "WHERE fact_id=?",
                (zero_fact_id,),
                "policy_or_state_binding_drift|content_address_mismatch",
            ),
        ]
        for index, (table, statement, params, reason) in enumerate(tamper_cases):
            savepoint = f"forward_tamper_{index}"
            conn.execute(f"SAVEPOINT {savepoint}")
            trigger_names = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND tbl_name=?",
                    (table,),
                ).fetchall()
            ]
            for trigger_name in trigger_names:
                conn.execute(f'DROP TRIGGER "{trigger_name}"')
            conn.execute(statement, params)
            worker.cash_phase_forward_store_path = (
                tmp_path / f"tamper-{index}.sqlite3"
            )
            tamper_result = worker._flush_cash_phase_forward_observations()
            assert tamper_result["state"] == "blocked_fail_closed"
            assert tamper_result["promotion_allowed"] is False
            assert tamper_result["quarantined_count"] == 1
            assert (
                tamper_result["reason"]
                == "cash_phase_forward_poison_quarantined"
            )
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")


def test_missing_delta_v2_policy_blocks_before_m5_intents() -> None:
    envelopes, priors, artifacts, activations, market, cash_policy = _inputs()

    compiled = compile_production_plan(
        probability_envelopes=envelopes,
        market_snapshot=market,
        policy_activations=activations,
        global_priors=priors,
        policy_artifacts=artifacts,
        evaluation_at_utc=DECISION,
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        cash_phase_policy=cash_policy,
        cash_phase_input=_cash_input(cash_policy),
    )

    assert compiled["status"] == "blocked"
    assert "delta_v2_policy_missing" in compiled["reason_codes"]
    assert "delta_v2_fill_resolver_missing" in compiled["reason_codes"]
    assert compiled["delta_v2_audit"]["authorized"] is False
    assert compiled["live_submit_authorized"] is False
    assert compiled["boundary"]["calls_order_gateway"] is False


def test_sizing_failure_is_not_misreported_as_a_cadence_block() -> None:
    envelopes, priors, artifacts, activations, market, cash_policy = _inputs()
    compiled = compile_production_plan(
        probability_envelopes=envelopes,
        market_snapshot=market,
        policy_activations=activations,
        global_priors=priors,
        policy_artifacts=artifacts,
        evaluation_at_utc=DECISION,
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        base_policy={
            "clip_min_notional_usdc": 1.0,
            "clip_max_notional_usdc": 3.0,
        },
        delta_v2_policy=_delta_v2_policy(),
        delta_v2_fill_resolver=_fill_resolver,
        regime_artifact=build_regime_artifact(envelopes[-1]),
        cash_phase_policy=cash_policy,
        cash_phase_input=_cash_input(cash_policy),
        resource_probe_ok=True,
    )

    assert compiled["status"] == "hold"
    assert compiled["reason_codes"] == ["clip_below_minimum"]
    assert "cadence_gate_blocked" not in compiled["reason_codes"]
    assert any(
        row["candidate_present"]
        and not row["eligible"]
        and row["reason_code"] == "clip_below_minimum"
        for row in compiled["order_sizing_decisions"]
    )


def test_optimizer_hold_remains_a_compiled_no_live_decision() -> None:
    envelopes, priors, artifacts, activations, market, cash_policy = _inputs()
    raw_policy = _delta_v2_policy().to_dict()
    raw_policy["execution_weights"]["epsilon_score"] = 1_000_000.0
    raw_policy["content_sha256"] = content_sha256(
        {
            key: value
            for key, value in raw_policy.items()
            if key != "content_sha256"
        }
    )
    compiled = compile_production_plan(
        probability_envelopes=envelopes,
        market_snapshot=market,
        policy_activations=activations,
        global_priors=priors,
        policy_artifacts=artifacts,
        evaluation_at_utc=DECISION,
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        base_policy={"clip_max_notional_usdc": 3.0},
        delta_v2_policy=raw_policy,
        delta_v2_fill_resolver=_fill_resolver,
        regime_artifact=build_regime_artifact(envelopes[-1]),
        cash_phase_policy=cash_policy,
        cash_phase_input=_cash_input(cash_policy),
        resource_probe_ok=True,
    )

    assert compiled["status"] == "hold"
    assert compiled["live_submit_authorized"] is False
    assert compiled["boundary"]["calls_order_gateway"] is False


def _p0_opening_compile():
    """The P0 opening fixture: the ensemble-only compile and its market."""

    envelopes, priors, artifacts, activations, market, cash_policy = _inputs()
    p0_raw = envelopes[0].to_dict()
    # A pre-observation stage may not carry observed-high lineage: the shared
    # fixture writes the P1 shape, so the observation-borne keys are stripped
    # rather than copied into a stage that has no observation yet.
    observation_only = {
        "canonical_observation_event_id",
        "truth_observation_revision_content_sha256",
        "canonical_observation_committed_at",
        "hard_bound_id",
        "reported_high_c",
        "reported_hard_lower_bound",
        "reported_high_bucket",
        "reported_high_ordinal",
        "hard_excluded_buckets",
        "source_lane",
        "report_type",
    }
    p0_raw["lineage"] = {
        key: value
        for key, value in p0_raw["lineage"].items()
        if key not in observation_only
    }
    p0_raw["lineage"].update({
        "native_selection_evidence_sha256": "b" * 64,
        "native_cross_activation_sha256": "c" * 64,
        "native_cross_ensemble_artifact_sha256": "d" * 64,
        "bucket_intervals_c_json": json.dumps({
            "37": [36.5, 37.5],
            "38": [37.5, 38.5],
            "39": [38.5, None],
        }),
    })
    p0 = ProbabilityEnvelope.from_mapping(p0_raw)
    delta_raw = _delta_v2_policy().to_dict()
    delta_raw.pop("content_sha256", None)
    delta_raw["stage"] = "P0"
    delta_raw["content_sha256"] = content_sha256(delta_raw)
    delta_policy = DeltaV2PolicyArtifact.from_mapping(delta_raw)
    # The shared fixture activations are P1-scoped; the opening stage resolves
    # its own scope, so clone them onto P0.
    from bayes_basket_target import PolicyArtifact, StationBasketPolicyActivation
    p0_artifacts = {}
    for item in artifacts.values():
        clone = PolicyArtifact.create(
            artifact_id=item.artifact_id,
            family=item.family,
            station_id=item.station_id,
            stage="P0",
            season_bin=item.season_bin,
            maturity_bin=item.maturity_bin,
            liquidity_bin=item.liquidity_bin,
            strategy_version=item.strategy_version,
            theta_local=dict(item.theta_local),
            effective_sample_n=item.effective_sample_n,
            kappa=item.kappa,
            trained_through_utc=item.trained_through_utc,
            valid_from_utc=item.valid_from_utc,
            valid_until_utc=item.valid_until_utc,
        )
        p0_artifacts[clone.artifact_id] = clone
    p0_activations = [
        StationBasketPolicyActivation.create(
            activation_id=item.activation_id,
            station_id=item.station_id,
            stage="P0",
            season_bin=item.season_bin,
            maturity_bin=item.maturity_bin,
            liquidity_bin=item.liquidity_bin,
            strategy_version=item.strategy_version,
            global_prior_id=item.global_prior_id,
            target_artifact_id=item.target_artifact_id,
            reserve_artifact_id=item.reserve_artifact_id,
            cadence_artifact_id=item.cadence_artifact_id,
            fallback_families=list(item.fallback_families),
            generation=item.generation,
            valid_from_utc=item.valid_from_utc,
            valid_until_utc=item.valid_until_utc,
            activated_at_utc=item.activated_at_utc,
        )
        for item in activations
    ]
    compiled = compile_production_plan(
        probability_envelopes=[p0],
        market_snapshot=market,
        policy_activations=p0_activations,
        global_priors=priors,
        policy_artifacts=p0_artifacts,
        evaluation_at_utc=DECISION,
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        base_policy={"clip_max_notional_usdc": 3.0},
        delta_v2_policy=delta_policy,
        delta_v2_fill_resolver=_fill_resolver,
        regime_artifact=build_regime_artifact(p0),
        cash_phase_policy=cash_policy,
        cash_phase_input=_cash_input(cash_policy),
        resource_probe_ok=True,
    )
    compiled["production_inputs"] = {
        "strategy_version": "target-v1",
        "season_bin": "summer",
        "probability_maturity_bin": "d1",
        "liquidity_bin": "liquid",
        "cash_phase_policy_version": cash_policy.policy_version,
        "cash_phase_policy_sha256": cash_policy.content_sha256,
        "delta_v2_artifact_id": delta_policy.artifact_id,
        "delta_v2_artifact_sha256": delta_policy.content_sha256,
        "delta_v2_fill_distribution_artifact_sha256": (
            delta_policy.fill_distribution_artifact_sha256
        ),
        "delta_v2_resolution_blocker": None,
    }
    return compiled, market, cash_policy, p0


def _seed_p0_opening_db(conn, *, p0, market, cash_policy) -> None:
    """Seed the ledger, registry, envelope, book and pool the opening reads."""

    apply_production_ssot_schema(conn)
    install_canonical_v12(conn)
    apply_target_market_registry_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            "1" * 64, "reported_trajectory_market_registry_v1",
            "shared_live_registry_export_only", DECISION,
            "/read-only/e2e.db", 1, DECISION, "2" * 64,
        ),
    )
    conn.execute(
        """
        INSERT INTO basket_target_market_registry(
          snapshot_sha256,slug,event_date,outcome,city_slug,station_id,
          settlement_timezone,settlement_unit,source_lane,source_eligible,
          source_block_reason,resolution_url,rules_sha256,yes_token,
          no_token,market_active,market_accepting_orders,registry_json,
          refreshed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "1" * 64, "e2e-market", "2026-07-23", "38", "chengdu",
            "ZUUU", "Asia/Shanghai", "C", "awc", 1, "", "",
            "3" * 64, "yes-38", "no-38", 1, 1, "{}", DECISION,
        ),
    )
    p0_stored = p0.to_dict()
    conn.execute(
        """
        INSERT INTO basket_probability_envelope VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            p0.checkpoint_id, p0.stage, p0.checkpoint_id,
            p0.station_id, p0.event_date, p0.settlement_timezone,
            p0.decision_at_utc, p0.fresh_until_utc,
            json.dumps(p0_stored["bucket_ordinals"]),
            json.dumps(p0_stored["probabilities"]),
            p0.posterior_version, p0.posterior_sha256,
            p0.bridge_sha256, json.dumps(p0_stored["lineage"]),
            p0.content_sha256, "COMMITTED", DECISION,
        ),
    )
    conn.execute(
            """
            INSERT INTO basket_official_ws_snapshot VALUES(
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
        (
            market["snapshot_id"], market["market_id"],
            market["station_id"], market["event_date"],
                market["settlement_timezone"], market["sequence"],
                market["observed_at"], market["quote_version"],
                market["quote_observed_at"], market["quote_expires_at"],
                "7" * 64, "OFFICIAL_CLOB_WS",
                market["inventory_version"], market["order_state_version"],
            market["remaining_budget_usdc"],
            json.dumps(market["buckets"]),
            json.dumps(_cash_input(cash_policy).to_dict()),
            "4" * 64, "COMMITTED", DECISION,
        ),
    )
    conn.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "e2e-inventory-snapshot", "ZUUU", "2026-07-23",
            market["inventory_version"], market["observed_at"], 1,
            "{}", "5" * 64, "COMMITTED", DECISION,
        ),
    )
    conn.execute(
        "INSERT INTO basket_order_state_snapshot "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "e2e-order-state-snapshot", "ZUUU", "2026-07-23",
            market["order_state_version"], market["observed_at"], 1,
            "{}", "6" * 64, "COMMITTED", DECISION,
        ),
    )
    initialize_capital_pool(
        conn, balance_usdc=30.0, capital_epoch_id="e2e-capital-v1", updated_at_utc=DECISION
    )


def test_p0_opening_commits_stage_rooted_intents_and_evidence() -> None:
    """P0 opens a position on the ensemble alone: no METAR, no hard bound.

    The commit must root on the stage evidence, persist that evidence in the
    same transaction, keep every intent inside the model envelope, and stay
    no-live end to end.
    """

    compiled, market, cash_policy, p0 = _p0_opening_compile()
    assert compiled["status"] == "ready_no_live"
    assert compiled["plan"]["lineage"]["probability_stage"] == "P0"
    eligible_count = len([
        row for row in compiled["order_sizing_decisions"]
        if row["eligible"] and row["candidate_present"]
    ])
    assert eligible_count > 1

    with sqlite3.connect(":memory:") as conn:
        _seed_p0_opening_db(conn, p0=p0, market=market, cash_policy=cash_policy)

        first = commit_production_candidate_atomic(
            conn,
            compiled_plan=compiled,
            market_snapshot=market,
            awc_observation_contract=None,
            truth_observation_revision_content_sha256="",
            bound=None,
            created_at_utc=DECISION,
        )
        assert first["state"] == "committed"
        assert first["receipt"]["intent_count"] == eligible_count
        assert first["orders_submitted"] == 0
        assert first["places_orders"] is False
        assert first["calls_order_gateway"] is False
        assert first["live_submit_authorized"] is False

        # The evidence root and its soft bound were persisted atomically with
        # the cycle, and no METAR-shaped observation fact was fabricated.
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_stage_evidence_roots "
            "WHERE probability_stage='P0' AND position_role='OPEN'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_stage_bound_facts "
            "WHERE bound_kind='MODEL_ENVELOPE_SOFT'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_observation_facts"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents "
            "WHERE state='prepared' AND live_submit_authorized=0"
        ).fetchone()[0] == eligible_count

        # Replaying the identical candidate must not open a second position.
        replay = commit_production_candidate_atomic(
            conn,
            compiled_plan=compiled,
            market_snapshot=market,
            awc_observation_contract=None,
            truth_observation_revision_content_sha256="",
            bound=None,
            created_at_utc=DECISION,
        )
        assert replay["receipt"]["cycle_id"] == first["receipt"]["cycle_id"]
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_stage_evidence_roots"
        ).fetchone()[0] == 1


def test_p0_stage_open_restatement_on_a_later_clock_is_retained_without_rows() -> None:
    """Same decision, later clock: retained before the ledger, no new rows.

    A restatement mints a new cycle (the clock is in the cycle id) under the
    same decision scope key.  Until 2026-09-02 it reached the ledger, missed
    the decision-id retain (the id hashes the plan sha, which moves with the
    quote) and was refused as
    target_training_fact_decision_scope_already_committed -- which the
    stage-open caller wrote down as a blocker and answered with a preliminary
    blocked decision.  Measured that day: 23 of 27 committed forward scopes
    in one cycle carried the blocker.
    """

    import datetime as _dt

    compiled, market, cash_policy, p0 = _p0_opening_compile()
    with sqlite3.connect(":memory:") as conn:
        _seed_p0_opening_db(conn, p0=p0, market=market, cash_policy=cash_policy)
        first = commit_production_candidate_atomic(
            conn,
            compiled_plan=compiled,
            market_snapshot=market,
            awc_observation_contract=None,
            truth_observation_revision_content_sha256="",
            bound=None,
            created_at_utc=DECISION,
        )
        assert first["state"] == "committed"
        before = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "basket_target_execution_cycles",
                "auto_trade_decisions",
                "basket_policy_training_fact",
                "basket_target_execution_version_vectors",
            )
        )
        assert before[0] == 1 and before[1] == 1 and before[2] == 1
        later = (
            _dt.datetime.fromisoformat(DECISION.replace("Z", "+00:00"))
            + _dt.timedelta(seconds=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        restated = commit_production_candidate_atomic(
            conn,
            compiled_plan=compiled,
            market_snapshot=market,
            awc_observation_contract=None,
            truth_observation_revision_content_sha256="",
            bound=None,
            created_at_utc=later,
        )
        assert restated["state"] == "already_committed_decision_scope"
        assert restated["idempotent"] is True
        assert restated["writes_target_ledger"] is False
        assert restated["orders_submitted"] == 0
        assert restated["calls_order_gateway"] is False
        assert restated["receipt"]["cycle_id"] == first["receipt"]["cycle_id"]
        held = conn.execute(
            "SELECT fact_id FROM basket_policy_training_fact"
        ).fetchone()[0]
        assert restated["delta_v2_training_fact"]["fact_id"] == held
        after = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "basket_target_execution_cycles",
                "auto_trade_decisions",
                "basket_policy_training_fact",
                "basket_target_execution_version_vectors",
            )
        )
        assert after == before
