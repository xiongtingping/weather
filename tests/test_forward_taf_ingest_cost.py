"""The TAF ingest's cost must follow the work, not the window.

``probability_p0m`` was 830.4s of a 4284.6s cycle on 2026-09-07 -- the
largest stage in the cycle -- and no profile had ever said which of its
four pieces that was, because three of them had no span.  What the pieces
did have was a size that nobody reported: the P0-M scope set went 168
scopes (2026-08-22) to 389 (2026-09-01) to 498 (2026-09-07) as the forward
inbox filled to its 2048-capture ceiling and came to span thirteen event
days, 447 of which are closed.

Inside the ingest, four SQL statements ran per capture.  Three could not
vary with the capture at all -- the registry's row count, whether the
market registry table exists -- and the fourth varies only by
(station, event_date), of which 2048 captures hold about 500 distinct
pairs.  Nothing in that loop writes, so no answer could change while it
ran.  Measured warm against the 38GB production database, the DISTINCT
scan alone was 5.4ms: 11.1s a cycle for an answer already computed.

These tests pin both halves: the outcomes are unchanged, and the query
count now follows the distinct scopes rather than the window.
"""

from __future__ import annotations

import sqlite3

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.native_ensemble_training_facts import (
    ingest_forward_taf_inbox,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from tests.pr12_atomic_test_support import install_canonical_v12


class _CountingConnection(sqlite3.Connection):
    """Counts statements by prefix, so a hoist can be proven, not asserted."""

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        counts = getattr(self, "statement_counts", None)
        if counts is None:
            counts = {}
            self.statement_counts = counts
        key = " ".join(str(sql).split())[:64]
        counts[key] = counts.get(key, 0) + 1
        return super().execute(sql, parameters)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", factory=_CountingConnection)
    apply_production_ssot_schema(conn)
    install_canonical_v12(conn)
    for station, city in (("ZUUU", "Chengdu"), ("ZSPD", "Shanghai")):
        conn.execute(
            "INSERT INTO calibration_station_registry("
            "station_id,icao_code,city_label,timezone_name,source_lane,"
            "settlement_definition,registry_version,source_role,content_sha256"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                station, station, city, "Asia/Shanghai", "AWC",
                "daily_high_local_midnight_to_midnight", "v1",
                "official", station.lower().ljust(64, "e")[:64],
            ),
        )
    conn.statement_counts = {}
    return conn


def _capture(station: str, day: str, sequence: int) -> dict:
    revision = {
        "schema": "bayes_basket_forecast_revision_v1",
        "revision_id": f"taf-{station.lower()}-{day}-{sequence}",
        "station_id": station,
        "event_date": day,
        "revision_kind": "TAF",
        "sequence": sequence,
        "issued_at_utc": "2026-08-19T09:00:00Z",
        "received_at_utc": "2026-08-19T09:05:00Z",
        "valid_from_utc": "2026-08-19T09:00:00Z",
        "valid_until_utc": "2026-08-20T18:00:00Z",
        "source_object_sha256": f"{sequence:064d}",
        "supersedes_revision_id": (
            None if sequence == 1
            else f"taf-{station.lower()}-{day}-{sequence - 1}"
        ),
    }
    identity = {
        "schema": "bayes_basket_target_forward_taf_capture_v1",
        "source_class": "official_forward_native_receipt",
        "station_id": station,
        "event_date": day,
        "settlement_timezone": "Asia/Shanghai",
        "forecast_revision": revision,
        "taf_fields": {
            "tx_c": 34.0,
            "cloud_cover_fraction": 0.25,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
        },
        "boundary": {
            "native_local_receipt": True,
            "retrospective_replay_only": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _inbox(captures: list[dict]) -> dict:
    return {
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": captures,
    }


DAYS = ("2026-08-20", "2026-08-21", "2026-08-22")
STATIONS = ("ZUUU", "ZSPD")


def _wide_inbox() -> dict:
    # Six distinct scopes, five issuances each: the real inbox's shape,
    # where captures far outnumber the scopes they belong to.
    return _inbox([
        _capture(station, day, sequence)
        for station in STATIONS
        for day in DAYS
        for sequence in range(1, 6)
    ])


def test_every_capture_is_still_written(tmp_path) -> None:
    """The hoist must change what it costs, not what it decides."""

    conn = _conn()
    receipt = ingest_forward_taf_inbox(conn, _wide_inbox())
    assert receipt["state"] == "COMMITTED"
    assert receipt["capture_count"] == 30
    assert receipt["wrote_count"] == 30
    assert conn.execute(
        "SELECT COUNT(DISTINCT station_id || '|' || event_date)"
        " FROM calibration_forward_taf_capture"
    ).fetchone()[0] == len(STATIONS) * len(DAYS)

    # ...and a replay of the same inbox still writes nothing.
    replay = ingest_forward_taf_inbox(conn, _wide_inbox())
    assert replay["wrote_count"] == 0


def test_the_invariant_lookups_are_asked_once_for_the_whole_inbox() -> None:
    """Thirty captures, one registry count, one schema probe.

    Without this the ingest's cost tracks the rolling window's size, which
    is how a stage that does the same work every cycle got three times more
    expensive without anything about the work changing.
    """

    conn = _conn()
    ingest_forward_taf_inbox(conn, _wide_inbox())
    counts = conn.statement_counts

    def count(prefix: str) -> int:
        return sum(
            value for key, value in counts.items() if key.startswith(prefix)
        )

    assert count("SELECT COUNT(*) FROM calibration_station_registry") == 1
    assert count("SELECT 1 FROM sqlite_master WHERE type='table'") == 1
    # Per station, not per capture: two stations, thirty captures.
    assert count(
        "SELECT 1 FROM calibration_station_registry WHERE station_id=?"
    ) == len(STATIONS)
    # This schema carries no market registry, so the timezone lookup is
    # skipped entirely -- which is itself the point of hoisting the
    # sqlite_master probe: it used to be asked thirty times to reach the
    # same conclusion.
    assert count("SELECT DISTINCT settlement_timezone") == 0


def _install_market_registry(conn: sqlite3.Connection) -> None:
    """Only the three columns this lookup reads, and only for this test."""

    conn.execute(
        "CREATE TABLE basket_target_market_registry ("
        " station_id TEXT, event_date TEXT, settlement_timezone TEXT)"
    )
    for station in STATIONS:
        for day in DAYS:
            conn.execute(
                "INSERT INTO basket_target_market_registry("
                "station_id,event_date,settlement_timezone) VALUES(?,?,?)",
                (station, day, "Asia/Shanghai"),
            )


def test_the_timezone_lookup_follows_the_scopes_not_the_captures() -> None:
    """Six scopes, thirty captures, six lookups.

    The DISTINCT scan measured 5.4ms warm against the production database.
    Once per capture that is 11.1s a cycle at the inbox's 2048 ceiling, for
    an answer already computed -- and it grows with the window, not with
    the work, which is what made this stage look like a regression.
    """

    conn = _conn()
    _install_market_registry(conn)
    conn.statement_counts = {}
    receipt = ingest_forward_taf_inbox(conn, _wide_inbox())
    assert receipt["wrote_count"] == 30
    lookups = sum(
        value for key, value in conn.statement_counts.items()
        if key.startswith("SELECT DISTINCT settlement_timezone")
    )
    assert lookups == len(STATIONS) * len(DAYS)



def _refused_alone(conn, receipt, scope, reason) -> None:
    """The guard still refuses the scope, and only that scope (2026-09-23):
    a refusal used to raise and stop P0-M for every station."""

    refused = {(r["station_id"], r["event_date"], r["reason"]) for r in receipt["rejected_captures"]}
    assert refused == {(*scope, reason)}
    assert receipt["rejected_count"] == 5
    assert receipt["wrote_count"] == len(STATIONS) * len(DAYS) * 5 - 5
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_forward_taf_capture WHERE station_id=? AND event_date=?",
        scope,
    ).fetchone()[0] == 0


def test_a_market_scope_mismatch_is_still_refused() -> None:
    """The cached answer must still be able to say no."""

    conn = _conn()
    _install_market_registry(conn)
    conn.execute(
        "UPDATE basket_target_market_registry SET settlement_timezone=?"
        " WHERE station_id=? AND event_date=?",
        ("America/New_York", "ZSPD", DAYS[1]),
    )
    receipt = ingest_forward_taf_inbox(conn, _wide_inbox())
    _refused_alone(conn, receipt, ("ZSPD", DAYS[1]), "forward_taf_market_scope_invalid")


def test_an_unregistered_station_is_still_refused() -> None:
    """The guard the per-station cache replaced still has to fire, and for
    a station the cache has never seen as well as one it has."""

    conn = _conn()
    receipt = ingest_forward_taf_inbox(
        conn, _inbox([_capture("ZUUU", "2026-08-20", 1),
                      _capture("KJFK", "2026-08-20", 1)])
    )
    assert receipt["wrote_count"] == 1
    assert [(r["station_id"], r["reason"]) for r in receipt["rejected_captures"]] == [
        ("KJFK", "forward_taf_station_unregistered"),
    ]
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_forward_taf_capture WHERE station_id='KJFK'"
    ).fetchone()[0] == 0


def test_a_cached_timezone_is_not_shared_across_scopes() -> None:
    """The cache is keyed by (station, event_date).  Keyed by station it
    would let one day's market scope vouch for another's."""

    conn = _conn()
    receipt = ingest_forward_taf_inbox(conn, _wide_inbox())
    assert receipt["wrote_count"] == 30
    rows = conn.execute(
        "SELECT DISTINCT station_id, event_date"
        " FROM calibration_forward_taf_capture ORDER BY 1,2"
    ).fetchall()
    assert [tuple(row) for row in rows] == sorted(
        (station, day) for station in STATIONS for day in DAYS
    )


def test_the_ingest_reports_where_its_own_time_went() -> None:
    """One name covered three phases, and the first fix chosen by reading
    the code was wrong about which one was expensive.

    ``p0m_taf_fact_ingress`` measured 68.0s of a 620s cycle on 2026-09-07
    and the profile could say no more than that.  A receipt that splits
    its own time costs one clock read per phase and ends the guessing.
    """

    conn = _conn()
    receipt = ingest_forward_taf_inbox(conn, _wide_inbox())
    phases = receipt["phase_ms"]
    # No projection phase: the TAF member lane was retired (charter 133).
    assert set(phases) == {"validate", "capture_insert"}
    assert all(isinstance(value, float) for value in phases.values()), phases
    assert all(value >= 0.0 for value in phases.values()), phases


def _install_snapshotted_market_registry(conn: sqlite3.Connection, zone_for) -> None:
    """The registry as production holds it: rows under content-addressed snapshots."""

    conn.execute(
        "CREATE TABLE basket_target_market_registry_snapshot (snapshot_sha256 TEXT PRIMARY KEY)"
    )
    conn.execute(
        "CREATE TABLE basket_target_market_registry ("
        " snapshot_sha256 TEXT, station_id TEXT, event_date TEXT, settlement_timezone TEXT)"
    )
    for sha in ("s1", "s2"):
        conn.execute("INSERT INTO basket_target_market_registry_snapshot VALUES(?)", (sha,))
        for station in STATIONS:
            for day in DAYS:
                conn.execute(
                    "INSERT INTO basket_target_market_registry VALUES(?,?,?,?)",
                    (sha, station, day, zone_for(sha, station, day)),
                )


def test_a_snapshotted_registry_answers_without_a_per_scope_distinct() -> None:
    """2026-09-14: ~1,500 rows a scope across retained snapshots, 457 scopes,
    23.7s a cycle.  The same answer is read snapshot by snapshot."""

    conn = _conn()
    _install_snapshotted_market_registry(conn, lambda *_: "Asia/Shanghai")
    conn.statement_counts = {}
    receipt = ingest_forward_taf_inbox(conn, _wide_inbox())
    assert receipt["wrote_count"] == 30
    assert not any(
        key.startswith("SELECT DISTINCT settlement_timezone")
        for key in conn.statement_counts
    ), conn.statement_counts


def test_a_snapshotted_registry_still_refuses_a_scope_it_names_two_ways() -> None:
    conn = _conn()
    _install_snapshotted_market_registry(
        conn,
        lambda sha, station, day: (
            "America/New_York" if (sha, station, day) == ("s2", "ZSPD", DAYS[1]) else "Asia/Shanghai"
        ),
    )
    receipt = ingest_forward_taf_inbox(conn, _wide_inbox())
    _refused_alone(conn, receipt, ("ZSPD", DAYS[1]), "forward_taf_market_scope_invalid")
