"""Charter 84 (user ruling, 2026-09-05): a seat the venue left short is completed.

First live morning: ZGSZ 2026-09-06 filled 30°C and 31°C at five shares and
the venue refused 32°C under its dollar minimum.  With the holdings visible
the Kelly law sized the seat at 0.75 USDC against 3.4 held and offered
nothing, the grid's slivers failed the scenario floor, and the plan read
``target_inventory_satisfied``.  The candidate domain had no "buy the third
rung at the size the other two were bought at".  Now it does, and it is
judged by the same floor, the same increment edge, the same lot and dollar
lift as any other candidate.
"""
from __future__ import annotations

import pytest

from bayes_basket_target import delta_v2_runtime
from bayes_basket_target.basket_kernel.contracts import (
    ContractError,
    TargetBasketSnapshot,
)
from bayes_basket_target.basket_kernel.delta_v2 import (
    CANDIDATE_KIND_BASELINE,
    CANDIDATE_KIND_COMPLETE,
    KELLY_CANDIDATE_ORIGIN,
    LADDER_BALANCE_CANDIDATE_ORIGIN,
    ladder_balance_targets_v1,
)
from bayes_basket_target.delta_v2_runtime import (
    _assert_candidate_support_contract,
    _target_candidates,
    compile_delta_v2_runtime_plan,
)
from tests.test_bayes_basket_target_delta_v2_runtime import (
    _basket_policy,
    _delta_policy,
    _fill_resolver,
)


def _held_snapshot(*, held_32: float = 0.0, ask_32: float = 0.15) -> TargetBasketSnapshot:
    """ZGSZ 2026-09-06 as the ledger saw it: 30°C and 31°C held, 32°C empty."""

    def bucket(name: str, ordinal: int, probability: float, ask: float,
               held: float, cost: float) -> dict:
        return {
            "bucket": name,
            "ordinal": ordinal,
            "probability": probability,
            "bid_price": max(0.01, ask - 0.02),
            "bid_levels": [{"price": max(0.01, ask - 0.02), "shares": 50.0}],
            "ask_levels": [{"price": ask, "shares": 100.0}],
            "price_tick_size": 0.01,
            "maker_queue_ahead_shares": 0.0,
            "maker_queue_known": True,
            "min_order_size": 5.0,
            "filled_shares": held,
            "filled_cost_usdc": cost,
        }

    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "rkpk-half-basket",
        "sequence": 1,
        "observed_at": "2026-07-28T03:00:00Z",
        "market_id": "rkpk-market",
        # The fixture policy is RKPK's; the shape is ZGSZ 2026-09-06's.
        "station_id": "RKPK",
        "event_date": "2026-07-28",
        "quote_version": "quote-v1",
        "quote_observed_at": "2026-07-28T03:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "p0_preselection_ledger_test",
        "order_state_version": "order-v1",
        "evidence": {
            "probability_stage": "P0",
            "posterior_version": "p0-v1",
            "posterior_sha256": "a" * 64,
        },
        "buckets": [
            # A symmetric day around 31°C, so the seat is the centre and its
            # two neighbours (charter 79), and the window's claim (0.90)
            # clears its all-in offer (0.80).  30°C and 31°C were bought
            # earlier at 0.25 and 0.43; the book has since moved.
            bucket("29", 29, 0.05, 0.06, 0.0, 0.0),
            bucket("30", 30, 0.20, 0.20, 5.0, 1.25),
            bucket("31", 31, 0.50, 0.45, 5.0, 2.15),
            bucket("32", 32, 0.20, ask_32, held_32, held_32 * ask_32),
            bucket("33", 33, 0.05, 0.06, 0.0, 0.0),
        ],
        "checkpoint_changed": True,
        "quote_changed": True,
        "fill_changed": True,
        "time_gate_changed": True,
        "evidence_fresh": True,
        "contract_matches": True,
        "order_state_known": True,
        "risk_inputs_complete": True,
        "time_left_minutes": 1440.0,
    })


def _candidates(snapshot: TargetBasketSnapshot, *, desired=("30", "31", "32")):
    return _target_candidates(
        snapshot=snapshot,
        desired=desired,
        artifact=_delta_policy(),
        capital_available_usdc=10.0,
        minimum_clip_notional_usdc=0.01,
        bankroll_usdc=_basket_policy().total_budget_usdc,
        probabilities_by_bucket={
            row.bucket: row.probability for row in snapshot.buckets
        },
        book_equity_usdc=514.29,
    )


def test_the_balance_shape_raises_every_short_rung_to_the_held_level() -> None:
    assert ladder_balance_targets_v1(
        held_by_bucket={"30": 5.0, "31": 5.0, "32": 0.0}, support=("30", "31", "32")
    ) == {"30": 5.0, "31": 5.0, "32": 5.0}
    assert ladder_balance_targets_v1(
        held_by_bucket={"30": 5.0, "31": 3.0}, support=("30", "31", "32")
    ) == {"30": 5.0, "31": 5.0, "32": 5.0}
    # A whole ladder, and an empty one, have nothing to balance.
    assert ladder_balance_targets_v1(
        held_by_bucket={"30": 5.0, "31": 5.0, "32": 5.0}, support=("30", "31", "32")
    ) == {}
    assert ladder_balance_targets_v1(
        held_by_bucket={}, support=("30", "31", "32")
    ) == {}
    with pytest.raises(ContractError, match="delta_v2_balance_support_duplicate"):
        ladder_balance_targets_v1(held_by_bucket={"30": 5.0}, support=("30", "30"))


def test_a_venue_short_seat_gets_a_completion_candidate_and_nothing_else_does() -> None:
    candidates = _candidates(_held_snapshot())
    balance = [
        row for row in candidates
        if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    ]
    assert len(balance) == 1
    row = balance[0]
    assert row["candidate_kind"] == CANDIDATE_KIND_COMPLETE
    # The held rungs are left exactly as they are; the short rung is bought
    # to their level down the posted offer.
    assert row["shares_by_bucket"]["30"] == pytest.approx(5.0)
    assert row["shares_by_bucket"]["31"] == pytest.approx(5.0)
    assert row["shares_by_bucket"]["32"] == pytest.approx(5.0)
    assert row["cost_by_bucket"]["30"] == pytest.approx(1.25)
    assert row["cost_by_bucket"]["32"] == pytest.approx(0.75)
    assert row["ladder_balance"] == {
        "level_shares": 5.0, "short_buckets": ["32"],
        "spend_usdc": pytest.approx(0.75), "full_spend_usdc": pytest.approx(0.75),
        "capital_scale": 1.0,
    }
    # A completion that does not fit the plan's capital whole is scaled to
    # what fits, never refused: the execution sizing raises the leg to the
    # venue minimum against the scope's budget, as it did the opener's legs.
    scaled = next(
        row for row in _target_candidates(
            snapshot=_held_snapshot(),
            desired=("30", "31", "32"),
            artifact=_delta_policy(),
            capital_available_usdc=0.30,
            minimum_clip_notional_usdc=0.01,
            bankroll_usdc=_basket_policy().total_budget_usdc,
            probabilities_by_bucket={
                row.bucket: row.probability for row in _held_snapshot().buckets
            },
            book_equity_usdc=514.29,
        )
        if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    )
    assert scaled["shares_by_bucket"]["32"] == pytest.approx(2.0)
    assert scaled["ladder_balance"]["capital_scale"] == pytest.approx(0.4)
    # The Kelly seat is None here -- the held rungs sit above what it would
    # size today -- which is exactly the hole this candidate fills.
    assert not [
        row for row in candidates if row["candidate_origin"] == KELLY_CANDIDATE_ORIGIN
    ]
    # A whole ladder offers no completion.
    assert not [
        row for row in _candidates(_held_snapshot(held_32=5.0))
        if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    ]


def test_the_completion_is_all_or_none_and_switchable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A short rung without an offer cannot be completed, so nothing is.
    raw = _held_snapshot().to_dict()
    raw["buckets"][3]["ask_levels"] = []
    raw["buckets"][3]["ask_available"] = False
    assert not [
        row for row in _candidates(TargetBasketSnapshot.from_mapping(raw))
        if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    ]
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_84_LADDER_BALANCE", "off")
    assert not [
        row for row in _candidates(_held_snapshot())
        if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    ]


def test_the_support_contract_is_atomic_at_the_result() -> None:
    baseline = {
        "candidate_id": "base", "candidate_kind": CANDIDATE_KIND_BASELINE,
        "shares_by_bucket": {"30": 5.0, "31": 5.0, "32": 0.0},
    }
    completion = {
        "candidate_id": "balance", "candidate_kind": CANDIDATE_KIND_COMPLETE,
        "shares_by_bucket": {"30": 5.0, "31": 5.0, "32": 5.0},
    }
    accepted = _assert_candidate_support_contract(
        [baseline, completion], ordered_support=("30", "31", "32")
    )
    assert [row["candidate_id"] for row in accepted] == ["base", "balance"]
    # A basket that leaves an unheld rung unbought is still incomplete, and
    # one that adds nothing is not an action.
    for shares in (
        {"30": 6.0, "31": 6.0, "32": 0.0},
        {"30": 5.0, "31": 5.0, "32": 0.0},
    ):
        with pytest.raises(
            ContractError, match="delta_v2_candidate_complete_basket_incomplete"
        ):
            _assert_candidate_support_contract(
                [baseline, {**completion, "shares_by_bucket": shares}],
                ordered_support=("30", "31", "32"),
            )


def test_the_half_basket_compiles_to_its_missing_rung_within_the_tilted_capital() -> None:
    """The whole chain on the ZGSZ shape: the solver seats the completion
    under the Kelly form, the floor accepts it, the increment edge authorizes
    it, and only the missing rung carries a gap.  The plan proposes inside
    the capital the consolidation tilt leaves (0.75 USDC here, as on the
    live day); the execution sizing lifts the leg to the venue minimum."""

    result = compile_delta_v2_runtime_plan(
        snapshot=_held_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )

    solution = result["target_solution"]
    assert solution["winner_candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    assert solution["winner_candidate_kind"] == CANDIDATE_KIND_COMPLETE
    assert result["basket_target_activated"] is True
    assert result["state"] != "HOLD"
    assert result["delta_v2_action_selection"]["authorized"] is True
    targets = {row["bucket"]: row for row in result["targets"]}
    assert set(result["execution_gap_support"]) == {"32"}
    assert targets["32"]["delta_shares"] == pytest.approx(5.0)
    assert result["delta_v2_audit"]["consolidation_tilt"]["capital_after_usdc"] == pytest.approx(0.75)


def test_the_plan_level_lift_binds_on_the_increment_not_on_what_is_held() -> None:
    from bayes_basket_target.basket_kernel.delta_v2 import raise_sub_lot_targets_v2

    common = dict(
        target_shares_by_bucket={"30": 5.0, "31": 5.0, "32": 5.0},
        support=("30", "31", "32"),
        min_lot_by_bucket={"30": 5.0, "31": 5.0, "32": 5.0},
        ask_by_bucket={"30": 0.20, "31": 0.45, "32": 0.15},
        held_shares_by_bucket={"30": 5.0, "31": 5.0},
    )
    lifted = raise_sub_lot_targets_v2(**common, capital_available_usdc=1.2)
    # 32°C's increment of five shares is $0.75, under the dollar: raised to
    # seven.  The held rungs are not orders and are neither raised nor
    # charged -- 0.75 + 0.30 fits 1.2 only because they are not.
    assert lifted["target_shares_by_bucket"] == {"30": 5.0, "31": 5.0, "32": 7.0}
    assert lifted["raised_buckets"] == ("32",)
    assert lifted["notional_raised_buckets"] == ("32",)
    assert lifted["lot_raise_usdc"] == pytest.approx(0.30)
    refused = raise_sub_lot_targets_v2(**common, capital_available_usdc=1.0)
    assert refused["refused_reason"] == "exchange_lot_top_up_unaffordable"
    # A held rung whose own dollar floor is above what it holds is still not
    # raised: nothing is being bought there.
    parked = raise_sub_lot_targets_v2(
        target_shares_by_bucket={"30": 5.0, "31": 5.0},
        support=("30", "31"),
        min_lot_by_bucket={"30": 5.0, "31": 5.0},
        ask_by_bucket={"30": 0.05, "31": 0.05},
        held_shares_by_bucket={"30": 5.0, "31": 5.0},
        capital_available_usdc=10.0,
    )
    assert parked["raised_buckets"] == ()
    # Without holdings every target is an increment: the pre-charter-84
    # reading the paper ledger still hands in.
    fresh = raise_sub_lot_targets_v2(
        target_shares_by_bucket={"32": 5.0}, support=("32",),
        min_lot_by_bucket={"32": 5.0}, ask_by_bucket={"32": 0.15},
        capital_available_usdc=10.0,
    )
    assert fresh["target_shares_by_bucket"] == {"32": 7.0}


def test_execution_sizing_reads_the_venue_minimum_as_lot_or_dollar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bayes_basket_target import runtime as production_runtime

    assert production_runtime.execution_minimum_lot_shares(
        min_order_size=5.0, limit_price=0.15
    ) == 7.0
    assert production_runtime.execution_minimum_lot_shares(
        min_order_size=5.0, limit_price=0.25
    ) == 5.0
    assert production_runtime.execution_minimum_lot_shares(
        min_order_size=5.0, limit_price=0.04
    ) == 25.0
    assert production_runtime.execution_minimum_lot_shares(
        min_order_size=0.0, limit_price=0.5
    ) == 2.0
    monkeypatch.setattr(production_runtime, "CHARTER_83_MIN_NOTIONAL", "off")
    assert production_runtime.execution_minimum_lot_shares(
        min_order_size=5.0, limit_price=0.04
    ) == 5.0


# --- the live cycle that traded is over, not still the day's candidate ----

def _live_cycle_with_fill(conn, *, index: int, fill_notional: float | None = 2.4,
                          intent_state: str = "filled", reconciled: bool = True):
    """One committed cycle (reservation 3.0) whose single intent ended."""

    from tests.test_bayes_basket_target_atomic_ledger import _committed_intent

    bundle, intent_id = _committed_intent(conn, index=index)
    cycle_id = bundle["cycle"]["cycle_id"]
    conn.execute(
        "UPDATE basket_target_order_intents SET state=? WHERE order_intent_id=?",
        (intent_state, intent_id),
    )
    if reconciled and fill_notional is not None:
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations("
            "order_intent_id,exchange_order_id,state,filled_size,"
            "filled_notional_usdc,last_event_id,blocking_reason,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (intent_id, "0x" + intent_id, "resolved_filled", 5.0,
             fill_notional, None, None, "2026-07-23T04:10:00Z"),
        )
    conn.commit()
    return cycle_id, intent_id


def _pool(conn) -> tuple[float, float]:
    row = conn.execute(
        "SELECT balance_usdc,reserved_usdc FROM basket_target_capital_pool"
    ).fetchone()
    return float(row[0]), float(row[1])


def test_a_live_cycle_whose_orders_all_ended_is_settled_by_what_it_spent() -> None:
    from bayes_basket_target.atomic_ledger import (
        AtomicLedgerError,
        settle_live_cycle_after_terminal_intents,
    )
    from tests.test_bayes_basket_target_atomic_ledger import _db

    with _db() as conn:
        cycle_id, _intent = _live_cycle_with_fill(conn, index=60)
        balance_before, reserved_before = _pool(conn)
        settled = settle_live_cycle_after_terminal_intents(
            conn, cycle_id=cycle_id, settled_at_utc="2026-07-23T04:20:00Z"
        )
        assert settled["state"] == "settled"
        assert settled["consumed_usdc"] == pytest.approx(2.4)
        assert settled["reservation_released_usdc"] == pytest.approx(3.0)
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()[0] == "settled"
        reservation = conn.execute(
            "SELECT state,release_reason FROM basket_target_capital_reservations"
            " WHERE cycle_id=?", (cycle_id,),
        ).fetchone()
        assert reservation[0] == "consumed"
        assert reservation[1].startswith("live_terminal_fills_actual_usdc=2.4")
        balance_after, reserved_after = _pool(conn)
        assert balance_after == pytest.approx(balance_before - 2.4)
        assert reserved_after == pytest.approx(reserved_before - 3.0)
        # Idempotent, and the guards refuse what is not over yet.
        assert settle_live_cycle_after_terminal_intents(
            conn, cycle_id=cycle_id, settled_at_utc="2026-07-23T04:21:00Z"
        )["state"] == "already_settled"

        still_live, _ = _live_cycle_with_fill(
            conn, index=61, intent_state="submitted", reconciled=False
        )
        with pytest.raises(AtomicLedgerError, match="intent_not_terminal"):
            settle_live_cycle_after_terminal_intents(
                conn, cycle_id=still_live, settled_at_utc="2026-07-23T04:22:00Z"
            )
        unreconciled, _ = _live_cycle_with_fill(conn, index=62, reconciled=False)
        with pytest.raises(AtomicLedgerError, match="fill_unreconciled"):
            settle_live_cycle_after_terminal_intents(
                conn, cycle_id=unreconciled, settled_at_utc="2026-07-23T04:23:00Z"
            )
        inert, _ = _live_cycle_with_fill(
            conn, index=63, intent_state="failed", reconciled=False
        )
        with pytest.raises(AtomicLedgerError, match="nothing_filled"):
            settle_live_cycle_after_terminal_intents(
                conn, cycle_id=inert, settled_at_utc="2026-07-23T04:24:00Z"
            )


def test_the_resident_sweep_settles_exactly_the_cycles_that_traded_and_ended() -> None:
    from types import SimpleNamespace

    from bayes_basket_target.production_worker import TargetProductionWorker
    from tests.test_bayes_basket_target_atomic_ledger import _db

    with _db() as conn:
        done, _ = _live_cycle_with_fill(conn, index=70)
        live, _ = _live_cycle_with_fill(
            conn, index=71, intent_state="submitted", reconciled=False
        )
        inert, _ = _live_cycle_with_fill(
            conn, index=72, intent_state="failed", reconciled=False
        )
        receipt = TargetProductionWorker._settle_live_cycles(
            SimpleNamespace(commit_conn=conn), as_of_utc="2026-07-23T04:30:00Z"
        )
        assert receipt["state"] == "committed"
        assert receipt["examined"] == 1
        assert receipt["settled"] == 1
        assert receipt["settled_scopes"][0]["cycle_id"] == done
        states = {
            row[0]: row[1] for row in conn.execute(
                "SELECT cycle_id,state FROM basket_target_execution_cycles"
            )
        }
        assert states[done] == "settled"
        assert states[live] == "prepared"
        assert states[inert] == "prepared"
        # Nothing left to do on the next pass.
        again = TargetProductionWorker._settle_live_cycles(
            SimpleNamespace(commit_conn=conn), as_of_utc="2026-07-23T04:31:00Z"
        )
        assert again["state"] == "no_settleable_cycles"


def test_station_edge_evidence_reads_the_increment_not_the_sunk_basket() -> None:
    """The allocator's evidence is what a dollar buys *now*.

    ZGSZ 2026-09-06, 13:12Z: the 12:51Z completion plan, read whole (five
    shares a rung against 4.35 of sunk-plus-new cost, in-window mass 0.77),
    scored -0.51 per basket, the allocator gave the station a zero quota,
    and the next compile had no capital to seat the completion again.
    """

    import sqlite3
    from types import SimpleNamespace

    from bayes_basket_target.p0_preselection import commit_p0_preselection
    from bayes_basket_target.production_worker import TargetProductionWorker
    from bayes_basket_target.ssot import apply_production_ssot_schema
    from tests.test_bayes_basket_target_p0_preselection import _value

    probabilities = {"30": 0.20, "31": 0.357, "32": 0.21, "33": 0.233}
    value = {
        **_value(),
        "station_id": "ZGSZ",
        "event_date": "2026-09-06",
        "compiled_plan": {
            "probability_envelope": {"probabilities": probabilities},
            "plan": {
                "final_support": ["30", "31", "32"],
                "execution_gap_support": ["32"],
                "total_delta_notional_usdc": 0.95,
                "targets": [
                    {"bucket": "30", "delta_shares": 0.0, "candidate_price": 0.0},
                    {"bucket": "31", "delta_shares": 0.0, "candidate_price": 0.0},
                    {"bucket": "32", "delta_shares": 5.0, "candidate_price": 0.19},
                ],
                "target_solution": {
                    "target_shares_by_bucket": {"30": 5.0, "31": 5.0, "32": 5.0},
                    "target_cost_by_bucket": {"30": 1.25, "31": 2.15, "32": 0.95},
                },
            },
        },
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        commit_p0_preselection(conn, value, created_at_utc="2026-09-05T12:51:33Z")
        rows = TargetProductionWorker._station_edge_evidence(
            SimpleNamespace(conn=conn), stations=["ZGSZ"], minimum_clip_usdc=0.25
        )
    assert len(rows) == 1
    # The increment: 5 shares of 32°C for 0.95 -- +0.10 expected on 0.95.
    assert rows[0]["edge_per_usdc"] == pytest.approx((0.21 * 5.0 - 0.95) / 0.95)
    assert rows[0]["minimum_basket_usdc"] == pytest.approx(0.75)
    # The whole-basket reading this replaces was negative.
    whole_cost = 4.35
    whole = sum(
        probabilities[b] * ((5.0 if b in ("30", "31", "32") else 0.0) - whole_cost)
        for b in probabilities
    )
    assert whole < 0.0
