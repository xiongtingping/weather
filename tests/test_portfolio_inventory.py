"""Portfolio-level exposure, Tier-1 bounds, and the drawdown breaker."""

from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.portfolio_inventory import (
    build_portfolio_inventory,
    evaluate_drawdown_breaker,
    load_portfolio_capital_policy,
    persist_portfolio_inventory,
)


def _book(rows, *, balance=100.0):
    """A ledger holding exactly the given open cycles."""

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_target_capital_pool("
        "pool_id INTEGER, balance_usdc REAL, reserved_usdc REAL);"
        "CREATE TABLE basket_target_execution_cycles("
        "cycle_id TEXT, station_id TEXT, event_date TEXT,"
        "settlement_timezone TEXT, state TEXT);"
        "CREATE TABLE basket_target_order_intents("
        "order_intent_id TEXT, cycle_id TEXT, state TEXT,"
        "requested_notional_usdc REAL);"
    )
    conn.execute(
        "INSERT INTO basket_target_capital_pool VALUES(1,?,0.0)", (balance,)
    )
    for index, (station, date, tz, notional, state) in enumerate(rows):
        cycle = f"cycle-{index}"
        conn.execute(
            "INSERT INTO basket_target_execution_cycles "
            "VALUES(?,?,?,?,?)", (cycle, station, date, tz, state),
        )
        conn.execute(
            "INSERT INTO basket_target_order_intents VALUES(?,?,?,?)",
            (f"intent-{index}", cycle, "prepared", notional),
        )
    return conn


def _policy(**overrides):
    policy, _ = load_portfolio_capital_policy()
    return {**policy, **overrides}


def test_shipped_policy_is_valid_and_breaker_cannot_liquidate() -> None:
    policy, sha = load_portfolio_capital_policy()
    assert len(sha) == 64
    breaker = policy["drawdown_circuit_breaker"]
    # Closing positions would realize the loss the breaker is reacting to.
    assert breaker["action"] == "STOP_NEW_INTENTS_ONLY"


def test_exposure_aggregates_by_window_block_and_station() -> None:
    conn = _book([
        ("ZUUU", "2026-08-12", "Asia/Shanghai", 10.0, "prepared"),
        ("ZUCK", "2026-08-12", "Asia/Shanghai", 6.0, "submitted"),
        ("KLGA", "2026-08-12", "America/New_York", 4.0, "prepared"),
        ("KSFO", "2026-08-12", "America/New_York", 3.0, "settled"),
    ])
    inventory = build_portfolio_inventory(
        conn, as_of_utc="2026-08-12T04:00:00Z",
        blocks_by_station={
            "ZUUU": "CHINA_CENTRAL", "ZUCK": "CHINA_CENTRAL",
            "KLGA": "NA_NORTHEAST",
        },
        policy=_policy(),
    )
    # A settled cycle is not open exposure.
    assert inventory["open_notional_usdc"] == pytest.approx(20.0)
    assert "KSFO" not in inventory["station_exposure_usdc"]
    # Two cities under one air mass count as one position.
    assert inventory["block_exposure_usdc"]["CHINA_CENTRAL"] == (
        pytest.approx(16.0)
    )
    # Asia settles a day earlier in UTC than the US does.
    windows = inventory["window_exposure_usdc"]
    assert len(windows) == 2
    assert windows["2026-08-12T16:00Z"] == pytest.approx(16.0)


def test_each_tier_one_bound_is_reported_and_stops_new_intents() -> None:
    conn = _book(
        [("ZUUU", "2026-08-12", "Asia/Shanghai", 60.0, "prepared")],
        balance=100.0,
    )
    inventory = build_portfolio_inventory(
        conn, as_of_utc="2026-08-12T04:00:00Z",
        blocks_by_station={"ZUUU": "CHINA_CENTRAL"},
        policy=_policy(),
    )
    breached = {row["limit"] for row in inventory["breaches"]}
    assert "portfolio_risk_fraction" in breached      # 60 > 35
    assert "station_cap_fraction" in breached         # 60 > 25
    assert "block_exposure_cap_fraction" in breached  # 60 > 50
    assert "settlement_window_cap_fraction" in breached
    assert inventory["new_intents_allowed"] is False

    within = build_portfolio_inventory(
        _book([("ZUUU", "2026-08-12", "Asia/Shanghai", 5.0, "prepared")]),
        as_of_utc="2026-08-12T04:00:00Z",
        blocks_by_station={"ZUUU": "CHINA_CENTRAL"},
        policy=_policy(),
    )
    assert within["breaches"] == []
    assert within["new_intents_allowed"] is True


def test_drawdown_breaker_trips_on_the_recorded_equity_curve() -> None:
    conn = _book([], balance=70.0)
    conn.executescript(
        "CREATE TABLE basket_target_capital_pool_history("
        "balance_usdc REAL, updated_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_target_capital_pool_history VALUES(?,?)",
        [(100.0, "2026-08-08T00:00:00Z"), (90.0, "2026-08-10T00:00:00Z")],
    )
    tripped = evaluate_drawdown_breaker(
        conn, as_of_utc="2026-08-12T00:00:00Z",
        policy=_policy(), bankroll_usdc=70.0,
    )
    assert tripped["peak_usdc"] == 100.0
    assert tripped["drawdown_fraction"] == pytest.approx(0.3)
    assert tripped["tripped"] is True

    # A peak outside the rolling window is not the peak.
    narrow = evaluate_drawdown_breaker(
        conn, as_of_utc="2026-08-12T00:00:00Z",
        policy=_policy(drawdown_circuit_breaker={
            **_policy()["drawdown_circuit_breaker"], "rolling_days": 1,
        }),
        bankroll_usdc=70.0,
    )
    assert narrow["peak_usdc"] == 70.0
    assert narrow["tripped"] is False

    # No history table: abstain rather than invent a peak from one point.
    abstained = evaluate_drawdown_breaker(
        _book([], balance=70.0), as_of_utc="2026-08-12T00:00:00Z",
        policy=_policy(), bankroll_usdc=70.0,
    )
    assert abstained["tripped"] is False


def test_policy_refuses_a_breaker_that_would_close_positions() -> None:
    import tempfile
    from pathlib import Path

    policy, _ = load_portfolio_capital_policy()
    bad = {
        **policy,
        "drawdown_circuit_breaker": {
            **policy["drawdown_circuit_breaker"],
            "action": "LIQUIDATE_ALL",
        },
    }
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "bad.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(
            ContractError,
            match="portfolio_capital_policy_breaker_action_invalid",
        ):
            load_portfolio_capital_policy(path)


def test_projection_is_append_only_and_idempotent() -> None:
    conn = _book([("ZUUU", "2026-08-12", "Asia/Shanghai", 5.0, "prepared")])
    snapshot = build_portfolio_inventory(
        conn, as_of_utc="2026-08-12T04:00:00Z",
        blocks_by_station={"ZUUU": "CHINA_CENTRAL"}, policy=_policy(),
    )
    assert persist_portfolio_inventory(
        conn, snapshot, created_at_utc="2026-08-12T04:00:00Z"
    ) == 1
    assert persist_portfolio_inventory(
        conn, snapshot, created_at_utc="2026-08-12T04:00:00Z"
    ) == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE basket_portfolio_inventory_snapshot SET bankroll_usdc=1"
        )
