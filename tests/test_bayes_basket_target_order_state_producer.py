from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.activation import LIVE_AUTHORITY_SCOPE, REQUIRED_INPUT_KEYS
from bayes_basket_target.database import TARGET_ACTIVATION_DDL
from bayes_basket_target.market_registry import apply_target_market_registry_schema
from bayes_basket_target.order_state_producer import build_no_live_order_state_ingress
from bayes_basket_target.ssot import apply_production_ssot_schema


HASH = "a" * 64


def _db(*, active_cycle: bool = False, active_cycle_state: str = "submitting") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    apply_target_market_registry_schema(conn)
    conn.executescript(TARGET_ACTIVATION_DDL)
    snapshot = "b" * 64
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot "
        "(snapshot_sha256,source_schema,source_scope,exported_at_utc,source_database_path,row_count,ingested_at_utc,content_sha256) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (snapshot, "reported_trajectory_market_registry_v1", "shared_live_registry_export_only",
         "2026-07-23T00:00:00Z", "/isolated/source.db", 2, "2026-07-23T00:00:00Z", snapshot),
    )
    conn.executemany(
        "INSERT INTO basket_target_market_registry "
        "(snapshot_sha256,slug,event_date,outcome,city_slug,station_id,settlement_timezone,settlement_unit,"
        "source_lane,source_eligible,source_block_reason,resolution_url,rules_sha256,yes_token,no_token,"
        "market_active,market_accepting_orders,registry_json,refreshed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (snapshot, "test", "2026-07-23", "38°C", "test", "TEST", "America/New_York", "C",
             "amsc", 1, "", "", HASH, "yes-38", "no-38", 1, 1, '{"market_id":"m-test"}', "2026-07-23T00:00:00Z"),
            (snapshot, "test", "2026-07-23", "39°C", "test", "TEST", "America/New_York", "C",
             "amsc", 1, "", "", HASH, "yes-39", "no-39", 1, 1, '{"market_id":"m-test"}', "2026-07-23T00:00:00Z"),
        ],
    )
    if active_cycle:
        conn.execute(
            "INSERT INTO basket_target_execution_cycles "
            "(cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,plan_sha256,requested_reserve_usdc,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("cycle-1", "plan-1", "TEST", "2026-07-23", "America/New_York", "basket", HASH, 0.0, active_cycle_state,
             "2026-07-23T00:00:00Z", "2026-07-23T00:00:00Z"),
        )
    return conn


# Both contracts, from one builder.  These tests were written when no-live
# was the only mode and asserted its shape as a constant; after the go-live
# flip they were asserting the mode the deployment had left, and the guard
# they cover -- which spelled "production_no_live" as a literal in two places
# -- had no live coverage at all.  That is how it survived the migration.
def _activation(tmp_path, *, mode: str = "production_no_live"):
    if mode == "production_live":
        return _live_activation(tmp_path)
    path = tmp_path / "activation.json"
    quote_policy = {
        "schema": "bayes_basket_quote_freshness_policy_v1",
        "policy_id": "test-quote-freshness-v1",
        "max_quote_age_seconds": 5.0,
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "evidence_status": "DESIGN_CANDIDATE",
    }
    quote_policy["content_sha256"] = content_sha256(quote_policy)
    value = {
        "schema": "bayes_basket_target_production_no_live_activation_v1",
        "state": "production_no_live",
        "execution_mode": "production_no_live",
        "strategy_id": "bayes_basket_target_v1",
        "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "quote_freshness_policy": quote_policy,
        "target_only": True,
        "authorizes_order": False,
        "live_decision_write_authorized": False,
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "production_mutation": False,
        "places_orders": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "network_access": False,
        "required_inputs": sorted(REQUIRED_INPUT_KEYS),
        "target_scope": {
            "season_bin": "ALL",
            "maturity_bin": "ALL",
            "liquidity_bin": "ALL",
            "strategy_version": "bayes_basket_target_v1",
        },
    }
    value["activation_contract_sha256"] = content_sha256(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, value


def _live_activation(tmp_path):
    """The live contract, shaped to satisfy ``validate_live_activation``."""

    path = tmp_path / "activation.json"
    quote_policy = {
        "schema": "bayes_basket_quote_freshness_policy_v1",
        "policy_id": "test-quote-freshness-v1",
        "max_quote_age_seconds": 5.0,
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "authority_scope": LIVE_AUTHORITY_SCOPE,
        "evidence_status": "DESIGN_CANDIDATE",
    }
    quote_policy["content_sha256"] = content_sha256(quote_policy)
    value = {
        "schema": "bayes_basket_target_production_live_activation_v1",
        "state": "production_live",
        "execution_mode": "production_live",
        "strategy_id": "bayes_basket_target_v1",
        "policy_authority_scope": LIVE_AUTHORITY_SCOPE,
        "quote_freshness_policy": quote_policy,
        "target_only": True,
        "authorizes_order": True,
        "live_decision_write_authorized": True,
        "live_order_authorized": True,
        "live_submit_authorized": True,
        "production_mutation": False,
        "places_orders": True,
        "writes_production_db": False,
        "calls_order_gateway": True,
        "network_access": True,
        "required_inputs": sorted(REQUIRED_INPUT_KEYS),
        "target_scope": {
            "season_bin": "ALL",
            "maturity_bin": "ALL",
            "liquidity_bin": "ALL",
            "strategy_version": "bayes_basket_target_v1",
        },
    }
    value["activation_contract_sha256"] = content_sha256(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, value


@pytest.fixture(params=["production_no_live", "production_live"])
def execution_mode(request, monkeypatch):
    """Run the mode-sensitive guards under both deployed modes.

    The guard reads the constant from ``bayes_basket_target.production`` at
    call time, so patching it here is the whole switch.
    """

    import bayes_basket_target.production as production_module

    monkeypatch.setattr(
        production_module, "PRODUCTION_EXECUTION_MODE", request.param
    )
    return request.param


def test_no_live_order_state_requires_activation_and_quiescence(tmp_path, execution_mode) -> None:
    path, activation = _activation(tmp_path, mode=execution_mode)
    with _db() as conn:
        conn.execute(
            "INSERT INTO basket_strategy_activation_state "
            "(singleton,generation,state,strategy_id,activation_sha256,activation_json,updated_at) VALUES(?,?,?,?,?,?,?)",
            (1, 1, "enabled", "bayes_basket_target_v1", activation["activation_contract_sha256"],
             json.dumps(activation), "2026-07-23T00:00:00Z"),
        )
        packet = build_no_live_order_state_ingress(
            conn,
            activation_path=path,
            observed_at_utc="2026-07-23T04:00:00Z",
        )
    market = packet["markets"][0]
    assert packet["schema"] == "bayes_basket_order_state_ingress_v1"
    assert market["order_state_json"]["pending_by_bucket"] == {"38°C": 0.0, "39°C": 0.0}
    assert market["known"] == 1


def test_no_live_order_state_rejects_active_target_cycle(tmp_path, execution_mode) -> None:
    path, activation = _activation(tmp_path, mode=execution_mode)
    with _db(active_cycle=True) as conn:
        conn.execute(
            "INSERT INTO basket_strategy_activation_state "
            "(singleton,generation,state,strategy_id,activation_sha256,activation_json,updated_at) VALUES(?,?,?,?,?,?,?)",
            (1, 1, "enabled", "bayes_basket_target_v1", activation["activation_contract_sha256"],
             json.dumps(activation), "2026-07-23T00:00:00Z"),
        )
        with pytest.raises(ContractError, match="not_quiescent"):
            build_no_live_order_state_ingress(
                conn,
                activation_path=path,
                observed_at_utc="2026-07-23T04:00:00Z",
            )


def test_no_live_order_state_rejects_activation_drift(tmp_path) -> None:
    path, activation = _activation(tmp_path)
    with _db() as conn:
        conn.execute(
            "INSERT INTO basket_strategy_activation_state "
            "(singleton,generation,state,strategy_id,activation_sha256,activation_json,updated_at) VALUES(?,?,?,?,?,?,?)",
            (1, 1, "enabled", "bayes_basket_target_v1", "b" * 64,
             json.dumps(activation), "2026-07-23T00:00:00Z"),
        )
        with pytest.raises(ContractError, match="activation_state_drift"):
            build_no_live_order_state_ingress(
                conn,
                activation_path=path,
                observed_at_utc="2026-07-23T04:00:00Z",
            )


def test_no_live_order_state_rejects_shared_live_owner(tmp_path) -> None:
    path, _activation_payload = _activation(tmp_path)
    with _db() as conn:
        conn.execute(
            "INSERT INTO basket_strategy_activation_state "
            "(singleton,state,activation_sha256,activation_json,strategy_id,generation,updated_at) "
            "VALUES(1,'enabled',?,?,?,7,'2026-07-23T00:00:00Z')",
            (
                "c" * 64,
                json.dumps({
                    "execution_mode": "production_live",
                    "live_submit_authorized": True,
                    "places_orders": True,
                    "calls_order_gateway": True,
                }),
                "weather_basket_plus_hard_no_v2",
            ),
        )
        with pytest.raises(ContractError, match="shared_live_activation_owner_conflict"):
            build_no_live_order_state_ingress(
                conn,
                activation_path=path,
                observed_at_utc="2026-07-23T04:00:00Z",
            )


def test_no_live_order_state_allows_prepared_no_live_candidate(tmp_path, execution_mode) -> None:
    path, activation = _activation(tmp_path, mode=execution_mode)
    with _db(active_cycle=True, active_cycle_state="prepared") as conn:
        conn.execute(
            "INSERT INTO basket_strategy_activation_state "
            "(singleton,generation,state,strategy_id,activation_sha256,activation_json,updated_at) VALUES(?,?,?,?,?,?,?)",
            (1, 1, "enabled", "bayes_basket_target_v1", activation["activation_contract_sha256"],
             json.dumps(activation), "2026-07-23T00:00:00Z"),
        )
        # A retired shared-line table must be ignored even if it contains an
        # active row.  The target producer owns only target SSOT tables.
        conn.execute("CREATE TABLE basket_strategy_cycles(state TEXT)")
        conn.execute("INSERT INTO basket_strategy_cycles VALUES('submitting')")
        packet = build_no_live_order_state_ingress(
            conn,
            activation_path=path,
            observed_at_utc="2026-07-23T04:00:00Z",
        )
    assert packet["known"] is True


def test_no_live_order_state_rejects_nonterminal_target_reconciliation(tmp_path, execution_mode) -> None:
    path, activation = _activation(tmp_path, mode=execution_mode)
    with _db() as conn:
        conn.execute(
            "INSERT INTO basket_strategy_activation_state "
            "(singleton,generation,state,strategy_id,activation_sha256,activation_json,updated_at) VALUES(?,?,?,?,?,?,?)",
            (1, 1, "enabled", "bayes_basket_target_v1", activation["activation_contract_sha256"],
             json.dumps(activation), "2026-07-23T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO basket_target_order_intents "
            "(order_intent_id,decision_id,cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
            "requested_notional_usdc,state,live_submit_authorized,idempotency_key,intent_json,intent_sha256,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("intent-1", "decision-1", "cycle-1", "38°C", "yes-38", "YES", "BUY", 0.4, 1.0, 0.4,
             "submitted", 0, "idem-1", "{}", HASH, "2026-07-23T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations "
            "(order_intent_id,exchange_order_id,state,filled_size,filled_notional_usdc,last_event_id,blocking_reason,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("intent-1", "exchange-1", "unknown", 0.0, 0.0, None, "unknown_post", "2026-07-23T00:00:00Z"),
        )
        with pytest.raises(ContractError, match="not_quiescent"):
            build_no_live_order_state_ingress(
                conn,
                activation_path=path,
                observed_at_utc="2026-07-23T04:00:00Z",
            )


# The reconciliation ledger's ``resolved_partial`` is written by two paths
# that mean opposite things, and the quiescence guard used to refuse both.
# These two tests are the distinction, so a future reader restoring the
# state to the blocking set has to say which of them they mean to break.
def _seed_partial(conn, activation, *, intent_state: str, blocking_reason: str | None) -> None:
    conn.execute(
        "INSERT INTO basket_strategy_activation_state "
        "(singleton,generation,state,strategy_id,activation_sha256,activation_json,updated_at) VALUES(?,?,?,?,?,?,?)",
        (1, 1, "enabled", "bayes_basket_target_v1", activation["activation_contract_sha256"],
         json.dumps(activation), "2026-07-23T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO basket_target_order_intents "
        "(order_intent_id,decision_id,cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
        "requested_notional_usdc,state,live_submit_authorized,idempotency_key,intent_json,intent_sha256,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("intent-1", "decision-1", "cycle-1", "38°C", "yes-38", "YES", "BUY", 0.4, 5.0, 2.0,
         intent_state, 0, "idem-1", "{}", HASH, "2026-07-23T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO basket_target_order_reconciliations "
        "(order_intent_id,exchange_order_id,state,filled_size,filled_notional_usdc,last_event_id,blocking_reason,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("intent-1", "exchange-1", "resolved_partial", 2.0, 0.8, None, blocking_reason,
         "2026-07-23T00:00:00Z"),
    )


def test_a_rung_cancelled_after_a_partial_fill_leaves_its_scope_quiescent(
    tmp_path, execution_mode
) -> None:
    """Charter 94's ordinary outcome is a finished order, not a pending one.

    A rung that filled two of its five shares and was then cancelled holds
    nothing at the gateway: ``resolve_cancelled_after_partial_fill`` ends
    the intent on ``cancelled`` precisely so the bucket is freed.  Counting
    it as pending cost the station-day its book for the rest of the event
    day -- the evidence pass skipped it, the WS snapshot stopped renewing,
    and every scope compiled against it died on ``production_quote_expired``
    (six station-days, 2026-09-09 04:24Z).
    """

    path, activation = _activation(tmp_path, mode=execution_mode)
    with _db(active_cycle=True, active_cycle_state="settled") as conn:
        _seed_partial(conn, activation, intent_state="cancelled", blocking_reason=None)
        packet = build_no_live_order_state_ingress(
            conn,
            activation_path=path,
            observed_at_utc="2026-07-23T04:00:00Z",
        )
    assert packet["known"] is True


def test_an_authoritative_partial_still_refuses_through_its_intent(
    tmp_path, execution_mode
) -> None:
    """The other writer of the same state still blocks, one probe over.

    ``resolve_authoritative_state("partial")`` is the exchange disagreeing
    about an order we still hold: it leaves the intent on
    ``reconcile_required`` and asks for a manual close.  Dropping
    ``resolved_partial`` from the reconciliation set must not free that
    scope -- the intent probe is what refuses it.
    """

    path, activation = _activation(tmp_path, mode=execution_mode)
    with _db(active_cycle=True, active_cycle_state="settled") as conn:
        _seed_partial(
            conn,
            activation,
            intent_state="reconcile_required",
            blocking_reason="authoritative_partial_requires_manual_close",
        )
        with pytest.raises(ContractError, match="order_intents_not_quiescent"):
            build_no_live_order_state_ingress(
                conn,
                activation_path=path,
                observed_at_utc="2026-07-23T04:00:00Z",
            )
