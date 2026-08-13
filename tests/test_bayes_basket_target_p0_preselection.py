from __future__ import annotations

import copy
import json
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest

from bayes_basket_target import ContractError, content_sha256
from bayes_basket_target.atomic_ledger import (
    commit_preliminary_scope_decision,
)
from bayes_basket_target.p0_preselection import (
    _decision_semantic_projection,
    _market_snapshot,
    commit_p0_preselection,
    latest_visible_p0_station_ids,
)
from bayes_basket_target.ssot import apply_production_ssot_schema


def _value() -> dict[str, object]:
    return {
        "schema": "bayes_basket_target_p0_preselection_v1",
        "preselection_id": "p0-preselection-test",
        "station_id": "ZUUU",
        "event_date": "2026-07-24",
        "checkpoint_id": "p0-test",
        "probability_stage": "P0",
        "decision_at_utc": "2026-07-23T12:00:00Z",
        "quote_observed_at_utc": "2026-07-23T12:00:01Z",
        "quote_manifest_sha256": "a" * 64,
        "policy_activation_id": "candidate-activation",
        "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "compiled_plan": {"plan": {"center_bucket": "38°C", "direction": "UP"}},
        "plan_sha256": "b" * 64,
        "state": "PRELIMINARY_NO_LIVE",
        "final_authority": "P1",
        "capital_authorized": False,
        "intents_written": 0,
        "reservations_written": 0,
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


def test_pre_event_p0_preselection_is_append_only_and_idempotent() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        first = commit_p0_preselection(
            conn,
            _value(),
            created_at_utc="2026-07-23T12:00:02Z",
        )
        assert first["state"] == "COMMITTED"
        replay = {
            **_value(),
            "quote_manifest_sha256": "c" * 64,
        }
        replay_result = commit_p0_preselection(
            conn,
            replay,
            created_at_utc="2026-07-23T12:00:02Z",
        )
        assert replay_result["state"] == "ALREADY_COMMITTED"
        assert replay_result["quote_manifest_sha256"] == "a" * 64
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_p0_preselection_plan"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(
                "UPDATE basket_p0_preselection_plan SET state='PRELIMINARY_NO_LIVE'"
            )


def test_decision_semantic_projection_ignores_transport_derived_ids() -> None:
    compiled = {
        "status": "hold",
        "reason_codes": ["complete_basket_target_not_activated"],
        "cadence_decision": "HOLD",
        "cash_phase_decision": {
            "clock_phase": "STANDARD",
            "next_phase": "STANDARD",
            "policy_version": "cash-v1",
            "reason_code": "UNCHANGED",
            "observed_at_local": "2026-07-29T21:00:00+09:00",
        },
        "resolved_station_policy": {
            "activation_id": "activation-v1",
            "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
            "policy_id": "policy-v1",
            "policy_sha256": "a" * 64,
            "cash_phase_observed_at_utc": "2026-07-29T12:00:00Z",
        },
        "plan": {
            "state": "HOLD",
            "reason_codes": ["complete_basket_target_not_activated"],
            "center_bucket": "37°C",
            "runner_up_bucket": "38°C",
            "center_stable": False,
            "direction": "NEUTRAL",
            "candidate_support": ["36°C", "37°C", "38°C"],
            "final_support": [],
            "selected_buckets": [],
            "observed_at": "2026-07-29T12:00:00Z",
            "quote_version": "quote-a",
            "snapshot_sha256": "b" * 64,
            "plan_id": "plan-a",
            "target_solution": {
                "target_shares_by_bucket": {},
                "target_cost_by_bucket": {},
                "target_value": 0.0,
                "winner_candidate_id": "candidate-a",
            },
            "delta_v2_action_selection": {
                "selected_actions": [],
                "blockers": ["complete_basket_target_not_activated"],
                "hold_selected": True,
                "basket_complete": False,
                "content_sha256": "c" * 64,
            },
            "delta_v2_audit": {
                "delta_basket_score_v2": 0.0,
                "policy_artifact_id": "delta-v2",
                "policy_artifact_sha256": "d" * 64,
                "fill_distribution_artifact_sha256": "e" * 64,
            },
            "scenario_pnl_usdc": {"37°C": 0.0},
            "expected_pnl_usdc": 0.0,
            "profit_floor_usdc": 0.0,
            "cvar_usdc": 0.0,
            "total_target_shares": 0.0,
            "total_delta_notional_usdc": 0.0,
        },
    }
    replay = copy.deepcopy(compiled)
    replay["cash_phase_decision"]["observed_at_local"] = (
        "2026-07-29T21:01:00+09:00"
    )
    replay["resolved_station_policy"]["cash_phase_observed_at_utc"] = (
        "2026-07-29T12:01:00Z"
    )
    replay["plan"]["observed_at"] = "2026-07-29T12:01:00Z"
    replay["plan"]["quote_version"] = "quote-b"
    replay["plan"]["snapshot_sha256"] = "f" * 64
    replay["plan"]["plan_id"] = "plan-b"
    replay["plan"]["target_solution"]["winner_candidate_id"] = (
        "candidate-b"
    )
    replay["plan"]["delta_v2_action_selection"]["content_sha256"] = (
        "1" * 64
    )

    assert _decision_semantic_projection(compiled) == (
        _decision_semantic_projection(replay)
    )


def test_decision_semantic_projection_tracks_basket_lifecycle() -> None:
    base = {
        "status": "hold",
        "reason_codes": [],
        "plan": {
            "state": "HOLD",
            "snapshot_sha256": "a" * 64,
            "candidate_support": ["36°C", "37°C"],
            "final_support": ["36°C", "37°C"],
            "basket_state": "REPLENISH",
            "basket_state_reason": "existing_support_positive_gap",
            "basket_state_transition": {
                "state": "REPLENISH",
                "reason": "existing_support_positive_gap",
                "desired_support": ["36°C", "37°C"],
                "selected_before": ["36°C", "37°C"],
                "added_buckets": [],
                "retired_buckets": [],
                "positive_gap_buckets": ["37°C"],
            },
        },
    }
    changed = copy.deepcopy(base)
    changed["plan"]["basket_state_transition"]["positive_gap_buckets"] = [
        "36°C"
    ]

    assert _decision_semantic_projection(base) != (
        _decision_semantic_projection(changed)
    )


def test_decision_semantic_projection_changes_with_target_inventory() -> None:
    compiled = {
        "status": "ready",
        "cash_phase_decision": {"clock_phase": "STANDARD"},
        "resolved_station_policy": {"activation_id": "activation-v1"},
        "plan": {
            "state": "READY",
            "target_solution": {
                "target_shares_by_bucket": {"37°C": 10.0},
                "target_cost_by_bucket": {"37°C": 2.0},
                "target_value": 1.0,
            },
            "delta_v2_action_selection": {
                "selected_actions": [{"bucket": "37°C", "shares": 10.0}],
            },
            "delta_v2_audit": {"delta_basket_score_v2": 1.0},
        },
    }
    changed = copy.deepcopy(compiled)
    changed["plan"]["target_solution"]["target_shares_by_bucket"] = {
        "37°C": 11.0
    }

    assert _decision_semantic_projection(compiled) != (
        _decision_semantic_projection(changed)
    )


@pytest.mark.parametrize("probability_stage", ["P0", "P0-M"])
def test_pre_event_preliminary_decision_has_zero_execution_side_effects(
    probability_stage: str,
) -> None:
    value = {
        **_value(),
        "preselection_id": f"{probability_stage.lower()}-preliminary-test",
        "checkpoint_id": f"{probability_stage.lower()}-checkpoint",
        "probability_stage": probability_stage,
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        result = commit_p0_preselection(
            conn,
            value,
            created_at_utc="2026-07-23T12:00:02Z",
        )

        assert result["state"] == "COMMITTED"
        decision = conn.execute(
            "SELECT state,final_authority,live_submit_authorized,"
            "calls_order_gateway,orders_submitted,plan_json "
            "FROM basket_p0_preselection_plan WHERE preselection_id=?",
            (value["preselection_id"],),
        ).fetchone()
        assert decision[:5] == (
            "PRELIMINARY_NO_LIVE",
            "P1",
            0,
            0,
            0,
        )
        payload = json.loads(decision[5])
        assert payload["capital_authorized"] is False
        assert payload["intents_written"] == 0
        assert payload["reservations_written"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_capital_reservations"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions"
        ).fetchone()[0] == 0


def test_preliminary_decision_replay_is_zero_write() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        value = _value()
        commit_p0_preselection(
            conn,
            value,
            created_at_utc="2026-07-23T12:00:02Z",
        )
        kwargs = {
            "preselection_id": str(value["preselection_id"]),
            "station_id": str(value["station_id"]),
            "event_date": str(value["event_date"]),
            "settlement_timezone": "America/New_York",
            "checkpoint_id": str(value["checkpoint_id"]),
            "probability_stage": str(value["probability_stage"]),
            "quote_manifest_sha256": str(
                value["quote_manifest_sha256"]
            ),
            "preselection_plan_sha256": str(value["plan_sha256"]),
            "created_at_utc": "2026-07-23T12:00:02Z",
        }

        first = commit_preliminary_scope_decision(conn, **kwargs)
        second = commit_preliminary_scope_decision(conn, **kwargs)

        assert first["state"] == "committed"
        assert second["state"] == "already_committed"
        assert second["idempotent"] is True
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles"
        ).fetchone()[0] == 1


def test_p0_preselection_can_never_authorize_execution() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(ContractError, match="execution_authority_forbidden"):
            commit_p0_preselection(
                conn,
                {**_value(), "live_submit_authorized": True},
                created_at_utc="2026-07-23T12:00:02Z",
            )


def _ws_inputs(*, observed_at: str) -> tuple[object, list[dict], dict, dict]:
    envelope = SimpleNamespace(
        station_id="ZUUU",
        event_date="2026-07-24",
        settlement_timezone="America/New_York",
        probabilities={"38": 1.0},
        bucket_ordinals={"38": 0},
    )
    registry_rows = [{
        "outcome": "38",
        "station_id": "ZUUU",
        "event_date": "2026-07-24",
        "settlement_timezone": "America/New_York",
        "yes_token": "yes-38",
        "registry_json": '{"event_id":"event-zuuu-20260724"}',
    }]
    quotes = {
        "yes-38": {
            "book_observed_at": observed_at,
            "asks": [{"price": 0.4, "size": 10}],
            "bids": [{"price": 0.3, "size": 10}],
        }
    }
    manifest = {
        "schema": "clob_ws_latest_quote_snapshot_v1",
        "state": "connected",
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
        "target_timezone": "America/New_York",
        "as_of_utc": "2026-07-23T12:00:00Z",
        "connection_observed_at": observed_at,
        "quote_storage": "inline_v1",
        "quotes_by_token": quotes,
    }
    manifest["snapshot_sha256"] = content_sha256(manifest)
    return envelope, registry_rows, quotes, manifest


def test_ws_quote_must_not_predate_probability_checkpoint() -> None:
    envelope, registry_rows, quotes, manifest = _ws_inputs(
        observed_at="2026-07-23T12:00:10Z"
    )
    envelope.decision_at_utc = "2026-07-23T12:00:11Z"
    with pytest.raises(
        ContractError,
        match="p0_preselection_quote_predates_probability",
    ):
        _market_snapshot(
            envelope=envelope,
            registry_rows=registry_rows,
            quotes=quotes,
            manifest=manifest,
            evaluation_at_utc="2026-07-23T12:01:00Z",
        )


def test_p0_preselection_derives_subcent_official_book_tick() -> None:
    envelope, registry_rows, quotes, manifest = _ws_inputs(
        observed_at="2026-07-23T12:00:10Z"
    )
    envelope.decision_at_utc = "2026-07-23T12:00:09Z"
    quotes["yes-38"] = {
        "book_observed_at": "2026-07-23T12:00:10Z",
        "asks": [
            {"price": 0.002, "size": 10},
            {"price": 0.003, "size": 20},
        ],
        "bids": [{"price": 0.001, "size": 30}],
    }
    manifest["quotes_by_token"] = quotes
    manifest["snapshot_sha256"] = content_sha256({
        key: value
        for key, value in manifest.items()
        if key != "snapshot_sha256"
    })

    market = _market_snapshot(
        envelope=envelope,
        registry_rows=registry_rows,
        quotes=quotes,
        manifest=manifest,
        evaluation_at_utc="2026-07-23T12:01:00Z",
    )

    bucket = market["buckets"][0]
    assert bucket["price_tick_size"] == 0.001
    # Preselection now proves the grid against every book in the scope, so a
    # bucket whose own levels happen to sit on whole cents still resolves the
    # market's real tick.  The tick is unchanged here; only the provenance
    # label records the wider evidence.
    assert (
        bucket["tick_size_source"]
        == "OFFICIAL_WS_SCOPE_GRID_DERIVED"
    )
    assert bucket["bid_levels"] == [
        {"price": 0.001, "shares": 30.0}
    ]


def test_p0_preselection_uses_no_live_planning_budget_without_authority() -> None:
    envelope, registry_rows, quotes, manifest = _ws_inputs(
        observed_at="2026-07-23T12:00:10Z"
    )
    envelope.decision_at_utc = "2026-07-23T12:00:09Z"

    market = _market_snapshot(
        envelope=envelope,
        registry_rows=registry_rows,
        quotes=quotes,
        manifest=manifest,
        evaluation_at_utc="2026-07-23T12:01:00Z",
        planning_budget_usdc=20.0,
    )

    assert market["remaining_budget_usdc"] == 20.0
    assert market["inventory_version"] == "p0_preselection_q0"
    assert market["order_state_version"] == (
        "p0_preselection_known_zero"
    )


@pytest.mark.parametrize("budget", [-0.01, float("nan"), float("inf")])
def test_p0_preselection_invalid_planning_budget_fails_closed(
    budget: float,
) -> None:
    envelope, registry_rows, quotes, manifest = _ws_inputs(
        observed_at="2026-07-23T12:00:10Z"
    )
    with pytest.raises(
        ContractError,
        match="p0_preselection_planning_budget_invalid",
    ):
        _market_snapshot(
            envelope=envelope,
            registry_rows=registry_rows,
            quotes=quotes,
            manifest=manifest,
            evaluation_at_utc="2026-07-23T12:01:00Z",
            planning_budget_usdc=budget,
        )


def test_p0_economic_quote_identity_ignores_transport_clocks() -> None:
    envelope, registry_rows, quotes, manifest = _ws_inputs(
        observed_at="2026-07-23T12:00:10Z"
    )
    envelope.decision_at_utc = "2026-07-23T12:00:09Z"
    first = _market_snapshot(
        envelope=envelope,
        registry_rows=registry_rows,
        quotes=quotes,
        manifest=manifest,
        evaluation_at_utc="2026-07-23T12:01:00Z",
    )
    heartbeat_quotes = copy.deepcopy(quotes)
    heartbeat_quotes["yes-38"]["_stream_confirmed_at"] = (
        "2026-07-23T12:00:45Z"
    )
    heartbeat_manifest = copy.deepcopy(manifest)
    heartbeat_manifest["connection_observed_at"] = (
        "2026-07-23T12:00:45Z"
    )
    heartbeat_manifest["as_of_utc"] = "2026-07-23T12:00:45Z"
    heartbeat_manifest["capture_window"] = {
        "started_at": "2026-07-23T11:59:00Z",
        "finished_at": "2026-07-23T12:00:45Z",
    }
    heartbeat_manifest["quotes_by_token"] = heartbeat_quotes
    heartbeat_manifest["snapshot_sha256"] = content_sha256({
        key: value
        for key, value in heartbeat_manifest.items()
        if key != "snapshot_sha256"
    })
    second = _market_snapshot(
        envelope=envelope,
        registry_rows=registry_rows,
        quotes=heartbeat_quotes,
        manifest=heartbeat_manifest,
        evaluation_at_utc="2026-07-23T12:01:00Z",
    )

    assert manifest["snapshot_sha256"] != (
        heartbeat_manifest["snapshot_sha256"]
    )
    assert first["economic_quote_sha256"] == (
        second["economic_quote_sha256"]
    )
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["quote_version"] == second["quote_version"]


def test_fresh_stream_confirmation_keeps_unchanged_book_usable() -> None:
    envelope, registry_rows, quotes, manifest = _ws_inputs(
        observed_at="2026-07-23T12:00:10Z"
    )
    envelope.decision_at_utc = "2026-07-23T12:04:00Z"
    for quote in quotes.values():
        quote["_stream_confirmed_at"] = "2026-07-23T12:04:50Z"
    manifest["connection_observed_at"] = "2026-07-23T12:04:50Z"
    manifest["as_of_utc"] = "2026-07-23T12:04:50Z"
    manifest["quotes_by_token"] = quotes
    manifest["snapshot_sha256"] = content_sha256({
        key: value
        for key, value in manifest.items()
        if key != "snapshot_sha256"
    })

    market = _market_snapshot(
        envelope=envelope,
        registry_rows=registry_rows,
        quotes=quotes,
        manifest=manifest,
        evaluation_at_utc="2026-07-23T12:05:00Z",
    )

    assert market["evidence_fresh"] is True
    assert market["quote_observed_at"] == "2026-07-23T12:00:10Z"


def test_stale_stream_confirmation_blocks_even_with_fresh_connection() -> None:
    envelope, registry_rows, quotes, manifest = _ws_inputs(
        observed_at="2026-07-23T12:00:10Z"
    )
    for quote in quotes.values():
        quote["_stream_confirmed_at"] = "2026-07-23T12:00:10Z"
    manifest["connection_observed_at"] = "2026-07-23T12:04:50Z"
    manifest["as_of_utc"] = "2026-07-23T12:04:50Z"
    manifest["quotes_by_token"] = quotes
    manifest["snapshot_sha256"] = content_sha256({
        key: value
        for key, value in manifest.items()
        if key != "snapshot_sha256"
    })

    with pytest.raises(ContractError, match="p0_preselection_quote_stale"):
        _market_snapshot(
            envelope=envelope,
            registry_rows=registry_rows,
            quotes=quotes,
            manifest=manifest,
            evaluation_at_utc="2026-07-23T12:05:00Z",
        )


def test_p0_economic_quote_identity_changes_with_depth_or_tick() -> None:
    envelope, registry_rows, quotes, manifest = _ws_inputs(
        observed_at="2026-07-23T12:00:10Z"
    )
    envelope.decision_at_utc = "2026-07-23T12:00:09Z"
    first = _market_snapshot(
        envelope=envelope,
        registry_rows=registry_rows,
        quotes=quotes,
        manifest=manifest,
        evaluation_at_utc="2026-07-23T12:01:00Z",
    )
    changed_quotes = copy.deepcopy(quotes)
    changed_quotes["yes-38"]["asks"][0]["size"] = 11
    changed_quotes["yes-38"]["tick_size"] = 0.01
    changed_manifest = copy.deepcopy(manifest)
    changed_manifest["quotes_by_token"] = changed_quotes
    changed_manifest["snapshot_sha256"] = content_sha256({
        key: value
        for key, value in changed_manifest.items()
        if key != "snapshot_sha256"
    })
    second = _market_snapshot(
        envelope=envelope,
        registry_rows=registry_rows,
        quotes=changed_quotes,
        manifest=changed_manifest,
        evaluation_at_utc="2026-07-23T12:01:00Z",
    )

    assert first["economic_quote_sha256"] != (
        second["economic_quote_sha256"]
    )
    assert first["quote_version"] != second["quote_version"]


def test_p0_ws_freshness_is_derived_from_manifest_and_evaluation_clock() -> None:
    envelope, registry_rows, quotes, manifest = _ws_inputs(
        observed_at="2026-07-23T12:00:10Z"
    )
    snapshot = _market_snapshot(
        envelope=envelope,
        registry_rows=registry_rows,
        quotes=quotes,
        manifest=manifest,
        evaluation_at_utc="2026-07-23T12:01:00Z",
    )
    assert snapshot["evidence_fresh"] is True
    assert snapshot["observed_at"] == "2026-07-23T12:00:10Z"


@pytest.mark.parametrize(
    ("observed_at", "evaluation_at", "reason"),
    [
        (
            "2026-07-23T11:57:59Z",
            "2026-07-23T12:00:00Z",
            "p0_preselection_manifest_stale",
        ),
        (
            "2026-07-23T12:00:01Z",
            "2026-07-23T12:00:00Z",
            "p0_preselection_manifest_not_strict_asof",
        ),
    ],
)
def test_p0_ws_stale_or_future_evidence_fails_closed(
    observed_at: str,
    evaluation_at: str,
    reason: str,
) -> None:
    envelope, registry_rows, quotes, manifest = _ws_inputs(observed_at=observed_at)
    with pytest.raises(ContractError, match=reason):
        _market_snapshot(
            envelope=envelope,
            registry_rows=registry_rows,
            quotes=quotes,
            manifest=manifest,
            evaluation_at_utc=evaluation_at,
        )


def test_latest_p0_checkpoint_is_resolved_per_station_not_by_global_max() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE basket_probability_envelope("
            "station_id TEXT,event_date TEXT,state TEXT,"
            "decision_at_utc TEXT,fresh_until_utc TEXT)"
        )
        conn.executemany(
            "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?)",
            [
                (
                    "EDDM",
                    "2026-07-27",
                    "COMMITTED",
                    "2026-07-26T09:30:00Z",
                    "2026-07-26T10:30:00Z",
                ),
                (
                    "RKPK",
                    "2026-07-27",
                    "COMMITTED",
                    "2026-07-26T09:40:00Z",
                    "2026-07-26T10:40:00Z",
                ),
            ],
        )
        assert latest_visible_p0_station_ids(
            conn,
            event_date="2026-07-27",
            evaluation_at_utc="2026-07-26T10:00:00Z",
        ) == ["EDDM", "RKPK"]


def test_stale_newest_checkpoint_never_falls_back_to_older_fresh_row() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE basket_probability_envelope("
            "station_id TEXT,event_date TEXT,state TEXT,"
            "decision_at_utc TEXT,fresh_until_utc TEXT)"
        )
        conn.executemany(
            "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?)",
            [
                (
                    "EDDM",
                    "2026-07-27",
                    "COMMITTED",
                    "2026-07-26T09:30:00Z",
                    "2026-07-26T10:30:00Z",
                ),
                (
                    "EDDM",
                    "2026-07-27",
                    "COMMITTED",
                    "2026-07-26T09:50:00Z",
                    "2026-07-26T09:59:00Z",
                ),
            ],
        )
        assert latest_visible_p0_station_ids(
            conn,
            event_date="2026-07-27",
            evaluation_at_utc="2026-07-26T10:00:00Z",
        ) == []


def test_p0_quote_must_be_content_bound_to_verified_manifest() -> None:
    envelope, registry_rows, quotes, manifest = _ws_inputs(
        observed_at="2026-07-23T12:00:10Z"
    )
    altered_quotes = copy.deepcopy(quotes)
    altered_quotes["yes-38"]["asks"][0]["price"] = 0.39
    with pytest.raises(
        ContractError,
        match="p0_preselection_quote_manifest_binding_invalid",
    ):
        _market_snapshot(
            envelope=envelope,
            registry_rows=registry_rows,
            quotes=altered_quotes,
            manifest=manifest,
            evaluation_at_utc="2026-07-23T12:01:00Z",
        )


def test_only_target_worker_owns_p0_append_entrypoint() -> None:
    root = Path(__file__).resolve().parents[1]
    validator = (
        root / "scripts/materialize_bayes_basket_target_p0_preselection.py"
    ).read_text(encoding="utf-8")
    dispatcher = (
        root / "scripts/run_bayes_basket_target_p0_worker_once.py"
    ).read_text(encoding="utf-8")
    worker = (
        root / "bayes_basket_target/production_worker.py"
    ).read_text(encoding="utf-8")
    assert "--commit" not in validator
    assert "commit_p0_preselection" not in validator
    assert "materialize_forward_p0_preselections" in dispatcher
    assert "commit_p0_preselection(" in worker
