from __future__ import annotations

import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from bayes_basket_target import (
    PRODUCTION_EXECUTION_MODE,
    ContractError,
    content_sha256,
    delta_v2_runtime,
)
from bayes_basket_target.production_io import (
    assert_p1_execution_evidence,
    STAGE_ZERO_EXECUTION_ANCHOR_SCHEMA,
    compile_production_cycle_from_ssot,
    load_materialized_probability_envelopes,
    load_production_market_snapshot,
    target_stage_zero_execution_anchor_from_cycle,
)
from bayes_basket_target.production_worker import TargetProductionWorker
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
from bayes_basket_target.atomic_ledger import initialize_capital_pool
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
)
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
from bayes_basket_target.forward_observation_store import (
    read_forward_observation_packet,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_bayes_basket_target import _cash_phase_inputs, _envelope, _policy_inputs
from pr12_atomic_test_support import awc_contract


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


def _seed_target_registry_lane(
    conn: sqlite3.Connection,
    *,
    station_id: str = "ZUUU",
    event_date: str = "2026-07-23",
    source_lane: str,
) -> None:
    """Install the minimal immutable target-local authority snapshot."""

    apply_target_market_registry_schema(conn)
    snapshot_sha = "9" * 64
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot("
        "snapshot_sha256,source_schema,source_scope,exported_at_utc,"
        "source_database_path,row_count,ingested_at_utc,content_sha256"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            snapshot_sha,
            "reported_trajectory_market_registry_v1",
            "shared_live_registry_export_only",
            "2026-07-23T05:00:00Z",
            "/test/shared-live.db",
            1,
            "2026-07-23T05:00:00Z",
            "8" * 64,
        ),
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry("
        "snapshot_sha256,slug,event_date,outcome,city_slug,station_id,"
        "settlement_timezone,settlement_unit,source_lane,source_eligible,"
        "source_block_reason,resolution_url,rules_sha256,yes_token,no_token,"
        "market_active,market_accepting_orders,registry_json,refreshed_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_sha,
            "highest-temperature-in-test",
            event_date,
            "38°C",
            "test-city",
            station_id,
            "America/New_York",
            "C",
            source_lane,
            1,
            "",
            "",
            "7" * 64,
            "yes-token",
            "no-token",
            1,
            1,
            json.dumps({"market_id": "market-1", "outcome": "38°C"}),
            "2026-07-23T05:00:00Z",
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
            # d0: the evaluation clock is the event's own local day; see
            # ``_policy_inputs`` in test_bayes_basket_target.
            "maturity_bin": "d0",
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
        "maturity_bin": "d0",
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
    # The XSB bridge requires the ladder's end buckets to be open-tailed, so
    # this three-bucket ladder has exactly one finite rung and no measurable
    # rung width; charter 53 refuses a Celsius dispersion without one.  The
    # fixture therefore keeps the base envelope's zero span rather than
    # publishing a dispersion it cannot spend.
    e["lineage"] = {
        **e["lineage"],
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
            "token_id": f"token-{bucket}",
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
        # The book, not the station's allowance: the stake cap is 2% of it and
        # P0 reaches 60% of that (``scope_stake_cap_usdc``, 2026-08-26), so a
        # 1000 USDC book gives this scope the 12 USDC the fixture always meant.
        "remaining_budget_usdc": 1000.0, "buckets_json": buckets,
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


def _stage_zero_compiled_cycle(conn: sqlite3.Connection) -> dict[str, object]:
    _seed(conn, cash_promoted=False)
    _seed_delta_v2(conn)
    return compile_production_cycle_from_ssot(
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


def _stage_zero_cycle_for_stage(
    conn: sqlite3.Connection,
    *,
    stage: str,
    compiled_cycle: dict[str, object] | None = None,
) -> dict[str, object]:
    compiled = (
        _stage_zero_compiled_cycle(conn)
        if compiled_cycle is None
        else deepcopy(compiled_cycle)
    )
    if stage == "P0":
        return compiled
    p0_probability = ProbabilityEnvelope.from_mapping(
        compiled["probability_envelope"]
    )
    lineage = {
        **dict(p0_probability.lineage),
        "p0_root_checkpoint_id": p0_probability.checkpoint_id,
        "p0_root_content_sha256": p0_probability.content_sha256,
    }
    awc_observation_contract = None
    if stage == "P1":
        # P1 roots on an observed METAR: the commit stages the revision fact
        # the contract carries and the lineage names that fact by content.
        awc_observation_contract, revision_sha = awc_contract(
            station_id="ZUUU",
            observation_id="obs-stage-zero-p1",
            event_date="2026-07-23",
            settlement_timezone=str(p0_probability.settlement_timezone),
            observed_at_utc="2026-07-23T05:00:01Z",
            raw_object_sha256="a" * 64,
        )
        lineage.update(
            {
                "canonical_observation_event_id": "obs-stage-zero-p1",
                "canonical_observation_sha256": "1" * 64,
                "canonical_observation_committed_at": "2026-07-23T05:00:01Z",
                "truth_observation_revision_content_sha256": revision_sha,
                "hard_bound_id": "bound-stage-zero-p1",
                "hard_excluded_buckets": "NONE",
                "source_lane": "AWC",
                "report_type": "METAR",
                "metar_trajectory_activation_id": "formal-activation-stage-zero",
                "metar_trajectory_artifact_id": "metar-trajectory-stage-zero",
                "metar_trajectory_proof_sha256": "2" * 64,
                "metar_conditional_path_state_sha256": "3" * 64,
                "metar_future_temperature_curve_sha256": "4" * 64,
                "metar_trajectory_applied_strength": "0.75",
                "observation_likelihood_mode": (
                    "CONDITIONAL_HOURLY_FUTURE_PATH_SOFT_LR_PLUS_HARD_EXCLUSION_V4"
                ),
            }
        )
    probability = ProbabilityEnvelope.from_mapping(
        {
            **p0_probability.to_dict(),
            "stage": stage,
            "checkpoint_id": f"{stage.lower()}-stage-zero",
            "decision_at_utc": "2026-07-23T05:00:01Z",
            "lineage": lineage,
        }
    )
    _insert_probability_row(
        conn,
        probability,
        created_at="2026-07-23T05:00:01Z",
    )
    regime = build_regime_artifact(probability)
    insert_regime_artifact(
        conn,
        regime,
        created_at_utc="2026-07-23T05:00:01Z",
    )
    compiled["probability_envelope"] = probability.to_dict()
    compiled["stage"] = stage
    if awc_observation_contract is not None:
        compiled["awc_observation_contract"] = awc_observation_contract
    compiled["plan"]["lineage"].update(
        {
            "probability_stage": stage,
            "probability_envelope_sha256": probability.content_sha256,
            "probability_decision_at_utc": probability.decision_at_utc,
            **(
                {
                    key: probability.lineage[key]
                    for key in (
                        "canonical_observation_event_id",
                        "canonical_observation_sha256",
                        "canonical_observation_committed_at",
                        "truth_observation_revision_content_sha256",
                        "hard_bound_id",
                        "source_lane",
                        "report_type",
                    )
                }
                if stage == "P1"
                else {}
            ),
        }
    )
    # A later stage is its own compile and carries its own plan address; the
    # copied P0 plan re-stamped for the stage must say so, because the
    # decision is keyed on what it decides (charter 69) and two stages that
    # present one plan address are one decision.
    compiled["plan"]["production_plan_sha256"] = content_sha256(
        {
            key: value
            for key, value in compiled["plan"].items()
            if key != "production_plan_sha256"
        }
    )
    compiled["production_inputs"].update(
        {
            "regime_artifact_sha256": regime.content_sha256,
            "regime_probability_sha256": probability.content_sha256,
        }
    )
    compiled["production_cycle_sha256"] = content_sha256(
        {
            key: value
            for key, value in compiled.items()
            if key not in {"production_cycle_sha256", "compiled_plan_sha256"}
        }
    )
    return compiled


def test_production_io_compiles_read_only_candidate_and_rejects_hash_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    # Charter 87 (2026-09-06): this fixture's book prices the seat where
    # charter 68's lambda(a) says most of the claim never arrives, so the
    # gate now refuses it; the property under test lives past the gate, so it runs the pre-87 arm.
    # The gate itself is pinned in test_clause_87_*.
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
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
        # The lane's mode is stamped on every plan since go-live (2026-08-31),
        # and the boundary's gateway flags are the lane's too; what stays
        # read-only about a compile is that it writes nothing and submits
        # nothing, and that the no-live candidate scope is carried as such.
        assert plan["execution_mode"] == PRODUCTION_EXECUTION_MODE
        assert plan["policy_authority_scope"] == "PRODUCTION_NO_LIVE_CANDIDATE"
        assert plan["boundary"]["writes_production_db"] is False
        assert plan["boundary"]["orders_submitted"] == 0
        assert plan["delta_v2_audit"]["authorized"] is True
        assert plan["production_inputs"]["probability_maturity_bin"] == "d0"
        assert (
            plan["production_inputs"]["configured_maturity_bin"]
            == "static-config-must-not-override-probability"
        )


def test_stage_zero_anchor_binds_complete_stage_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    # Charter 87 (2026-09-06): this fixture's book prices the seat where
    # charter 68's lambda(a) says most of the claim never arrives, so the
    # gate now refuses it; the property under test lives past the gate, so it runs the pre-87 arm.
    # The gate itself is pinned in test_clause_87_*.
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
    with sqlite3.connect(":memory:") as conn:
        compiled = _stage_zero_compiled_cycle(conn)

        anchor = target_stage_zero_execution_anchor_from_cycle(compiled)
        assert anchor["schema"] == STAGE_ZERO_EXECUTION_ANCHOR_SCHEMA
        assert anchor["probability_envelope"]["stage"] == "P0"
        assert anchor["compiled_plan"] == compiled
        assert anchor["content_sha256"] == content_sha256(
            {key: value for key, value in anchor.items() if key != "content_sha256"}
        )
        assert anchor["official_ws_snapshot"]["snapshot_id"] == "quote-1"
        assert anchor["inventory_snapshot"]["inventory_snapshot_id"] == "inv-snapshot-1"
        assert anchor["order_state_snapshot"]["order_state_snapshot_id"] == "ord-snapshot-1"
        assert "canonical_observation_event_id" not in anchor["probability_envelope"]["lineage"]

        for stage in ("P0-M", "P1"):
            stage_cycle = _stage_zero_cycle_for_stage(
                conn,
                stage=stage,
                compiled_cycle=compiled,
            )
            stage_anchor = target_stage_zero_execution_anchor_from_cycle(stage_cycle)
            assert stage_anchor["probability_envelope"]["stage"] == stage
            assert stage_anchor["compiled_plan"] == stage_cycle
            if stage == "P1":
                mismatched_p1 = deepcopy(stage_cycle)
                mismatched_p1["plan"]["lineage"]["hard_bound_id"] = (
                    "bound-mismatch"
                )
                with pytest.raises(
                    ContractError,
                    match="stage_zero_execution_p1_plan_canonical_facts_mismatch",
                ):
                    target_stage_zero_execution_anchor_from_cycle(
                        mismatched_p1
                    )


def test_stage_zero_anchor_fails_closed_on_stage_clock_and_cycle_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    # Charter 87 (2026-09-06): this fixture's book prices the seat where
    # charter 68's lambda(a) says most of the claim never arrives, so the
    # gate now refuses it; the property under test lives past the gate, so it runs the pre-87 arm.
    # The gate itself is pinned in test_clause_87_*.
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
    with sqlite3.connect(":memory:") as conn:
        compiled = _stage_zero_compiled_cycle(conn)

        p1_cycle = deepcopy(compiled)
        p1_cycle["probability_envelope"]["stage"] = "P1"
        p1_cycle["stage"] = "P1"
        p1_cycle["plan"]["lineage"]["probability_stage"] = "P1"
        with pytest.raises(
            ContractError, match="stage_zero_execution_p1_canonical_facts_missing"
        ):
            target_stage_zero_execution_anchor_from_cycle(p1_cycle)

        expiry_cycle = deepcopy(compiled)
        expiry_cycle["evaluation_at_utc"] = "2026-07-23T05:00:05Z"
        expiry_cycle["production_cycle_sha256"] = content_sha256(
            {
                key: value
                for key, value in expiry_cycle.items()
                if key not in {"production_cycle_sha256", "compiled_plan_sha256"}
            }
        )
        with pytest.raises(ContractError, match="stage_zero_execution_quote_stale"):
            target_stage_zero_execution_anchor_from_cycle(expiry_cycle)

        tampered_cycle = deepcopy(compiled)
        tampered_cycle["production_inputs"]["regime_artifact_sha256"] = "0" * 64
        with pytest.raises(
            ContractError, match="stage_zero_execution_production_cycle_sha_mismatch"
        ):
            target_stage_zero_execution_anchor_from_cycle(tampered_cycle)


@pytest.mark.parametrize(
    ("lineage_update", "lineage_remove"),
    [
        ({"metar_trajectory_activation_id": "NONE"}, None),
        ({"metar_trajectory_applied_strength": "0"}, None),
        ({"metar_trajectory_proof_sha256": "not-a-sha"}, None),
        ({"metar_conditional_path_state_sha256": "not-a-sha"}, None),
        ({"metar_future_temperature_curve_sha256": "not-a-sha"}, None),
        ({"observation_likelihood_mode": "HARD_EXCLUSION_ONLY"}, None),
        ({}, "metar_trajectory_artifact_id"),
        ({}, "metar_conditional_path_state_sha256"),
        ({}, "metar_future_temperature_curve_sha256"),
    ],
)
def test_stage_zero_anchor_rejects_p1_without_formal_strong_likelihood(
    lineage_update: dict[str, str],
    lineage_remove: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Charter 87 (2026-09-06): this fixture's book prices the seat where
    # charter 68's lambda(a) says most of the claim never arrives, so the
    # gate now refuses it; the property under test lives past the gate, so it runs the pre-87 arm.
    # The gate itself is pinned in test_clause_87_*.
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
    with sqlite3.connect(":memory:") as conn:
        p1_cycle = _stage_zero_cycle_for_stage(conn, stage="P1")
        lineage = p1_cycle["probability_envelope"]["lineage"]
        lineage.update(lineage_update)
        if lineage_remove is not None:
            lineage.pop(lineage_remove)
        with pytest.raises(
            ContractError,
            match="p1_strong_likelihood_missing",
        ):
            target_stage_zero_execution_anchor_from_cycle(p1_cycle)


# Stage-zero is the pre-final P0/P0-M path.  A P1 candidate is routed to the
# final-authority commit, which since go-live (2026-08-31) prepares the lane's
# intents; its rows are pinned by the atomic-ledger tests, not here.
@pytest.mark.parametrize("stage", ["P0", "P0-M"])
def test_resident_stage_zero_commits_only_no_execution_target_rows(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Charter 87 (2026-09-06): this fixture's book prices the seat where
    # charter 68's lambda(a) says most of the claim never arrives, so the
    # gate now refuses it; the property under test lives past the gate, so it runs the pre-87 arm.
    # The gate itself is pinned in test_clause_87_*.
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        candidate = _stage_zero_cycle_for_stage(conn, stage=stage)
        # Every materialized probability stage writes an exact d2 forward
        # fact.  Seed the same target-local market authority a file-backed
        # production target must already have; do not mask a missing-registry
        # contract with a test-only fallback.
        _seed_target_registry_lane(conn, source_lane="amsc")
        worker = object.__new__(TargetProductionWorker)
        worker.commit_conn = conn
        protected_tables = (
            "basket_target_observation_facts",
            "basket_target_bound_facts",
            "basket_target_order_intents",
            "basket_target_capital_reservations",
            "basket_policy_training_fact",
        )
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected_tables
        }

        result = worker._commit_current_stage_no_execution_candidate(
            candidate,
            station_id="ZUUU",
            event_date="2026-07-23",
            created_at_utc="2026-07-23T05:00:04Z",
        )

        assert result["state"] == "committed"
        assert int(result.get("intents_written") or 0) == 0
        assert int(result.get("reservations_written") or 0) == 0
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected_tables
        }
        # Pre-final facts bind only P0/P0-M, official WS, complete
        # inventory/order-state and the NO_EXECUTION policy.  They must
        # never manufacture a P1 observation/bound or reserve capital.
        assert after == {
            **before,
            "basket_policy_training_fact": (
                before["basket_policy_training_fact"] + 1
            ),
        }
        assert result["training_facts_written"] == 1
        assert result["forward_observation_status"] == "ELIGIBLE"
        fact = conn.execute(
            "SELECT f.stage,f.feature_json,v.training_fact_id "
            "FROM basket_policy_training_fact f "
            "JOIN basket_target_execution_version_vectors v "
            "ON v.training_fact_id=f.fact_id"
        ).fetchone()
        assert fact is not None
        assert fact[0] == stage
        assert fact[2]
        feature = json.loads(str(fact[1]))
        assert feature["no_execution_policy"]["policy"] == "NO_EXECUTION"
        assert feature["execution_version_vectors"] == []
        assert "canonical_observation_event_id" not in feature["plan"]["lineage"]
        replay = worker._commit_current_stage_no_execution_candidate(
            candidate,
            station_id="ZUUU",
            event_date="2026-07-23",
            created_at_utc="2026-07-23T05:00:04Z",
        )
        assert replay["state"] == "already_committed"
        assert replay["training_facts_written"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_policy_training_fact"
        ).fetchone()[0] == after["basket_policy_training_fact"]
        decision = conn.execute(
            "SELECT d.will_order,d.decided_state,d.reason_codes_json,"
            "v.vector_policy "
            "FROM auto_trade_decisions d "
            "JOIN basket_target_execution_version_vectors v "
            "ON v.cycle_id=d.cycle_id"
        ).fetchone()
        assert decision[0] == 0
        assert decision[1] == "blocked"
        assert decision[3] == "NO_EXECUTION"
        reason_codes = json.loads(str(decision[2]))
        assert isinstance(reason_codes, list) and reason_codes
        assert reason_codes == ["p1_final_authority_pending"]


def test_stage_zero_p0_and_p0m_flush_as_distinct_exact_stage_oos_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next forward flush preserves both same-clock pre-final facts."""
    # Charter 87 (2026-09-06): this fixture's book prices the seat where
    # charter 68's lambda(a) says most of the claim never arrives, so the
    # gate now refuses it; the property under test lives past the gate, so it runs the pre-87 arm.
    # The gate itself is pinned in test_clause_87_*.
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        p0_candidate = _stage_zero_cycle_for_stage(conn, stage="P0")
        # DB canonicalizes the formal AWC lane as lowercase ``awc``.
        _seed_target_registry_lane(conn, source_lane="awc")
        worker = object.__new__(TargetProductionWorker)
        worker.commit_conn = conn
        worker.cash_phase_forward_store_root = tmp_path
        worker.cash_phase_forward_store_path = tmp_path / "forward.sqlite3"

        for stage, candidate in (("P0", p0_candidate),):
            committed = worker._commit_current_stage_no_execution_candidate(
                candidate,
                station_id="ZUUU",
                event_date="2026-07-23",
                created_at_utc="2026-07-23T05:00:04Z",
            )
            assert committed["state"] == "committed"
            assert committed["forward_observation_status"] == "ELIGIBLE"
            assert committed["orders_submitted"] == 0
            assert committed["calls_order_gateway"] is False

            flushed = worker._flush_cash_phase_forward_observations()
            assert flushed["state"] == "committed"
            assert int(flushed.get("quarantined_count") or 0) == 0
            assert flushed["orders_submitted"] == 0
            assert flushed["calls_order_gateway"] is False

            packet = read_forward_observation_packet(
                worker.cash_phase_forward_store_path,
                allowed_root=tmp_path,
            )
            records = packet["observations"]
            expected_stages = {"P0"} if stage == "P0" else {"P0", "P0-M"}
            assert {record["probability_stage"] for record in records} == expected_stages

        # The P0 commit has advanced the durable stage boundary.  Only now
        # may the later P0-M become visible; both have the same cycle clock
        # and therefore exercise the stage discriminator in the packet key.
        p0m_candidate = _stage_zero_cycle_for_stage(
            conn,
            stage="P0-M",
            compiled_cycle=p0_candidate,
        )
        committed = worker._commit_current_stage_no_execution_candidate(
            p0m_candidate,
            station_id="ZUUU",
            event_date="2026-07-23",
            created_at_utc="2026-07-23T05:00:04Z",
        )
        assert committed["state"] == "committed"
        assert committed["forward_observation_status"] == "ELIGIBLE"
        assert committed["orders_submitted"] == 0
        assert committed["calls_order_gateway"] is False
        flushed = worker._flush_cash_phase_forward_observations()
        assert flushed["state"] == "committed"
        assert int(flushed.get("quarantined_count") or 0) == 0
        assert flushed["orders_submitted"] == 0
        assert flushed["calls_order_gateway"] is False

        packet = read_forward_observation_packet(
            worker.cash_phase_forward_store_path,
            allowed_root=tmp_path,
        )
        records = packet["observations"]
        assert len(records) == 2
        assert {record["probability_stage"] for record in records} == {"P0", "P0-M"}
        assert len({record["observation_key"] for record in records}) == 2
        assert all(record["decision_at_utc"] == "2026-07-23T05:00:04.000Z" for record in records)
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_capital_reservations"
        ).fetchone()[0] == 0


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


@pytest.mark.parametrize("row_factory", [None, sqlite3.Row])
def test_market_snapshot_restores_exact_prior_core_from_decision_receipt(
    row_factory: object,
) -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = row_factory
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
                "plan_core_1",
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


def test_production_io_resolves_delta_v2_policy_and_fill_from_ssot(monkeypatch: pytest.MonkeyPatch) -> None:
    # Charter 87 (2026-09-06): this fixture's book prices the seat where
    # charter 68's lambda(a) says most of the claim never arrives, so the
    # gate now refuses it; the property under test lives past the gate, so it runs the pre-87 arm.
    # The gate itself is pinned in test_clause_87_*.
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
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


def test_production_io_missing_delta_v2_authority_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Charter 87 (2026-09-06): this fixture's book prices the seat where
    # charter 68's lambda(a) says most of the claim never arrives, so the
    # gate now refuses it; the property under test lives past the gate, so it runs the pre-87 arm.
    # The gate itself is pinned in test_clause_87_*.
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
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


def test_current_p1_source_lane_blocks_fallback_but_historical_lane_can_be_superseded() -> None:
    """A wrong current P1 blocks; a newer authoritative P1 restores SSOT.

    This is the append-only repair path for the old China/AWC P1 rows.  The
    historical row remains in the table, but must never keep a later AMSC P1
    from becoming the current stage.
    """

    with sqlite3.connect(":memory:") as conn:
        root = _seed_native_probability_only(conn)
        old_awc = _stage_envelope(
            root,
            stage="P1",
            checkpoint_id="p1-old-awc",
            decision_at="2026-07-23T05:00:01Z",
            fresh_until="2026-07-23T06:00:00Z",
        )
        old_awc = ProbabilityEnvelope.from_mapping(
            {
                **old_awc.to_dict(),
                "lineage": {**dict(old_awc.lineage), "source_lane": "AWC"},
            }
        )
        _insert_probability_row(
            conn,
            old_awc,
            created_at="2026-07-23T05:00:01Z",
        )

        with pytest.raises(
            ContractError,
            match="probability_p1_source_lane_mismatch",
        ):
            load_materialized_probability_envelopes(
                conn,
                station_id="ZUUU",
                event_date="2026-07-23",
                evaluation_at_utc="2026-07-23T05:00:02Z",
                expected_source_lane="amsc",
            )

        p0m = _stage_envelope(
            root,
            stage="P0-M",
            checkpoint_id="p0m-new-amsc",
            decision_at="2026-07-23T05:00:02Z",
            fresh_until="2026-07-23T06:00:00Z",
        )
        _insert_probability_row(
            conn,
            p0m,
            created_at="2026-07-23T05:00:02Z",
        )
        new_amsc = _stage_envelope(
            p0m,
            stage="P1",
            checkpoint_id="p1-new-amsc",
            decision_at="2026-07-23T05:00:03Z",
            fresh_until="2026-07-23T06:00:00Z",
        )
        new_amsc = ProbabilityEnvelope.from_mapping(
            {
                **new_amsc.to_dict(),
                "lineage": {
                    **dict(new_amsc.lineage),
                    "p0_root_checkpoint_id": root.checkpoint_id,
                    "p0_root_content_sha256": root.content_sha256,
                    "prediction_root_stage": "P0-M",
                    "prediction_root_checkpoint_id": p0m.checkpoint_id,
                    "prediction_root_content_sha256": p0m.content_sha256,
                    "source_lane": "AMSC",
                },
            }
        )
        _insert_probability_row(
            conn,
            new_amsc,
            created_at="2026-07-23T05:00:03Z",
        )

        envelopes = load_materialized_probability_envelopes(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
            expected_source_lane="amsc",
        )
        assert select_current_stage(
            envelopes,
            at_utc="2026-07-23T05:00:04Z",
        ).checkpoint_id == "p1-new-amsc"


def test_current_p1_accepts_p0_root_and_keeps_append_only_history() -> None:
    """A valid P0-root P1 is current when no exact P0-M exists."""

    with sqlite3.connect(":memory:") as conn:
        root = _seed_native_probability_only(conn)
        legacy = _stage_envelope(
            root,
            stage="P1",
            checkpoint_id="p1-legacy-p0-root",
            decision_at="2026-07-23T05:00:01Z",
            fresh_until="2026-07-23T06:00:00Z",
        )
        legacy = ProbabilityEnvelope.from_mapping(
            {
                **legacy.to_dict(),
                "lineage": {
                    **dict(legacy.lineage),
                    "prediction_root_stage": "P0",
                    "prediction_root_checkpoint_id": root.checkpoint_id,
                    "prediction_root_content_sha256": root.content_sha256,
                    "source_lane": "AMSC",
                },
            }
        )
        _insert_probability_row(
            conn,
            legacy,
            created_at="2026-07-23T05:00:01Z",
        )

        envelopes = load_materialized_probability_envelopes(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:02Z",
            expected_source_lane="amsc",
        )
        assert envelopes[-1].lineage["prediction_root_stage"] == "P0"

        p0m = _stage_envelope(
            root,
            stage="P0-M",
            checkpoint_id="p0m-authoritative",
            decision_at="2026-07-23T05:00:02Z",
            fresh_until="2026-07-23T06:00:00Z",
        )
        _insert_probability_row(
            conn,
            p0m,
            created_at="2026-07-23T05:00:02Z",
        )
        authoritative = _stage_envelope(
            p0m,
            stage="P1",
            checkpoint_id="p1-p0m-root",
            decision_at="2026-07-23T05:00:03Z",
            fresh_until="2026-07-23T06:00:00Z",
        )
        authoritative = ProbabilityEnvelope.from_mapping(
            {
                **authoritative.to_dict(),
                "lineage": {
                    **dict(authoritative.lineage),
                    "p0_root_checkpoint_id": root.checkpoint_id,
                    "p0_root_content_sha256": root.content_sha256,
                    "prediction_root_stage": "P0-M",
                    "prediction_root_checkpoint_id": p0m.checkpoint_id,
                    "prediction_root_content_sha256": p0m.content_sha256,
                    "source_lane": "AMSC",
                },
            }
        )
        _insert_probability_row(
            conn,
            authoritative,
            created_at="2026-07-23T05:00:03Z",
        )

        envelopes = load_materialized_probability_envelopes(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
            expected_source_lane="amsc",
        )
        assert select_current_stage(
            envelopes,
            at_utc="2026-07-23T05:00:04Z",
        ).checkpoint_id == "p1-p0m-root"


def test_target_registry_lane_mismatch_rejects_p1_before_p0_fallback() -> None:
    """File-shaped target registry authority is stronger than P1 lineage."""

    with sqlite3.connect(":memory:") as conn:
        _seed(conn, cash_promoted=False)
        _seed_delta_v2(conn)
        _seed_target_registry_lane(conn, source_lane="amsc")
        wrong_lane = _stage_envelope(
            _envelope(),
            stage="P1",
            checkpoint_id="p1-registry-awc",
            decision_at="2026-07-23T05:00:01Z",
            fresh_until="2026-07-23T06:00:00Z",
        )
        wrong_lane = ProbabilityEnvelope.from_mapping(
            {
                **wrong_lane.to_dict(),
                "lineage": {
                    **dict(wrong_lane.lineage),
                    "source_lane": "AWC",
                },
            }
        )
        _insert_probability_row(
            conn,
            wrong_lane,
            created_at="2026-07-23T05:00:01Z",
        )

        with pytest.raises(
            ContractError,
            match="probability_p1_source_lane_mismatch",
        ):
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
                base_policy={"clip_max_notional_usdc": 3.0},
                cash_phase_authority_manifest=_cash_authority(
                    conn,
                    authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
                ),
            )


# P1 keeps serving for a bounded while past its cadence, so the instant at
# which each stage must fail closed differs.  What does not differ is that
# failing closed means blocking, never crossing down to the P0 underneath.
@pytest.mark.parametrize(
    "stage,expired_at",
    [
        ("P0-M", "2026-07-23T05:00:04Z"),
        ("P1", "2026-07-23T08:00:04Z"),
    ],
)
def test_highest_existing_stage_expiry_fails_closed_without_crossing(
    stage: str,
    expired_at: str,
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
            evaluation_at_utc=expired_at,
        )
        with pytest.raises(
            ContractError,
            match="latest_stage_stale_fail_closed",
        ):
            select_current_stage(
                envelopes,
                at_utc=expired_at,
            )


def test_p1_just_past_its_cadence_still_holds_authority() -> None:
    """The lapse that used to hand a whole event day back to nothing."""

    with sqlite3.connect(":memory:") as conn:
        root = _seed_native_probability_only(conn)
        p1 = _stage_envelope(
            root,
            stage="P1",
            checkpoint_id="P1-past-cadence",
            decision_at="2026-07-23T05:00:01Z",
            fresh_until="2026-07-23T05:00:03Z",
        )
        _insert_probability_row(conn, p1, created_at="2026-07-23T05:00:01Z")

        envelopes = load_materialized_probability_envelopes(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
        )
        selected = select_current_stage(
            envelopes,
            at_utc="2026-07-23T05:00:04Z",
        )
        assert selected.stage == "P1"
        assert selected.checkpoint_id == "P1-past-cadence"


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


def test_maturity_bin_is_the_event_distance_not_the_run_lead() -> None:
    """Two measurements, one vocabulary, and no table that shows both.

    ``d0``/``d1``/``d2``/``d2_fallback`` label a run's lead in one place and an
    event's maturity in another, so a join written
    ``ON a.lead_bucket = b.maturity_bin`` returns rows and looks right. This
    pins what the conversion actually promises, which is the only place the
    difference is visible at all.
    """

    from bayes_basket_target.production_io import derive_policy_maturity_bin

    common = {
        "event_date": "2026-08-28",
        "settlement_timezone": "Asia/Shanghai",
        "evaluation_at_utc": "2026-08-26T02:00:00Z",
    }

    # An hour bucket is converted, and what it converts to is the distance to
    # the event -- never the age of the run that produced it.  Every hour
    # bucket on the same station-day gives the same maturity.
    assert derive_policy_maturity_bin("h00_12", **common) == "d2"
    assert derive_policy_maturity_bin("h12_24", **common) == "d2"
    assert derive_policy_maturity_bin("h24_48", **common) == "d2"
    assert derive_policy_maturity_bin("h48_plus", **common) == "d2"

    # The distance is a local-day count, so the timezone decides it.
    assert derive_policy_maturity_bin(
        "h24_48",
        event_date="2026-08-26",
        settlement_timezone="Asia/Shanghai",
        evaluation_at_utc="2026-08-26T02:00:00Z",
    ) == "d0"

    # A calendar label chooses the vocabulary and nothing else.  It is a
    # property of the moment it was stamped, and this is a different moment:
    # a capture that was honestly d1 yesterday is d0 for a decision taken on
    # the event day, and it was the frozen label that used to win.
    assert derive_policy_maturity_bin("d1", **common) == "d2"
    assert derive_policy_maturity_bin("d0", **common) == "d2"
    assert derive_policy_maturity_bin("d2_fallback", **common) == "d2"

    # And an empty lead is refused rather than defaulted.
    with pytest.raises(ContractError):
        derive_policy_maturity_bin("", **common)

    # A calendar label outside the maturity vocabulary is refused rather than
    # carried into a policy lookup nothing can match.  The fallback lane's own
    # deriver can emit these: it computes d{delta} from the capture time with
    # no ceiling, so a stale capture yields d3 and a future one d-1.
    for outside in ("d3", "d-1", "D0"):
        with pytest.raises(ContractError) as caught:
            derive_policy_maturity_bin(outside, **common)
        assert "vocabulary_invalid" in str(caught.value)

    # The 'h' prefix is the branch selector, not a vocabulary member: anything
    # leading with it is converted from the dates and never checked against the
    # maturity set, so an unknown hour spelling still yields a real maturity.
    assert derive_policy_maturity_bin("h00_12x", **common) == "d2"


def test_market_snapshot_reports_the_real_account_not_the_pool(monkeypatch) -> None:
    """Charter 141: ``book_equity_usdc`` is the real account, budget-capped.

    The pool counter (seeded with a paper balance, never credited with a
    payout) is no longer read at all: a pool of 514.29 changes nothing.  With
    no account table the snapshot carries no equity (a fixture sizes off its
    budget as before); with the table and a fresh read it carries cash plus
    positions, capped by the budget dial, and beside it the basket's own open
    exposure (charter 223) -- not the account's positions, which this ledger
    holds no open basket order for; with the table and no fresh read the
    compile is refused.
    """

    import pytest
    from contextlib import contextmanager

    import bayes_basket_target.runtime_config as runtime_config
    from bayes_basket_target.account_equity import capture_account_equity

    monkeypatch.setattr(
        runtime_config, "load_target_live_runtime_config",
        lambda *a, **k: {"target_remaining_budget_usdc": 90.0},
    )
    with sqlite3.connect(":memory:") as conn:
        _seed(conn, cash_promoted=False)
        _seed_delta_v2(conn)
        initialize_capital_pool(
            conn,
            balance_usdc=514.29,
            capital_epoch_id="book-equity-fixture",
            updated_at_utc="2026-07-23T05:00:04Z",
            commit=False,
        )
        bare = load_production_market_snapshot(
            conn, station_id="ZUUU", event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
        )
        assert "book_equity_usdc" not in bare

        @contextmanager
        def write():
            yield

        receipt = capture_account_equity(
            conn, as_of_utc="2026-07-23T05:00:00.000Z", write=write,
            cash_reader=lambda **k: (55.28, "2026-07-23T05:00:00.000Z"),
            positions_reader=lambda **k: 44.52,
        )
        assert receipt["state"] == "recorded"
        real = load_production_market_snapshot(
            conn, station_id="ZUUU", event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T05:00:04Z",
        )
        # 99.80 in the account, capped at the 90 the budget dial allows.
        assert real["book_equity_usdc"] == pytest.approx(90.0)
        assert real["basket_open_exposure_usdc"] == 0.0
        assert real["account_positions_value_usdc"] == pytest.approx(44.52)
        assert real["account_cash_usdc"] == pytest.approx(55.28)
        # The scope's own budget is untouched by the equity.
        assert real["remaining_budget_usdc"] == bare["remaining_budget_usdc"]

        with pytest.raises(Exception, match="production_account_equity_stale"):
            load_production_market_snapshot(
                conn, station_id="ZUUU", event_date="2026-07-23",
                evaluation_at_utc="2026-07-23T06:30:04Z",
            )


def _suspended_p1_lineage(
    receipt: dict[str, str] | None,
    *,
    four_layer: dict[str, str] | None = None,
    regional: dict[str, str] | None = None,
) -> dict[str, str]:
    """The served lineage of a P1 compiled under the 2026-09-02 suspension."""

    values = {
        "canonical_observation_event_id": "obs-suspended-p1",
        "canonical_observation_sha256": "1" * 64,
        "canonical_observation_committed_at": "2026-09-02T05:00:01Z",
        "hard_bound_id": "hardbound-" + "2" * 32,
        "hard_excluded_buckets": "26°C or below,27°C",
        "settlement_timezone": "Asia/Karachi",
        "source_lane": "AWC",
        "report_type": "METAR",
        "metar_trajectory_proof_sha256": "2" * 64,
        "metar_trajectory_applied_strength": "0",
        "metar_trajectory_soft_likelihood_policy": (
            "SUSPENDED_BY_RULING_2026_09_02"
        ),
        "observation_likelihood_mode": (
            "HARD_EXCLUSION_PLUS_CLOCK_CEILING_SOFT_LR_SUSPENDED_V6"
        ),
        # The artifact may or may not have resolved; under suspension neither
        # decides tradability, so the darkest possible lineage is the test.
        "metar_trajectory_activation_id": "NONE",
        "metar_trajectory_artifact_id": "metar-trajectory-fallback",
        "metar_conditional_path_state_sha256": "NONE",
        "metar_future_temperature_curve_sha256": "NONE",
    }
    if receipt is not None:
        values["metar_ceiling_json"] = json.dumps(
            {"schema": "metar_ceiling_receipt_v1", **receipt}, sort_keys=True
        )
    if four_layer is not None:
        values["p1_four_layer_serving_json"] = json.dumps(
            {"schema": "p1_four_layer_serving_receipt_v1", **four_layer},
            sort_keys=True,
        )
    if regional is not None:
        values["regional_path_prior_json"] = json.dumps(
            {"schema": "regional_path_prior_receipt_v1", **regional},
            sort_keys=True,
        )
    return values


@pytest.mark.parametrize(
    "receipt",
    [
        {"metar_ceiling_applied": "true", "metar_ceiling_hours_left": "8.5"},
        {"metar_ceiling_applied": "false",
         "metar_ceiling_skip_reason": "TOO_EARLY"},
        {"metar_ceiling_applied": "false",
         "metar_ceiling_skip_reason": "SPLIT_DEGENERATE"},
    ],
)
def test_p1_execution_evidence_admits_suspended_p1_on_hard_bound_and_ceiling(
    receipt: dict[str, str],
) -> None:
    """Under suspension the evidence P1 must carry is the hard bound and the
    clock ceiling having reached the served distribution -- a ceiling that
    declined for this step is still the layer speaking.  No artifact needs to
    have resolved: the layer it would have governed is silent by policy."""

    assert_p1_execution_evidence(_suspended_p1_lineage(receipt))


@pytest.mark.parametrize(
    ("lineage_update", "lineage_remove"),
    [
        # No ceiling receipt at all: the layer never reached the envelope.
        ({}, "metar_ceiling_json"),
        # The layer was absent, not declining.
        ({"metar_ceiling_json": json.dumps({
            "schema": "metar_ceiling_receipt_v1",
            "metar_ceiling_applied": "false",
            "metar_ceiling_skip_reason": "DISABLED"})}, None),
        ({"metar_ceiling_json": json.dumps({
            "schema": "metar_ceiling_receipt_v1",
            "metar_ceiling_applied": "false",
            "metar_ceiling_skip_reason": "NO_ARTIFACT"})}, None),
        # A zero strength without the declared policy is the old failure.
        ({"metar_trajectory_soft_likelihood_policy": "ACTIVE"}, None),
        ({}, "metar_trajectory_soft_likelihood_policy"),
        # A suspended mode that still carries strength is not suspended.
        ({"metar_trajectory_applied_strength": "0.4"}, None),
        ({"metar_trajectory_proof_sha256": "not-a-sha"}, None),
    ],
)
def test_p1_execution_evidence_rejects_suspended_p1_missing_its_evidence(
    lineage_update: dict[str, str],
    lineage_remove: str | None,
) -> None:
    lineage = _suspended_p1_lineage({
        "metar_ceiling_applied": "false",
        "metar_ceiling_skip_reason": "TOO_EARLY",
    })
    lineage.update(lineage_update)
    if lineage_remove is not None:
        lineage.pop(lineage_remove)
    with pytest.raises(
        ContractError,
        match="p1_execution_evidence_missing",
    ):
        assert_p1_execution_evidence(lineage)


def test_p1_execution_evidence_admits_the_layer_that_replaced_the_ceiling(
) -> None:
    """The 2026-09-07 promotion put the four-layer chain at P1's outermost
    composition point, and a chain that speaks suppresses the ceiling call --
    so the served row carries a chain receipt and no ceiling receipt at all.
    That row is the best-evidenced P1 production makes, and the gate has to
    admit it.  Replaying the real rows of 2026-09-07 through this rule before
    the fix refused 2558 of 2916, 755 of them chain-served."""

    assert_p1_execution_evidence(
        _suspended_p1_lineage(None, four_layer={"applied": "true"})
    )


def test_p1_execution_evidence_admits_the_regional_prior_when_it_serves(
) -> None:
    """Second claimant, same rule: when the chain cannot speak the regional
    path prior answers for the step and the ceiling is not called."""

    assert_p1_execution_evidence(
        _suspended_p1_lineage(
            None, regional={"regional_path_prior_applied": "true"}
        )
    )


@pytest.mark.parametrize(
    ("four_layer", "regional"),
    [
        # A chain that stood down is not the chain speaking.  It needs no
        # clause of its own: when it stands down the next claimant runs and
        # writes its own receipt, and here neither did.
        ({"applied": "false", "refused": "ValueError:empty intercept"}, None),
        ({"applied": "false"}, None),
        (None, {"regional_path_prior_applied": "false"}),
        # Both stood down and nothing else answered.
        (
            {"applied": "false"},
            {"regional_path_prior_applied": "false"},
        ),
    ],
)
def test_p1_execution_evidence_rejects_a_claimant_that_stood_down(
    four_layer: dict[str, str] | None,
    regional: dict[str, str] | None,
) -> None:
    with pytest.raises(
        ContractError,
        match="p1_execution_evidence_missing",
    ):
        assert_p1_execution_evidence(
            _suspended_p1_lineage(
                None, four_layer=four_layer, regional=regional
            )
        )


def test_p1_execution_evidence_reads_the_chain_receipt_by_its_schema() -> None:
    """A payload that is not the receipt the materializer writes is not
    evidence, however applied it claims to be -- the same rule the ceiling
    branch has always applied to its own schema."""

    lineage = _suspended_p1_lineage(None)
    lineage["p1_four_layer_serving_json"] = json.dumps(
        {"schema": "p1_four_layer_shadow_receipt_v1", "applied": "true"},
        sort_keys=True,
    )
    with pytest.raises(
        ContractError,
        match="p1_execution_evidence_missing",
    ):
        assert_p1_execution_evidence(lineage)

    lineage["p1_four_layer_serving_json"] = "{not json"
    with pytest.raises(
        ContractError,
        match="p1_execution_evidence_missing",
    ):
        assert_p1_execution_evidence(lineage)


def test_p1_execution_evidence_still_needs_the_hard_bound_under_a_chain(
) -> None:
    """The outermost layer is one half of the clause.  A chain receipt does
    not excuse a P1 whose suspension is undeclared or whose proof is not
    content-addressed."""

    for update in (
        {"metar_trajectory_soft_likelihood_policy": "ACTIVE"},
        {"metar_trajectory_applied_strength": "0.4"},
        {"metar_trajectory_proof_sha256": "not-a-sha"},
    ):
        with pytest.raises(
            ContractError,
            match=(
                "p1_execution_evidence_missing"
            ),
        ):
            assert_p1_execution_evidence({
                **_suspended_p1_lineage(None, four_layer={"applied": "true"}),
                **update,
            })


def test_p1_execution_evidence_keeps_the_old_rule_for_the_old_modes() -> None:
    """Pre-suspension lineage is judged as before: a resolved promoted
    artifact at a strength in (0, 1] admits, a zero strength does not, and a
    dark placeholder in an old mode is not rescued by the new branch."""

    formal = {
        **_suspended_p1_lineage(None),
        "metar_trajectory_activation_id": "formal-activation",
        "metar_trajectory_artifact_id": "metar-trajectory-formal",
        "metar_conditional_path_state_sha256": "3" * 64,
        "metar_future_temperature_curve_sha256": "none",
        "metar_trajectory_applied_strength": "0.6",
        "metar_trajectory_soft_likelihood_policy": "ACTIVE",
        "observation_likelihood_mode": (
            "PHYSICAL_ABSOLUTE_TMAX_SOFT_LR_PLUS_HARD_EXCLUSION_V5"
        ),
    }
    assert_p1_execution_evidence(formal)
    for update in (
        {"metar_trajectory_applied_strength": "0"},
        {"metar_trajectory_activation_id": "NONE"},
        {"observation_likelihood_mode": (
            "FULL_PREFIX_ROOT_RECOMPUTE_SOFT_LR_PLUS_HARD_EXCLUSION_V2"
        )},
    ):
        with pytest.raises(
            ContractError,
            match="p1_strong_likelihood_missing",
        ):
            assert_p1_execution_evidence({**formal, **update})
