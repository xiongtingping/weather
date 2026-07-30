from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.atomic_ledger import initialize_capital_pool
from bayes_basket_target.cash_phase_authority import (
    CashPhaseAuthorityManifest,
)
from bayes_basket_target.contracts import (
    FORMAL_ENSEMBLE_METHOD_VERSION,
    ProbabilityEnvelope,
    content_sha256,
    model_set_hash,
)
from bayes_basket_target.execution_reconciliation import (
    apply_execution_receipt,
    build_no_live_execution_receipt,
)
from bayes_basket_target.delta_v2_artifact import resolve_delta_v2_policy
from bayes_basket_target.delta_v2_fill_artifact import (
    resolve_delta_v2_fill_distribution,
)
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
)
from bayes_basket_target.production import compile_production_plan
from bayes_basket_target.production_io import (
    compile_production_cycle_from_ssot,
)
from bayes_basket_target.probability_bridge import (
    materialize_probability_bridge,
)
from bayes_basket_target.production_worker import TargetProductionWorker
from bayes_basket_target.regime_materialization import (
    build_regime_artifact,
    insert_regime_artifact,
)
from bayes_basket_target.resident_market_ingress import (
    materialize_resident_no_live_market_evidence,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from tests.bayes_basket_target_e2e_fixtures import (
    _cash_input,
    _cash_policy,
    _envelope,
    _policy_inputs,
)
from tests.test_bayes_basket_target_production_io import _seed_delta_v2
from tests.test_bayes_basket_target_resident_market_ingress import (
    _activation,
)


DECISION = "2026-07-23T04:30:00Z"
RECEIPT_AT = "2026-07-23T04:30:02Z"
RECOMPUTE_AT = "2026-07-23T04:30:04Z"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _manifest(
    root: Path,
    *,
    observed_at: str,
    generation: int,
) -> Path:
    shard_dir = root / f"shards-{generation}"
    shard_dir.mkdir()
    for ordinal, bucket in enumerate(("37", "38", "39")):
        token = f"yes-{bucket}"
        price = (0.12, 0.22, 0.12)[ordinal]
        quote = {
            "token_id": token,
            "event_date": "2026-07-23",
            "outcome": bucket,
            "side": "YES",
            "best_bid": price - 0.02,
            "best_ask": price,
            "bids": [{"price": price - 0.02, "size": 100.0}],
            "asks": [{"price": price, "size": 100.0}],
            "book_observed_at": observed_at,
            "created_at": observed_at,
        }
        core = {
            "schema": "clob_ws_token_quote_snapshot_v1",
            "token_id": token,
            "quote": quote,
            "shard_index": 0,
            "shard_generation": generation,
            "shard_active": True,
            "source": "official_polymarket_clob_websocket",
            "evidence_only": True,
            "authorizes_order": False,
        }
        shard = {
            **core,
            "snapshot_sha256": hashlib.sha256(
                _canonical(core)
            ).hexdigest(),
        }
        (shard_dir / f"{token}.json").write_text(
            json.dumps(shard),
            encoding="utf-8",
        )
    manifest = {
        "schema": "clob_ws_latest_quote_snapshot_v1",
        "state": "connected",
        "active_shards": [0],
        "expected_shards": 1,
        "active_shard_generations": {"0": generation},
        "quote_storage": "sha256_token_shards_v1",
        "quote_shard_dir": str(shard_dir),
        "quotes_by_token": {},
        "materialized_token_count": 3,
        "unmaterialized_tokens": [],
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
    }
    manifest["snapshot_sha256"] = hashlib.sha256(
        _canonical(manifest)
    ).hexdigest()
    path = root / f"manifest-{generation}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _seed_policy_authority(conn: sqlite3.Connection) -> None:
    prior, policies, activations = _policy_inputs()
    conn.execute(
        "INSERT INTO basket_global_prior_artifact VALUES(?,?,?,?,?,?,?)",
        (
            prior.global_prior_id,
            prior.strategy_version,
            json.dumps(prior.family_parameters),
            json.dumps(prior.default_policy),
            prior.valid_from_utc,
            prior.valid_until_utc,
            prior.content_sha256,
        ),
    )
    conn.execute(
        "INSERT INTO basket_policy_promotion VALUES(?,?,?,?,?,?,?,?)",
        (
            "post-fill-promote-prior",
            "GLOBAL_PRIOR",
            prior.global_prior_id,
            1,
            "PROMOTED",
            "post-fill-gate-v1",
            "2026-07-22T00:00:00Z",
            "a" * 64,
        ),
    )
    for policy in policies:
        conn.execute(
            "INSERT INTO basket_station_policy_artifact "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                policy.artifact_id,
                policy.family,
                policy.station_id,
                policy.stage,
                policy.season_bin,
                policy.maturity_bin,
                policy.liquidity_bin,
                policy.strategy_version,
                json.dumps(policy.theta_local),
                policy.effective_sample_n,
                policy.kappa,
                policy.trained_through_utc,
                policy.valid_from_utc,
                policy.valid_until_utc,
                policy.content_sha256,
            ),
        )
        conn.execute(
            "INSERT INTO basket_policy_promotion VALUES(?,?,?,?,?,?,?,?)",
            (
                f"post-fill-promote-{policy.family}",
                "STATION_POLICY",
                policy.artifact_id,
                1,
                "PROMOTED",
                "post-fill-gate-v1",
                "2026-07-22T00:00:00Z",
                "b" * 64,
            ),
        )
    activation = activations[0]
    conn.execute(
        "INSERT INTO basket_policy_activation "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            activation.activation_id,
            activation.station_id,
            activation.stage,
            activation.season_bin,
            activation.maturity_bin,
            activation.liquidity_bin,
            activation.strategy_version,
            activation.global_prior_id,
            activation.target_artifact_id,
            activation.reserve_artifact_id,
            activation.cadence_artifact_id,
            json.dumps(list(activation.fallback_families)),
            activation.generation,
            activation.valid_from_utc,
            activation.valid_until_utc,
            activation.activated_at_utc,
            activation.content_sha256,
        ),
    )
    conn.execute(
        "INSERT INTO basket_policy_promotion VALUES(?,?,?,?,?,?,?,?)",
        (
            "post-fill-promote-activation",
            "STATION_ACTIVATION",
            activation.activation_id,
            1,
            "PROMOTED",
            "post-fill-gate-v1",
            "2026-07-22T00:00:00Z",
            "c" * 64,
        ),
    )


def _seed_cash_authority(
    conn: sqlite3.Connection,
) -> CashPhaseAuthorityManifest:
    policy = _cash_policy("post-fill-cash-v1", 1.8)
    conn.execute(
        "INSERT INTO basket_cash_phase_policy_artifact "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            policy.policy_version,
            policy.station_id,
            policy.station_timezone,
            policy.heavy_to_standard_local_time,
            policy.standard_to_tail_local_time,
            json.dumps(policy.cash_by_phase),
            policy.valid_from_utc,
            policy.valid_until_utc,
            policy.evidence_status,
            0,
            policy.content_sha256,
        ),
    )
    manifest = CashPhaseAuthorityManifest.create(
        station_id=policy.station_id,
        policy_version=policy.policy_version,
        artifact_sha256=policy.content_sha256,
        station_timezone=policy.station_timezone,
        valid_from_utc=policy.valid_from_utc,
        valid_until_utc=policy.valid_until_utc,
        authority_scope=AUTHORITY_SCOPE,
    )
    conn.execute(
        "INSERT INTO basket_cash_phase_authority_manifest "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            manifest.manifest_sha256,
            manifest.station_id,
            manifest.policy_version,
            manifest.artifact_sha256,
            manifest.station_timezone,
            manifest.valid_from_utc,
            manifest.valid_until_utc,
            manifest.authority_scope,
        ),
    )
    return manifest


def _seed_probability(conn: sqlite3.Connection) -> None:
    root = _formal_p0_envelope()
    envelope = _p1_envelope(root=root)
    for item in (root, envelope):
        materialize_probability_bridge(
            conn,
            item,
            created_at_utc=DECISION,
            bucket_intervals={
                "37": [None, 37.5],
                "38": [37.5, 38.5],
                "39": [38.5, None],
            },
        )
    insert_regime_artifact(
        conn,
        build_regime_artifact(envelope),
        created_at_utc=DECISION,
    )


def _formal_p0_envelope() -> ProbabilityEnvelope:
    models = ("ECMWF", "GFS")
    available_hash = model_set_hash(models)
    compatibility_identity = {
        "schema": "bayes_basket_taf_nwp_live_compatibility_fingerprint_v1",
        "station_id": "ZUUU",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "available_model_set_hash": available_hash,
        "available_models": list(models),
        "resolved_weights": {"ECMWF": 0.6, "GFS": 0.4},
        "ensemble_artifact_id": "post-fill-ensemble",
        "ensemble_artifact_content_sha256": "e" * 64,
        "ensemble_selection_evidence_sha256": "f" * 64,
        "ensemble_weight_method": FORMAL_ENSEMBLE_METHOD_VERSION,
        "model_calibration_artifacts": {
            "ECMWF": {
                "calibration_artifact_id": "post-fill-cal-ecmwf",
                "calibration_artifact_content_sha256": "1" * 64,
            },
            "GFS": {
                "calibration_artifact_id": "post-fill-cal-gfs",
                "calibration_artifact_content_sha256": "2" * 64,
            },
        },
    }
    fingerprint = {
        **compatibility_identity,
        "fingerprint_sha256": content_sha256(compatibility_identity),
    }
    source = _envelope("P0", "post-fill-p0").to_dict()
    source.update({
        "decision_at_utc": "2026-07-23T04:29:59Z",
        "posterior_version": "post-fill-p0",
        "lineage": {
            **source["lineage"],
            "settlement_timezone": "Asia/Shanghai",
            "season_bin": "summer",
            "lead_bucket": "d1",
            "run_cycle": "00Z",
            "settlement_definition": "official_daily_tmax_c",
            "model_run_ids": "post-fill-run-ecmwf,post-fill-run-gfs",
            "calibration_artifact_ids": (
                "post-fill-cal-ecmwf,post-fill-cal-gfs"
            ),
            "activation_ids": (
                "post-fill-act-ecmwf,post-fill-act-gfs,"
                "post-fill-act-ensemble"
            ),
            "ensemble_artifact_id": "post-fill-ensemble",
            "available_model_set_hash": available_hash,
            "selection_evidence_sha256": "f" * 64,
            "ensemble_artifact_content_sha256": "e" * 64,
            "peripheral_model_reliability_by_bucket_json": (
                '{"37":0.8,"38":0.9,"39":0.8}'
            ),
            "formal_model_members_json": json.dumps(
                [
                    {
                        "model_id": "ECMWF",
                        "calibrated_center_bucket": "39",
                        "calibrated_center_c": 39.0,
                        "weight": 0.6,
                    },
                    {
                        "model_id": "GFS",
                        "calibrated_center_bucket": "37",
                        "calibrated_center_c": 37.0,
                        "weight": 0.4,
                    },
                ],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "formal_model_anchor_lower_ordinal": "37",
            "formal_model_anchor_upper_ordinal": "39",
            "taf_nwp_live_compatibility_fingerprint_sha256": (
                fingerprint["fingerprint_sha256"]
            ),
            "taf_nwp_live_compatibility_json": json.dumps(
                fingerprint,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    })
    return ProbabilityEnvelope.from_mapping(source)


def _p1_envelope(
    *,
    root: ProbabilityEnvelope | None = None,
) -> ProbabilityEnvelope:
    source = _envelope("P1", "post-fill-p1").to_dict()
    source["lineage"] = {
        **source["lineage"],
        "settlement_timezone": "Asia/Shanghai",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        **({
            "p0_root_checkpoint_id": root.checkpoint_id,
            "p0_root_content_sha256": root.content_sha256,
            "peripheral_model_reliability_by_bucket_json": str(
                root.lineage[
                    "peripheral_model_reliability_by_bucket_json"
                ]
            ),
            "formal_model_members_json": str(
                root.lineage["formal_model_members_json"]
            ),
            "formal_model_anchor_lower_ordinal": str(
                root.lineage["formal_model_anchor_lower_ordinal"]
            ),
            "formal_model_anchor_upper_ordinal": str(
                root.lineage["formal_model_anchor_upper_ordinal"]
            ),
        } if root is not None else {}),
    }
    return ProbabilityEnvelope.from_mapping(source)


def _seed_registry_and_activation(
    conn: sqlite3.Connection,
    activation_path: Path,
) -> None:
    apply_target_market_registry_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            "1" * 64,
            "reported_trajectory_market_registry_v1",
            "shared_live_registry_export_only",
            DECISION,
            "/read-only/post-fill-e2e.db",
            3,
            DECISION,
            "2" * 64,
        ),
    )
    conn.executemany(
        """
        INSERT INTO basket_target_market_registry(
          snapshot_sha256,slug,event_date,outcome,city_slug,station_id,
          settlement_timezone,settlement_unit,source_lane,source_eligible,
          source_block_reason,resolution_url,rules_sha256,yes_token,
          no_token,market_active,market_accepting_orders,registry_json,
          refreshed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                "1" * 64,
                "post-fill-market",
                "2026-07-23",
                bucket,
                "chengdu",
                "ZUUU",
                "Asia/Shanghai",
                "C",
                "amsc",
                1,
                "",
                "",
                "3" * 64,
                f"yes-{bucket}",
                f"no-{bucket}",
                1,
                1,
                "{}",
                DECISION,
            )
            for bucket in ("37", "38", "39")
        ],
    )
    conn.execute(
        "CREATE TABLE reported_trajectory_market_registry("
        "slug TEXT,event_date TEXT,station_id TEXT,"
        "settlement_timezone TEXT,outcome TEXT,yes_token TEXT,"
        "registry_json TEXT,source_eligible INTEGER,"
        "market_active INTEGER,market_accepting_orders INTEGER)"
    )
    conn.executemany(
        "INSERT INTO reported_trajectory_market_registry "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "post-fill-market",
                "2026-07-23",
                "ZUUU",
                "Asia/Shanghai",
                bucket,
                f"yes-{bucket}",
                json.dumps({"lower": int(bucket), "upper": int(bucket)}),
                1,
                1,
                1,
            )
            for bucket in ("37", "38", "39")
        ],
    )
    activation = json.loads(
        activation_path.read_text(encoding="utf-8")
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_strategy_activation_state("
        "singleton INTEGER PRIMARY KEY,generation INTEGER,state TEXT,"
        "strategy_id TEXT,activation_sha256 TEXT,activation_json TEXT,"
        "updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_strategy_activation_state "
        "VALUES(1,?,?,?,?,?,?)",
        (
            1,
            "enabled",
            activation["strategy_id"],
            activation["activation_contract_sha256"],
            json.dumps(activation),
            DECISION,
        ),
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_strategy_cycles(state TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_strategy_parent_orders("
        "state TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_strategy_child_orders("
        "state TEXT)"
    )


@pytest.mark.parametrize(
    ("receipt_type", "fill_fraction", "expected_transition_state"),
    [
        ("MAKER_FULL_FILL", 1.0, "FULL_FILLED"),
        ("MAKER_PARTIAL_FILL", 0.25, "PARTIAL_FILLED"),
    ],
)
def test_real_candidate_receipt_projection_recompute_commit_and_replay(
    tmp_path: Path,
    receipt_type: str,
    fill_fraction: float,
    expected_transition_state: str,
) -> None:
    activation_path = _activation(
        tmp_path,
        authority_scope=AUTHORITY_SCOPE,
    )
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        _seed_policy_authority(conn)
        cash_authority = _seed_cash_authority(conn)
        _seed_probability(conn)
        _seed_delta_v2(
            conn,
            stage="P1",
            price_by_bucket={"37": 0.11, "38": 0.21, "39": 0.11},
        )
        _seed_registry_and_activation(conn, activation_path)
        initialize_capital_pool(
            conn,
            balance_usdc=30.0,
            capital_epoch_id="post-fill-real-e2e-epoch",
            updated_at_utc=DECISION,
        )
        initial_market = materialize_resident_no_live_market_evidence(
            conn,
            manifest_path=_manifest(
                tmp_path,
                observed_at=DECISION,
                generation=1,
            ),
            activation_path=activation_path,
            remaining_budget_usdc=30.0,
            policy_authority_scope=AUTHORITY_SCOPE,
            cash_phase_authority_manifest=cash_authority,
        )
        assert initial_market["state"] == "COMMITTED"

        ssot_candidate = compile_production_cycle_from_ssot(
            conn,
            station_id="ZUUU",
            event_date="2026-07-23",
            evaluation_at_utc="2026-07-23T04:30:01Z",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            policy_authority_scope=AUTHORITY_SCOPE,
                base_policy={
                    "clip_max_notional_usdc": 3.0,
                    "clip_min_notional_usdc": 0.1,
                    "base_coverage": 0.4,
                "maximum_coverage": 0.4,
            },
        )
        market = dict(ssot_candidate["production_market_snapshot"])
        market["cadence_snapshot"] = {
            "observed_at_utc": DECISION,
            "order_state": "FLAT",
            "hazard_score": 0.67,
            "safety_gates_passed": True,
            "candidate_present": True,
            "checkpoint_version": "post-fill-p1",
            "quote_version": market["quote_version"],
            "fill_version": market["inventory_version"],
            "time_gate_version": "post-fill-time-2",
        }
        market["previous_cadence_snapshot"] = {
            "observed_at_utc": "2026-07-23T04:24:59Z",
            "order_state": "FLAT",
            "hazard_score": 0.67,
            "safety_gates_passed": True,
            "candidate_present": True,
            "checkpoint_version": "post-fill-p0m",
            "quote_version": "previous-quote",
            "fill_version": "previous-inventory",
            "time_gate_version": "post-fill-time-1",
        }
        prior, policies, activations = _policy_inputs()
        delta_v2_artifact = resolve_delta_v2_policy(
            conn,
            station_id="ZUUU",
            stage="P1",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            evaluation_at_utc="2026-07-23T04:30:01Z",
        )

        def resolve_fill(**kwargs: object) -> dict[str, object]:
            mode = str(kwargs["order_mode"]).upper()
            timeout = (
                float(kwargs["timeout_seconds"])
                if mode == "MAKER"
                else float(delta_v2_artifact.timeout_grid_seconds[0])
            )
            artifact = resolve_delta_v2_fill_distribution(
                conn,
                station_id="ZUUU",
                stage="P1",
                season_bin="summer",
                maturity_bin="d1",
                liquidity_bin="liquid",
                bucket=str(kwargs["bucket"]),
                price=float(kwargs["limit_price"]),
                timeout_seconds=timeout,
                queue_ahead_shares=float(kwargs["queue_ahead_shares"]),
                spread=float(kwargs["spread"]),
                depth=float(kwargs["depth"]),
                quote_version=str(kwargs["quote_version"]),
                evaluation_at_utc="2026-07-23T04:30:01Z",
                policy_fill_distribution_artifact_sha256=(
                    delta_v2_artifact.fill_distribution_artifact_sha256
                ),
            )
            return {
                "fill_distribution": artifact.to_kernel_fill_distribution(
                    mode
                ),
                "execution_costs": artifact.to_kernel_execution_costs(mode),
                "fill_artifact_id": artifact.artifact_id,
                "fill_artifact_content_sha256": artifact.content_sha256,
                "fill_distribution_artifact_sha256": (
                    artifact.fill_distribution_artifact_sha256
                ),
            }

        formal_p1 = _p1_envelope(root=_formal_p0_envelope())
        initial_candidate = compile_production_plan(
            probability_envelopes=[formal_p1],
            market_snapshot=market,
            policy_activations=activations,
            global_priors={prior.global_prior_id: prior},
            policy_artifacts={
                item.artifact_id: item for item in policies
            },
            evaluation_at_utc="2026-07-23T04:30:01Z",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            base_policy={
                "clip_max_notional_usdc": 3.0,
                "clip_min_notional_usdc": 0.1,
                "base_coverage": 0.4,
                "maximum_coverage": 0.4,
            },
            delta_v2_policy=delta_v2_artifact,
            delta_v2_fill_resolver=resolve_fill,
            cash_phase_policy=_cash_policy(
                "post-fill-cash-v1",
                1.8,
            ),
            cash_phase_input=_cash_input(
                _cash_policy("post-fill-cash-v1", 1.8)
            ),
            policy_authority_scope=AUTHORITY_SCOPE,
            regime_artifact=build_regime_artifact(formal_p1),
            require_regime_ssot=True,
            resource_probe_ok=True,
        )
        initial_candidate["production_inputs"] = dict(
            ssot_candidate["production_inputs"]
        )
        initial_candidate["production_market_snapshot"] = market
        initial_candidate["production_cycle_sha256"] = content_sha256({
            key: value
            for key, value in initial_candidate.items()
            if key != "production_cycle_sha256"
        })
        assert initial_candidate["cadence_allows_candidate"] is True
        worker = object.__new__(TargetProductionWorker)
        worker.conn = conn
        worker.commit_conn = conn
        committed = worker._commit_candidate(
            initial_candidate,
            station_id="ZUUU",
            event_date="2026-07-23",
            created_at_utc="2026-07-23T04:30:01Z",
        )
        assert committed["writes_target_ledger"] is True
        assert committed["candidate_consumption_cas"]["state"] == (
            "EVIDENCE_ONLY_VALIDATED"
        )
        assert committed["candidate_consumption_cas"][
            "authorizes_order"
        ] is False
        assert committed["candidate_consumption_cas"][
            "live_submit_authorized"
        ] is False
        assert committed["authorizes_order"] is False
        assert committed["evidence_only"] is True
        intents = conn.execute(
            "SELECT order_intent_id,bucket,token_id,side,order_action,"
            "limit_price,requested_size,requested_notional_usdc,intent_json "
            "FROM basket_target_order_intents"
        ).fetchall()
        maker_intents = [
            row
            for row in intents
            if json.loads(str(row[8]))["execution_candidate"]
            == "MAKER"
        ]
        # The complete Maker basket starts in one atomic cycle.  A receipt on
        # any leg then cancels/reconciles siblings and recomputes the basket.
        assert len(maker_intents) == len(
            initial_candidate["plan"]["execution_gap_support"]
        )
        assert len(maker_intents) > 1
        intent = maker_intents[0]
        vector = json.loads(
            str(
                conn.execute(
                    "SELECT vector_json FROM "
                    "basket_target_execution_version_vectors "
                    "WHERE order_intent_id=?",
                    (intent[0],),
                ).fetchone()[0]
            )
        )
        quote_sequence = conn.execute(
            "SELECT sequence FROM basket_official_ws_snapshot "
            "WHERE content_sha256=?",
            (vector["official_ws_quote_sha256"],),
        ).fetchone()[0]
        receipt = build_no_live_execution_receipt(
            receipt_type=receipt_type,
            order_intent_id=str(intent[0]),
            scope={
                "station_id": "ZUUU",
                "event_date": "2026-07-23",
                "settlement_timezone": "Asia/Shanghai",
                "bucket": str(intent[1]),
                "token_id": str(intent[2]),
                "side": str(intent[3]),
                "order_action": str(intent[4]),
            },
            version_vector=vector,
            quote_sequence=int(quote_sequence),
            observed_at_utc=RECEIPT_AT,
            requested_size=float(intent[6]),
            matched_size=float(intent[6]) * fill_fraction,
            matched_notional_usdc=float(intent[7]) * fill_fraction,
        )
        transition = apply_execution_receipt(conn, receipt)
        assert transition["state_after"] == expected_transition_state
        assert transition["post_fill_recompute_required"] is True
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents "
            "WHERE cycle_id=? AND order_intent_id<>? AND state='cancelled'",
            (
                committed["receipt"]["cycle_id"],
                intent[0],
            ),
        ).fetchone()[0] == len(intents) - 1
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles"
        ).fetchone()[0] == "settled"
        assert conn.execute(
            "SELECT state FROM "
            "basket_target_post_fill_recompute_requests"
        ).fetchone()[0] == "PENDING"

        post_fill_market = materialize_resident_no_live_market_evidence(
            conn,
            manifest_path=_manifest(
                tmp_path,
                observed_at="2026-07-23T04:30:03Z",
                generation=2,
            ),
            activation_path=activation_path,
            remaining_budget_usdc=30.0,
            policy_authority_scope=AUTHORITY_SCOPE,
            cash_phase_authority_manifest=cash_authority,
        )
        assert post_fill_market["state"] == "COMMITTED"
        projected_inventory = json.loads(
            conn.execute(
                "SELECT inventory_json FROM "
                "basket_confirmed_inventory_snapshot "
                "ORDER BY observed_at DESC,rowid DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert projected_inventory["filled_by_bucket"][
            str(intent[1])
        ] == pytest.approx(float(intent[6]) * fill_fraction)
        assert transition["transition_sha256"] in projected_inventory[
            "transition_content_sha256"
        ]

        recomputed = worker._consume_post_fill_recompute_requests(
            activation={"policy_authority_scope": AUTHORITY_SCOPE},
            scope_config={
                "season_bin": "summer",
                "maturity_bin": "d1",
                "liquidity_bin": "liquid",
                "strategy_version": "target-v1",
                "base_policy": {
                    "clip_max_notional_usdc": 3.0,
                    "base_coverage": 0.4,
                    "maximum_coverage": 0.4,
                },
            },
            as_of_utc=RECOMPUTE_AT,
        )
        assert recomputed["state"] == "COMMITTED"
        assert recomputed["request_count"] == 1
        assert recomputed["applied_count"] == 1
        result = recomputed["results"][0]
        assert result["completion"]["state"] == "APPLIED"
        assert result["candidate"]["atomic_commit"][
            "writes_target_ledger"
        ] is True
        assert result["candidate"]["atomic_commit"][
            "candidate_consumption_cas"
        ]["authorizes_order"] is False
        assert result["candidate"]["production_market_snapshot"][
            "buckets"
        ][int(intent[1]) - 37]["filled_shares"] == pytest.approx(
            float(intent[6]) * fill_fraction
        )
        stored_result = json.loads(
            conn.execute(
                "SELECT result_json FROM "
                "basket_target_post_fill_recompute_requests"
            ).fetchone()[0]
        )
        assert stored_result["post_fill_transition_sha256"] == (
            transition["transition_sha256"]
        )
        assert stored_result["calls_order_gateway"] is False
        assert stored_result["orders_submitted"] == 0

        duplicate_receipt = apply_execution_receipt(conn, receipt)
        assert duplicate_receipt["state"] == "DUPLICATE_RECEIPT"
        replay = worker._consume_post_fill_recompute_requests(
            activation={"policy_authority_scope": AUTHORITY_SCOPE},
            scope_config={
                "season_bin": "summer",
                "maturity_bin": "d1",
                "liquidity_bin": "liquid",
                "strategy_version": "target-v1",
                "base_policy": {
                    "clip_max_notional_usdc": 3.0,
                    "base_coverage": 0.4,
                    "maximum_coverage": 0.4,
                },
            },
            as_of_utc="2026-07-23T04:30:05Z",
        )
        assert replay["state"] == "COMMITTED"
        assert replay["request_count"] == 0
        assert replay["applied_count"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE name IN ('orders','outbox')"
        ).fetchone()[0] == 0
