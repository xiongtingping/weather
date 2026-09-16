"""Portfolio-level exposure, Tier-1 bounds, and the drawdown breaker.

Charter 141 (2026-09-17): the bankroll is the real account read from the venue
(``basket_target_account_equity``), exposure is the ledger's own fills on
station-days that have not settled plus resting buys, and the breaker's curve
is the account reads.  The pool counter is not read.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

import pytest

from bayes_basket_target.account_equity import (
    capture_account_equity,
    ensure_account_equity_table,
)
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.portfolio_inventory import (
    build_portfolio_inventory,
    evaluate_drawdown_breaker,
    load_portfolio_capital_policy,
    persist_portfolio_inventory,
)


def _book(rows, *, reads=((100.0, 0.0, "2026-08-12T03:59:00.000Z"),)):
    """A ledger holding the given (station, date, tz, usdc, state) positions,
    and an account read for each (cash, positions, observed_at) in ``reads``.

    ``state`` is the reconciliation state: ``resolved_filled`` is a filled
    position, ``pending`` a resting buy.
    """

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(
        "CREATE TABLE auto_trade_decisions("
        "decision_id TEXT, station_id TEXT, event_date TEXT,"
        "settlement_timezone TEXT);"
        "CREATE TABLE basket_target_order_intents("
        "order_intent_id TEXT, decision_id TEXT, order_action TEXT,"
        "requested_notional_usdc REAL);"
        "CREATE TABLE basket_target_order_reconciliations("
        "order_intent_id TEXT, state TEXT, filled_size REAL,"
        "filled_notional_usdc REAL);"
    )
    for index, (station, date, tz, usdc, state) in enumerate(rows):
        conn.execute(
            "INSERT INTO auto_trade_decisions VALUES(?,?,?,?)",
            (f"d-{index}", station, date, tz),
        )
        conn.execute(
            "INSERT INTO basket_target_order_intents VALUES(?,?,?,?)",
            (f"i-{index}", f"d-{index}", "BUY", usdc),
        )
        filled = usdc if state == "resolved_filled" else 0.0
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations VALUES(?,?,?,?)",
            (f"i-{index}", state, 5.0 if filled else 0.0, filled),
        )
    ensure_account_equity_table(conn)

    @contextmanager
    def write():
        yield

    for cash, positions, observed in reads:
        capture_account_equity(
            conn, as_of_utc=observed, write=write,
            cash_reader=lambda _c=cash, _o=observed, **k: (_c, _o),
            positions_reader=lambda _p=positions, **k: _p,
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
        ("ZUUU", "2026-08-12", "Asia/Shanghai", 10.0, "resolved_filled"),
        ("ZUCK", "2026-08-12", "Asia/Shanghai", 6.0, "pending"),
        ("KLGA", "2026-08-12", "America/New_York", 4.0, "resolved_filled"),
        # A day that ended more than the settle grace ago is not exposure.
        ("KSFO", "2026-08-09", "America/New_York", 3.0, "resolved_filled"),
        # A cancelled order that bought nothing is not exposure either.
        ("KATL", "2026-08-12", "America/New_York", 9.0, "resolved_cancelled"),
    ])
    inventory = build_portfolio_inventory(
        conn, as_of_utc="2026-08-12T04:00:00Z",
        blocks_by_station={
            "ZUUU": "CHINA_CENTRAL", "ZUCK": "CHINA_CENTRAL",
            "KLGA": "NA_NORTHEAST",
        },
        policy=_policy(),
    )
    assert inventory["open_notional_usdc"] == pytest.approx(20.0)
    assert "KSFO" not in inventory["station_exposure_usdc"]
    assert "KATL" not in inventory["station_exposure_usdc"]
    # Two cities under one air mass count as one position.
    assert inventory["block_exposure_usdc"]["CHINA_CENTRAL"] == (
        pytest.approx(16.0)
    )
    # Asia settles a day earlier in UTC than the US does.
    windows = inventory["window_exposure_usdc"]
    assert len(windows) == 2
    assert windows["2026-08-12T16:00Z"] == pytest.approx(16.0)
    # The bankroll is the account read, not a pool.
    assert inventory["bankroll_usdc"] == pytest.approx(100.0)


def test_each_tier_one_bound_is_reported_and_stops_new_intents() -> None:
    # 40 cash and 60 in positions: the book is 100 and 60 of it is not cash.
    conn = _book(
        [("ZUUU", "2026-08-12", "Asia/Shanghai", 60.0, "resolved_filled")],
        reads=((40.0, 60.0, "2026-08-12T03:59:00.000Z"),),
    )
    inventory = build_portfolio_inventory(
        conn, as_of_utc="2026-08-12T04:00:00Z",
        blocks_by_station={"ZUUU": "CHINA_CENTRAL"},
        policy=_policy(portfolio_risk_fraction=0.5),
    )
    breached = {row["limit"] for row in inventory["breaches"]}
    assert "portfolio_risk_fraction" in breached      # 60 > 50
    assert "station_cap_fraction" in breached         # 60 > 25
    assert "block_exposure_cap_fraction" in breached  # 60 > 50
    assert "settlement_window_cap_fraction" in breached
    assert inventory["new_intents_allowed"] is False

    within = build_portfolio_inventory(
        _book(
            [("ZUUU", "2026-08-12", "Asia/Shanghai", 5.0, "resolved_filled")],
            reads=((95.0, 5.0, "2026-08-12T03:59:00.000Z"),),
        ),
        as_of_utc="2026-08-12T04:00:00Z",
        blocks_by_station={"ZUUU": "CHINA_CENTRAL"},
        policy=_policy(),
    )
    assert within["breaches"] == []
    assert within["new_intents_allowed"] is True


def test_the_budget_dial_caps_the_bankroll() -> None:
    inventory = build_portfolio_inventory(
        _book([], reads=((150.0, 50.0, "2026-08-12T03:59:00.000Z"),)),
        as_of_utc="2026-08-12T04:00:00Z",
        blocks_by_station={}, policy=_policy(), budget_usdc=120.0,
    )
    assert inventory["bankroll_usdc"] == pytest.approx(120.0)


def test_no_fresh_account_read_takes_on_nothing() -> None:
    stale = _book([], reads=((100.0, 0.0, "2026-08-12T01:00:00.000Z"),))
    inventory = build_portfolio_inventory(
        stale, as_of_utc="2026-08-12T04:00:00Z",
        blocks_by_station={}, policy=_policy(),
    )
    assert {row["limit"] for row in inventory["breaches"]} == {
        "account_equity_unavailable"
    }
    assert inventory["new_intents_allowed"] is False
    assert inventory["bankroll_usdc"] == 0.0


def test_drawdown_breaker_trips_on_the_account_reads() -> None:
    conn = _book([], reads=(
        (100.0, 0.0, "2026-08-08T00:00:00.000Z"),
        (90.0, 0.0, "2026-08-10T00:00:00.000Z"),
        (70.0, 0.0, "2026-08-11T23:59:00.000Z"),
    ))
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

    # No reads at all: abstain rather than invent a peak from one point.
    empty = sqlite3.connect(":memory:")
    abstained = evaluate_drawdown_breaker(
        empty, as_of_utc="2026-08-12T00:00:00Z",
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
    conn = _book(
        [("ZUUU", "2026-08-12", "Asia/Shanghai", 5.0, "resolved_filled")]
    )
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
