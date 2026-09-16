"""Charter 141 (user rulings 2026-09-17: "统一优化资金层面", "资金池用余额", "改").

The capital layer reads the real account and gives the station-day two
numbers: what sizing may target (6.5% of the book) and what may go out (10%):
the lift to the venue's minimum, a completion, the execution candidates and
the bundle's budget gate.  The per-stage share is retired.

EPWA 2026-09-18 is the specimen: 21C held at 5 x 0.43, 22C's resting bid
cancelled when the market ran past it, and every later decision wanted 22C
back at 0.43 x 5 = 2.15 with 1.44 of stage allowance left -- the completion
search returned nothing and the bundle's budget gate would have refused it
had it existed.
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3

import pytest

from bayes_basket_target import account_equity as AE
from bayes_basket_target import runtime as R
from bayes_basket_target.basket_kernel import delta_v2 as K
from bayes_basket_target.basket_kernel.delta_v2 import (
    LADDER_BALANCE_CANDIDATE_ORIGIN,
)
from bayes_basket_target.delta_v2_runtime import compile_delta_v2_runtime_plan
from tests.test_charter_84_ladder_balance import (  # noqa: F401 -- fixture
    _basket_policy,
    _charter_81_arm_on,
    _delta_policy,
    _fill_resolver,
    _held_snapshot,
)


# ----------------------------------------------------------------- kernel


def test_the_station_day_has_a_sizing_number_and_a_ceiling() -> None:
    assert K.MAX_STAKE_FRACTION_PER_SCOPE == pytest.approx(0.065)
    assert K.STATION_DAY_HARD_FRACTION == pytest.approx(0.10)
    # The real book on 2026-09-17, with EPWA's 21C already bought.
    soft = K.scope_stake_cap_usdc(
        book_equity_usdc=99.80, stage="P0", committed_usdc=2.15,
    )
    hard = K.station_day_ceiling_usdc(
        book_equity_usdc=99.80, committed_usdc=2.15,
    )
    assert soft == pytest.approx(99.80 * 0.065 - 2.15)
    assert hard == pytest.approx(99.80 * 0.10 - 2.15)
    # Enough to buy 22C back at 0.43 x 5 -- the 2% x 0.6 rule left 1.44.
    assert hard >= 2.15
    assert 99.80 * 0.02 * 0.6 - 2.15 < 2.15


def test_no_stage_owns_a_share_any_more() -> None:
    assert not hasattr(K, "STAGE_BUDGET_SHARE")
    caps = {
        stage: K.scope_stake_cap_usdc(book_equity_usdc=99.80, stage=stage)
        for stage in ("P0", "P0-M", "P1", "")
    }
    assert len(set(round(v, 9) for v in caps.values())) == 1


def test_the_ceiling_is_never_below_the_sizing_number() -> None:
    for equity in (10.0, 99.8, 500.0):
        for committed in (0.0, 3.0, 50.0):
            assert K.station_day_ceiling_usdc(
                book_equity_usdc=equity, committed_usdc=committed,
            ) >= K.scope_stake_cap_usdc(
                book_equity_usdc=equity, committed_usdc=committed,
            ) - 1e-12
    assert K.station_day_ceiling_usdc(book_equity_usdc=0.0) == 0.0


def test_the_policy_bankroll_bounds_follow_the_ceiling() -> None:
    bounds = R._capital_layer_policy_bounds(99.80)
    assert bounds["total_budget_usdc"] == pytest.approx(9.98)
    assert bounds["minimum_expected_pnl_after_usdc"] == pytest.approx(-9.98)
    assert bounds["protected_floor_usdc"] == pytest.approx(-9.98)
    assert bounds["max_outside_loss_usdc"] == pytest.approx(9.98)
    # No equity on the snapshot: the policy keeps what it had.
    assert R._capital_layer_policy_bounds(None) == {}
    # The retired dial is not read.
    assert not hasattr(R, "_station_cap_override")


# ---------------------------------------------------------------- compile


def _compile(**capital):
    return compile_delta_v2_runtime_plan(
        snapshot=_held_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=99.80,
        **capital,
    )


def test_a_completion_the_sizing_number_cannot_afford_goes_out_under_the_ceiling() -> None:
    """The ZGSZ half-basket (30C and 31C held, 32C short) with EPWA's arithmetic:
    a sizing remainder too small for the missing rung's lift, and a ceiling
    that is not."""

    starved = _compile(capital_available_usdc=0.50)
    assert starved["delta_v2_action_selection"]["authorized"] is not True
    assert not starved.get("execution_gap_support")

    funded = _compile(capital_available_usdc=0.50, capital_ceiling_usdc=3.00)
    solution = funded["target_solution"]
    assert solution["winner_candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    assert set(funded["execution_gap_support"]) == {"32"}
    assert funded["delta_v2_action_selection"]["authorized"] is True
    tilt = funded["delta_v2_audit"]["consolidation_tilt"]
    # Sizing still targeted the small number; the lift and the gate read the
    # ceiling.
    assert tilt["capital_after_usdc"] == pytest.approx(0.50)
    assert tilt["execution_capital_usdc"] == pytest.approx(3.00)
    assert tilt["capital_layer_rule"] == K.CAPITAL_LAYER_RULE_VERSION


def test_no_ceiling_is_the_pre_ruling_arithmetic() -> None:
    plain = _compile(capital_available_usdc=10.0)
    same = _compile(capital_available_usdc=10.0, capital_ceiling_usdc=10.0)
    assert plain["target_solution"]["winner_candidate_origin"] == (
        same["target_solution"]["winner_candidate_origin"]
    )
    assert plain["execution_gap_support"] == same["execution_gap_support"]
    assert plain["delta_v2_audit"]["consolidation_tilt"][
        "execution_capital_usdc"
    ] == pytest.approx(10.0)


# --------------------------------------------------------- account equity


def _ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(
        "CREATE TABLE auto_trade_decisions(decision_id TEXT, station_id TEXT,"
        " event_date TEXT, settlement_timezone TEXT);"
        "CREATE TABLE basket_target_order_intents(order_intent_id TEXT,"
        " decision_id TEXT, order_action TEXT, requested_notional_usdc REAL);"
        "CREATE TABLE basket_target_order_reconciliations(order_intent_id TEXT,"
        " state TEXT, filled_size REAL, filled_notional_usdc REAL);"
    )
    return conn


@contextmanager
def _write():
    yield


def test_the_account_is_cash_plus_marked_positions_and_ages_out() -> None:
    conn = _ledger()
    conn.execute("INSERT INTO auto_trade_decisions VALUES('d','EPWA','2026-09-18','Europe/Warsaw')")
    conn.execute("INSERT INTO basket_target_order_intents VALUES('i','d','BUY',1.10)")
    conn.execute("INSERT INTO basket_target_order_reconciliations VALUES('i','pending',0,0)")
    receipt = AE.capture_account_equity(
        conn, as_of_utc="2026-09-16T16:00:00.000Z", write=_write,
        cash_reader=lambda **k: (55.28, "2026-09-16T15:59:30.000Z"),
        positions_reader=lambda **k: 44.52,
    )
    assert receipt["state"] == "recorded"
    row = AE.load_account_equity(conn, as_of_utc="2026-09-16T16:10:00.000Z")
    assert row["equity_usdc"] == pytest.approx(99.80)
    assert row["resting_buy_usdc"] == pytest.approx(1.10)
    # Older than the freshness bound: not the account any more.
    assert AE.load_account_equity(
        conn, as_of_utc="2026-09-16T16:15:01.000Z",
    ) is None
    # Never from the future.
    assert AE.load_account_equity(
        conn, as_of_utc="2026-09-16T15:59:59.000Z",
    ) is None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE {AE.TABLE} SET equity_usdc=1")


def test_a_failed_read_is_a_receipt_not_a_row() -> None:
    conn = _ledger()

    def broken(**_kwargs):
        raise AE.ContractError("account_equity_gateway_cash_stale")

    receipt = AE.capture_account_equity(
        conn, as_of_utc="2026-09-16T16:00:00.000Z", write=_write,
        cash_reader=broken, positions_reader=lambda **k: 44.52,
    )
    assert receipt["state"] == "read_failed"
    assert "gateway_cash_stale" in receipt["reason"]
    assert AE.load_account_equity(
        conn, as_of_utc="2026-09-16T16:00:00.000Z",
    ) is None


def test_the_budget_dial_caps_the_book() -> None:
    assert AE.budget_capped_equity(99.80, 120.0) == pytest.approx(99.80)
    assert AE.budget_capped_equity(99.80, 50.0) == pytest.approx(50.0)
    assert AE.budget_capped_equity(99.80, None) == pytest.approx(99.80)
    assert AE.budget_capped_equity(99.80, 0.0) == pytest.approx(99.80)


def test_the_ddl_never_commits_a_callers_transaction() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("CREATE TABLE marker(x)")
    AE.ensure_account_equity_table(conn)
    assert conn.in_transaction
    conn.execute("ROLLBACK")
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('marker', ?)",
        (AE.TABLE,),
    ).fetchone()[0] == 0


def test_the_runtime_bounds_a_station_day_by_the_whole_books_room() -> None:
    import inspect

    source = inspect.getsource(R._compile_target_plan)
    # Sizing reads the soft number, orders the ceiling, both under the room.
    assert "capital_available_usdc=sizing_capital_usdc" in source
    assert "capital_ceiling_usdc=remaining_budget" in source
    assert 'market_snapshot.get("account_open_exposure_usdc")' in source
    assert "book_equity_usdc * STATION_DAY_HARD_FRACTION" in source


# ----------------------------------------------------------------- worker


def test_the_resident_reads_the_account_once_a_minute(monkeypatch) -> None:
    from bayes_basket_target import production_worker as W

    worker = object.__new__(W.TargetProductionWorker)
    worker.commit_conn = _ledger()
    worker.conn = worker.commit_conn
    calls = []
    monkeypatch.setattr(
        W, "capture_account_equity",
        lambda conn, **kw: calls.append(kw["as_of_utc"]) or {"state": "recorded"},
    )
    assert worker._capture_account_equity(as_of_utc="2026-09-16T16:00:00.000Z")[
        "state"
    ] == "recorded"
    # The table exists before any read succeeds, so a store whose first read
    # fails reads as stale, never as a store without an account.
    assert AE.table_present(worker.commit_conn)
    assert worker._capture_account_equity(as_of_utc="2026-09-16T16:00:10.000Z")[
        "state"
    ] == "throttled"
    assert len(calls) == 1


def test_a_stale_account_is_not_a_book(monkeypatch) -> None:
    from bayes_basket_target import production_worker as W

    worker = object.__new__(W.TargetProductionWorker)
    worker.conn = _ledger()
    worker.commit_conn = worker.conn
    monkeypatch.setattr(W, "_resolve_target_remaining_budget", lambda: 120.0)
    # A store that never read the account sizes as before.
    assert worker._account_book_equity_usdc(
        as_of_utc="2026-09-16T16:00:00.000Z",
    ) == (None, "account_equity_table_absent")
    AE.ensure_account_equity_table(worker.conn)
    assert worker._account_book_equity_usdc(
        as_of_utc="2026-09-16T16:00:00.000Z",
    ) == (None, "account_equity_stale")
    AE.capture_account_equity(
        worker.conn, as_of_utc="2026-09-16T16:00:00.000Z", write=_write,
        cash_reader=lambda **k: (55.28, "2026-09-16T16:00:00.000Z"),
        positions_reader=lambda **k: 44.52,
    )
    equity, state = worker._account_book_equity_usdc(
        as_of_utc="2026-09-16T16:05:00.000Z",
    )
    assert state == "recorded"
    assert equity == pytest.approx(99.80)


def test_the_reservation_pool_is_rebased_on_the_account() -> None:
    conn = _ledger()
    conn.execute(
        "CREATE TABLE basket_target_capital_pool(pool_id INTEGER PRIMARY KEY,"
        " balance_usdc REAL NOT NULL, reserved_usdc REAL NOT NULL,"
        " capital_epoch_id TEXT, version INTEGER NOT NULL, updated_at TEXT,"
        " CHECK(reserved_usdc<=balance_usdc))"
    )
    # The paper counter: 534.29 seeded, 236.05 of fills taken off, 40.59
    # still reserved by prepared cycles.
    conn.execute(
        "INSERT INTO basket_target_capital_pool VALUES(1,298.24,40.59,'e',7,'t')"
    )
    receipt = AE.capture_account_equity(
        conn, as_of_utc="2026-09-16T16:00:00.000Z", write=_write,
        cash_reader=lambda **k: (55.28, "2026-09-16T16:00:00.000Z"),
        positions_reader=lambda **k: 44.52, budget_usdc=120.0,
    )
    assert receipt["pool"]["moved"] is True
    balance, reserved, version = conn.execute(
        "SELECT balance_usdc,reserved_usdc,version FROM basket_target_capital_pool"
    ).fetchone()
    # A new reservation now sees the real book as its room.
    assert balance - reserved == pytest.approx(99.80)
    assert reserved == pytest.approx(40.59)
    assert version == 8
    # A budget dial below the account caps the room.
    AE.capture_account_equity(
        conn, as_of_utc="2026-09-16T16:01:00.000Z", write=_write,
        cash_reader=lambda **k: (55.28, "2026-09-16T16:01:00.000Z"),
        positions_reader=lambda **k: 44.52, budget_usdc=60.0,
    )
    balance, reserved = conn.execute(
        "SELECT balance_usdc,reserved_usdc FROM basket_target_capital_pool"
    ).fetchone()
    assert balance - reserved == pytest.approx(60.0)
