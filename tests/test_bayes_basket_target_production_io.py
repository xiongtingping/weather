from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from bayes_basket_target import ContractError, content_sha256
from bayes_basket_target.production_io import (
    compile_production_cycle_from_ssot,
    load_materialized_probability_envelopes,
    load_production_market_snapshot,
)
from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
)
from bayes_basket_target.stages import select_current_stage
from bayes_basket_target.probability_bridge import materialize_probability_bridge
from bayes_basket_target.regime_materialization import (
    build_regime_artifact,
    insert_regime_artifact,
)
from bayes_basket_target.cash_phase_authority import CashPhaseAuthorityManifest
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.delta_v2_artifact import (
    ACTIVATION_SCHEMA,
    ARTIFACT_SCHEMA,
    PROMOTION_SCHEMA,
    commit_delta_v2_authority,
)
from bayes_basket_target.delta_v2_fill_artifact import (
    FILL_DISTRIBUTION_ARTIFACT_SCHEMA,
    FILL_DISTRIBUTION_PROMOTION_SCHEMA,
    commit_delta_v2_fill_distribution_artifact,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_bayes_basket_target import _cash_phase_inputs, _envelope, _policy_inputs


def _record_sha(schema: str, values: dict[str, object]) -> str:
    return content_sha256({"schema": schema, **{key: value for key, value in values.items() if key != "content_sha256"}})


def _insert_probability_row(
    conn: sqlite3.Connection,
    envelope: ProbabilityEnvelope,
    *,
    created_at: str,
) -> None:
    values = envelope.to_dict()
    conn.execute(
        """INSERT INTO basket_probability_envelope(
            envelope_id,stage,checkpoint_id,station_id,event_date,
            settlement_timezone,decision_at_utc,fresh_until_utc,
            bucket_ordinals_json,probabilities_json,posterior_version,
            posterior_sha256,bridge_sha256,lineage_json,content_sha256,
            state,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            envelope.checkpoint_id,
            envelope.stage,
            envelope.checkpoint_id,
            envelope.station_id,
            envelope.event_date,
            envelope.settlement_timezone,
            envelope.decision_at_utc,
            envelope.fresh_until_utc,
            json.dumps(values["bucket_ordinals"], sort_keys=True),
            json.dumps(values["probabilities"], sort_keys=True),
            envelope.posterior_version,
            envelope.posterior_sha256,
            envelope.bridge_sha256,
            json.dumps(values["lineage"], sort_keys=True),
            envelope.content_sha256,
            "COMMITTED",
            created_at,
        ),
    )


def _stage_envelope(
    root: ProbabilityEnvelope,
    *,
    stage: str,
    checkpoint_id: str,
    decision_at: str,
    fresh_until: str,
) -> ProbabilityEnvelope:
    return ProbabilityEnvelope.from_mapping(
        {
            **root.to_dict(),
            "stage": stage,
            "checkpoint_id": checkpoint_id,
            "decision_at_utc": decision_at,
            "fresh_until_utc": fresh_until,
            "lineage": {
                **dict(root.lineage),
                "p0_root_checkpoint_id": root.checkpoint_id,
                "p0_root_content_sha256": root.content_sha256,
            },
        }
    )


def _seed_probability_only(conn: sqlite3.Connection) -> ProbabilityEnvelope:
    conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(conn)
    root = _envelope()
    _insert_probability_row(
        conn,
        root,
        created_at="2026-07-23T04:00:00Z",
    )
    return root


def _seed_native_probability_only(
    conn: sqlite3.Connection,
) -> ProbabilityEnvelope:
    conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(conn)
    base = _envelope()
    root = ProbabilityEnvelope.from_mapping(
        {
            **base.to_dict(),
            "lineage": {
                **dict(base.lineage),
                "probability_pathway": P0_PROBABILITY_PATHWAY,
                "p0_probability_pathway": P0_PROBABILITY_PATHWAY,
            },
        }
    )
    _insert_probability_row(
        conn,
        root,
        created_at="2026-07-23T04:00:00Z",
    )
    return root


def _seed_delta_v2(
    conn: sqlite3.Connection,
    *,
    stage: str = "P0",
    price_by_bucket: dict[str, float] | None = None,
) -> None:
    fill_family_sha = "1" * 64
    for index, bucket in enumerate(("37", "38", "39"), start=1):
        fill = {
            "schema": FILL_DISTRIBUTION_ARTIFACT_SCHEMA,
            "artifact_id": f"fill-zuuu-{bucket}-v2",
            "station_id": "ZUUU",
            "stage": stage,
            "season_bin": "summer",
            "maturity_bin": "d1",
            "liquidity_bin": "liquid",
            "bucket": bucket,
            "price": float((price_by_bucket or {}).get(bucket, 0.21)),
            "timeout_seconds": 30.0,
            "queue_ahead_min": 0.0,
            "queue_ahead_max": 1_000_000_000.0,
            "spread_min": 0.0,
            "spread_max": 1.0,
            "depth_min": 0.0,
            "depth_max": 1_000_000_000.0,
            "quote_version_scope": "*",
            "maker_distribution": {
                "fill_probability_zero": 0.1,
                "fill_probability_partial": 0.2,
                "fill_probability_full": 0.7,
                "late_fill_after_cancel_request_probability": 0.01,
                "partial_fill_fraction": 0.5,
            },
            "fok_distribution": {
                "fill_probability_zero": 0.0,
                "fill_probability_partial": 0.0,
                "fill_probability_full": 1.0,
                "late_fill_after_cancel_request_probability": 0.0,
                "partial_fill_fraction": 0.0,
            },
            "maker_execution_costs": {
                "wait_cost": 0.0,
                "queue_risk": 0.0,
                "adverse_selection": 0.0,
                "reprice_risk": 0.0,
                "capital_lock_cost": 0.0,
            },
            "fok_execution_costs": {
                "wait_cost": 0.0,
                "queue_risk": 0.0,
                "adverse_selection": 0.0,
                "reprice_risk": 0.0,
                "capital_lock_cost": 0.0,
            },
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
                "resource_failures": 0.0,
            },
            "oos_gate_version": "delta-v2-fill-oos-v1",
            "oos_gate_passed": True,
            "trained_through_utc": "2026-07-20T00:00:00Z",
            "valid_from_utc": "2026-07-21T00:00:00Z",
            "valid_until_utc": "2026-08-01T00:00:00Z",
            "authority_scope": "PRODUCTION_NO_LIVE",
            "fill_distribution_artifact_sha256": fill_family_sha,
        }
        fill["content_sha256"] = content_sha256(fill)
        fill_promotion = {
            "schema": FILL_DISTRIBUTION_PROMOTION_SCHEMA,
            "promotion_id": f"promote-fill-zuuu-{bucket}-v2",
            "artifact_id": fill["artifact_id"],
            "decision_seq": index,
            "decision": "PROMOTED",
            "gate_version": fill["oos_gate_version"],
            "decided_at_utc": "2026-07-20T12:00:00Z",
            "receipt_sha256": f"{index}" * 64,
        }
        fill_promotion["content_sha256"] = content_sha256(fill_promotion)
        commit_delta_v2_fill_distribution_artifact(
            conn,
            artifact=fill,
            promotion=fill_promotion,
        )

    artifact = {
        "schema": ARTIFACT_SCHEMA,
        "artifact_id": "delta-v2-oos-io-test-v1",
        "station_id": "ZUUU",
        "stage": stage,
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
        "price_grid": [0.21],
        "timeout_grid_seconds": [30.0],
        "fill_distribution_artifact_sha256": fill_family_sha,
        "training_evidence_sha256": "d" * 64,
        "train_start_date": "2026-05-01",
        "train_end_date": "2026-06-30",
        "holdout_start_date": "2026-07-01",
        "holdout_end_date": "2026-07-20",
        "sample_n": 60,
        "holdout_n": 20,
        "consecutive_forward_windows": 2,
        "compared_baselines": ["EV_ONLY", "HOLD", "ZERO_WEIGHT", "v1"],
        "oos_metrics": {
            "delta_score_lift_vs_v1": 0.1,
            "profit_floor_lift": 0.0,
            "resource_failures": 0.0,
        },
        "oos_gate_version": "delta-v2-oos-gate-v1",
        "oos_gate_passed": True,
        "trained_through_utc": "2026-07-20T23:59:59Z",
        "valid_from_utc": "2026-07-21T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE",
    }
    artifact["content_sha256"] = content_sha256(artifact)
    promotion = {
        "schema": PROMOTION_SCHEMA,
        "promotion_id": "delta-v2-promotion-io-v1",
        "artifact_id": artifact["artifact_id"],
        "decision_seq": 1,
        "decision": "PROMOTED",
        "gate_version": artifact["oos_gate_version"],
        "decided_at_utc": "2026-07-20T23:59:59Z",
        "receipt_sha256": "e" * 64,
    }
    promotion["content_sha256"] = content_sha256(promotion)
    activation = {
        "schema": ACTIVATION_SCHEMA,
        "activation_id": "delta-v2-activation-io-v1",
        "artifact_id": artifact["artifact_id"],
        "station_id": artifact["station_id"],
        "stage": artifact["stage"],
        "season_bin": artifact["season_bin"],
        "maturity_bin": artifact["maturity_bin"],
        "liquidity_bin": artifact["liquidity_bin"],
        "strategy_version": artifact["strategy_version"],
        "generation": 1,
        "valid_from_utc": artifact["valid_from_utc"],
        "valid_until_utc": artifact["valid_until_utc"],
        "activated_at_utc": artifact["valid_from_utc"],
        "authority_scope": artifact["authority_scope"],
    }
    activation["content_sha256"] = content_sha256(activation)
    commit_delta_v2_authority(
        conn,
        artifact=artifact,
        promotion=promotion,
        activation=activation,
    )


def _seed(
    conn: sqlite3.Connection,
    *,
    promote_activation: bool = True,
    cash_promoted: bool = True,
    include_regime: bool = True,
) -> None:
    conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(conn)
    prior, policies, activations = _policy_inputs()
    conn.execute(
        "INSERT INTO basket_global_prior_artifact VALUES(?,?,?,?,?,?,?)",
        (
            prior.global_prior_id, prior.strategy_version, json.dumps(prior.family_parameters),
            json.dumps(prior.default_policy), prior.valid_from_utc, prior.valid_until_utc,
            prior.content_sha256,
        ),
    )
    conn.execute(
        "INSERT INTO basket_policy_promotion VALUES(?,?,?,?,?,?,?,?)",
        ("promote-prior", "GLOBAL_PRIOR", prior.global_prior_id, 1, "PROMOTED", "gate-v1", "2026-07-22T00:00:00Z", "a" * 64),
    )
    for policy in policies:
        conn.execute(
            "INSERT INTO basket_station_policy_artifact VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                policy.artifact_id, policy.family, policy.station_id, policy.stage,
                policy.season_bin, policy.maturity_bin, policy.liquidity_bin,
                policy.strategy_version, json.dumps(policy.theta_local),
                policy.effective_sample_n, policy.kappa, policy.trained_through_utc,
                policy.valid_from_utc, policy.valid_until_utc, policy.content_sha256,
            ),
        )
        conn.execute(
            "INSERT INTO basket_policy_promotion VALUES(?,?,?,?,?,?,?,?)",
            (f"promote-{policy.family}", "STATION_POLICY", policy.artifact_id, 1, "PROMOTED", "gate-v1", "2026-07-22T00:00:00Z", "b" * 64),
        )
    activation = activations[0]
    conn.execute(
        "INSERT INTO basket_policy_activation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            activation.activation_id, activation.station_id, activation.stage,
            activation.season_bin, activation.maturity_bin, activation.liquidity_bin,
            activation.strategy_version, activation.global_prior_id,
            activation.target_artifact_id, activation.reserve_artifact_id,
            activation.cadence_artifact_id, json.dumps(list(activation.fallback_families)),
            activation.generation, activation.valid_from_utc, activation.valid_until_utc,
            activation.activated_at_utc, activation.content_sha256,
        ),
    )
    if promote_activation:
        conn.execute(
            "INSERT INTO basket_policy_promotion VALUES(?,?,?,?,?,?,?,?)",
            ("promote-activation", "STATION_ACTIVATION", activation.activation_id, 1, "PROMOTED", "gate-v1", "2026-07-22T00:00:00Z", "c" * 64),
        )
    cash, phase_input = _cash_phase_inputs()
    cash_values = cash.to_dict()
    cash_values["station_timezone"] = "America/New_York"
    cash_values["evidence_status"] = (
        "OFFLINE_VALIDATED" if cash_promoted else "DESIGN_CANDIDATE"
    )
    cash_values["promoted"] = cash_promoted
    cash_values["content_sha256"] = content_sha256({
        "schema": "basket_cash_phase_policy_artifact_v1",
        **{key: value for key, value in cash_values.items() if key != "content_sha256"},
    })
    conn.execute(
        "INSERT INTO basket_cash_phase_policy_artifact VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            cash_values["policy_version"], cash_values["station_id"], cash_values["station_timezone"],
            cash_values["heavy_to_standard_local_time"], cash_values["standard_to_tail_local_time"],
            json.dumps(cash_values["cash_by_phase"]), cash_values["valid_from_utc"],
            cash_values["valid_until_utc"], cash_values["evidence_status"],
            int(cash_values["promoted"]), cash_values["content_sha256"],
        ),
    )
    envelope = _envelope()
    e = envelope.to_dict()
    e["lineage"] = {
        **e["lineage"],
        "model_envelope_lower_bucket": "37",
        "model_envelope_upper_bucket": "39",
        "model_envelope_span_c": "2",
        "bucket_intervals_c_json": json.dumps(
            {
                "37": [None, 37.5],
                "38": [37.5, 38.5],
                "39": [38.5, None],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    envelope = ProbabilityEnvelope.from_mapping(e)
    materialize_probability_bridge(
        conn,
        envelope,
        created_at_utc="2026-07-23T05:00:00Z",
    )
    if include_regime:
        insert_regime_artifact(
            conn,
            build_regime_artifact(envelope),
            created_at_utc="2026-07-23T05:00:00Z",
        )
    buckets = [
        {
            "bucket": bucket,
            "ordinal": ordinal,
            "probability": envelope.probabilities[bucket],
            "bid_price": 0.20,
            "bid_levels": [{"price": 0.20, "shares": 100.0}],
            "ask_levels": [{"price": 0.30, "shares": 100.0}],
            "maker_queue_ahead_shares": 0.0,
            "maker_queue_known": True,
            "filled_shares": 0.0,
            "filled_cost_usdc": 0.0,
            "pending_shares": 0.0,
            "pending_notional_usdc": 0.0,
            "active_before": False,
        }
        for bucket, ordinal in envelope.bucket_ordinals.items()
    ]
    cash_input = phase_input.to_dict()
    cash_input["station_timezone"] = "America/New_York"
    quote = {
        "snapshot_id": "quote-1", "market_id": "market-1", "station_id": "ZUUU",
        "event_date": "2026-07-23", "settlement_timezone": "America/New_York",
        "sequence": 1,
        "observed_at": "2026-07-23T05:00:00Z", "quote_version": "q-1",
        "quote_observed_at": "2026-07-23T05:00:00Z",
        "quote_expires_at": "2026-07-23T05:00:05Z",
        "quote_freshness_policy_sha256": "f" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inv-1", "order_state_version": "ord-1",
        "remaining_budget_usdc": 12.0, "buckets_json": buckets,
        "cash_phase_input_json": cash_input, "state": "COMMITTED",
        "created_at": "2026-07-23T05:00:00Z",
    }
    quote["content_sha256"] = _record_sha("basket_official_ws_snapshot_v1", quote)
    conn.execute(
        """INSERT INTO basket_official_ws_snapshot(
            snapshot_id,market_id,station_id,event_date,settlement_timezone,
            sequence,observed_at,quote_version,quote_observed_at,
            quote_expires_at,quote_freshness_policy_sha256,quote_source,
            inventory_version,order_state_version,remaining_budget_usdc,
            buckets_json,cash_phase_input_json,content_sha256,state,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(quote[key] if key not in {"buckets_json", "cash_phase_input_json"} else json.dumps(quote[key]) for key in (
            "snapshot_id", "market_id", "station_id", "event_date", "settlement_timezone",
            "sequence", "observed_at",
            "quote_version", "quote_observed_at", "quote_expires_at",
            "quote_freshness_policy_sha256", "quote_source",
            "inventory_version", "order_state_version",
            "remaining_budget_usdc", "buckets_json", "cash_phase_input_json", "content_sha256", "state", "created_at",
        )),
    )
    inventory = {
        "inventory_snapshot_id": "inv-snapshot-1", "station_id": "ZUUU", "event_date": "2026-07-23",
        "inventory_version": "inv-1", "observed_at": "2026-07-23T05:00:00Z", "complete": 1,
        "inventory_json": {"filled_by_bucket": {}}, "state": "COMMITTED", "created_at": "2026-07-23T05:00:00Z",
    }
    inventory["content_sha256"] = _record_sha("basket_confirmed_inventory_snapshot_v1", inventory)
    conn.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
        (inventory["inventory_snapshot_id"], inventory["station_id"], inventory["event_date"], inventory["inventory_version"], inventory["observed_at"], inventory["complete"], json.dumps(inventory["inventory_json"]), inventory["content_sha256"], inventory["state"], inventory["created_at"]),
    )
    order_state = {
        "order_state_snapshot_id": "ord-snapshot-1", "station_id": "ZUUU", "event_date": "2026-07-23",
        "order_state_version": "ord-1", "observed_at": "2026-07-23T05:00:00Z", "known": 1,
        "order_state_json": {"pending_by_bucket": {}}, "state": "COMMITTED", "created_at": "2026-07-23T05:00:00Z",
    }
    order_state["content_sha256"] = _record_sha("basket_order_state_snapshot_v1", order_state)
    conn.execute(
        "INSERT INTO basket_order_state_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
        (order_state["order_state_snapshot_id"], order_state["station_id"], order_state["event_date"], order_state["order_state_version"], order_state["observed_at"], order_state["known"], json.dumps(order_state["order_state_json"]), order_state["content_sha256"], order_state["state"], order_state["created_at"]),
    )
    conn.commit()


def _cash_authority(
    conn: sqlite3.Connection,
    *,
    authority_scope: str,
) -> CashPhaseAuthorityManifest:
    row = conn.execute(
        "SELECT policy_version,station_id,station_timezone,valid_from_utc,"
        "valid_until_utc,content_sha256 "
        "FROM basket_cash_phase_policy_artifact"
    ).fetchone()
    assert row is not None
    return CashPhaseAuthorityManifest.create(
        policy_version=row[0],
        station_id=row[1],
        station_timezone=row[2],
        valid_from_utc=row[3],
        valid_until_utc=row[4],
        artifact_sha256=row[5],
        authority_scope=authority_scope,
    )


def test_production_io_compiles_read_only_candidate_and_rejects_hash_drift() -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn, cash_promoted=False)
        _seed_delta_v2(conn)
        snapshot = load_production_market_snapshot(
            conn, station_id="ZUUU", event_date="2026-07-23", evaluation_at_utc="2026-07-23T05:00:04Z"
        )
        assert snapshot["quote_source"] == "OFFICIAL_CLOB_WS"
        assert (
            snapshot["target_execution_hard_deadline_at"]
            == "2026-07-23T05:00:05.000Z"
        )
        plan = compile_production_cycle_from_ssot(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
            season_bin="summer",
            maturity_bin="static-config-must-not-override-probability",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            policy_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
            base_policy={"clip_max_notional_usdc": 3.0},
            cash_phase_authority_manifest=_cash_authority(
                conn,
                authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
            ),
        )
        assert plan["execution_mode"] == "production_no_live"
        assert plan["boundary"]["calls_order_gateway"] is False
        assert plan["boundary"]["orders_submitted"] == 0
        assert plan["delta_v2_audit"]["authorized"] is True
        assert plan["production_inputs"]["probability_maturity_bin"] == "d1"
        assert (
            plan["production_inputs"]["configured_maturity_bin"]
            == "static-config-must-not-override-probability"
        )


def test_production_io_rejects_quote_at_exact_expiry_boundary() -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn)
        with pytest.raises(ContractError, match="production_quote_expired"):
            load_production_market_snapshot(
                conn,
                station_id="ZUUU",
                event_date="2026-07-23",
                evaluation_at_utc="2026-07-23T05:00:05Z",
            )


def test_market_snapshot_restores_exact_prior_core_from_decision_receipt() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        _seed(conn, cash_promoted=False)
        conn.execute(
            "INSERT INTO auto_trade_decisions("
            "decision_id,cycle_id,plan_id,station_id,event_date,"
            "settlement_timezone,strategy_lane,decided_state,will_order,"
            "target_execution_hard_deadline_at,reason_codes_json,"
            "details_json,decision_sha256,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "decision-core-1",
                "cycle-core-1",
                "plan-core-1",
                "ZUUU",
                "2026-07-23",
                "America/New_York",
                "basket",
                "blocked",
                0,
                None,
                json.dumps(["no_live"]),
                json.dumps({
                    "schema": "bayes_basket_production_decision_v1",
                    "current_core_buckets": ["37"],
                    "core_buckets": ["38", "39"],
                    "direction": "UP",
                }),
                "d" * 64,
                "2026-07-23T05:00:01Z",
            ),
        )

        snapshot = load_production_market_snapshot(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
        )

        assert snapshot["current_core_buckets"] == ["38", "39"]
        assert snapshot["current_direction"] == "UP"


def test_production_io_fresh_stale_fresh_recovery_uses_new_snapshot() -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn)
        first = load_production_market_snapshot(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
        )
        assert first["snapshot_id"] == "quote-1"
        with pytest.raises(ContractError, match="production_quote_expired"):
            load_production_market_snapshot(
                conn,
                station_id="ZUUU",
                event_date="2026-07-23",
                evaluation_at_utc="2026-07-23T05:00:05Z",
            )

        row = conn.execute(
            "SELECT * FROM basket_official_ws_snapshot "
            "WHERE snapshot_id='quote-1'"
        ).fetchone()
        assert row is not None
        refreshed = {key: row[key] for key in row.keys()}
        refreshed.update(
            {
                "snapshot_id": "quote-2",
                "sequence": 2,
                "observed_at": "2026-07-23T05:00:06Z",
                "quote_version": "q-2",
                "quote_observed_at": "2026-07-23T05:00:06Z",
                "quote_expires_at": "2026-07-23T05:00:11Z",
                "created_at": "2026-07-23T05:00:06Z",
            }
        )
        refreshed["buckets_json"] = json.loads(refreshed["buckets_json"])
        refreshed["cash_phase_input_json"] = json.loads(
            refreshed["cash_phase_input_json"]
        )
        refreshed["content_sha256"] = _record_sha(
            "basket_official_ws_snapshot_v1",
            refreshed,
        )
        columns = tuple(refreshed)
        conn.execute(
            "INSERT INTO basket_official_ws_snapshot("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(
                json.dumps(refreshed[name])
                if name in {"buckets_json", "cash_phase_input_json"}
                else refreshed[name]
                for name in columns
            ),
        )
        recovered = load_production_market_snapshot(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:07Z",
        )
        assert recovered["snapshot_id"] == "quote-2"
        assert recovered["quote_version"] == "q-2"


def test_production_io_requires_exact_regime_ssot() -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn, include_regime=False)
        with pytest.raises(ContractError, match="regime_ssot_not_unique"):
            compile_production_cycle_from_ssot(
                conn,
                station_id="ZUUU",
                event_date="2026-07-23",
                evaluation_at_utc="2026-07-23T05:00:04Z",
                season_bin="summer",
                maturity_bin="d1",
                liquidity_bin="liquid",
                strategy_version="target-v1",
                policy_authority_scope="FORMAL_PROMOTED",
                cash_phase_authority_manifest=_cash_authority(
                    conn,
                    authority_scope="FORMAL_PROMOTED",
                ),
            )


def test_production_io_accepts_ask_only_for_buy_and_disables_maker() -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn)
        row = conn.execute(
            "SELECT buckets_json,cash_phase_input_json FROM basket_official_ws_snapshot"
        ).fetchone()
        buckets = json.loads(row[0])
        for bucket in buckets:
            bucket["bid_levels"] = []
            bucket["bid_price"] = 0.0
            bucket["bid_available"] = False
            bucket["maker_queue_known"] = True
        quote = conn.execute(
            "SELECT * FROM basket_official_ws_snapshot"
        ).fetchone()
        columns = [
            item[1]
            for item in conn.execute(
                "PRAGMA table_info(basket_official_ws_snapshot)"
            ).fetchall()
        ]
        values = dict(zip(columns, quote))
        values["buckets_json"] = buckets
        values["cash_phase_input_json"] = json.loads(row[1])
        content_sha = _record_sha("basket_official_ws_snapshot_v1", values)
        conn.execute("DROP TRIGGER basket_official_ws_snapshot_no_update")
        conn.execute(
            "UPDATE basket_official_ws_snapshot SET buckets_json=?,content_sha256=?",
            (json.dumps(buckets), content_sha),
        )

        snapshot = load_production_market_snapshot(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
        )
        assert all(item["bid_available"] is False for item in snapshot["buckets"])
        assert all(item["bid_price"] == 0.0 for item in snapshot["buckets"])
        assert all(item["maker_queue_known"] is False for item in snapshot["buckets"])


def test_production_io_does_not_infer_activation_promotion() -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn, promote_activation=False)
        with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
            compile_production_cycle_from_ssot(
                conn,
                station_id="ZUUU",
                event_date="2026-07-23",
                evaluation_at_utc="2026-07-23T05:00:04Z",
                season_bin="summer",
                maturity_bin="d1",
                liquidity_bin="liquid",
                strategy_version="target-v1",
                policy_authority_scope="FORMAL_PROMOTED",
                cash_phase_authority_manifest=_cash_authority(
                    conn,
                    authority_scope="FORMAL_PROMOTED",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("UPDATE basket_order_state_snapshot SET content_sha256=?", ("c" * 64,))


def test_production_io_has_no_inline_v1_optimizer_arguments() -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(
            conn,
            promote_activation=False,
            cash_promoted=False,
        )
        with pytest.raises(TypeError, match="unexpected keyword"):
            compile_production_cycle_from_ssot(
                conn,
                station_id="ZUUU",
                event_date="2026-07-23",
                evaluation_at_utc="2026-07-23T05:00:04Z",
                season_bin="summer",
                maturity_bin="d1",
                liquidity_bin="liquid",
                strategy_version="target-v1",
                policy_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
                cash_phase_authority_manifest=_cash_authority(
                    conn,
                    authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
                ),
                strict_delta_policy={"policy_version": "forbidden-v1"},
            )


def test_production_io_resolves_delta_v2_policy_and_fill_from_ssot() -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn, cash_promoted=False)
        _seed_delta_v2(conn)
        plan = compile_production_cycle_from_ssot(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            policy_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
            base_policy={"clip_max_notional_usdc": 3.0},
            cash_phase_authority_manifest=_cash_authority(
                conn,
                authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
            ),
        )
        production_inputs = plan["production_inputs"]
        assert production_inputs["delta_v2_artifact_id"] == (
            "delta-v2-oos-io-test-v1"
        )
        assert production_inputs["delta_v2_resolution_blocker"] is None
        assert plan["delta_v2_audit"]["authorized"] is True


def test_production_io_missing_delta_v2_authority_fails_closed() -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn, cash_promoted=False)
        plan = compile_production_cycle_from_ssot(
                conn,
                station_id="ZUUU",
                event_date="2026-07-23",
                evaluation_at_utc="2026-07-23T05:00:04Z",
                season_bin="summer",
                maturity_bin="d1",
                liquidity_bin="liquid",
                strategy_version="target-v1",
                policy_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
                cash_phase_authority_manifest=_cash_authority(
                    conn,
                    authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
                ),
            )
        assert plan["status"] == "blocked"
        assert plan["production_inputs"]["delta_v2_artifact_id"] is None
        assert plan["production_inputs"]["delta_v2_resolution_blocker"]


def test_production_io_forbids_inline_delta_v2_policy_fallback() -> None:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn)
        with pytest.raises(TypeError, match="unexpected keyword"):
            compile_production_cycle_from_ssot(
                conn,
                station_id="ZUUU",
                event_date="2026-07-23",
                evaluation_at_utc="2026-07-23T05:00:04Z",
                season_bin="summer",
                maturity_bin="d1",
                liquidity_bin="liquid",
                strategy_version="target-v1",
                policy_authority_scope="FORMAL_PROMOTED",
                delta_v2_policy={"artifact_id": "inline-forbidden"},
            )


@pytest.mark.parametrize("stage", ["P0-M", "P1"])
def test_probability_loader_does_not_hide_corrupt_high_stage_behind_p0_fallback(
    stage: str,
) -> None:
    with sqlite3.connect(":memory:") as conn:
        root = _seed_native_probability_only(conn)
        conn.execute(
            """INSERT INTO basket_probability_envelope(
                envelope_id,stage,checkpoint_id,station_id,event_date,
                settlement_timezone,decision_at_utc,fresh_until_utc,
                bucket_ordinals_json,probabilities_json,posterior_version,
                posterior_sha256,bridge_sha256,lineage_json,content_sha256,
                state,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"{stage}-corrupt",
                stage,
                f"{stage}-corrupt",
                root.station_id,
                root.event_date,
                root.settlement_timezone,
                "not-an-iso-timestamp",
                "2026-07-23T08:00:00Z",
                json.dumps(dict(root.bucket_ordinals)),
                "{corrupt-json",
                f"{stage}-corrupt-test",
                "1" * 64,
                "2" * 64,
                json.dumps({"settlement_timezone": root.settlement_timezone}),
                "3" * 64,
                "COMMITTED",
                "2026-07-23T05:00:02Z",
            ),
        )

        with pytest.raises(
            ContractError,
            match="probabilities_json_invalid",
        ):
            load_materialized_probability_envelopes(
                conn,
                station_id="ZUUU",
                event_date="2026-07-23",
                evaluation_at_utc="2026-07-23T05:00:04Z",
            )


@pytest.mark.parametrize("stage", ["P0-M", "P1"])
def test_highest_existing_stage_expiry_fails_closed_without_crossing(
    stage: str,
) -> None:
    with sqlite3.connect(":memory:") as conn:
        root = _seed_native_probability_only(conn)
        higher = _stage_envelope(
            root,
            stage=stage,
            checkpoint_id=f"{stage}-stale",
            decision_at="2026-07-23T05:00:01Z",
            fresh_until="2026-07-23T05:00:03Z",
        )
        _insert_probability_row(
            conn,
            higher,
            created_at="2026-07-23T05:00:01Z",
        )

        envelopes = load_materialized_probability_envelopes(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
        )
        with pytest.raises(
            ContractError,
            match="latest_stage_stale_fail_closed",
        ):
            select_current_stage(
                envelopes,
                at_utc="2026-07-23T05:00:04Z",
            )


def test_stage_routing_only_falls_back_when_higher_stage_is_not_yet_asof() -> None:
    with sqlite3.connect(":memory:") as conn:
        root = _seed_native_probability_only(conn)
        future_p0m = _stage_envelope(
            root,
            stage="P0-M",
            checkpoint_id="p0m-future",
            decision_at="2026-07-23T05:00:06Z",
            fresh_until="2026-07-23T06:00:00Z",
        )
        _insert_probability_row(
            conn,
            future_p0m,
            created_at="2026-07-23T05:00:01Z",
        )

        before = load_materialized_probability_envelopes(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
        )
        assert select_current_stage(
            before,
            at_utc="2026-07-23T05:00:04Z",
        ).stage == "P0"

        after = load_materialized_probability_envelopes(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:07Z",
        )
        assert select_current_stage(
            after,
            at_utc="2026-07-23T05:00:07Z",
        ).checkpoint_id == "p0m-future"


def test_low_high_low_arrival_never_rewinds_selected_p1() -> None:
    with sqlite3.connect(":memory:") as conn:
        root = _seed_native_probability_only(conn)
        p1 = _stage_envelope(
            root,
            stage="P1",
            checkpoint_id="p1-authority",
            decision_at="2026-07-23T05:00:01Z",
            fresh_until="2026-07-23T06:00:00Z",
        )
        p0_late = _stage_envelope(
            root,
            stage="P0",
            checkpoint_id="p0-late-refresh",
            decision_at="2026-07-23T05:00:02Z",
            fresh_until="2026-07-23T06:00:00Z",
        )
        _insert_probability_row(conn, p1, created_at="2026-07-23T05:00:01Z")
        _insert_probability_row(
            conn,
            p0_late,
            created_at="2026-07-23T05:00:03Z",
        )

        envelopes = load_materialized_probability_envelopes(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
        )
        selected = select_current_stage(
            list(reversed(envelopes)),
            at_utc="2026-07-23T05:00:04Z",
        )
        assert selected.checkpoint_id == "p1-authority"


def test_native_root_keeps_legacy_p1_without_copied_pathway_visible() -> None:
    with sqlite3.connect(":memory:") as conn:
        root = _seed_native_probability_only(conn)
        p1 = _stage_envelope(
            root,
            stage="P1",
            checkpoint_id="p1-before-pathway-inheritance",
            decision_at="2026-07-23T05:00:01Z",
            fresh_until="2026-07-23T06:00:00Z",
        )
        p1 = ProbabilityEnvelope.from_mapping(
            {
                **p1.to_dict(),
                "lineage": {
                    key: value
                    for key, value in p1.lineage.items()
                    if key not in {
                        "probability_pathway",
                        "p0_probability_pathway",
                    }
                },
            }
        )
        _insert_probability_row(
            conn,
            p1,
            created_at="2026-07-23T05:00:01Z",
        )

        envelopes = load_materialized_probability_envelopes(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:02Z",
        )
        assert select_current_stage(
            envelopes,
            at_utc="2026-07-23T05:00:02Z",
        ).checkpoint_id == "p1-before-pathway-inheritance"
