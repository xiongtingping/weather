"""The opener-tilt writer must accept the plan shape the worker hands it.

This layer shipped 2026-08-28 with a guard on ``plan.get("stage")`` -- a key
the compiled production plan has never carried -- so the guard chain
returned False on every call, the table was never created, and every P0-M
and P1 top-up ran untilted ("no_opener_record") for two days while the
module's own docstring said the semantics were live.  These tests hold the
writer to the real plan shape, and hold the failure mode to a loud one.
"""

from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.consolidation_chain import (
    load_chain_tilt,
    record_chain_opener_tilt,
)
from bayes_basket_target.contracts import ContractError


def _plan(
    *,
    status: str = "ready_no_live",
    basis: str = "p0_own_lights",
    factor: float = 0.05,
    with_audit: bool = True,
) -> dict:
    # Mirrors what compile_production_cycle_from_ssot actually returns:
    # status and delta_v2_audit at the top level, and NO "stage" key.
    plan = {
        "status": status,
        "production_plan_sha256": "c" * 64,
    }
    if with_audit:
        plan["delta_v2_audit"] = {
            "schema": "delta_basket_score_authorization_v2",
            "consolidation_tilt": {
                "factor": factor,
                "basis": basis,
                "signals_lit": ["sharp"],
            },
        }
    return plan


@pytest.fixture()
def conn():
    with sqlite3.connect(":memory:") as connection:
        yield connection


def test_p0_opener_records_and_later_stages_read_it(conn) -> None:
    wrote = record_chain_opener_tilt(
        conn,
        station_id="WSSS",
        event_date="2026-08-27",
        plan=_plan(),
        decided_at_utc="2026-08-27T02:00:00Z",
    )
    assert wrote is True
    assert load_chain_tilt(
        conn, station_id="WSSS", event_date="2026-08-27"
    ) == 0.05


def test_untilted_opener_records_full_stake_for_the_chain(conn) -> None:
    # A dark-open chain (no lights lit) must record factor 1.0 so later
    # stages inherit FULL stake even when their own moment's lights are
    # lit -- starving exactly these chains' top-ups is what the stage
    # semantics cost 81.8-95u on the book.
    assert record_chain_opener_tilt(
        conn,
        station_id="WSSS",
        event_date="2026-08-27",
        plan=_plan(factor=1.0),
        decided_at_utc="2026-08-27T02:00:00Z",
    )
    assert load_chain_tilt(
        conn, station_id="WSSS", event_date="2026-08-27"
    ) == 1.0


def test_first_authorized_opener_wins_forever(conn) -> None:
    record_chain_opener_tilt(
        conn,
        station_id="WSSS",
        event_date="2026-08-27",
        plan=_plan(factor=1.0),
        decided_at_utc="2026-08-27T02:00:00Z",
    )
    wrote = record_chain_opener_tilt(
        conn,
        station_id="WSSS",
        event_date="2026-08-27",
        plan=_plan(factor=0.05),
        decided_at_utc="2026-08-27T03:00:00Z",
    )
    assert wrote is False
    assert load_chain_tilt(
        conn, station_id="WSSS", event_date="2026-08-27"
    ) == 1.0


def test_hold_and_blocked_plans_do_not_open_the_chain(conn) -> None:
    for status in ("hold", "blocked", ""):
        assert not record_chain_opener_tilt(
            conn,
            station_id="WSSS",
            event_date="2026-08-27",
            plan=_plan(status=status),
            decided_at_utc="2026-08-27T02:00:00Z",
        )
    assert load_chain_tilt(
        conn, station_id="WSSS", event_date="2026-08-27"
    ) is None


def test_later_stage_bases_return_false(conn) -> None:
    for basis in ("opener_inherited", "no_opener_record"):
        assert not record_chain_opener_tilt(
            conn,
            station_id="WSSS",
            event_date="2026-08-27",
            plan=_plan(basis=basis),
            decided_at_utc="2026-08-27T02:00:00Z",
        )


def test_ready_plan_without_tilt_audit_is_loud(conn) -> None:
    with pytest.raises(
        ContractError, match="consolidation_chain_plan_missing_tilt_audit"
    ):
        record_chain_opener_tilt(
            conn,
            station_id="WSSS",
            event_date="2026-08-27",
            plan=_plan(with_audit=False),
            decided_at_utc="2026-08-27T02:00:00Z",
        )


def test_reader_tolerates_a_missing_table(conn) -> None:
    assert load_chain_tilt(
        conn, station_id="WSSS", event_date="2026-08-27"
    ) is None
