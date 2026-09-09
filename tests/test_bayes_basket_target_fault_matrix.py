from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.production import PRODUCTION_EXECUTION_MODE
from bayes_basket_target import ContractError, content_sha256
from bayes_basket_target.canonical_ssot_v12_migration import (
    TARGET_TABLE_DDL as V12_TABLE_DDL,
    TARGET_TRIGGER_DDL as V12_TRIGGER_DDL,
)
from bayes_basket_target.atomic_ledger import (
    InjectedAtomicFault,
    apply_target_ledger_schema,
    build_target_atomic_bundle,
    commit_target_atomic_bundle,
    initialize_capital_pool,
)
from bayes_basket_target.execution_reconciliation import (
    ExecutionCasError,
    VERSION_FIELDS,
    assert_execution_version_cas,
    can_submit_next_order,
    record_order_event,
    resolve_reconciliation,
)
from bayes_basket_target.market_ingress import load_official_ws_quotes
from bayes_basket_target.market_materialization import (
    build_confirmed_inventory_snapshot,
    build_official_ws_snapshot,
    build_order_state_snapshot,
    insert_market_evidence_bundle,
)
from bayes_basket_target.policy import (
    GlobalBasketPriorArtifact,
    PolicyArtifact,
    StationBasketPolicyActivation,
    resolve_station_policy,
)
from bayes_basket_target.production import commit_production_candidate_atomic
from bayes_basket_target.ssot import apply_production_ssot_schema

from tests.pr12_atomic_test_support import awc_contract


NOW = "2026-07-23T05:00:00Z"
SETTLEMENT_TIMEZONE = "America/New_York"
VALID_FROM = "2026-07-01T00:00:00Z"
VALID_UNTIL = "2026-08-01T00:00:00Z"
SHA_A = "a" * 64


def _awc_contract(index: int) -> tuple[dict[str, object], str]:
    return awc_contract(
        station_id="ZUUU",
        observation_id=f"fault-obs-{index}",
        event_date="2026-07-23",
        settlement_timezone=SETTLEMENT_TIMEZONE,
        observed_at_utc="2026-07-23T05:00:00.000Z",
        raw_object_sha256=SHA_A,
    )


def _exact_vector(index: int = 0) -> dict[str, str]:
    return {
        "schema": "bayes_basket_target_execution_version_vector_v2",
        "probability_sha256": "1" * 64,
        "official_ws_quote_sha256": "2" * 64,
        "inventory_sha256": "3" * 64,
        "order_state_sha256": "4" * 64,
        "station_policy_sha256": "5" * 64,
        "cash_policy_decision_sha256": "6" * 64,
        "economic_policy_schema": "delta_basket_score_policy_artifact_v2",
        "economic_policy_artifact_sha256": "7" * 64,
        "target_price_id": f"fault-price-{index}",
        "quote_observed_at": NOW,
        "quote_expires_at": "2026-07-23T05:01:00Z",
    }


def _ledger_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(V12_TABLE_DDL)
    for ddl in V12_TRIGGER_DDL:
        conn.execute(ddl)
    initialize_capital_pool(
        conn, balance_usdc=20.0, capital_epoch_id="fault-matrix-epoch", updated_at_utc=NOW
    )
    return conn


def _bundle(index: int = 0, *, amount: float = 4.0) -> dict[str, object]:
    contract, revision_sha = _awc_contract(index)
    return build_target_atomic_bundle(
        awc_observation_contract=contract,
        truth_observation_revision_content_sha256=revision_sha,
        bound={
            "bound_id": f"fault-bound-{index}",
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "bound_generation": index,
            "hard_lower_bound": 38,
            "bound_json": {"source": "official_metar", "excluded_buckets": ["37"]},
        },
        plan={
            "strategy_lane": "basket",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "probability_checkpoint_id": f"fault-p1-{index}",
            "quote_snapshot_id": f"fault-quote-{index}",
            "lineage": {
                "probability_stage": "P1",
                "settlement_timezone": SETTLEMENT_TIMEZONE,
                "canonical_observation_event_id": f"fault-obs-{index}",
                "truth_observation_revision_content_sha256": revision_sha,
                "canonical_observation_committed_at": "2026-07-23T05:00:00.000Z",
                "hard_bound_id": f"fault-bound-{index}",
                "source_lane": "AWC",
                "report_type": "METAR",
            },
        },
        decision={
            "strategy_lane": "basket",
            "decided_state": "prepared",
            "will_order": False,
            "reason_codes": [],
            # ``production_plan_sha256`` is what makes two indices two
            # decisions.  Since the economic identity ruling (2026-09-02)
            # the decision id is the four economic legs and what was
            # decided -- token ids, price ids and checkpoint ids are audit
            # metadata and are not in it -- so bundles that differed only
            # in those were one decision, and the second commit returned
            # ``already_committed_decision`` with ``intent_count`` 0.  The
            # test that needs two live orders at once then reconciled an
            # intent that had never been written.
            "details": {
                "candidate_will_order": True,
                "no_live": True,
                "production_plan_sha256": f"{index:064x}",
            },
        },
        intents=[{
            "bucket": "38",
            "token_id": f"fault-token-{index}",
            "side": "YES",
            "order_action": "BUY",
            "limit_price": 0.40,
            "requested_size": amount / 0.40,
            "requested_notional_usdc": amount,
            "intent_json": {
                "candidate_will_order": True,
                "no_live": True,
                "target_price_id": f"fault-price-{index}",
                "target_execution_hard_deadline_at": "2026-07-23T05:00:03.000Z",
            },
        }],
        reserved_amount_usdc=amount,
        execution_version_vectors=[_exact_vector(index)],
        created_at_utc=NOW,
    )


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "basket_target_observation_facts",
        "basket_target_bound_facts",
        "basket_target_execution_cycles",
        "auto_trade_decisions",
        "basket_target_order_intents",
        "basket_target_capital_reservations",
    )
    return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


def test_fault_matrix_transaction_points_are_all_or_zero() -> None:
    for point in (
        "canonical_stage", "projection", "bound_projection", "cycle",
        "decision", "intent", "reserve",
    ):
        with _ledger_db() as conn:
            with pytest.raises(InjectedAtomicFault, match=point):
                commit_target_atomic_bundle(conn, bundle=_bundle(), fault_after=point)
            assert all(value == 0 for value in _counts(conn).values())
            assert conn.execute("SELECT reserved_usdc FROM basket_target_capital_pool").fetchone()[0] == 0


def _prior() -> GlobalBasketPriorArtifact:
    return GlobalBasketPriorArtifact.create(
        global_prior_id="fault-prior-v1",
        strategy_version="target-v1",
        family_parameters={"target": {}, "reserve": {"price_margin": 0.0}, "cadence": {}},
        default_policy={"total_budget_usdc": 12.0},
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
    )


def _policy_artifacts() -> list[PolicyArtifact]:
    return [
        PolicyArtifact.create(
            artifact_id=f"fault-policy-{family}",
            family=family,
            station_id="ZUUU",
            stage="P0",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            theta_local={"price_margin": 0.0} if family == "reserve" else {},
            effective_sample_n=10.0,
            kappa=10.0,
            trained_through_utc="2026-07-22T00:00:00Z",
            valid_from_utc=VALID_FROM,
            valid_until_utc=VALID_UNTIL,
        )
        for family in ("target", "reserve", "cadence")
    ]


def _activation() -> StationBasketPolicyActivation:
    return StationBasketPolicyActivation.create(
        activation_id="fault-activation-v1",
        station_id="ZUUU",
        stage="P0",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        global_prior_id="fault-prior-v1",
        target_artifact_id="fault-policy-target",
        reserve_artifact_id="fault-policy-reserve",
        cadence_artifact_id="fault-policy-cadence",
        fallback_families=[],
        generation=1,
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
        activated_at_utc="2026-06-30T00:00:00Z",
    )


def _resolve_policy(*, prior=None, artifacts=None, activation=None, station_id="ZUUU"):
    return resolve_station_policy(
        activations=[activation or _activation()],
        global_priors={"fault-prior-v1": prior or _prior()},
        artifacts={item.artifact_id: item for item in (artifacts or _policy_artifacts())},
        station_id=station_id,
        stage="P0",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        decision_at_utc=NOW,
    )


def test_fault_matrix_artifact_hash_scope_and_validity_fail_closed() -> None:
    prior = _prior()
    raw_prior = prior.to_dict()
    raw_prior["content_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="content_sha256_mismatch"):
        GlobalBasketPriorArtifact.from_mapping(raw_prior)

    policy = _policy_artifacts()[0]
    raw_policy = policy.to_dict()
    raw_policy["content_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="policy_content_sha256_mismatch"):
        PolicyArtifact.from_mapping(raw_policy)

    activation = _activation()
    raw_activation = activation.to_dict()
    raw_activation["content_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="policy_activation_content_sha256_mismatch"):
        StationBasketPolicyActivation.from_mapping(raw_activation)

    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        _resolve_policy(artifacts=[
            PolicyArtifact.create(**{**item.to_dict(), "valid_until_utc": "2026-07-22T00:00:00Z"})
            for item in _policy_artifacts()
        ])
    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        _resolve_policy(station_id="ZBAA")


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _write_manifest(tmp_path: Path) -> Path:
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    quote = {
        "token_id": "yes-38",
        "side": "YES",
        "best_bid": 0.40,
        "best_ask": 0.42,
        "bids": [{"price": 0.40, "size": 10.0}],
        "asks": [{"price": 0.42, "size": 10.0}],
        "book_observed_at": NOW,
        "created_at": NOW,
    }
    shard_core = {
        "schema": "clob_ws_token_quote_snapshot_v1",
        "token_id": "yes-38",
        "quote": quote,
        "shard_index": 0,
        "shard_generation": 1,
        "shard_active": True,
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
    }
    shard = {**shard_core, "snapshot_sha256": hashlib.sha256(_canonical(shard_core)).hexdigest()}
    (shard_dir / "yes.json").write_text(json.dumps(shard), encoding="utf-8")
    root = {
        "schema": "clob_ws_latest_quote_snapshot_v1",
        "state": "connected",
        "active_shards": [0],
        "expected_shards": 1,
        "active_shard_generations": {"0": 1},
        "quote_storage": "sha256_token_shards_v1",
        "quote_shard_dir": str(shard_dir),
        "unmaterialized_tokens": [],
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
    }
    root["snapshot_sha256"] = hashlib.sha256(_canonical(root)).hexdigest()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(root), encoding="utf-8")
    return path


def _rewrite_root(path: Path, **changes: object) -> None:
    root = json.loads(path.read_text(encoding="utf-8"))
    root.update(changes)
    root["snapshot_sha256"] = hashlib.sha256(_canonical({k: v for k, v in root.items() if k != "snapshot_sha256"})).hexdigest()
    path.write_text(json.dumps(root), encoding="utf-8")


def test_fault_matrix_ws_manifest_and_partial_generation_fail_closed(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)
    load_official_ws_quotes(path)

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["state"] = "tampered"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ContractError, match="content_sha_mismatch"):
        load_official_ws_quotes(path)

    _rewrite_root(path, state="disconnected")
    with pytest.raises(ContractError, match="not_connected"):
        load_official_ws_quotes(path)

    _rewrite_root(path, state="connected", active_shard_generations={"0": 2})
    with pytest.raises(ContractError, match="complete_quotes_missing"):
        load_official_ws_quotes(path)

    _rewrite_root(path, active_shard_generations={"0": 1})
    shard_path = tmp_path / "shards" / "yes.json"
    shard = json.loads(shard_path.read_text(encoding="utf-8"))
    shard["snapshot_sha256"] = "0" * 64
    shard_path.write_text(json.dumps(shard), encoding="utf-8")
    with pytest.raises(ContractError, match="complete_quotes_missing"):
        load_official_ws_quotes(path)

def _cash_input() -> dict[str, object]:
    return {
        "policy_version": "fault-cash-v1",
        "station_id": "ZUUU",
        "current_phase": "HEAVY",
        "event_date_local": "2026-07-23",
        "observed_at_utc": NOW,
        "station_timezone": SETTLEMENT_TIMEZONE,
        "checkpoint_id": "fault-cash-checkpoint",
    }


def _market_rows(*, order_version: str = "ord-v1") -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    quote = {
        "snapshot_id": "fault-ws-snapshot",
        "market_id": "fault-market",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "America/New_York",
        "sequence": 1,
        "observed_at": NOW,
        "quote_version": "quote-v1",
        "quote_observed_at": NOW,
        "quote_expires_at": "2026-07-23T05:00:05Z",
        "quote_freshness_policy_sha256": SHA_A,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inv-v1",
        "order_state_version": order_version,
        "remaining_budget_usdc": 20.0,
        "buckets": [{"bucket": "38", "ordinal": 0, "ask_levels": [{"price": 0.42, "shares": 10.0}]}],
        "cash_phase_input": _cash_input(),
    }
    inventory = {
        "inventory_snapshot_id": "fault-inventory",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "inventory_version": "inv-v1",
        "observed_at": NOW,
        "complete": 1,
        "inventory_json": {"filled_by_bucket": {"38": 0.0}},
    }
    order = {
        "order_state_snapshot_id": "fault-order-state",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "order_state_version": order_version,
        "observed_at": NOW,
        "known": 1,
        "order_state_json": {"pending_by_bucket": {"38": 0.0}},
    }
    return quote, inventory, order


def test_fault_matrix_inventory_order_state_and_market_bundle_are_atomic() -> None:
    with pytest.raises(ContractError, match="confirmed_inventory_incomplete"):
        build_confirmed_inventory_snapshot({**_market_rows()[1], "complete": 0})
    with pytest.raises(ContractError, match="order_state_unknown"):
        build_order_state_snapshot({**_market_rows()[2], "known": 0})

    quote, inventory, order = _market_rows()
    order["order_state_version"] = "ord-v2"
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(ContractError, match="order_state_version_mismatch"):
            insert_market_evidence_bundle(conn, quote, inventory, order)
        for table in (
            "basket_official_ws_snapshot",
            "basket_confirmed_inventory_snapshot",
            "basket_order_state_snapshot",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    # Build-time validity is also checked independently of the SQL boundary.
    with pytest.raises(ContractError, match="official_ws_source_required"):
        build_official_ws_snapshot({**quote, "quote_source": "REST"})


def _committed_intent(
    conn: sqlite3.Connection,
    *,
    index: int = 0,
) -> str:
    bundle = _bundle(index)
    commit_target_atomic_bundle(conn, bundle=bundle)
    intent_id = str(bundle["intents"][0]["order_intent_id"])
    return intent_id


def test_fault_matrix_cas_every_bound_field_and_expiry_fail_closed() -> None:
    for field in VERSION_FIELDS:
        with _ledger_db() as conn:
            intent_id = _committed_intent(conn)
            vector = _exact_vector()
            changed = dict(vector)
            if field in {"quote_observed_at", "quote_expires_at"}:
                changed[field] = "2026-07-23T05:00:30Z"
            elif field == "target_price_id":
                changed[field] = "changed"
            else:
                changed[field] = "8" * 64
            expected = (
                "economic_policy_schema_not_delta_v2"
                if field == "economic_policy_schema"
                else "execution_version_drift"
            )
            with pytest.raises(ExecutionCasError, match=expected):
                assert_execution_version_cas(conn, order_intent_id=intent_id, current_vector=changed, at_utc=NOW)

    with _ledger_db() as conn:
        intent_id = _committed_intent(conn)
        vector = _exact_vector()
        with pytest.raises(ExecutionCasError, match="execution_quote_expired"):
            assert_execution_version_cas(
                conn,
                order_intent_id=intent_id,
                current_vector=vector,
                at_utc="2026-07-23T05:01:00Z",
            )


def test_fault_matrix_unknown_partial_and_gateway_boundary_never_authorize() -> None:
    with _ledger_db() as conn:
        unknown_id = _committed_intent(conn, index=1)
        unknown = record_order_event(
            conn,
            order_intent_id=unknown_id,
            event={"event_type": "UNKNOWN_POST", "observed_at": NOW, "exchange_order_id": "ex-unknown"},
        )
        assert unknown["reconciliation_state"] == "unknown"
        assert can_submit_next_order(conn, bucket="38", token_id="fault-token-1")["allowed"] is False

        partial_id = _committed_intent(conn, index=2)
        partial = record_order_event(
            conn,
            order_intent_id=partial_id,
            event={
                "event_type": "PARTIAL_FILL",
                "observed_at": NOW,
                "exchange_order_id": "ex-partial",
                "matched_size": 2.0,
                "matched_notional_usdc": 0.8,
            },
        )
        assert partial["reconciliation_state"] == "pending"
        assert partial["state"] == "reconcile_required"
        resolved = resolve_reconciliation(
            conn,
            order_intent_id=partial_id,
            authoritative_state="partial",
            filled_size=2.0,
            filled_notional_usdc=0.8,
            resolved_at_utc=NOW,
        )
        assert resolved["reconciliation_state"] == "resolved_partial"

    with _ledger_db() as conn:
        with pytest.raises(ContractError, match="production_gateway_boundary_invalid"):
            commit_production_candidate_atomic(
                conn,
                compiled_plan={
                    "execution_mode": PRODUCTION_EXECUTION_MODE,
                    "live_submit_authorized": PRODUCTION_EXECUTION_MODE == "production_live",
                    # The wrong value for whichever mode is compiled in: the
                    # validator demands agreement with the mode, and this test
                    # is that a disagreement is refused before any write.
                    "boundary": {"calls_order_gateway": PRODUCTION_EXECUTION_MODE != "production_live"},
                    "plan": {"station_id": "ZUUU", "event_date": "2026-07-23"},
                },
                market_snapshot={"station_id": "ZUUU", "event_date": "2026-07-23"},
                    awc_observation_contract={},
                    truth_observation_revision_content_sha256="",
                bound={},
                created_at_utc=NOW,
            )
        assert all(value == 0 for value in _counts(conn).values())
