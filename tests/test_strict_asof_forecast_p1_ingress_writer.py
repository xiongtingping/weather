from __future__ import annotations

import sqlite3

import pytest

from strict_asof_forecast_p1_ingress_writer import (
    StrictForecastP1IngressError,
    persist_tier_a_forecast_p1_proposal,
)


def test_forecast_p1_writer_is_typed_rejected_before_database_read() -> None:
    conn = sqlite3.connect(":memory:")
    conn.set_authorizer(
        lambda *_args: sqlite3.SQLITE_DENY
    )
    with pytest.raises(
        StrictForecastP1IngressError,
        match="forecast_or_nwp_cannot_materialize_p1_use_p0_or_p0m_pipeline",
    ):
        persist_tier_a_forecast_p1_proposal(conn, {"ok": True})


def test_forecast_p1_writer_never_mutates_existing_transaction() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sentinel(value TEXT)")
    conn.execute("INSERT INTO sentinel(value) VALUES('before')")
    before_changes = conn.total_changes
    with pytest.raises(StrictForecastP1IngressError):
        persist_tier_a_forecast_p1_proposal(conn, {})
    assert conn.total_changes == before_changes
    assert conn.execute("SELECT value FROM sentinel").fetchall() == [("before",)]
