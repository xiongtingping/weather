"""Backlog discovery has to ask whether a scope *is* eligible, not whether it
ever was.

LLBG, LTFM and UUWW were admitted to the market registry at 2026-08-31T03:51Z
and blocked again at 05:49Z as ``official_source_route_unmapped`` -- the
official source route table carried no entry for them, so nothing in the fleet
captured them.  The eligible rows from those two hours stay in the registry
forever and nothing later contradicts them, because nothing later mentions the
scope at all.  Six station-days therefore sat on the board as gaps a backfill
would close.  It would not: AWC had the reports, and no lane of ours was ever
going to ask for them.
"""

from __future__ import annotations

import sqlite3

from bayes_basket_target.settlement_truth import registry_scope_still_eligible


def _registry() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_market_registry("
        "  station_id TEXT, event_date TEXT, source_eligible INTEGER,"
        "  source_block_reason TEXT, refreshed_at TEXT)"
    )
    return conn


def _row(
    conn: sqlite3.Connection,
    station: str,
    day: str,
    eligible: int,
    refreshed_at: str,
    reason: str = "",
) -> None:
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?)",
        (station, day, eligible, reason, refreshed_at),
    )


def test_a_scope_the_registry_still_admits_is_asked_about() -> None:
    conn = _registry()
    try:
        _row(conn, "KMIA", "2026-08-30", 1, "2026-08-31T00:00:00Z")

        assert registry_scope_still_eligible(
            conn, station_id="KMIA", event_date="2026-08-30"
        ) is True
    finally:
        conn.close()


def test_two_eligible_hours_do_not_owe_a_truth_row_forever() -> None:
    conn = _registry()
    try:
        _row(conn, "LLBG", "2026-09-01", 1, "2026-08-31T03:51:17Z")
        _row(conn, "LLBG", "2026-09-01", 1, "2026-08-31T05:49:31Z")
        _row(
            conn, "LLBG", "2026-09-01", 0, "2026-09-02T11:17:30Z",
            "official_source_route_unmapped",
        )

        assert registry_scope_still_eligible(
            conn, station_id="LLBG", event_date="2026-09-01"
        ) is False
    finally:
        conn.close()


def test_the_latest_word_can_readmit_a_scope_that_was_blocked() -> None:
    # The rule is "latest", not "ever blocked".  Onboarding the three stations
    # for real has to put their days back in the backlog by itself.
    conn = _registry()
    try:
        _row(conn, "LLBG", "2026-09-01", 0, "2026-09-02T11:17:30Z")
        _row(conn, "LLBG", "2026-09-01", 1, "2026-09-03T06:00:00Z")

        assert registry_scope_still_eligible(
            conn, station_id="LLBG", event_date="2026-09-01"
        ) is True
    finally:
        conn.close()


def test_a_scope_with_no_registry_row_is_owed_nothing() -> None:
    conn = _registry()
    try:
        assert registry_scope_still_eligible(
            conn, station_id="ZZZZ", event_date="2026-09-01"
        ) is False
        assert registry_scope_still_eligible(
            conn, station_id="", event_date="2026-09-01"
        ) is False
        assert registry_scope_still_eligible(
            conn, station_id="ZZZZ", event_date=""
        ) is False
    finally:
        conn.close()


def test_the_station_is_matched_case_insensitively() -> None:
    conn = _registry()
    try:
        _row(conn, "KMIA", "2026-08-30", 1, "2026-08-31T00:00:00Z")

        assert registry_scope_still_eligible(
            conn, station_id=" kmia ", event_date="2026-08-30"
        ) is True
    finally:
        conn.close()
