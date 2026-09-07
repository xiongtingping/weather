from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.cash_phase import BasketCashPhasePolicyArtifact
from bayes_basket_target.cash_phase_authority import CashPhaseAuthorityManifest
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.activation import load_target_activation
from bayes_basket_target.resident_market_ingress import (
    _complete_quote_scopes,
    _policy_for_scope,
    materialize_resident_no_live_market_evidence,
)
from bayes_basket_target.ssot import apply_production_ssot_schema


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _manifest(tmp_path: Path) -> Path:
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    quotes = []
    for token, bucket, price in (("yes-38", "38°C", 0.42), ("yes-39", "39°C", 0.31)):
        quote = {
            "token_id": token,
            "event_date": "2026-07-23",
            "outcome": bucket,
            "side": "YES",
            "best_bid": price - 0.02,
            "best_ask": price,
            "bids": [{"price": price - 0.02, "size": 10.0}],
            "asks": [{"price": price, "size": 10.0}],
            "book_observed_at": "2026-07-23T04:00:00Z",
            "created_at": "2026-07-23T04:00:00Z",
        }
        core = {
            "schema": "clob_ws_token_quote_snapshot_v1",
            "token_id": token,
            "quote": quote,
            "shard_index": 0,
            "shard_generation": 1,
            "shard_active": True,
            "source": "official_polymarket_clob_websocket",
            "evidence_only": True,
            "authorizes_order": False,
        }
        shard = {**core, "snapshot_sha256": hashlib.sha256(_canonical(core)).hexdigest()}
        (shard_dir / f"{token}.json").write_text(json.dumps(shard), encoding="utf-8")
        quotes.append(token)
    root = {
        "schema": "clob_ws_latest_quote_snapshot_v1",
        "state": "connected",
        "active_shards": [0],
        "expected_shards": 1,
        "active_shard_generations": {"0": 1},
        "quote_storage": "sha256_token_shards_v1",
        "quote_shard_dir": str(shard_dir),
        "quotes_by_token": {},
        "materialized_token_count": len(quotes),
        "unmaterialized_tokens": [],
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
    }
    root["snapshot_sha256"] = hashlib.sha256(_canonical(root)).hexdigest()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(root), encoding="utf-8")
    return path


def _activation(
    tmp_path: Path,
    *,
    authority_scope: str = "PRODUCTION_NO_LIVE_CANDIDATE",
) -> Path:
    # The activation in force, not the file this fixture was written
    # against.  The lane's name is historical: production passes
    # self.activation_path straight into it, and since the 2026-08-30
    # go-live DEFAULT_TARGET_ACTIVATION points at the live contract.  A
    # fixture pinned to the no-live file made order_state_producer refuse
    # the row it later reads -- first as target_activation_state_mode_drift
    # and then, once only the database row was moved, as
    # target_activation_state_drift, because that guard requires the row
    # and the lane to carry the same contract.  Deriving it keeps them one.
    from bayes_basket_target.production_worker import (  # noqa: PLC0415
        DEFAULT_TARGET_ACTIVATION,
    )

    source = Path(DEFAULT_TARGET_ACTIVATION)
    value = json.loads(source.read_text(encoding="utf-8"))
    value["policy_authority_scope"] = authority_scope
    quote_policy = dict(value["quote_freshness_policy"])
    quote_policy["authority_scope"] = authority_scope
    quote_policy["evidence_status"] = (
        "PROMOTED" if authority_scope == "FORMAL_PROMOTED"
        else "DESIGN_CANDIDATE"
    )
    quote_policy.pop("content_sha256", None)
    quote_policy["content_sha256"] = hashlib.sha256(
        _canonical(quote_policy)
    ).hexdigest()
    value["quote_freshness_policy"] = quote_policy
    value.pop("activation_contract_sha256", None)
    value["activation_contract_sha256"] = hashlib.sha256(
        _canonical(value)
    ).hexdigest()
    path = tmp_path / "activation.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _cash_authority(
    policy: BasketCashPhasePolicyArtifact,
    *,
    authority_scope: str,
) -> CashPhaseAuthorityManifest:
    return CashPhaseAuthorityManifest.create(
        station_id=policy.station_id,
        policy_version=policy.policy_version,
        artifact_sha256=policy.content_sha256,
        station_timezone=policy.station_timezone,
        valid_from_utc=policy.valid_from_utc,
        valid_until_utc=policy.valid_until_utc,
        authority_scope=authority_scope,
    )


def test_committed_activation_routes_target_worker_to_weather_basket_yes() -> None:
    activation = load_target_activation(
        Path(__file__).parents[1]
        / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
    )
    assert activation["next_consumers"] == [
        "weather_basket_yes_internal_target_worker"
    ]


def test_resident_no_live_market_ingress_materializes_manifest_and_q0(tmp_path: Path) -> None:
    policy = BasketCashPhasePolicyArtifact.create(
        policy_version="target-cash-test-v1",
        station_id="TEST",
        station_timezone="UTC",
        heavy_to_standard_local_time="19:54",
        standard_to_tail_local_time="09:58",
        cash_by_phase={"HEAVY": 5.0, "STANDARD": 3.0, "TAIL": 1.0},
        valid_from_utc="2026-01-01T00:00:00Z",
        valid_until_utc="2027-01-01T00:00:00Z",
        evidence_status="DESIGN_CANDIDATE",
        promoted=False,
    )
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        conn.execute(
            "CREATE TABLE reported_trajectory_market_registry("
            "slug TEXT,event_date TEXT,station_id TEXT,settlement_timezone TEXT,"
            "outcome TEXT,yes_token TEXT,registry_json TEXT,source_eligible INTEGER,"
            "market_active INTEGER,market_accepting_orders INTEGER)"
        )
        conn.executemany(
            "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                ("test", "2026-07-23", "TEST", "UTC", "38°C", "yes-38", json.dumps({"lower": 38, "upper": 38, "order_min_size": 5, "order_price_min_tick_size": 0.01}), 1, 1, 1),
                ("test", "2026-07-23", "TEST", "UTC", "39°C", "yes-39", json.dumps({"lower": 39, "upper": 39, "order_min_size": 5, "order_price_min_tick_size": 0.01}), 1, 1, 1),
            ],
        )
        conn.execute(
            "INSERT INTO basket_cash_phase_policy_artifact VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                policy.policy_version, policy.station_id, policy.station_timezone,
                policy.heavy_to_standard_local_time, policy.standard_to_tail_local_time,
                json.dumps(policy.cash_by_phase), policy.valid_from_utc, policy.valid_until_utc,
                policy.evidence_status, 0, policy.content_sha256,
            ),
        )
        conn.execute(
            "CREATE TABLE basket_strategy_activation_state(singleton INTEGER PRIMARY KEY,"
            "generation INTEGER,state TEXT,strategy_id TEXT,activation_sha256 TEXT,"
            "activation_json TEXT,updated_at TEXT)"
        )
        activation_path = _activation(
            tmp_path,
            authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
        )
        activation = json.loads(activation_path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO basket_strategy_activation_state VALUES(1,?,?,?,?,?,?)",
            (1, "enabled", activation["strategy_id"], activation["activation_contract_sha256"], json.dumps(activation), "2026-07-23T00:00:00Z"),
        )
        conn.execute("CREATE TABLE basket_strategy_cycles(state TEXT)")
        conn.execute("CREATE TABLE basket_strategy_parent_orders(state TEXT)")
        conn.execute("CREATE TABLE basket_strategy_child_orders(state TEXT)")

        result = materialize_resident_no_live_market_evidence(
            conn,
            manifest_path=_manifest(tmp_path),
            activation_path=activation_path,
            remaining_budget_usdc=30.0,
            policy_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
            cash_phase_authority_manifest=_cash_authority(
                policy,
                authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
            ),
        )
        assert result["state"] == "COMMITTED"
        assert result["evidence_cache"]["evidence_only"] is True
        assert result["evidence_cache"]["authorizes_order"] is False
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_evidence_cache").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_snapshot").fetchone()[0] == 1
        inventory = json.loads(conn.execute("SELECT inventory_json FROM basket_confirmed_inventory_snapshot").fetchone()[0])
        assert inventory["filled_by_bucket"] == {"38°C": 0.0, "39°C": 0.0}
        assert inventory["production_no_live"] is True


def test_resident_missing_promoted_policy_keeps_only_evidence_cache(tmp_path: Path) -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        conn.execute(
            "CREATE TABLE reported_trajectory_market_registry("
            "slug TEXT,event_date TEXT,station_id TEXT,settlement_timezone TEXT,"
            "outcome TEXT,yes_token TEXT,registry_json TEXT,source_eligible INTEGER,"
            "market_active INTEGER,market_accepting_orders INTEGER)"
        )
        conn.executemany(
            "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                ("test", "2026-07-23", "TEST", "UTC", "38°C", "yes-38", json.dumps({"lower": 38, "upper": 38, "order_min_size": 5, "order_price_min_tick_size": 0.01}), 1, 1, 1),
                ("test", "2026-07-23", "TEST", "UTC", "39°C", "yes-39", json.dumps({"lower": 39, "upper": 39, "order_min_size": 5, "order_price_min_tick_size": 0.01}), 1, 1, 1),
            ],
        )
        conn.execute(
            "CREATE TABLE basket_strategy_activation_state(singleton INTEGER PRIMARY KEY,"
            "generation INTEGER,state TEXT,strategy_id TEXT,activation_sha256 TEXT,"
            "activation_json TEXT,updated_at TEXT)"
        )
        activation_path = _activation(
            tmp_path,
            authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
        )
        activation = json.loads(activation_path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO basket_strategy_activation_state VALUES(1,?,?,?,?,?,?)",
            (1, "enabled", activation["strategy_id"], activation["activation_contract_sha256"], json.dumps(activation), "2026-07-23T00:00:00Z"),
        )
        conn.execute("CREATE TABLE basket_strategy_cycles(state TEXT)")
        conn.execute("CREATE TABLE basket_strategy_parent_orders(state TEXT)")
        conn.execute("CREATE TABLE basket_strategy_child_orders(state TEXT)")
        missing = BasketCashPhasePolicyArtifact.create(
            policy_version="missing-policy-v1",
            station_id="TEST",
            station_timezone="UTC",
            heavy_to_standard_local_time="19:54",
            standard_to_tail_local_time="09:58",
            cash_by_phase={"HEAVY": 5.0, "STANDARD": 3.0, "TAIL": 1.0},
            valid_from_utc="2026-01-01T00:00:00Z",
            valid_until_utc="2027-01-01T00:00:00Z",
            evidence_status="DESIGN_CANDIDATE",
            promoted=False,
        )
        with pytest.raises(
            ContractError,
            match="resident_cash_phase_authority_artifact_mismatch",
        ):
            materialize_resident_no_live_market_evidence(
                conn,
                manifest_path=_manifest(tmp_path),
                activation_path=activation_path,
                remaining_budget_usdc=30.0,
                policy_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
                cash_phase_authority_manifest=_cash_authority(
                    missing,
                    authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
                ),
            )
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_evidence_cache").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_snapshot").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_confirmed_inventory_snapshot").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_order_state_snapshot").fetchone()[0] == 0


def test_no_live_candidate_policy_uses_exact_manifest_not_highest_generation() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        policies = {}
        for generation in (1, 2):
            policy = BasketCashPhasePolicyArtifact.create(
                policy_version=f"target-no-live-cash-design-v{generation}-TEST",
                station_id="TEST",
                station_timezone="UTC",
                heavy_to_standard_local_time="12:00",
                standard_to_tail_local_time="12:00",
                cash_by_phase={"HEAVY": 3.0, "STANDARD": 2.0, "TAIL": 1.0},
                valid_from_utc="2026-01-01T00:00:00Z",
                valid_until_utc="2027-01-01T00:00:00Z",
                evidence_status="DESIGN_CANDIDATE",
                promoted=False,
            )
            conn.execute(
                "INSERT INTO basket_cash_phase_policy_artifact VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
            policies[generation] = policy

        selected = _policy_for_scope(
            conn,
            station_id="TEST",
            observed_at_utc="2026-07-26T13:00:00Z",
            policy_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
            cash_phase_authority_manifest=_cash_authority(
                policies[1],
                authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
            ),
        )

    assert selected.policy_version == "target-no-live-cash-design-v1-TEST"


def test_missing_one_ws_leg_holds_entire_scope_not_partial_bundle() -> None:
    scopes = {
        ("RKPK", "2026-07-30"): [
            {"bucket": "36°C", "token_id": "yes-36"},
            {"bucket": "37°C", "token_id": "yes-37"},
            {"bucket": "38°C", "token_id": "yes-38"},
        ]
    }
    complete, skipped = _complete_quote_scopes(
        scopes,
        {
            "yes-36": {"best_bid": 0.08, "best_ask": 0.09},
            "yes-38": {"best_bid": 0.38, "best_ask": 0.39},
        },
    )

    assert complete == {}
    assert skipped == {"RKPK|2026-07-30": ["yes-37"]}
