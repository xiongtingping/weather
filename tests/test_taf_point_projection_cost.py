"""The point projection must not re-attempt what the archive already holds.

It runs behind every TAF capture ingest, which is every production cycle.
Measured 2026-09-07: 1,960 candidates, 1,960 rows already in the archive,
``wrote_count`` 0 -- and the old form issued an ``INSERT OR IGNORE`` plus a
``SELECT changes()`` for each, 3,920 statements inside the write lock's
savepoint to write nothing.  Batching removed the Python round trips; it
did not remove the work behind them, because this table carries a primary
key, a UNIQUE on content_sha256 and a natural-key UNIQUE, so every
attempted row probes three B-trees over 1,485,727 rows.

Two keys refuse a row and both are read once now.  The primary key is
content-addressed, so an amended issuance for an already-archived scope
produces a *new* id, clears that filter, and is then refused by the
natural key -- which is the append-only rule the projection's own
docstring states.  On the production archive the split was 1,558 refused
by id and 402 by the natural key, every cycle, forever.

Equivalence was checked against the real archive before this landed: seven
rows deleted from a copy, both forms re-projected, both wrote exactly
those seven, and a sha256 over all 1,485,727 (id, content_sha256) pairs
was identical.
"""

from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.native_ensemble_training_facts import (
    TAF_POINT_EXTRACTION_VERSION,
    TAF_POINT_SYSTEM_ID,
    project_taf_point_member_runs,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from tests.pr12_atomic_test_support import install_canonical_v12

STATION = "ZUUU"
DAY = "2026-07-01"


class _CountingConnection(sqlite3.Connection):
    """Counts write attempts, so the saving is measured and not asserted."""

    def executemany(self, sql, parameters, /):  # type: ignore[override]
        rows = list(parameters)
        self.insert_attempts = getattr(self, "insert_attempts", 0) + len(rows)
        return super().executemany(sql, rows)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", factory=_CountingConnection)
    apply_production_ssot_schema(connection)
    install_canonical_v12(connection)
    connection.execute(
        "INSERT INTO calibration_station_registry("
        "station_id,icao_code,city_label,timezone_name,source_lane,"
        "settlement_definition,registry_version,source_role,content_sha256"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        (
            STATION, STATION, "Chengdu", "Asia/Shanghai", "AWC",
            "daily_high_local_midnight_to_midnight", "v1",
            "official", "e" * 64,
        ),
    )
    connection.insert_attempts = 0
    return connection


def _capture(connection: sqlite3.Connection, *, issued: str, tx_c: float) -> None:
    identity = {
        "station_id": STATION,
        "event_date": DAY,
        "settlement_timezone": "Asia/Shanghai",
        "revision_id": f"taf-{issued}",
        "revision_kind": "TAF",
        "revision_sequence": 1,
        "issued_at_utc": issued,
        "received_at_utc": issued,
        "provider_available_at_utc": issued,
        "valid_from_utc": issued,
        "valid_until_utc": "2026-07-02T18:00:00Z",
        "source_object_sha256": "c" * 64,
        "source_capture_sha256": "d" * 64,
        "tx_c": tx_c,
        "cloud_cover_fraction": 0.25,
        "precipitation_probability": 0.1,
        "wind_speed_mps": 4.0,
        "source_class": "official_forward_native_receipt",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    sha = content_sha256(identity)
    columns = ["taf_capture_id", *identity, "tx_available", "content_sha256"]
    values = ["taf-capture-" + sha[:32], *identity.values(), 1, sha]
    connection.execute(
        "INSERT INTO calibration_forward_taf_capture("
        + ",".join(columns) + ") VALUES("
        + ",".join("?" for _ in columns) + ")",
        tuple(values),
    )


def _stored(connection: sqlite3.Connection) -> int:
    return int(connection.execute(
        "SELECT COUNT(*) FROM calibration_native_ensemble_historical_member_run"
        " WHERE ensemble_system_id=? AND extraction_version=?",
        (TAF_POINT_SYSTEM_ID, TAF_POINT_EXTRACTION_VERSION),
    ).fetchone()[0])


def test_a_new_capture_is_still_projected(conn: sqlite3.Connection) -> None:
    """A faster no-op is worthless if it also skips the work."""

    _capture(conn, issued="2026-06-30T22:00:00Z", tx_c=31.0)
    receipt = project_taf_point_member_runs(conn)
    assert receipt["candidate_count"] == 1
    assert receipt["attempted_count"] == 1
    assert receipt["wrote_count"] == 1
    assert _stored(conn) == 1


def test_the_second_pass_attempts_nothing(conn: sqlite3.Connection) -> None:
    """Every cycle re-projects the same archive; the common path is a no-op
    and must cost no write attempts at all."""

    _capture(conn, issued="2026-06-30T22:00:00Z", tx_c=31.0)
    project_taf_point_member_runs(conn)
    conn.insert_attempts = 0

    receipt = project_taf_point_member_runs(conn)
    assert receipt["candidate_count"] == 1
    assert receipt["already_stored_count"] == 1
    assert receipt["attempted_count"] == 0
    assert receipt["wrote_count"] == 0
    assert conn.insert_attempts == 0, (
        "a re-projection must send no INSERT at all"
    )
    assert _stored(conn) == 1


def test_an_amendment_the_natural_key_refuses_is_not_re_attempted(
    conn: sqlite3.Connection,
) -> None:
    """The id is content-addressed, so a later issuance for an archived
    scope clears the id filter and is then refused by the natural key --
    append-only, one row per (station, day, lead).  That refusal is
    permanent, so re-attempting it every cycle buys nothing.

    On production this was 402 of 1,960 candidates, forever.
    """

    _capture(conn, issued="2026-06-30T22:00:00Z", tx_c=31.0)
    project_taf_point_member_runs(conn)
    stored_before = _stored(conn)

    # A later issuance for the same scope: different content, different id.
    _capture(conn, issued="2026-06-30T23:00:00Z", tx_c=32.0)
    conn.insert_attempts = 0
    receipt = project_taf_point_member_runs(conn)

    assert receipt["candidate_count"] == 1
    assert receipt["already_stored_count"] == 1
    assert receipt["attempted_count"] == 0
    assert conn.insert_attempts == 0
    # ...and the archive is untouched, which is the rule, not a side effect.
    assert _stored(conn) == stored_before


def test_the_counts_account_for_every_candidate(
    conn: sqlite3.Connection,
) -> None:
    """A receipt whose parts do not sum is how "wrote 0" got read as "had
    nothing to do" for as long as it did."""

    _capture(conn, issued="2026-06-30T22:00:00Z", tx_c=31.0)
    receipt = project_taf_point_member_runs(conn)
    assert (
        receipt["already_stored_count"] + receipt["attempted_count"]
        == receipt["candidate_count"]
    )
