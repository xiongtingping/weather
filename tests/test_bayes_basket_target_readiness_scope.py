from __future__ import annotations

import sqlite3

import pytest

import bayes_basket_target.readiness_scope as readiness_scope
from bayes_basket_target.contracts import ContractError


SHA = "a" * 64
POLICY_SCOPE = {
    "season_bin": "summer",
    "maturity_bin": "d0",
    "liquidity_bin": "liquid",
    "strategy_version": "target-v1",
}


def _registry(station_id: str, event_date: str, timezone: str = "UTC"):
    return {
        "snapshot_sha256": SHA,
        "slug": f"{station_id.lower()}-{event_date}",
        "event_date": event_date,
        "outcome": "38°C",
        "station_id": station_id,
        "settlement_timezone": timezone,
        "yes_token": f"yes-{station_id}-{event_date}",
        "no_token": f"no-{station_id}-{event_date}",
        "rules_sha256": SHA,
        "market_active": 1,
        "market_accepting_orders": 1,
    }


def _compiled(station_id: str, event_date: str, timezone: str = "UTC"):
    return {
        "status": "ready_no_live",
        "reason_codes": [],
        "probability_envelope": {
            "stage": "P1",
            "checkpoint_id": f"p1-{station_id}-{event_date}",
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": timezone,
            "content_sha256": "1" * 64,
            "posterior_sha256": "2" * 64,
            "bridge_sha256": "3" * 64,
            "lineage": {"ensemble_artifact_id": "ensemble-1"},
        },
        "production_market_snapshot": {
            "snapshot_id": f"ws-{station_id}-{event_date}",
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": timezone,
            "official_ws_quote_sha256": "4" * 64,
            "quote_freshness_policy_sha256": "5" * 64,
            "inventory_sha256": "6" * 64,
            "order_state_sha256": "7" * 64,
            "quote_observed_at": "2026-07-27T00:00:00Z",
            "quote_expires_at": "2026-07-27T00:00:05Z",
        },
        "plan": {
            "lineage": {
                "policy_resolution_sha256": "8" * 64,
            },
        },
        "resolved_station_policy": {
            "activation_id": "activation-1",
            "policy_sha256": "8" * 64,
        },
        "production_inputs": {
            "probability_bridge_history_id": (
                f"xsb-{station_id}-{event_date}"
            ),
            "probability_bridge_projection_sha256": "d" * 64,
            "cash_phase_policy_sha256": "9" * 64,
            "cash_phase_authority_manifest": {
                "manifest_sha256": "c" * 64,
            },
            "strict_delta_artifact_sha256": "b" * 64,
        },
    }


def test_every_registry_scope_must_resolve_exactly(monkeypatch) -> None:
    rows = [
        _registry("AAAA", "2026-07-27"),
        _registry("BBBB", "2026-07-27"),
    ]
    monkeypatch.setattr(
        readiness_scope, "load_started_registry_rows", lambda *a, **k: rows
    )

    def compile_scope(_conn, *, station_id, event_date, **_kwargs):
        if station_id == "BBBB":
            raise ContractError("materialized_probability_envelope_missing")
        return _compiled(station_id, event_date)

    monkeypatch.setattr(
        readiness_scope,
        "compile_production_cycle_from_ssot",
        compile_scope,
    )
    with sqlite3.connect(":memory:") as conn:
        result = readiness_scope.resolve_target_readiness_scopes(
            conn,
            decision_at_utc="2026-07-27T00:00:01Z",
            target_scope=POLICY_SCOPE,
            authority_scope="FORMAL_PROMOTED",
        )
    assert result["ready"] is False
    assert result["scope_count"] == 2
    assert result["exact_scope_count"] == 1
    assert [item["status"] for item in result["items"]] == [
        "EXACTLY_ONE",
        "MISSING",
    ]


def test_irrelevant_good_scope_cannot_satisfy_allowlisted_target(
    monkeypatch,
) -> None:
    rows = [
        _registry("AAAA", "2026-07-27"),
        _registry("BBBB", "2026-07-28"),
    ]
    monkeypatch.setattr(
        readiness_scope, "load_started_registry_rows", lambda *a, **k: rows
    )

    def compile_scope(_conn, *, station_id, event_date, **_kwargs):
        if station_id == "AAAA":
            raise ContractError("basket_probability_envelope_missing")
        return _compiled(station_id, event_date)

    monkeypatch.setattr(
        readiness_scope,
        "compile_production_cycle_from_ssot",
        compile_scope,
    )
    with sqlite3.connect(":memory:") as conn:
        result = readiness_scope.resolve_target_readiness_scopes(
            conn,
            decision_at_utc="2026-07-27T00:00:01Z",
            target_scope=POLICY_SCOPE,
            authority_scope="FORMAL_PROMOTED",
            station_ids=["AAAA"],
        )
    assert result["scope_count"] == 1
    assert result["exact_scope_count"] == 0
    assert result["items"][0]["station_id"] == "AAAA"
    assert result["ready"] is False


def test_scope_receipt_is_order_independent(monkeypatch) -> None:
    rows = [
        _registry("BBBB", "2026-07-27"),
        _registry("AAAA", "2026-07-27"),
    ]
    monkeypatch.setattr(
        readiness_scope,
        "compile_production_cycle_from_ssot",
        lambda _conn, *, station_id, event_date, **_kwargs: _compiled(
            station_id, event_date
        ),
    )
    with sqlite3.connect(":memory:") as conn:
        monkeypatch.setattr(
            readiness_scope,
            "load_started_registry_rows",
            lambda *a, **k: list(rows),
        )
        first = readiness_scope.resolve_target_readiness_scopes(
            conn,
            decision_at_utc="2026-07-27T00:00:01Z",
            target_scope=POLICY_SCOPE,
            authority_scope="FORMAL_PROMOTED",
        )
        monkeypatch.setattr(
            readiness_scope,
            "load_started_registry_rows",
            lambda *a, **k: list(reversed(rows)),
        )
        second = readiness_scope.resolve_target_readiness_scopes(
            conn,
            decision_at_utc="2026-07-27T00:00:01Z",
            target_scope=POLICY_SCOPE,
            authority_scope="FORMAL_PROMOTED",
        )
    assert first == second
    assert first["ready"] is True


def test_same_station_day_with_two_timezones_is_ambiguous(monkeypatch) -> None:
    rows = [
        _registry("AAAA", "2026-07-27", "UTC"),
        _registry("AAAA", "2026-07-27", "Asia/Shanghai"),
    ]
    monkeypatch.setattr(
        readiness_scope, "load_started_registry_rows", lambda *a, **k: rows
    )
    monkeypatch.setattr(
        readiness_scope,
        "compile_production_cycle_from_ssot",
        lambda *a, **k: pytest.fail("compiler must not run"),
    )
    with sqlite3.connect(":memory:") as conn:
        result = readiness_scope.resolve_target_readiness_scopes(
            conn,
            decision_at_utc="2026-07-27T00:00:01Z",
            target_scope=POLICY_SCOPE,
            authority_scope="FORMAL_PROMOTED",
        )
    assert result["items"][0]["status"] == "AMBIGUOUS"
    assert result["ready"] is False


def test_p0_cannot_satisfy_final_readiness(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness_scope,
        "load_started_registry_rows",
        lambda *a, **k: [_registry("AAAA", "2026-07-27")],
    )
    compiled = _compiled("AAAA", "2026-07-27")
    compiled["probability_envelope"]["stage"] = "P0"
    monkeypatch.setattr(
        readiness_scope,
        "compile_production_cycle_from_ssot",
        lambda *a, **k: compiled,
    )
    with sqlite3.connect(":memory:") as conn:
        result = readiness_scope.resolve_target_readiness_scopes(
            conn,
            decision_at_utc="2026-07-27T00:00:01Z",
            target_scope=POLICY_SCOPE,
            authority_scope="FORMAL_PROMOTED",
        )
    assert result["items"][0]["status"] == "SCOPE_MISMATCH"
    assert result["ready"] is False
